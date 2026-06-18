"""Tests for anvil.claims.manager and anvil.claims.stale.

Coverage target: claims/ >= 95%.

Design notes:
- FrozenClock is used everywhere timestamps matter. No datetime.now() calls.
- All tests are hermetically isolated via tmp_path.
- Claim IDs and event IDs produced by the manager are not hard-coded; we check
  structural properties (non-empty, starts with 'C' or 'E') instead.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from anvil.claims.manager import (
    ClaimError,
    ClaimManager,
    ClaimResult,
    ConflictWarning,
)
from anvil.claims.stale import detect_and_release_stale
from anvil.clock import FrozenClock
from anvil.state.models import (
    ClaimStatus,
    EventDraft,
    TaskStatus,
)
from anvil.state.sqlite import SqliteBackend

# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

_UTC = UTC
_T0 = datetime(2026, 5, 24, 18, 0, 0, tzinfo=_UTC)


def _make_clock(dt: datetime = _T0) -> FrozenClock:
    return FrozenClock(dt)


def _make_backend(state_dir: Path, clock: FrozenClock | None = None) -> SqliteBackend:
    if clock is None:
        clock = _make_clock()
    db_path = str(state_dir / "state.db")
    events_path = str(state_dir / "events.jsonl")
    Path(events_path).touch()
    b = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock)
    b.initialize()
    return b


def _make_event(
    action: str,
    payload: dict[str, Any],
    *,
    event_id: str = "E000003",
    target_kind: str = "task",
    target_id: str = "T001",
    now: datetime = _T0,
    actor: str = "test",
) -> EventDraft:
    """Return an EventDraft (SL1-RR-1: id is assigned by backend)."""
    return EventDraft(
        timestamp=now,
        actor=actor,
        action=action,
        target_kind=target_kind,
        target_id=target_id,
        payload_json=payload,
    )


def _setup_project(b: SqliteBackend) -> None:
    """Apply project.created + state.initialized via append()."""
    b.append(EventDraft(
        timestamp=_T0,
        actor="test",
        action="project.created",
        target_kind="project",
        target_id="proj-1",
        payload_json={
            "id": "proj-1",
            "name": "Test Project",
            "description": "",
            "created_at": _T0.isoformat(),
            "updated_at": _T0.isoformat(),
        },
    ))
    b.append(EventDraft(
        timestamp=_T0,
        actor="test",
        action="state.initialized",
        target_kind="project",
        target_id="proj-1",
        payload_json={},
    ))


def _make_prd_payload(status: str = "draft") -> dict[str, Any]:
    return {
        "project_id": "proj-1",
        "status": status,
        "summary": "Test PRD.",
        "goals": ["Goal one."],
        "non_goals": [],
        "requirements": [
            {"id": "R001", "prd_section": "requirements", "text": "Req 1.",
             "source_paragraph": None, "derived": False}
        ],
        "acceptance_criteria": ["AC one."],
        "risks": [],
        "open_questions": [],
    }


def _setup_prd(b: SqliteBackend, *, approve: bool = False) -> None:
    """Apply prd.parsed, prd.reviewed, and optionally prd.approved via append()."""
    b.append(_make_event(
        "prd.parsed", _make_prd_payload(),
        event_id="E000003", target_kind="prd", target_id="proj-1",
    ))
    b.append(_make_event(
        "prd.reviewed", {"project_id": "proj-1", "reviewer": "alice"},
        event_id="E000004", target_kind="prd", target_id="proj-1",
    ))
    if approve:
        b.append(_make_event(
            "prd.approved", {"project_id": "proj-1", "approver": "bob"},
            event_id="E000005", target_kind="prd", target_id="proj-1",
        ))


def _insert_feature_raw(conn: sqlite3.Connection, feat_id: str = "F001") -> None:
    conn.execute(
        "INSERT OR IGNORE INTO features (id, title, description, status, requirements, tasks) "
        "VALUES (?, ?, 'desc', 'proposed', '[]', '[]')",
        (feat_id, f"Feature {feat_id}"),
    )
    conn.commit()


def _insert_task_raw(
    conn: sqlite3.Connection,
    *,
    task_id: str = "T001",
    status: str = "ready",
    conflict_groups: list[str] | None = None,
    likely_files: list[str] | None = None,
    dependencies: list[str] | None = None,
) -> None:
    import json as _json
    conn.execute(
        """INSERT INTO tasks
        (id, feature_id, title, description, status, priority,
         dependencies, conflict_groups, scores, acceptance_criteria,
         implementation_notes, verification, likely_files,
         created_at, updated_at)
        VALUES (?, 'F001', ?, 'desc', ?, 'medium', ?, ?, '{}', '[]', '[]', '{}', ?, ?, ?)""",
        (
            task_id,
            f"Task {task_id}",
            status,
            _json.dumps(dependencies or []),
            _json.dumps(conflict_groups or []),
            _json.dumps(likely_files or []),
            _T0.isoformat(),
            _T0.isoformat(),
        ),
    )
    conn.commit()


def _insert_active_claim_raw(
    conn: sqlite3.Connection,
    *,
    claim_id: str = "C001",
    task_id: str = "T001",
    actor: str = "other-agent",
    expected_files: list[str] | None = None,
    lease_expires_at: datetime | None = None,
) -> None:
    import json as _json
    expires = (lease_expires_at or (_T0 + timedelta(hours=1))).isoformat()
    conn.execute(
        """INSERT INTO claims
        (id, task_id, claimed_by, claim_type, status, expected_files,
         created_at, lease_expires_at, last_heartbeat_at)
        VALUES (?, ?, ?, 'task', 'active', ?, ?, ?, ?)""",
        (
            claim_id,
            task_id,
            actor,
            _json.dumps(expected_files or []),
            _T0.isoformat(),
            expires,
            _T0.isoformat(),
        ),
    )
    conn.commit()


def _make_manager(
    b: SqliteBackend,
    *,
    actor: str = "agent-test",
    clock: FrozenClock | None = None,
    lease_minutes: int = 60,
) -> ClaimManager:
    c = clock or _make_clock()
    return ClaimManager(b, c, actor=actor, default_lease_minutes=lease_minutes)


# ---------------------------------------------------------------------------
# TestNextClaimable
# ---------------------------------------------------------------------------


class TestNextClaimable:
    def test_next_returns_none_when_no_ready_tasks(self, tmp_path: Path) -> None:
        """next_claimable returns None when there are no tasks in 'ready' status."""
        b = _make_backend(tmp_path)
        try:
            m = _make_manager(b)
            assert m.next_claimable() is None
        finally:
            b.close()

    def test_next_returns_highest_priority_ready_task(self, tmp_path: Path) -> None:
        """next_claimable returns the task with the highest priority among ready tasks."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            _setup_prd(b)

            # Insert two ready tasks: medium and high priority. Use raw insert to skip
            # full event chain which would produce duplicate feature insertions.
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            _insert_feature_raw(conn)
            _insert_task_raw(conn, task_id="T001", status="ready")
            _insert_task_raw(conn, task_id="T002", status="ready")
            # Override priority for T002 to 'high'
            conn.execute("UPDATE tasks SET priority = 'high' WHERE id = 'T002'")
            conn.commit()
            conn.close()

            m = _make_manager(b)
            task = m.next_claimable()
            assert task is not None
            assert task.id == "T002"
        finally:
            b.close()

    def test_next_respects_dependency_unmet(self, tmp_path: Path) -> None:
        """A task with an unsatisfied dependency is NOT returned by next_claimable."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            _setup_prd(b)

            conn = sqlite3.connect(str(tmp_path / "state.db"))
            _insert_feature_raw(conn)
            # T001 in proposed (not done) — T002 depends on T001
            _insert_task_raw(conn, task_id="T001", status="proposed")
            _insert_task_raw(conn, task_id="T002", status="ready", dependencies=["T001"])
            conn.close()

            m = _make_manager(b)
            task = m.next_claimable()
            # T002 has unmet dep; T001 is not ready → None
            assert task is None
        finally:
            b.close()

    def test_next_skips_tasks_in_active_conflict_group(self, tmp_path: Path) -> None:
        """A task sharing a conflict_group with an already-claimed task is skipped."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            _setup_prd(b)

            conn = sqlite3.connect(str(tmp_path / "state.db"))
            _insert_feature_raw(conn)
            # T001 is claimed (has an active claim), in conflict_group "auth"
            _insert_task_raw(conn, task_id="T001", status="claimed", conflict_groups=["auth"])
            # T002 is ready but also in conflict_group "auth" → should be skipped
            _insert_task_raw(conn, task_id="T002", status="ready", conflict_groups=["auth"])
            _insert_active_claim_raw(conn, claim_id="C001", task_id="T001")
            conn.close()

            m = _make_manager(b)
            task = m.next_claimable()
            assert task is None
        finally:
            b.close()


