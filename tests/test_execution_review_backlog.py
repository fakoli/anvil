"""Real proofs and public cohort admission continue while review remains pending."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from anvil import web
from anvil.state.hashing import canonical_json_bytes
from anvil.state.snapshot import serialize_state
from tests.test_bundle_command_proofs import _capture
from tests.test_bundle_command_proofs import project as project
from tests.test_bundle_state import _event
from tests.test_execution_journeys import _cli
from tests.test_web_actions import _request, _running_server


def _claim_public(repository, config_path, bundle_id, surface, *, succeeds=True):
    plan = _cli(repository, "bundle", "execution-plan", bundle_id)
    if surface == "cli":
        result = _cli(
            repository, "bundle", "claim", bundle_id, "--actor", "codex-main",
            "--expected-scope-digest", plan["scope_digest"], succeeds=succeeds,
        )
    else:
        with _running_server(config_path, allow_writes=True, actor="codex-main") as server:
            token = _request(server, "/api/catalog")[1]["csrf_token"]
            prefix = f"/api/repositories/{repository.id}"
            status, preview = _request(
                server, prefix + "/bundle-preview", {"bundle_id": bundle_id}, token=token,
            )
            assert status == 200, preview
            status, result = _request(
                server, prefix + "/bundle-claim",
                {"preview_id": preview["preview_id"], "confirmed": True}, token=token,
            )
            assert status == (200 if succeeds else 409), result
            if not succeeds:
                result = result["error"]
    return plan["scope_digest"], result


def _submit_real_capture(project, repository, bundle_id, task_id, tmp_path):
    backend, _, _, _, command = project
    bundle_claim = backend.get_bundle_claim(bundle_id)
    claim = backend.get_claim(bundle_claim.member_claim_ids[task_id])
    draft = _capture(project, SimpleNamespace(claim=bundle_claim), task_id=task_id)
    proof = draft.payload_json["proofs"][0]
    proof_file = tmp_path / f"{task_id.replace(':', '-')}-capture.json"
    proof_file.write_bytes(canonical_json_bytes({
        "envelope_id": "review-backlog-capture",
        "payload": proof["evidence_core"], "issuer": proof["issuer"],
    }))
    submitted = _cli(
        repository, "submit", task_id, "--actor", "codex-main", "--commands", command,
        "--files-changed", "", "--command-proof-file", str(proof_file),
    )
    assert submitted["missing_claim_bound_proofs"] == []
    evidence = backend.get_latest_evidence(task_id)
    assert len(evidence.proofs) == 1
    core = evidence.proofs[0].evidence_core
    assert (core.task_id, core.claim_id, core.generation, core.claimed_by) == (
        task_id, claim.id, claim.generation, "codex-main",
    )
    assert backend.get_claim(claim.id).status.value == "released"
    assert backend.get_task(task_id).status.value == "needs_review"


@pytest.mark.parametrize("surface", ["cli", "http"])
def test_real_proof_cohorts_continue_while_review_waits(project, tmp_path, surface):
    """Synthetic actors authorize execution; no human or final acceptance is simulated."""
    backend, _, repo, events, _ = project
    template = backend.get_task("release:T001")
    task_template = {**template.model_dump(mode="json"), "prd_id": template.prd_id}
    config_path = tmp_path / "review-backlog-web.json"
    repository = web.enroll_repository(config_path, repo)
    scope_digest, _ = _claim_public(repository, config_path, "B001", surface)
    assert backend.get_bundle_claim("B001").expected_scope_digest == scope_digest

    for pending_count in range(2):
        plan = _cli(repository, "bundle", "execution-plan", "B001", "--max-workers", "1")
        assert plan["counts"]["pending_review"] == pending_count
        assert plan["counts"]["completed"] == 0
        assert len(plan["wave"]) == 1
        _submit_real_capture(project, repository, "B001", plan["wave"][0], tmp_path)
    completed = _cli(repository, "bundle", "complete", "B001", "--actor", "codex-main")
    assert completed["bundle"]["status"] == "implemented_unreviewed"

    # Protected final approval must remain separate from the earlier scope grant.
    before = events.read_bytes()
    refusal = _cli(
        repository, "apply", "release:T001", "--approve", "--strict",
        "--reviewer", "synthetic-reviewer", succeeds=False,
    )
    assert refusal["code"] == "bundle_review_required"
    assert events.read_bytes() == before

    # Plan new work only after B001 has submitted its real proofs and is waiting.
    for task_id, bundle_id, dependencies in (
        ("release:T003", "B002", []),
        ("release:T004", "B003", ["release:T001"]),
    ):
        now = datetime.now(UTC)
        backend.append(_event(
            "task.created", {
                **task_template, "id": task_id, "title": task_id,
                "dependencies": dependencies,
                "created_at": now.isoformat(), "updated_at": now.isoformat(),
            }, target_kind="task", target_id=task_id, timestamp=now, actor="fixture-planner",
        ))
        _cli(
            repository, "bundle", "create", bundle_id, task_id,
            "--prd", "release", "--coordinator", "codex-main", "--actor", "fixture-planner",
        )

    scope_digest, _ = _claim_public(repository, config_path, "B002", surface)
    assert backend.get_bundle_claim("B002").expected_scope_digest == scope_digest
    plan = _cli(repository, "bundle", "execution-plan", "B002", "--max-workers", "1")
    assert plan["wave"] == ["release:T003"]
    _submit_real_capture(project, repository, "B002", "release:T003", tmp_path)

    # Proof is not acceptance: a true dependent cohort must still refuse atomically.
    before = events.read_bytes()
    _, refusal = _claim_public(repository, config_path, "B003", surface, succeeds=False)
    assert "dependencies are not done" in refusal["message"]
    assert events.read_bytes() == before
    assert backend.get_bundle_claim("B003") is None
    assert backend.get_task("release:T004").status.value == "ready"
    assert backend.list_active_claims() == []
    assert all(backend.get_task(task_id).status.value == "needs_review"
               for task_id in ("release:T001", "release:T002", "release:T003"))
    assert not any(json.loads(line)["action"] == "task.applied"
                   for line in events.read_text().splitlines())

    with _running_server(config_path, allow_writes=True, actor="codex-main") as server:
        status, listing = _request(server, f"/api/repositories/{repository.id}/bundles")
        assert status == 200, listing
        plans = {plan["bundle_id"]: plan for plan in listing["plans"]}
        assert plans["B001"]["status"] == "implemented_unreviewed"
        assert plans["B001"]["counts"]["pending_review"] == 2
        assert plans["B002"]["counts"]["pending_review"] == 1
        assert all(plan["counts"]["completed"] == 0 for plan in plans.values())
    snapshot = serialize_state(backend)
    backend.replay_from_empty(str(events))
    assert serialize_state(backend) == snapshot
