"""Prepared root profiles and immutable proof import retain owner custody."""
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from anvil.cli._helpers import _open_backend
from anvil.roots import registry as root_registry
from anvil.roots.registry import RootSetRegistry
from anvil.state.sqlite import SqliteBackend
from tests.test_profile_cli import _git, _prepared
from tests.test_profile_planning_cli import _invoke
from tests.test_root_set_evidence import _repo

pytestmark = pytest.mark.skipif(
    root_registry.fcntl is None, reason="POSIX owner registry requires flock",
)

def _roots(tmp_path, monkeypatch, *, workspace=False, profiled=True):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    primary, state, _ = _prepared(tmp_path, monkeypatch, workspace=workspace, profiled=profiled)
    secondary = _repo(tmp_path / "library")
    backend = _open_backend(state, project_root=primary)
    try:
        commands = backend.get_task("T001").verification.commands
    finally:
        backend.close()
    for name, path, allowed in (("app", primary, commands), ("library", secondary, ["python -m unittest"])):
        args = ["roots", "enroll", "--repository-id", name, "--path", str(path),
                "--origin", f"local:{name}", "--json"]
        for command in allowed:
            args += ["--verification-command", command]
        _invoke(primary, args, explicit=False)
    request = {"schema": "anvil.root-set-request/v1", "request_id": "profile-roots",
               "primary_root_id": "app", "roots": [
                   {"root_id": "app", "repository_id": "app", "path": str(primary),
                    "expected_files": ["src/feature.txt"], "verification_commands": commands},
                   {"root_id": "library", "repository_id": "library", "path": str(secondary),
                    "expected_files": [], "verification_commands": ["python -m unittest"]}]}
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    return primary, state, path, commands


def _claim(primary, request, *, expected=0):
    result = _invoke(primary, ["roots", "claim", "T001", "--request-file", str(request),
                              "--actor", "root-author", "--json"], expected=expected)
    return json.loads(result.output)


@pytest.mark.parametrize("workspace", [False, True])
@pytest.mark.parametrize("canonical_drift", [False, True])
def test_profile_roots_validate_actual_prepared_primary(tmp_path, monkeypatch, workspace, canonical_drift):
    primary, state, request, commands = _roots(tmp_path, monkeypatch, workspace=workspace)
    original = (primary / "verify.py").read_bytes()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("ANVIL_ROOT", str(elsewhere))
    if canonical_drift:
        (primary / "verify.py").write_text("uncommitted canonical drift\n")
    data = _claim(primary, request)["data"]
    backend = _open_backend(state, project_root=primary)
    try:
        claim = backend.get_claim(data["claim_id"])
        target = Path(claim.git_metadata.target_path)
        assert target != primary and (target / "verify.py").read_bytes() == original
        assert claim.root_set.primary_root_id == "app"
        assert claim.root_set.root_facts[0].verification_commands == tuple(commands)
        assert claim.root_set.root_facts[1].verification_commands == ("python -m unittest",)
        assert backend.get_task("T001").status.value == "claimed"
    finally:
        backend.close()
    with RootSetRegistry().locked() as registry:
        assert registry["reservations"]["profile-roots"]["claim_id"] == data["claim_id"]


def test_primary_target_drift_refuses_claim_and_preserves_prepared_custody(tmp_path, monkeypatch):
    import anvil.git_ops as git_ops

    primary, state, request, _ = _roots(tmp_path, monkeypatch)
    before = (state / "events.jsonl").read_bytes()
    original = git_ops.apply_claim_plan
    targets = []

    def change_prepared(plan, **kwargs):
        result = original(plan, **kwargs)
        target = Path(plan.target_path)
        targets.append(target)
        if (target / "verify.py").exists():
            (target / "verify.py").write_text("prepared target changed\n")
        return result

    monkeypatch.setattr(git_ops, "apply_claim_plan", change_prepared)
    result = _claim(primary, request, expected=1)
    assert result["error"]["code"] == "root_set_provision_failed"
    assert (state / "events.jsonl").read_bytes() == before
    backend = _open_backend(state, project_root=primary)
    try:
        assert backend.list_active_claims() == []
    finally:
        backend.close()
    assert len(targets) == 2 and all(path.exists() for path in targets)
    with RootSetRegistry().locked() as registry:
        reservation = registry["reservations"]["profile-roots"]
        assert reservation["state"] != "released" and reservation["claim_id"] is None