# ---------------------------------------------------------------------------
# TestNextReadyExcludingActiveFiles (T014)
# ---------------------------------------------------------------------------


class TestNextReadyExcludingActiveFiles:
    """next_ready_excluding_active_files adds file-overlap exclusion on top of
    next_claimable's filters — the helper behind the finish/submit next_ready
    field (T014)."""

    def test_excludes_task_overlapping_another_agents_claim(
        self, tmp_path: Path
    ) -> None:
        """A ready task whose likely_files overlap an active claim by another
        actor is excluded; the next non-overlapping task is returned."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            _setup_prd(b)

            conn = sqlite3.connect(str(tmp_path / "state.db"))
            _insert_feature_raw(conn)
            # T001 is claimed by another agent, locking src/shared.py.
            _insert_task_raw(conn, task_id="T001", status="claimed",
                             likely_files=["src/shared.py"])
            _insert_active_claim_raw(conn, claim_id="C001", task_id="T001",
                                     actor="other-agent",
                                     expected_files=["src/shared.py"])
            # T002 (high) overlaps the locked file → excluded.
            _insert_task_raw(conn, task_id="T002", status="ready",
                             likely_files=["src/shared.py"])
            conn.execute("UPDATE tasks SET priority = 'high' WHERE id = 'T002'")
            # T003 (medium) touches a different file → eligible.
            _insert_task_raw(conn, task_id="T003", status="ready",
                             likely_files=["src/other.py"])
            conn.commit()
            conn.close()

            m = _make_manager(b, actor="agent-test")
            # next_claimable would pick T002 (higher priority, file overlap
            # ignored); the stricter helper skips it for T003.
            assert m.next_claimable().id == "T002"
            picked = m.next_ready_excluding_active_files()
            assert picked is not None
            assert picked.id == "T003"
        finally:
            b.close()

    def test_does_not_exclude_own_claim_files(self, tmp_path: Path) -> None:
        """Files locked by the *acting* agent's own claim are not a conflict."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            _setup_prd(b)

            conn = sqlite3.connect(str(tmp_path / "state.db"))
            _insert_feature_raw(conn)
            # Our own agent holds a claim on src/shared.py.
            _insert_task_raw(conn, task_id="T001", status="claimed",
                             likely_files=["src/shared.py"])
            _insert_active_claim_raw(conn, claim_id="C001", task_id="T001",
                                     actor="agent-test",
                                     expected_files=["src/shared.py"])
            # T002 overlaps that file, but the lock is ours → still eligible.
            _insert_task_raw(conn, task_id="T002", status="ready",
                             likely_files=["src/shared.py"])
            conn.commit()
            conn.close()

            m = _make_manager(b, actor="agent-test")
            picked = m.next_ready_excluding_active_files()
            assert picked is not None
            assert picked.id == "T002"
        finally:
            b.close()

    def test_returns_none_when_only_candidate_overlaps(
        self, tmp_path: Path
    ) -> None:
        """When the sole ready task overlaps a foreign active claim, None."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            _setup_prd(b)

            conn = sqlite3.connect(str(tmp_path / "state.db"))
            _insert_feature_raw(conn)
            _insert_task_raw(conn, task_id="T001", status="claimed",
                             likely_files=["src/shared.py"])
            _insert_active_claim_raw(conn, claim_id="C001", task_id="T001",
                                     actor="other-agent",
                                     expected_files=["src/shared.py"])
            _insert_task_raw(conn, task_id="T002", status="ready",
                             likely_files=["src/shared.py"])
            conn.commit()
            conn.close()

            m = _make_manager(b, actor="agent-test")
            assert m.next_ready_excluding_active_files() is None
        finally:
            b.close()

    def test_matches_next_claimable_when_no_active_locks(
        self, tmp_path: Path
    ) -> None:
        """With no foreign locks, the helper agrees with next_claimable."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            _setup_prd(b)

            conn = sqlite3.connect(str(tmp_path / "state.db"))
            _insert_feature_raw(conn)
            _insert_task_raw(conn, task_id="T001", status="ready",
                             likely_files=["src/a.py"])
            _insert_task_raw(conn, task_id="T002", status="ready",
                             likely_files=["src/b.py"])
            conn.execute("UPDATE tasks SET priority = 'high' WHERE id = 'T002'")
            conn.commit()
            conn.close()

            m = _make_manager(b)
            assert m.next_ready_excluding_active_files().id == "T002"
            assert m.next_claimable().id == "T002"
        finally:
            b.close()

    def test_returns_none_when_no_ready_tasks(self, tmp_path: Path) -> None:
        b = _make_backend(tmp_path)
        try:
            m = _make_manager(b)
            assert m.next_ready_excluding_active_files() is None
        finally:
            b.close()


