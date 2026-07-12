"""Foundation tests for event-sourced execution bundles (issue #171)."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from anvil.clock import FrozenClock
from anvil.state.backend import EventRejected
from anvil.state.models import (
    BundleStatus,
    DelegatedAgentObservation,
    DelegatedAgentStatus,
    Event,
    EventDraft,
    ExecutionBundle,
)
from anvil.state.schema import SCHEMA_VERSION
from anvil.state.snapshot import serialize_state
from anvil.state.sqlite import SqliteBackend

_T0 = datetime(2026, 7, 11, 18, 0, tzinfo=UTC)


def _backend(root: Path) -> SqliteBackend:
    events = root / "events.jsonl"
    events.touch()
    backend = SqliteBackend(
        db_path=str(root / "state.db"),
        events_path=str(events),
        clock=FrozenClock(_T0),
    )
    backend.initialize()
    return backend


def _event(
    action: str,
    payload: dict[str, Any],
    *,
    target_kind: str,
    target_id: str,
) -> EventDraft:
    return EventDraft(
        timestamp=_T0,
        actor="coordinator",
        action=action,
        target_kind=target_kind,
        target_id=target_id,
        payload_json=payload,
    )


def _seed(backend: SqliteBackend, *, second_prd: bool = False) -> None:
    backend.append(
        _event(
            "project.created",
            {
                "id": "proj",
                "name": "Bundles",
                "description": "",
                "created_at": _T0.isoformat(),
                "updated_at": _T0.isoformat(),
            },
            target_kind="project",
            target_id="proj",
        )
    )
    for prd_id in (["release", "other"] if second_prd else ["release"]):
        requirement_id = f"{prd_id}:R001"
        backend.append(
            _event(
                "prd.parsed",
                {
                    "project_id": "proj",
                    "prd_id": prd_id,
                    "title": prd_id,
                    "is_default": False,
                    "status": "approved",
                    "summary": "Bundle test.",
                    "goals": ["Test bundles."],
                    "non_goals": [],
                    "requirements": [
                        {
                            "id": requirement_id,
                            "prd_id": prd_id,
                            "prd_section": "Requirements",
                            "text": "Bundle tasks.",
                            "source_paragraph": None,
                            "derived": False,
                        }
                    ],
                    "acceptance_criteria": ["Bundle persists."],
                    "risks": [],
                    "open_questions": [],
                },
                target_kind="prd",
                target_id=prd_id,
            )
        )
        feature_id = f"{prd_id}:F001"
        backend.append(
            _event(
                "feature.created",
                {
                    "id": feature_id,
                    "prd_id": prd_id,
                    "title": "Feature",
                    "description": "",
                    "status": "ready",
                    "requirements": [requirement_id],
                    "tasks": [f"{prd_id}:T001", f"{prd_id}:T002"],
                },
                target_kind="feature",
                target_id=feature_id,
            )
        )
        for suffix in ("T001", "T002"):
            task_id = f"{prd_id}:{suffix}"
            backend.append(
                _event(
                    "task.created",
                    {
                        "id": task_id,
                        "feature_id": feature_id,
                        "prd_id": prd_id,
                        "title": task_id,
                        "description": "",
                        "status": "ready",
                        "priority": "high",
                        "task_type": "feature",
                        "dependencies": [],
                        "conflict_groups": [],
                        "scores": {},
                        "acceptance_criteria": ["Done."],
                        "implementation_notes": [],
                        "verification": {"commands": ["pytest -q"]},
                        "likely_files": [],
                        "parent_task_id": None,
                        "created_at": _T0.isoformat(),
                        "updated_at": _T0.isoformat(),
                    },
                    target_kind="task",
                    target_id=task_id,
                )
            )


def _bundle_payload(
    bundle_id: str = "B001",
    task_ids: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": bundle_id,
        "prd_id": "release",
        "task_ids": task_ids or ["release:T002", "release:T001"],
        "coordinator": "codex-main",
        "status": "planned",
        "branch": "feat/bundles",
        "worktree_path": "C:/worktrees/bundles",
        "review_policy": {
            "max_reviews": 1,
            "max_rereviews": 1,
            "independent_reviewer_required": True,
            "required_angles": ["boundary"],
        },
        "throughput_budget": {"max_tasks": 12, "max_serial_stages": 6},
        "delegated_agents": [],
        "checkpoint": None,
        "created_at": _T0.isoformat(),
        "updated_at": _T0.isoformat(),
    }


def _claim_payload(claim_id: str, task_id: str) -> dict[str, Any]:
    return {
        "id": claim_id,
        "task_id": task_id,
        "claimed_by": "worker",
        "claim_type": "task",
        "status": "active",
        "created_at": _T0.isoformat(),
        "lease_expires_at": (_T0 + timedelta(hours=1)).isoformat(),
        "last_heartbeat_at": _T0.isoformat(),
        "branch": None,
        "worktree_path": None,
        "expected_files": [],
    }


def _create_bundle(
    backend: SqliteBackend,
    bundle_id: str = "B001",
    task_ids: list[str] | None = None,
) -> str:
    event = backend.append(
        _event(
            "bundle.created",
            _bundle_payload(bundle_id, task_ids),
            target_kind="bundle",
            target_id=bundle_id,
        )
    )
    assert event is not None
    return event.id


def _event_count(root: Path) -> int:
    return len((root / "events.jsonl").read_text(encoding="utf-8").splitlines())


def _append_raw_event(root: Path, event: Event) -> None:
    with (root / "events.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(event.model_dump_json() + "\n")


def test_bundle_model_preserves_order_and_rejects_duplicate_members() -> None:
    model = ExecutionBundle.model_validate(
        {**_bundle_payload(), "creation_event_id": "E000001"}
    )
    assert model.task_ids == ["release:T002", "release:T001"]
    with pytest.raises(ValidationError, match="task_ids must be unique"):
        ExecutionBundle.model_validate(
            {
                **_bundle_payload(task_ids=["release:T001", "release:T001"]),
                "creation_event_id": "E000001",
            }
        )


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"id": ""}, "id and coordinator must not be empty"),
        ({"coordinator": "   "}, "id and coordinator must not be empty"),
        (
            {"updated_at": (_T0 - timedelta(seconds=1)).isoformat()},
            "updated_at must not precede created_at",
        ),
    ],
)
def test_bundle_model_rejects_malformed_identity_and_chronology(
    override: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        ExecutionBundle.model_validate(
            {**_bundle_payload(), **override, "creation_event_id": "E000001"}
        )


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"id": ""}, "observation id must not be empty"),
        ({"task_ids": ["release:T001", "release:T001"]}, "must be unique"),
    ],
)
def test_delegated_agent_observation_rejects_malformed_identity_and_members(
    override: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        DelegatedAgentObservation.model_validate(
            {
                "id": "obs-1",
                "task_ids": [],
                "status": DelegatedAgentStatus.active,
                "observed_at": _T0,
                **override,
            }
        )


@pytest.mark.parametrize("status", list(DelegatedAgentStatus))
def test_delegated_agent_statuses_allow_missing_handles(
    status: DelegatedAgentStatus,
) -> None:
    observation = DelegatedAgentObservation(
        id=f"obs-{status.value}",
        handle=None,
        status=status,
        observed_at=_T0,
    )
    assert observation.status is status
    assert observation.handle is None


def test_create_round_trips_order_policy_and_filters(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    try:
        _seed(backend)
        creation_event_id = _create_bundle(backend)
        bundle = backend.get_bundle("B001")
        assert bundle is not None
        assert bundle.creation_event_id == creation_event_id
        assert bundle.task_ids == ["release:T002", "release:T001"]
        assert bundle.coordinator == "codex-main"
        assert bundle.review_policy.required_angles == ["boundary"]
        assert bundle.throughput_budget.max_serial_stages == 6
        assert backend.list_bundles(prd_id="release") == [bundle]
        assert backend.list_bundles(status="planned") == [bundle]
        assert backend.list_bundles(status="active") == []
    finally:
        backend.close()


@pytest.mark.parametrize(
    ("task_ids", "message"),
    [
        (["release:T404"], "member tasks not found"),
        (["release:T001", "release:T001"], "task_ids must be unique"),
        (["other:T001"], "another PRD"),
    ],
)
def test_creation_refusals_are_atomic(
    tmp_path: Path, task_ids: list[str], message: str
) -> None:
    backend = _backend(tmp_path)
    try:
        _seed(backend, second_prd=True)
        before = _event_count(tmp_path)
        with pytest.raises(EventRejected, match=message):
            _create_bundle(backend, task_ids=task_ids)
        assert backend.get_bundle("B001") is None
        assert _event_count(tmp_path) == before
    finally:
        backend.close()


def test_active_membership_and_claim_refusals_are_atomic(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    try:
        _seed(backend)
        _create_bundle(backend, "B001", ["release:T001"])
        before = _event_count(tmp_path)
        with pytest.raises(EventRejected, match="active execution bundle 'B001'"):
            backend.append(
                _event(
                    "claim.created",
                    _claim_payload("C-bundled", "release:T001"),
                    target_kind="claim",
                    target_id="C-bundled",
                )
            )
        assert backend.get_claim("C-bundled") is None
        assert _event_count(tmp_path) == before

        with pytest.raises(EventRejected, match="already belong to active bundles"):
            _create_bundle(backend, "B002", ["release:T001"])
        assert backend.get_bundle("B002") is None
        assert _event_count(tmp_path) == before

        backend.append(
            _event(
                "claim.created",
                _claim_payload("C001", "release:T002"),
                target_kind="claim",
                target_id="C001",
            )
        )
        before = _event_count(tmp_path)
        with pytest.raises(EventRejected, match="incompatible active claims"):
            _create_bundle(backend, "B003", ["release:T002"])
        assert backend.get_bundle("B003") is None
        assert _event_count(tmp_path) == before
    finally:
        backend.close()


def test_bundle_size_over_budget_rejects_before_sql_or_log(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    try:
        _seed(backend)
        payload = _bundle_payload(task_ids=[f"release:T{i:03d}" for i in range(13)])
        before = _event_count(tmp_path)
        with pytest.raises(EventRejected, match="throughput budget permits 12"):
            backend.append(
                _event(
                    "bundle.created",
                    payload,
                    target_kind="bundle",
                    target_id="B001",
                )
            )
        assert backend.get_bundle("B001") is None
        assert _event_count(tmp_path) == before
    finally:
        backend.close()


def test_bundle_event_targets_are_bound_before_log(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    try:
        _seed(backend)
        before = _event_count(tmp_path)
        with pytest.raises(EventRejected, match="event target must be bundle 'B001'"):
            backend.append(
                _event(
                    "bundle.created",
                    _bundle_payload(),
                    target_kind="task",
                    target_id="release:T002",
                )
            )
        assert backend.get_bundle("B001") is None
        assert _event_count(tmp_path) == before

        creation_event_id = _create_bundle(backend)
        for action, payload in (
            (
                "bundle.status_changed",
                {
                    "bundle_id": "B001",
                    "creation_event_id": creation_event_id,
                    "from": "planned",
                    "to": "active",
                    "changed_at": _T0.isoformat(),
                },
            ),
            (
                "bundle.agent_observed",
                {
                    "bundle_id": "B001",
                    "creation_event_id": creation_event_id,
                    "observation": {
                        "id": "wrong-target",
                        "task_ids": [],
                        "status": "active",
                        "observed_at": _T0.isoformat(),
                    },
                },
            ),
        ):
            before = _event_count(tmp_path)
            with pytest.raises(EventRejected, match="event target must be bundle 'B001'"):
                backend.append(
                    _event(
                        action,
                        payload,
                        target_kind="task",
                        target_id="release:T002",
                    )
                )
            assert _event_count(tmp_path) == before
    finally:
        backend.close()


def test_status_change_cannot_rewind_bundle_chronology(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    try:
        _seed(backend)
        creation_event_id = _create_bundle(backend)
        before = _event_count(tmp_path)
        with pytest.raises(EventRejected, match="must not precede"):
            backend.append(
                _event(
                    "bundle.status_changed",
                    {
                        "bundle_id": "B001",
                        "creation_event_id": creation_event_id,
                        "from": "planned",
                        "to": "active",
                        "changed_at": (_T0 - timedelta(days=1)).isoformat(),
                    },
                    target_kind="bundle",
                    target_id="B001",
                )
            )
        bundle = backend.get_bundle("B001")
        assert bundle is not None
        assert bundle.status is BundleStatus.planned
        assert bundle.updated_at == _T0
        assert _event_count(tmp_path) == before
    finally:
        backend.close()


def test_agent_observation_preserves_monotonic_bundle_chronology(
    tmp_path: Path,
) -> None:
    backend = _backend(tmp_path)
    try:
        _seed(backend)
        creation_event_id = _create_bundle(backend)
        later = _T0 + timedelta(hours=1)
        backend.append(
            _event(
                "bundle.status_changed",
                {
                    "bundle_id": "B001",
                    "creation_event_id": creation_event_id,
                    "from": "planned",
                    "to": "active",
                    "changed_at": later.isoformat(),
                },
                target_kind="bundle",
                target_id="B001",
            )
        )
        backend.append(
            _event(
                "bundle.agent_observed",
                {
                    "bundle_id": "B001",
                    "creation_event_id": creation_event_id,
                    "observation": {
                        "id": "late-arrival",
                        "task_ids": [],
                        "status": "active",
                        "observed_at": _T0.isoformat(),
                    },
                },
                target_kind="bundle",
                target_id="B001",
            )
        )
        bundle = backend.get_bundle("B001")
        assert bundle is not None
        assert bundle.updated_at == later
        assert bundle.delegated_agents[0].id == "late-arrival"
    finally:
        backend.close()


def test_claim_created_target_refusal_is_atomic(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    try:
        _seed(backend)
        before = _event_count(tmp_path)
        with pytest.raises(EventRejected, match="event target must be claim 'C001'"):
            backend.append(
                _event(
                    "claim.created",
                    _claim_payload("C001", "release:T001"),
                    target_kind="task",
                    target_id="release:T001",
                )
            )
        assert backend.get_claim("C001") is None
        assert backend.get_task("release:T001").status.value == "ready"  # type: ignore[union-attr]
        assert _event_count(tmp_path) == before
    finally:
        backend.close()


@pytest.mark.parametrize(
    ("claim_id", "message"),
    [
        ("C-other-task", "belongs to task 'release:T002'"),
        ("C-missing", "claim 'C-missing' not found"),
    ],
)
def test_evidence_claim_pairing_refuses_before_log(
    tmp_path: Path, claim_id: str, message: str
) -> None:
    backend = _backend(tmp_path)
    try:
        _seed(backend)
        backend.append(
            _event(
                "claim.created",
                _claim_payload("C-task", "release:T001"),
                target_kind="claim",
                target_id="C-task",
            )
        )
        backend.append(
            _event(
                "claim.created",
                _claim_payload("C-other-task", "release:T002"),
                target_kind="claim",
                target_id="C-other-task",
            )
        )
        before = _event_count(tmp_path)
        with pytest.raises(EventRejected, match=message):
            backend.append(
                _event(
                    "evidence.submitted",
                    {
                        "task_id": "release:T001",
                        "claim_id": claim_id,
                        "submitted_by": "worker",
                        "evidence_id": "EV-invalid-pair",
                        "commands_run": ["pytest -q"],
                        "files_changed": [],
                    },
                    target_kind="task",
                    target_id="release:T001",
                )
            )
        assert backend.get_latest_evidence("release:T001") is None
        assert _event_count(tmp_path) == before
    finally:
        backend.close()


def test_initial_agent_observation_cannot_reference_non_member(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    try:
        _seed(backend)
        payload = _bundle_payload(task_ids=["release:T001"])
        payload["delegated_agents"] = [
            {
                "id": "wrong-task",
                "handle": None,
                "runtime": "codex",
                "task_ids": ["release:T002"],
                "status": "missing",
                "observed_at": _T0.isoformat(),
            }
        ]
        before = _event_count(tmp_path)
        with pytest.raises(EventRejected, match="non-member tasks"):
            backend.append(
                _event(
                    "bundle.created",
                    payload,
                    target_kind="bundle",
                    target_id="B001",
                )
            )
        assert backend.get_bundle("B001") is None
        assert _event_count(tmp_path) == before
    finally:
        backend.close()


def test_bundled_task_delete_rejects_before_log_and_reopens_cleanly(
    tmp_path: Path,
) -> None:
    backend = _backend(tmp_path)
    try:
        _seed(backend)
        _create_bundle(backend, task_ids=["release:T001"])
        before = _event_count(tmp_path)
        with pytest.raises(EventRejected, match="bundle membership row"):
            backend.append(
                _event(
                    "task.deleted",
                    {
                        "task_id": "release:T001",
                        "force": False,
                        "reason": "orphan prune",
                    },
                    target_kind="task",
                    target_id="release:T001",
                )
            )
        assert _event_count(tmp_path) == before
        assert backend.get_task("release:T001") is not None
    finally:
        backend.close()

    reopened = _backend(tmp_path)
    try:
        assert reopened.get_bundle("B001") is not None
        assert reopened.get_task("release:T001") is not None
    finally:
        reopened.close()


def test_concurrent_bundle_membership_race_has_one_logged_winner(
    tmp_path: Path,
) -> None:
    seed = _backend(tmp_path)
    _seed(seed)
    seed.close()
    before = _event_count(tmp_path)

    left = _backend(tmp_path)
    right = _backend(tmp_path)

    def attempt(backend: SqliteBackend, bundle_id: str) -> str:
        try:
            _create_bundle(backend, bundle_id, ["release:T001"])
        except EventRejected:
            return "rejected"
        return "created"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(attempt, left, "B-left"),
                pool.submit(attempt, right, "B-right"),
            ]
            results = sorted(future.result() for future in futures)
        assert results == ["created", "rejected"]
        assert len(left.list_bundles()) == 1
        assert _event_count(tmp_path) == before + 1
    finally:
        left.close()
        right.close()


def test_agent_observation_is_metadata_and_status_transitions_independently(
    tmp_path: Path,
) -> None:
    backend = _backend(tmp_path)
    try:
        _seed(backend)
        creation_event_id = _create_bundle(backend)
        backend.append(
            _event(
                "bundle.agent_observed",
                {
                    "bundle_id": "B001",
                    "creation_event_id": creation_event_id,
                    "observation": {
                        "id": "codex-subtask-1",
                        "handle": None,
                        "runtime": "codex",
                        "task_ids": ["release:T001"],
                        "status": "missing",
                        "observed_at": _T0.isoformat(),
                        "detail": "handle unavailable",
                    },
                },
                target_kind="bundle",
                target_id="B001",
            )
        )
        assert backend.get_bundle("B001").status is BundleStatus.planned  # type: ignore[union-attr]
        backend.append(
            _event(
                "bundle.status_changed",
                {
                    "bundle_id": "B001",
                    "creation_event_id": creation_event_id,
                    "from": "planned",
                    "to": "active",
                    "changed_at": _T0.isoformat(),
                    "reason": "coordinator started",
                },
                target_kind="bundle",
                target_id="B001",
            )
        )
        bundle = backend.get_bundle("B001")
        assert bundle is not None
        assert bundle.status is BundleStatus.active
        assert bundle.delegated_agents[0].status is DelegatedAgentStatus.missing
    finally:
        backend.close()


def test_bundle_snapshot_replays_byte_equivalently(tmp_path: Path) -> None:
    normal_root = tmp_path / "normal"
    replay_root = tmp_path / "replay"
    normal_root.mkdir()
    replay_root.mkdir()
    normal = _backend(normal_root)
    replay = _backend(replay_root)
    try:
        _seed(normal)
        _create_bundle(normal)
        expected = json.dumps(serialize_state(normal), sort_keys=True)
        assert "bundles" in serialize_state(normal)

        replay.replay_from_empty(str(normal_root / "events.jsonl"))
        actual = json.dumps(serialize_state(replay), sort_keys=True)
        assert actual == expected
    finally:
        normal.close()
        replay.close()


def test_replay_keeps_first_divergent_bundle_creation_atomically(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    replay_root = tmp_path / "replay"
    source_root.mkdir()
    replay_root.mkdir()
    source = _backend(source_root)
    try:
        _seed(source)
        winner_creation_id = _create_bundle(source, task_ids=["release:T001"])
    finally:
        source.close()
    next_id = _event_count(source_root) + 1
    losing_creation_id = f"E{next_id:06d}"
    _append_raw_event(
        source_root,
        Event(
            id=losing_creation_id,
            timestamp=_T0 + timedelta(seconds=1),
            actor="other-branch",
            action="bundle.created",
            target_kind="bundle",
            target_id="B001",
            payload_json=_bundle_payload("B001", ["release:T002"]),
        ),
    )
    next_id = _event_count(source_root) + 1
    _append_raw_event(
        source_root,
        Event(
            id=f"E{next_id:06d}",
            timestamp=_T0 + timedelta(seconds=2),
            actor="other-branch",
            action="bundle.agent_observed",
            target_kind="bundle",
            target_id="B001",
            payload_json={
                "bundle_id": "B001",
                "creation_event_id": losing_creation_id,
                "observation": {
                    "id": "losing-agent",
                    "handle": "agent-2",
                    "runtime": "codex",
                    "task_ids": ["release:T002"],
                    "status": "completed",
                    "observed_at": (_T0 + timedelta(seconds=2)).isoformat(),
                },
            },
        ),
    )
    next_id = _event_count(source_root) + 1
    _append_raw_event(
        source_root,
        Event(
            id=f"E{next_id:06d}",
            timestamp=_T0 + timedelta(seconds=3),
            actor="other-branch",
            action="bundle.status_changed",
            target_kind="bundle",
            target_id="B001",
            payload_json={
                "bundle_id": "B001",
                "creation_event_id": losing_creation_id,
                "from": "planned",
                "to": "active",
                "changed_at": (_T0 + timedelta(seconds=3)).isoformat(),
            },
        ),
    )

    replay = _backend(replay_root)
    try:
        replay.replay_from_empty(str(source_root / "events.jsonl"))
        bundle = replay.get_bundle("B001")
        assert bundle is not None
        assert bundle.task_ids == ["release:T001"]
        assert bundle.creation_event_id == winner_creation_id
        assert bundle.delegated_agents == []
        assert bundle.status is BundleStatus.planned
    finally:
        replay.close()


def test_empty_historical_review_policy_replays_without_default_drift(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    replay_root = tmp_path / "replay"
    source_root.mkdir()
    replay_root.mkdir()
    source = _backend(source_root)
    try:
        _seed(source)
        payload = _bundle_payload()
        payload["review_policy"] = {}
        source.append(
            _event(
                "bundle.created",
                payload,
                target_kind="bundle",
                target_id="B001",
            )
        )
        existing_snapshot = serialize_state(source)
        policy = source.get_bundle("B001").review_policy  # type: ignore[union-attr]
        assert policy.max_reviews == 1
        assert policy.required_angles == []
    finally:
        source.close()
    replay = _backend(replay_root)
    try:
        replay.replay_from_empty(str(source_root / "events.jsonl"))
        assert serialize_state(replay) == existing_snapshot
    finally:
        replay.close()


def test_replay_keeps_first_bundle_when_distinct_ids_compete_for_task(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    replay_root = tmp_path / "replay"
    source_root.mkdir()
    replay_root.mkdir()
    source = _backend(source_root)
    try:
        _seed(source)
        _create_bundle(source, "B-first", ["release:T001"])
    finally:
        source.close()
    next_id = _event_count(source_root) + 1
    _append_raw_event(
        source_root,
        Event(
            id=f"E{next_id:06d}",
            timestamp=_T0 + timedelta(seconds=1),
            actor="other-branch",
            action="bundle.created",
            target_kind="bundle",
            target_id="B-second",
            payload_json=_bundle_payload("B-second", ["release:T001"]),
        ),
    )

    replay = _backend(replay_root)
    try:
        replay.replay_from_empty(str(source_root / "events.jsonl"))
        bundles = replay.list_bundles()
        assert [bundle.id for bundle in bundles] == ["B-first"]
        assert bundles[0].task_ids == ["release:T001"]
    finally:
        replay.close()


def test_replay_claim_bundle_exclusion_is_first_event_wins(tmp_path: Path) -> None:
    bundle_first_root = tmp_path / "bundle-first"
    bundle_first_replay_root = tmp_path / "bundle-first-replay"
    bundle_first_root.mkdir()
    bundle_first_replay_root.mkdir()
    source = _backend(bundle_first_root)
    try:
        _seed(source)
        _create_bundle(source, task_ids=["release:T001"])
    finally:
        source.close()
    next_id = _event_count(bundle_first_root) + 1
    _append_raw_event(
        bundle_first_root,
        Event(
            id=f"E{next_id:06d}",
            timestamp=_T0 + timedelta(seconds=1),
            actor="claim-branch",
            action="claim.created",
            target_kind="claim",
            target_id="C-loser",
            payload_json=_claim_payload("C-loser", "release:T001"),
        ),
    )
    next_id = _event_count(bundle_first_root) + 1
    _append_raw_event(
        bundle_first_root,
        Event(
            id=f"E{next_id:06d}",
            timestamp=_T0 + timedelta(seconds=2),
            actor="claim-branch",
            action="evidence.submitted",
            target_kind="task",
            target_id="release:T001",
            payload_json={
                "task_id": "release:T001",
                "claim_id": "C-loser",
                "submitted_by": "worker",
                "evidence_id": "EV-loser",
                "commands_run": ["pytest -q"],
                "files_changed": ["src/example.py"],
            },
        ),
    )
    replay = _backend(bundle_first_replay_root)
    try:
        replay.replay_from_empty(str(bundle_first_root / "events.jsonl"))
        assert replay.get_bundle("B001").status is BundleStatus.superseded  # type: ignore[union-attr]
        assert replay.get_claim("C-loser").status.value == "released"  # type: ignore[union-attr]
        assert replay.get_task("release:T001").status.value == "needs_review"  # type: ignore[union-attr]
        assert replay.get_latest_evidence("release:T001") is not None
    finally:
        replay.close()

    claim_first_root = tmp_path / "claim-first"
    claim_first_replay_root = tmp_path / "claim-first-replay"
    claim_first_root.mkdir()
    claim_first_replay_root.mkdir()
    source = _backend(claim_first_root)
    try:
        _seed(source)
        source.append(
            _event(
                "claim.created",
                _claim_payload("C-winner", "release:T001"),
                target_kind="claim",
                target_id="C-winner",
            )
        )
    finally:
        source.close()
    next_id = _event_count(claim_first_root) + 1
    _append_raw_event(
        claim_first_root,
        Event(
            id=f"E{next_id:06d}",
            timestamp=_T0 + timedelta(seconds=1),
            actor="bundle-branch",
            action="bundle.created",
            target_kind="bundle",
            target_id="B-loser",
            payload_json=_bundle_payload("B-loser", ["release:T001"]),
        ),
    )
    replay = _backend(claim_first_replay_root)
    try:
        replay.replay_from_empty(str(claim_first_root / "events.jsonl"))
        assert replay.get_claim("C-winner") is not None
        assert replay.get_bundle("B-loser") is None
    finally:
        replay.close()


def test_replay_duplicate_claim_id_has_no_losing_projection_side_effects(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    replay_root = tmp_path / "replay"
    source_root.mkdir()
    replay_root.mkdir()
    source = _backend(source_root)
    try:
        _seed(source)
        _create_bundle(source, task_ids=["release:T001"])
        source.append(
            _event(
                "claim.created",
                _claim_payload("C-DUP", "release:T002"),
                target_kind="claim",
                target_id="C-DUP",
            )
        )
    finally:
        source.close()
    next_id = _event_count(source_root) + 1
    _append_raw_event(
        source_root,
        Event(
            id=f"E{next_id:06d}",
            timestamp=_T0 + timedelta(seconds=1),
            actor="other-branch",
            action="claim.created",
            target_kind="claim",
            target_id="C-DUP",
            payload_json=_claim_payload("C-DUP", "release:T001"),
        ),
    )
    next_id = _event_count(source_root) + 1
    _append_raw_event(
        source_root,
        Event(
            id=f"E{next_id:06d}",
            timestamp=_T0 + timedelta(seconds=2),
            actor="other-branch",
            action="evidence.submitted",
            target_kind="task",
            target_id="release:T001",
            payload_json={
                "task_id": "release:T001",
                "claim_id": "C-DUP",
                "submitted_by": "other-branch",
                "evidence_id": "EV-DUP",
                "commands_run": ["pytest -q"],
                "files_changed": ["src/loser.py"],
            },
        ),
    )

    replay = _backend(replay_root)
    try:
        replay.replay_from_empty(str(source_root / "events.jsonl"))
        claim = replay.get_claim("C-DUP")
        assert claim is not None
        assert claim.task_id == "release:T002"
        assert claim.status.value == "active"
        assert replay.get_task("release:T001").status.value == "ready"  # type: ignore[union-attr]
        assert replay.get_bundle("B001").status is BundleStatus.planned  # type: ignore[union-attr]
        assert replay.get_latest_evidence("release:T001") is None
    finally:
        replay.close()


@pytest.mark.parametrize(
    ("action", "payload"),
    [
        (
            "claim.released",
            {
                "claim_id": "C-SAME",
                "released_by": "loser",
                "release_reason": "losing branch",
            },
        ),
        (
            "claim.renewed",
            {
                "claim_id": "C-SAME",
                "renewed_by": "loser",
                "lease_expires_at": (_T0 + timedelta(hours=5)).isoformat(),
                "last_heartbeat_at": (_T0 + timedelta(hours=4)).isoformat(),
            },
        ),
        (
            "claim.stale",
            {
                "claim_id": "C-SAME",
                "task_id": "release:T001",
                "expired_at": (_T0 - timedelta(hours=1)).isoformat(),
                "detected_at": (_T0 + timedelta(hours=2)).isoformat(),
                "reason": "lease_expired",
                "actor": "loser",
            },
        ),
    ],
)
def test_replay_quarantines_same_task_divergent_claim_descendants(
    tmp_path: Path, action: str, payload: dict[str, Any]
) -> None:
    source_root = tmp_path / "source"
    replay_root = tmp_path / "replay"
    source_root.mkdir()
    replay_root.mkdir()
    source = _backend(source_root)
    try:
        _seed(source)
        source.append(
            _event(
                "claim.created",
                _claim_payload("C-SAME", "release:T001"),
                target_kind="claim",
                target_id="C-SAME",
            )
        )
    finally:
        source.close()

    losing_creation = _claim_payload("C-SAME", "release:T001")
    losing_creation["claimed_by"] = "loser"
    next_id = _event_count(source_root) + 1
    _append_raw_event(
        source_root,
        Event(
            id=f"E{next_id:06d}",
            timestamp=_T0 + timedelta(seconds=1),
            actor="loser",
            action="claim.created",
            target_kind="claim",
            target_id="C-SAME",
            payload_json=losing_creation,
        ),
    )
    next_id = _event_count(source_root) + 1
    _append_raw_event(
        source_root,
        Event(
            id=f"E{next_id:06d}",
            timestamp=_T0 + timedelta(seconds=2),
            actor="loser",
            action=action,
            target_kind="claim",
            target_id="C-SAME",
            payload_json=payload,
        ),
    )

    replay = _backend(replay_root)
    try:
        replay.replay_from_empty(str(source_root / "events.jsonl"))
        claim = replay.get_claim("C-SAME")
        assert claim is not None
        assert claim.claimed_by == "worker"
        assert claim.status.value == "active"
        assert claim.lease_expires_at == _T0 + timedelta(hours=1)
        assert claim.last_heartbeat_at == _T0
        assert replay.get_task("release:T001").status.value == "claimed"  # type: ignore[union-attr]
        before = _event_count(replay_root)
        with pytest.raises(EventRejected, match="divergent creation lineages"):
            replay.append(
                _event(
                    action,
                    payload,
                    target_kind="claim",
                    target_id="C-SAME",
                )
            )
        assert _event_count(replay_root) == before
    finally:
        replay.close()


def test_replay_malformed_claim_target_cannot_supersede_bundle(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    replay_root = tmp_path / "replay"
    source_root.mkdir()
    replay_root.mkdir()
    source = _backend(source_root)
    try:
        _seed(source)
        _create_bundle(source, task_ids=["release:T001"])
    finally:
        source.close()
    next_id = _event_count(source_root) + 1
    _append_raw_event(
        source_root,
        Event(
            id=f"E{next_id:06d}",
            timestamp=_T0 + timedelta(seconds=1),
            actor="malformed-branch",
            action="claim.created",
            target_kind="task",
            target_id="release:T001",
            payload_json=_claim_payload("C-wrong-target", "release:T001"),
        ),
    )

    replay = _backend(replay_root)
    try:
        replay.replay_from_empty(str(source_root / "events.jsonl"))
        assert replay.get_claim("C-wrong-target") is None
        assert replay.get_task("release:T001").status.value == "ready"  # type: ignore[union-attr]
        assert replay.get_bundle("B001").status is BundleStatus.planned  # type: ignore[union-attr]
    finally:
        replay.close()


def test_replay_claim_supersession_preserves_monotonic_bundle_chronology(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    replay_root = tmp_path / "replay"
    source_root.mkdir()
    replay_root.mkdir()
    source = _backend(source_root)
    try:
        _seed(source)
        _create_bundle(source, task_ids=["release:T001"])
    finally:
        source.close()
    next_id = _event_count(source_root) + 1
    _append_raw_event(
        source_root,
        Event(
            id=f"E{next_id:06d}",
            # Lexically later than 18:00+00:00, but chronologically one hour
            # earlier. Claim projection must compare/store canonical UTC.
            timestamp=datetime(
                2026, 7, 11, 19, 0, tzinfo=timezone(timedelta(hours=2))
            ),
            actor="claim-branch",
            action="claim.created",
            target_kind="claim",
            target_id="C-older-clock",
            payload_json=_claim_payload("C-older-clock", "release:T001"),
        ),
    )

    replay = _backend(replay_root)
    try:
        replay.replay_from_empty(str(source_root / "events.jsonl"))
        bundle = replay.get_bundle("B001")
        assert bundle is not None
        assert bundle.status is BundleStatus.superseded
        assert bundle.updated_at == _T0
    finally:
        replay.close()


def test_replay_ignores_stale_divergent_status_transition(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    replay_root = tmp_path / "replay"
    source_root.mkdir()
    replay_root.mkdir()
    source = _backend(source_root)
    try:
        _seed(source)
        creation_event_id = _create_bundle(source)
        source.append(
            _event(
                "bundle.status_changed",
                {
                    "bundle_id": "B001",
                    "creation_event_id": creation_event_id,
                    "from": "planned",
                    "to": "active",
                    "changed_at": _T0.isoformat(),
                },
                target_kind="bundle",
                target_id="B001",
            )
        )
    finally:
        source.close()

    for offset, target in enumerate(
        ("replan_required", "implemented_unreviewed"), start=1
    ):
        next_id = _event_count(source_root) + 1
        changed_at = _T0 + timedelta(seconds=offset)
        _append_raw_event(
            source_root,
            Event(
                id=f"E{next_id:06d}",
                timestamp=changed_at,
                    actor="codex-main" if offset == 1 else f"branch-{offset}",
                action="bundle.status_changed",
                target_kind="bundle",
                target_id="B001",
                payload_json={
                    "bundle_id": "B001",
                    "creation_event_id": creation_event_id,
                    "from": "active",
                    "to": target,
                    "changed_at": changed_at.isoformat(),
                },
            ),
        )

    replay = _backend(replay_root)
    try:
        replay.replay_from_empty(str(source_root / "events.jsonl"))
        assert replay.get_bundle("B001").status is BundleStatus.replan_required  # type: ignore[union-attr]
    finally:
        replay.close()


def test_empty_snapshot_omits_bundles_for_legacy_byte_shape(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    try:
        assert "bundles" not in serialize_state(backend)
    finally:
        backend.close()


def test_v10_database_auto_migrates_to_current_without_losing_project(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    try:
        backend.append(
            _event(
                "project.created",
                {
                    "id": "proj",
                    "name": "Before migration",
                    "description": "",
                    "created_at": _T0.isoformat(),
                    "updated_at": _T0.isoformat(),
                },
                target_kind="project",
                target_id="proj",
            )
        )
    finally:
        backend.close()

    with sqlite3.connect(tmp_path / "state.db") as conn:
        conn.execute("DROP TABLE execution_bundle_members")
        conn.execute("DROP TABLE execution_bundles")
        conn.execute("DROP TABLE claim_replay_lineages")
        conn.execute("DROP TABLE bundle_claims")
        conn.execute("PRAGMA user_version = 10")
        conn.commit()

    migrated = _backend(tmp_path)
    try:
        assert migrated.get_schema_version() == SCHEMA_VERSION == 12
        assert migrated.get_project().name == "Before migration"  # type: ignore[union-attr]
        tables = {
            row[0]
            for row in migrated._conn.execute(  # noqa: SLF001
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {
            "execution_bundles",
            "execution_bundle_members",
            "claim_replay_lineages",
            "bundle_claims",
        } <= tables
    finally:
        migrated.close()


def test_v10_upgrade_seeds_claim_lineage_before_log_ahead_catchup(
    tmp_path: Path,
) -> None:
    backend = _backend(tmp_path)
    try:
        _seed(backend)
        backend.append(
            _event(
                "claim.created",
                _claim_payload("C-UPGRADE", "release:T001"),
                target_kind="claim",
                target_id="C-UPGRADE",
            )
        )
    finally:
        backend.close()

    with sqlite3.connect(tmp_path / "state.db") as conn:
        conn.execute("DROP TABLE execution_bundle_members")
        conn.execute("DROP TABLE execution_bundles")
        conn.execute("DROP TABLE claim_replay_lineages")
        conn.execute("DROP TABLE bundle_claims")
        conn.execute("PRAGMA user_version = 10")
        conn.commit()

    losing_creation = _claim_payload("C-UPGRADE", "release:T001")
    losing_creation["claimed_by"] = "loser"
    next_id = _event_count(tmp_path) + 1
    _append_raw_event(
        tmp_path,
        Event(
            id=f"E{next_id:06d}",
            timestamp=_T0 + timedelta(seconds=1),
            actor="loser",
            action="claim.created",
            target_kind="claim",
            target_id="C-UPGRADE",
            payload_json=losing_creation,
        ),
    )
    next_id = _event_count(tmp_path) + 1
    _append_raw_event(
        tmp_path,
        Event(
            id=f"E{next_id:06d}",
            timestamp=_T0 + timedelta(seconds=2),
            actor="loser",
            action="claim.released",
            target_kind="claim",
            target_id="C-UPGRADE",
            payload_json={
                "claim_id": "C-UPGRADE",
                "released_by": "loser",
                "release_reason": "losing lineage",
            },
        ),
    )

    upgraded = _backend(tmp_path)
    try:
        claim = upgraded.get_claim("C-UPGRADE")
        assert claim is not None
        assert claim.claimed_by == "worker"
        assert claim.status.value == "active"
        assert upgraded.get_task("release:T001").status.value == "claimed"  # type: ignore[union-attr]
        with sqlite3.connect(tmp_path / "state.db") as conn:
            collision = conn.execute(
                "SELECT collision_detected FROM claim_replay_lineages "
                "WHERE claim_id = 'C-UPGRADE'"
            ).fetchone()
        assert collision == (1,)
    finally:
        upgraded.close()
