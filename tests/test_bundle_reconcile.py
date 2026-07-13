"""Bundle checkpoint, reconciliation, supersession, and replay."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from anvil.bundles.delivery import BundleDeliveryError, BundleDeliveryManager
from anvil.bundles.manager import BundleManager
from anvil.bundles.review import BundleReviewManager
from anvil.clock import FrozenClock
from anvil.state.backend import EventRejected
from anvil.state.models import BundleStatus, Event, EventDraft, ReviewDecision
from anvil.state.schema import SCHEMA_VERSION
from anvil.state.snapshot import serialize_state
from anvil.state.sqlite import SqliteBackend
from tests.test_bundle_execution import (
    _NOW,
    _append_raw,
    _backend,
    _event,
    _implement_bundle,
    _manager,
    _next_event_id,
    _seed,
)


def _integrate_bundle(backend, root) -> None:
    _implement_bundle(backend, root)
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
    BundleReviewManager(backend, FrozenClock(_NOW), actor="coordinator").finalize(
        "B001"
    )
    BundleDeliveryManager(
        backend, FrozenClock(_NOW), actor="coordinator"
    ).reconcile("B001", commit_sha="abc123")
    assert backend.get_bundle("B001").status is BundleStatus.integrated  # type: ignore[union-attr]


def _project_legacy_terminal_event(backend, root, status: BundleStatus) -> Event:
    bundle = backend.get_bundle("B001")
    claim = backend.get_bundle_claim("B001")
    assert bundle is not None
    assert claim is not None and claim.status.value == "active"
    changed_at = _NOW + timedelta(minutes=1)
    event = Event(
        id=_next_event_id(root),
        timestamp=changed_at,
        actor="coordinator",
        action="bundle.status_changed",
        target_kind="bundle",
        target_id="B001",
        payload_json={
            "bundle_id": "B001",
            "schema_version": SCHEMA_VERSION,
            "creation_event_id": bundle.creation_event_id,
            "bundle_claim_id": claim.id,
            "from": "integrated",
            "to": status.value,
            "changed_at": changed_at.isoformat(),
        },
    )
    _append_raw(root, event)
    backend._apply_write_only(backend._require_conn(), event)  # noqa: SLF001
    return event


def test_reconcile_merged_releases_claim_idempotently_and_replays(tmp_path) -> None:
    replay_root = tmp_path / "replay"
    replay_root.mkdir()
    backend = _backend(tmp_path)
    try:
        _integrate_bundle(backend, tmp_path)
        bundle = backend.get_bundle("B001")
        active_claim = backend.get_bundle_claim("B001")
        assert bundle is not None
        assert active_claim is not None
        before_rejected = len((tmp_path / "events.jsonl").read_text().splitlines())
        with pytest.raises(EventRejected, match="must release"):
            backend.append(
                EventDraft(
                    timestamp=_NOW,
                    actor="coordinator",
                    action="bundle.status_changed",
                    target_kind="bundle",
                    target_id="B001",
                    payload_json={
                        "bundle_id": "B001",
                        "creation_event_id": bundle.creation_event_id,
                        "bundle_claim_id": active_claim.id,
                        "from": "integrated",
                        "to": "merged",
                        "changed_at": _NOW.isoformat(),
                    },
                )
            )
        assert (
            len((tmp_path / "events.jsonl").read_text().splitlines())
            == before_rejected
        )
        delivery = BundleDeliveryManager(
            backend, FrozenClock(_NOW), actor="coordinator"
        )
        delivery.reconcile("B001", commit_sha="abc123", merged=True)
        first_count = len((tmp_path / "events.jsonl").read_text().splitlines())
        BundleDeliveryManager(
            backend,
            FrozenClock(_NOW + timedelta(minutes=1)),
            actor="coordinator",
        ).reconcile("B001", commit_sha="abc123", merged=True)
        assert len((tmp_path / "events.jsonl").read_text().splitlines()) == first_count
        assert backend.get_bundle("B001").status is BundleStatus.merged  # type: ignore[union-attr]
        assert backend.get_bundle("B001").checkpoint.commit_sha == "abc123"  # type: ignore[union-attr]
        claim = backend.get_bundle_claim("B001")
        assert claim is not None
        assert claim.status.value == "released"
        assert claim.released_at == _NOW
        assert backend.list_active_claims() == []
        assert all(
            backend.get_task(task_id).status.value == "needs_review"  # type: ignore[union-attr]
            for task_id in ("release:T001", "release:T002")
        )
        source = serialize_state(backend)
    finally:
        backend.close()
    replay = _backend(replay_root)
    try:
        replay.replay_from_empty(str(tmp_path / "events.jsonl"))
        assert serialize_state(replay) == source
        assert replay.get_bundle_claim("B001").status.value == "released"  # type: ignore[union-attr]
    finally:
        replay.close()


def test_interrupted_merged_projection_rolls_back_then_catches_up(
    tmp_path, monkeypatch
) -> None:
    backend = _backend(tmp_path)
    _integrate_bundle(backend, tmp_path)
    original = SqliteBackend._write_bundle_claim_terminal

    def fail_after_claim_release(*args, **kwargs) -> None:
        original(*args, **kwargs)
        raise RuntimeError("injected failure after terminal claim release")

    with monkeypatch.context() as patch:
        patch.setattr(
            SqliteBackend,
            "_write_bundle_claim_terminal",
            staticmethod(fail_after_claim_release),
        )
        with pytest.raises(BundleDeliveryError, match="Transaction aborted"):
            BundleDeliveryManager(
                backend,
                FrozenClock(_NOW + timedelta(minutes=1)),
                actor="coordinator",
            ).reconcile("B001", commit_sha="abc123", merged=True)
        assert backend.get_bundle("B001").status is BundleStatus.integrated  # type: ignore[union-attr]
        assert backend.get_bundle_claim("B001").status.value == "active"  # type: ignore[union-attr]
    backend.close()

    healed = _backend(tmp_path)
    try:
        assert healed.get_bundle("B001").status is BundleStatus.merged  # type: ignore[union-attr]
        assert healed.get_bundle_claim("B001").status.value == "released"  # type: ignore[union-attr]
    finally:
        healed.close()


@pytest.mark.parametrize("terminal_status", [BundleStatus.merged, BundleStatus.completed])
def test_reconcile_repairs_legacy_terminal_claim_once_and_replays(
    tmp_path, terminal_status
) -> None:
    replay_root = tmp_path / "replay"
    replay_root.mkdir()
    backend = _backend(tmp_path)
    try:
        _integrate_bundle(backend, tmp_path)
        _project_legacy_terminal_event(backend, tmp_path, terminal_status)
        assert backend.get_bundle_claim("B001").status.value == "active"  # type: ignore[union-attr]
        repair = BundleDeliveryManager(
            backend,
            FrozenClock(_NOW + timedelta(minutes=2)),
            actor="coordinator",
        )
        repair.reconcile("B001", commit_sha="abc123", merged=True)
        first_count = len((tmp_path / "events.jsonl").read_text().splitlines())
        repair.reconcile("B001", commit_sha="abc123", merged=True)
        assert len((tmp_path / "events.jsonl").read_text().splitlines()) == first_count
        assert backend.get_bundle("B001").status is terminal_status  # type: ignore[union-attr]
        claim = backend.get_bundle_claim("B001")
        assert claim is not None
        assert claim.status.value == "released"
        assert claim.released_at == _NOW + timedelta(minutes=2)
        assert claim.release_reason == "terminal reconciliation repair"
        source = serialize_state(backend)
    finally:
        backend.close()
    replay = _backend(replay_root)
    try:
        replay.replay_from_empty(str(tmp_path / "events.jsonl"))
        assert serialize_state(replay) == source
    finally:
        replay.close()


def test_replay_preserves_explicit_release_metadata_after_legacy_merge(
    tmp_path,
) -> None:
    replay_root = tmp_path / "replay"
    replay_root.mkdir()
    backend = _backend(tmp_path)
    released_at = _NOW + timedelta(minutes=3)
    release_reason = "operator confirmed delivery"
    try:
        _integrate_bundle(backend, tmp_path)
        _project_legacy_terminal_event(backend, tmp_path, BundleStatus.merged)
        claim = backend.get_bundle_claim("B001")
        assert claim is not None and claim.status.value == "active"
        backend.append(
            EventDraft(
                timestamp=released_at,
                actor="coordinator",
                action="bundle.claim_released",
                target_kind="bundle",
                target_id="B001",
                payload_json={
                    "bundle_claim_id": claim.id,
                    "bundle_id": "B001",
                    "released_by": "coordinator",
                    "release_reason": release_reason,
                },
            )
        )
        released = backend.get_bundle_claim("B001")
        assert released is not None
        assert released.status.value == "released"
        assert released.released_at == released_at
        assert released.release_reason == release_reason
        source = serialize_state(backend)
    finally:
        backend.close()

    replay = _backend(replay_root)
    try:
        replay.replay_from_empty(str(tmp_path / "events.jsonl"))
        assert serialize_state(replay) == source
        released = replay.get_bundle_claim("B001")
        assert released is not None
        assert released.released_at == released_at
        assert released.release_reason == release_reason
    finally:
        replay.close()


def test_concurrent_legacy_repairs_are_idempotent(tmp_path) -> None:
    primary = _backend(tmp_path)
    _integrate_bundle(primary, tmp_path)
    _project_legacy_terminal_event(primary, tmp_path, BundleStatus.merged)
    secondary = _backend(tmp_path)
    barrier = threading.Barrier(2)

    def synchronize_claim_read(backend):
        original = backend.get_bundle_claim

        def read(bundle_id):
            claim = original(bundle_id)
            if claim is not None and claim.status.value == "active":
                barrier.wait(timeout=5)
            return claim

        backend.get_bundle_claim = read

    synchronize_claim_read(primary)
    synchronize_claim_read(secondary)
    before = len((tmp_path / "events.jsonl").read_text().splitlines())

    def reconcile(backend) -> None:
        BundleDeliveryManager(
            backend,
            FrozenClock(_NOW + timedelta(minutes=2)),
            actor="coordinator",
        ).reconcile("B001", commit_sha="abc123", merged=True)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(reconcile, (primary, secondary)))
        assert len((tmp_path / "events.jsonl").read_text().splitlines()) == before + 1
        assert primary.get_bundle_claim("B001").status.value == "released"  # type: ignore[union-attr]
    finally:
        secondary.close()
        primary.close()


def test_nonterminal_claim_release_intent_fails_closed_live_and_replay(
    tmp_path,
) -> None:
    replay_root = tmp_path / "replay"
    replay_root.mkdir()
    backend = _backend(tmp_path)
    try:
        _integrate_bundle(backend, tmp_path)
        claimed = backend.get_bundle_claim("B001")
        bundle = backend.get_bundle("B001")
        assert claimed is not None
        assert bundle is not None
        before = len((tmp_path / "events.jsonl").read_text().splitlines())
        draft = EventDraft(
            timestamp=_NOW,
            actor="coordinator",
            action="bundle.status_changed",
            target_kind="bundle",
            target_id="B001",
            payload_json={
                "bundle_id": "B001",
                "schema_version": SCHEMA_VERSION,
                "creation_event_id": bundle.creation_event_id,
                "bundle_claim_id": claimed.id,
                "release_claim": True,
                "from": "integrated",
                "to": "replan_required",
                "changed_at": _NOW.isoformat(),
            },
        )
        with pytest.raises(EventRejected, match="requires a terminal status"):
            backend.append(draft)
        assert len((tmp_path / "events.jsonl").read_text().splitlines()) == before
        forged = Event(
            id=_next_event_id(tmp_path),
            **draft.model_dump(),
        )
        _append_raw(tmp_path, forged)
    finally:
        backend.close()

    replay = _backend(replay_root)
    try:
        replay.replay_from_empty(str(tmp_path / "events.jsonl"))
        assert replay.get_bundle("B001").status is BundleStatus.integrated  # type: ignore[union-attr]
        assert replay.get_bundle_claim("B001").status.value == "active"  # type: ignore[union-attr]
    finally:
        replay.close()


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
                    "schema_version": SCHEMA_VERSION,
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
        source_claim = _manager(backend, tmp_path).claim("B001").claim
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
                    "schema_version": SCHEMA_VERSION,
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
        assert backend.get_bundle_claim("B001").status.value == "released"  # type: ignore[union-attr]
        assert all(
            backend.get_claim(claim_id).status.value == "released"  # type: ignore[union-attr]
            for claim_id in source_claim.member_claim_ids.values()
        )
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


def test_replanned_bundle_can_be_superseded_by_same_members_and_reclaimed(
    tmp_path,
) -> None:
    replay_root = tmp_path / "replay-same-members"
    replay_root.mkdir()
    backend = _backend(tmp_path)
    try:
        _implement_bundle(backend, tmp_path)
        original_evidence = {
            task_id: backend.get_latest_evidence(task_id).id  # type: ignore[union-attr]
            for task_id in ("release:T001", "release:T002")
        }
        for review_round, prefix in ((1, "first"), (2, "second")):
            for reviewer, angle in (
                (f"{prefix}-a", "correctness"),
                (f"{prefix}-b", "security"),
                (f"{prefix}-c", "integration"),
            ):
                BundleReviewManager(
                    backend, FrozenClock(_NOW), actor=reviewer
                ).record(
                    "B001",
                    review_round=review_round,
                    angle=angle,
                    decision=(
                        ReviewDecision.needs_changes
                        if angle == "security"
                        else ReviewDecision.approve
                    ),
                    notes="blocking rework" if angle == "security" else None,
                )
        BundleReviewManager(
            backend, FrozenClock(_NOW), actor="coordinator"
        ).finalize("B001")
        assert backend.get_bundle("B001").status is BundleStatus.replan_required  # type: ignore[union-attr]

        replacement_at = _NOW + timedelta(minutes=1)
        backend.append(
            EventDraft(
                timestamp=replacement_at,
                actor="coordinator",
                action="bundle.created",
                target_kind="bundle",
                target_id="B002",
                payload_json={
                    "id": "B002",
                    "schema_version": SCHEMA_VERSION,
                    "prd_id": "release",
                    "task_ids": ["release:T001", "release:T002"],
                    "coordinator": "coordinator",
                    "status": "planned",
                    "review_policy": {},
                    "throughput_budget": {},
                    "created_at": replacement_at.isoformat(),
                    "updated_at": replacement_at.isoformat(),
                },
            )
        )
        before_resurrection = len(
            (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
        )
        with pytest.raises(EventRejected, match="replacement bundle 'B002'"):
            backend.append(
                EventDraft(
                    timestamp=_NOW + timedelta(minutes=1, seconds=1),
                    actor="coordinator",
                    action="bundle.status_changed",
                    target_kind="bundle",
                    target_id="B001",
                    payload_json={
                        "bundle_id": "B001",
                        "creation_event_id": backend.get_bundle(
                            "B001"
                        ).creation_event_id,  # type: ignore[union-attr]
                        "from": "replan_required",
                        "to": "planned",
                        "changed_at": (
                            _NOW + timedelta(minutes=1, seconds=1)
                        ).isoformat(),
                    },
                )
            )
        assert (
            len((tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines())
            == before_resurrection
        )
        BundleDeliveryManager(
            backend,
            FrozenClock(_NOW + timedelta(minutes=2)),
            actor="coordinator",
        ).supersede("B001", replacement_bundle_id="B002")
        assert backend.get_bundle("B001").status is BundleStatus.superseded  # type: ignore[union-attr]
        assert all(
            backend.get_task(task_id).status.value == "ready"  # type: ignore[union-attr]
            for task_id in original_evidence
        )
        assert {
            task_id: backend.get_latest_evidence(task_id).id  # type: ignore[union-attr]
            for task_id in original_evidence
        } == original_evidence

        replacement_manager = BundleManager(
            backend,
            FrozenClock(_NOW + timedelta(minutes=3)),
            actor="coordinator",
            project_root=tmp_path,
        )
        claimed = replacement_manager.claim("B002")
        assert claimed.bundle.status is BundleStatus.active
        source = serialize_state(backend)
    finally:
        backend.close()

    replay = _backend(replay_root)
    try:
        replay.replay_from_empty(str(tmp_path / "events.jsonl"))
        assert serialize_state(replay) == source
    finally:
        replay.close()


def test_late_supersession_does_not_rewind_replacement_evidence(tmp_path) -> None:
    replay_root = tmp_path / "replay-late-supersession"
    replay_root.mkdir()
    backend = _backend(tmp_path)
    try:
        _seed(backend)
        source_manager = _manager(backend, tmp_path)
        source_manager.claim("B001")
        source_manager.release("B001", reason="replace generation")
        replacement_at = _NOW + timedelta(minutes=1)
        backend.append(
            EventDraft(
                timestamp=replacement_at,
                actor="coordinator",
                action="bundle.created",
                target_kind="bundle",
                target_id="B002",
                payload_json={
                    "id": "B002",
                    "schema_version": SCHEMA_VERSION,
                    "prd_id": "release",
                    "task_ids": ["release:T001", "release:T002"],
                    "coordinator": "coordinator",
                    "status": "planned",
                    "review_policy": {},
                    "throughput_budget": {},
                    "created_at": replacement_at.isoformat(),
                    "updated_at": replacement_at.isoformat(),
                },
            )
        )
        replacement_manager = BundleManager(
            backend,
            FrozenClock(_NOW + timedelta(minutes=2)),
            actor="coordinator",
            project_root=tmp_path,
        )
        claimed = replacement_manager.claim("B002")
        for index, task_id in enumerate(("release:T001", "release:T002"), start=1):
            evidence_at = _NOW + timedelta(minutes=2, seconds=index)
            backend.append(
                EventDraft(
                    timestamp=evidence_at,
                    actor="coordinator",
                    action="evidence.submitted",
                    target_kind="task",
                    target_id=task_id,
                    payload_json={
                        "task_id": task_id,
                        "claim_id": claimed.claim.member_claim_ids[task_id],
                        "submitted_by": "coordinator",
                        "evidence_id": f"EV-B2-{index}",
                        "commands_run": [f"verify-{task_id}"],
                        "files_changed": [f"src/{index}.py"],
                    },
                )
            )
        BundleManager(
            backend,
            FrozenClock(_NOW + timedelta(minutes=3)),
            actor="coordinator",
            project_root=tmp_path,
        ).mark_implemented("B002")
        BundleDeliveryManager(
            backend,
            FrozenClock(_NOW + timedelta(minutes=4)),
            actor="coordinator",
        ).supersede("B001", replacement_bundle_id="B002")
        assert backend.get_bundle("B002").status is BundleStatus.implemented_unreviewed  # type: ignore[union-attr]
        assert all(
            backend.get_task(task_id).status.value == "needs_review"  # type: ignore[union-attr]
            for task_id in ("release:T001", "release:T002")
        )
        assert [
            backend.get_latest_evidence(task_id).id  # type: ignore[union-attr]
            for task_id in ("release:T001", "release:T002")
        ] == ["EV-B2-1", "EV-B2-2"]
        source = serialize_state(backend)
    finally:
        backend.close()
    replay = _backend(replay_root)
    try:
        replay.replay_from_empty(str(tmp_path / "events.jsonl"))
        assert serialize_state(replay) == source
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
        with pytest.raises(EventRejected, match="changed_at must match event time"):
            backend.append(
                EventDraft(
                    timestamp=forged_at,
                    actor="coordinator",
                    action="bundle.status_changed",
                    target_kind="bundle",
                    target_id="B001",
                    payload_json={
                        "bundle_id": "B001",
                        "creation_event_id": bundle.creation_event_id,
                        "from": "reviewed_unintegrated",
                        "to": "integrated",
                        "changed_at": (forged_at + timedelta(days=1)).isoformat(),
                    },
                )
            )
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


def test_replay_rejects_illegal_bundle_status_transition(tmp_path) -> None:
    replay_root = tmp_path / "replay"
    replay_root.mkdir()
    backend = _backend(tmp_path)
    try:
        from tests.test_bundle_execution import _seed

        _seed(backend)
        bundle = backend.get_bundle("B001")
        assert bundle is not None
        _append_raw(
            tmp_path,
            Event(
                id=_next_event_id(tmp_path),
                timestamp=_NOW,
                actor="coordinator",
                action="bundle.status_changed",
                target_kind="bundle",
                target_id="B001",
                payload_json={
                    "bundle_id": "B001",
                    "creation_event_id": bundle.creation_event_id,
                    "from": "planned",
                    "to": "completed",
                    "changed_at": _NOW.isoformat(),
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
        assert replayed.status is BundleStatus.planned
        assert replayed.last_result_at is None
    finally:
        replay.close()