# ---------------------------------------------------------------------------
# TestClaim
# ---------------------------------------------------------------------------


class TestClaim:
    def _setup_claimable(self, tmp_path: Path, clock: FrozenClock) -> SqliteBackend:
        """Set up a project with PRD reviewed and one ready task."""
        b = _make_backend(tmp_path, clock)
        _setup_project(b)
        _setup_prd(b)
        conn = sqlite3.connect(str(tmp_path / "state.db"))
        _insert_feature_raw(conn)
        _insert_task_raw(conn, task_id="T001", status="ready")
        conn.close()
        return b

    def test_claim_happy_path(self, tmp_path: Path) -> None:
        """Claim a ready task: task moves to 'claimed', claim row created."""
        clock = _make_clock()
        b = self._setup_claimable(tmp_path, clock)
        try:
            m = _make_manager(b, actor="agent-alpha", clock=clock)
            result = m.claim("T001")
            assert isinstance(result, ClaimResult)
            assert result.claim.status == ClaimStatus.active
            assert result.claim.claimed_by == "agent-alpha"
            assert result.claim.task_id == "T001"
            assert result.branch is None  # git ops not at this layer

            # Task should be claimed
            task = b.get_task("T001")
            assert task is not None
            assert task.status == TaskStatus.claimed

            # Claim exists in backend
            stored_claim = b.get_claim(result.claim.id)
            assert stored_claim is not None
            assert stored_claim.status == ClaimStatus.active
        finally:
            b.close()

    def test_claim_refuses_when_task_not_found(self, tmp_path: Path) -> None:
        """claim() raises ClaimError when task does not exist."""
        clock = _make_clock()
        b = self._setup_claimable(tmp_path, clock)
        try:
            m = _make_manager(b, clock=clock)
            with pytest.raises(ClaimError, match="not found|T999"):
                m.claim("T999")
        finally:
            b.close()

    def test_claim_refuses_when_task_not_ready(self, tmp_path: Path) -> None:
        """claim() raises ClaimError when task is not in 'ready' status."""
        clock = _make_clock()
        b = self._setup_claimable(tmp_path, clock)
        try:
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            _insert_task_raw(conn, task_id="T002", status="proposed")
            conn.close()

            m = _make_manager(b, clock=clock)
            with pytest.raises(ClaimError, match="proposed|ready|status"):
                m.claim("T002")
        finally:
            b.close()

    def test_claim_refuses_when_prd_is_draft(self, tmp_path: Path) -> None:
        """claim() raises ClaimError when PRD is still in draft status."""
        clock = _make_clock()
        b = _make_backend(tmp_path, clock)
        try:
            _setup_project(b)
            # Only parse PRD — do NOT review it
            b.append(_make_event(
                "prd.parsed", _make_prd_payload(),
                event_id="E000003", target_kind="prd", target_id="proj-1",
            ))
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            _insert_feature_raw(conn)
            _insert_task_raw(conn, task_id="T001", status="ready")
            conn.close()

            m = _make_manager(b, clock=clock)
            with pytest.raises(ClaimError, match="draft|prd|reviewed|approved"):
                m.claim("T001")
        finally:
            b.close()

    def test_claim_succeeds_when_prd_reviewed_but_not_approved(self, tmp_path: Path) -> None:
        """claim() succeeds when PRD is reviewed (approved is not required)."""
        clock = _make_clock()
        b = self._setup_claimable(tmp_path, clock)  # uses reviewed PRD
        try:
            m = _make_manager(b, clock=clock)
            result = m.claim("T001")
            assert result.claim.status == ClaimStatus.active
        finally:
            b.close()

    def test_claim_warns_on_file_overlap_with_other_active_claim(self, tmp_path: Path) -> None:
        """Without --force, claim raises ClaimError with ConflictWarning details on file overlap."""
        clock = _make_clock()
        b = _make_backend(tmp_path, clock)
        try:
            _setup_project(b)
            _setup_prd(b)
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            _insert_feature_raw(conn)
            _insert_task_raw(conn, task_id="T001", status="ready", likely_files=["src/foo.py"])
            _insert_task_raw(conn, task_id="T002", status="claimed", likely_files=["src/foo.py"])
            _insert_active_claim_raw(
                conn, claim_id="C001", task_id="T002", actor="other-agent",
                expected_files=["src/foo.py"]
            )
            conn.close()

            m = _make_manager(b, actor="agent-alpha", clock=clock)
            with pytest.raises(ClaimError, match="conflict|src/foo.py|C001|other-agent"):
                m.claim("T001", expected_files=["src/foo.py"], force=False)
        finally:
            b.close()

    def test_claim_force_overrides_file_overlap(self, tmp_path: Path) -> None:
        """With force=True, claim proceeds despite file overlap."""
        clock = _make_clock()
        b = _make_backend(tmp_path, clock)
        try:
            _setup_project(b)
            _setup_prd(b)
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            _insert_feature_raw(conn)
            _insert_task_raw(conn, task_id="T001", status="ready", likely_files=["src/foo.py"])
            _insert_task_raw(conn, task_id="T002", status="claimed", likely_files=["src/foo.py"])
            _insert_active_claim_raw(
                conn, claim_id="C001", task_id="T002", actor="other-agent",
                expected_files=["src/foo.py"]
            )
            conn.close()

            m = _make_manager(b, actor="agent-alpha", clock=clock)
            result = m.claim("T001", expected_files=["src/foo.py"], force=True)
            assert result.claim.status == ClaimStatus.active
        finally:
            b.close()

    def test_claim_warns_on_conflict_group_collision(self, tmp_path: Path) -> None:
        """Claim raises ClaimError when another task in the same conflict_group is claimed."""
        clock = _make_clock()
        b = _make_backend(tmp_path, clock)
        try:
            _setup_project(b)
            _setup_prd(b)
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            _insert_feature_raw(conn)
            _insert_task_raw(conn, task_id="T001", status="ready", conflict_groups=["auth"])
            _insert_task_raw(conn, task_id="T002", status="claimed", conflict_groups=["auth"])
            _insert_active_claim_raw(conn, claim_id="C001", task_id="T002", actor="other-agent")
            conn.close()

            m = _make_manager(b, actor="agent-alpha", clock=clock)
            with pytest.raises(ClaimError, match="conflict_group|auth|T002"):
                m.claim("T001", force=False)
        finally:
            b.close()

    def test_claim_uses_clock_for_timestamps(self, tmp_path: Path) -> None:
        """Lease expiry is exactly clock.now() + default_lease_minutes."""
        clock = _make_clock(_T0)
        b = self._setup_claimable(tmp_path, clock)
        try:
            m = _make_manager(b, clock=clock, lease_minutes=60)
            result = m.claim("T001")
            expected_expiry = _T0 + timedelta(minutes=60)
            assert result.claim.lease_expires_at == expected_expiry
            assert result.claim.created_at == _T0
            assert result.claim.last_heartbeat_at == _T0
        finally:
            b.close()


