"""Board operations preserve workflow gates, custody, closure and replay."""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from anvil.claims.manager import ClaimManager
from anvil.project_snapshot import read_project_snapshot
from anvil.state.backend import EventRejected, SchemaMismatch, TransactionAborted
from anvil.state.models import EventDraft, TaskStatus
from anvil.state.sqlite import SqliteBackend
from anvil.task_actions import available_task_actions, transition_task
from tests.test_attempt_view import _submit
from tests.test_bundle_state import _bundle_payload
from tests.test_project_snapshot import _event, _feature_payload, _task_payload
from tests.test_sqlite import _make_backend, _setup_claimable_task_and_claim


@pytest.fixture
def board_backend(approved_backend, frozen_clock):
    frozen_clock.advance(seconds=(datetime.now(UTC) - frozen_clock.now()).total_seconds())
    approved_backend.append(_event(
        "feature.created", _feature_payload("F001", "default"), kind="feature", target="F001",
    ))
    for task_id, dependencies in (("T001", []), ("T002", ["T001"])):
        approved_backend.append(_event(
            "task.created", _task_payload(task_id, "F001", "default", dependencies=dependencies),
            kind="task", target=task_id,
        ))
    return approved_backend


def _act(backend, action, actor="operator", task_id="T001", **kwargs):
    task = backend.get_task(task_id)
    return transition_task(
        backend, task_id, action, actor, expected_status=task.status,
        expected_updated_at=task.updated_at.isoformat(), **kwargs,
    )


def test_board_actions_require_claim_owner_and_do_not_skip_review(board_backend, frozen_clock):
    backend = board_backend
    assert available_task_actions(backend, backend.get_task("T001"), "operator") == ["backlog", "close"]
    with pytest.raises(EventRejected):
        _act(backend, "start")
    assert _act(backend, "backlog").status == TaskStatus.drafted
    assert _act(backend, "ready").status == TaskStatus.ready
    ClaimManager(backend, frozen_clock, actor="worker").claim("T001")
    assert available_task_actions(backend, backend.get_task("T001"), "operator") == []
    with pytest.raises(EventRejected):
        _act(backend, "close", resolution="cancelled", reason="Stop")
    with pytest.raises(EventRejected):
        _act(backend, "start")
    assert _act(backend, "start", actor="worker").status == TaskStatus.in_progress
    with pytest.raises(EventRejected, match="reason"):
        _act(backend, "block", actor="worker")
    assert _act(backend, "block", actor="worker", reason="Waiting for review").status == TaskStatus.blocked
    assert _act(backend, "resume", actor="worker").status == TaskStatus.in_progress
    with pytest.raises(ValidationError):
        _act(backend, "done", actor="worker")


def test_action_stale_compare_is_inside_engine_transaction(board_backend):
    backend = board_backend
    original = backend.get_task("T001")
    _act(backend, "backlog")
    _act(backend, "ready")
    before = Path(backend._events_path).read_bytes()
    with pytest.raises(EventRejected, match="stale_task"):
        transition_task(backend, "T001", "close", "operator", expected_status="ready",
                        expected_updated_at=original.updated_at, resolution="cancelled", reason="Stop")
    assert Path(backend._events_path).read_bytes() == before


def test_post_append_missing_task_reports_unknown_outcome(board_backend, monkeypatch):
    task = board_backend.get_task("T001")
    original_get = board_backend.get_task
    monkeypatch.setattr(board_backend, "get_task", lambda _task_id: None)
    with pytest.raises(TransactionAborted, match="may already be durable"):
        transition_task(board_backend, "T001", "backlog", "operator",
                        expected_status=task.status, expected_updated_at=task.updated_at)
    assert original_get("T001").status == TaskStatus.drafted


