"""Disposable CLI/HTTP journeys for bounded execution and human review."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from typer.testing import CliRunner

from anvil import web
from anvil.cli import app
from anvil.cli._helpers import _resolve_state_dir, prd_source_path
from anvil.clock import SystemClock
from anvil.state.sqlite import SqliteBackend
from tests.test_bundle_state import _seed
from tests.test_web_actions import _request, _running_server


def _cli(repository, *arguments, succeeds=True):
    result = CliRunner().invoke(app, [*arguments, "--cwd", repository.root, "--json"])
    assert result.exit_code == (0 if succeeds else 1), result.output
    envelope = json.loads(result.output)
    assert envelope["ok"] is succeeds, envelope
    return envelope["data"] if succeeds else envelope["error"]


@pytest.fixture
def execution_project(git_repo, monkeypatch, tmp_path):
    """Seed only through canonical events, with the CLI-resolved isolated home."""
    monkeypatch.delenv("ANVIL_STATE_LAYOUT", raising=False)
    state_dir = _resolve_state_dir(git_repo)
    state_dir.mkdir(parents=True)
    events_path = state_dir / "events.jsonl"
    events_path.touch()
    backend = SqliteBackend(
        db_path=str(state_dir / "state.db"), events_path=str(events_path),
        clock=SystemClock(), project_root=str(git_repo),
    )
    backend.initialize()
    try:
        _seed(backend)
        prd = backend.get_prd("release")
        assert prd is not None and prd.source_bytes is not None
        source_path = prd_source_path(state_dir, "release")
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(prd.source_bytes)
        config_path = tmp_path / "execution-web.json"
        repository = web.enroll_repository(config_path, git_repo)
        _cli(
            repository, "bundle", "create", "B001", "release:T001", "release:T002",
            "--prd", "release", "--coordinator", "human", "--actor", "planner",
        )
        yield backend, config_path, repository, events_path, source_path
    finally:
        backend.close()


def _bundle_preview(server, repository, token):
    status, preview = _request(
        server, f"/api/repositories/{repository.id}/bundle-preview",
        {"bundle_id": "B001"}, token=token,
    )
    assert status == 200, preview
    return preview


def _claim_from_preview(server, repository, token, preview):
    return _request(
        server, f"/api/repositories/{repository.id}/bundle-claim",
        {"preview_id": preview["preview_id"], "confirmed": True}, token=token,
    )


def _submit(repository, task_id, *extra, succeeds=True):
    return _cli(
        repository, "submit", task_id, "--commands", "pytest -q",
        "--files-changed", f"src/{task_id.rsplit(':', 1)[-1]}.py",
        "--actor", "human", *extra, succeeds=succeeds,
    )


def test_execution_continues_from_cli_after_web_closes(execution_project):
    """J1/J2/J6: one web authorization, CLI proof intake, no implicit acceptance."""
    backend, config_path, repository, events_path, _ = execution_project
    with _running_server(config_path, allow_writes=True, actor="human") as server:
        token = _request(server, "/api/catalog")[1]["csrf_token"]
        status, listing = _request(server, f"/api/repositories/{repository.id}/bundles")
        assert status == 200, listing
        assert listing["actor"] == "human" and listing["truncated"] is False
        assert [plan["bundle_id"] for plan in listing["plans"]] == ["B001"]
        assert listing["plans"][0]["status"] == "planned"
        assert listing["plans"][0]["counts"]["total"] == 2
        preview = _bundle_preview(server, repository, token)
        status, claimed = _claim_from_preview(server, repository, token, preview)
        assert status == 200, claimed

    packet = _cli(repository, "bundle", "packet", "B001", "--actor", "human", "--format", "json")
    assert packet["content"]["bundle"]["id"] == "B001"
    _submit(repository, "release:T001")
    assert backend.get_task("release:T001").status.value == "needs_review"
    assert backend.get_task("release:T002").status.value == "claimed"

    plan = _cli(repository, "bundle", "execution-plan", "B001", "--max-workers", "2")
    assert plan["wave"] == ["release:T002"]
    assert plan["counts"]["pending_review"] == 1
    _submit(repository, "release:T002")
    completed = _cli(repository, "bundle", "complete", "B001", "--actor", "human")
    assert completed["bundle"]["status"] == "implemented_unreviewed"
    before_review = events_path.read_bytes()
    _cli(repository, "bundle", "finalize-review", "B001", "--actor", "human", succeeds=False)
    assert events_path.read_bytes() == before_review
    refusal = _cli(
        repository, "apply", "release:T001", "--approve", "--strict", "--reviewer", "human",
        succeeds=False,
    )
    assert refusal["code"] == "bundle_review_required"
    assert events_path.read_bytes() == before_review
    assert all(backend.get_task(task_id).status.value == "needs_review"
               for task_id in ("release:T001", "release:T002"))
    assert not any(action == "task.applied" for task_id in ("release:T001", "release:T002")
                   for action, _ in backend.list_events(target_id=task_id, limit=50))
    with _running_server(config_path, allow_writes=True, actor="human") as server:
        status, listing = _request(server, f"/api/repositories/{repository.id}/bundles")
        assert status == 200, listing
        assert listing["plans"][0]["status"] == "implemented_unreviewed"
        assert listing["plans"][0]["counts"]["pending_review"] == 2
        assert listing["plans"][0]["wave"] == []


@pytest.mark.parametrize("changed_binding", ["member_dependencies", "source_bytes"])
def test_changed_execution_preview_refuses_without_partial_claim(execution_project, changed_binding):
    """J1: source and membership changes need a new reviewable authorization."""
    backend, config_path, repository, events_path, source_path = execution_project
    with _running_server(config_path, allow_writes=True, actor="human") as server:
        token = _request(server, "/api/catalog")[1]["csrf_token"]
        preview = _bundle_preview(server, repository, token)
        if changed_binding == "member_dependencies":
            _cli(repository, "deps", "--add", "release:T002->release:T001", "--prd", "release")
        else:
            source_path.write_bytes(source_path.read_bytes() + b"\nChanged after preview.\n")
        before = events_path.read_bytes()
        status, refusal = _claim_from_preview(server, repository, token, preview)
        assert status == 409, refusal
        assert refusal["ok"] is False
        assert events_path.read_bytes() == before
        assert backend.get_bundle("B001").status.value == "planned"
        assert backend.get_bundle_claim("B001") is None
        assert backend.list_active_claims() == []


def test_competing_execution_confirmations_have_one_owner(execution_project):
    """J1: two fresh previews racing for one bundle cannot split ownership."""
    backend, config_path, repository, events_path, _ = execution_project
    with _running_server(config_path, allow_writes=True, actor="human") as server:
        token = _request(server, "/api/catalog")[1]["csrf_token"]
        previews = [_bundle_preview(server, repository, token) for _ in range(2)]
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(
                lambda preview: _claim_from_preview(server, repository, token, preview), previews,
            ))
        assert sorted(status for status, _ in outcomes) == [200, 409], outcomes
    claim = backend.get_bundle_claim("B001")
    assert claim is not None and claim.claimed_by == "human"
    member_claims = backend.list_active_claims()
    assert len(member_claims) == 2
    assert {member.bundle_claim_id for member in member_claims} == {claim.id}
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    assert sum(event["action"] == "bundle.claimed" for event in events) == 1


def test_bad_proof_does_not_complete_bundle_or_block_independent_member(execution_project, tmp_path):
    """J3/J4/J5: failed proof intake is isolated and cannot become acceptance."""
    backend, _, repository, events_path, _ = execution_project
    plan = _cli(repository, "bundle", "execution-plan", "B001")
    _cli(repository, "bundle", "claim", "B001", "--actor", "human", "--shared-tree",
         "--expected-scope-digest", plan["scope_digest"])
    malformed_proof = tmp_path / "malformed-proof.json"
    malformed_proof.write_text("{}\n")
    before = events_path.read_bytes()
    _submit(repository, "release:T001", "--command-proof-file", str(malformed_proof), succeeds=False)
    assert events_path.read_bytes() == before
    assert backend.get_latest_evidence("release:T001") is None
    assert backend.get_task("release:T001").status.value == "claimed"

    _submit(repository, "release:T002")
    refusal = _cli(repository, "bundle", "complete", "B001", "--actor", "human", succeeds=False)
    assert refusal["code"] == "bundle_not_ready"
    assert backend.get_bundle("B001").status.value == "active"
    assert backend.get_task("release:T002").status.value == "needs_review"
    assert backend.get_task("release:T001").status.value == "claimed"


def test_batch_access_requires_writer_and_matching_coordinator(execution_project):
    backend, config_path, repository, events_path, _ = execution_project
    prefix = f"/api/repositories/{repository.id}"
    before = events_path.read_bytes()
    with _running_server(config_path) as server:
        token = _request(server, "/api/catalog")[1]["csrf_token"]
        status, refusal = _request(server, prefix + "/bundles")
        assert status == 502 and refusal["error"]["code"] == "read_only_repository"
        assert _request(server, prefix + "/bundle-preview", {"bundle_id": "B001"}, token=token)[0] == 405
    with _running_server(config_path, allow_writes=True, actor="someone-else") as server:
        token = _request(server, "/api/catalog")[1]["csrf_token"]
        status, refusal = _request(server, prefix + "/bundle-preview", {"bundle_id": "B001"}, token=token)
        assert status == 409 and refusal["error"]["code"] == "coordinator_mismatch"
    assert events_path.read_bytes() == before
    assert backend.list_active_claims() == []


def test_task_and_bundle_confirmation_tokens_are_not_interchangeable(execution_project):
    backend, config_path, repository, events_path, _ = execution_project
    prefix = f"/api/repositories/{repository.id}"
    with _running_server(config_path, allow_writes=True, actor="human") as server:
        token = _request(server, "/api/catalog")[1]["csrf_token"]
        preview = _bundle_preview(server, repository, token)
        before = events_path.read_bytes()
        status, refusal = _request(server, prefix + "/transition", {
            "preview_id": preview["preview_id"], "confirmed": True,
        }, token=token)
        assert status == 409 and refusal["error"]["code"] == "preview_kind_mismatch"
        status, refusal = _claim_from_preview(server, repository, token, preview)
        assert status == 409 and refusal["error"]["code"] == "preview_expired"
        assert events_path.read_bytes() == before
        assert backend.list_active_claims() == []

        preview = _bundle_preview(server, repository, token)
        assert _claim_from_preview(server, repository, token, preview)[0] == 200
        _submit(repository, "release:T001")
        status, snapshot = _request(server, prefix + "/snapshot")
        assert status == 200, snapshot
        status, preview = _request(server, prefix + "/preview", {
            "task_id": "T001", "prd_id": "release", "action": "reject",
            "task_version": snapshot["workflow"]["versions"]["release:T001"],
            "snapshot_digest": snapshot["snapshot"]["snapshot_digest"],
        }, token=token)
        assert status == 200, preview
        before = events_path.read_bytes()
        status, refusal = _claim_from_preview(server, repository, token, preview)
        assert status == 409 and refusal["error"]["code"] == "preview_kind_mismatch"
        assert events_path.read_bytes() == before
        assert backend.get_task("release:T001").status.value == "needs_review"