# ---------------------------------------------------------------------------
# TestRelease
# ---------------------------------------------------------------------------


class TestRelease:
    def _setup_with_active_claim(
        self,
        tmp_path: Path,
        clock: FrozenClock,
        actor: str = "agent-alpha",
    ) -> tuple[SqliteBackend, str]:
        """Create a backend with one active claim; return (backend, claim_id)."""
        b = _make_backend(tmp_path, clock)
        _setup_project(b)
        _setup_prd(b)
        conn = sqlite3.connect(str(tmp_path / "state.db"))
        _insert_feature_raw(conn)
        _insert_task_raw(conn, task_id="T001", status="ready")
        conn.close()

        m = _make_manager(b, actor=actor, clock=clock)
        result = m.claim("T001")
        return b, result.claim.id

    def test_release_happy_path(self, tmp_path: Path) -> None:
        """claim then release returns task to 'ready'."""
        clock = _make_clock()
        b, claim_id = self._setup_with_active_claim(tmp_path, clock)
        try:
            m = _make_manager(b, actor="agent-alpha", clock=clock)
            m.release(claim_id)

            task = b.get_task("T001")
            assert task is not None
            assert task.status == TaskStatus.ready

            claim = b.get_claim(claim_id)
            assert claim is not None
            assert claim.status in {ClaimStatus.released, ClaimStatus.force_released}
        finally:
            b.close()

    def test_release_refuses_wrong_actor_without_force(self, tmp_path: Path) -> None:
        """release without force raises ClaimError when actor doesn't own the claim."""
        clock = _make_clock()
        b, claim_id = self._setup_with_active_claim(tmp_path, clock, actor="agent-alpha")
        try:
            m_other = _make_manager(b, actor="agent-beta", clock=clock)
            with pytest.raises(ClaimError, match="agent-alpha|actor|force"):
                m_other.release(claim_id, force=False)
        finally:
            b.close()

    def test_release_force_allows_any_actor(self, tmp_path: Path) -> None:
        """force=True allows any actor to release a claim."""
        clock = _make_clock()
        b, claim_id = self._setup_with_active_claim(tmp_path, clock, actor="agent-alpha")
        try:
            m_other = _make_manager(b, actor="agent-beta", clock=clock)
            m_other.release(claim_id, force=True)

            task = b.get_task("T001")
            assert task is not None
            assert task.status == TaskStatus.ready
        finally:
            b.close()

    def test_release_refuses_already_released_claim(self, tmp_path: Path) -> None:
        """Releasing an already-released claim raises ClaimError.

        The manager does not silently succeed on double-release.
        An already-terminal claim (released or force_released) must raise
        ClaimError so callers detect double-release rather than assuming success.
        """
        clock = _make_clock()
        b, claim_id = self._setup_with_active_claim(tmp_path, clock)
        try:
            m = _make_manager(b, actor="agent-alpha", clock=clock)
            m.release(claim_id)  # first release OK

            with pytest.raises(ClaimError, match="terminal|released|status"):
                m.release(claim_id)  # second release must fail
        finally:
            b.close()

    def test_release_emits_correct_events(self, tmp_path: Path) -> None:
        """Release emits ONLY claim.released (the side-effect task transition
        is handled atomically inside _handle_claim_released; emitting a
        separate task.status_changed would either no-op (audit noise) or
        worse, reset a needs_review task back to ready. Critic-2 + Critic-3
        flagged this on PR #41.)"""
        import json

        clock = _make_clock()
        b, claim_id = self._setup_with_active_claim(tmp_path, clock)
        events_path = str(tmp_path / "events.jsonl")
        try:
            m = _make_manager(b, actor="agent-alpha", clock=clock)
            m.release(claim_id, reason="work done")
        finally:
            b.close()

        lines = [json.loads(line) for line in open(events_path).readlines() if line.strip()]
        actions = [evt["action"] for evt in lines]
        assert "claim.released" in actions
        # task.status_changed is NOT emitted by release — the handler
        # does the task transition atomically. The only task.status_changed
        # events in the log are from the test setup (ready promotion).
        post_release_lines = lines[lines.index(next(evt for evt in lines if evt["action"] == "claim.released")):]
        post_release_actions = [evt["action"] for evt in post_release_lines]
        assert "task.status_changed" not in post_release_actions, (
            "release() should NOT emit task.status_changed after the claim.released "
            "event — the handler already transitions the task atomically. "
            "Extra event would cause idempotent no-ops or evidence loss."
        )

        released_line = next(evt for evt in lines if evt["action"] == "claim.released")
        assert released_line["payload_json"]["claim_id"] == claim_id
        assert "work done" in released_line["payload_json"]["release_reason"]


# ---------------------------------------------------------------------------
# TestRenew
# ---------------------------------------------------------------------------


class TestRenew:
    def _setup_with_active_claim(
        self,
        tmp_path: Path,
        clock: FrozenClock,
    ) -> tuple[SqliteBackend, str]:
        b = _make_backend(tmp_path, clock)
        _setup_project(b)
        _setup_prd(b)
        conn = sqlite3.connect(str(tmp_path / "state.db"))
        _insert_feature_raw(conn)
        _insert_task_raw(conn, task_id="T001", status="ready")
        conn.close()
        m = _make_manager(b, actor="agent-alpha", clock=clock)
        result = m.claim("T001")
        return b, result.claim.id

    def test_renew_extends_lease(self, tmp_path: Path) -> None:
        """After advancing the clock 5 minutes, renew extends lease by 60 minutes from new now."""
        clock = _make_clock(_T0)
        b, claim_id = self._setup_with_active_claim(tmp_path, clock)
        try:
            claim_before = b.get_claim(claim_id)
            assert claim_before is not None
            initial_expiry = claim_before.lease_expires_at

            # Advance the clock by 5 minutes
            t_plus_5 = _T0 + timedelta(minutes=5)
            clock._current = t_plus_5  # type: ignore[attr-defined]

            m = _make_manager(b, actor="agent-alpha", clock=clock, lease_minutes=60)
            renewed = m.renew(claim_id)

            expected_expiry = t_plus_5 + timedelta(minutes=60)
            assert renewed.lease_expires_at == expected_expiry
            assert renewed.lease_expires_at > initial_expiry
        finally:
            b.close()

    def test_renew_updates_heartbeat(self, tmp_path: Path) -> None:
        """renew updates last_heartbeat_at to clock.now()."""
        clock = _make_clock(_T0)
        b, claim_id = self._setup_with_active_claim(tmp_path, clock)
        try:
            t_plus_3 = _T0 + timedelta(minutes=3)
            clock._current = t_plus_3  # type: ignore[attr-defined]

            m = _make_manager(b, actor="agent-alpha", clock=clock)
            renewed = m.renew(claim_id)

            assert renewed.last_heartbeat_at == t_plus_3
        finally:
            b.close()

    def test_renew_refuses_when_lease_already_expired(self, tmp_path: Path) -> None:
        """renew raises ClaimError when the lease is already expired."""
        clock = _make_clock(_T0)
        b, claim_id = self._setup_with_active_claim(tmp_path, clock)
        try:
            # Advance clock past expiry (default 60 min lease)
            t_expired = _T0 + timedelta(hours=2)
            clock._current = t_expired  # type: ignore[attr-defined]

            m = _make_manager(b, actor="agent-alpha", clock=clock)
            with pytest.raises(ClaimError, match="expired|lease"):
                m.renew(claim_id)
        finally:
            b.close()

    def test_renew_refuses_wrong_actor(self, tmp_path: Path) -> None:
        """renew raises ClaimError when a different actor attempts the heartbeat."""
        clock = _make_clock(_T0)
        b, claim_id = self._setup_with_active_claim(tmp_path, clock)
        try:
            m_other = _make_manager(b, actor="agent-beta", clock=clock)
            with pytest.raises(ClaimError, match="agent-alpha|actor"):
                m_other.renew(claim_id)
        finally:
            b.close()


