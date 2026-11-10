"""Bundle review is a native, versioned prerequisite for new member acceptance."""

from __future__ import annotations

import json

import pytest

from anvil.bundles.execution import bundle_execution_scope_digest
from anvil.bundles.review import BundleReviewManager
from anvil.clock import FrozenClock
from anvil.state.backend import EventRejected
from anvil.state.models import Event, RejectionReasonCode, ReviewDecision
from anvil.state.snapshot import serialize_state
from tests.test_bundle_execution import _NOW, _backend, _event, _manager, _seed


def _submit(backend, claimed, task_id):
    backend.append(_event("evidence.submitted", "task", task_id, {
        "task_id": task_id, "claim_id": claimed.claim.member_claim_ids[task_id],
        "submitted_by": "coordinator", "evidence_id": f"EV-REVIEW-{task_id}",
        "commands_run": [f"verify-{task_id}"], "files_changed": ["src/shared.py"],
    }))


def _implement_bundle(backend, root, *, scoped=True):
    _seed(backend)
    manager = _manager(backend, root)
    digest = bundle_execution_scope_digest(backend, "B001") if scoped else None
    claimed = manager.claim("B001", expected_scope_digest=digest)
    for task_id in ("release:T001", "release:T002"):
        _submit(backend, claimed, task_id)
    assert manager.mark_implemented("B001").can_mark_implemented


def _acceptance(backend, *, version=1):
    payload = {
        "schema_version": 1, "task_id": "release:T001", "reviewer": "human",
        "decision": "accepted", "review_attempt_id": backend.get_latest_evidence("release:T001").id,
    }
    if version != "omitted":
        payload["bundle_review_guard_version"] = version
    return _event("task.applied", "task", "release:T001", payload, actor="human")


def _quorum(backend, *, finalize=True):
    for reviewer, angle in (("review-a", "correctness"), ("review-b", "security"), ("review-c", "integration")):
        BundleReviewManager(backend, FrozenClock(_NOW), actor=reviewer).record(
            "B001", review_round=1, angle=angle, decision=ReviewDecision.approve,
        )
    if finalize:
        BundleReviewManager(backend, FrozenClock(_NOW), actor="coordinator").finalize("B001")


def test_live_acceptance_requires_version_and_finalized_quorum(tmp_path):
    backend = _backend(tmp_path)
    try:
        _implement_bundle(backend, tmp_path)
        before = (tmp_path / "events.jsonl").read_bytes()
        for version in (1, "omitted", None):
            with pytest.raises(EventRejected, match="bundle_review_required"):
                backend.append(_acceptance(backend, version=version))
        assert (tmp_path / "events.jsonl").read_bytes() == before
        _quorum(backend, finalize=False)
        with pytest.raises(EventRejected, match="bundle_review_required"):
            backend.append(_acceptance(backend))
        BundleReviewManager(backend, FrozenClock(_NOW), actor="coordinator").finalize("B001")
        assert backend.bundle_member_acceptance_blocker("release:T001") is None
        with pytest.raises(EventRejected, match="guard version"):
            backend.append(_acceptance(backend, version="omitted"))
        backend.append(_acceptance(backend))
        assert backend.get_task("release:T001").status.value == "done"
        before_replay = serialize_state(backend)
        backend.replay_from_empty(str(tmp_path / "events.jsonl"))
        assert serialize_state(backend) == before_replay
    finally:
        backend.close()


@pytest.mark.parametrize("version", [1, None, "omitted"])
@pytest.mark.parametrize("scoped", [False, True])
def test_replay_guard_uses_winning_claim_scope_including_missing_version(tmp_path, version, scoped):
    backend = _backend(tmp_path)
    try:
        _implement_bundle(backend, tmp_path, scoped=scoped)
        draft = _acceptance(backend, version=version)
        lines = (tmp_path / "events.jsonl").read_text().splitlines()
        event = Event(id=f"E{len(lines) + 1:06d}", **draft.model_dump())
        with (tmp_path / "events.jsonl").open("a") as handle:
            handle.write(event.model_dump_json() + "\n")
        backend.replay_from_empty(str(tmp_path / "events.jsonl"))
        expected_status = "needs_review" if scoped else "done"
        assert backend.get_task("release:T001").status.value == expected_status
        accepted = [review for review in backend.list_reviews() if review.target_id == "release:T001"]
        assert len(accepted) == (1 if expected_status == "done" else 0)
    finally:
        backend.close()


