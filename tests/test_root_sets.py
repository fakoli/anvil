"""Disposable CLI coverage for owner-global multi-root reservations."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from anvil.cli import app
from anvil.roots import registry as root_registry
from anvil.roots.registry import (
    RootSetError,
    RootSetRegistry,
    authorize_bound_root_set_claim,
    authorize_root_set_claim,
    request_digest,
    root_set_claim_authorized,
    root_set_use_authorized,
)
from anvil.state.models import RootSetClaimBinding, RootSetRootFact

runner = CliRunner()


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


def _repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "test@example.invalid")
    _git(path, "config", "user.name", "Anvil Test")
    (path / "README.md").write_text("initial\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-qm", "initial")
    return path


def _json(result):
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


@pytest.fixture
def idle_enrollment(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path / "owner")
    monkeypatch.chdir(repo)
    args = ["roots", "enroll", "--repository-id", "owner", "--path", str(repo),
            "--origin", "local:owner", "--verification-command", "pytest old.py", "--json"]
    _json(runner.invoke(app, args, catch_exceptions=False))
    return repo, args, RootSetRegistry()


def test_explicit_idle_verification_replacement_is_exact_and_idempotent(idle_enrollment):
    _repo_path, args, registry = idle_enrollment
    original = registry.path.read_bytes()
    replacement = ["pytest next.py", "python -m unittest tests.next"]
    changed = args.copy()
    changed[changed.index("pytest old.py")] = replacement[0]
    refused = runner.invoke(app, changed, catch_exceptions=False)
    assert refused.exit_code == 1 and registry.path.read_bytes() == original
    changed += ["--verification-command", replacement[1], "--replace-verification-policy"]
    _json(runner.invoke(app, changed, catch_exceptions=False))
    updated = registry.path.read_bytes()
    expected = json.loads(original)
    expected["repositories"]["owner"]["verification_commands"] = replacement
    assert json.loads(updated) == expected
    _json(runner.invoke(app, changed, catch_exceptions=False))
    assert registry.path.read_bytes() == updated
    manifest = _json(runner.invoke(app, ["describe", "--json"], catch_exceptions=False))["data"]
    assert "--replace-verification-policy" in manifest["cli"]["options"]["roots enroll"]


@pytest.mark.parametrize("state", ["pending", "bound", "release_pending"])
def test_verification_replacement_refuses_every_reservation(idle_enrollment, state):
    repo, args, registry = idle_enrollment
    with registry.locked() as data:
        reservation = registry.reserve(data, request_id="old-use", digest="a" * 64,
            actor="owner", state_identity=str(repo / ".anvil"),
            roots=[{"root_id": "owner", "repository_id": "owner", "path": str(repo)}])
        reservation.update(state=state, created_at=0, claim_id="COLD")
    before = registry.path.read_bytes()
    refused = runner.invoke(app, [*args, "--replace-verification-policy"], catch_exceptions=False)
    assert refused.exit_code == 1
    assert json.loads(refused.output)["error"]["code"] == "root_set_conflict"
    assert registry.path.read_bytes() == before


@pytest.mark.parametrize("fault", ["unknown", "origin", "new_alias", "empty", "oversize"])
def test_verification_replacement_rejects_identity_and_policy_changes(idle_enrollment, tmp_path, fault):
    repo, args, registry = idle_enrollment
    changed = [*args, "--replace-verification-policy"]
    if fault == "unknown":
        changed[changed.index("owner")] = "unknown"
    elif fault == "origin":
        changed[changed.index("local:owner")] = "local:different"
    elif fault == "new_alias":
        alias = tmp_path / "alias"
        _git(repo, "worktree", "add", "-qb", "alias", str(alias))
        changed[changed.index(str(repo))] = str(alias)
    elif fault == "empty":
        index = changed.index("--verification-command")
        del changed[index:index + 2]
    else:
        changed[changed.index("pytest old.py")] = "x" * 1025
    before = registry.path.read_bytes()
    refused = runner.invoke(app, changed, catch_exceptions=False)
    assert refused.exit_code == 1
    assert registry.path.read_bytes() == before


@pytest.mark.parametrize("fault", ["active", "unreadable"])
def test_verification_replacement_checks_all_alias_state_under_owner_lock(idle_enrollment, tmp_path, monkeypatch, fault):
    import fcntl

    from anvil.cli import roots as roots_cli

    repo, args, registry = idle_enrollment
    alias = tmp_path / "alias"
    _git(repo, "worktree", "add", "-qb", "alias", str(alias))
    alias_args = args.copy()
    alias_args[alias_args.index(str(repo))] = str(alias)
    _json(runner.invoke(app, alias_args, catch_exceptions=False))
    states = {root: root / "state-for-test" for root in (repo, alias)}
    for state in states.values():
        state.mkdir()
    monkeypatch.setattr(roots_cli, "_resolve_state_dir", lambda path: states[path])
    visited = []

    def open_backend(path):
        visited.append(path)
        # Another lock descriptor cannot enter while alias State is checked.
        with registry.lock_path.open("a+b") as handle:
            with pytest.raises(BlockingIOError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        if path == states[alias] and fault == "unreadable":
            raise OSError("private diagnostic must not escape")
        return SimpleNamespace(
            list_active_claims=lambda: [object()] if path == states[alias] else [],
            close=lambda: None,
        )

    monkeypatch.setattr(roots_cli, "_open_backend", open_backend)
    before = registry.path.read_bytes()
    refused = runner.invoke(app, [*args, "--replace-verification-policy"], catch_exceptions=False)
    assert refused.exit_code == 1 and visited == list(states.values())
    assert "private diagnostic" not in refused.output
    assert registry.path.read_bytes() == before


def test_disposable_cli_root_claim_conflict_status_and_release(tmp_path, monkeypatch):
    """Two real Git roots receive one canonical State claim and reservation."""
    home = tmp_path / "home"
    app_root, lib_root = _repo(tmp_path / "app"), _repo(tmp_path / "lib")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(app_root)
    initialized = runner.invoke(app, ["init", "--with-sample"], catch_exceptions=False)
    assert initialized.exit_code == 0, initialized.output
    _git(app_root, "add", "-A")
    _git(app_root, "commit", "-qm", "state")

    primary_task = _json(
        runner.invoke(app, ["next", "--cwd", str(app_root), "--json"], catch_exceptions=False)
    )["data"]["task"]
    _json(runner.invoke(app, [
        "roots", "enroll", "--repository-id", "app", "--path", str(app_root),
        "--origin", "local:app", "--verification-command", primary_task["verification"]["commands"][0], "--json",
    ], catch_exceptions=False))
    _json(runner.invoke(app, [
        "roots", "enroll", "--repository-id", "lib", "--path", str(lib_root),
        "--origin", "local:lib", "--verification-command", "python -m pytest -q", "--json",
    ], catch_exceptions=False))
    request = {
        "schema": "anvil.root-set-request/v1",
        "request_id": "request-one",
        "primary_root_id": "app",
        "roots": [
            {"root_id": "app", "repository_id": "app", "path": str(app_root), "expected_files": ["README.md"], "verification_commands": primary_task["verification"]["commands"]},
            {"root_id": "lib", "repository_id": "lib", "path": str(lib_root), "expected_files": [], "verification_commands": ["python -m pytest -q"]},
        ],
    }
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    claimed = _json(runner.invoke(app, [
        "roots", "claim", primary_task["id"], "--request-file", str(request_path),
        "--actor", "root-test", "--cwd", str(app_root), "--json",
    ], catch_exceptions=False))["data"]
    before_lookup = ((app_root / ".anvil" / "events.jsonl").read_bytes(),
                     (app_root / ".anvil" / "state.db").read_bytes(),
                     (home / ".anvil" / "root-sets" / "registry.json").read_bytes(),
                     request_path.read_bytes(),
                     _git(app_root, "rev-parse", "HEAD"),
                     _git(app_root, "status", "--porcelain=v1", "--untracked-files=all"))
    missing_actor = runner.invoke(app, [
        "roots", "request-digest", primary_task["id"], "--request-file", str(request_path),
        "--cwd", str(app_root), "--json",
    ], catch_exceptions=False)
    assert missing_actor.exit_code == 2
    assert "--actor" in missing_actor.output and "Missing option" in missing_actor.output
    recovered_identity = _json(runner.invoke(app, [
        "roots", "request-digest", primary_task["id"], "--request-file", str(request_path),
        "--actor", "root-test", "--cwd", str(app_root), "--json",
    ], catch_exceptions=False))["data"]
    assert recovered_identity == {"schema": "anvil.root-set-request-digest/v1", "request_id": "request-one", "request_digest": claimed["request_digest"]}
    assert before_lookup == (
        (app_root / ".anvil" / "events.jsonl").read_bytes(),
        (app_root / ".anvil" / "state.db").read_bytes(),
        (home / ".anvil" / "root-sets" / "registry.json").read_bytes(),
        request_path.read_bytes(),
        _git(app_root, "rev-parse", "HEAD"),
        _git(app_root, "status", "--porcelain=v1", "--untracked-files=all"),
    )
    drifted_digests = []
    for changed_task, changed_actor, changed_request in (
        ("T999", "root-test", request),
        (primary_task["id"], "other-actor", request),
        (primary_task["id"], "root-test", request | {"roots": [request["roots"][0] | {"expected_files": ["other.py"]}, request["roots"][1]]}),
    ):
        changed_path = tmp_path / f"changed-{len(drifted_digests)}.json"
        changed_path.write_text(json.dumps(changed_request), encoding="utf-8")
        identity = _json(runner.invoke(app, [
            "roots", "request-digest", changed_task, "--request-file", str(changed_path),
            "--actor", changed_actor, "--cwd", str(app_root), "--json",
        ], catch_exceptions=False))["data"]
        assert identity["request_digest"] != recovered_identity["request_digest"]
        drifted_digests.append(identity["request_digest"])
    other_state = _repo(tmp_path / "other-state")
    monkeypatch.chdir(other_state)
    assert runner.invoke(app, ["init", "--with-sample"], catch_exceptions=False).exit_code == 0
    other_identity = _json(runner.invoke(app, [
        "roots", "request-digest", primary_task["id"], "--request-file", str(request_path),
        "--actor", "root-test", "--cwd", str(other_state), "--json",
    ], catch_exceptions=False))["data"]
    assert other_identity["request_digest"] != recovered_identity["request_digest"]
    drifted_digests.append(other_identity["request_digest"])
    for wrong_digest in drifted_digests:
        refused = runner.invoke(app, [
            "roots", "status", "--request-id", "request-one", "--request-digest", wrong_digest,
            "--actor", "root-test", "--cwd", str(app_root), "--json",
        ], catch_exceptions=False)
        assert refused.exit_code == 1
        assert json.loads(refused.output)["error"]["code"] == "root_set_reconciliation_required"
    assert claimed["status"] == "ready" and len(claimed["roots"]) == 2
    assert all(Path(root["claim_worktree"]).is_dir() for root in claimed["roots"])
    assert all(root["baseline_sha"] for root in claimed["roots"])
    # Rebuild the disposable projection while every owner-registry accessor
    # fails. Historical root-set events must validate locally during replay.
    from anvil.clock import SystemClock
    from anvil.state.sqlite import SqliteBackend

    state_dir = app_root / ".anvil"
    (state_dir / "state.db").unlink()
    with monkeypatch.context() as replay_patch:
        replay_patch.setattr(
            RootSetRegistry,
            "locked",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("replay read registry")),
        )
        replay = SqliteBackend(
            db_path=str(state_dir / "state.db"),
            events_path=str(state_dir / "events.jsonl"),
            clock=SystemClock(),
        )
        try:
            replay.initialize()
            replay_claim = replay.get_claim(claimed["claim_id"])
            assert replay_claim is not None
        finally:
            replay.close()
    expired = SimpleNamespace(
        id=replay_claim.id,
        root_set=replay_claim.root_set,
        status=SimpleNamespace(value="active"),
        lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    with pytest.raises(RootSetError, match="live canonical facts"):
        with root_set_use_authorized(
            replay_claim.root_set,
            backend=SimpleNamespace(
                _db_path=str(state_dir / "state.db"),
                get_claim=lambda _claim_id: expired,
            ),
        ):
            pass
    # Healthy nonterminal use is owner-authorized for both a hook append and a
    # packet sidecar write.
    assert runner.invoke(app, [
        "packet", primary_task["id"], "--cwd", str(app_root),
    ], catch_exceptions=False).exit_code == 0
    assert runner.invoke(app, [
        "progress", primary_task["id"], "implementing",
        "--actor", "root-test", "--cwd", str(app_root),
    ], catch_exceptions=False).exit_code == 0

    # A normal --force claim cannot bypass a pending/bound whole-repository lock.
    blocked = runner.invoke(app, [
        "claim", primary_task["id"], "--actor", "other", "--force", "--cwd", str(app_root), "--json",
    ], catch_exceptions=False)
    assert blocked.exit_code == 1
    assert json.loads(blocked.output)["error"]["message"].startswith("root_set_registered:")

    # A supported lost-response replay only binds the already-proven canonical
    # claim after every retained Git target is present.
    registry_path = home / ".anvil" / "root-sets" / "registry.json"
    registry_data = json.loads(registry_path.read_text(encoding="utf-8"))
    event_lines = (app_root / ".anvil" / "events.jsonl").read_text(encoding="utf-8").count("\n")
    tampered = json.loads(json.dumps(registry_data))
    tampered["reservations"]["request-one"]["root_results"][0]["branch"] = "agent/tampered"
    registry_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(RootSetError, match="immutable owner facts"):
        authorize_bound_root_set_claim(
            replay_claim.root_set, tampered["reservations"]["request-one"]
        )
    assert runner.invoke(app, ["packet", primary_task["id"], "--cwd", str(app_root)]).exit_code != 0
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    registry_path.write_text("{", encoding="utf-8")
    # Hooks intentionally preserve their zero exit convention, but direct
    # uncoordinated use must not append. Packets fail before writing a sidecar.
    assert runner.invoke(app, [
        "hook", "record-file-change", "--file", "README.md", "--tool", "Edit",
        "--actor", "root-test", "--cwd", str(app_root),
    ], catch_exceptions=False).exit_code == 0
    assert (app_root / ".anvil" / "events.jsonl").read_text(encoding="utf-8").count("\n") == event_lines
    blocked_packet = runner.invoke(app, [
        "packet", primary_task["id"], "--cwd", str(app_root),
    ])
    assert blocked_packet.exit_code != 0
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    registry_data["reservations"]["request-one"]["state"] = "pending"
    registry_data["reservations"]["request-one"]["claim_id"] = None
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    reconciled = _json(runner.invoke(app, [
        "roots", "reconcile", "--request-id", "request-one", "--request-digest", recovered_identity["request_digest"],
        "--actor", "root-test", "--cwd", str(app_root), "--json",
    ], catch_exceptions=False))["data"]
    assert reconciled["status"] == "ready"

    # Progress is required before the registry records its global extension;
    # then the canonical renewal proceeds while the owner lock remains held.
    assert runner.invoke(app, [
        "hook", "record-file-change", "--file", "README.md", "--tool", "Edit",
        "--actor", "root-test", "--cwd", str(app_root),
    ], catch_exceptions=False).exit_code == 0
    renewed = _json(runner.invoke(app, [
        "renew", claimed["claim_id"], "--actor", "root-test", "--cwd", str(app_root), "--json",
    ], catch_exceptions=False))["data"]
    assert renewed["renewed"] is True
    assert "renewed_at" in json.loads(registry_path.read_text(encoding="utf-8"))["reservations"]["request-one"]

    status = _json(runner.invoke(app, [
        "roots", "status", "--request-id", "request-one", "--request-digest", claimed["request_digest"],
        "--actor", "root-test", "--cwd", str(app_root), "--json",
    ], catch_exceptions=False))["data"]
    assert status["state"] == "bound" and status["claim_id"] == claimed["claim_id"]
    wrong = runner.invoke(app, [
        "roots", "status", "--request-id", "request-one", "--request-digest", "0" * 64,
        "--actor", "root-test", "--cwd", str(app_root), "--json",
    ], catch_exceptions=False)
    assert wrong.exit_code == 1
    assert json.loads(wrong.output)["error"]["code"] == "root_set_reconciliation_required"

    released = _json(runner.invoke(app, [
        "release", claimed["claim_id"], "--actor", "root-test", "--cwd", str(app_root), "--json",
    ], catch_exceptions=False))["data"]
    assert released["released"] is True
    final = _json(runner.invoke(app, [
        "roots", "status", "--request-id", "request-one", "--request-digest", claimed["request_digest"],
        "--actor", "root-test", "--cwd", str(app_root), "--json",
    ], catch_exceptions=False))["data"]
    assert final["state"] == "release_pending"
    # Simulate the durable canonical release winning while the owner journal
    # write failed. Reconcile reconstructs the overhold before confirmation.
    registry_data = json.loads(registry_path.read_text(encoding="utf-8"))
    registry_data["reservations"]["request-one"]["state"] = "pending"
    registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
    confirmed = _json(runner.invoke(app, [
        "roots", "reconcile", "--request-id", "request-one", "--request-digest", claimed["request_digest"],
        "--actor", "root-test", "--confirm-runner-stopped", "--cwd", str(app_root), "--json",
    ], catch_exceptions=False))["data"]
    assert confirmed["state"] == "released"

    previous = json.loads(registry_path.read_text())["reservations"]["request-one"]
    next_commands = ["python -m pytest next.py -q"]
    _json(runner.invoke(app, [
        "roots", "enroll", "--repository-id", "lib", "--path", str(lib_root),
        "--origin", "local:lib", "--replace-verification-policy",
        "--verification-command", next_commands[0], "--json",
    ], catch_exceptions=False))
    next_task = _json(runner.invoke(app, ["show", "T002", "--cwd", str(app_root), "--json"], catch_exceptions=False))["data"]["task"]
    request["request_id"] = "request-two"
    request["roots"][0]["verification_commands"] = next_task["verification"]["commands"]
    request_path.write_text(json.dumps(request))
    refused = runner.invoke(app, [
        "roots", "claim", next_task["id"], "--request-file", str(request_path),
        "--actor", "root-test", "--cwd", str(app_root), "--json",
    ], catch_exceptions=False)
    assert refused.exit_code == 1
    assert json.loads(refused.output)["error"]["code"] == "root_set_authority_lost"
    request["roots"][1]["verification_commands"] = next_commands
    request_path.write_text(json.dumps(request))
    second = _json(runner.invoke(app, [
        "roots", "claim", next_task["id"], "--request-file", str(request_path),
        "--actor", "root-test", "--cwd", str(app_root), "--json",
    ], catch_exceptions=False))["data"]
    assert second["roots"][1]["verification_commands"] == next_commands
    assert json.loads(registry_path.read_text())["reservations"]["request-one"] == previous


def test_activated_corrupt_registry_fails_closed_before_ordinary_claim(tmp_path, monkeypatch):
    home = tmp_path / "home"
    repo = _repo(tmp_path / "repo")
    monkeypatch.setenv("HOME", str(home))
    _json(runner.invoke(app, [
        "roots", "enroll", "--repository-id", "repo", "--path", str(repo),
        "--origin", "local:repo", "--json",
    ], catch_exceptions=False))
    registry = home / ".anvil" / "root-sets" / "registry.json"
    registry.write_text("{", encoding="utf-8")
    monkeypatch.chdir(repo)
    assert runner.invoke(app, ["init", "--with-sample"], catch_exceptions=False).exit_code == 0
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "state")
    task = _json(runner.invoke(app, ["next", "--cwd", str(repo), "--json"], catch_exceptions=False))["data"]["task"]
    result = runner.invoke(app, ["claim", task["id"], "--actor", "blocked", "--cwd", str(repo), "--json"], catch_exceptions=False)
    assert result.exit_code == 1
    assert json.loads(result.output)["error"]["message"].startswith("root_set_registry_unavailable:")


def test_remote_clone_alias_cannot_bypass_an_enrolled_reservation(tmp_path, monkeypatch):
    """A second clone with the enrolled remote is denied before State claims."""
    home = tmp_path / "home"
    enrolled, alias = _repo(tmp_path / "enrolled"), _repo(tmp_path / "alias")
    origin = "https://example.test/owner/project.git"
    _git(enrolled, "remote", "add", "origin", origin)
    _git(alias, "remote", "add", "origin", origin)
    monkeypatch.setenv("HOME", str(home))
    _json(runner.invoke(app, [
        "roots", "enroll", "--repository-id", "project", "--path", str(enrolled),
        "--origin", origin, "--json",
    ], catch_exceptions=False))
    with RootSetRegistry().locked() as registry:
        RootSetRegistry().reserve(
            registry,
            request_id="reserved-clone",
            digest="a" * 64,
            actor="owner",
            state_identity="fixture",
            roots=[{"root_id": "project", "repository_id": "project", "path": str(enrolled)}],
        )
    with pytest.raises(RootSetError, match="enrolled") as exc_info:
        RootSetRegistry().ordinary_claim_allowed(alias)
    assert exc_info.value.code == "root_set_registered"


def test_full_binding_capability_rejects_substituted_root_facts():
    """A matching request triple cannot authorize a changed root set."""
    fact = RootSetRootFact(
        root_id="app", repository_id="app", baseline_sha="a" * 40,
        canonical_root="/workspace/app", claim_worktree="/workspace/wt-app",
        branch="agent/t001", verification_commands=(),
    )
    binding = RootSetClaimBinding(
        request_id="request", primary_root_id="app", request_digest="b" * 64,
        root_set_digest=request_digest({"primary_root_id": "app", "roots": [fact.model_dump(mode="json")]}), reservation_id="R" + "d" * 32,
        root_facts=(fact,),
    )
    reservation = {
        "request_id": "request", "digest": "b" * 64,
        "reservation_id": "R" + "d" * 32, "state": "pending",
    }
    capability = authorize_root_set_claim(binding, reservation)
    forged = binding.model_copy(update={"root_set_digest": "e" * 64})
    assert root_set_claim_authorized(binding, capability)
    assert not root_set_claim_authorized(forged, capability)
    with pytest.raises(ValueError, match="baseline_sha"):
        RootSetRootFact(
            root_id="bad", repository_id="bad", baseline_sha="a" * 41,
            canonical_root="/workspace/bad", claim_worktree="/workspace/wt-bad",
            branch="agent/bad", verification_commands=(),
        )


def test_inactive_registry_preserves_legacy_when_platform_has_no_flock(tmp_path, monkeypatch):
    """Absent activation must not require fcntl or create a user-home registry."""
    repo = _repo(tmp_path / "repo")
    home = tmp_path / "unwritable-home"
    monkeypatch.setattr(root_registry, "fcntl", None)
    RootSetRegistry(home=home).ordinary_claim_allowed(repo)
    assert not home.exists()


@pytest.mark.parametrize("owner_state", ["absent", "empty", "activated"])
def test_enrollment_without_flock_refuses_before_owner_changes(tmp_path, monkeypatch, owner_state):
    repo = _repo(tmp_path / "repo")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("ANVIL_STATE_LAYOUT", "local")
    monkeypatch.chdir(repo)
    initialized = runner.invoke(app, ["init", "--with-sample"], catch_exceptions=False)
    assert initialized.exit_code == 0, initialized.output
    registry = RootSetRegistry()
    assert registry.base == home / ".anvil" / "root-sets"
    if owner_state != "absent":
        registry.base.mkdir(parents=True)
    if owner_state == "activated":
        registry.activation_path.write_text(
            json.dumps({"schema": "anvil.root-set-activation/v1"}) + "\n", encoding="utf-8",
        )
        registry.path.write_text(json.dumps({
            "schema": "anvil.root-set-registry/v1", "repositories": {}, "reservations": {},
        }) + "\n", encoding="utf-8")
        registry.lock_path.write_bytes(b"")

    def snapshot(path):
        if not path.exists():
            return None
        return {
            str(item.relative_to(path)): item.read_bytes() if item.is_file() else None
            for item in path.rglob("*")
        }

    owner_before, project_before = snapshot(registry.base), snapshot(repo)
    monkeypatch.setattr(root_registry, "fcntl", None)
    result = runner.invoke(app, [
        "roots", "enroll", "--repository-id", "repo", "--path", str(repo),
        "--origin", "local:repo", "--json",
    ], catch_exceptions=False)
    assert result.exit_code == 1, result.output
    assert json.loads(result.output)["error"]["code"] == "root_set_unsupported"
    assert snapshot(registry.base) == owner_before
    assert snapshot(repo) == project_before
    if owner_state == "activated":
        with pytest.raises(RootSetError) as error:
            registry.ordinary_claim_allowed(repo)
        assert error.value.code == "root_set_unsupported"
    else:
        registry.ordinary_claim_allowed(repo)
    assert snapshot(registry.base) == owner_before
    assert snapshot(repo) == project_before


def test_only_proven_prelog_no_claim_reservation_can_be_cancelled(tmp_path, monkeypatch):
    """A false append marker cancels; an uncertain marker remains overheld."""
    home = tmp_path / "home"
    repo = _repo(tmp_path / "repo")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(repo)
    assert runner.invoke(app, ["init", "--with-sample"], catch_exceptions=False).exit_code == 0
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "state")
    _json(runner.invoke(app, [
        "roots", "enroll", "--repository-id", "repo", "--path", str(repo),
        "--origin", "local:repo", "--json",
    ], catch_exceptions=False))
    state_dir = repo / ".anvil"
    root = {"root_id": "repo", "repository_id": "repo", "path": str(repo)}
    with RootSetRegistry().locked() as data:
        clean = RootSetRegistry().reserve(
            data, request_id="known-prelog", digest="a" * 64, actor="owner",
            state_identity=str(state_dir.resolve()), roots=[root],
        )
        RootSetRegistry().checkpoint(data)
    cancelled = _json(runner.invoke(app, [
        "roots", "reconcile", "--request-id", "known-prelog", "--request-digest", "a" * 64,
        "--actor", "owner", "--cancel-if-no-claim", "--cwd", str(repo), "--json",
    ], catch_exceptions=False))["data"]
    assert cancelled["state"] == "released" and clean["state_append_attempted"] is False
    with RootSetRegistry().locked() as data:
        uncertain = RootSetRegistry().reserve(
            data, request_id="after-prelog", digest="b" * 64, actor="owner",
            state_identity=str(state_dir.resolve()), roots=[root],
        )
        uncertain["state_append_attempted"] = True
        RootSetRegistry().checkpoint(data)
    refused = runner.invoke(app, [
        "roots", "reconcile", "--request-id", "after-prelog", "--request-digest", "b" * 64,
        "--actor", "owner", "--cancel-if-no-claim", "--cwd", str(repo), "--json",
    ], catch_exceptions=False)
    assert refused.exit_code == 1
    assert json.loads(refused.output)["error"]["code"] == "root_set_reconciliation_required"


def test_readiness_refusal_before_prelog_is_cancellable(tmp_path, monkeypatch):
    """A real ClaimManager readiness gate leaves its owner reservation cancelable."""
    from anvil.cli._helpers import _open_backend, _resolve_state_dir
    from anvil.clock import SystemClock
    from anvil.state.models import EventDraft

    home = tmp_path / "home"
    repo = _repo(tmp_path / "repo")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(repo)
    assert runner.invoke(app, ["init", "--with-sample"], catch_exceptions=False).exit_code == 0
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "state")
    task = _json(runner.invoke(app, ["next", "--cwd", str(repo), "--json"], catch_exceptions=False))["data"]["task"]
    _json(runner.invoke(app, [
        "roots", "enroll", "--repository-id", "repo", "--path", str(repo),
        "--origin", "local:repo", "--verification-command", task["verification"]["commands"][0], "--json",
    ], catch_exceptions=False))
    state_dir = _resolve_state_dir(repo)
    backend = _open_backend(state_dir)
    try:
        now = SystemClock().now()
        backend.append(EventDraft(
            timestamp=now, actor="test", action="task.status_changed", target_kind="task", target_id=task["id"],
            payload_json={"task_id": task["id"], "from": "ready", "to": "blocked", "reason": "fixture"},
        ))
    finally:
        backend.close()
    request = {
        "schema": "anvil.root-set-request/v1", "request_id": "blocked-before-prelog", "primary_root_id": "repo",
        "roots": [{"root_id": "repo", "repository_id": "repo", "path": str(repo), "expected_files": [], "verification_commands": task["verification"]["commands"]}],
    }
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    refused = runner.invoke(app, ["roots", "claim", task["id"], "--request-file", str(request_path), "--actor", "owner", "--cwd", str(repo), "--json"], catch_exceptions=False)
    assert refused.exit_code == 1
    digest = json.loads(refused.output)["error"]
    assert digest["code"] == "root_set_provision_failed"
    with RootSetRegistry().locked() as data:
        reservation = data["reservations"]["blocked-before-prelog"]
        assert reservation["state_append_attempted"] is False
        request_digest_value = _json(runner.invoke(app, ["roots", "request-digest", task["id"], "--request-file", str(request_path), "--actor", "owner", "--cwd", str(repo), "--json"], catch_exceptions=False))["data"]["request_digest"]
    cancelled = runner.invoke(app, ["roots", "reconcile", "--request-id", "blocked-before-prelog", "--request-digest", request_digest_value, "--actor", "owner", "--cancel-if-no-claim", "--cwd", str(repo), "--json"], catch_exceptions=False)
    assert cancelled.exit_code == 0, cancelled.output
