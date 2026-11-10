"""Real command captures retain bundle member custody through replay."""
from __future__ import annotations

import base64
import hashlib
import json
import shlex
import subprocess
import sys
from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

from anvil.bundles.execution import bundle_execution_scope_digest
from anvil.bundles.manager import BundleManager
from anvil.claims.command_proof_artifact import (
    claim_command_cwd_identity,
    load_claim_command_proof,
    verify_claim_command_proof_batch,
)
from anvil.cli import app
from anvil.cli._helpers import _resolve_state_dir, prd_source_path
from anvil.clock import SystemClock
from anvil.state.backend import EventRejected
from anvil.state.hashing import canonical_json_bytes
from anvil.state.models import Event, EventDraft, task_snapshot_revision
from anvil.state.snapshot import serialize_state
from anvil.state.sqlite import SqliteBackend
from tests.test_bundle_state import _create_bundle, _event, _seed


@pytest.fixture
def project(git_repo, tmp_path):
    state = _resolve_state_dir(git_repo)
    state.mkdir(parents=True)
    events = state / "events.jsonl"
    events.touch()
    backend = SqliteBackend(db_path=str(state / "state.db"), events_path=str(events),
                            clock=SystemClock(), project_root=str(git_repo))
    backend.initialize()
    _seed(backend)
    source = prd_source_path(state, "release")
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(backend.get_prd("release").source_bytes)
    command = shlex.join([sys.executable, "-c", "print('bundle-proof-ok')"])
    for task in backend.list_tasks():
        payload = {**task.model_dump(mode="json"), "prd_id": task.prd_id}
        payload["verification"] = {"commands": [command], "required_proofs": [
            {"kind": "command", "command": command, "passing_exit_codes": [0], "label": "real command"},
        ]}
        backend.append(_event("task.created", payload, target_kind="task", target_id=task.id))
    _create_bundle(backend)
    manager = BundleManager(backend, SystemClock(), actor="codex-main", project_root=git_repo)
    try:
        yield backend, manager, git_repo, events, command
    finally:
        backend.close()


def _claim(project):
    backend, manager, *_ = project
    return manager.claim("B001", expected_scope_digest=bundle_execution_scope_digest(backend, "B001"))


def _capture(project, claimed):
    backend, _, repo, _, command = project
    task = backend.get_task("release:T001")
    claim = backend.get_claim(claimed.claim.member_claim_ids[task.id])
    context = claim.attestation_context
    assert context is not None
    started = datetime.now(UTC)
    run = subprocess.run(shlex.split(command), cwd=repo, capture_output=True, timeout=10)
    ended = datetime.now(UTC)
    assert run.returncode == 0
    output = run.stdout + run.stderr
    core = {
        "schema_version": 1, "project_id": "proj", "claim_id": claim.id,
        "generation": claim.generation, "claimed_by": claim.claimed_by, "task_id": task.id,
        "task_revision": context.task_revision, "prd_id": context.prd_id,
        "prd_revision": context.prd_revision, "repository_id": context.repository_id,
        "claim_start_sha": context.claim_start_sha, "cwd_relative": ".",
        "cwd_identity": claim_command_cwd_identity(repo, context.repository_id, "."),
        "command_base64": base64.b64encode(command.encode()).decode(),
        "started_at": started.isoformat().replace("+00:00", "Z"),
        "ended_at": ended.isoformat().replace("+00:00", "Z"),
        "exit_code": run.returncode, "output_base64": base64.b64encode(output).decode(),
        "output_sha256": hashlib.sha256(output).hexdigest(),
    }
    raw = canonical_json_bytes({"envelope_id": "real-bundle-capture", "payload": core,
                                "issuer": None})
    proofs = verify_claim_command_proof_batch(
        [load_claim_command_proof(raw)], claim=claim, task=task, project_id="proj",
        project_root=repo, actor=claim.claimed_by, declared_commands=[command],
        now=datetime.now(UTC),
    )
    return EventDraft(timestamp=datetime.now(UTC), actor=claim.claimed_by,
                      action="evidence.submitted", target_kind="task", target_id=task.id,
                      payload_json={"task_id": task.id, "claim_id": claim.id,
                                    "submitted_by": claim.claimed_by, "evidence_id": "EV-REAL",
                                    "commands_run": [command], "files_changed": [],
                                    "proofs": [proof.model_dump(mode="json") for proof in proofs]})


