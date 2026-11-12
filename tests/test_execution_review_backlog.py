"""Real proofs and public cohort admission continue while review remains pending."""

from __future__ import annotations

import hashlib
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
from tests.test_web_actions import _preview_request, _request, _running_server


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


@pytest.mark.parametrize(("surface", "expire_preview"), [
    pytest.param("cli", False, id="cli-confirmation"),
    pytest.param("http", False, id="http-confirmation"),
    pytest.param("http", True, id="http-expired-preview"),
])
def test_final_approval_wait_preserves_exact_attempt(
    project, tmp_path, monkeypatch, surface, expire_preview,
):
    """Disposable synthetic approvals exercise native CLI/HTTP, not human authority."""
    backend, _, repo, events, _ = project
    template = backend.get_task("release:T001")
    task_template = {**template.model_dump(mode="json"), "prd_id": template.prd_id}
    config_path = tmp_path / "final-approval-web.json"
    repository = web.enroll_repository(config_path, repo)
    _claim_public(repository, config_path, "B001", surface)
    for task_id in ("release:T001", "release:T002"):
        _submit_real_capture(project, repository, "B001", task_id, tmp_path)
    _cli(repository, "bundle", "complete", "B001", "--actor", "codex-main")
    for reviewer, angle in (("review-a", "correctness"), ("review-b", "security"),
                            ("review-c", "integration")):
        _cli(repository, "bundle", "review", "B001", "--round", "1", "--angle", angle,
             "--decision", "approve", "--actor", reviewer)
    _cli(repository, "bundle", "finalize-review", "B001", "--actor", "codex-main")
    assert backend.get_bundle("B001").status.value == "reviewed_unintegrated"
    assert len(backend.list_bundle_reviews("B001")) == 3
    assert not any(json.loads(line)["action"] == "task.applied"
                   for line in events.read_text().splitlines())
    target = backend.get_task("release:T001")
    saved_evidence = {task_id: backend.get_latest_evidence(task_id)
                      for task_id in ("release:T001", "release:T002")}
    saved_claims = {evidence.claim_id: backend.get_claim(evidence.claim_id)
                    for evidence in saved_evidence.values()}
    proof_core = saved_evidence[target.id].proofs[0].evidence_core
    saved_proof_hash = hashlib.sha256(canonical_json_bytes(proof_core.model_dump(mode="json"))).hexdigest()
    saved_identity = (proof_core.claim_id, proof_core.generation)
    saved_updated_at = target.updated_at.isoformat()

    with _running_server(config_path, allow_writes=True, actor="synthetic-human") as server:
        # Replace only this server module's clock reference; real capture/subprocess
        # wall time and the process-wide time module remain untouched.
        preview_clock = SimpleNamespace(now=1000.0)
        if expire_preview:
            monkeypatch.setattr(web, "time", SimpleNamespace(monotonic=lambda: preview_clock.now))
        token = _request(server, "/api/catalog")[1]["csrf_token"]
        prefix = f"/api/repositories/{repository.id}"
        status, initial_snapshot = _request(server, prefix + "/snapshot")
        assert status == 200, initial_snapshot
        saved_version = initial_snapshot["workflow"]["versions"][target.id]
        request = _preview_request(
            "T001", "approve", version=saved_version,
            digest=initial_snapshot["snapshot"]["snapshot_digest"],
        )
        request["prd_id"] = "release"
        status, preview = _request(server, prefix + "/preview", request, token=token)
        assert status == 200, preview
        assert f"Evidence: {saved_evidence[target.id].id}" in preview["details"]
        assert preview["requires_confirmation"] is True

        # Independent work executes and submits real proof while this exact
        # preview remains pending, within the existing command/HTTP deadlines.
        now = datetime.now(UTC)
        backend.append(_event("task.created", {
            **task_template, "id": "release:T003", "title": "Independent work",
            "created_at": now.isoformat(), "updated_at": now.isoformat(),
        }, target_kind="task", target_id="release:T003", timestamp=now, actor="fixture-planner"))
        _cli(repository, "bundle", "create", "B002", "release:T003", "--prd", "release",
             "--coordinator", "codex-main", "--actor", "fixture-planner")
        _claim_public(repository, config_path, "B002", surface)
        _submit_real_capture(project, repository, "B002", "release:T003", tmp_path)
        status, later_snapshot = _request(server, prefix + "/snapshot")
        assert status == 200, later_snapshot
        assert later_snapshot["snapshot"]["snapshot_digest"] != initial_snapshot["snapshot"]["snapshot_digest"]
        assert later_snapshot["workflow"]["versions"][target.id] == saved_version
        assert backend.get_task(target.id) == target
        assert target.status.value == "needs_review"
        for task_id, evidence in saved_evidence.items():
            assert backend.get_latest_evidence(task_id) == evidence
        saved_evidence["release:T003"] = backend.get_latest_evidence("release:T003")
        independent_claim = saved_evidence["release:T003"].claim_id
        saved_claims[independent_claim] = backend.get_claim(independent_claim)

        if expire_preview:
            assert web.PREVIEW_LIFETIME_SECONDS == 300
            preview_clock.now += web.PREVIEW_LIFETIME_SECONDS + 1
            before_bytes, before_state = events.read_bytes(), serialize_state(backend)
            status, refusal = _request(server, prefix + "/transition", {
                "preview_id": preview["preview_id"], "confirmed": True,
            }, token=token)
            assert status == 409 and refusal["error"]["code"] == "preview_expired", refusal
            assert events.read_bytes() == before_bytes
            assert serialize_state(backend) == before_state
            request["snapshot_digest"] = later_snapshot["snapshot"]["snapshot_digest"]
            status, preview = _request(server, prefix + "/preview", request, token=token)
            assert status == 200, preview
            assert f"Evidence: {saved_evidence[target.id].id}" in preview["details"]

        if surface == "cli":
            # Equivalent exact-attempt binding; CLI does not consume the HTTP token.
            _cli(repository, "apply", target.id, "--approve", "--strict",
                 "--reviewer", "synthetic-human", "--expected-evidence-id", saved_evidence[target.id].id,
                 "--expected-task-updated-at", saved_updated_at)
        else:
            status, accepted = _request(server, prefix + "/transition", {
                "preview_id": preview["preview_id"], "confirmed": True,
            }, token=token)
            assert status == 200, accepted
            before_bytes, before_state = events.read_bytes(), serialize_state(backend)
            status, refusal = _request(server, prefix + "/transition", {
                "preview_id": preview["preview_id"], "confirmed": True,
            }, token=token)
            assert status == 409 and refusal["error"]["code"] == "preview_expired", refusal
            assert events.read_bytes() == before_bytes
            assert serialize_state(backend) == before_state

    applied = [json.loads(line) for line in events.read_text().splitlines()
               if json.loads(line)["action"] == "task.applied"]
    assert len(applied) == 1
    assert applied[0]["target_id"] == target.id
    payload = applied[0]["payload_json"]
    assert payload["decision"] == "accepted"
    assert payload["review_attempt_id"] == payload["expected_evidence_id"] == saved_evidence[target.id].id
    assert payload["bundle_review_guard_version"] == 1
    assert payload["expected_task_updated_at"] == saved_updated_at
    assert backend.get_task(target.id).status.value == "done"
    assert all(backend.get_task(task_id).status.value == "needs_review"
               for task_id in ("release:T002", "release:T003"))
    for task_id, evidence in saved_evidence.items():
        assert backend.get_latest_evidence(task_id) == evidence
    for claim_id, claim in saved_claims.items():
        assert backend.get_claim(claim_id) == claim
    core = backend.get_latest_evidence(target.id).proofs[0].evidence_core
    assert (core.claim_id, core.generation) == saved_identity
    assert hashlib.sha256(canonical_json_bytes(core.model_dump(mode="json"))).hexdigest() == saved_proof_hash
    snapshot = serialize_state(backend)
    backend.replay_from_empty(str(events))
    assert serialize_state(backend) == snapshot