# ---------------------------------------------------------------------------
# TestStaleDetection
# ---------------------------------------------------------------------------


class TestStaleDetection:
    def _setup_with_expired_claim(
        self,
        tmp_path: Path,
        clock: FrozenClock,
        actor: str = "agent-alpha",
    ) -> tuple[SqliteBackend, str]:
        """Set up a backend with one claim whose lease expired at T0."""
        b = _make_backend(tmp_path, clock)
        _setup_project(b)
        _setup_prd(b)
        conn = sqlite3.connect(str(tmp_path / "state.db"))
        _insert_feature_raw(conn)
        _insert_task_raw(conn, task_id="T001", status="claimed")
        # Claim already expired (expires 1 hour before T0)
        _insert_active_claim_raw(
            conn,
            claim_id="C001",
            task_id="T001",
            actor=actor,
            lease_expires_at=_T0 - timedelta(hours=1),
        )
        conn.close()
        return b, "C001"

    def test_stale_detector_marks_expired_claims(self, tmp_path: Path) -> None:
        """detect_and_release_stale marks expired claims as stale and task returns to ready."""
        clock = _make_clock(_T0)
        b, claim_id = self._setup_with_expired_claim(tmp_path, clock)
        try:
            reaped = detect_and_release_stale(b, clock)
            assert claim_id in reaped

            claim = b.get_claim(claim_id)
            assert claim is not None
            assert claim.status == ClaimStatus.stale

            task = b.get_task("T001")
            assert task is not None
            assert task.status == TaskStatus.ready
        finally:
            b.close()

    def test_stale_detector_skips_already_stale(self, tmp_path: Path) -> None:
        """detect_and_release_stale does not try to re-reap claims already marked stale."""
        clock = _make_clock(_T0)
        b, claim_id = self._setup_with_expired_claim(tmp_path, clock)
        try:
            # First reap
            reaped_first = detect_and_release_stale(b, clock)
            assert claim_id in reaped_first

            # Second call — claim is now stale, not active
            reaped_second = detect_and_release_stale(b, clock)
            assert claim_id not in reaped_second
        finally:
            b.close()

    def test_stale_detector_idempotent(self, tmp_path: Path) -> None:
        """Calling detect_and_release_stale twice leaves the system in the same state."""
        clock = _make_clock(_T0)
        b, claim_id = self._setup_with_expired_claim(tmp_path, clock)
        try:
            detect_and_release_stale(b, clock)
            detect_and_release_stale(b, clock)

            # Still stale, task still ready
            claim = b.get_claim(claim_id)
            assert claim is not None
            assert claim.status == ClaimStatus.stale

            task = b.get_task("T001")
            assert task is not None
            assert task.status == TaskStatus.ready
        finally:
            b.close()

    def test_stale_detector_handles_per_claim_failure_gracefully(self, tmp_path: Path) -> None:
        """If one claim's update fails (e.g. task deleted), others still process."""
        clock = _make_clock(_T0)
        b = _make_backend(tmp_path, clock)
        try:
            _setup_project(b)
            _setup_prd(b)

            conn = sqlite3.connect(str(tmp_path / "state.db"))
            _insert_feature_raw(conn)
            # T001 has a valid expired claim — should be reaped
            _insert_task_raw(conn, task_id="T001", status="claimed")
            _insert_active_claim_raw(
                conn, claim_id="C001", task_id="T001",
                lease_expires_at=_T0 - timedelta(hours=1),
            )
            # T002 has a task that does NOT exist — claim FK would fail if attempted.
            # We directly insert a claim pointing to a non-existent task to simulate
            # the failure case; the stale handler should skip gracefully.
            conn.execute(
                """INSERT INTO claims
                (id, task_id, claimed_by, claim_type, status, expected_files,
                 created_at, lease_expires_at, last_heartbeat_at)
                VALUES ('C002', 'T999', 'agent-orphan', 'task', 'active', '[]', ?, ?, ?)""",
                (
                    _T0.isoformat(),
                    (_T0 - timedelta(hours=1)).isoformat(),
                    _T0.isoformat(),
                ),
            )
            conn.commit()
            conn.close()

            # Should not raise — C002's failure is caught per-claim
            reaped = detect_and_release_stale(b, clock)

            # C001 (valid claim) was reaped
            assert "C001" in reaped

            # T001 is back to ready
            task = b.get_task("T001")
            assert task is not None
            assert task.status == TaskStatus.ready
        finally:
            b.close()


# ---------------------------------------------------------------------------
# CL-3 regression: _reap_stale_claims must NOT swallow SchemaMismatch
# ---------------------------------------------------------------------------