def test_release_preserves_finalized_review_for_human_acceptance(tmp_path):
    backend = _backend(tmp_path)
    try:
        _implement_bundle(backend, tmp_path)
        _quorum(backend)
        _manager(backend, tmp_path).release("B001", reason="replan the cohort")
        backend.append(_acceptance(backend))
        assert backend.get_task("release:T001").status.value == "done"
    finally:
        backend.close()


def test_member_rejection_is_allowed_before_bundle_review(tmp_path):
    backend = _backend(tmp_path)
    try:
        _implement_bundle(backend, tmp_path)
        provenance = backend.derive_task_rejection_provenance(
            "release:T001", reason_code=RejectionReasonCode.unspecified_quality, quality_findings=[],
        )
        backend.append(_event("task.applied", "task", "release:T001", {
            "schema_version": 1, "task_id": "release:T001", "reviewer": "human",
            "decision": "rejected", "notes": "Needs correction",
            "rejection": provenance.model_dump(mode="json"),
        }, actor="human"))
        assert backend.get_task("release:T001").status.value == "drafted"
        last = json.loads((tmp_path / "events.jsonl").read_text().splitlines()[-1])
        assert "bundle_review_guard_version" not in last["payload_json"]
    finally:
        backend.close()


@pytest.mark.parametrize("version", [1, None, "omitted"])
def test_legacy_internal_dependency_bundle_can_accept_predecessor(tmp_path, version):
    backend = _backend(tmp_path)
    try:
        _seed(backend, internal_dependency=True)
        manager = _manager(backend, tmp_path)
        claimed = manager.claim("B001")
        assert claimed.claim.expected_scope_digest is None
        _submit(backend, claimed, "release:T001")
        backend.append(_acceptance(backend, version=version))
        assert backend.get_task("release:T001").status.value == "done"
        _submit(backend, claimed, "release:T002")
        assert manager.mark_implemented("B001").can_mark_implemented
        before_replay = serialize_state(backend)
        backend.replay_from_empty(str(tmp_path / "events.jsonl"))
        assert serialize_state(backend) == before_replay
    finally:
        backend.close()


@pytest.mark.parametrize("scoped", [False, True])
def test_losing_claim_id_collision_cannot_reclassify_winning_authorization(tmp_path, scoped):
    from tests.test_bundle_execution import _bundle_claim_payload

    backend = _backend(tmp_path)
    try:
        _implement_bundle(backend, tmp_path, scoped=scoped)
        winning = backend.get_bundle_claim("B001")
        payload = _bundle_claim_payload(backend, winning.id)
        if not scoped:
            payload["expected_scope_digest"] = "f" * 64
        draft = _event("bundle.claimed", "bundle", "B001", payload, actor="coordinator")
        drafts = [draft, _acceptance(backend, version="omitted")]
        lines = (tmp_path / "events.jsonl").read_text().splitlines()
        with (tmp_path / "events.jsonl").open("a") as handle:
            for index, draft in enumerate(drafts, start=1):
                event = Event(id=f"E{len(lines) + index:06d}", **draft.model_dump())
                handle.write(event.model_dump_json() + "\n")
        backend.replay_from_empty(str(tmp_path / "events.jsonl"))
        assert backend.get_bundle_claim("B001").expected_scope_digest == winning.expected_scope_digest
        assert backend.get_task("release:T001").status.value == ("needs_review" if scoped else "done")
    finally:
        backend.close()
