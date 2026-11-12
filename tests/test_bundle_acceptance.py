"""Bundle review is a native, versioned prerequisite for new member acceptance."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from anvil.bundles.execution import bundle_execution_scope_digest
from anvil.bundles.manager import BundleManager
from anvil.bundles.review import BundleReviewError, BundleReviewManager
from anvil.clock import FrozenClock
from anvil.state.backend import EventRejected
from anvil.state.models import Event, RejectionReasonCode, ReviewDecision
from anvil.state.snapshot import serialize_state
from tests.test_bundle_execution import _NOW, _backend, _event, _manager, _seed


def _submit(backend, claimed, task_id, *, clock=None):
    event = _event("evidence.submitted", "task", task_id, {
        "task_id": task_id, "claim_id": claimed.claim.member_claim_ids[task_id],
        "submitted_by": "coordinator", "evidence_id": f"EV-REVIEW-{task_id}",
        "commands_run": [f"verify-{task_id}"], "files_changed": ["src/shared.py"],
    })
    backend.append(event if clock is None else event.model_copy(update={"timestamp": clock.now()}))


def _implement_bundle(backend, root, *, scoped=True, clock=None):
    _seed(backend)
    manager = (_manager(backend, root) if clock is None else
               BundleManager(backend, clock, actor="coordinator", project_root=root))
    digest = bundle_execution_scope_digest(backend, "B001") if scoped else None
    claimed = manager.claim("B001", expected_scope_digest=digest)
    for task_id in ("release:T001", "release:T002"):
        _submit(backend, claimed, task_id, clock=clock)
    assert manager.mark_implemented("B001").can_mark_implemented


def _acceptance(backend, *, version=1, clock=None):
    payload = {
        "schema_version": 1, "task_id": "release:T001", "reviewer": "human",
        "decision": "accepted", "review_attempt_id": backend.get_latest_evidence("release:T001").id,
    }
    if version != "omitted":
        payload["bundle_review_guard_version"] = version
    event = _event("task.applied", "task", "release:T001", payload, actor="human")
    return event if clock is None else event.model_copy(update={"timestamp": clock.now()})


def _quorum(backend, *, finalize=True, clock=None):
    clock = clock or FrozenClock(_NOW)
    for reviewer, angle in (("review-a", "correctness"), ("review-b", "security"), ("review-c", "integration")):
        BundleReviewManager(backend, clock, actor=reviewer).record(
            "B001", review_round=1, angle=angle, decision=ReviewDecision.approve,
        )
    if finalize:
        BundleReviewManager(backend, clock, actor="coordinator").finalize("B001")


@pytest.mark.parametrize("finalize_before_expiry", [False, True])
def test_coordinator_expiry_preserves_finalized_acceptance_boundary(tmp_path, finalize_before_expiry):
    """Synthetic fixture acceptance uses the advanced clock, not an old event timestamp."""
    backend = _backend(tmp_path)
    clock = FrozenClock(datetime(2040, 1, 1, tzinfo=UTC))
    events = tmp_path / "events.jsonl"
    try:
        _implement_bundle(backend, tmp_path, clock=clock)
        _quorum(backend, finalize=finalize_before_expiry, clock=clock)
        coordinator = BundleReviewManager(backend, clock, actor="coordinator")
        assert coordinator.gate("B001").passed
        claim = backend.get_bundle_claim("B001")
        assert claim.lease_expires_at > clock.now()
        saved_attempt = backend.get_latest_evidence("release:T001").id
        before = serialize_state(backend)
        assert all(task["status"] == "needs_review" for task in before["tasks"])
        assert not any(json.loads(line)["action"] == "task.applied"
                       for line in events.read_text().splitlines())
        clock.advance(seconds=(claim.lease_expires_at - clock.now()).total_seconds() + 1)
        assert clock.now() > claim.lease_expires_at
        if finalize_before_expiry:
            assert backend.bundle_member_acceptance_blocker("release:T001") is None
            acceptance = _acceptance(backend, clock=clock)
            assert acceptance.timestamp == clock.now()
            assert acceptance.payload_json["review_attempt_id"] == saved_attempt
            backend.append(acceptance)
            assert backend.get_task("release:T001").status.value == "done"
            assert backend.get_task("release:T002").status.value == "needs_review"
            applied = [json.loads(line) for line in events.read_text().splitlines()
                       if json.loads(line)["action"] == "task.applied"]
            assert len(applied) == 1
            assert datetime.fromisoformat(applied[0]["timestamp"]) == clock.now()
            assert applied[0]["payload_json"]["decision"] == "accepted"
            for collection in ("claims", "evidence"):
                assert serialize_state(backend)[collection] == before[collection]
        else:
            before_bytes = events.read_bytes()
            with pytest.raises(BundleReviewError, match="active coordinator claim"):
                BundleReviewManager(backend, clock, actor="review-d").record(
                    "B001", review_round=1, angle="boundary", decision=ReviewDecision.approve,
                )
            assert events.read_bytes() == before_bytes
            assert serialize_state(backend) == before
            with pytest.raises(BundleReviewError, match="active coordinator claim"):
                coordinator.finalize("B001")
            assert events.read_bytes() == before_bytes
            assert serialize_state(backend) == before
            assert backend.get_bundle("B001").status.value == "implemented_unreviewed"
            assert backend.bundle_member_acceptance_blocker("release:T001") is not None
        snapshot = serialize_state(backend)
        backend.replay_from_empty(str(events))
        assert serialize_state(backend) == snapshot
    finally:
        backend.close()


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