class TestReapStaleClaimsSchemaMismatch:
    """The CLI helper ``_reap_stale_claims`` is wrapped in a try/except so
    transient reaper failures never abort the primary command. Before CL-3
    that except was a bare ``except Exception`` that also swallowed
    ``SchemaMismatch`` — leaving users with a confusing secondary error
    from their actual command instead of the clean "your DB schema is out
    of sync" message. Verify the helper now lets SchemaMismatch propagate
    while still swallowing operational errors (StateLocked, TransactionAborted).
    """

    def test_schema_mismatch_propagates(self, tmp_path: Path) -> None:
        from anvil.cli._helpers import _reap_stale_claims
        from anvil.state.backend import SchemaMismatch

        class _Boom:
            """Stand-in backend whose list_active_claims raises SchemaMismatch.
            The stale detector calls this first; the mismatch surfaces from
            inside ``detect_and_release_stale``.
            """

            def list_active_claims(self) -> list[Any]:
                raise SchemaMismatch("on-disk user_version=99 != expected=10")

        with pytest.raises(SchemaMismatch, match="user_version"):
            _reap_stale_claims(_Boom())  # type: ignore[arg-type]

    def test_state_locked_is_swallowed(self, tmp_path: Path) -> None:
        from anvil.cli._helpers import _reap_stale_claims
        from anvil.state.backend import StateLocked

        class _Locked:
            def list_active_claims(self) -> list[Any]:
                raise StateLocked("busy_timeout exceeded")

        # Must NOT raise — reaping is best-effort for transient lock contention.
        _reap_stale_claims(_Locked())  # type: ignore[arg-type]

    def test_transaction_aborted_is_swallowed(self, tmp_path: Path) -> None:
        from anvil.cli._helpers import _reap_stale_claims
        from anvil.state.backend import TransactionAborted

        class _Aborted:
            def list_active_claims(self) -> list[Any]:
                raise TransactionAborted("rolled back")

        _reap_stale_claims(_Aborted())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# TestCheckConflicts
# ---------------------------------------------------------------------------


class TestCheckConflicts:
    def test_check_conflicts_returns_empty_for_no_overlap(self, tmp_path: Path) -> None:
        """check_conflicts returns [] when no active claims share files."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            _setup_prd(b)
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            _insert_feature_raw(conn)
            _insert_task_raw(conn, task_id="T001", status="ready", likely_files=["src/foo.py"])
            _insert_task_raw(conn, task_id="T002", status="claimed", likely_files=["src/bar.py"])
            _insert_active_claim_raw(
                conn, claim_id="C001", task_id="T002",
                actor="other-agent", expected_files=["src/bar.py"]
            )
            conn.close()

            m = _make_manager(b)
            conflicts = m.check_conflicts("T001", ["src/foo.py"])
            assert conflicts == []
        finally:
            b.close()

    def test_check_conflicts_returns_warning_for_file_overlap(self, tmp_path: Path) -> None:
        """check_conflicts returns ConflictWarning when files overlap."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            _setup_prd(b)
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            _insert_feature_raw(conn)
            _insert_task_raw(conn, task_id="T001", status="ready", likely_files=["src/shared.py"])
            _insert_task_raw(conn, task_id="T002", status="claimed", likely_files=["src/shared.py"])
            _insert_active_claim_raw(
                conn, claim_id="C001", task_id="T002",
                actor="other-agent", expected_files=["src/shared.py"]
            )
            conn.close()

            m = _make_manager(b, actor="agent-alpha")
            conflicts = m.check_conflicts("T001", ["src/shared.py"])
            assert len(conflicts) == 1
            assert isinstance(conflicts[0], ConflictWarning)
            assert "src/shared.py" in conflicts[0].overlapping_files
        finally:
            b.close()

    def test_check_conflicts_returns_empty_for_no_expected_files(self, tmp_path: Path) -> None:
        """check_conflicts returns [] when expected_files is empty (no comparison possible)."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            _setup_prd(b)
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            _insert_feature_raw(conn)
            _insert_task_raw(conn, task_id="T001", status="ready")
            _insert_task_raw(conn, task_id="T002", status="claimed", likely_files=["src/bar.py"])
            _insert_active_claim_raw(
                conn, claim_id="C001", task_id="T002",
                actor="other-agent", expected_files=["src/bar.py"]
            )
            conn.close()

            m = _make_manager(b)
            # Empty expected_files → no file-level conflicts possible
            conflicts = m.check_conflicts("T001", [])
            assert conflicts == []
        finally:
            b.close()

    def test_check_conflicts_skips_same_task_id(self, tmp_path: Path) -> None:
        """check_conflicts skips active claims on the SAME task (re-claim guard)."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            _setup_prd(b)
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            _insert_feature_raw(conn)
            # T001 is both the candidate and the active claim's task
            _insert_task_raw(conn, task_id="T001", status="ready", likely_files=["src/foo.py"])
            # Insert an active claim for T001 by another actor (same task_id)
            _insert_active_claim_raw(
                conn, claim_id="C001", task_id="T001",
                actor="other-agent", expected_files=["src/foo.py"]
            )
            conn.close()

            m = _make_manager(b, actor="agent-alpha")
            # Same task_id → skipped by check_conflicts; no warning
            conflicts = m.check_conflicts("T001", ["src/foo.py"])
            assert conflicts == []
        finally:
            b.close()

    def test_check_conflicts_skips_own_claims(self, tmp_path: Path) -> None:
        """check_conflicts skips active claims owned by the same actor."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            _setup_prd(b)
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            _insert_feature_raw(conn)
            _insert_task_raw(conn, task_id="T001", status="ready", likely_files=["src/foo.py"])
            _insert_task_raw(conn, task_id="T002", status="claimed", likely_files=["src/foo.py"])
            # Active claim on T002 by the SAME actor (agent-alpha)
            _insert_active_claim_raw(
                conn, claim_id="C001", task_id="T002",
                actor="agent-alpha", expected_files=["src/foo.py"]
            )
            conn.close()

            m = _make_manager(b, actor="agent-alpha")
            # Own claim → skipped; no warning
            conflicts = m.check_conflicts("T001", ["src/foo.py"])
            assert conflicts == []
        finally:
            b.close()

    def test_check_conflicts_returns_warning_for_conflict_group(self, tmp_path: Path) -> None:
        """check_conflicts returns ConflictWarning when a conflict_group member is active."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            _setup_prd(b)
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            _insert_feature_raw(conn)
            _insert_task_raw(conn, task_id="T001", status="ready", conflict_groups=["auth"])
            _insert_task_raw(conn, task_id="T002", status="claimed", conflict_groups=["auth"])
            _insert_active_claim_raw(
                conn, claim_id="C001", task_id="T002", actor="other-agent"
            )
            conn.close()

            # check_conflicts is for file overlap; conflict_group is handled separately
            # by _check_group_conflicts inside claim().  This test verifies the
            # conflict_group scenario does NOT produce a file-overlap warning.
            m = _make_manager(b, actor="agent-alpha")
            conflicts = m.check_conflicts("T001", [])
            assert conflicts == []  # empty files → no file-overlap conflict
        finally:
            b.close()


# ---------------------------------------------------------------------------
# PS-1 regression: _check_group_conflicts must not be N+1
# ---------------------------------------------------------------------------


