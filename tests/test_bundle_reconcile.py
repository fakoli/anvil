"""Bundle checkpoint, reconciliation, supersession, and replay."""

from __future__ import annotations

from datetime import timedelta

import pytest

from anvil.bundles.delivery import BundleDeliveryError, BundleDeliveryManager
from anvil.bundles.review import BundleReviewManager
from anvil.clock import FrozenClock
from anvil.state.backend import EventRejected
from anvil.state.models import BundleStatus, Event, EventDraft, ReviewDecision
from anvil.state.snapshot import serialize_state
from tests.test_bundle_execution import (
    _NOW,
    _append_raw,
    _backend,
    _event,
    _implement_bundle,
    _next_event_id,
)


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
        future = _NOW + timedelta(minutes=10)
        backend.append(
            EventDraft(
                timestamp=future,
                actor="seed",
                action="task.created",
                target_kind="task",
                target_id="release:T004",
                payload_json={
                    "id": "release:T004",
                    "feature_id": "release:F001",
                    "prd_id": "release",
                    "title": "Future replacement member",
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
                    "likely_files": ["src/4.py"],
                    "parent_task_id": None,
                    "created_at": future.isoformat(),
                    "updated_at": future.isoformat(),
                },
            )
        )
        backend.append(
            EventDraft(
                timestamp=future,
                actor="seed",
                action="bundle.created",
                target_kind="bundle",
                target_id="B003",
                payload_json={
                    "id": "B003",
                    "prd_id": "release",
                    "task_ids": ["release:T004"],
                    "coordinator": "coordinator",
                    "status": "planned",
                    "review_policy": {},
                    "throughput_budget": {},
                    "created_at": future.isoformat(),
                    "updated_at": future.isoformat(),
                },
            )
        )
        with pytest.raises(BundleDeliveryError, match="predates replacement state"):
            BundleDeliveryManager(
                backend, FrozenClock(_NOW), actor="coordinator"
            ).supersede("B001", replacement_bundle_id="B003")
        source_bundle = backend.get_bundle("B001")
        assert source_bundle is not None
        _append_raw(
            tmp_path,
            Event(
                id=_next_event_id(tmp_path),
                timestamp=_NOW,
                actor="coordinator",
                action="bundle.superseded",
                target_kind="bundle",
                target_id="B001",
                payload_json={
                    "bundle_id": "B001",
                    "creation_event_id": source_bundle.creation_event_id,
                    "replacement_bundle_id": "B003",
                    "superseded_by_actor": "coordinator",
                    "superseded_at": _NOW.isoformat(),
                },
            ),
        )
        with pytest.raises(BundleDeliveryError, match="predates"):
            BundleDeliveryManager(
                backend,
                FrozenClock(_NOW - timedelta(days=1)),
                actor="coordinator",
            ).supersede("B001", replacement_bundle_id="B002")
        conn = backend._require_conn()
        conn.execute("UPDATE execution_bundles SET status = 'completed' WHERE id = 'B002'")
        conn.commit()
        with pytest.raises(BundleDeliveryError, match="replacement is terminal"):
            BundleDeliveryManager(
                backend, FrozenClock(_NOW), actor="coordinator"
            ).supersede("B001", replacement_bundle_id="B002")
        conn.execute("UPDATE execution_bundles SET status = 'planned' WHERE id = 'B002'")
        conn.commit()
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
        assert replay.get_bundle("B003").status is BundleStatus.planned  # type: ignore[union-attr]
    finally:
        replay.close()


def test_delivery_events_reject_forged_and_backdated_mutations(tmp_path) -> None:
    replay_root = tmp_path / "replay"
    replay_root.mkdir()
    backend = _backend(tmp_path)
    try:
        from tests.test_bundle_execution import _seed

        _seed(backend)
        bundle = backend.get_bundle("B001")
        assert bundle is not None
        before = len((tmp_path / "events.jsonl").read_text().splitlines())
        with pytest.raises(EventRejected, match="only the coordinator"):
            backend.append(
                EventDraft(
                    timestamp=_NOW,
                    actor="attacker",
                    action="bundle.checkpoint_recorded",
                    target_kind="bundle",
                    target_id="B001",
                    payload_json={
                        "bundle_id": "B001",
                        "creation_event_id": bundle.creation_event_id,
                        "checkpoint": {
                            "commit_sha": "attacker-sha",
                            "recorded_at": _NOW.isoformat(),
                            "recorded_by": "attacker",
                        },
                    },
                )
            )
        with pytest.raises(BundleDeliveryError, match="predates"):
            BundleDeliveryManager(
                backend,
                FrozenClock(_NOW - timedelta(minutes=1)),
                actor="coordinator",
            ).checkpoint("B001", commit_sha="old")
        assert len((tmp_path / "events.jsonl").read_text().splitlines()) == before
        _append_raw(
            tmp_path,
            Event(
                id=_next_event_id(tmp_path),
                timestamp=_NOW,
                actor="attacker",
                action="bundle.checkpoint_recorded",
                target_kind="bundle",
                target_id="B001",
                payload_json={
                    "bundle_id": "B001",
                    "creation_event_id": bundle.creation_event_id,
                    "checkpoint": {
                        "commit_sha": "attacker-sha",
                        "recorded_at": _NOW.isoformat(),
                        "recorded_by": "attacker",
                    },
                },
            ),
        )
    finally:
        backend.close()
    replay = _backend(replay_root)
    try:
        replay.replay_from_empty(str(tmp_path / "events.jsonl"))
        assert replay.get_bundle("B001").checkpoint is None  # type: ignore[union-attr]
    finally:
        replay.close()


def test_replay_result_time_ignores_forged_status_event(tmp_path) -> None:
    replay_root = tmp_path / "replay"
    replay_root.mkdir()
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
        bundle = backend.get_bundle("B001")
        assert bundle is not None
        assert bundle.last_result_at == _NOW
        forged_at = _NOW + timedelta(hours=1)
        _append_raw(
            tmp_path,
            Event(
                id=_next_event_id(tmp_path),
                timestamp=forged_at,
                actor="attacker",
                action="bundle.status_changed",
                target_kind="bundle",
                target_id="B001",
                payload_json={
                    "bundle_id": "B001",
                    "creation_event_id": bundle.creation_event_id,
                    "from": "reviewed_unintegrated",
                    "to": "integrated",
                    "changed_at": forged_at.isoformat(),
                },
            ),
        )
    finally:
        backend.close()

    replay = _backend(replay_root)
    try:
        replay.replay_from_empty(str(tmp_path / "events.jsonl"))
        replayed = replay.get_bundle("B001")
        assert replayed is not None
        assert replayed.status is BundleStatus.reviewed_unintegrated
        assert replayed.last_result_at == _NOW
    finally:
        replay.close()