@pytest.mark.parametrize("resolution", ["cancelled", "wont_do", "obsolete", "cannot_reproduce", "duplicate"])
def test_closure_is_durable_and_never_success(board_backend, frozen_clock, resolution):
    backend = board_backend
    task = _act(backend, "close", resolution=resolution, reason="No longer required",
                duplicate_of="T002" if resolution == "duplicate" else None)
    assert task.status == TaskStatus.closed
    assert task.closure.resolution == resolution
    assert available_task_actions(backend, task, "operator") == []
    assert ClaimManager(backend, frozen_clock, actor="worker").next_ready_excluding_active_files() is None
    snapshot = read_project_snapshot(Path(backend._events_path).parent)
    record = next(task for task in snapshot.payload.tasks if task.local_id == "T001")
    assert record.closure.resolution == resolution
    if resolution == "duplicate":
        assert record.closure.duplicate_of.task_id == "T002"
    backend.replay_from_empty(backend._events_path)
    assert backend.get_task("T001").closure == task.closure
    assert read_project_snapshot(Path(backend._events_path).parent).snapshot_digest == snapshot.snapshot_digest


@pytest.mark.parametrize("target", ["T001", "T999", None])
def test_duplicate_requires_different_existing_task(board_backend, target):
    with pytest.raises((EventRejected, ValidationError)):
        _act(board_backend, "close", resolution="duplicate", reason="Duplicate", duplicate_of=target)
    assert board_backend.get_task("T001").status == TaskStatus.ready


def test_expired_active_claim_still_requires_custody_reconciliation(board_backend, frozen_clock):
    manager = ClaimManager(board_backend, frozen_clock, actor="worker", default_lease_minutes=1)
    manager.claim("T001")
    frozen_clock.advance(minutes=2)
    with pytest.raises(EventRejected):
        _act(board_backend, "start", actor="worker")
    with pytest.raises(EventRejected):
        _act(board_backend, "close", resolution="cancelled", reason="Stop")


