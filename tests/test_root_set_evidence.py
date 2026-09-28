"""CLI-only per-root evidence submission stays bound to one root-set claim."""

from __future__ import annotations

import json
import base64
import hashlib
import subprocess
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from anvil.cli import app
from anvil.cli._helpers import _open_backend
from anvil.cli.roots import _load_evidence_manifest, _root_evidence_payload
from anvil.claims.command_proof_artifact import claim_command_cwd_identity
from anvil.clock import SystemClock
from anvil.review.gates import evidence_complete
from anvil.roots.registry import RootSetError, root_set_use_authorized
from anvil.state.backend import EventRejected
from anvil.state.models import EventDraft, HookCommandAttribution, hook_command_semantic_digest
from anvil.state.hashing import canonical_json_bytes

runner = CliRunner()


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


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
    return json.loads(result.output)["data"]


@pytest.mark.parametrize("proof_case", ["valid", "missing", "malformed", "failed", "wrong_owner", "external", "external_symlink", "external_fifo"])
def test_root_set_submit_evidence_preserves_each_root_and_reconciles_response(tmp_path, monkeypatch, proof_case):
    """Same filenames remain separate and a terminal claim stays overheld."""
    home = tmp_path / "home"
    app_root, library_root = _repo(tmp_path / "app"), _repo(tmp_path / "library")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(app_root)
    assert runner.invoke(app, ["init", "--with-sample"], catch_exceptions=False).exit_code == 0
    _git(app_root, "add", "-A")
    _git(app_root, "commit", "-qm", "state")
    task = _json(runner.invoke(app, ["next", "--cwd", str(app_root), "--json"], catch_exceptions=False))["task"]
    primary_commands = task["verification"]["commands"]
    _json(runner.invoke(app, [
        "roots", "enroll", "--repository-id", "app", "--path", str(app_root),
        "--origin", "local:app", "--verification-command", primary_commands[0], "--json",
    ], catch_exceptions=False))
    _json(runner.invoke(app, [
        "roots", "enroll", "--repository-id", "library", "--path", str(library_root),
        "--origin", "local:library", "--verification-command", "python -m pytest -q", "--json",
    ], catch_exceptions=False))
    request = {
        "schema": "anvil.root-set-request/v1", "request_id": "evidence-request",
        "primary_root_id": "app", "roots": [
            {"root_id": "app", "repository_id": "app", "path": str(app_root),
             "expected_files": ["README.md"], "verification_commands": primary_commands},
            {"root_id": "library", "repository_id": "library", "path": str(library_root),
             "expected_files": [], "verification_commands": ["python -m pytest -q"]},
        ],
    }
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    claimed = _json(runner.invoke(app, [
        "roots", "claim", task["id"], "--request-file", str(request_path),
        "--actor", "root-evidence", "--cwd", str(app_root), "--json",
    ], catch_exceptions=False))
    manifest = {
        "schema": "anvil.root-set-evidence/v1", "submission_id": "submission-one",
        "serving_manifest_digest": "a" * 64,
        "roots": [
            {"root_id": "app", "baseline_sha": claimed["roots"][0]["baseline_sha"],
             "artifact_digest": "b" * 64, "verification_digest": "c" * 64,
             "commands": primary_commands, "files": ["README.md"]},
            {"root_id": "library", "baseline_sha": claimed["roots"][1]["baseline_sha"],
             "artifact_digest": "d" * 64, "verification_digest": "e" * 64,
             "commands": ["python -m pytest -q"], "files": ["README.md"]},
        ],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    before = (app_root / ".anvil" / "events.jsonl").read_bytes()
    generic = runner.invoke(app, [
        "submit", task["id"], "--commands", primary_commands[0],
        "--files-changed", "README.md", "--actor", "root-evidence",
        "--cwd", str(app_root), "--json",
    ], catch_exceptions=False)
    assert generic.exit_code == 1
    assert json.loads(generic.output)["error"]["code"] == "root_set_authorization_required"
    assert (app_root / ".anvil" / "events.jsonl").read_bytes() == before
    monkeypatch.setenv("ANVIL_ACTOR", "root-evidence")
    monkeypatch.setenv("ANVIL_CLAIM_ID", claimed["claim_id"])
    buffer = app_root / ".anvil" / ".evidence-buffer" / f"{claimed['claim_id']}.json"
    if proof_case not in {"missing", "external", "external_symlink", "external_fifo"}:
        for command in primary_commands:
            captured = runner.invoke(app, [
                "hook", "capture-evidence", "--command", command,
                "--exit-code", "1" if proof_case == "failed" else "0",
                "--actor", "root-evidence", "--cwd", str(app_root),
            ], catch_exceptions=False)
            assert captured.exit_code == 0
        assert buffer.exists()
    if proof_case == "malformed":
        buffer.write_text('{"command":', encoding="utf-8")
    if proof_case == "wrong_owner":
        record = json.loads(buffer.read_text(encoding="utf-8").splitlines()[0])
        record["attribution"]["claimed_by"] = "another-actor"
        record["semantic_digest"] = hook_command_semantic_digest(
            attribution=HookCommandAttribution.model_validate(record["attribution"]),
            command=record["command"], exit_code=record["exit_code"],
            output_sha256=record["output_sha256"],
            captured_at=datetime.fromisoformat(record["timestamp"]),
        )
        buffer.write_text(json.dumps(record) + "\n", encoding="utf-8")
        with pytest.raises(EventRejected, match="hook command proof batch"):
            runner.invoke(app, [
                "roots", "submit-evidence", task["id"], "--request-file", str(request_path),
                "--manifest-file", str(manifest_path), "--actor", "root-evidence",
                "--cwd", str(app_root), "--json",
            ], catch_exceptions=False)
        assert (app_root / ".anvil" / "events.jsonl").read_bytes() == before
        backend = _open_backend(app_root / ".anvil")
        try:
            assert backend.get_claim(claimed["claim_id"]).status.value == "active"
            assert backend.get_latest_evidence(task["id"]) is None
        finally:
            backend.close()
        return
    proof_options = []
    if proof_case.startswith("external"):
        backend = _open_backend(app_root / ".anvil")
        try:
            claim = backend.get_claim(claimed["claim_id"])
            project = backend.get_project()
        finally:
            backend.close()
        context = claim.attestation_context
        output = b"synthetic checked result\n"
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        payload = {
            "schema_version": 1, "project_id": project.id, "claim_id": claim.id,
            "generation": claim.generation, "claimed_by": claim.claimed_by,
            "task_id": task["id"], "task_revision": context.task_revision,
            "prd_id": context.prd_id, "prd_revision": context.prd_revision,
            "repository_id": context.repository_id, "claim_start_sha": context.claim_start_sha,
            "cwd_relative": ".", "cwd_identity": claim_command_cwd_identity(
                app_root, context.repository_id, "."),
            "command_base64": base64.b64encode(primary_commands[0].encode()).decode(),
            "started_at": claim.created_at.isoformat().replace("+00:00", "Z"),
            "ended_at": now, "exit_code": 0,
            "output_base64": base64.b64encode(output).decode(),
            "output_sha256": hashlib.sha256(output).hexdigest(),
        }
        proof_file = tmp_path / "proof.json"
        proof_file.write_bytes(canonical_json_bytes({"envelope_id": "root-proof-1", "payload": payload}))
        if proof_case == "external_symlink":
            link = tmp_path / "proof-link.json"
            link.symlink_to(proof_file)
            proof_file = link
        elif proof_case == "external_fifo":
            if not hasattr(os, "mkfifo"):
                pytest.skip("FIFO requires POSIX")
            fifo = tmp_path / "proof-pipe"
            os.mkfifo(fifo)
            proof_file = fifo
        proof_options = ["--command-proof-file", str(proof_file)]
    if proof_case in {"external_symlink", "external_fifo"}:
        refused = runner.invoke(app, [
            "roots", "submit-evidence", task["id"], "--request-file", str(request_path),
            "--manifest-file", str(manifest_path), "--actor", "root-evidence",
            *proof_options, "--cwd", str(app_root), "--json",
        ], catch_exceptions=False)
        assert refused.exit_code == 1
        assert json.loads(refused.output)["error"]["code"] == "root_set_command_proof_invalid"
        assert (app_root / ".anvil" / "events.jsonl").read_bytes() == before
        return
    submitted = _json(runner.invoke(app, [
        "roots", "submit-evidence", task["id"], "--request-file", str(request_path),
        "--manifest-file", str(manifest_path), "--actor", "root-evidence",
        *proof_options, "--cwd", str(app_root), "--json",
    ], catch_exceptions=False))
    assert submitted["status"] == "submitted"
    backend = _open_backend(app_root / ".anvil")
    try:
        submitted_task = backend.get_task(task["id"])
        submitted_evidence = backend.get_latest_evidence(task["id"])
        assert submitted_task.verification.required_proofs
        assert submitted_evidence is not None
        assert len(submitted_evidence.proofs) == (len(primary_commands) if proof_case in {"valid", "failed"} else 1 if proof_case == "external" else 0)
        assert evidence_complete(submitted_task, submitted_evidence)[0] == (proof_case in {"valid", "external"})
    finally:
        backend.close()
    status = _json(runner.invoke(app, [
        "roots", "evidence-status", task["id"], "--request-file", str(request_path),
        "--manifest-file", str(manifest_path), "--actor", "root-evidence",
        "--cwd", str(app_root), "--json",
    ], catch_exceptions=False))
    assert status["status"] == "submitted" and status["evidence_id"] == submitted["evidence_id"]
    # Exact recovery is an idempotent read; it cannot append a second evidence row.
    submitted_events = (app_root / ".anvil" / "events.jsonl").read_bytes()
    buffer.parent.mkdir(exist_ok=True)
    buffer.write_text("replacement buffer must not be read\n", encoding="utf-8")
    again = _json(runner.invoke(app, [
        "roots", "submit-evidence", task["id"], "--request-file", str(request_path),
        "--manifest-file", str(manifest_path), "--actor", "root-evidence",
        "--cwd", str(app_root), "--json",
    ], catch_exceptions=False))
    assert again == submitted
    assert (app_root / ".anvil" / "events.jsonl").read_bytes() == submitted_events
    event = json.loads((app_root / ".anvil" / "events.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    roots = event["payload_json"]["root_set_evidence"]["roots"]
    assert [item["root_id"] for item in roots] == ["app", "library"]
    assert [item["files"] for item in roots] == [["README.md"], ["README.md"]]
    final = _json(runner.invoke(app, [
        "roots", "reconcile", "--request-id", "evidence-request",
        "--request-digest", claimed["request_digest"], "--actor", "root-evidence",
        "--confirm-runner-stopped", "--cwd", str(app_root), "--json",
    ], catch_exceptions=False))
    assert final["state"] == "released"


def test_root_set_evidence_refuses_changed_root_fact(tmp_path, monkeypatch):
    """The owner rejects a manifest that substitutes an immutable baseline."""
    # The full CLI fixture above proves accepted execution; a smaller malformed
    # request is enough here because strict loading refuses before State writes.
    manifest = tmp_path / "bad.json"
    manifest.write_text(json.dumps({
        "schema": "anvil.root-set-evidence/v1", "submission_id": "bad",
        "serving_manifest_digest": "a" * 64,
        "roots": [{"root_id": "r", "baseline_sha": "b" * 41,
                   "artifact_digest": "c" * 64, "verification_digest": "d" * 64,
                   "commands": ["pytest"], "files": ["README.md"]}],
    }), encoding="utf-8")
    with pytest.raises(RootSetError, match="digest"):
        _load_evidence_manifest(manifest)


def test_direct_backend_root_set_evidence_requires_exact_canonical_binding(tmp_path, monkeypatch):
    """A live use capability cannot turn a forged payload into owner evidence."""
    home = tmp_path / "home"
    app_root, library_root = _repo(tmp_path / "app"), _repo(tmp_path / "library")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(app_root)
    assert runner.invoke(app, ["init", "--with-sample"], catch_exceptions=False).exit_code == 0
    _git(app_root, "add", "-A")
    _git(app_root, "commit", "-qm", "state")
    task = _json(runner.invoke(app, ["next", "--cwd", str(app_root), "--json"], catch_exceptions=False))["task"]
    commands = task["verification"]["commands"]
    for repository_id, path, command in (
        ("app", app_root, commands[0]),
        ("library", library_root, "python -m pytest -q"),
    ):
        _json(runner.invoke(app, [
            "roots", "enroll", "--repository-id", repository_id, "--path", str(path),
            "--origin", f"local:{repository_id}", "--verification-command", command, "--json",
        ], catch_exceptions=False))
    request = {
        "schema": "anvil.root-set-request/v1", "request_id": "direct-evidence",
        "primary_root_id": "app", "roots": [
            {"root_id": "app", "repository_id": "app", "path": str(app_root),
             "expected_files": ["README.md"], "verification_commands": commands},
            {"root_id": "library", "repository_id": "library", "path": str(library_root),
             "expected_files": [], "verification_commands": ["python -m pytest -q"]},
        ],
    }
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    claimed = _json(runner.invoke(app, [
        "roots", "claim", task["id"], "--request-file", str(request_path),
        "--actor", "direct-evidence", "--cwd", str(app_root), "--json",
    ], catch_exceptions=False))
    manifest = {
        "schema": "anvil.root-set-evidence/v1", "submission_id": "direct-submission",
        "serving_manifest_digest": "a" * 64,
        "roots": [
            {"root_id": "app", "baseline_sha": claimed["roots"][0]["baseline_sha"],
             "artifact_digest": "b" * 64, "verification_digest": "c" * 64,
             "commands": commands, "files": ["README.md"]},
            {"root_id": "library", "baseline_sha": claimed["roots"][1]["baseline_sha"],
             "artifact_digest": "d" * 64, "verification_digest": "e" * 64,
             "commands": ["python -m pytest -q"], "files": ["README.md"]},
        ],
    }
    backend = _open_backend(app_root / ".anvil")
    try:
        claim = backend.get_claim(claimed["claim_id"])
        assert claim is not None and claim.root_set is not None
        evidence = _root_evidence_payload(claim.root_set, claim.id, manifest)
        payload = {
            "task_id": task["id"], "claim_id": claim.id, "submitted_by": "direct-evidence",
            "evidence_id": "EVROOT-" + evidence["owner_manifest_digest"][:24],
            "commands_run": [item for root in evidence["roots"] for item in root["commands"]],
            "files_changed": [f"{root['root_id']}/{path}" for root in evidence["roots"] for path in root["files"]],
            "output_excerpt": "direct writer", "root_set_evidence": evidence,
        }
        for altered in (
            payload | {"root_set_evidence": None},
            payload | {"root_set_evidence": evidence | {"owner_manifest_digest": "0" * 64}},
            payload | {"root_set_evidence": evidence | {"roots": [
                evidence["roots"][0] | {"baseline_sha": "f" * 40}, evidence["roots"][1]
            ]}},
            payload | {"files_changed": ["app/arbitrary-secret.txt"]},
        ):
            with pytest.raises(EventRejected, match="coordinated claims require|frozen root facts|owner digest|evidence summary"):
                with root_set_use_authorized(claim.root_set, backend=backend):
                    backend.append(EventDraft(
                        timestamp=SystemClock().now(), actor="direct-evidence",
                        action="evidence.submitted", target_kind="task", target_id=task["id"],
                        payload_json=altered,
                    ))
    finally:
        backend.close()


def test_direct_backend_rejects_structured_root_evidence_for_ordinary_claim(tmp_path, monkeypatch):
    """The structured representation cannot be injected into a legacy claim."""
    project = _repo(tmp_path / "project")
    monkeypatch.chdir(project)
    assert runner.invoke(app, ["init", "--with-sample"], catch_exceptions=False).exit_code == 0
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "state")
    task = _json(runner.invoke(app, ["next", "--cwd", str(project), "--json"], catch_exceptions=False))["task"]
    claimed = _json(runner.invoke(app, [
        "claim", task["id"], "--actor", "ordinary", "--cwd", str(project), "--json",
    ], catch_exceptions=False))
    backend = _open_backend(project / ".anvil")
    try:
        claim_id = claimed["claim"]["id"]
        with pytest.raises(EventRejected, match="ordinary claims cannot carry root-set evidence"):
            backend.append(EventDraft(
                timestamp=SystemClock().now(), actor="ordinary", action="evidence.submitted",
                target_kind="task", target_id=task["id"], payload_json={
                    "task_id": task["id"], "claim_id": claim_id, "submitted_by": "ordinary",
                    "evidence_id": "EVORDINARY", "commands_run": ["pytest"], "files_changed": ["README.md"],
                    "root_set_evidence": {
                        "schema": "anvil.root-set-evidence/v1", "submission_id": "ordinary-root",
                        "serving_manifest_digest": "a" * 64, "owner_manifest_digest": "b" * 64,
                        "roots": [{"root_id": "root", "baseline_sha": "c" * 40,
                                   "artifact_digest": "d" * 64, "verification_digest": "e" * 64,
                                   "commands": ["pytest"], "files": ["README.md"]}],
                    },
                },
            ))
    finally:
        backend.close()
