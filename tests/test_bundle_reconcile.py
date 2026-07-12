"""Bundle checkpoint, reconciliation, supersession, and replay."""

from __future__ import annotations

from datetime import timedelta

from anvil.bundles.delivery import BundleDeliveryManager
from anvil.bundles.review import BundleReviewManager
from anvil.clock import FrozenClock
from anvil.state.models import BundleStatus, ReviewDecision
from anvil.state.snapshot import serialize_state
from tests.test_bundle_execution import _NOW, _backend, _event, _implement_bundle


def test_reconcile_is_idempotent_and_does_not_apply_member_tasks(tmp_path) -> None:
    backend = _backend(tmp_path)
    try:
        _implement_bundle(backend, tmp_path)
        for reviewer, angle in (
            ("reviewer-a", "correctness"),
            ("reviewer-b", "security"),
            ("reviewer-c", "integration"),
        ):
            BundleReviewManager(backend, FrozenClock(_NOW), actor=reviewer).record(
                "B001",
                review_round=1,
                angle=angle,
                decision=ReviewDecision.approve,
            )
        BundleReviewManager(
            backend, FrozenClock(_NOW), actor="coordinator"
        ).finalize("B001")
        delivery = BundleDeliveryManager(
            backend, FrozenClock(_NOW), actor="coordinator"
        )
        delivery.reconcile("B001", commit_sha="abc123")
        first_count = len((tmp_path / "events.jsonl").read_text().splitlines())
        BundleDeliveryManager(
            backend,
            FrozenClock(_NOW + timedelta(minutes=1)),
            actor="coordinator",
        ).reconcile("B001", commit_sha="abc123")
        assert len((tmp_path / "events.jsonl").read_text().splitlines()) == first_count
        assert backend.get_bundle("B001").status is BundleStatus.integrated  # type: ignore[union-attr]
        assert backend.get_bundle("B001").checkpoint.commit_sha == "abc123"  # type: ignore[union-attr]
        assert all(
            backend.get_task(task_id).status.value == "needs_review"  # type: ignore[union-attr]
            for task_id in ("release:T001", "release:T002")
        )
    finally:
        backend.close()


def test_supersession_preserves_source_and_replays_replacement(tmp_path) -> None:
    replay_root = tmp_path / "replay"
    replay_root.mkdir()
    backend = _backend(tmp_path)
    try:
        from tests.test_bundle_execution import _seed

        _seed(backend)
        backend.append(
            _event(
                "task.created",
                "task",
                "release:T003",
                {
                    "id": "release:T003",
                    "feature_id": "release:F001",
                    "prd_id": "release",
                    "title": "Replacement member",
                    "description": "",
                    "status": "ready",
                    "priority": "high",
                    "task_type": "feature",
                    "dependencies": [],
                    "conflict_groups": [],
                    "scores": {},
                    "acceptance_criteria": ["Done"],
                    "implementation_notes": [],
                    "verification": {"commands": ["verify"]},
                    "likely_files": ["src/3.py"],
                    "parent_task_id": None,
                    "created_at": _NOW.isoformat(),
                    "updated_at": _NOW.isoformat(),
                },
            )
        )
        backend.append(
            _event(
                "bundle.created",
                "bundle",
                "B002",
                {
                    "id": "B002",
                    "prd_id": "release",
                    "task_ids": ["release:T003"],
                    "coordinator": "coordinator",
                    "status": "planned",
                    "review_policy": {},
                    "throughput_budget": {},
                    "created_at": _NOW.isoformat(),
                    "updated_at": _NOW.isoformat(),
                },
            )
        )
        BundleDeliveryManager(
            backend, FrozenClock(_NOW), actor="coordinator"
        ).supersede("B001", replacement_bundle_id="B002")
        assert backend.get_bundle("B001").status is BundleStatus.superseded  # type: ignore[union-attr]
        assert backend.get_bundle("B001").superseded_by == "B002"  # type: ignore[union-attr]
        source = serialize_state(backend)
    finally:
        backend.close()
    replay = _backend(replay_root)
    try:
        replay.replay_from_empty(str(tmp_path / "events.jsonl"))
        assert serialize_state(replay) == source
    finally:
        replay.close()
