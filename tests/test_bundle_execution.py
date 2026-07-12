"""Coordinator-first execution flow for milestone bundles (issue #171)."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from anvil.bundles.manager import BundleError, BundleManager
from anvil.bundles.review import BundleReviewError, BundleReviewManager
from anvil.claims.manager import ClaimError, ClaimManager
from anvil.claims.stale import detect_and_release_stale
from anvil.cli import app
from anvil.clock import FrozenClock
from anvil.state.backend import EventRejected
from anvil.state.models import BundleStatus, Event, EventDraft, ReviewDecision
from anvil.state.snapshot import serialize_state
from anvil.state.sqlite import SqliteBackend

_NOW = datetime(2026, 7, 11, 18, 0, tzinfo=UTC)


def _event(
    action: str,
    target_kind: str,
    target_id: str,
    payload: dict,
    *,
    actor: str = "seed",
) -> EventDraft:
    return EventDraft(
        timestamp=_NOW,
        actor=actor,
        action=action,
        target_kind=target_kind,
        target_id=target_id,
        payload_json=payload,
    )


def _backend(root: Path) -> SqliteBackend:
    events = root / "events.jsonl"
    events.touch()
    backend = SqliteBackend(
        db_path=str(root / "state.db"),
        events_path=str(events),
        clock=FrozenClock(_NOW),
    )
    backend.initialize()
    return backend


def _append_raw(root: Path, event: Event) -> None:
    with (root / "events.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(event.model_dump_json() + "\n")


def _next_event_id(root: Path) -> str:
    count = len((root / "events.jsonl").read_text(encoding="utf-8").splitlines())
    return f"E{count + 1:06d}"


def _bundle_claim_payload(backend: SqliteBackend, claim_id: str) -> dict:
    bundle = backend.get_bundle("B001")
    assert bundle is not None
    return {
        "id": claim_id,
        "bundle_id": "B001",
        "creation_event_id": bundle.creation_event_id,
        "claimed_by": "coordinator",
        "expected_files": ["src/1.py", "src/shared.py", "src/2.py"],
        "member_claims": [
            {"id": f"{claim_id}-1", "task_id": "release:T001"},
            {"id": f"{claim_id}-2", "task_id": "release:T002"},
        ],
        "created_at": _NOW.isoformat(),
        "lease_expires_at": (_NOW + timedelta(hours=4)).isoformat(),
        "last_heartbeat_at": _NOW.isoformat(),
    }


def _seed(
    backend: SqliteBackend,
    *,
    internal_dependency: bool = False,
    external_dependency_status: str | None = None,
    required_evidence: list[str] | None = None,
) -> str:
    backend.append(
        _event(
            "project.created",
            "project",
            "proj",
            {
                "id": "proj",
                "name": "Bundle execution",
                "description": "",
                "created_at": _NOW.isoformat(),
                "updated_at": _NOW.isoformat(),
            },
        )
    )
    backend.append(
        _event(
            "prd.parsed",
            "prd",
            "release",
            {
                "project_id": "proj",
                "prd_id": "release",
                "title": "Release",
                "is_default": False,
                "status": "approved",
                "summary": "",
                "goals": [],
                "non_goals": [],
                "requirements": [
                    {
                        "id": "release:R001",
                        "prd_id": "release",
                        "prd_section": "Requirements",
                        "text": "Ship coherently.",
                        "source_paragraph": None,
                        "derived": False,
                    }
                ],
                "acceptance_criteria": [],
                "risks": [],
                "open_questions": [],
            },
        )
    )
    task_ids = ["release:T001", "release:T002"]
    if external_dependency_status is not None:
        task_ids.append("release:T000")
    backend.append(
        _event(
            "feature.created",
            "feature",
            "release:F001",
            {
                "id": "release:F001",
                "prd_id": "release",
                "title": "Execution",
                "description": "",
                "status": "ready",
                "requirements": ["release:R001"],
                "tasks": task_ids,
            },
        )
    )
    for index, task_id in enumerate(task_ids):
        dependencies: list[str] = []
        status = "ready"
        if task_id == "release:T002" and internal_dependency:
            dependencies = ["release:T001"]
        if task_id == "release:T002" and external_dependency_status is not None:
            dependencies = ["release:T000"]
        if task_id == "release:T000":
            status = external_dependency_status or "ready"
        backend.append(
            _event(
                "task.created",
                "task",
                task_id,
                {
                    "id": task_id,
                    "feature_id": "release:F001",
                    "prd_id": "release",
                    "title": task_id,
                    "description": f"Member {index}",
                    "status": status,
                    "priority": "high",
                    "task_type": "feature",
                    "dependencies": dependencies,
                    "conflict_groups": ["shared"] if task_id.endswith("T002") else [],
                    "scores": {},
                    "acceptance_criteria": [f"{task_id} accepted"],
                    "implementation_notes": [],
                    "verification": {
                        "commands": [f"verify-{task_id}"],
                        "required_evidence": (
                            required_evidence
                            if task_id == "release:T001" and required_evidence
                            else []
                        ),
                    },
                    "likely_files": [f"src/{task_id[-1]}.py", "src/shared.py"],
                    "parent_task_id": None,
                    "created_at": _NOW.isoformat(),
                    "updated_at": _NOW.isoformat(),
                },
            )
        )
    created = backend.append(
        _event(
            "bundle.created",
            "bundle",
            "B001",
            {
                "id": "B001",
                "prd_id": "release",
                "task_ids": ["release:T001", "release:T002"],
                "coordinator": "coordinator",
                "status": "planned",
                "review_policy": {},
                "throughput_budget": {},
                "delegated_agents": [
                    {
                        "id": "optional-worker",
                        "task_ids": ["release:T002"],
                        "status": "missing",
                        "observed_at": _NOW.isoformat(),
                    }
                ],
                "created_at": _NOW.isoformat(),
                "updated_at": _NOW.isoformat(),
            },
        )
    )
    assert created is not None
    return created.id


def _manager(backend: SqliteBackend, root: Path) -> BundleManager:
    return BundleManager(
        backend,
        FrozenClock(_NOW),
        actor="coordinator",
        project_root=root,
    )


def _implement_bundle(backend: SqliteBackend, root: Path) -> None:
    _seed(backend)
    manager = _manager(backend, root)
    claimed = manager.claim("B001")
    for index, task_id in enumerate(("release:T001", "release:T002"), start=1):
        backend.append(
            _event(
                "evidence.submitted",
                "task",
                task_id,
                {
                    "task_id": task_id,
                    "claim_id": claimed.claim.member_claim_ids[task_id],
                    "submitted_by": "coordinator",
                    "evidence_id": f"EV-REVIEW-{index}",
                    "commands_run": [f"verify-{task_id}"],
                    "files_changed": [f"src/{index}.py"],
                },
            )
        )
    assert manager.mark_implemented("B001").can_mark_implemented


def test_claim_creates_one_public_claim_and_ordered_member_authorizations(
    tmp_path: Path,
) -> None:
    backend = _backend(tmp_path)
    try:
        _seed(backend, internal_dependency=True)
        result = _manager(backend, tmp_path).claim(
            "B001", branch="agent/bundle-b001", worktree_path="C:/wt/b001"
        )
        assert result.bundle.status is BundleStatus.active
        assert result.bundle.branch == "agent/bundle-b001"
        assert result.claim.bundle_id == "B001"
        assert list(result.claim.member_claim_ids) == ["release:T001", "release:T002"]
        assert len(backend.list_bundle_claims()) == 1
        child_claims = backend.list_active_claims()
        assert {claim.bundle_claim_id for claim in child_claims} == {result.claim.id}
        assert {claim.task_id for claim in child_claims} == {
            "release:T001",
            "release:T002",
        }
        assert all(
            backend.get_task(task_id).status.value == "claimed"  # type: ignore[union-attr]
            for task_id in result.bundle.task_ids
        )
        packet = _manager(backend, tmp_path).packet("B001")
        assert [member["task"]["id"] for member in packet.json_data["members"]] == [
            "release:T001",
            "release:T002",
        ]
        assert "release:R001: Ship coherently." in packet.markdown
        assert packet.json_data["aggregate"]["shared_file_overlaps"] == [
            {
                "path": "src/shared.py",
                "tasks": ["release:T001", "release:T002"],
            }
        ]
        assert "verify-release:T001" in packet.markdown
        assert result.claim.member_claim_ids["release:T002"] in packet.markdown
    finally:
        backend.close()


def test_external_dependency_refuses_atomically_but_done_dependency_allows(
    tmp_path: Path,
) -> None:
    blocked_root = tmp_path / "blocked"
    allowed_root = tmp_path / "allowed"
    blocked_root.mkdir()
    allowed_root.mkdir()
    blocked = _backend(blocked_root)
    try:
        _seed(blocked, external_dependency_status="ready")
        before = (blocked_root / "events.jsonl").read_text(encoding="utf-8")
        with pytest.raises(BundleError, match="external dependencies are not done"):
            _manager(blocked, blocked_root).claim("B001")
        assert blocked.get_bundle_claim("B001") is None
        assert blocked.get_bundle("B001").status is BundleStatus.planned  # type: ignore[union-attr]
        assert (blocked_root / "events.jsonl").read_text(encoding="utf-8") == before
    finally:
        blocked.close()
    allowed = _backend(allowed_root)
    try:
        _seed(allowed, external_dependency_status="done")
        assert _manager(allowed, allowed_root).claim("B001").bundle.status is BundleStatus.active
    finally:
        allowed.close()


def test_partial_evidence_reports_exact_unproven_members_and_agents_do_not_gate(
    tmp_path: Path,
) -> None:
    backend = _backend(tmp_path)
    try:
        _seed(backend)
        manager = _manager(backend, tmp_path)
        result = manager.claim("B001")
        manager.note_progress(
            "B001", phase="build", member_task_ids=["release:T001"]
        )
        first_claim = result.claim.member_claim_ids["release:T001"]
        backend.append(
            _event(
                "evidence.submitted",
                "task",
                "release:T001",
                {
                    "task_id": "release:T001",
                    "claim_id": first_claim,
                    "submitted_by": "coordinator",
                    "evidence_id": "EV-T001",
                    "commands_run": ["verify-release:T001"],
                    "files_changed": ["src/1.py"],
                },
            )
        )
        partial = manager.mark_implemented("B001")
        assert not partial.can_mark_implemented
        assert list(partial.unproven_members) == ["release:T002"]
        assert backend.get_bundle("B001").status is BundleStatus.active  # type: ignore[union-attr]

        second_claim = result.claim.member_claim_ids["release:T002"]
        backend.append(
            _event(
                "evidence.submitted",
                "task",
                "release:T002",
                {
                    "task_id": "release:T002",
                    "claim_id": second_claim,
                    "submitted_by": "coordinator",
                    "evidence_id": "EV-T002",
                    "commands_run": ["verify-release:T002"],
                    "files_changed": ["src/2.py"],
                },
            )
        )
        complete = manager.mark_implemented("B001")
        assert complete.can_mark_implemented
        assert complete.unproven_members == {}
        assert (
            backend.get_bundle("B001").status
            is BundleStatus.implemented_unreviewed  # type: ignore[union-attr]
        )
        assert all(
            backend.get_task(task_id).status.value == "needs_review"  # type: ignore[union-attr]
            for task_id in ("release:T001", "release:T002")
        )
    finally:
        backend.close()


def test_three_independent_bundle_reviews_gate_transition_and_replay(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    replay_root = tmp_path / "replay"
    source_root.mkdir()
    replay_root.mkdir()
    source = _backend(source_root)
    try:
        _implement_bundle(source, source_root)
        for reviewer, angle in (
            ("reviewer-a", "correctness"),
            ("reviewer-b", "security"),
            ("reviewer-c", "integration"),
        ):
            BundleReviewManager(
                source, FrozenClock(_NOW), actor=reviewer
            ).record(
                "B001",
                review_round=1,
                angle=angle,
                decision=ReviewDecision.approve,
            )
        gate = BundleReviewManager(
            source, FrozenClock(_NOW), actor="coordinator"
        ).finalize("B001")
        assert gate.passed
        assert source.get_bundle("B001").status is BundleStatus.reviewed_unintegrated  # type: ignore[union-attr]
        assert len(source.list_bundle_reviews("B001")) == 3
        source_snapshot = serialize_state(source)
    finally:
        source.close()

    replay = _backend(replay_root)
    try:
        replay.replay_from_empty(str(source_root / "events.jsonl"))
        assert replay.get_bundle("B001").status is BundleStatus.reviewed_unintegrated  # type: ignore[union-attr]
        assert len(replay.list_bundle_reviews("B001")) == 3
        assert serialize_state(replay) == source_snapshot
    finally:
        replay.close()


def test_bundle_review_gate_rejects_self_review_duplicate_angles_and_forged_pass(
    tmp_path: Path,
) -> None:
    backend = _backend(tmp_path)
    try:
        _implement_bundle(backend, tmp_path)
        before = len((tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines())
        with pytest.raises(BundleReviewError, match="self-review"):
            BundleReviewManager(
                backend, FrozenClock(_NOW), actor="coordinator"
            ).record(
                "B001",
                review_round=1,
                angle="correctness",
                decision=ReviewDecision.approve,
            )
        assert len((tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()) == before
        for reviewer in ("reviewer-a", "reviewer-b", "reviewer-c"):
            BundleReviewManager(backend, FrozenClock(_NOW), actor=reviewer).record(
                "B001",
                review_round=1,
                angle="correctness",
                decision=ReviewDecision.approve,
            )
        gate = BundleReviewManager(
            backend, FrozenClock(_NOW), actor="coordinator"
        ).gate("B001")
        assert not gate.passed
        assert gate.missing_reviewers == 2
        with pytest.raises(BundleReviewError, match="prior round"):
            BundleReviewManager(
                backend, FrozenClock(_NOW), actor="reviewer-d"
            ).record(
                "B001",
                review_round=2,
                angle="security",
                decision=ReviewDecision.approve,
            )
        with pytest.raises(BundleReviewError, match="remains incomplete"):
            BundleReviewManager(
                backend, FrozenClock(_NOW), actor="coordinator"
            ).finalize("B001")
        bundle = backend.get_bundle("B001")
        assert bundle is not None
        with pytest.raises(EventRejected, match="review quorum is incomplete"):
            backend.append(
                _event(
                    "bundle.status_changed",
                    "bundle",
                    "B001",
                    {
                        "bundle_id": "B001",
                        "creation_event_id": bundle.creation_event_id,
                        "bundle_claim_id": backend.get_bundle_claim("B001").id,  # type: ignore[union-attr]
                        "from": "implemented_unreviewed",
                        "to": "reviewed_unintegrated",
                        "changed_at": _NOW.isoformat(),
                    },
                    actor="coordinator",
                )
            )
    finally:
        backend.close()


def test_second_blocking_review_round_forces_replan(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    try:
        _implement_bundle(backend, tmp_path)
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
                    notes="security blocker" if angle == "security" else None,
                )
            gate = BundleReviewManager(
                backend, FrozenClock(_NOW), actor="coordinator"
            ).gate("B001")
            assert gate.replan_required is (review_round == 2)
        BundleReviewManager(
            backend, FrozenClock(_NOW), actor="coordinator"
        ).finalize("B001")
        assert backend.get_bundle("B001").status is BundleStatus.replan_required  # type: ignore[union-attr]
    finally:
        backend.close()


def test_replay_ignores_coordinator_self_review_and_forged_gate_pass(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    replay_root = tmp_path / "replay"
    source_root.mkdir()
    replay_root.mkdir()
    source = _backend(source_root)
    try:
        _implement_bundle(source, source_root)
        bundle = source.get_bundle("B001")
        claim = source.get_bundle_claim("B001")
        assert bundle is not None and claim is not None
        _append_raw(
            source_root,
            Event(
                id=_next_event_id(source_root),
                timestamp=_NOW,
                actor="coordinator",
                action="bundle.review_recorded",
                target_kind="bundle",
                target_id="B001",
                payload_json={
                    "id": "BR-FORGED",
                    "bundle_id": "B001",
                    "creation_event_id": bundle.creation_event_id,
                    "review_round": 1,
                    "angle": "correctness",
                    "reviewed_by": "coordinator",
                    "decision": "approve",
                    "created_at": _NOW.isoformat(),
                },
            ),
        )
        _append_raw(
            source_root,
            Event(
                id=_next_event_id(source_root),
                timestamp=_NOW,
                actor="coordinator",
                action="bundle.status_changed",
                target_kind="bundle",
                target_id="B001",
                payload_json={
                    "bundle_id": "B001",
                    "creation_event_id": bundle.creation_event_id,
                    "bundle_claim_id": claim.id,
                    "from": "implemented_unreviewed",
                    "to": "reviewed_unintegrated",
                    "changed_at": _NOW.isoformat(),
                },
            ),
        )
    finally:
        source.close()
    replay = _backend(replay_root)
    try:
        replay.replay_from_empty(str(source_root / "events.jsonl"))
        assert replay.list_bundle_reviews("B001") == []
        assert replay.get_bundle("B001").status is BundleStatus.implemented_unreviewed  # type: ignore[union-attr]
    finally:
        replay.close()


def test_union_file_conflict_refuses_whole_bundle(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    try:
        _seed(backend)
        backend.append(
            _event(
                "task.created",
                "task",
                "release:T999",
                {
                    "id": "release:T999",
                    "feature_id": "release:F001",
                    "prd_id": "release",
                    "title": "External work",
                    "description": "",
                    "status": "ready",
                    "priority": "high",
                    "task_type": "feature",
                    "dependencies": [],
                    "conflict_groups": [],
                    "scores": {},
                    "acceptance_criteria": [],
                    "implementation_notes": [],
                    "verification": {"commands": ["verify"]},
                    "likely_files": ["src/external.py"],
                    "parent_task_id": None,
                    "created_at": _NOW.isoformat(),
                    "updated_at": _NOW.isoformat(),
                },
            )
        )
        backend.append(
            _event(
                "claim.created",
                "claim",
                "C-EXTERNAL",
                {
                    "id": "C-EXTERNAL",
                    "task_id": "release:T999",
                    "claimed_by": "other",
                    "claim_type": "task",
                    "status": "active",
                    "expected_files": ["src/shared.py"],
                    "created_at": _NOW.isoformat(),
                    "lease_expires_at": (_NOW + timedelta(hours=1)).isoformat(),
                    "last_heartbeat_at": _NOW.isoformat(),
                },
            )
        )
        with pytest.raises(BundleError, match="conflicts with active claims"):
            _manager(backend, tmp_path).claim("B001")
        assert backend.get_bundle_claim("B001") is None
        assert all(
            backend.get_task(task_id).status.value == "ready"  # type: ignore[union-attr]
            for task_id in ("release:T001", "release:T002")
        )
    finally:
        backend.close()


def test_bundle_claim_replays_byte_equivalently(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    replay_root = tmp_path / "replay"
    source_root.mkdir()
    replay_root.mkdir()
    source = _backend(source_root)
    replay = _backend(replay_root)
    try:
        _seed(source, internal_dependency=True)
        _manager(source, source_root).claim("B001", branch="agent/b001")
        replay.replay_from_empty(str(source_root / "events.jsonl"))
        assert replay.get_bundle("B001") == source.get_bundle("B001")
        assert replay.get_bundle_claim("B001") == source.get_bundle_claim("B001")
        assert replay.list_claims() == source.list_claims()
    finally:
        source.close()
        replay.close()


def test_public_lease_renew_and_stale_reap_are_atomic(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    try:
        _seed(backend)
        result = _manager(backend, tmp_path).claim("B001")
        with pytest.raises(EventRejected, match="detected_at must match event time"):
            backend.append(
                EventDraft(
                    timestamp=_NOW,
                    actor="system",
                    action="bundle.claim_stale",
                    target_kind="bundle",
                    target_id="B001",
                    payload_json={
                        "bundle_claim_id": result.claim.id,
                        "bundle_id": "B001",
                        "detected_at": (_NOW + timedelta(days=1)).isoformat(),
                        "actor": "system",
                    },
                )
            )
        assert backend.get_bundle_claim("B001").status.value == "active"  # type: ignore[union-attr]
        renewed_at = _NOW + timedelta(minutes=30)
        renewed = BundleManager(
            backend,
            FrozenClock(renewed_at),
            actor="coordinator",
            project_root=tmp_path,
        ).renew("B001")
        children = [
            backend.get_claim(claim_id)
            for claim_id in renewed.member_claim_ids.values()
        ]
        assert all(
            child is not None
            and child.lease_expires_at == renewed.lease_expires_at
            and child.last_heartbeat_at == renewed.last_heartbeat_at
            for child in children
        )

        reaped = detect_and_release_stale(
            backend, FrozenClock(_NOW + timedelta(hours=6))
        )
        assert reaped == [result.claim.id]
        assert backend.get_bundle_claim("B001").status.value == "stale"  # type: ignore[union-attr]
        assert backend.get_bundle("B001").status is BundleStatus.replan_required  # type: ignore[union-attr]
        assert all(
            backend.get_claim(claim_id).status.value == "stale"  # type: ignore[union-attr]
            for claim_id in result.claim.member_claim_ids.values()
        )
        assert all(
            backend.get_task(task_id).status.value == "ready"  # type: ignore[union-attr]
            for task_id in ("release:T001", "release:T002")
        )
    finally:
        backend.close()


def test_replanned_bundle_can_acquire_a_new_claim_generation(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    try:
        _seed(backend)
        manager = _manager(backend, tmp_path)
        first = manager.claim("B001")
        manager.release("B001", reason="replan")
        bundle = backend.get_bundle("B001")
        assert bundle is not None
        backend.append(
            EventDraft(
                timestamp=_NOW + timedelta(minutes=1),
                actor="coordinator",
                action="bundle.status_changed",
                target_kind="bundle",
                target_id="B001",
                payload_json={
                    "bundle_id": "B001",
                    "creation_event_id": bundle.creation_event_id,
                    "from": "replan_required",
                    "to": "planned",
                    "changed_at": (_NOW + timedelta(minutes=1)).isoformat(),
                },
            )
        )
        second = BundleManager(
            backend,
            FrozenClock(_NOW + timedelta(minutes=2)),
            actor="coordinator",
            project_root=tmp_path,
        ).claim("B001")
        assert second.claim.id != first.claim.id
        assert second.claim.status.value == "active"
        assert len(backend.list_bundle_claims()) == 2
    finally:
        backend.close()


def test_expired_public_claim_cannot_be_renewed(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    try:
        _seed(backend)
        _manager(backend, tmp_path).claim("B001")
        with pytest.raises(BundleError, match="lease has expired"):
            BundleManager(
                backend,
                FrozenClock(_NOW + timedelta(hours=5)),
                actor="coordinator",
                project_root=tmp_path,
            ).renew("B001")
    finally:
        backend.close()


def test_public_renewal_cannot_shorten_coordinator_or_child_leases(
    tmp_path: Path,
) -> None:
    backend = _backend(tmp_path)
    try:
        _seed(backend)
        claimed = _manager(backend, tmp_path).claim("B001")
        original_expiry = claimed.claim.lease_expires_at
        with pytest.raises(BundleError, match="must extend the lease"):
            BundleManager(
                backend,
                FrozenClock(_NOW + timedelta(minutes=1)),
                actor="coordinator",
                project_root=tmp_path,
                lease_minutes=1,
            ).renew("B001")
        assert backend.get_bundle_claim("B001").lease_expires_at == original_expiry  # type: ignore[union-attr]
        assert all(
            backend.get_claim(claim_id).lease_expires_at == original_expiry  # type: ignore[union-attr]
            for claim_id in claimed.claim.member_claim_ids.values()
        )
    finally:
        backend.close()


def test_same_coordinator_cannot_overlap_bundle_child_authorization(
    tmp_path: Path,
) -> None:
    backend = _backend(tmp_path)
    try:
        _seed(backend)
        _manager(backend, tmp_path).claim("B001")
        backend.append(
            _event(
                "task.created",
                "task",
                "release:T999",
                {
                    "id": "release:T999",
                    "feature_id": "release:F001",
                    "prd_id": "release",
                    "title": "External",
                    "description": "",
                    "status": "ready",
                    "priority": "high",
                    "task_type": "feature",
                    "dependencies": [],
                    "conflict_groups": [],
                    "scores": {},
                    "acceptance_criteria": [],
                    "implementation_notes": [],
                    "verification": {"commands": ["verify"]},
                    "likely_files": ["src/shared.py"],
                    "parent_task_id": None,
                    "created_at": _NOW.isoformat(),
                    "updated_at": _NOW.isoformat(),
                },
            )
        )
        with pytest.raises(ClaimError, match="expected_files overlap"):
            ClaimManager(
                backend, FrozenClock(_NOW), actor="coordinator"
            ).claim("release:T999", expected_files=["src/shared.py"])
    finally:
        backend.close()


def test_readiness_requires_declared_evidence_and_current_member_claim(
    tmp_path: Path,
) -> None:
    backend = _backend(tmp_path)
    try:
        _seed(backend, required_evidence=["screenshot"])
        manager = _manager(backend, tmp_path)
        result = manager.claim("B001")
        for task_id, claim_id in result.claim.member_claim_ids.items():
            backend.append(
                _event(
                    "evidence.submitted",
                    "task",
                    task_id,
                    {
                        "task_id": task_id,
                        "claim_id": claim_id,
                        "submitted_by": "coordinator",
                        "evidence_id": f"EV-{task_id[-1]}",
                        "commands_run": [f"verify-{task_id}"],
                        "files_changed": [],
                    },
                )
            )
        readiness = manager.readiness("B001")
        assert not readiness.can_mark_implemented
        assert readiness.unproven_members["release:T001"] == [
            "evidence missing: screenshot"
        ]
    finally:
        backend.close()


def test_packet_fails_closed_on_missing_canonical_requirement(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    try:
        _seed(backend)
        backend._conn.execute(  # noqa: SLF001
            "DELETE FROM requirements WHERE id = 'release:R001'"
        )
        with pytest.raises(BundleError, match="missing requirements"):
            _manager(backend, tmp_path).packet("B001")
    finally:
        backend.close()


def test_bundle_member_claim_id_collision_rejects_before_log(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    try:
        _seed(backend)
        backend._conn.execute(  # noqa: SLF001
            """INSERT INTO claims
               (id, task_id, claimed_by, claim_type, status, expected_files,
                created_at, lease_expires_at, last_heartbeat_at)
               VALUES ('C-COLLIDE', 'release:T001', 'old', 'task', 'released',
                       '[]', ?, ?, ?)""",
            (
                _NOW.isoformat(),
                (_NOW + timedelta(hours=1)).isoformat(),
                _NOW.isoformat(),
            ),
        )
        bundle = backend.get_bundle("B001")
        assert bundle is not None
        before = (tmp_path / "events.jsonl").read_text(encoding="utf-8")
        with pytest.raises(EventRejected, match="member claim ids already exist"):
            backend.append(
                _event(
                    "bundle.claimed",
                    "bundle",
                    "B001",
                    {
                        "id": "BC-NEW",
                        "bundle_id": "B001",
                        "creation_event_id": bundle.creation_event_id,
                        "claimed_by": "coordinator",
                        "expected_files": [
                            "src/1.py",
                            "src/shared.py",
                            "src/2.py",
                        ],
                        "member_claims": [
                            {"id": "C-COLLIDE", "task_id": "release:T001"},
                            {"id": "C-NEW", "task_id": "release:T002"},
                        ],
                        "created_at": _NOW.isoformat(),
                        "lease_expires_at": (
                            _NOW + timedelta(hours=4)
                        ).isoformat(),
                        "last_heartbeat_at": _NOW.isoformat(),
                    },
                    actor="coordinator",
                )
            )
        assert (tmp_path / "events.jsonl").read_text(encoding="utf-8") == before
    finally:
        backend.close()


def test_replay_fences_losing_bundle_claim_status_descendant(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    replay_root = tmp_path / "replay"
    source_root.mkdir()
    replay_root.mkdir()
    source = _backend(source_root)
    try:
        _seed(source)
        winner = _manager(source, source_root).claim("B001")
        loser_payload = _bundle_claim_payload(source, "BC-LOSER")
        _append_raw(
            source_root,
            Event(
                id=_next_event_id(source_root),
                timestamp=_NOW,
                actor="coordinator",
                action="bundle.claimed",
                target_kind="bundle",
                target_id="B001",
                payload_json=loser_payload,
            ),
        )
        _append_raw(
            source_root,
            Event(
                id=_next_event_id(source_root),
                timestamp=_NOW + timedelta(seconds=1),
                actor="coordinator",
                action="bundle.status_changed",
                target_kind="bundle",
                target_id="B001",
                payload_json={
                    "bundle_id": "B001",
                    "creation_event_id": source.get_bundle("B001").creation_event_id,  # type: ignore[union-attr]
                    "bundle_claim_id": "BC-LOSER",
                    "from": "active",
                    "to": "implemented_unreviewed",
                    "changed_at": (_NOW + timedelta(seconds=1)).isoformat(),
                },
            ),
        )
    finally:
        source.close()
    replay = _backend(replay_root)
    try:
        replay.replay_from_empty(str(source_root / "events.jsonl"))
        assert replay.get_bundle_claim("B001").id == winner.claim.id  # type: ignore[union-attr]
        assert replay.get_bundle("B001").status is BundleStatus.active  # type: ignore[union-attr]
    finally:
        replay.close()


def test_replay_ignores_attacker_bundle_release_descendant(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    replay_root = tmp_path / "replay"
    source_root.mkdir()
    replay_root.mkdir()
    source = _backend(source_root)
    try:
        _seed(source)
        winner = _manager(source, source_root).claim("B001")
        _append_raw(
            source_root,
            Event(
                id=_next_event_id(source_root),
                timestamp=_NOW + timedelta(minutes=1),
                actor="attacker",
                action="bundle.claim_released",
                target_kind="bundle",
                target_id="B001",
                payload_json={
                    "bundle_claim_id": winner.claim.id,
                    "bundle_id": "B001",
                    "released_by": "attacker",
                    "release_reason": "spoof",
                },
            ),
        )
    finally:
        source.close()
    replay = _backend(replay_root)
    try:
        replay.replay_from_empty(str(source_root / "events.jsonl"))
        assert replay.get_bundle_claim("B001").status.value == "active"  # type: ignore[union-attr]
        assert replay.get_bundle("B001").status is BundleStatus.active  # type: ignore[union-attr]
    finally:
        replay.close()


def test_replay_ignores_attacker_impersonating_coordinator_renewal(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    replay_root = tmp_path / "replay"
    source_root.mkdir()
    replay_root.mkdir()
    source = _backend(source_root)
    try:
        _seed(source)
        winner = _manager(source, source_root).claim("B001")
        original_expiry = winner.claim.lease_expires_at
        original_heartbeat = winner.claim.last_heartbeat_at
        _append_raw(
            source_root,
            Event(
                id=_next_event_id(source_root),
                timestamp=_NOW + timedelta(minutes=1),
                actor="attacker",
                action="bundle.claim_renewed",
                target_kind="bundle",
                target_id="B001",
                payload_json={
                    "bundle_claim_id": winner.claim.id,
                    "bundle_id": "B001",
                    "renewed_by": "coordinator",
                    "lease_expires_at": (_NOW + timedelta(hours=8)).isoformat(),
                    "last_heartbeat_at": (_NOW + timedelta(minutes=1)).isoformat(),
                },
            ),
        )
    finally:
        source.close()
    replay = _backend(replay_root)
    try:
        replay.replay_from_empty(str(source_root / "events.jsonl"))
        replayed = replay.get_bundle_claim("B001")
        assert replayed is not None
        assert replayed.lease_expires_at == original_expiry
        assert replayed.last_heartbeat_at == original_heartbeat
    finally:
        replay.close()


@pytest.mark.parametrize("bundle_first", [False, True])
def test_replay_aggregate_conflict_is_first_event_wins(
    tmp_path: Path, bundle_first: bool
) -> None:
    source_root = tmp_path / "source"
    replay_root = tmp_path / "replay"
    source_root.mkdir()
    replay_root.mkdir()
    source = _backend(source_root)
    try:
        _seed(source, external_dependency_status="done")
        bundle_payload = _bundle_claim_payload(source, "BC-RACE")
    finally:
        source.close()
    claim_payload = {
        "id": "C-EXTERNAL-RACE",
        "task_id": "release:T000",
        "claimed_by": "other",
        "claim_type": "task",
        "status": "active",
        "expected_files": ["src/shared.py"],
        "created_at": _NOW.isoformat(),
        "lease_expires_at": (_NOW + timedelta(hours=1)).isoformat(),
        "last_heartbeat_at": _NOW.isoformat(),
    }
    actions = (
        [("bundle.claimed", "bundle", "B001", "coordinator", bundle_payload),
         ("claim.created", "claim", "C-EXTERNAL-RACE", "other", claim_payload)]
        if bundle_first
        else [("claim.created", "claim", "C-EXTERNAL-RACE", "other", claim_payload),
              ("bundle.claimed", "bundle", "B001", "coordinator", bundle_payload)]
    )
    for offset, (action, kind, target, actor, payload) in enumerate(actions, start=1):
        event_time = _NOW + timedelta(seconds=offset)
        if action == "bundle.claimed":
            payload = {
                **payload,
                "created_at": event_time.isoformat(),
                "last_heartbeat_at": event_time.isoformat(),
                "lease_expires_at": (event_time + timedelta(hours=4)).isoformat(),
            }
        _append_raw(
            source_root,
            Event(
                id=_next_event_id(source_root),
                timestamp=event_time,
                actor=actor,
                action=action,
                target_kind=kind,
                target_id=target,
                payload_json=payload,
            ),
        )
    replay = _backend(replay_root)
    try:
        replay.replay_from_empty(str(source_root / "events.jsonl"))
        if bundle_first:
            assert replay.get_bundle_claim("B001") is not None
            assert replay.get_claim("C-EXTERNAL-RACE") is None
        else:
            assert replay.get_claim("C-EXTERNAL-RACE") is not None
            assert replay.get_bundle_claim("B001") is None
    finally:
        replay.close()


def test_bundle_claim_packet_and_progress_cli_share_coordinator_flow(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / ".anvil"
    state_dir.mkdir()
    subprocess.run(
        ["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True
    )
    backend = _backend(state_dir)
    try:
        _seed(backend)
    finally:
        backend.close()
    runner = CliRunner()
    env = {"ANVIL_STATE_LAYOUT": "local"}
    claimed = runner.invoke(
        app,
        [
            "claim",
            "B001",
            "--bundle",
            "--actor",
            "coordinator",
            "--branch",
            "agent/b001",
            "--cwd",
            str(tmp_path),
        ],
        env=env,
    )
    assert claimed.exit_code == 0, claimed.output
    assert "coordinator claim" in claimed.output

    packet = runner.invoke(
        app,
        ["packet", "B001", "--bundle", "--cwd", str(tmp_path)],
        env=env,
    )
    assert packet.exit_code == 0, packet.output
    assert "Ordered member work" in packet.output
    assert (state_dir / "packets" / "B001.md").is_file()

    progress = runner.invoke(
        app,
        [
            "progress",
            "B001",
            "build",
            "--bundle",
            "--actor",
            "coordinator",
            "--cwd",
            str(tmp_path),
        ],
        env=env,
    )
    assert progress.exit_code == 0, progress.output
    assert "Progress recorded for bundle" in progress.output

    backend = _backend(state_dir)
    try:
        bundle_claim = backend.get_bundle_claim("B001")
        assert bundle_claim is not None
        claim_id = bundle_claim.id
    finally:
        backend.close()
    renewed = runner.invoke(
        app,
        [
            "renew",
            claim_id,
            "--actor",
            "coordinator",
            "--lease",
            "300",
            "--cwd",
            str(tmp_path),
        ],
        env=env,
    )
    assert renewed.exit_code == 0, renewed.output
    released = runner.invoke(
        app,
        [
            "release",
            claim_id,
            "--force",
            "--actor",
            "recovery-operator",
            "--reason",
            "abandoned coordinator",
            "--cwd",
            str(tmp_path),
        ],
        env=env,
    )
    assert released.exit_code == 0, released.output
    backend = _backend(state_dir)
    try:
        bundle_claim = backend.get_bundle_claim("B001")
        assert bundle_claim is not None
        assert bundle_claim.status.value == "force_released"
        assert all(
            backend.get_claim(claim_id).status.value == "force_released"  # type: ignore[union-attr]
            for claim_id in bundle_claim.member_claim_ids.values()
        )
    finally:
        backend.close()