def test_bundle_command_proof_intake_and_replay(project):
    backend, manager, _, events, _ = project
    claimed = _claim(project)
    contexts = [backend.get_claim(cid).attestation_context
                for cid in claimed.claim.member_claim_ids.values()]
    assert all(context is not None for context in contexts)
    assert len({context.task_revision for context in contexts}) == 2
    packet = manager.packet("B001").json_data
    for member in packet["members"]:
        retained = backend.get_claim(member["member_claim_id"])
        assert member["member_claim"] == retained.model_dump(mode="json")
        assert member["member_claim"]["attestation_context"] is not None
    backend.append(_capture(project, claimed))
    assert backend.get_task("release:T001").status.value == "needs_review"
    assert backend.get_task("release:T002").status.value == "claimed"
    evidence = backend.get_latest_evidence("release:T001")
    assert len(evidence.proofs) == 1
    assert evidence.proofs[0].evidence_core.claim_id == claimed.claim.member_claim_ids["release:T001"]
    before = serialize_state(backend)
    backend.replay_from_empty(str(events))
    assert serialize_state(backend) == before


def test_cli_imports_real_bundle_member_command_proof(project, tmp_path):
    backend, _, repo, _, command = project
    draft = _capture(project, _claim(project))
    proof_file = tmp_path / "captured-command.json"
    proof = draft.payload_json["proofs"][0]
    proof_file.write_bytes(canonical_json_bytes({
        "envelope_id": "cli-capture", "payload": proof["evidence_core"], "issuer": proof["issuer"],
    }))
    result = CliRunner().invoke(app, [
        "submit", "release:T001", "--cwd", str(repo), "--actor", "codex-main",
        "--commands", command, "--files-changed", "",
        "--command-proof-file", str(proof_file), "--json",
    ])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["missing_claim_bound_proofs"] == []
    assert backend.get_task("release:T001").status.value == "needs_review"


@pytest.mark.parametrize("change", ["actor", "sibling", "released"])
def test_bundle_proof_cannot_escape_coordinator_custody(project, change):
    backend, manager, _, events, _ = project
    claimed = _claim(project)
    draft = _capture(project, claimed)
    if change == "actor":
        draft.actor = "delegate"
        draft.payload_json["submitted_by"] = "delegate"
    elif change == "sibling":
        draft.target_id = "release:T002"
        draft.payload_json["task_id"] = "release:T002"
        draft.payload_json["claim_id"] = claimed.claim.member_claim_ids["release:T002"]
    else:
        manager.release("B001", reason="replan")
    before = events.read_bytes()
    with pytest.raises(EventRejected, match="claim-bound command proof"):
        backend.append(draft)
    assert events.read_bytes() == before
    assert backend.get_latest_evidence("release:T001") is None


@pytest.mark.parametrize("change", [
    "generation", "prd_revision", "paths", "zero_task_revision",
    "sibling_task_revision", "stale_task_content",
])
def test_member_context_tampering_refuses_live_and_replay(project, monkeypatch, change):
    backend, _, _, events, _ = project
    captured = []
    original = backend.append

    def capture(draft, **kwargs):
        if draft.action == "bundle.claimed":
            captured.append(draft.model_copy(deep=True))
            raise EventRejected("capture only")
        return original(draft, **kwargs)

    monkeypatch.setattr(backend, "append", capture)
    with pytest.raises(Exception, match="capture only"):
        _claim(project)
    monkeypatch.setattr(backend, "append", original)
    draft = captured[0]
    member = draft.payload_json["member_claims"][0]
    if change == "generation":
        member["generation"] += 1
    elif change == "prd_revision":
        member["attestation_context"]["prd_revision"] += 1
    elif change == "paths":
        member["attestation_context"]["expected_paths"] = [{"path": "forged.py"}]
    elif change == "zero_task_revision":
        member["attestation_context"]["task_revision"] = "0" * 64
    elif change == "sibling_task_revision":
        member["attestation_context"]["task_revision"] = (
            draft.payload_json["member_claims"][1]["attestation_context"]["task_revision"]
        )
    else:
        stale_task = backend.get_task(member["task_id"]).model_copy(
            update={"title": "Old task content"},
        )
        member["attestation_context"]["task_revision"] = task_snapshot_revision(stale_task)
    before = events.read_bytes()
    with pytest.raises(EventRejected, match="context or generation mismatch"):
        backend.append(draft)
    assert events.read_bytes() == before
    lines = before.decode().splitlines()
    forged = Event(id=f"E{len(lines) + 1:06d}", **draft.model_dump())
    with events.open("a") as stream:
        stream.write(forged.model_dump_json() + "\n")
    backend.replay_from_empty(str(events))
    assert backend.get_bundle_claim("B001") is None
    assert backend.list_active_claims() == []
    assert all(task.status.value == "ready" for task in backend.list_tasks())