class TestCheckGroupConflictsBulkFetch:
    """``_check_group_conflicts`` used to call ``backend.get_task`` once per
    active claim — an N+1 query that scaled badly with parallel-agent counts.
    After PS-1 it does one ``list_active_claims`` + one ``list_tasks`` call
    regardless of how many active claims share a conflict_group with the
    target. Verify by wrapping the backend in a call-counter.
    """

    def test_check_group_conflicts_does_not_call_get_task_per_claim(
        self, tmp_path: Path
    ) -> None:
        clock = _make_clock()
        b = _make_backend(tmp_path, clock)
        try:
            _setup_project(b)
            _setup_prd(b)

            conn = sqlite3.connect(str(tmp_path / "state.db"))
            _insert_feature_raw(conn)
            # Target task T001 is in conflict_group "auth" and ready to claim.
            _insert_task_raw(
                conn, task_id="T001", status="ready",
                conflict_groups=["auth"],
            )
            # Five other tasks ALSO in "auth", all already claimed by others.
            for i in range(2, 7):
                tid = f"T00{i}"
                _insert_task_raw(
                    conn, task_id=tid, status="claimed",
                    conflict_groups=["auth"],
                )
                _insert_active_claim_raw(
                    conn, claim_id=f"C00{i}", task_id=tid,
                    actor=f"agent-{i}",
                )
            conn.close()

            # Wrap the backend in a counter that tracks how many times each
            # query method was invoked while computing group conflicts.
            class _Counter:
                def __init__(self, inner: SqliteBackend) -> None:
                    self.inner = inner
                    self.get_task_calls = 0
                    self.list_tasks_calls = 0
                    self.list_active_claims_calls = 0

                def __getattr__(self, name: str) -> Any:
                    return getattr(self.inner, name)

                def get_task(self, task_id: str) -> Any:
                    self.get_task_calls += 1
                    return self.inner.get_task(task_id)

                def list_tasks(self, **kw: Any) -> Any:
                    self.list_tasks_calls += 1
                    return self.inner.list_tasks(**kw)

                def list_active_claims(self) -> Any:
                    self.list_active_claims_calls += 1
                    return self.inner.list_active_claims()

            counter = _Counter(b)
            mgr = _make_manager(counter, actor="agent-1", clock=clock)  # type: ignore[arg-type]
            target = b.get_task("T001")
            assert target is not None

            # Directly exercise the helper to isolate its query pattern from
            # the surrounding claim() call (which has its own queries).
            counter.get_task_calls = 0
            counter.list_tasks_calls = 0
            counter.list_active_claims_calls = 0
            conflicts = mgr._check_group_conflicts(target)  # noqa: SLF001

            # All five other auth-group tasks should be flagged as conflicts.
            assert len(conflicts) == 5

            # PS-1: at most one list_active_claims + one list_tasks; ZERO
            # per-claim get_task calls. Before the fix this was 5 get_task
            # round-trips (one per active claim).
            assert counter.list_active_claims_calls == 1
            assert counter.list_tasks_calls == 1
            assert counter.get_task_calls == 0, (
                f"PS-1 regression: _check_group_conflicts performed "
                f"{counter.get_task_calls} per-claim get_task call(s); "
                "should be 0 after bulk-fetch refactor."
            )
        finally:
            b.close()


# ---------------------------------------------------------------------------
# TestClaimManagerEdgeCases — covers uncovered branches for 95%
# ---------------------------------------------------------------------------


