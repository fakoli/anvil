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
        "roots", "reconcile", "--request-id", "request-one", "--request-digest", claimed["request_digest"],
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
        request_digest_value = reservation["digest"]
    cancelled = runner.invoke(app, ["roots", "reconcile", "--request-id", "blocked-before-prelog", "--request-digest", request_digest_value, "--actor", "owner", "--cancel-if-no-claim", "--cwd", str(repo), "--json"], catch_exceptions=False)
    assert cancelled.exit_code == 0, cancelled.output