def test_schema_22_upgrade_is_additive_and_23_refuses_before_writes(tmp_path, frozen_clock):
    for version in (22, 23):
        directory = tmp_path / str(version)
        directory.mkdir()
        database = directory / "state.db"
        events = directory / "events.jsonl"
        events.touch()
        backend = SqliteBackend(db_path=str(database), events_path=str(events), clock=frozen_clock)
        backend.initialize()
        backend.close()
        with sqlite3.connect(database) as connection:
            connection.execute("ALTER TABLE tasks DROP COLUMN closure")
            connection.execute(f"PRAGMA user_version = {version}")
        before = database.read_bytes(), events.read_bytes()
        if version == 23:
            with pytest.raises(SchemaMismatch):
                backend.initialize()
            assert (database.read_bytes(), events.read_bytes()) == before
        else:
            backend.initialize()
            with sqlite3.connect(database) as connection:
                assert connection.execute("PRAGMA user_version").fetchone()[0] == 25
                assert "closure" in {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
            backend.close()


def test_generic_status_event_cannot_forge_or_reopen_closed(board_backend):
    with pytest.raises(EventRejected):
        board_backend.append(_event("task.status_changed", {
            "task_id": "T001", "from": "ready", "to": "closed",
        }, kind="task", target="T001"))
    _act(board_backend, "close", resolution="cancelled", reason="Stop")
    with pytest.raises(EventRejected):
        board_backend.append(_event("task.status_changed", {
            "task_id": "T001", "from": "closed", "to": "ready",
        }, kind="task", target="T001"))


def test_readiness_gate_and_replanning_preserve_closure(board_backend):
    task_payload = _task_payload("T001", "F001", "default", dependencies=[])
    task_payload["verification"] = {"commands": []}
    board_backend.append(_event("task.created", task_payload, kind="task", target="T001"))
    _act(board_backend, "backlog")
    assert available_task_actions(board_backend, board_backend.get_task("T001"), "operator") == ["close"]
    with pytest.raises(EventRejected):
        _act(board_backend, "ready")
    closed = _act(board_backend, "close", resolution="duplicate", reason="Duplicate", duplicate_of="T002")
    board_backend.append(_event("task.created", task_payload, kind="task", target="T001"))
    assert board_backend.get_task("T001").status == TaskStatus.closed
    assert board_backend.get_task("T001").closure == closed.closure
    with pytest.raises(EventRejected, match="duplicate closure"):
        board_backend.append(_event("task.deleted", {
            "task_id": "T002", "force": True, "reason": "Prune",
        }, kind="task", target="T002"))


def test_replanning_actual_closed_model_preserves_audited_closure(board_backend):
    closed = _act(board_backend, "close", resolution="cancelled", reason="No longer needed")
    payload = closed.model_dump(mode="json")
    payload.update(prd_id=closed.prd_id, conflict_groups=["CG-refreshed"])
    board_backend.append(_event("task.created", payload, kind="task", target=closed.id))
    updated = board_backend.get_task(closed.id)
    assert updated.closure == closed.closure
    assert updated.status == TaskStatus.closed
    assert updated.conflict_groups == ["CG-refreshed"]
    forged = dict(payload, id="T003")
    with pytest.raises(EventRejected, match="closure"):
        board_backend.append(_event("task.created", forged, kind="task", target="T003"))
    payload["closure"]["reason"] = "Changed without a closure event"
    with pytest.raises(EventRejected, match="closure"):
        board_backend.append(_event("task.created", payload, kind="task", target=closed.id))


def test_expansion_cannot_create_a_closed_subtask(board_backend):
    closed = _act(board_backend, "close", resolution="cancelled", reason="Not required")
    subtask = closed.model_dump(mode="json")
    subtask.update(id="T002.1", prd_id=closed.prd_id)
    with pytest.raises(EventRejected, match="closure"):
        board_backend.append(_event("task.expanded", {
            "parent_task_id": "T002", "subtasks": [subtask],
        }, kind="task", target="T002"))
    assert board_backend.get_task("T002.1") is None


@pytest.mark.parametrize("other_action", ["delete_duplicate", "remote_sync", "bundle", "claim"])
@pytest.mark.parametrize("closure_first", [False, True])
def test_git_union_closure_conflicts_preserve_valid_state(tmp_path, other_action, closure_first):
    """Both union byte orders preserve the same deterministic replay winner."""
    from tests.test_bundle_state import _seed
    from tests.test_git_events import _draft, _replay_merged
    from tests.test_git_events import _make_backend as git_backend

    source = tmp_path / "source"
    source.mkdir()
    backend = git_backend(source)
    _seed(backend)
    task_id, other_task_id = "release:T001", "release:T002"
    baseline = backend.get_task(task_id)
    baseline_time = baseline.updated_at
    backend.close()
    prefix = (source / "events.jsonl").read_text().splitlines()
    suffixes = []
    for branch_name in ("closure", "other"):
        branch = tmp_path / branch_name
        branch.mkdir()
        (branch / "events.jsonl").write_text("\n".join(prefix) + "\n")
        backend = git_backend(branch)
        timestamp = baseline_time + timedelta(seconds=(1 if (branch_name == "closure") == closure_first else 2))
        if branch_name == "closure":
            backend.append(_draft("task.closed", {
                "task_id": task_id, "expected_status": baseline.status.value,
                "expected_updated_at": baseline.updated_at.isoformat(),
                "closure": {"resolution": "duplicate" if other_action == "delete_duplicate" else "cancelled",
                            "reason": "Scope removed", "closed_by": "operator", "closed_at": timestamp.isoformat(),
                            "duplicate_of": other_task_id if other_action == "delete_duplicate" else None},
            }, target_kind="task", target_id=task_id, ts=timestamp, actor="operator"))
        elif other_action == "delete_duplicate":
            backend.append(_draft("task.deleted", {"task_id": other_task_id, "force": True},
                                  target_kind="task", target_id=other_task_id, ts=timestamp))
        elif other_action == "remote_sync":
            backend.append(_draft("task.synced_from_remote", {
                "task_id": task_id, "title": "Remote update", "description": "Remote description", "status": "ready",
            }, target_kind="task", target_id=task_id, ts=timestamp))
        elif other_action == "bundle":
            payload = _bundle_payload()
            payload.update(created_at=timestamp.isoformat(), updated_at=timestamp.isoformat())
            backend.append(_draft("bundle.created", payload, target_kind="bundle", target_id="B001",
                                  ts=timestamp, actor="coordinator"))
        else:
            # The manager uses its injected event-time clock; no Git worktree or
            # operator registry is needed for this isolated engine claim.
            from anvil.clock import FrozenClock
            ClaimManager(backend, FrozenClock(timestamp), actor="worker").claim(task_id)
        backend.close()
        suffixes.append((branch / "events.jsonl").read_text().splitlines()[len(prefix):])
    states = []
    for index, (first, second) in enumerate((suffixes, list(reversed(suffixes)))):
        merged = _replay_merged(tmp_path, f"merged-{index}", prefix + first + second)
        task = merged.get_task(task_id)
        snapshot = read_project_snapshot(Path(merged._events_path).parent)
        states.append(snapshot.snapshot_digest)
        if closure_first:
            assert task.status == TaskStatus.closed
            assert task.closure is not None
            if other_action == "delete_duplicate":
                assert merged.get_task(other_task_id) is not None
            if other_action == "bundle":
                assert merged.get_bundle("B001") is None
            if other_action == "claim":
                assert not merged.list_active_claims()
        else:
            assert task.status != TaskStatus.closed
            assert task.closure is None
        merged.close()
    assert states[0] == states[1]


def test_live_bundle_retains_member_custody(board_backend):
    payload = _bundle_payload(task_ids=["T001", "T002"])
    payload.update(prd_id="default", created_at=datetime(2026, 8, 8, 12, tzinfo=UTC).isoformat(),
                   updated_at=datetime(2026, 8, 8, 12, tzinfo=UTC).isoformat())
    board_backend.append(_event("bundle.created", payload, kind="bundle", target="B001"))
    assert available_task_actions(board_backend, board_backend.get_task("T001"), "operator") == []
    with pytest.raises(EventRejected):
        _act(board_backend, "close", resolution="cancelled", reason="Stop")


def test_preview_approval_rechecks_task_timestamp_with_unchanged_evidence(tmp_path):
    backend = _make_backend(tmp_path)
    try:
        _setup_claimable_task_and_claim(backend)
        _submit(backend)
        task = backend.get_task("T001")
        evidence = backend.get_latest_evidence("T001")
        backend.append(EventDraft(
            timestamp=task.updated_at + timedelta(seconds=1), actor="other",
            action="task.status_changed", target_kind="task", target_id="T001",
            payload_json={"task_id": "T001", "from": "needs_review", "to": "needs_review"},
        ))
        before = Path(backend._events_path).read_bytes()
        with pytest.raises(EventRejected, match="stale_review"):
            backend.append(EventDraft(
                timestamp=task.updated_at + timedelta(seconds=2), actor="human",
                action="task.applied", target_kind="task", target_id="T001",
                payload_json={"task_id": "T001", "reviewer": "human", "decision": "accepted",
                              "schema_version": 1, "review_attempt_id": evidence.id,
                              "expected_evidence_id": evidence.id,
                              "expected_task_updated_at": task.updated_at.isoformat()},
            ))
        assert Path(backend._events_path).read_bytes() == before
    finally:
        backend.close()


@pytest.mark.parametrize("decision", ["accepted", "rejected"])
def test_git_union_preview_decision_cannot_replay_after_task_change(tmp_path, monkeypatch, decision):
    from anvil.roots import registry
    from tests.test_git_events import _draft, _replay_merged
    from tests.test_git_events import _make_backend as git_backend
    from tests.test_sqlite import _make_applied_payload, _make_rejected_applied_payload

    original_registry = registry.RootSetRegistry
    monkeypatch.setattr(registry, "RootSetRegistry", lambda: original_registry(tmp_path / "owner"))
    seed = tmp_path / "seed"
    seed.mkdir()
    backend = git_backend(seed)
    _setup_claimable_task_and_claim(backend)
    _submit(backend)
    task = backend.get_task("T001")
    backend.close()
    prefix = (seed / "events.jsonl").read_text().splitlines()
    suffixes = []
    for index, branch_name in enumerate(("score", "decision")):
        branch = tmp_path / branch_name
        branch.mkdir()
        (branch / "events.jsonl").write_text("\n".join(prefix) + "\n")
        backend = git_backend(branch)
        timestamp = task.updated_at + timedelta(seconds=index + 1)
        if branch_name == "score":
            backend.append(_draft("task.scored", {"task_id": task.id, "scores": {"blast_radius": 4}},
                                  target_kind="task", target_id=task.id, ts=timestamp))
        else:
            payload = (_make_applied_payload() if decision == "accepted"
                       else _make_rejected_applied_payload(backend))
            payload.update(expected_evidence_id="EV001", expected_task_updated_at=task.updated_at.isoformat())
            backend.append(_draft("task.applied", payload, target_kind="task", target_id=task.id,
                                  ts=timestamp, actor="alice"))
        backend.close()
        suffixes.append((branch / "events.jsonl").read_text().splitlines()[len(prefix):])
    for index, (first, second) in enumerate((suffixes, list(reversed(suffixes)))):
        merged = _replay_merged(tmp_path, f"merged-{index}", prefix + first + second)
        assert merged.get_task(task.id).status == TaskStatus.needs_review
        assert merged.get_task(task.id).scores.blast_radius == 4
        assert merged.list_reviews() == []
        merged.close()


@pytest.mark.parametrize("decision", ["accepted", "rejected"])
@pytest.mark.parametrize("closure_first", [False, True])
def test_git_union_legacy_review_and_closure_keep_first_disposition(
    tmp_path, monkeypatch, decision, closure_first,
):
    from anvil.roots import registry
    from tests.test_git_events import _draft, _replay_merged
    from tests.test_git_events import _make_backend as git_backend
    from tests.test_sqlite import _make_applied_payload, _make_rejected_applied_payload

    original_registry = registry.RootSetRegistry
    monkeypatch.setattr(registry, "RootSetRegistry", lambda: original_registry(tmp_path / "owner"))
    seed = tmp_path / "seed"
    seed.mkdir()
    backend = git_backend(seed)
    _setup_claimable_task_and_claim(backend)
    _submit(backend)
    task = backend.get_task("T001")
    backend.close()
    prefix = (seed / "events.jsonl").read_text().splitlines()
    suffixes = []
    for branch_name in ("closure", "review"):
        branch = tmp_path / branch_name
        branch.mkdir()
        (branch / "events.jsonl").write_text("\n".join(prefix) + "\n")
        backend = git_backend(branch)
        timestamp = task.updated_at + timedelta(
            seconds=1 if (branch_name == "closure") == closure_first else 2,
        )
        if branch_name == "closure":
            backend.append(_draft("task.closed", {
                "task_id": task.id, "expected_status": task.status.value,
                "expected_updated_at": task.updated_at.isoformat(),
                "closure": {"resolution": "cancelled", "reason": "Scope removed",
                            "closed_by": "operator", "closed_at": timestamp.isoformat()},
            }, target_kind="task", target_id=task.id, ts=timestamp, actor="operator"))
        else:
            payload = (_make_applied_payload() if decision == "accepted"
                       else _make_rejected_applied_payload(backend))
            assert "expected_evidence_id" not in payload
            backend.append(_draft("task.applied", payload, target_kind="task", target_id=task.id,
                                  ts=timestamp, actor="alice"))
        backend.close()
        suffixes.append((branch / "events.jsonl").read_text().splitlines()[len(prefix):])
    snapshots = []
    for index, (first, second) in enumerate((suffixes, list(reversed(suffixes)))):
        merged = _replay_merged(tmp_path, f"merged-{index}", prefix + first + second)
        result = merged.get_task(task.id)
        expected_status = (TaskStatus.closed if closure_first else
                           TaskStatus.done if decision == "accepted" else TaskStatus.drafted)
        assert result.status == expected_status
        assert (result.closure is not None) == closure_first
        assert len(merged.list_reviews()) == (0 if closure_first else 1)
        snapshots.append(read_project_snapshot(Path(merged._events_path).parent).snapshot_digest)
        merged.close()
    assert snapshots[0] == snapshots[1]
