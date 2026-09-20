"""CLI-only per-root evidence submission stays bound to one root-set claim."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from anvil.cli import app
from anvil.cli._helpers import _open_backend
from anvil.cli.roots import _load_evidence_manifest, _root_evidence_payload
from anvil.clock import SystemClock
from anvil.roots.registry import RootSetError, root_set_use_authorized
from anvil.state.backend import EventRejected
from anvil.state.models import EventDraft


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


def test_root_set_submit_evidence_preserves_each_root_and_reconciles_response(tmp_path, monkeypatch):
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
    submitted = _json(runner.invoke(app, [
        "roots", "submit-evidence", task["id"], "--request-file", str(request_path),
        "--manifest-file", str(manifest_path), "--actor", "root-evidence",
        "--cwd", str(app_root), "--json",
    ], catch_exceptions=False))
    assert submitted["status"] == "submitted"
    status = _json(runner.invoke(app, [
        "roots", "evidence-status", task["id"], "--request-file", str(request_path),
        "--manifest-file", str(manifest_path), "--actor", "root-evidence",
        "--cwd", str(app_root), "--json",
    ], catch_exceptions=False))
    assert status["status"] == "submitted" and status["evidence_id"] == submitted["evidence_id"]
    # Exact recovery is an idempotent read; it cannot append a second evidence row.
    again = _json(runner.invoke(app, [
        "roots", "submit-evidence", task["id"], "--request-file", str(request_path),
        "--manifest-file", str(manifest_path), "--actor", "root-evidence",
        "--cwd", str(app_root), "--json",
    ], catch_exceptions=False))
    assert again == submitted
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