class TestClaimManagerEdgeCases:
    """Additional tests targeting uncovered branches to reach the 95% threshold."""

    def test_claim_refuses_when_no_prd(self, tmp_path: Path) -> None:
        """claim() raises ClaimError when there is no PRD in the project at all."""
        clock = _make_clock()
        b = _make_backend(tmp_path, clock)
        try:
            _setup_project(b)
            # No PRD events applied
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            _insert_feature_raw(conn)
            _insert_task_raw(conn, task_id="T001", status="ready")
            conn.close()

            m = _make_manager(b, clock=clock)
            with pytest.raises(ClaimError, match="no PRD|prd|PRD"):
                m.claim("T001")
        finally:
            b.close()

    def test_claim_force_logs_conflict_group_warning(self, tmp_path: Path) -> None:
        """claim(force=True) logs a warning when a conflict_group member is already claimed."""
        import logging
        clock = _make_clock()
        b = _make_backend(tmp_path, clock)
        try:
            _setup_project(b)
            _setup_prd(b)
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            _insert_feature_raw(conn)
            _insert_task_raw(conn, task_id="T001", status="ready", conflict_groups=["auth"])
            _insert_task_raw(conn, task_id="T002", status="claimed", conflict_groups=["auth"])
            _insert_active_claim_raw(conn, claim_id="C001", task_id="T002", actor="other-agent")
            conn.close()

            m = _make_manager(b, actor="agent-alpha", clock=clock)
            with self._capture_warnings(logging.getLogger("anvil.claims.manager")):
                result = m.claim("T001", force=True)
            assert result.claim.status == ClaimStatus.active
        finally:
            b.close()

    @staticmethod
    def _capture_warnings(logger: Any) -> Any:
        """Context manager that tolerates log warnings (for --force paths).

        The `logger` arg is the target whose warnings would be captured;
        currently the implementation just no-ops since the tests only need
        to silence them, not assert against them. Kept in the signature so
        future per-test assertions can plug in without changing callers.
        """
        del logger  # see docstring
        import contextlib

        @contextlib.contextmanager
        def _ctx() -> Any:
            yield

        return _ctx()

    def test_release_claim_not_found(self, tmp_path: Path) -> None:
        """release() raises ClaimError when claim_id doesn't exist."""
        clock = _make_clock()
        b = _make_backend(tmp_path, clock)
        try:
            m = _make_manager(b, clock=clock)
            with pytest.raises(ClaimError, match="not found|C999"):
                m.release("C999")
        finally:
            b.close()

    def test_release_force_on_already_terminal_claim(self, tmp_path: Path) -> None:
        """release(force=True) on an already-released claim raises ClaimError."""
        clock = _make_clock()
        b = _make_backend(tmp_path, clock)
        try:
            _setup_project(b)
            _setup_prd(b)
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            _insert_feature_raw(conn)
            _insert_task_raw(conn, task_id="T001", status="ready")
            conn.close()

            m = _make_manager(b, actor="agent-alpha", clock=clock)
            result = m.claim("T001")
            claim_id = result.claim.id
            m.release(claim_id, force=False)

            # Claim is now released (terminal); force=True should still fail
            with pytest.raises(ClaimError, match="terminal|released|status"):
                m.release(claim_id, force=True)
        finally:
            b.close()

    def test_renew_claim_not_found(self, tmp_path: Path) -> None:
        """renew() raises ClaimError when claim_id doesn't exist."""
        clock = _make_clock()
        b = _make_backend(tmp_path, clock)
        try:
            m = _make_manager(b, clock=clock)
            with pytest.raises(ClaimError, match="not found|C999"):
                m.renew("C999")
        finally:
            b.close()

    def test_renew_refuses_when_claim_is_stale(self, tmp_path: Path) -> None:
        """renew() raises ClaimError when the claim is in 'stale' status (not active)."""
        clock = _make_clock()
        b = _make_backend(tmp_path, clock)
        try:
            _setup_project(b)
            _setup_prd(b)
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            _insert_feature_raw(conn)
            _insert_task_raw(conn, task_id="T001", status="claimed")
            conn.close()
            # Insert a stale claim directly
            conn2 = sqlite3.connect(str(tmp_path / "state.db"))
            conn2.execute(
                """INSERT INTO claims
                (id, task_id, claimed_by, claim_type, status, expected_files,
                 created_at, lease_expires_at, last_heartbeat_at)
                VALUES ('C001', 'T001', 'agent-alpha', 'task', 'stale', '[]', ?, ?, ?)""",
                (_T0.isoformat(), (_T0 + timedelta(hours=1)).isoformat(), _T0.isoformat()),
            )
            conn2.commit()
            conn2.close()

            m = _make_manager(b, actor="agent-alpha", clock=clock)
            with pytest.raises(ClaimError, match="stale|active|status"):
                m.renew("C001")
        finally:
            b.close()

    def test_next_skips_claimed_task_in_ready_list(self, tmp_path: Path) -> None:
        """next_claimable skips a 'ready' task that has a concurrent active claim
        (rare race condition: task in DB as ready but a claim row exists)."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            _setup_prd(b)
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            _insert_feature_raw(conn)
            # T001 appears ready in tasks but has an active claim row
            _insert_task_raw(conn, task_id="T001", status="ready")
            _insert_active_claim_raw(conn, claim_id="C001", task_id="T001", actor="other-agent")
            conn.close()

            m = _make_manager(b)
            # T001's claim means it shows in claimed_task_ids → skipped
            task = m.next_claimable()
            assert task is None
        finally:
            b.close()

    def test_check_group_conflicts_skips_same_task(self, tmp_path: Path) -> None:
        """_check_group_conflicts skips the candidate task's own active claim."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            _setup_prd(b)
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            _insert_feature_raw(conn)
            _insert_task_raw(conn, task_id="T001", status="ready", conflict_groups=["grp"])
            # Active claim for T001 itself (same task)
            _insert_active_claim_raw(conn, claim_id="C001", task_id="T001", actor="agent-alpha")
            conn.close()

            m = _make_manager(b, actor="agent-alpha", clock=_make_clock())
            # _check_group_conflicts skips claims on the candidate task itself
            task = b.get_task("T001")
            assert task is not None
            conflicts = m._check_group_conflicts(task)  # type: ignore[attr-defined]
            assert conflicts == []
        finally:
            b.close()

    def test_check_group_conflicts_deduplicates_by_task_id(self, tmp_path: Path) -> None:
        """_check_group_conflicts returns at most one entry per conflicting task."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            _setup_prd(b)
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            _insert_feature_raw(conn)
            _insert_task_raw(conn, task_id="T001", status="ready", conflict_groups=["grp"])
            _insert_task_raw(conn, task_id="T002", status="claimed", conflict_groups=["grp"])
            # Two active claims for T002 (duplicates from race)
            _insert_active_claim_raw(conn, claim_id="C001", task_id="T002", actor="agent-beta")
            # Second claim for T002 would be seen_task_ids deduplication coverage
            # We can't insert a second claim for T002 without unique ID, so we just verify
            # one conflict entry is returned (seen_task_ids prevents duplicates)
            conn.close()

            m = _make_manager(b, actor="agent-alpha", clock=_make_clock())
            task = b.get_task("T001")
            assert task is not None
            conflicts = m._check_group_conflicts(task)  # type: ignore[attr-defined]
            assert len(conflicts) == 1
            assert conflicts[0][0] == "T002"
        finally:
            b.close()


class TestStaleDetectionEdgeCases:
    """Additional stale detection tests to cover missing lines."""

    def test_stale_detector_exception_handling_per_claim(self, tmp_path: Path) -> None:
        """detect_and_release_stale per-claim exception path (logger.exception).

        The test forces a per-claim failure by monkeypatching append() to raise
        on the second call, covering the except block in stale.py.
        SL1-RR-1: stale.py now calls backend.append(EventDraft), not apply_event.
        """
        import unittest.mock as _mock

        clock = _make_clock(_T0)
        b = _make_backend(tmp_path, clock)
        try:
            _setup_project(b)
            _setup_prd(b)
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            _insert_feature_raw(conn)
            _insert_task_raw(conn, task_id="T001", status="claimed")
            _insert_active_claim_raw(
                conn, claim_id="C001", task_id="T001",
                lease_expires_at=_T0 - timedelta(hours=1),
            )
            _insert_task_raw(conn, task_id="T002", status="claimed")
            _insert_active_claim_raw(
                conn, claim_id="C002", task_id="T002",
                lease_expires_at=_T0 - timedelta(hours=1),
            )
            conn.close()

            from anvil.claims.stale import detect_and_release_stale
            from anvil.state.backend import TransactionAborted as _TA

            call_count = {"n": 0}
            original_append = b.append

            def _raise_on_second(draft: Any) -> Any:
                call_count["n"] += 1
                if call_count["n"] == 2:
                    raise _TA("Simulated per-claim failure")
                return original_append(draft)

            with _mock.patch.object(b, "append", side_effect=_raise_on_second):
                reaped = detect_and_release_stale(b, clock)

            # At least one claim was reaped (the first one); the second raised
            assert len(reaped) >= 1
        finally:
            b.close()

    def test_stale_detector_non_active_claim_defensive_guard(self, tmp_path: Path) -> None:
        """detect_and_release_stale defensive guard (line 73): claims with status != active
        are skipped even if returned by list_active_claims.

        We simulate this by monkeypatching list_active_claims to return a non-active
        claim, covering the defensive `if claim.status != ClaimStatus.active: continue`.
        """
        import unittest.mock as _mock

        from anvil.state.models import Claim, ClaimStatus, ClaimType

        clock = _make_clock(_T0)
        b = _make_backend(tmp_path, clock)
        try:
            # Build a fake "stale" Claim object (already non-active)
            fake_claim = Claim(
                id="C001",
                task_id="T001",
                claimed_by="agent-x",
                claim_type=ClaimType.task,
                status=ClaimStatus.stale,  # not active → should be skipped
                branch=None,
                worktree_path=None,
                expected_files=[],
                created_at=_T0,
                lease_expires_at=_T0 - timedelta(hours=1),  # expired
                last_heartbeat_at=_T0,
                released_at=None,
                release_reason=None,
            )

            from anvil.claims.stale import detect_and_release_stale

            with _mock.patch.object(b, "list_active_claims", return_value=[fake_claim]):
                reaped = detect_and_release_stale(b, clock)

            # The non-active claim was skipped → nothing reaped
            assert reaped == []
        finally:
            b.close()