@pytest.mark.parametrize("workspace", [False, True])
def test_root_postlog_failure_retains_recoverable_targets_and_uncertain_custody(
    tmp_path, monkeypatch, workspace,
):
    primary, state, request, _ = _roots(tmp_path, monkeypatch, workspace=workspace)
    original = (primary / "verify.py").read_bytes()
    (primary / "verify.py").write_text("canonical drift survives recovery\n")
    canonical = (primary / "verify.py").read_bytes()
    log = state / "events.jsonl"
    before = log.read_bytes()
    published_claims = []

    def refuse_projection(backend, conn, payload, event):
        assert backend._append_lock_depth > 0
        appended = [json.loads(line) for line in log.read_bytes()[len(before):].splitlines()]
        assert len(appended) == 1 and appended[0]["id"] == event.id
        assert appended[0]["action"] == "claim.created"
        assert len(payload.root_set.root_facts) == 2
        published_claims.append(payload.id)
        raise sqlite3.OperationalError("actual post-log projection failure")

    with monkeypatch.context() as injection:
        injection.setattr(SqliteBackend, "_write_claim_created", refuse_projection)
        failure = _claim(primary, request, expected=1)
    assert failure["error"]["code"] == "root_set_provision_failed"
    published = log.read_bytes()
    assert published != before and published.startswith(before)
    with RootSetRegistry().locked() as registry:
        pending = dict(registry["reservations"]["profile-roots"])
    assert pending["state"] == "pending" and pending["state_append_attempted"] is True
    assert pending["claim_id"] is None
    backend = _open_backend(state, project_root=primary)
    try:
        claim = backend.get_claim(published_claims[0])
        assert claim.status.value == "active" and backend.get_task("T001").status.value == "claimed"
        assert claim.root_set.reservation_id == pending["reservation_id"]
        facts = claim.root_set.root_facts
        for fact in facts:
            assert Path(fact.claim_worktree).is_dir()
            assert fact.claim_worktree != fact.canonical_root
            assert _git(Path(fact.claim_worktree), "rev-parse", "HEAD") == fact.baseline_sha
            assert _git(Path(fact.canonical_root), "rev-parse", "--verify", "refs/heads/" + fact.branch) == fact.baseline_sha
        assert (Path(claim.git_metadata.target_path) / "verify.py").read_bytes() == original
    finally:
        backend.close()
    assert log.read_bytes() == published
    result = _invoke(primary, ["roots", "reconcile", "--request-id", "profile-roots",
                              "--request-digest", pending["digest"], "--actor", "root-author",
                              "--cancel-if-no-claim", "--json"], expected=1)
    assert json.loads(result.output)["error"]["code"] == "root_set_reconciliation_required"
    with RootSetRegistry().locked() as registry:
        assert registry["reservations"]["profile-roots"] == pending
    assert log.read_bytes() == published
    assert (primary / "verify.py").read_bytes() == canonical
    assert all(Path(fact.claim_worktree).is_dir() for fact in facts)


@pytest.mark.parametrize("late_change", [False, True])
def test_root_proof_buffer_is_inspected_under_state_lock_and_rechecked(tmp_path, monkeypatch, late_change):
    import anvil.claims.evidence_import as evidence_import

    primary, state, request, commands = _roots(tmp_path, monkeypatch, profiled=False)
    data = _claim(primary, request)["data"]
    manifest = {"schema": "anvil.root-set-evidence/v1", "submission_id": "buffer-lock",
                "serving_manifest_digest": "a" * 64, "roots": [
                    {"root_id": item["root_id"], "baseline_sha": item["baseline_sha"],
                     "artifact_digest": hashlib.sha256(b"").hexdigest(), "verification_digest": "c" * 64,
                     "commands": item["verification_commands"], "files": []}
                    for item in data["roots"]]}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    buffer = state / ".evidence-buffer" / f"{data['claim_id']}.json"
    before = (state / "events.jsonl").read_bytes()
    original_inspect, original_append = evidence_import.inspect_command_buffer, SqliteBackend.append
    locked, captured_backend = [], []

    def inspect(*args, **kwargs):
        locked.append(captured_backend[-1]._append_lock_depth > 0)
        return original_inspect(*args, **kwargs)

    def append(backend, draft, **kwargs):
        if draft.action == "evidence.submitted":
            callback = kwargs["pre_log_check"]
            if late_change:
                def change_buffer():
                    buffer.parent.mkdir(exist_ok=True)
                    buffer.write_text("late malformed record\n")
                    callback()
                kwargs["pre_log_check"] = change_buffer
        return original_append(backend, draft, **kwargs)

    import anvil.cli.roots as roots
    original_open = roots._open_backend

    def opened(*args, **kwargs):
        backend = original_open(*args, **kwargs)
        captured_backend.append(backend)
        return backend

    monkeypatch.setattr(roots, "_open_backend", opened)
    monkeypatch.setattr(evidence_import, "inspect_command_buffer", inspect)
    monkeypatch.setattr(SqliteBackend, "append", append)
    result = _invoke(primary, ["roots", "submit-evidence", "T001", "--request-file", str(request),
                               "--manifest-file", str(path), "--actor", "root-author", "--json"],
                     expected=1 if late_change else 0)
    assert locked and all(locked)
    if late_change:
        assert "command_proof" in result.output
        assert (state / "events.jsonl").read_bytes() == before
        assert buffer.read_text() == "late malformed record\n"
    else:
        assert json.loads(result.output)["data"]["status"] == "submitted"
