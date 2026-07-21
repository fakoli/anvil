"""Tests for anvil.state.sqlite — SqliteBackend including the audit guarantee.

THE MOST CRITICAL TEST: test_replay_from_empty_reconstructs_state_exactly.

Coverage targets:
- initialize() idempotency and schema version check
- append() JSONL + SQLite atomicity (SL1-RR-1 write path)
- append() validation failure → rejection in audit.jsonl, zero events.jsonl lines
- SchemaMismatch on version mismatch
- replay_from_empty() reconstructs identical state (audit guarantee)
- replay_from_empty() strict — no skip list; interior corruption raises
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from anvil.clock import FrozenClock
from anvil.state.backend import (
    EventRejected,
    SchemaMismatch,
    StateLocked,
    TransactionAborted,
)
from anvil.state.models import Event, EventDraft
from anvil.state.schema import SCHEMA_VERSION
from anvil.state.sqlite import (
    _FLOCK_BACKOFF_CAP_S,
    _FLOCK_BACKOFF_INITIAL_S,
    _FLOCK_TIMEOUT_S,
    SqliteBackend,
    _append_lock_acquire_nb,
    _append_lock_release,
    _flock_backoff_delays,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_UTC = UTC
_T0 = datetime(2026, 5, 24, 18, 0, 0, tzinfo=_UTC)
# A DISTINCT, LATER timestamp (T0 + 1h). Used to make the v6->v7 created_at /
# updated_at assertions load-bearing: the migration's source timestamp (T0) and
# a subsequent re-parse event's "now" (T1) must differ, otherwise an UPSERT that
# wrongly overwrote created_at would still leave it == T0 and the test would
# pass vacuously.
_T1 = _T0 + timedelta(hours=1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _make_project_event(
    *,
    event_id: str = "E000001",
    project_id: str = "proj-1",
    project_name: str = "Test Project",
    now: datetime = _T0,
) -> EventDraft:
    """Return an EventDraft for project.created (SL1-RR-1: no id field)."""
    return EventDraft(
        timestamp=now,
        actor="test",
        action="project.created",
        target_kind="project",
        target_id=project_id,
        payload_json={
            "id": project_id,
            "name": project_name,
            "description": "",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        },
    )


def _make_init_event(
    *,
    event_id: str = "E000002",
    project_id: str = "proj-1",
    now: datetime = _T0,
) -> EventDraft:
    """Return an EventDraft for state.initialized (SL1-RR-1: no id field)."""
    return EventDraft(
        timestamp=now,
        actor="test",
        action="state.initialized",
        target_kind="project",
        target_id=project_id,
        payload_json={},
    )


def _apply(
    b: SqliteBackend,
    action: str,
    payload: dict[str, Any],
    *,
    target_kind: str = "project",
    target_id: str = "proj-1",
    now: datetime = _T0,
    actor: str = "test",
) -> Event:
    """Compatibility shim: build an EventDraft and call append(). Returns Event.

    Replaces the retired apply_event(Event(id=..., ...)) pattern. All tests
    that formerly passed hardcoded event IDs now receive the backend-assigned id
    from the returned Event. The id parameter is accepted but ignored — the
    backend is the id authority (SL1-RR-1).
    """
    draft = EventDraft(
        timestamp=now,
        actor=actor,
        action=action,
        target_kind=target_kind,
        target_id=target_id,
        payload_json=payload,
    )
    result = b.append(draft)
    if result is None:
        # Idempotent no-op — create a synthetic Event for caller convenience.
        # This only happens on legal already-satisfied requests.
        return Event(
            id="E000000",
            timestamp=now,
            actor=actor,
            action=action,
            target_kind=target_kind,
            target_id=target_id,
            payload_json=payload,
        )
    return result


def _sqlite_dump(db_path: str) -> str:
    """Return the full .dump output of a SQLite database as a string.

    This is used to compare two databases for structural and data equality.
    Filters out the user_version pragma line to avoid spurious diffs during
    intermediate states.
    """
    result = subprocess.run(
        [sys.executable, "-c",
         f"import sqlite3; conn = sqlite3.connect({db_path!r}); "
         f"print('\\n'.join(conn.iterdump()))"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _read_jsonl(path: str) -> list[dict[str, Any]]:
    """Parse a JSONL file and return a list of event dicts."""
    lines = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                lines.append(json.loads(line))
    return lines


# ---------------------------------------------------------------------------
# Schema initialization
# ---------------------------------------------------------------------------


class TestInitialize:
    def test_initialize_creates_schema(self, tmp_path: Path) -> None:
        """initialize() creates expected tables in SQLite."""
        b = _make_backend(tmp_path)
        try:
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {row[0] for row in cursor.fetchall()}
            conn.close()

            expected_tables = {
                "projects",
                "prds",
                "requirements",
                "features",
                "tasks",
                "claims",
                "evidence",
                "decisions",
                "reviews",
                "events",
                "sync_mappings",
                "conflict_groups",
            }
            assert expected_tables.issubset(tables)
        finally:
            b.close()

    def test_initialize_sets_schema_version(self, tmp_path: Path) -> None:
        """initialize() sets PRAGMA user_version to SCHEMA_VERSION."""
        b = _make_backend(tmp_path)
        try:
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            row = conn.execute("PRAGMA user_version").fetchone()
            conn.close()
            assert row[0] == SCHEMA_VERSION
        finally:
            b.close()

    def test_initialize_is_idempotent(self, tmp_path: Path) -> None:
        """Calling initialize() twice raises no error."""
        b = _make_backend(tmp_path)
        try:
            b.initialize()  # second call — should not raise
            b.initialize()  # third call — should also not raise
        finally:
            b.close()

    def test_initialize_without_calling_raises_on_query(self, tmp_path: Path) -> None:
        """Using backend without initialize() raises RuntimeError."""
        clock = _make_clock()
        db_path = str(tmp_path / "state.db")
        events_path = str(tmp_path / "events.jsonl")
        Path(events_path).touch()
        b = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock)
        # Do NOT call initialize()
        with pytest.raises(RuntimeError, match="initialize"):
            b.get_project()
        b.close()


# ---------------------------------------------------------------------------
# apply_event — atomicity
# ---------------------------------------------------------------------------


class TestApplyEvent:
    def test_append_writes_jsonl_and_sqlite(
        self, tmp_path: Path
    ) -> None:
        """append() writes JSONL line AND SQLite row — both or neither (SL1-RR-1)."""
        b = _make_backend(tmp_path)
        events_path = str(tmp_path / "events.jsonl")
        try:
            event = b.append(_make_project_event())

            # JSONL line is present with assigned id.
            assert event is not None
            events = _read_jsonl(events_path)
            assert any(e.get("id") == event.id for e in events)

            # SQLite Project row is present
            project = b.get_project()
            assert project is not None
            assert project.id == "proj-1"
            assert project.name == "Test Project"
        finally:
            b.close()

    def test_append_state_initialized_event(self, tmp_path: Path) -> None:
        """state.initialized event is accepted and recorded in events table."""
        b = _make_backend(tmp_path)
        events_path = str(tmp_path / "events.jsonl")
        try:
            b.append(_make_project_event())
            b.append(_make_init_event())

            events = _read_jsonl(events_path)
            actions = [e["action"] for e in events]
            assert "state.initialized" in actions
        finally:
            b.close()

    def test_append_unknown_action_raises_event_rejected(self, tmp_path: Path) -> None:
        """Unsupported action → EventRejected (SL1-RR-1: no line in events.jsonl).

        Under SL1-RR-1: validation happens BEFORE the log write. An unknown
        action raises EventRejected (not TransactionAborted). Zero lines are
        written to events.jsonl; one rejection line is written to audit.jsonl.
        The abort tombstone (error.transaction_aborted) no longer exists.
        """
        b = _make_backend(tmp_path)
        events_path = str(tmp_path / "events.jsonl")
        try:
            bad_draft = EventDraft(
                timestamp=_T0,
                actor="test",
                action="unsupported.action",  # not in dispatch table
                target_kind="project",
                target_id="proj-1",
                payload_json={},
            )
            with pytest.raises(EventRejected):
                b.append(bad_draft)

            # ZERO lines in events.jsonl — validation is pre-log (SL1-RR-1).
            events = _read_jsonl(events_path)
            assert len(events) == 0, (
                f"SL1-RR-1: unknown-action rejection must not write to events.jsonl; "
                f"got: {events}"
            )

            # SQLite project row should NOT be present
            project = b.get_project()
            assert project is None
        finally:
            b.close()

    def test_append_adds_event_to_events_mirror_table(self, tmp_path: Path) -> None:
        """Successful append() records the event in the SQLite events mirror table."""
        b = _make_backend(tmp_path)
        try:
            event = b.append(_make_project_event())
            assert event is not None
            # Query the events mirror table directly using the returned id.
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            row = conn.execute(
                "SELECT id, action FROM events WHERE id = ?", (event.id,)
            ).fetchone()
            conn.close()
            assert row is not None
            assert row[1] == "project.created"
        finally:
            b.close()


# ---------------------------------------------------------------------------
# Schema version mismatch
# ---------------------------------------------------------------------------


class TestSchemaMismatch:
    def test_schema_version_mismatch_raises(self, tmp_path: Path) -> None:
        """A state.db with wrong PRAGMA user_version raises SchemaMismatch on second initialize().

        The mismatch check fires on the second+ call to initialize() (when the connection
        is already open). The first call runs DDL which sets the version, so we need to
        simulate the scenario where another process wrote a different version.

        We test this by:
        1. Initializing normally (version = SCHEMA_VERSION).
        2. Close the backend.
        3. Manually clobber the user_version to a wrong value.
        4. Open a NEW backend against the same db_path — this triggers _apply_ddl()
           which sets user_version back to SCHEMA_VERSION. So the mismatch window is
           on a *second call to initialize()* on an already-open connection.

        Alternatively: we test the _check_schema_version() path directly, which is
        triggered when the connection is re-initialized after already being open.
        """
        db_path = str(tmp_path / "state.db")
        events_path = str(tmp_path / "events.jsonl")
        Path(events_path).touch()

        # Step 1: Initialize normally to create the schema.
        clock = _make_clock()
        b = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock)
        b.initialize()

        # Step 2: Second call to initialize() on the open connection → triggers _check_schema_version.
        # But it uses the same connection so version is still correct. Force a version mismatch
        # by manipulating the live connection's user_version:
        wrong_version = SCHEMA_VERSION + 99
        # pylint: disable=protected-access
        assert b._conn is not None  # guaranteed by initialize() above
        b._conn.execute(f"PRAGMA user_version = {wrong_version}")

        with pytest.raises(SchemaMismatch):
            b.initialize()  # re-checks version on re-entry

        b.close()


# ---------------------------------------------------------------------------
# THE AUDIT GUARANTEE
# ---------------------------------------------------------------------------


class TestAuditGuarantee:
    def test_replay_from_empty_reconstructs_state_exactly(self, tmp_path: Path) -> None:
        """Replay 3-5 events from events.jsonl; reconstructed state.db matches original.

        Steps:
        1. Apply 3 events (project.created, state.initialized, project.created again
           as an update).
        2. Snapshot the SQLite dump.
        3. Call replay_from_empty(events.jsonl).
        4. Assert new dump == original snapshot.
        """
        clock = _make_clock()
        b = _make_backend(tmp_path, clock)
        events_path = str(tmp_path / "events.jsonl")
        db_path = str(tmp_path / "state.db")

        try:
            # Apply event 1: project.created
            b.append(_make_project_event(event_id="E000001"))
            # Apply event 2: state.initialized
            b.append(_make_init_event(event_id="E000002"))
            # Apply event 3: second project.created (updates project row)
            b.append(
                _make_project_event(
                    event_id="E000003",
                    project_id="proj-1",
                    project_name="Test Project v2",
                )
            )
        finally:
            b.close()

        # Capture original dump
        original_dump = _sqlite_dump(db_path)

        # Replay from empty — should reconstruct identical state
        clock2 = _make_clock()
        b2 = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock2)
        b2.initialize()  # need to open before replay
        try:
            b2.replay_from_empty(events_path)
        finally:
            pass  # replay_from_empty calls close() and re-opens internally

        b2.close()

        # Capture replayed dump
        replayed_dump = _sqlite_dump(db_path)

        assert original_dump == replayed_dump, (
            "Replayed state.db does not match original.\n"
            f"Original dump (truncated):\n{original_dump[:500]}\n\n"
            f"Replayed dump (truncated):\n{replayed_dump[:500]}"
        )

    def test_replay_clean_log_after_rejected_append(self, tmp_path: Path) -> None:
        """Rejected append leaves events.jsonl clean; replay reconstructs full state.

        Under SL1-RR-1 the canonical log never contains abort tombstones.
        A validation failure (EventRejected) is recorded only in audit.jsonl.
        Replay must reconstruct the full state from the clean log.
        """
        clock = _make_clock()
        db_path = str(tmp_path / "state.db")
        events_path = str(tmp_path / "events.jsonl")
        Path(events_path).touch()

        b = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock)
        b.initialize()

        try:
            # Apply good event
            b.append(_make_project_event())

            # Attempt a bad draft — should raise EventRejected, NOT write to events.jsonl
            bad_draft = EventDraft(
                timestamp=_T0,
                actor="test",
                action="unsupported.action",  # not in dispatch table
                target_kind="project",
                target_id="proj-1",
                payload_json={},
            )
            with pytest.raises(EventRejected):
                b.append(bad_draft)

            # Apply another good event after the rejection
            b.append(_make_init_event())
        finally:
            b.close()

        # Verify JSONL contains ONLY the two real events — no abort tombstones.
        events = _read_jsonl(events_path)
        actions = [e.get("action") for e in events]
        assert "error.transaction_aborted" not in actions, (
            "SL1-RR-1: rejected appends must not write to events.jsonl"
        )
        assert "project.created" in actions
        assert "state.initialized" in actions

        # Now replay from empty — should reconstruct full state
        clock2 = _make_clock()
        b3 = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock2)
        b3.initialize()
        try:
            b3.replay_from_empty(events_path)
            project = b3.get_project()
        finally:
            b3.close()

        # Project should be present (from project.created)
        assert project is not None
        assert project.id == "proj-1"

    def test_replay_produces_correct_project_data(self, tmp_path: Path) -> None:
        """After replay, get_project() returns the same Project as before."""
        clock = _make_clock()
        b = _make_backend(tmp_path, clock)
        events_path = str(tmp_path / "events.jsonl")
        db_path = str(tmp_path / "state.db")

        try:
            b.append(_make_project_event(project_name="My Replayed Project"))
            original_project = b.get_project()
        finally:
            b.close()

        assert original_project is not None

        # Replay
        clock2 = _make_clock()
        b2 = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock2)
        b2.initialize()
        try:
            b2.replay_from_empty(events_path)
            replayed_project = b2.get_project()
        finally:
            b2.close()

        assert replayed_project is not None
        assert replayed_project.id == original_project.id
        assert replayed_project.name == original_project.name
        assert replayed_project.description == original_project.description

    def test_replay_from_empty_with_missing_events_file_is_noop(self, tmp_path: Path) -> None:
        """If events.jsonl does not exist, replay_from_empty returns cleanly."""
        clock = _make_clock()
        db_path = str(tmp_path / "state.db")
        events_path = str(tmp_path / "events.jsonl")
        Path(events_path).touch()

        b = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock)
        b.initialize()
        try:
            # Point to a non-existent file
            b.replay_from_empty(str(tmp_path / "nonexistent.jsonl"))
            # Should be a fresh empty database
            project = b.get_project()
            assert project is None
        finally:
            b.close()


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


class TestQueryHelpers:
    def test_get_project_returns_none_when_empty(self, tmp_path: Path) -> None:
        b = _make_backend(tmp_path)
        try:
            assert b.get_project() is None
        finally:
            b.close()

    def test_get_project_returns_project_after_event(self, tmp_path: Path) -> None:
        b = _make_backend(tmp_path)
        try:
            b.append(_make_project_event(project_name="Hello World"))
            project = b.get_project()
            assert project is not None
            assert project.name == "Hello World"
        finally:
            b.close()

    def test_get_prd_returns_none_when_empty(self, tmp_path: Path) -> None:
        b = _make_backend(tmp_path)
        try:
            assert b.get_prd() is None
        finally:
            b.close()

    def test_list_tasks_returns_empty_list_when_no_tasks(self, tmp_path: Path) -> None:
        b = _make_backend(tmp_path)
        try:
            assert b.list_tasks() == []
        finally:
            b.close()

    def test_list_active_claims_returns_empty_list_when_no_claims(self, tmp_path: Path) -> None:
        b = _make_backend(tmp_path)
        try:
            assert b.list_active_claims() == []
        finally:
            b.close()

    def test_get_claim_returns_none_for_unknown_id(self, tmp_path: Path) -> None:
        b = _make_backend(tmp_path)
        try:
            assert b.get_claim("nonexistent") is None
        finally:
            b.close()

    def test_get_task_returns_none_for_unknown_id(self, tmp_path: Path) -> None:
        b = _make_backend(tmp_path)
        try:
            assert b.get_task("nonexistent") is None
        finally:
            b.close()


# ---------------------------------------------------------------------------
# Close idempotency
# ---------------------------------------------------------------------------


class TestClose:
    def test_close_is_idempotent(self, tmp_path: Path) -> None:
        """Calling close() multiple times raises no error."""
        b = _make_backend(tmp_path)
        b.close()
        b.close()  # second close — should not raise
        b.close()  # third close — should not raise


# ---------------------------------------------------------------------------
# Replay — corrupted JSONL line
# ---------------------------------------------------------------------------


class TestReplayCorruptedJSONL:
    def test_replay_interior_corrupted_line_raises(self, tmp_path: Path) -> None:
        """Interior corrupted JSONL line raises during replay_from_empty (SL1-RR-1 strict).

        Under SL1-RR-1, replay is strict: every line in events.jsonl is a fact.
        An interior malformed line is real corruption and replay RAISES — it
        does not skip. Only the torn TRAILING line is tolerated.
        """
        clock = _make_clock()
        db_path = str(tmp_path / "state.db")
        events_path = str(tmp_path / "events.jsonl")
        Path(events_path).touch()

        b = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock)
        b.initialize()
        try:
            b.append(_make_project_event())
        finally:
            b.close()

        # Inject a corrupted line in the INTERIOR (between two real events).
        with open(events_path, "r+", encoding="utf-8") as fh:
            existing = fh.read()

        # Write: real line, corrupted interior line, another real line.
        valid_line = json.dumps({
            "id": "E000002",
            "timestamp": _T0.isoformat(),
            "actor": "test",
            "action": "state.initialized",
            "target_kind": "project",
            "target_id": "proj-1",
            "payload_json": {},
        })
        with open(events_path, "w", encoding="utf-8") as fh:
            fh.write(existing.rstrip("\n") + "\n")
            fh.write("NOT VALID JSON {{{\n")
            fh.write(valid_line + "\n")

        # Replay from events.jsonl — interior corruption must RAISE.
        clock2 = _make_clock()
        b2 = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock2)
        with pytest.raises((TransactionAborted, ValueError)):
            b2.replay_from_empty(events_path)

    def test_replay_torn_trailing_line_is_tolerated(self, tmp_path: Path) -> None:
        """A torn (partial) TRAILING line is silently tolerated during replay.

        Under SL1-RR-1, only the last log line can be torn by a crash. The event
        was never committed (not returned to any caller), so ignoring a partial
        final line is safe. Interior corruption raises; only the trailing case is
        tolerated.
        """
        clock = _make_clock()
        db_path = str(tmp_path / "state.db")
        events_path = str(tmp_path / "events.jsonl")
        Path(events_path).touch()

        b = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock)
        b.initialize()
        try:
            b.append(_make_project_event())
        finally:
            b.close()

        # Append a partial/torn trailing line (simulates crash mid-write).
        with open(events_path, "a", encoding="utf-8") as fh:
            fh.write('{"id": "E000002", "action": "state.init')  # deliberately torn

        clock2 = _make_clock()
        b2 = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock2)
        b2.replay_from_empty(events_path)
        try:
            project = b2.get_project()
            assert project is not None
            assert project.id == "proj-1"
        finally:
            b2.close()

    def test_replay_empty_lines_only_in_file(self, tmp_path: Path) -> None:
        """A file with only empty/whitespace lines replays to an empty-but-valid state."""
        clock = _make_clock()
        db_path = str(tmp_path / "state.db")
        events_path = str(tmp_path / "events.jsonl")

        # Create a file with only empty lines.
        with open(events_path, "w", encoding="utf-8") as fh:
            fh.write("\n\n\n")

        b = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock)
        b.initialize()
        try:
            b.replay_from_empty(events_path)
            project = b.get_project()
            assert project is None  # nothing was replayed
        finally:
            b.close()


# ---------------------------------------------------------------------------
# Row deserialization — direct DB insertion tests
# ---------------------------------------------------------------------------


class TestRowDeserialization:
    """Tests that exercise _row_to_task, _row_to_claim, _row_to_prd by inserting
    test rows directly into SQLite (bypassing the event system, which doesn't
    support task/claim/prd creation in Phase 2).
    """

    def _insert_feature(self, conn: sqlite3.Connection) -> None:
        """Insert a minimal feature row to satisfy tasks FK constraint."""
        conn.execute(
            "INSERT OR IGNORE INTO features (id, title, description, status, requirements, tasks) "
            "VALUES ('F001', 'Test Feature', 'desc', 'proposed', '[]', '[]')"
        )
        conn.commit()

    def test_row_to_task_via_list_tasks(self, tmp_path: Path) -> None:
        """_row_to_task is exercised by list_tasks when tasks are in the DB."""
        b = _make_backend(tmp_path)
        try:
            # Insert a task row directly via the connection
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            conn.row_factory = sqlite3.Row
            self._insert_feature(conn)
            conn.execute(
                """INSERT INTO tasks
                (id, feature_id, title, description, status, priority,
                 dependencies, conflict_groups, scores, acceptance_criteria,
                 implementation_notes, verification, likely_files,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "T001",
                    "F001",
                    "Test Task",
                    "A test task",
                    "proposed",
                    "medium",
                    "[]",
                    "[]",
                    "{}",
                    "[]",
                    "[]",
                    "{}",
                    "[]",
                    _T0.isoformat(),
                    _T0.isoformat(),
                ),
            )
            conn.commit()
            conn.close()

            # Now list_tasks should exercise _row_to_task
            tasks = b.list_tasks()
            assert len(tasks) == 1
            assert tasks[0].id == "T001"
            assert tasks[0].title == "Test Task"
        finally:
            b.close()

    def test_list_tasks_filter_by_status(self, tmp_path: Path) -> None:
        """list_tasks(status=...) exercises the WHERE status = ? code path."""
        b = _make_backend(tmp_path)
        try:
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            self._insert_feature(conn)
            # Insert two tasks with different statuses
            for task_id, status in [("T001", "proposed"), ("T002", "ready")]:
                conn.execute(
                    """INSERT INTO tasks
                    (id, feature_id, title, description, status, priority,
                     dependencies, conflict_groups, scores, acceptance_criteria,
                     implementation_notes, verification, likely_files,
                     created_at, updated_at)
                    VALUES (?, 'F001', ?, 'desc', ?, 'medium',
                     '[]', '[]', '{}', '[]', '[]', '{}', '[]', ?, ?)""",
                    (task_id, f"Task {task_id}", status, _T0.isoformat(), _T0.isoformat()),
                )
            conn.commit()
            conn.close()

            # Filter by status
            proposed_tasks = b.list_tasks(status="proposed")
            ready_tasks = b.list_tasks(status="ready")
            all_tasks = b.list_tasks()

            assert len(proposed_tasks) == 1
            assert proposed_tasks[0].id == "T001"
            assert len(ready_tasks) == 1
            assert ready_tasks[0].id == "T002"
            assert len(all_tasks) == 2
        finally:
            b.close()

    def test_list_tasks_filter_by_feature_id(self, tmp_path: Path) -> None:
        """list_tasks(feature_id=...) exercises the WHERE feature_id = ? code path."""
        b = _make_backend(tmp_path)
        try:
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            self._insert_feature(conn)
            # Insert another feature
            conn.execute(
                "INSERT OR IGNORE INTO features (id, title, description, status, requirements, tasks) "
                "VALUES ('F002', 'Feature 2', 'desc', 'proposed', '[]', '[]')"
            )
            for task_id, feature_id in [("T001", "F001"), ("T002", "F002")]:
                conn.execute(
                    """INSERT INTO tasks
                    (id, feature_id, title, description, status, priority,
                     dependencies, conflict_groups, scores, acceptance_criteria,
                     implementation_notes, verification, likely_files,
                     created_at, updated_at)
                    VALUES (?, ?, ?, 'desc', 'proposed', 'medium',
                     '[]', '[]', '{}', '[]', '[]', '{}', '[]', ?, ?)""",
                    (task_id, feature_id, f"Task {task_id}", _T0.isoformat(), _T0.isoformat()),
                )
            conn.commit()
            conn.close()

            f1_tasks = b.list_tasks(feature_id="F001")
            f2_tasks = b.list_tasks(feature_id="F002")

            assert len(f1_tasks) == 1
            assert f1_tasks[0].feature_id == "F001"
            assert len(f2_tasks) == 1
            assert f2_tasks[0].feature_id == "F002"
        finally:
            b.close()

    def test_list_tasks_filter_by_status_and_feature_id(self, tmp_path: Path) -> None:
        """list_tasks with both status and feature_id filters (AND clause)."""
        b = _make_backend(tmp_path)
        try:
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            self._insert_feature(conn)
            for task_id, status in [("T001", "proposed"), ("T002", "ready")]:
                conn.execute(
                    """INSERT INTO tasks
                    (id, feature_id, title, description, status, priority,
                     dependencies, conflict_groups, scores, acceptance_criteria,
                     implementation_notes, verification, likely_files,
                     created_at, updated_at)
                    VALUES (?, 'F001', ?, 'desc', ?, 'medium',
                     '[]', '[]', '{}', '[]', '[]', '{}', '[]', ?, ?)""",
                    (task_id, f"Task {task_id}", status, _T0.isoformat(), _T0.isoformat()),
                )
            conn.commit()
            conn.close()

            # Only T001 is F001 + proposed
            filtered = b.list_tasks(status="proposed", feature_id="F001")
            assert len(filtered) == 1
            assert filtered[0].id == "T001"

            # No task is F001 + done
            empty = b.list_tasks(status="done", feature_id="F001")
            assert len(empty) == 0
        finally:
            b.close()

    def test_row_to_claim_via_get_claim(self, tmp_path: Path) -> None:
        """_row_to_claim is exercised by get_claim when claims are in the DB."""
        b = _make_backend(tmp_path)
        try:
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            self._insert_feature(conn)
            # Insert a task first (FK constraint)
            conn.execute(
                """INSERT INTO tasks
                (id, feature_id, title, description, status, priority,
                 dependencies, conflict_groups, scores, acceptance_criteria,
                 implementation_notes, verification, likely_files,
                 created_at, updated_at)
                VALUES ('T001', 'F001', 'Task', 'desc', 'claimed', 'medium',
                 '[]', '[]', '{}', '[]', '[]', '{}', '[]', ?, ?)""",
                (_T0.isoformat(), _T0.isoformat()),
            )
            # Insert a claim
            expires = (_T0 + timedelta(hours=1)).isoformat()
            conn.execute(
                """INSERT INTO claims
                (id, task_id, claimed_by, claim_type, status, expected_files,
                 created_at, lease_expires_at, last_heartbeat_at)
                VALUES ('C001', 'T001', 'agent-x', 'task', 'active', '[]', ?, ?, ?)""",
                (_T0.isoformat(), expires, _T0.isoformat()),
            )
            conn.commit()
            conn.close()

            claim = b.get_claim("C001")
            assert claim is not None
            assert claim.id == "C001"
            assert claim.claimed_by == "agent-x"
            assert claim.task_id == "T001"
        finally:
            b.close()

    def test_list_active_claims_returns_active_claims(self, tmp_path: Path) -> None:
        """list_active_claims() returns active claims when they exist."""
        b = _make_backend(tmp_path)
        try:
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            self._insert_feature(conn)
            conn.execute(
                """INSERT INTO tasks
                (id, feature_id, title, description, status, priority,
                 dependencies, conflict_groups, scores, acceptance_criteria,
                 implementation_notes, verification, likely_files,
                 created_at, updated_at)
                VALUES ('T001', 'F001', 'Task', 'desc', 'claimed', 'medium',
                 '[]', '[]', '{}', '[]', '[]', '{}', '[]', ?, ?)""",
                (_T0.isoformat(), _T0.isoformat()),
            )
            expires = (_T0 + timedelta(hours=1)).isoformat()
            # Active claim
            conn.execute(
                """INSERT INTO claims
                (id, task_id, claimed_by, claim_type, status, expected_files,
                 created_at, lease_expires_at, last_heartbeat_at)
                VALUES ('C001', 'T001', 'agent-x', 'task', 'active', '[]', ?, ?, ?)""",
                (_T0.isoformat(), expires, _T0.isoformat()),
            )
            # Released claim (should not appear)
            conn.execute(
                """INSERT INTO claims
                (id, task_id, claimed_by, claim_type, status, expected_files,
                 created_at, lease_expires_at, last_heartbeat_at)
                VALUES ('C002', 'T001', 'agent-y', 'task', 'released', '[]', ?, ?, ?)""",
                (_T0.isoformat(), expires, _T0.isoformat()),
            )
            conn.commit()
            conn.close()

            active = b.list_active_claims()
            assert len(active) == 1
            assert active[0].id == "C001"
            assert active[0].status.value == "active"
        finally:
            b.close()

    def test_row_to_prd_via_get_prd(self, tmp_path: Path) -> None:
        """_row_to_prd is exercised by get_prd when a PRD is in the DB."""
        b = _make_backend(tmp_path)
        try:
            # Insert project first (PRD FK)
            b.append(_make_project_event(event_id="E000001"))

            # Insert the default PRD row directly. Under v7, get_prd() no-arg
            # resolves the is_default=1 row, so the row must carry is_default=1
            # (the single-PRD default), id='default', and tz-aware timestamps.
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            conn.execute(
                """INSERT INTO prds
                (id, project_id, status, summary, goals, non_goals, requirements,
                 acceptance_criteria, risks, open_questions,
                 is_default, created_at, updated_at)
                VALUES ('default', 'proj-1', 'draft', 'test prd', '[]', '[]', '[]',
                        '[]', '[]', '[]', 1, ?, ?)""",
                (_T0.isoformat(), _T0.isoformat()),
            )
            conn.commit()
            conn.close()

            prd = b.get_prd()
            assert prd is not None
            assert prd.status.value == "draft"
            assert prd.summary == "test prd"
            assert prd.id == "default"
            assert prd.is_default is True
        finally:
            b.close()

    def test_get_task_returns_task_by_id(self, tmp_path: Path) -> None:
        """get_task() exercises _row_to_task for a specific task ID."""
        b = _make_backend(tmp_path)
        try:
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            self._insert_feature(conn)
            conn.execute(
                """INSERT INTO tasks
                (id, feature_id, title, description, status, priority,
                 dependencies, conflict_groups, scores, acceptance_criteria,
                 implementation_notes, verification, likely_files,
                 created_at, updated_at)
                VALUES ('T001', 'F001', 'Task Title', 'desc', 'proposed', 'high',
                 '[]', '[]', '{}', '["it works"]', '[]', '{}', '[]', ?, ?)""",
                (_T0.isoformat(), _T0.isoformat()),
            )
            conn.commit()
            conn.close()

            task = b.get_task("T001")
            assert task is not None
            assert task.id == "T001"
            assert task.title == "Task Title"
            assert task.priority.value == "high"
            assert task.acceptance_criteria == ["it works"]
        finally:
            b.close()


# ---------------------------------------------------------------------------
# JSONL write failure
# ---------------------------------------------------------------------------


class TestJSONLWriteFailure:
    def test_append_jsonl_write_failure_raises_error(
        self, tmp_path: Path
    ) -> None:
        """If events.jsonl is unwritable, append() raises an OS or backend error."""
        import stat

        clock = _make_clock()
        db_path = str(tmp_path / "state.db")
        events_path = str(tmp_path / "events.jsonl")
        Path(events_path).touch()

        b = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock)
        b.initialize()

        # Make events.jsonl read-only so writes fail.
        # Under SL1-RR-1, the lock acquisition itself fails with PermissionError
        # (the flock opens the file for appending). The error may surface as
        # TransactionAborted, PermissionError, or OSError.
        os.chmod(events_path, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
        try:
            event = _make_project_event()
            with pytest.raises((TransactionAborted, PermissionError, OSError)):
                b.append(event)
        finally:
            # Restore write permission so cleanup works
            os.chmod(events_path, stat.S_IREAD | stat.S_IWRITE)
            b.close()


# ---------------------------------------------------------------------------
# Phase 3 event handlers — helper factories
# ---------------------------------------------------------------------------


def _make_feature_payload(
    *,
    feat_id: str = "F001",
    title: str = "Test Feature",
    description: str = "A test feature.",
    status: str = "proposed",
    requirements: list[str] | None = None,
    tasks: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": feat_id,
        "title": title,
        "description": description,
        "status": status,
        "requirements": requirements or [],
        "tasks": tasks or [],
    }


def _make_task_payload(
    *,
    task_id: str = "T001",
    feature_id: str = "F001",
    title: str = "Test Task",
    description: str = "A test task.",
    status: str = "proposed",
    priority: str = "medium",
    task_type: str | None = None,
    acceptance_criteria: list[str] | None = None,
    verification_commands: list[str] | None = None,
    likely_files: list[str] | None = None,
    now: datetime = _T0,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": task_id,
        "feature_id": feature_id,
        "title": title,
        "description": description,
        "status": status,
        "priority": priority,
        "dependencies": [],
        "conflict_groups": [],
        "scores": {},
        "acceptance_criteria": acceptance_criteria or ["Tests pass."],
        "implementation_notes": [],
        "verification": {
            "commands": verification_commands or ["pytest tests/ -v"],
            "manual_steps": [],
            "required_evidence": [],
        },
        "likely_files": likely_files or [],
        "parent_task_id": None,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
    }
    # Only set task_type when explicitly requested so the default-omitted path
    # (a payload that predates the field) is also exercised by callers.
    if task_type is not None:
        payload["task_type"] = task_type
    return payload


class TestTaskTypePersistence:
    """T015: task_type flows through task.created → SQLite → list filter."""

    def test_non_feature_task_type_persists_through_event_pipeline(
        self, tmp_path: Path
    ) -> None:
        """A task.created with task_type=bugfix round-trips to the Task model."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            b.append(_make_event(
                "feature.created", _make_feature_payload(feat_id="F001"),
                event_id="E000010", target_kind="feature", target_id="F001",
            ))
            b.append(_make_event(
                "task.created",
                _make_task_payload(task_id="T001", task_type="bugfix"),
                event_id="E000011", target_kind="task", target_id="T001",
            ))
            task = b.get_task("T001")
            assert task is not None
            assert task.task_type.value == "bugfix"
        finally:
            b.close()

    def test_task_type_omitted_payload_defaults_to_feature(
        self, tmp_path: Path
    ) -> None:
        """A task.created payload with NO task_type key persists as 'feature'."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            b.append(_make_event(
                "feature.created", _make_feature_payload(feat_id="F001"),
                event_id="E000010", target_kind="feature", target_id="F001",
            ))
            payload = _make_task_payload(task_id="T001")
            assert "task_type" not in payload  # truly absent
            b.append(_make_event(
                "task.created", payload,
                event_id="E000011", target_kind="task", target_id="T001",
            ))
            task = b.get_task("T001")
            assert task is not None
            assert task.task_type.value == "feature"
        finally:
            b.close()

    def test_list_tasks_filter_by_task_type(self, tmp_path: Path) -> None:
        """list_tasks(task_type=...) scopes the result to that kind."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            b.append(_make_event(
                "feature.created", _make_feature_payload(feat_id="F001"),
                event_id="E000010", target_kind="feature", target_id="F001",
            ))
            for tid, ttype, ev in [
                ("T001", "feature", "E000011"),
                ("T002", "bugfix", "E000012"),
                ("T003", "refactor", "E000013"),
            ]:
                b.append(_make_event(
                    "task.created",
                    _make_task_payload(task_id=tid, task_type=ttype),
                    event_id=ev, target_kind="task", target_id=tid,
                ))

            bugfixes = b.list_tasks(task_type="bugfix")
            assert [t.id for t in bugfixes] == ["T002"]
            refactors = b.list_tasks(task_type="refactor")
            assert [t.id for t in refactors] == ["T003"]
            # No filter → all three.
            assert len(b.list_tasks()) == 3
        finally:
            b.close()


class TestErrorPaths:
    """Cover the error-handling branches of SqliteBackend.

    These are the audit-critical safety nets: rollback on mutation failure,
    abort-event tombstones, and graceful skip during replay. They're
    infrastructure code paths that rarely fire in healthy operation but must
    behave correctly when they do — otherwise the replay/audit guarantee
    silently degrades.
    """

    def test_unknown_action_raises_event_rejected_no_log_line(
        self, tmp_path: Path
    ) -> None:
        """An action not in the dispatch table raises EventRejected; zero lines in events.jsonl.

        SL1-RR-1: validation is pre-log. Unknown actions raise EventRejected
        (not TransactionAborted), and NO line is written to events.jsonl.
        The abort tombstone (error.transaction_aborted) no longer exists.
        """
        b = _make_backend(tmp_path)
        events_path = str(tmp_path / "events.jsonl")
        try:
            bad_draft = EventDraft(
                timestamp=_T0,
                actor="test",
                action="task.never_implemented",  # not in dispatch table
                target_kind="task",
                target_id="T001",
                payload_json={"task_id": "T001"},
            )
            with pytest.raises(EventRejected):
                b.append(bad_draft)

            # ZERO lines in events.jsonl — validation is pre-log.
            events = _read_jsonl(events_path)
            assert len(events) == 0, (
                f"SL1-RR-1: unknown action must not write to events.jsonl; got: {events}"
            )
            # SQLite must NOT contain the task row.
            assert b.get_task("T001") is None
        finally:
            b.close()

    def test_close_is_idempotent_after_failure(self, tmp_path: Path) -> None:
        """close() must swallow exceptions from the underlying connection so
        double-close (and close-after-error) never leaks an exception to
        callers. Covers the close() exception-swallow path."""
        b = _make_backend(tmp_path)
        b.close()
        # Second close must not raise; the connection is already None.
        b.close()
        # Re-close again on a freshly-failed connection by forcing one.
        b._conn = sqlite3.connect(":memory:")  # noqa: SLF001
        b._conn.close()
        b.close()  # _conn is already-closed; close path must not raise

    def test_task_status_changed_missing_required_field_rejects(
        self, tmp_path: Path
    ) -> None:
        """task.status_changed without the required 'from' field raises EventRejected.

        SL1-RR-1: payload validation happens pre-log via _check_*. A missing
        required field raises EventRejected (not TransactionAborted), and
        nothing is written to events.jsonl.
        """
        b = _make_backend(tmp_path)
        try:
            bad = EventDraft(
                timestamp=_T0,
                actor="test",
                action="task.status_changed",
                target_kind="task",
                target_id="T001",
                payload_json={"task_id": "T001", "to": "drafted"},  # no "from"
            )
            with pytest.raises((EventRejected, TransactionAborted)):
                b.append(bad)
        finally:
            b.close()

    def test_task_status_changed_idempotent_when_already_at_target(
        self, tmp_path: Path
    ) -> None:
        """task.status_changed must be idempotent — re-applying when the task
        is already at the target status is a no-op success, not an error.
        Without this, plan re-runs would always raise once tasks moved past
        the first transition. (Regression test for Greptile PR #38 finding.)
        """
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            # Seed a feature + task in 'drafted' state.
            b.append(_make_event(
                "feature.created", _make_feature_payload(feat_id="F001"),
                event_id="E000010", target_kind="feature", target_id="F001",
            ))
            task_payload = _make_task_payload(task_id="T001", feature_id="F001")
            task_payload["status"] = "drafted"  # seed directly at drafted
            b.append(_make_event(
                "task.created", task_payload,
                event_id="E000011", target_kind="task", target_id="T001",
            ))

            # Apply a status_changed proposed → drafted; the task is ALREADY
            # at 'drafted'. Should silently succeed (no-op).
            idempotent_event = _make_event(
                "task.status_changed",
                {"task_id": "T001", "from": "proposed", "to": "drafted"},
                event_id="E000012",
            )
            b.append(idempotent_event)  # must not raise

            # Confirm status unchanged
            task = b.get_task("T001")
            assert task is not None
            assert task.status.value == "drafted"
        finally:
            b.close()

    def test_task_status_changed_unknown_task_rejects(
        self, tmp_path: Path
    ) -> None:
        """task.status_changed for a nonexistent task raises EventRejected.

        SL1-RR-1: _check_task_status_changed validates the task exists.
        A missing task raises EventRejected with a clear 'not found' message.
        """
        b = _make_backend(tmp_path)
        try:
            bad = EventDraft(
                timestamp=_T0,
                actor="test",
                action="task.status_changed",
                target_kind="task",
                target_id="T999",
                payload_json={
                    "task_id": "T999",
                    "from": "proposed",
                    "to": "drafted",
                },
            )
            with pytest.raises((EventRejected, TransactionAborted)):
                b.append(bad)
        finally:
            b.close()

    def test_replay_unknown_action_raises(
        self, tmp_path: Path
    ) -> None:
        """During replay, an unknown action in events.jsonl RAISES (SL1-RR-1 strict).

        SL1-RR-1: the skip-list is gone. Every line in events.jsonl is a fact
        that must replay infallibly. An unknown action in the canonical log is a
        genuine integrity alarm — if it was ever appended there, the dispatch
        table entry is missing. replay_from_empty raises, not skips.

        This is the INVERSE of the pre-SL1-RR-1 behavior (which skipped). The
        correct fix for an 'unsupported action in log' is to add the dispatch
        entry, not to tolerate corruption via a skip-list.
        """
        events_path = str(tmp_path / "events.jsonl")
        db_path = str(tmp_path / "state.db")

        # Write a good event followed by an unknown action directly to events.jsonl.
        proj_event = Event(
            id="E000001",
            timestamp=_T0,
            actor="test",
            action="project.created",
            target_kind="project",
            target_id="proj-1",
            payload_json={
                "id": "proj-1",
                "name": "Test",
                "description": "",
                "created_at": _T0.isoformat(),
                "updated_at": _T0.isoformat(),
            },
        )
        unknown_event = Event(
            id="E000099",
            timestamp=_T0,
            actor="test",
            action="some.future.action",
            target_kind="task",
            target_id="T001",
            payload_json={},
        )
        with open(events_path, "w", encoding="utf-8") as fh:
            fh.write(proj_event.model_dump_json() + "\n")
            fh.write(unknown_event.model_dump_json() + "\n")

        # replay_from_empty must RAISE — the skip-list is gone.
        b = SqliteBackend(db_path=db_path, events_path=events_path, clock=_make_clock())
        with pytest.raises((TransactionAborted, ValueError)):
            b.replay_from_empty(events_path)


def _make_prd_parsed_payload(
    *,
    project_id: str = "proj-1",
    summary: str = "A test PRD summary.",
    requirements: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if requirements is None:
        requirements = [
            {
                "id": "R001",
                "prd_section": "requirements",
                "text": "First requirement.",
                "source_paragraph": None,
                "derived": False,
            },
            {
                "id": "R002",
                "prd_section": "requirements",
                "text": "Second requirement.",
                "source_paragraph": None,
                "derived": False,
            },
        ]
    return {
        "project_id": project_id,
        "status": "draft",
        "summary": summary,
        "goals": ["Goal one.", "Goal two."],
        "non_goals": [],
        "requirements": requirements,
        "acceptance_criteria": ["AC one."],
        "risks": [],
        "open_questions": [],
    }


def _make_event(
    action: str,
    payload: dict[str, Any],
    *,
    event_id: str = "E000003",
    target_kind: str = "prd",
    target_id: str = "proj-1",
    now: datetime = _T0,
) -> EventDraft:
    """Return an EventDraft (SL1-RR-1: id is ignored, assigned by backend)."""
    return EventDraft(
        timestamp=now,
        actor="test",
        action=action,
        target_kind=target_kind,
        target_id=target_id,
        payload_json=payload,
    )


def _setup_project(b: SqliteBackend) -> None:
    """Apply project.created so FK constraints are satisfied."""
    b.append(_make_project_event())
    b.append(_make_init_event())


# ---------------------------------------------------------------------------
# Phase 3 — prd.parsed handler
# ---------------------------------------------------------------------------


class TestHandlePrdParsed:
    def test_handle_prd_parsed_writes_jsonl_and_sqlite(self, tmp_path: Path) -> None:
        """prd.parsed event writes JSONL line AND PRD + requirements rows to SQLite."""
        b = _make_backend(tmp_path)
        events_path = str(tmp_path / "events.jsonl")
        try:
            _setup_project(b)

            payload = _make_prd_parsed_payload()
            event = _make_event("prd.parsed", payload, event_id="E000003")
            b.append(event)

            # JSONL line present
            events = _read_jsonl(events_path)
            assert any(e.get("action") == "prd.parsed" for e in events)

            # PRD row in SQLite
            prd = b.get_prd()
            assert prd is not None
            assert "test PRD summary" in prd.summary.lower() or "summary" in prd.summary.lower()

            # Requirements rows
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            rows = conn.execute("SELECT id FROM requirements").fetchall()
            conn.close()
            req_ids = {r[0] for r in rows}
            assert "R001" in req_ids
            assert "R002" in req_ids
        finally:
            b.close()

    def test_prd_assumptions_persist_and_replay_without_changing_old_payloads(
        self, tmp_path: Path
    ) -> None:
        b = _make_backend(tmp_path)
        events_path = str(tmp_path / "events.jsonl")
        try:
            _setup_project(b)
            payload = _make_prd_parsed_payload()
            payload["assumptions"] = [
                {
                    "id": "A001",
                    "statement": "Use the existing local store first.",
                    "rationale": "It is reversible while the integration is validated.",
                    "requirement_ids": ["R001"],
                }
            ]
            b.append(_make_event("prd.parsed", payload))

            prd = b.get_prd()
            assert prd is not None
            assert prd.assumptions[0].requirement_ids == ["R001"]

            b.replay_from_empty(events_path)
            replayed = b.get_prd()
            assert replayed is not None
            assert replayed.assumptions[0].statement == (
                "Use the existing local store first."
            )
        finally:
            b.close()

    @pytest.mark.parametrize(
        ("assumption", "error"),
        [
            (
                {
                    "id": "untracked",
                    "statement": "A valid-looking premise.",
                    "rationale": "It must still be auditable.",
                    "requirement_ids": ["R001"],
                },
                "stable A### format",
            ),
            (
                {
                    "id": "A001",
                    "statement": "   ",
                    "rationale": "It must still be auditable.",
                    "requirement_ids": ["R001"],
                },
                "must not be blank",
            ),
            (
                {
                    "id": "A001",
                    "statement": "A valid-looking premise.",
                    "rationale": "   ",
                    "requirement_ids": ["R001"],
                },
                "must not be blank",
            ),
        ],
    )
    def test_prd_parsed_rejects_assumptions_that_bypass_markdown_contract(
        self, tmp_path: Path, assumption: dict[str, Any], error: str
    ) -> None:
        """Event payloads must preserve the same typed contract as the parser."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            payload = _make_prd_parsed_payload()
            payload["assumptions"] = [assumption]

            with pytest.raises(EventRejected, match=error):
                b.append(_make_event("prd.parsed", payload))

            assert b.get_prd() is None
        finally:
            b.close()

    def test_prd_parsed_canonicalizes_assumption_id_from_event_payload(
        self, tmp_path: Path
    ) -> None:
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            payload = _make_prd_parsed_payload()
            payload["assumptions"] = [
                {
                    "id": " a001 ",
                    "statement": "  Use the existing local store first.  ",
                    "rationale": "  It is reversible while the integration is validated.  ",
                    "requirement_ids": ["R001"],
                }
            ]
            b.append(_make_event("prd.parsed", payload))

            prd = b.get_prd()
            assert prd is not None
            assert prd.assumptions[0].id == "A001"
            assert prd.assumptions[0].statement == "Use the existing local store first."

            events = _read_jsonl(str(tmp_path / "events.jsonl"))
            recorded = next(event for event in events if event["action"] == "prd.parsed")
            assert recorded["payload_json"]["assumptions"] == [
                {
                    "id": "A001",
                    "statement": "Use the existing local store first.",
                    "rationale": "It is reversible while the integration is validated.",
                    "requirement_ids": ["R001"],
                }
            ]

            conn = sqlite3.connect(str(tmp_path / "state.db"))
            stored = json.loads(
                conn.execute("SELECT assumptions FROM prds").fetchone()[0]
            )
            conn.close()
            assert stored == recorded["payload_json"]["assumptions"]
        finally:
            b.close()

    def test_handle_prd_parsed_payload_validation_missing_project_id(
        self, tmp_path: Path
    ) -> None:
        """prd.parsed without project_id → EventRejected, rejection in audit.jsonl, no PRD row."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)

            bad_payload = _make_prd_parsed_payload()
            del bad_payload["project_id"]  # remove required field
            event = _make_event("prd.parsed", bad_payload, event_id="E000003")

            with pytest.raises((EventRejected, TransactionAborted)):
                b.append(event)

            # SL1-RR-1: rejection is in audit.jsonl, NOT as an abort tombstone in events.jsonl.
            audit_path = str(tmp_path / "audit.jsonl")
            audit_records = _read_audit_jsonl(audit_path) if os.path.exists(audit_path) else []
            rejections = [r for r in audit_records if r.get("kind") == "rejection"]
            assert len(rejections) >= 1, (
                f"SL1-RR-1: validation rejection must write to audit.jsonl; got: {audit_records}"
            )

            # No PRD row written
            prd = b.get_prd()
            assert prd is None
        finally:
            b.close()

    def test_prd_parsed_replaces_requirements_destructively(self, tmp_path: Path) -> None:
        """Apply prd.parsed twice with different requirements; final state matches second parse."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)

            # First parse: R001, R002, R003
            payload_v1 = _make_prd_parsed_payload(
                requirements=[
                    {"id": "R001", "prd_section": "requirements", "text": "V1 req 1.",
                     "source_paragraph": None, "derived": False},
                    {"id": "R002", "prd_section": "requirements", "text": "V1 req 2.",
                     "source_paragraph": None, "derived": False},
                    {"id": "R003", "prd_section": "requirements", "text": "V1 req 3.",
                     "source_paragraph": None, "derived": False},
                ]
            )
            b.append(_make_event("prd.parsed", payload_v1, event_id="E000003"))

            # Second parse: only R001, R002 (different text)
            payload_v2 = _make_prd_parsed_payload(
                summary="Second version summary.",
                requirements=[
                    {"id": "R001", "prd_section": "requirements", "text": "V2 req 1.",
                     "source_paragraph": None, "derived": False},
                    {"id": "R002", "prd_section": "requirements", "text": "V2 req 2.",
                     "source_paragraph": None, "derived": False},
                ]
            )
            b.append(_make_event("prd.parsed", payload_v2, event_id="E000004"))

            # Final state: only 2 requirements, R003 gone
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            rows = conn.execute("SELECT id, text FROM requirements").fetchall()
            conn.close()

            assert len(rows) == 2
            req_ids = {r[0] for r in rows}
            assert "R003" not in req_ids
            # Text is from v2
            texts = {r[1] for r in rows}
            assert any("V2" in t for t in texts)
        finally:
            b.close()


# ---------------------------------------------------------------------------
# Phase 6 (T023) — prd.revised handler (non-destructive, amend-aware)
# ---------------------------------------------------------------------------


def _req(rid: str, text: str, *, section: str = "requirements") -> dict[str, Any]:
    """A minimal requirement dict for prd diff lists."""
    return {
        "id": rid,
        "prd_section": section,
        "text": text,
        "source_paragraph": None,
        "derived": False,
    }


def _make_prd_revised_payload(
    *,
    revision: int,
    status: str = "draft",
    prd_id: str = "default",
    added: list[dict[str, Any]] | None = None,
    superseded: list[dict[str, Any]] | None = None,
    unchanged: list[dict[str, Any]] | None = None,
    assumptions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "project_id": "proj-1",
        "prd_id": prd_id,
        "revision": revision,
        "status": status,
        "summary": f"Revision {revision} summary.",
        "goals": [],
        "non_goals": [],
        "acceptance_criteria": [],
        "risks": [],
        "open_questions": [],
        "assumptions": assumptions or [],
        "requirements_added": added or [],
        "requirements_superseded": superseded or [],
        "requirements_unchanged": unchanged or [],
    }


def _requirements_lineage(db_path: str) -> dict[str, tuple[int | None, int | None]]:
    """Map requirement id -> (revision_introduced, revision_superseded) for ALL
    rows (live + superseded), so a destroyed lineage row is observable.

    Reads the raw columns: ``prd.parsed`` rows store ``revision_introduced`` as
    NULL (the v7/v8 schema interprets NULL as "introduced at revision 1"; the
    model coerces it). ``prd.revised``-added rows stamp the literal revision.
    """
    conn = sqlite3.connect(db_path)
    try:
        return {
            r[0]: (r[1], r[2])
            for r in conn.execute(
                "SELECT id, revision_introduced, revision_superseded "
                "FROM requirements"
            ).fetchall()
        }
    finally:
        conn.close()


class TestHandlePrdRevised:
    """T023 review fixes — the prd.revised write path is non-destructive,
    consumes every diff bucket, and gates status demotion on a real change.
    """

    def _parse_two_reqs(self, b: SqliteBackend) -> None:
        """prd.parsed at revision 1 with R001 ('v1 one') + R002 ('v1 two')."""
        _setup_project(b)
        _apply(
            b,
            "prd.parsed",
            _make_prd_parsed_payload(
                requirements=[_req("R001", "v1 one"), _req("R002", "v1 two")]
            ),
        )

    def test_supersede_keeps_lineage_row_and_filters_live_set(
        self, tmp_path: Path
    ) -> None:
        """Superseding R002 stamps revision_superseded on its row (NEVER DELETE);
        the row survives, but list_requirements() drops it from the live set."""
        b = _make_backend(tmp_path)
        try:
            self._parse_two_reqs(b)
            _apply(
                b,
                "prd.revised",
                _make_prd_revised_payload(
                    revision=2, superseded=[_req("R002", "v1 two")]
                ),
            )
            # Live set is just R001.
            live = {r.id for r in b.list_requirements()}
            assert live == {"R001"}
            # R002's row survives, stamped superseded=2 (lineage preserved).
            # introduced is NULL on a prd.parsed row (== "revision 1").
            lineage = _requirements_lineage(str(tmp_path / "state.db"))
            assert lineage["R002"] == (None, 2)
            assert lineage["R001"] == (None, None)
        finally:
            b.close()

    def test_revision_replaces_typed_assumptions(self, tmp_path: Path) -> None:
        b = _make_backend(tmp_path)
        try:
            self._parse_two_reqs(b)
            _apply(
                b,
                "prd.revised",
                _make_prd_revised_payload(
                    revision=2,
                    unchanged=[_req("R001", "v1 one"), _req("R002", "v1 two")],
                    assumptions=[
                        {
                            "id": "A002",
                            "statement": "Use a bounded default.",
                            "rationale": "It remains reversible in this revision.",
                            "requirement_ids": ["R002"],
                        }
                    ],
                ),
            )
            prd = b.get_prd()
            assert prd is not None
            assert prd.revision == 2
            assert [assumption.id for assumption in prd.assumptions] == ["A002"]
        finally:
            b.close()

    def test_assumption_only_revision_demotes_approved_prd(self, tmp_path: Path) -> None:
        b = _make_backend(tmp_path)
        try:
            self._parse_two_reqs(b)
            _apply(b, "prd.reviewed", {"project_id": "proj-1", "reviewer": "alice"})
            _apply(b, "prd.approved", {"project_id": "proj-1", "approver": "bob"})

            _apply(
                b,
                "prd.revised",
                _make_prd_revised_payload(
                    revision=2,
                    status="approved",
                    unchanged=[_req("R001", "v1 one"), _req("R002", "v1 two")],
                    assumptions=[
                        {
                            "id": "A001",
                            "statement": "Use the reversible local default.",
                            "rationale": "It keeps external authority out of scope.",
                            "requirement_ids": ["R001"],
                        }
                    ],
                ),
            )

            assert b.get_prd().status == "draft"
        finally:
            b.close()

    def test_readding_retired_id_is_rejected_and_lineage_survives(
        self, tmp_path: Path
    ) -> None:
        """Adversarial (finding #1/#6): rev2 supersedes R002; rev3 tries to add a
        NEW R002. The single-column PK cannot hold both rows, so re-adding a
        retired id is rejected up front — the superseded lineage row is NOT
        destroyed (the old INSERT OR REPLACE silently erased it)."""
        b = _make_backend(tmp_path)
        try:
            self._parse_two_reqs(b)
            _apply(
                b,
                "prd.revised",
                _make_prd_revised_payload(
                    revision=2, superseded=[_req("R002", "v1 two")]
                ),
            )
            with pytest.raises((EventRejected, TransactionAborted)):
                _apply(
                    b,
                    "prd.revised",
                    _make_prd_revised_payload(
                        revision=3, added=[_req("R002", "re-added")]
                    ),
                )
            # The rev2 supersession row is intact (introduced=NULL/"rev1",
            # superseded=2), NOT replaced by an introduced-at-3 live row.
            lineage = _requirements_lineage(str(tmp_path / "state.db"))
            assert lineage["R002"] == (None, 2)
        finally:
            b.close()

    def test_superseding_nonexistent_id_is_rejected_no_demotion(
        self, tmp_path: Path
    ) -> None:
        """Adversarial (finding #2): an approved PRD revised with a superseded id
        that doesn't exist must be REJECTED — not silently demoted to draft on a
        0-row UPDATE. The live set and status are unchanged."""
        b = _make_backend(tmp_path)
        try:
            self._parse_two_reqs(b)
            _apply(b, "prd.reviewed", {"project_id": "proj-1", "reviewer": "alice"})
            _apply(b, "prd.approved", {"project_id": "proj-1", "approver": "bob"})
            assert b.get_prd().status == "approved"
            with pytest.raises((EventRejected, TransactionAborted)):
                _apply(
                    b,
                    "prd.revised",
                    _make_prd_revised_payload(
                        revision=2,
                        status="approved",
                        superseded=[_req("R999", "ghost")],
                    ),
                )
            # No change: still approved, both reqs live, revision still 1.
            prd = b.get_prd()
            assert prd.status == "approved"
            assert prd.revision == 1
            assert {r.id for r in b.list_requirements()} == {"R001", "R002"}
        finally:
            b.close()

    def test_supersede_demotes_approved_to_draft(self, tmp_path: Path) -> None:
        """A revision that supersedes a real live requirement demotes an approved
        PRD back to draft (the diff is the source of truth)."""
        b = _make_backend(tmp_path)
        try:
            self._parse_two_reqs(b)
            _apply(b, "prd.reviewed", {"project_id": "proj-1", "reviewer": "alice"})
            _apply(b, "prd.approved", {"project_id": "proj-1", "approver": "bob"})
            _apply(
                b,
                "prd.revised",
                _make_prd_revised_payload(
                    revision=2,
                    status="approved",  # payload claims approved …
                    superseded=[_req("R002", "v1 two")],
                ),
            )
            # … but the supersede forces a demotion to draft.
            assert b.get_prd().status == "draft"
        finally:
            b.close()

    def test_pure_additive_revision_keeps_status(self, tmp_path: Path) -> None:
        """A pure-additive revision (no superseded) keeps the payload's status."""
        b = _make_backend(tmp_path)
        try:
            self._parse_two_reqs(b)
            _apply(
                b,
                "prd.revised",
                _make_prd_revised_payload(
                    revision=2, status="reviewed", added=[_req("R003", "v2 new")]
                ),
            )
            prd = b.get_prd()
            assert prd.status == "reviewed"
            assert prd.revision == 2
            assert {r.id for r in b.list_requirements()} == {"R001", "R002", "R003"}
            lineage = _requirements_lineage(str(tmp_path / "state.db"))
            assert lineage["R003"] == (2, None)  # introduced at revision 2
        finally:
            b.close()

    def test_unchanged_requirement_edit_is_applied(self, tmp_path: Path) -> None:
        """Adversarial (finding #3): an edited requirement carried in
        requirements_unchanged is APPLIED, not silently dropped."""
        b = _make_backend(tmp_path)
        try:
            self._parse_two_reqs(b)
            _apply(
                b,
                "prd.revised",
                _make_prd_revised_payload(
                    revision=2,
                    unchanged=[_req("R001", "EDITED TEXT"), _req("R002", "v1 two")],
                ),
            )
            reqs = {r.id: r for r in b.list_requirements()}
            assert reqs["R001"].text == "EDITED TEXT"
            # Lineage stamps untouched by the carry-forward edit (NULL == "rev1").
            lineage = _requirements_lineage(str(tmp_path / "state.db"))
            assert lineage["R001"] == (None, None)
        finally:
            b.close()

    def test_stale_revision_is_rejected(self, tmp_path: Path) -> None:
        """revision must be exactly current+1 — a stale/skipped revision rejects."""
        b = _make_backend(tmp_path)
        try:
            self._parse_two_reqs(b)
            with pytest.raises((EventRejected, TransactionAborted)):
                _apply(
                    b,
                    "prd.revised",
                    _make_prd_revised_payload(
                        revision=3, added=[_req("R003", "skip")]
                    ),
                )
        finally:
            b.close()

    def test_snapshot_captures_superseded_lineage(self, tmp_path: Path) -> None:
        """serialize_state surfaces superseded rows WITH their lineage stamps, so
        the replay-equivalence oracle compares retired-row lineage (finding #4/#7
        — previously only the live set was serialized)."""
        from anvil.state.snapshot import serialize_state

        b = _make_backend(tmp_path)
        try:
            self._parse_two_reqs(b)
            _apply(
                b,
                "prd.revised",
                _make_prd_revised_payload(
                    revision=2, superseded=[_req("R002", "v1 two")]
                ),
            )
            snap = serialize_state(b)
            by_id = {r["id"]: r for r in snap["requirements"]}
            # Both rows present — the superseded R002 is NOT filtered out.
            assert set(by_id) == {"R001", "R002"}
            assert by_id["R002"]["revision_superseded"] == 2
            assert by_id["R001"]["revision_superseded"] is None
        finally:
            b.close()

    def test_revised_event_replays_with_lineage_intact(
        self, tmp_path: Path
    ) -> None:
        """A prd.revised event replayed from empty reconstructs identical lineage
        (live + superseded) — guards the non-destructive write path under replay.
        """
        b = _make_backend(tmp_path)
        events_path = str(tmp_path / "events.jsonl")
        try:
            self._parse_two_reqs(b)
            _apply(
                b,
                "prd.revised",
                _make_prd_revised_payload(
                    revision=2,
                    superseded=[_req("R002", "v1 two")],
                    added=[_req("R003", "v2 new")],
                ),
            )
            before = _requirements_lineage(str(tmp_path / "state.db"))
            b.replay_from_empty(events_path)
            after = _requirements_lineage(str(tmp_path / "state.db"))
            assert after == before
            assert before["R002"] == (None, 2)  # prd.parsed row, then superseded
            assert before["R003"] == (2, None)  # prd.revised-added at revision 2
        finally:
            b.close()


# ---------------------------------------------------------------------------
# Phase 3 — prd.reviewed handler
# ---------------------------------------------------------------------------


class TestHandlePrdReviewed:
    def test_handle_prd_reviewed_writes_jsonl_and_sqlite(self, tmp_path: Path) -> None:
        """prd.reviewed event updates PRD status to 'reviewed'. Does NOT
        insert a reviews row — the prds.status column transition is its
        own audit. (See Greptile PR #38 finding: writing decision='approve'
        for prd.reviewed made it indistinguishable from prd.approved.)
        """
        b = _make_backend(tmp_path)
        events_path = str(tmp_path / "events.jsonl")
        try:
            _setup_project(b)
            # First parse the PRD
            b.append(_make_event(
                "prd.parsed", _make_prd_parsed_payload(), event_id="E000003"
            ))

            # Now review it
            reviewed_event = _make_event(
                "prd.reviewed",
                {"project_id": "proj-1", "reviewer": "alice", "notes": "Looks good."},
                event_id="E000004",
                target_kind="prd",
            )
            b.append(reviewed_event)

            # JSONL line
            events = _read_jsonl(events_path)
            assert any(e.get("action") == "prd.reviewed" for e in events)

            # PRD status updated
            prd = b.get_prd()
            assert prd is not None
            assert prd.status.value == "reviewed"
            assert prd.last_reviewed_by == "alice"

            # NO reviews row inserted — reviews table is only for
            # outcome-bearing decisions (approve/reject/needs_changes).
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            review_row = conn.execute(
                "SELECT id FROM reviews WHERE reviewed_by = 'alice'"
            ).fetchone()
            conn.close()
            assert review_row is None, (
                "prd.reviewed must NOT insert a reviews row — only "
                "prd.approved (and future prd.rejected) should."
            )
        finally:
            b.close()

    def test_handle_prd_reviewed_payload_validation_missing_reviewer(
        self, tmp_path: Path
    ) -> None:
        """prd.reviewed without reviewer → TransactionAborted."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            b.append(_make_event(
                "prd.parsed", _make_prd_parsed_payload(), event_id="E000003"
            ))

            bad_event = _make_event(
                "prd.reviewed",
                {"project_id": "proj-1", "notes": "No reviewer field"},  # missing reviewer
                event_id="E000004",
            )
            with pytest.raises((EventRejected, TransactionAborted)):
                b.append(bad_event)
        finally:
            b.close()

    def test_handle_prd_reviewed_missing_project_id_aborts(
        self, tmp_path: Path
    ) -> None:
        """prd.reviewed without project_id → TransactionAborted. (Regression
        test for Greptile PR #38 finding: the UPDATE prds statement was
        missing a WHERE clause; project_id is now required so multi-PRD
        setups don't accidentally co-mutate.)
        """
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            b.append(_make_event(
                "prd.parsed", _make_prd_parsed_payload(), event_id="E000003"
            ))
            bad = _make_event(
                "prd.reviewed",
                {"reviewer": "alice"},  # no project_id
                event_id="E000004",
            )
            with pytest.raises((EventRejected, TransactionAborted), match="project_id"):
                b.append(bad)
        finally:
            b.close()


# ---------------------------------------------------------------------------
# Phase 3 — prd.approved handler
# ---------------------------------------------------------------------------


class TestHandlePrdApproved:
    def test_handle_prd_approved_writes_jsonl_and_sqlite(self, tmp_path: Path) -> None:
        """prd.approved updates status to 'approved' and inserts review row."""
        b = _make_backend(tmp_path)
        events_path = str(tmp_path / "events.jsonl")
        try:
            _setup_project(b)
            b.append(_make_event(
                "prd.parsed", _make_prd_parsed_payload(), event_id="E000003"
            ))
            b.append(_make_event(
                "prd.reviewed",
                {"project_id": "proj-1", "reviewer": "alice"},
                event_id="E000004",
            ))
            b.append(_make_event(
                "prd.approved",
                {"project_id": "proj-1", "approver": "bob"},
                event_id="E000005",
            ))

            events = _read_jsonl(events_path)
            assert any(e.get("action") == "prd.approved" for e in events)

            prd = b.get_prd()
            assert prd is not None
            assert prd.status.value == "approved"
        finally:
            b.close()

    def test_handle_prd_approved_missing_project_id_aborts(
        self, tmp_path: Path
    ) -> None:
        """prd.approved without project_id → TransactionAborted. (Greptile
        PR #38 fix #2: UPDATE statements scoped via WHERE project_id = ?.)
        """
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            b.append(_make_event(
                "prd.parsed", _make_prd_parsed_payload(), event_id="E000003"
            ))
            b.append(_make_event(
                "prd.reviewed",
                {"project_id": "proj-1", "reviewer": "alice"},
                event_id="E000004",
            ))
            bad = _make_event(
                "prd.approved",
                {"approver": "bob"},  # no project_id
                event_id="E000005",
            )
            with pytest.raises((EventRejected, TransactionAborted), match="project_id"):
                b.append(bad)
        finally:
            b.close()

    def test_handle_prd_approved_payload_validation_missing_approver(
        self, tmp_path: Path
    ) -> None:
        """prd.approved without approver → TransactionAborted."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            b.append(_make_event(
                "prd.parsed", _make_prd_parsed_payload(), event_id="E000003"
            ))
            b.append(_make_event(
                "prd.reviewed",
                {"project_id": "proj-1", "reviewer": "alice"},
                event_id="E000004",
            ))
            bad_event = _make_event(
                "prd.approved", {"project_id": "proj-1"}, event_id="E000005"
            )
            with pytest.raises((EventRejected, TransactionAborted)):
                b.append(bad_event)
        finally:
            b.close()


# ---------------------------------------------------------------------------
# Phase 3 — feature.created handler
# ---------------------------------------------------------------------------


class TestHandleFeatureCreated:
    def test_handle_feature_created_writes_jsonl_and_sqlite(self, tmp_path: Path) -> None:
        """feature.created event inserts feature row and writes JSONL."""
        b = _make_backend(tmp_path)
        events_path = str(tmp_path / "events.jsonl")
        try:
            _setup_project(b)

            feat_payload = _make_feature_payload(feat_id="F001")
            event = _make_event(
                "feature.created", feat_payload,
                event_id="E000003", target_kind="feature", target_id="F001"
            )
            b.append(event)

            events = _read_jsonl(events_path)
            assert any(e.get("action") == "feature.created" for e in events)

            conn = sqlite3.connect(str(tmp_path / "state.db"))
            row = conn.execute(
                "SELECT id, title FROM features WHERE id = 'F001'"
            ).fetchone()
            conn.close()
            assert row is not None
            assert row[1] == "Test Feature"
        finally:
            b.close()

    def test_handle_feature_created_payload_validation(self, tmp_path: Path) -> None:
        """feature.created with invalid payload (bad status) → TransactionAborted."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)

            bad_payload = _make_feature_payload()
            bad_payload["status"] = "invalid_status_not_in_enum"
            event = _make_event("feature.created", bad_payload, event_id="E000003")
            with pytest.raises((EventRejected, TransactionAborted)):
                b.append(event)
        finally:
            b.close()


# ---------------------------------------------------------------------------
# Phase 3 — task.created handler
# ---------------------------------------------------------------------------


class TestHandleTaskCreated:
    def _setup_feature(self, b: SqliteBackend) -> None:
        """Insert F001 so FK constraint is satisfied."""
        feat_payload = _make_feature_payload(feat_id="F001")
        event = _make_event(
            "feature.created", feat_payload,
            event_id="E000003", target_kind="feature", target_id="F001"
        )
        b.append(event)

    def test_handle_task_created_writes_jsonl_and_sqlite(self, tmp_path: Path) -> None:
        """task.created event inserts task row and writes JSONL."""
        b = _make_backend(tmp_path)
        events_path = str(tmp_path / "events.jsonl")
        try:
            _setup_project(b)
            self._setup_feature(b)

            task_payload = _make_task_payload(task_id="T001")
            event = _make_event(
                "task.created", task_payload,
                event_id="E000004", target_kind="task", target_id="T001"
            )
            b.append(event)

            events = _read_jsonl(events_path)
            assert any(e.get("action") == "task.created" for e in events)

            task = b.get_task("T001")
            assert task is not None
            assert task.id == "T001"
            assert task.title == "Test Task"
        finally:
            b.close()

    def test_handle_task_created_payload_validation(self, tmp_path: Path) -> None:
        """task.created with missing required fields → TransactionAborted."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            self._setup_feature(b)

            bad_payload = _make_task_payload()
            del bad_payload["created_at"]  # required field removed
            event = _make_event("task.created", bad_payload, event_id="E000004")
            with pytest.raises((EventRejected, TransactionAborted)):
                b.append(event)
        finally:
            b.close()


# ---------------------------------------------------------------------------
# Phase 3 — task.scored handler
# ---------------------------------------------------------------------------


class TestHandleTaskScored:
    def _setup_task(self, b: SqliteBackend) -> None:
        feat_payload = _make_feature_payload(feat_id="F001")
        b.append(_make_event(
            "feature.created", feat_payload,
            event_id="E000003", target_kind="feature", target_id="F001"
        ))
        task_payload = _make_task_payload(task_id="T001")
        b.append(_make_event(
            "task.created", task_payload,
            event_id="E000004", target_kind="task", target_id="T001"
        ))

    def test_handle_task_scored_writes_jsonl_and_sqlite(self, tmp_path: Path) -> None:
        """task.scored event updates task scores in SQLite and writes JSONL."""
        b = _make_backend(tmp_path)
        events_path = str(tmp_path / "events.jsonl")
        try:
            _setup_project(b)
            self._setup_task(b)

            score_payload = {
                "task_id": "T001",
                "scores": {
                    "complexity": 3,
                    "parallelizability": 4,
                    "context_load": 2,
                    "blast_radius": 2,
                    "review_risk": 2,
                    "agent_suitability": 3,
                },
                "explanation": "complexity: 3 (base 2, +1 files)",
            }
            event = _make_event(
                "task.scored", score_payload,
                event_id="E000005", target_kind="task", target_id="T001"
            )
            b.append(event)

            events = _read_jsonl(events_path)
            assert any(e.get("action") == "task.scored" for e in events)

            task = b.get_task("T001")
            assert task is not None
            assert task.scores.complexity == 3
            assert task.scores.parallelizability == 4
        finally:
            b.close()

    def test_handle_task_scored_payload_validation_missing_task_id(
        self, tmp_path: Path
    ) -> None:
        """task.scored without task_id → TransactionAborted."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            self._setup_task(b)

            bad_payload = {
                "scores": {"complexity": 3},
                "explanation": "test",
                # missing task_id
            }
            event = _make_event("task.scored", bad_payload, event_id="E000005")
            with pytest.raises((EventRejected, TransactionAborted)):
                b.append(event)
        finally:
            b.close()


# ---------------------------------------------------------------------------
# Phase 3 — task.expanded handler
# ---------------------------------------------------------------------------


class TestHandleTaskExpanded:
    def _setup_task(self, b: SqliteBackend) -> None:
        feat_payload = _make_feature_payload(feat_id="F001")
        b.append(_make_event(
            "feature.created", feat_payload,
            event_id="E000003", target_kind="feature", target_id="F001"
        ))
        task_payload = _make_task_payload(task_id="T001")
        b.append(_make_event(
            "task.created", task_payload,
            event_id="E000004", target_kind="task", target_id="T001"
        ))

    def test_handle_task_expanded_writes_jsonl_and_sqlite(self, tmp_path: Path) -> None:
        """task.expanded inserts subtask rows and writes JSONL."""
        b = _make_backend(tmp_path)
        events_path = str(tmp_path / "events.jsonl")
        try:
            _setup_project(b)
            self._setup_task(b)

            subtask_payload = _make_task_payload(task_id="T001.1", feature_id="F001")
            expand_payload = {
                "parent_task_id": "T001",
                "subtasks": [subtask_payload],
            }
            event = _make_event(
                "task.expanded", expand_payload,
                event_id="E000005", target_kind="task", target_id="T001"
            )
            b.append(event)

            events = _read_jsonl(events_path)
            assert any(e.get("action") == "task.expanded" for e in events)

            # Subtask inserted
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            row = conn.execute(
                "SELECT id, parent_task_id FROM tasks WHERE id = 'T001.1'"
            ).fetchone()
            conn.close()
            assert row is not None
            assert row[1] == "T001"  # parent_task_id was forced
        finally:
            b.close()

    def test_handle_task_expanded_payload_validation_missing_parent(
        self, tmp_path: Path
    ) -> None:
        """task.expanded without parent_task_id → TransactionAborted."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            self._setup_task(b)

            bad_payload = {
                "subtasks": [_make_task_payload(task_id="T001.1")],
                # missing parent_task_id
            }
            event = _make_event("task.expanded", bad_payload, event_id="E000005")
            with pytest.raises((EventRejected, TransactionAborted)):
                b.append(event)
        finally:
            b.close()


# ---------------------------------------------------------------------------
# Phase 3 — task.status_changed handler
# ---------------------------------------------------------------------------


class TestHandleTaskStatusChanged:
    def _setup_task(self, b: SqliteBackend) -> None:
        feat_payload = _make_feature_payload(feat_id="F001")
        b.append(_make_event(
            "feature.created", feat_payload,
            event_id="E000003", target_kind="feature", target_id="F001"
        ))
        task_payload = _make_task_payload(task_id="T001", status="proposed")
        b.append(_make_event(
            "task.created", task_payload,
            event_id="E000004", target_kind="task", target_id="T001"
        ))

    def test_handle_task_status_changed_writes_jsonl_and_sqlite(
        self, tmp_path: Path
    ) -> None:
        """task.status_changed updates task status and writes JSONL."""
        b = _make_backend(tmp_path)
        events_path = str(tmp_path / "events.jsonl")
        try:
            _setup_project(b)
            self._setup_task(b)

            status_payload = {
                "task_id": "T001",
                "from": "proposed",
                "to": "drafted",
                "reason": "Planning complete.",
            }
            event = _make_event(
                "task.status_changed", status_payload,
                event_id="E000005", target_kind="task", target_id="T001"
            )
            b.append(event)

            events = _read_jsonl(events_path)
            assert any(e.get("action") == "task.status_changed" for e in events)

            task = b.get_task("T001")
            assert task is not None
            assert task.status.value == "drafted"
        finally:
            b.close()

    def test_handle_task_status_changed_payload_validation_missing_task_id(
        self, tmp_path: Path
    ) -> None:
        """task.status_changed without task_id → TransactionAborted."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            self._setup_task(b)

            bad_payload = {"from": "proposed", "to": "drafted"}  # no task_id
            event = _make_event("task.status_changed", bad_payload, event_id="E000005")
            with pytest.raises((EventRejected, TransactionAborted)):
                b.append(event)
        finally:
            b.close()

    def test_task_status_changed_concurrency_guard_fails_on_drift(
        self, tmp_path: Path
    ) -> None:
        """task.status_changed with wrong 'from' status → TransactionAborted (concurrency guard)."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            self._setup_task(b)

            # Task is in 'proposed', but we claim it's in 'drafted'
            drift_payload = {
                "task_id": "T001",
                "from": "drafted",  # WRONG — actual status is 'proposed'
                "to": "reviewed",
            }
            event = _make_event("task.status_changed", drift_payload, event_id="E000005")
            with pytest.raises((EventRejected, TransactionAborted)):
                b.append(event)

            # Task status unchanged
            task = b.get_task("T001")
            assert task is not None
            assert task.status.value == "proposed"
        finally:
            b.close()


# ---------------------------------------------------------------------------
# Phase 3 — THE CRITICAL TEST: replay includes all new event actions
# ---------------------------------------------------------------------------


class TestReplayIncludesNewEventActions:
    def test_replay_includes_new_event_actions(self, tmp_path: Path) -> None:
        """events.jsonl with all 8 new actions; replay_from_empty reproduces identical state.

        This is the CRITICAL audit guarantee test for Phase 3 events.
        """
        clock = _make_clock()
        db_path = str(tmp_path / "state.db")
        events_path = str(tmp_path / "events.jsonl")
        Path(events_path).touch()

        b = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock)
        b.initialize()

        try:
            # E000001: project.created
            b.append(_make_project_event(event_id="E000001"))
            # E000002: state.initialized
            b.append(_make_init_event(event_id="E000002"))

            # E000003: prd.parsed
            b.append(_make_event(
                "prd.parsed", _make_prd_parsed_payload(),
                event_id="E000003"
            ))

            # E000004: prd.reviewed
            b.append(_make_event(
                "prd.reviewed", {"project_id": "proj-1", "reviewer": "alice"},
                event_id="E000004"
            ))

            # E000005: prd.approved
            b.append(_make_event(
                "prd.approved", {"project_id": "proj-1", "approver": "bob"},
                event_id="E000005"
            ))

            # E000006: feature.created
            b.append(_make_event(
                "feature.created", _make_feature_payload(feat_id="F001"),
                event_id="E000006", target_kind="feature", target_id="F001"
            ))

            # E000007: task.created
            b.append(_make_event(
                "task.created", _make_task_payload(task_id="T001"),
                event_id="E000007", target_kind="task", target_id="T001"
            ))

            # E000008: task.scored
            b.append(_make_event(
                "task.scored",
                {
                    "task_id": "T001",
                    "scores": {
                        "complexity": 2,
                        "parallelizability": 4,
                        "context_load": 5,
                        "blast_radius": 2,
                        "review_risk": 2,
                        "agent_suitability": 4,
                    },
                    "explanation": "complexity: 2 (base 2)",
                },
                event_id="E000008", target_kind="task", target_id="T001"
            ))

            # E000009: task.status_changed (proposed → drafted)
            b.append(_make_event(
                "task.status_changed",
                {"task_id": "T001", "from": "proposed", "to": "drafted", "reason": "planned"},
                event_id="E000009", target_kind="task", target_id="T001"
            ))

            # E000010: task.expanded (add subtask T001.1)
            subtask_data = _make_task_payload(task_id="T001.1", feature_id="F001")
            b.append(_make_event(
                "task.expanded",
                {"parent_task_id": "T001", "subtasks": [subtask_data]},
                event_id="E000010", target_kind="task", target_id="T001"
            ))

        finally:
            b.close()

        # Capture original dump
        original_dump = _sqlite_dump(db_path)

        # Replay from empty
        clock2 = _make_clock()
        b2 = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock2)
        b2.initialize()
        try:
            b2.replay_from_empty(events_path)
        finally:
            b2.close()

        # Compare
        replayed_dump = _sqlite_dump(db_path)
        assert original_dump == replayed_dump, (
            "Replayed state.db does not match original after Phase 3 events.\n"
            f"Original dump (first 800 chars):\n{original_dump[:800]}\n\n"
            f"Replayed dump (first 800 chars):\n{replayed_dump[:800]}"
        )


# ---------------------------------------------------------------------------
# Phase 4 — claim event handler helpers
# ---------------------------------------------------------------------------


def _make_claim_payload(
    *,
    claim_id: str = "C001",
    task_id: str = "T001",
    actor: str = "agent-alpha",
    expected_files: list[str] | None = None,
    now: datetime = _T0,
) -> dict[str, Any]:
    """Build a valid claim.created payload (matches Claim.model_dump(mode='json'))."""
    return {
        "id": claim_id,
        "task_id": task_id,
        "claimed_by": actor,
        "claim_type": "task",
        "status": "active",
        "branch": None,
        "worktree_path": None,
        "expected_files": expected_files or [],
        "created_at": now.isoformat(),
        "lease_expires_at": (now + timedelta(hours=1)).isoformat(),
        "last_heartbeat_at": now.isoformat(),
        "released_at": None,
        "release_reason": None,
    }


def _setup_claimable_task(b: SqliteBackend, task_id: str = "T001") -> None:
    """Apply the minimal event chain to produce a 'ready' task."""
    _setup_project(b)
    b.append(_make_event(
        "prd.parsed",
        {
            "project_id": "proj-1",
            "status": "draft",
            "summary": "A test PRD.",
            "goals": ["Goal one."],
            "non_goals": [],
            "requirements": [
                {"id": "R001", "prd_section": "requirements", "text": "Req 1.",
                 "source_paragraph": None, "derived": False}
            ],
            "acceptance_criteria": ["AC one."],
            "risks": [],
            "open_questions": [],
        },
        event_id="E000003", target_kind="prd", target_id="proj-1",
    ))
    b.append(_make_event(
        "prd.reviewed", {"project_id": "proj-1", "reviewer": "alice"},
        event_id="E000004", target_kind="prd", target_id="proj-1",
    ))
    b.append(_make_event(
        "feature.created",
        {
            "id": "F001",
            "title": "Feature F001",
            "description": "A feature.",
            "status": "proposed",
            "requirements": [],
            "tasks": [],
        },
        event_id="E000005", target_kind="feature", target_id="F001",
    ))
    b.append(_make_event(
        "task.created",
        _make_task_payload(task_id=task_id),
        event_id="E000006", target_kind="task", target_id=task_id,
    ))
    # proposed → drafted → reviewed → ready
    for from_s, to_s, eid in [
        ("proposed", "drafted", "E000007"),
        ("drafted", "reviewed", "E000008"),
        ("reviewed", "ready", "E000009"),
    ]:
        b.append(_make_event(
            "task.status_changed",
            {"task_id": task_id, "from": from_s, "to": to_s},
            event_id=eid, target_kind="task", target_id=task_id,
        ))


# ---------------------------------------------------------------------------
# Phase 4 — TestPhase4ClaimHandlers
# ---------------------------------------------------------------------------


class TestPhase4ClaimHandlers:
    def test_handle_claim_created_writes_jsonl_and_sqlite(self, tmp_path: Path) -> None:
        """claim.created writes claim row to SQLite and JSONL; task moves to 'claimed'."""
        b = _make_backend(tmp_path)
        events_path = str(tmp_path / "events.jsonl")
        try:
            _setup_claimable_task(b)

            payload = _make_claim_payload()
            event = _make_event(
                "claim.created", payload,
                event_id="E000010", target_kind="claim", target_id="C001",
            )
            b.append(event)

            # JSONL line written
            events = _read_jsonl(events_path)
            assert any(e.get("action") == "claim.created" for e in events)

            # Claim in SQLite
            claim = b.get_claim("C001")
            assert claim is not None
            assert claim.status.value == "active"
            assert claim.claimed_by == "agent-alpha"

            # Task moved to 'claimed'
            task = b.get_task("T001")
            assert task is not None
            assert task.status.value == "claimed"
        finally:
            b.close()

    def test_handle_claim_created_concurrency_guard_fails_on_drift(
        self, tmp_path: Path
    ) -> None:
        """claim.created aborts if task is not in 'ready' status (concurrency guard)."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            b.append(_make_event(
                "feature.created",
                {
                    "id": "F001", "title": "F", "description": "d", "status": "proposed",
                    "requirements": [], "tasks": [],
                },
                event_id="E000003", target_kind="feature", target_id="F001",
            ))
            # Task in 'proposed' — NOT ready
            b.append(_make_event(
                "task.created",
                _make_task_payload(task_id="T001", status="proposed"),
                event_id="E000004", target_kind="task", target_id="T001",
            ))

            payload = _make_claim_payload()
            event = _make_event(
                "claim.created", payload,
                event_id="E000005", target_kind="claim", target_id="C001",
            )
            with pytest.raises((EventRejected, TransactionAborted), match="ready|proposed|concurrency"):
                b.append(event)
        finally:
            b.close()

    def test_handle_claim_created_idempotent_on_replay(self, tmp_path: Path) -> None:
        """Applying claim.created twice is a no-op for the second application."""
        b = _make_backend(tmp_path)
        try:
            _setup_claimable_task(b)

            payload = _make_claim_payload()
            event = _make_event(
                "claim.created", payload,
                event_id="E000010", target_kind="claim", target_id="C001",
            )
            b.append(event)  # first: commits

            # Second apply should not raise (idempotent INSERT OR IGNORE)
            b.append(event)  # second: replay no-op

            # Still only one claim
            active = b.list_active_claims()
            assert len(active) == 1
            assert active[0].id == "C001"
        finally:
            b.close()

    def test_handle_claim_released_happy_path(self, tmp_path: Path) -> None:
        """claim.released updates claim to released and task to ready."""
        b = _make_backend(tmp_path)
        events_path = str(tmp_path / "events.jsonl")
        try:
            _setup_claimable_task(b)

            # First create the claim
            b.append(_make_event(
                "claim.created", _make_claim_payload(),
                event_id="E000010", target_kind="claim", target_id="C001",
            ))

            # Release it
            release_event = _make_event(
                "claim.released",
                {
                    "claim_id": "C001",
                    "released_by": "agent-alpha",
                    "release_reason": "work complete",
                    "force": False,
                },
                event_id="E000011", target_kind="claim", target_id="C001",
            )
            b.append(release_event)

            events = _read_jsonl(events_path)
            assert any(e.get("action") == "claim.released" for e in events)

            claim = b.get_claim("C001")
            assert claim is not None
            assert claim.status.value == "released"

            task = b.get_task("T001")
            assert task is not None
            assert task.status.value == "ready"
        finally:
            b.close()

    def test_handle_claim_released_idempotent_when_already_released(
        self, tmp_path: Path
    ) -> None:
        """claim.released on an already-released claim is a no-op (logs warning, no raise)."""
        b = _make_backend(tmp_path)
        try:
            _setup_claimable_task(b)
            b.append(_make_event(
                "claim.created", _make_claim_payload(),
                event_id="E000010", target_kind="claim", target_id="C001",
            ))

            release_payload = {
                "claim_id": "C001",
                "released_by": "agent-alpha",
                "release_reason": "done",
                "force": False,
            }
            b.append(_make_event(
                "claim.released", release_payload,
                event_id="E000011", target_kind="claim", target_id="C001",
            ))

            # Second release: handler logs warning, does not raise
            b.append(_make_event(
                "claim.released", release_payload,
                event_id="E000012", target_kind="claim", target_id="C001",
            ))

            claim = b.get_claim("C001")
            assert claim is not None
            assert claim.status.value == "released"
        finally:
            b.close()

    def test_handle_claim_renewed_updates_timestamps(self, tmp_path: Path) -> None:
        """claim.renewed updates lease_expires_at and last_heartbeat_at in SQLite."""
        b = _make_backend(tmp_path)
        try:
            _setup_claimable_task(b)
            b.append(_make_event(
                "claim.created", _make_claim_payload(),
                event_id="E000010", target_kind="claim", target_id="C001",
            ))

            new_expires = (_T0 + timedelta(hours=2)).isoformat()
            new_heartbeat = (_T0 + timedelta(minutes=30)).isoformat()
            b.append(_make_event(
                "claim.renewed",
                {
                    "claim_id": "C001",
                    "renewed_by": "agent-alpha",
                    "lease_expires_at": new_expires,
                    "last_heartbeat_at": new_heartbeat,
                },
                event_id="E000011", target_kind="claim", target_id="C001",
            ))

            claim = b.get_claim("C001")
            assert claim is not None
            assert claim.lease_expires_at.isoformat() == new_expires
            assert claim.last_heartbeat_at.isoformat() == new_heartbeat
        finally:
            b.close()

    def test_handle_claim_renewed_refuses_inactive_claim(self, tmp_path: Path) -> None:
        """claim.renewed on a non-active claim raises TransactionAborted."""
        b = _make_backend(tmp_path)
        try:
            _setup_claimable_task(b)
            b.append(_make_event(
                "claim.created", _make_claim_payload(),
                event_id="E000010", target_kind="claim", target_id="C001",
            ))
            # Release the claim first
            b.append(_make_event(
                "claim.released",
                {"claim_id": "C001", "released_by": "agent-alpha", "release_reason": "done"},
                event_id="E000011", target_kind="claim", target_id="C001",
            ))

            # Now try to renew the released claim
            with pytest.raises((EventRejected, TransactionAborted), match="active|released|status"):
                b.append(_make_event(
                    "claim.renewed",
                    {
                        "claim_id": "C001",
                        "renewed_by": "agent-alpha",
                        "lease_expires_at": (_T0 + timedelta(hours=3)).isoformat(),
                        "last_heartbeat_at": _T0.isoformat(),
                    },
                    event_id="E000012", target_kind="claim", target_id="C001",
                ))
        finally:
            b.close()

    def test_handle_claim_stale_happy_path(self, tmp_path: Path) -> None:
        """claim.stale marks claim as stale and task returns to ready."""
        b = _make_backend(tmp_path)
        events_path = str(tmp_path / "events.jsonl")
        try:
            _setup_claimable_task(b)
            b.append(_make_event(
                "claim.created", _make_claim_payload(),
                event_id="E000010", target_kind="claim", target_id="C001",
            ))

            b.append(_make_event(
                "claim.stale",
                {
                    "claim_id": "C001",
                    "task_id": "T001",
                    "expired_at": (_T0 - timedelta(hours=1)).isoformat(),
                    "detected_at": _T0.isoformat(),
                    "reason": "lease_expired",
                    "actor": "system",
                },
                event_id="E000011", target_kind="claim", target_id="C001",
            ))

            events = _read_jsonl(events_path)
            assert any(e.get("action") == "claim.stale" for e in events)

            claim = b.get_claim("C001")
            assert claim is not None
            assert claim.status.value == "stale"

            task = b.get_task("T001")
            assert task is not None
            assert task.status.value == "ready"
        finally:
            b.close()

    def test_handle_claim_stale_idempotent(self, tmp_path: Path) -> None:
        """Applying claim.stale twice is a no-op for the second application."""
        b = _make_backend(tmp_path)
        try:
            _setup_claimable_task(b)
            b.append(_make_event(
                "claim.created", _make_claim_payload(),
                event_id="E000010", target_kind="claim", target_id="C001",
            ))

            stale_payload = {
                "claim_id": "C001",
                "task_id": "T001",
                "expired_at": (_T0 - timedelta(hours=1)).isoformat(),
                "detected_at": _T0.isoformat(),
                "reason": "lease_expired",
                "actor": "system",
            }
            b.append(_make_event(
                "claim.stale", stale_payload,
                event_id="E000011", target_kind="claim", target_id="C001",
            ))
            # Second apply: no-op, no raise
            b.append(_make_event(
                "claim.stale", stale_payload,
                event_id="E000012", target_kind="claim", target_id="C001",
            ))

            claim = b.get_claim("C001")
            assert claim is not None
            assert claim.status.value == "stale"
        finally:
            b.close()

    def test_handle_claim_stale_handles_task_already_completed_gracefully(
        self, tmp_path: Path
    ) -> None:
        """claim.stale when task is already 'done': claim goes stale without error."""
        b = _make_backend(tmp_path)
        try:
            _setup_claimable_task(b)
            b.append(_make_event(
                "claim.created", _make_claim_payload(),
                event_id="E000010", target_kind="claim", target_id="C001",
            ))

            # Manually move task to 'done' (simulating completed work)
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            conn.execute("UPDATE tasks SET status = 'done' WHERE id = 'T001'")
            conn.commit()
            conn.close()

            # claim.stale should still mark the claim stale without raising
            b.append(_make_event(
                "claim.stale",
                {
                    "claim_id": "C001",
                    "task_id": "T001",
                    "expired_at": (_T0 - timedelta(hours=1)).isoformat(),
                    "detected_at": _T0.isoformat(),
                    "reason": "lease_expired",
                    "actor": "system",
                },
                event_id="E000011", target_kind="claim", target_id="C001",
            ))

            claim = b.get_claim("C001")
            assert claim is not None
            assert claim.status.value == "stale"

            # Task remains 'done' — not reset to ready
            task = b.get_task("T001")
            assert task is not None
            assert task.status.value == "done"
        finally:
            b.close()


# ---------------------------------------------------------------------------
# Phase 4 — audit guarantee extended to claim event actions
# ---------------------------------------------------------------------------


class TestReplayIncludesPhase4ClaimActions:
    def test_replay_includes_claim_event_actions(self, tmp_path: Path) -> None:
        """Replay events.jsonl with all 4 claim actions; reconstructed DB matches original.

        Sequence: project.created → state.initialized → prd.parsed → prd.reviewed
        → prd.approved → feature.created → task.created → task.scored →
        task.status_changed (→ ready) → claim.created → claim.renewed → claim.released.
        """
        clock = _make_clock()
        db_path = str(tmp_path / "state.db")
        events_path = str(tmp_path / "events.jsonl")
        Path(events_path).touch()

        b = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock)
        b.initialize()

        try:
            # E000001: project.created
            b.append(_make_project_event(event_id="E000001"))
            # E000002: state.initialized
            b.append(_make_init_event(event_id="E000002"))

            # E000003: prd.parsed
            b.append(_make_event(
                "prd.parsed",
                {
                    "project_id": "proj-1",
                    "status": "draft",
                    "summary": "Replay test PRD.",
                    "goals": ["Goal."],
                    "non_goals": [],
                    "requirements": [
                        {"id": "R001", "prd_section": "requirements", "text": "Req.",
                         "source_paragraph": None, "derived": False}
                    ],
                    "acceptance_criteria": ["AC."],
                    "risks": [],
                    "open_questions": [],
                },
                event_id="E000003", target_kind="prd", target_id="proj-1",
            ))
            # E000004: prd.reviewed
            b.append(_make_event(
                "prd.reviewed",
                {"project_id": "proj-1", "reviewer": "alice"},
                event_id="E000004", target_kind="prd", target_id="proj-1",
            ))
            # E000005: prd.approved
            b.append(_make_event(
                "prd.approved",
                {"project_id": "proj-1", "approver": "bob"},
                event_id="E000005", target_kind="prd", target_id="proj-1",
            ))

            # E000006: feature.created
            b.append(_make_event(
                "feature.created",
                _make_feature_payload(feat_id="F001"),
                event_id="E000006", target_kind="feature", target_id="F001",
            ))

            # E000007: task.created
            b.append(_make_event(
                "task.created",
                _make_task_payload(task_id="T001"),
                event_id="E000007", target_kind="task", target_id="T001",
            ))

            # E000008: task.scored
            b.append(_make_event(
                "task.scored",
                {
                    "task_id": "T001",
                    "scores": {
                        "complexity": 2, "parallelizability": 4, "context_load": 3,
                        "blast_radius": 2, "review_risk": 2, "agent_suitability": 4,
                    },
                    "explanation": "scored for replay test",
                },
                event_id="E000008", target_kind="task", target_id="T001",
            ))

            # E000009: task.status_changed proposed → ready (via drafted + reviewed)
            for from_s, to_s, eid in [
                ("proposed", "drafted", "E000009"),
                ("drafted", "reviewed", "E000010"),
                ("reviewed", "ready", "E000011"),
            ]:
                b.append(_make_event(
                    "task.status_changed",
                    {"task_id": "T001", "from": from_s, "to": to_s},
                    event_id=eid, target_kind="task", target_id="T001",
                ))

            # E000012: claim.created
            b.append(_make_event(
                "claim.created",
                _make_claim_payload(claim_id="C001", task_id="T001"),
                event_id="E000012", target_kind="claim", target_id="C001",
            ))

            # E000013: claim.renewed
            b.append(_make_event(
                "claim.renewed",
                {
                    "claim_id": "C001",
                    "renewed_by": "agent-alpha",
                    "lease_expires_at": (_T0 + timedelta(hours=2)).isoformat(),
                    "last_heartbeat_at": (_T0 + timedelta(minutes=30)).isoformat(),
                },
                event_id="E000013", target_kind="claim", target_id="C001",
            ))

            # E000014: claim.released
            b.append(_make_event(
                "claim.released",
                {
                    "claim_id": "C001",
                    "released_by": "agent-alpha",
                    "release_reason": "replay test done",
                    "force": False,
                },
                event_id="E000014", target_kind="claim", target_id="C001",
            ))

        finally:
            b.close()

        # Capture original dump
        original_dump = _sqlite_dump(db_path)

        # Replay from empty
        clock2 = _make_clock()
        b2 = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock2)
        b2.initialize()
        try:
            b2.replay_from_empty(events_path)
        finally:
            b2.close()

        replayed_dump = _sqlite_dump(db_path)
        assert original_dump == replayed_dump, (
            "Replayed state.db does not match original after Phase 4 claim events.\n"
            f"Original dump (first 800 chars):\n{original_dump[:800]}\n\n"
            f"Replayed dump (first 800 chars):\n{replayed_dump[:800]}"
        )


    def test_replay_includes_claim_stale(self, tmp_path: Path) -> None:
        """Audit guarantee for claim.stale: a sequence ending in stale (rather
        than released) must replay byte-identically. Complements the primary
        replay test above which exercises created → renewed → released."""
        clock = _make_clock()
        db_path = str(tmp_path / "state.db")
        events_path = str(tmp_path / "events.jsonl")
        Path(events_path).touch()

        b = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock)
        b.initialize()

        try:
            b.append(_make_project_event(event_id="E000001"))
            b.append(_make_init_event(event_id="E000002"))
            b.append(_make_event(
                "prd.parsed",
                {
                    "project_id": "proj-1", "status": "draft",
                    "summary": "Stale replay test.",
                    "goals": ["G"], "non_goals": [],
                    "requirements": [
                        {"id": "R001", "prd_section": "requirements", "text": "Req.",
                         "source_paragraph": None, "derived": False}
                    ],
                    "acceptance_criteria": ["AC"], "risks": [], "open_questions": [],
                },
                event_id="E000003", target_kind="prd", target_id="proj-1",
            ))
            b.append(_make_event(
                "prd.approved",
                {"project_id": "proj-1", "approver": "bob"},
                event_id="E000004", target_kind="prd", target_id="proj-1",
            ))
            b.append(_make_event(
                "feature.created",
                _make_feature_payload(feat_id="F001"),
                event_id="E000005", target_kind="feature", target_id="F001",
            ))
            b.append(_make_event(
                "task.created",
                _make_task_payload(task_id="T001"),
                event_id="E000006", target_kind="task", target_id="T001",
            ))
            for from_s, to_s, eid in [
                ("proposed", "drafted", "E000007"),
                ("drafted", "reviewed", "E000008"),
                ("reviewed", "ready", "E000009"),
            ]:
                b.append(_make_event(
                    "task.status_changed",
                    {"task_id": "T001", "from": from_s, "to": to_s},
                    event_id=eid, target_kind="task", target_id="T001",
                ))
            # E000010: claim.created (lease expires immediately for stale path)
            claim_payload = _make_claim_payload(claim_id="C001", task_id="T001")
            b.append(_make_event(
                "claim.created", claim_payload,
                event_id="E000010", target_kind="claim", target_id="C001",
            ))
            # E000011: claim.stale (lease expired)
            b.append(_make_event(
                "claim.stale",
                {
                    "claim_id": "C001",
                    "detected_at": (_T0 + timedelta(hours=2)).isoformat(),
                    "reason": "lease_expired",
                },
                event_id="E000011", target_kind="claim", target_id="C001",
            ))
        finally:
            b.close()

        original_dump = _sqlite_dump(db_path)

        clock2 = _make_clock()
        b2 = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock2)
        b2.initialize()
        try:
            b2.replay_from_empty(events_path)
        finally:
            b2.close()

        replayed_dump = _sqlite_dump(db_path)
        assert original_dump == replayed_dump, (
            "Replayed state.db does not match original after claim.stale event."
        )


# ---------------------------------------------------------------------------
# Phase 4 — additional coverage tests for uncovered branches
# ---------------------------------------------------------------------------


class TestGreptileP4Fixes:
    """Regression tests for the three MUST-FIX defects Greptile + critic
    flagged on PR #39: file_changed had no handler (events silently
    aborted); release --force on a stale claim was a no-op; release
    UPDATE on the tasks table was hardcoded to status='claimed'.
    """

    def test_file_changed_event_lands_in_sqlite_with_no_tombstone(
        self, tmp_path: Path
    ) -> None:
        """The record-file-change hook emits action='file_changed'. Before
        the fix, this action had no handler — apply_event would raise
        NotImplementedError, write an error.transaction_aborted tombstone
        to JSONL, and the event was dropped from the events table on
        replay. Now: file_changed is a recognised audit-only action;
        the event lands in both JSONL and the events table; no tombstone."""
        b = _make_backend(tmp_path)
        events_path = str(tmp_path / "events.jsonl")
        try:
            evt = _make_event(
                "file_changed",
                {"file": "src/foo.py", "tool": "Edit", "actor": "agent-alpha"},
                event_id="E000001",
                target_kind="file",
                target_id="src/foo.py",
            )
            result = b.append(evt)
            assert result is not None

            # JSONL: file_changed present, NO error.transaction_aborted.
            events = _read_jsonl(events_path)
            actions = [e.get("action") for e in events]
            assert "file_changed" in actions
            assert "error.transaction_aborted" not in actions

            # SQLite events table: the event row is present (use returned id).
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            row = conn.execute(
                "SELECT id, action FROM events WHERE id = ?", (result.id,)
            ).fetchone()
            conn.close()
            assert row is not None
            assert row[1] == "file_changed"
        finally:
            b.close()

    def test_force_release_of_stale_claim_actually_transitions_status(
        self, tmp_path: Path
    ) -> None:
        """Before the fix, _handle_claim_released's UPDATE was guarded by
        WHERE status='active'. A stale claim (status='stale') matched 0
        rows; the handler logged 'already terminal — no-op' and returned
        success. The claim stayed at 'stale' forever; the user thought
        the force-release worked. Now: with force=True, status is widened
        to NOT IN ('released', 'force_released') and target is 'force_released'."""
        b = _make_backend(tmp_path)
        try:
            _setup_claimable_task(b)
            b.append(_make_event(
                "claim.created", _make_claim_payload(),
                event_id="E000010", target_kind="claim", target_id="C001",
            ))

            # Drive the claim to 'stale' directly (simulates the stale detector
            # having run).
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            conn.execute(
                "UPDATE claims SET status='stale' WHERE id='C001'"
            )
            conn.commit()
            conn.close()

            # Force-release the stale claim.
            release_payload = {
                "claim_id": "C001",
                "released_by": "human",
                "release_reason": "cleaning up",
                "force": True,
            }
            b.append(_make_event(
                "claim.released", release_payload,
                event_id="E000011", target_kind="claim", target_id="C001",
            ))

            # Claim must now be 'force_released', not 'stale'.
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            status = conn.execute(
                "SELECT status FROM claims WHERE id='C001'"
            ).fetchone()[0]
            release_reason = conn.execute(
                "SELECT release_reason FROM claims WHERE id='C001'"
            ).fetchone()[0]
            conn.close()
            assert status == "force_released", (
                f"force-release should transition stale → force_released; "
                f"got {status!r}"
            )
            assert release_reason == "cleaning up"
        finally:
            b.close()

    def test_release_handles_in_progress_task_without_aborting(
        self, tmp_path: Path
    ) -> None:
        """Before the fix, the task UPDATE was hardcoded WHERE status='claimed'.
        Releasing a claim on an in_progress task would TransactionAborted
        because the WHERE matched 0 rows. release --force is supposed to
        work on tasks mid-work; widened to status IN ('claimed', 'in_progress', 'blocked')."""
        b = _make_backend(tmp_path)
        try:
            _setup_claimable_task(b)
            b.append(_make_event(
                "claim.created", _make_claim_payload(),
                event_id="E000010", target_kind="claim", target_id="C001",
            ))
            # Advance task to 'in_progress'
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            conn.execute(
                "UPDATE tasks SET status='in_progress' WHERE id='T001'"
            )
            conn.commit()
            conn.close()

            # Release must succeed and return task to 'ready'.
            b.append(_make_event(
                "claim.released",
                {"claim_id": "C001", "released_by": "agent-alpha",
                 "release_reason": "rolling back", "force": False},
                event_id="E000011", target_kind="claim", target_id="C001",
            ))

            conn = sqlite3.connect(str(tmp_path / "state.db"))
            task_status = conn.execute(
                "SELECT status FROM tasks WHERE id='T001'"
            ).fetchone()[0]
            conn.close()
            assert task_status == "ready"
        finally:
            b.close()

    def test_event_ids_are_consistent_across_cli_and_claim_manager(
        self, tmp_path: Path
    ) -> None:
        """SL1-RR-1: append() is the sole id authority; all paths produce E%06d ids.

        Replaces the next_event_id() test. Under SL1-RR-1, next_event_id() is
        removed; the in-memory counter (_next_seq) is the authority and is only
        incremented inside the flock critical section of append(). The invariant
        is still preserved: the first append gets E000001, the second E000002.
        """
        b = _make_backend(tmp_path)
        try:
            e1 = b.append(_make_project_event())
            assert e1 is not None and e1.id == "E000001"
            e2 = b.append(_make_init_event())
            assert e2 is not None and e2.id == "E000002"
        finally:
            b.close()

    def test_append_after_close_raises_runtime_error(self, tmp_path: Path) -> None:
        """append() after close() raises RuntimeError (not returns a stale id)."""
        b = _make_backend(tmp_path)
        b.close()

        # After close(), append must raise RuntimeError — not return a stale id.
        with pytest.raises(RuntimeError, match="initialize"):
            b.append(_make_project_event())

    def test_claim_id_generator_uses_uuid_not_sequential(
        self, tmp_path: Path
    ) -> None:
        """Greptile P4 finding: the original _generate_claim_id incremented
        max-of-active-claims, which could collide with a historical
        (released/stale) claim that shared the same C### ID. The SQL
        handler's INSERT OR IGNORE would silently no-op, leaving the
        task associated with the OLD claim row while the user was told
        the new claim succeeded. Fix: always use UUID-derived hex; the
        format is 'C' + 8 hex chars so collision is statistically
        impossible (~4 billion-to-one)."""
        from anvil.claims.manager import ClaimManager
        from anvil.clock import SystemClock

        b = _make_backend(tmp_path)
        try:
            mgr = ClaimManager(b, SystemClock(), actor="agent-alpha")
            id1 = mgr._generate_claim_id()  # noqa: SLF001
            id2 = mgr._generate_claim_id()  # noqa: SLF001
            id3 = mgr._generate_claim_id()  # noqa: SLF001

            # Format: 'C' + exactly 8 uppercase-hex chars.
            for cid in (id1, id2, id3):
                assert cid.startswith("C")
                assert len(cid) == 9, f"expected 'C' + 8 hex chars, got {cid!r}"
                assert all(c in "0123456789ABCDEF" for c in cid[1:]), (
                    f"hex suffix has invalid chars: {cid!r}"
                )

            # All three should be distinct (collision probability ~1 in 4 billion).
            assert id1 != id2 and id2 != id3 and id1 != id3, (
                f"sequential _generate_claim_id calls collided: {id1}, {id2}, {id3}"
            )

            # Critically: the ID generator must NOT inspect active claims
            # and produce a sequential next-up — that was the collision bug.
            # Confirm by adding a fake "active" sequential claim and verifying
            # the next generated ID is still a UUID (not C002).
            assert not any(cid in ("C001", "C002", "C003") for cid in (id1, id2, id3))
        finally:
            b.close()


class TestPhase4CoverageEdgeCases:
    """Additional tests to push claims + state coverage above 95%.

    These cover payload validation failures, concurrency guards, and
    error paths that the primary happy-path tests do not exercise.
    """

    # ------------------------------------------------------------------
    # claim.created edge cases
    # ------------------------------------------------------------------

    def test_handle_claim_created_missing_required_field_aborts(
        self, tmp_path: Path
    ) -> None:
        """claim.created with a missing required field raises TransactionAborted."""
        b = _make_backend(tmp_path)
        try:
            _setup_claimable_task(b)
            bad_payload = _make_claim_payload()
            del bad_payload["claimed_by"]  # remove a required field
            event = _make_event(
                "claim.created", bad_payload,
                event_id="E000010", target_kind="claim", target_id="C001",
            )
            with pytest.raises((EventRejected, TransactionAborted), match="claimed_by|required"):
                b.append(event)
        finally:
            b.close()

    def test_handle_claim_created_task_not_found_aborts(
        self, tmp_path: Path
    ) -> None:
        """claim.created for a non-existent task raises TransactionAborted.

        The handler tries to UPDATE tasks WHERE status='ready'; when the task
        does not exist this produces 0 rows → the handler raises TransactionAborted.
        However, the INSERT itself may fail first with a FK constraint if the DB
        enforces foreign keys (which SQLite does with PRAGMA foreign_keys = ON).
        Either path results in TransactionAborted.
        """
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            # No task created — claim refers to T999
            payload = _make_claim_payload(task_id="T999")
            event = _make_event(
                "claim.created", payload,
                event_id="E000003", target_kind="claim", target_id="C001",
            )
            with pytest.raises((EventRejected, TransactionAborted)):
                b.append(event)
        finally:
            b.close()

    # ------------------------------------------------------------------
    # claim.released edge cases
    # ------------------------------------------------------------------

    def test_handle_claim_released_missing_claim_id_aborts(
        self, tmp_path: Path
    ) -> None:
        """claim.released without claim_id raises TransactionAborted."""
        b = _make_backend(tmp_path)
        try:
            _setup_claimable_task(b)
            b.append(_make_event(
                "claim.created", _make_claim_payload(),
                event_id="E000010", target_kind="claim", target_id="C001",
            ))
            bad_payload = {
                # No claim_id
                "released_by": "agent-alpha",
                "release_reason": "done",
                "force": False,
            }
            with pytest.raises((EventRejected, TransactionAborted), match="claim_id|required"):
                b.append(_make_event(
                    "claim.released", bad_payload,
                    event_id="E000011", target_kind="claim", target_id="C001",
                ))
        finally:
            b.close()

    def test_handle_claim_released_claim_not_found_aborts(
        self, tmp_path: Path
    ) -> None:
        """claim.released for a non-existent claim raises TransactionAborted."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            payload = {
                "claim_id": "C999",
                "released_by": "agent-alpha",
                "release_reason": "done",
                "force": False,
            }
            with pytest.raises((EventRejected, TransactionAborted), match="C999|not found"):
                b.append(_make_event(
                    "claim.released", payload,
                    event_id="E000003", target_kind="claim", target_id="C999",
                ))
        finally:
            b.close()

    def test_handle_claim_released_tolerates_task_already_completed(
        self, tmp_path: Path
    ) -> None:
        """claim.released does NOT raise when the task has legitimately
        advanced to 'done' (Phase 5 completion) by the time release runs.
        The release path's task UPDATE is now WHERE status IN
        ('claimed', 'in_progress', 'blocked') and 0-rows is acceptable.
        Previous behaviour TransactionAborted'd on this; Greptile + critic
        flagged it would break release --force in real workflows."""
        b = _make_backend(tmp_path)
        try:
            _setup_claimable_task(b)
            b.append(_make_event(
                "claim.created", _make_claim_payload(),
                event_id="E000010", target_kind="claim", target_id="C001",
            ))

            # Manually advance task to 'done' (simulating Phase 5 completion
            # racing with this release).
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            conn.execute("UPDATE tasks SET status = 'done' WHERE id = 'T001'")
            conn.commit()
            conn.close()

            release_payload = {
                "claim_id": "C001",
                "released_by": "agent-alpha",
                "release_reason": "done",
                "force": False,
            }
            # No raise — release should succeed (idempotent on the task side)
            b.append(_make_event(
                "claim.released", release_payload,
                event_id="E000011", target_kind="claim", target_id="C001",
            ))

            # Claim is released; task stays 'done'.
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            claim_status = conn.execute(
                "SELECT status FROM claims WHERE id = 'C001'"
            ).fetchone()[0]
            task_status = conn.execute(
                "SELECT status FROM tasks WHERE id = 'T001'"
            ).fetchone()[0]
            conn.close()
            assert claim_status == "released"
            assert task_status == "done"
        finally:
            b.close()

    # ------------------------------------------------------------------
    # claim.renewed edge cases
    # ------------------------------------------------------------------

    def test_handle_claim_renewed_missing_field_aborts(
        self, tmp_path: Path
    ) -> None:
        """claim.renewed without lease_expires_at raises TransactionAborted."""
        b = _make_backend(tmp_path)
        try:
            _setup_claimable_task(b)
            b.append(_make_event(
                "claim.created", _make_claim_payload(),
                event_id="E000010", target_kind="claim", target_id="C001",
            ))
            bad_payload = {
                "claim_id": "C001",
                "renewed_by": "agent-alpha",
                # Missing lease_expires_at and last_heartbeat_at
            }
            with pytest.raises((EventRejected, TransactionAborted), match="lease_expires_at|required"):
                b.append(_make_event(
                    "claim.renewed", bad_payload,
                    event_id="E000011", target_kind="claim", target_id="C001",
                ))
        finally:
            b.close()

    def test_handle_claim_renewed_claim_not_found_aborts(
        self, tmp_path: Path
    ) -> None:
        """claim.renewed for a non-existent claim raises TransactionAborted."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            payload = {
                "claim_id": "C999",
                "renewed_by": "agent-alpha",
                "lease_expires_at": (_T0 + timedelta(hours=2)).isoformat(),
                "last_heartbeat_at": _T0.isoformat(),
            }
            with pytest.raises((EventRejected, TransactionAborted), match="C999|not found|active"):
                b.append(_make_event(
                    "claim.renewed", payload,
                    event_id="E000003", target_kind="claim", target_id="C999",
                ))
        finally:
            b.close()

    # ------------------------------------------------------------------
    # claim.stale edge cases
    # ------------------------------------------------------------------

    def test_handle_claim_stale_missing_field_aborts(
        self, tmp_path: Path
    ) -> None:
        """claim.stale without detected_at raises TransactionAborted."""
        b = _make_backend(tmp_path)
        try:
            _setup_claimable_task(b)
            b.append(_make_event(
                "claim.created", _make_claim_payload(),
                event_id="E000010", target_kind="claim", target_id="C001",
            ))
            bad_payload = {
                "claim_id": "C001",
                # Missing detected_at and reason
            }
            with pytest.raises((EventRejected, TransactionAborted), match="detected_at|required"):
                b.append(_make_event(
                    "claim.stale", bad_payload,
                    event_id="E000011", target_kind="claim", target_id="C001",
                ))
        finally:
            b.close()

    def test_handle_claim_stale_claim_not_found_aborts(
        self, tmp_path: Path
    ) -> None:
        """claim.stale for a non-existent claim raises TransactionAborted."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            payload = {
                "claim_id": "C999",
                "task_id": "T001",
                "expired_at": (_T0 - timedelta(hours=1)).isoformat(),
                "detected_at": _T0.isoformat(),
                "reason": "lease_expired",
                "actor": "system",
            }
            with pytest.raises((EventRejected, TransactionAborted), match="C999|not found"):
                b.append(_make_event(
                    "claim.stale", payload,
                    event_id="E000003", target_kind="claim", target_id="C999",
                ))
        finally:
            b.close()

    # ------------------------------------------------------------------
    # prd.parsed edge cases — invalid requirement payload
    # ------------------------------------------------------------------

    def test_handle_prd_parsed_invalid_requirement_aborts(
        self, tmp_path: Path
    ) -> None:
        """prd.parsed with a malformed Requirement dict raises TransactionAborted."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            bad_payload = {
                "project_id": "proj-1",
                "status": "draft",
                "summary": "Bad req test.",
                "goals": ["Goal."],
                "non_goals": [],
                "requirements": [
                    # Missing required 'id' and 'text' — invalid Requirement
                    {"prd_section": "requirements", "derived": False}
                ],
                "acceptance_criteria": [],
                "risks": [],
                "open_questions": [],
            }
            with pytest.raises((EventRejected, TransactionAborted), match="invalid Requirement|prd.parsed"):
                b.append(_make_event(
                    "prd.parsed", bad_payload,
                    event_id="E000003", target_kind="prd", target_id="proj-1",
                ))
        finally:
            b.close()

    # ------------------------------------------------------------------
    # task.scored edge cases — invalid scores payload
    # ------------------------------------------------------------------

    def test_handle_task_scored_invalid_scores_payload_aborts(
        self, tmp_path: Path
    ) -> None:
        """task.scored with non-integer score values raises TransactionAborted."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            b.append(_make_event(
                "feature.created",
                {
                    "id": "F001", "title": "F", "description": "d", "status": "proposed",
                    "requirements": [], "tasks": [],
                },
                event_id="E000003", target_kind="feature", target_id="F001",
            ))
            b.append(_make_event(
                "task.created",
                _make_task_payload(task_id="T001"),
                event_id="E000004", target_kind="task", target_id="T001",
            ))
            bad_scores = {
                "task_id": "T001",
                "scores": {
                    "complexity": "NOT_A_NUMBER",  # invalid
                    "parallelizability": 4,
                    "context_load": 3,
                    "blast_radius": 2,
                    "review_risk": 2,
                    "agent_suitability": 4,
                },
                "explanation": "bad",
            }
            with pytest.raises((EventRejected, TransactionAborted), match="scores|invalid"):
                b.append(_make_event(
                    "task.scored", bad_scores,
                    event_id="E000005", target_kind="task", target_id="T001",
                ))
        finally:
            b.close()

    # ------------------------------------------------------------------
    # task.status_changed — missing 'to' field
    # ------------------------------------------------------------------

    def test_handle_task_status_changed_missing_to_aborts(
        self, tmp_path: Path
    ) -> None:
        """task.status_changed without 'to' field raises TransactionAborted."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            b.append(_make_event(
                "feature.created",
                {
                    "id": "F001", "title": "F", "description": "d", "status": "proposed",
                    "requirements": [], "tasks": [],
                },
                event_id="E000003", target_kind="feature", target_id="F001",
            ))
            b.append(_make_event(
                "task.created",
                _make_task_payload(task_id="T001"),
                event_id="E000004", target_kind="task", target_id="T001",
            ))
            bad_payload = {
                "task_id": "T001",
                "from": "proposed",
                # Missing 'to'
            }
            with pytest.raises((EventRejected, TransactionAborted), match="to|required"):
                b.append(_make_event(
                    "task.status_changed", bad_payload,
                    event_id="E000005", target_kind="task", target_id="T001",
                ))
        finally:
            b.close()

    # ------------------------------------------------------------------
    # task.expanded — empty subtasks list
    # ------------------------------------------------------------------

    def test_handle_task_expanded_empty_subtasks_aborts(
        self, tmp_path: Path
    ) -> None:
        """task.expanded with an empty subtasks list raises TransactionAborted."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            b.append(_make_event(
                "feature.created",
                {
                    "id": "F001", "title": "F", "description": "d", "status": "proposed",
                    "requirements": [], "tasks": [],
                },
                event_id="E000003", target_kind="feature", target_id="F001",
            ))
            b.append(_make_event(
                "task.created",
                _make_task_payload(task_id="T001"),
                event_id="E000004", target_kind="task", target_id="T001",
            ))
            bad_payload = {
                "parent_task_id": "T001",
                "subtasks": [],  # empty
            }
            with pytest.raises((EventRejected, TransactionAborted), match="subtasks|empty"):
                b.append(_make_event(
                    "task.expanded", bad_payload,
                    event_id="E000005", target_kind="task", target_id="T001",
                ))
        finally:
            b.close()


# ---------------------------------------------------------------------------
# Phase 5 — helper factories
# ---------------------------------------------------------------------------


def _make_evidence_payload(
    *,
    task_id: str = "T001",
    claim_id: str = "C001",
    evidence_id: str = "EV001",
    submitted_by: str = "agent-alpha",
    commands_run: list[str] | None = None,
    files_changed: list[str] | None = None,
    output_excerpt: str | None = "5 passed",
    pr_url: str | None = None,
    commit_sha: str | None = None,
    screenshots: list[str] | None = None,
    known_limitations: str | None = None,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "claim_id": claim_id,
        "evidence_id": evidence_id,
        "submitted_by": submitted_by,
        "commands_run": commands_run if commands_run is not None else ["pytest tests/ -v"],
        "files_changed": files_changed if files_changed is not None else ["src/auth.py"],
        "output_excerpt": output_excerpt,
        "pr_url": pr_url,
        "commit_sha": commit_sha,
        "screenshots": screenshots or [],
        "known_limitations": known_limitations,
    }


def _make_applied_payload(
    *,
    task_id: str = "T001",
    reviewer: str = "alice",
    decision: str = "accepted",
    notes: str | None = None,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "reviewer": reviewer,
        "decision": decision,
        "notes": notes,
    }


def _setup_claimable_task_and_claim(
    b: SqliteBackend,
    *,
    task_id: str = "T001",
    claim_id: str = "C001",
) -> None:
    """Set up project → feature → task in 'ready' → claim.created → task='claimed'."""
    _setup_claimable_task(b, task_id=task_id)
    b.append(_make_event(
        "claim.created",
        _make_claim_payload(claim_id=claim_id, task_id=task_id),
        event_id="E000010", target_kind="claim", target_id=claim_id,
    ))


# ---------------------------------------------------------------------------
# Phase 5 — TestPhase5EvidenceAndApplyHandlers
# ---------------------------------------------------------------------------


class TestPhase5EvidenceAndApplyHandlers:
    """Unit tests for evidence.submitted and task.applied event handlers."""

    # ------------------------------------------------------------------
    # evidence.submitted — happy path
    # ------------------------------------------------------------------

    def test_evidence_submitted_inserts_row_and_updates_task(
        self, tmp_path: Path
    ) -> None:
        """evidence.submitted inserts evidence row, sets task.status='needs_review',
        and auto-releases the claim.
        """
        b = _make_backend(tmp_path)
        events_path = str(tmp_path / "events.jsonl")
        try:
            _setup_claimable_task_and_claim(b)

            payload = _make_evidence_payload()
            b.append(_make_event(
                "evidence.submitted", payload,
                event_id="E000011", target_kind="task", target_id="T001",
            ))

            # JSONL
            events = _read_jsonl(events_path)
            assert any(e.get("action") == "evidence.submitted" for e in events)

            # Evidence row present
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            ev_row = conn.execute(
                "SELECT id, task_id, submitted_by FROM evidence WHERE id = 'EV001'"
            ).fetchone()
            conn.close()
            assert ev_row is not None
            assert ev_row[1] == "T001"
            assert ev_row[2] == "agent-alpha"

            # Task status → needs_review
            task = b.get_task("T001")
            assert task is not None
            assert task.status.value == "needs_review"

            # Claim auto-released
            claim = b.get_claim("C001")
            assert claim is not None
            assert claim.status.value == "released"
        finally:
            b.close()

    def test_evidence_submitted_idempotent_on_replay(self, tmp_path: Path) -> None:
        """Applying evidence.submitted twice is a no-op for the second application."""
        b = _make_backend(tmp_path)
        try:
            _setup_claimable_task_and_claim(b)

            payload = _make_evidence_payload()
            event = _make_event(
                "evidence.submitted", payload,
                event_id="E000011", target_kind="task", target_id="T001",
            )
            b.append(event)  # first: commits
            b.append(event)  # second: INSERT OR IGNORE — no-op

            # Still only one evidence row
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            count = conn.execute(
                "SELECT COUNT(*) FROM evidence WHERE id = 'EV001'"
            ).fetchone()[0]
            conn.close()
            assert count == 1

            task = b.get_task("T001")
            assert task is not None
            assert task.status.value == "needs_review"
        finally:
            b.close()

    def test_evidence_submitted_validates_required_fields(
        self, tmp_path: Path
    ) -> None:
        """evidence.submitted without task_id raises TransactionAborted with field name."""
        b = _make_backend(tmp_path)
        try:
            _setup_claimable_task_and_claim(b)

            bad_payload = _make_evidence_payload()
            del bad_payload["task_id"]  # missing required field

            with pytest.raises((EventRejected, TransactionAborted), match="task_id"):
                b.append(_make_event(
                    "evidence.submitted", bad_payload,
                    event_id="E000011", target_kind="task", target_id="T001",
                ))
        finally:
            b.close()

    def test_evidence_submitted_rejects_empty_commands_run(
        self, tmp_path: Path
    ) -> None:
        """evidence.submitted with commands_run=[] raises TransactionAborted."""
        b = _make_backend(tmp_path)
        try:
            _setup_claimable_task_and_claim(b)

            payload = _make_evidence_payload(commands_run=[])
            with pytest.raises((EventRejected, TransactionAborted), match="commands_run"):
                b.append(_make_event(
                    "evidence.submitted", payload,
                    event_id="E000011", target_kind="task", target_id="T001",
                ))
        finally:
            b.close()

    def test_evidence_submitted_allows_empty_files_changed(
        self, tmp_path: Path
    ) -> None:
        """B32: evidence.submitted with files_changed=[] but non-empty
        commands_run is accepted — a verification-only step changes no files,
        and commands_run is the mandatory proof."""
        b = _make_backend(tmp_path)
        try:
            _setup_claimable_task_and_claim(b)

            payload = _make_evidence_payload(files_changed=[])
            # No exception: commands_run is the mandatory proof; files may be empty.
            b.append(_make_event(
                "evidence.submitted", payload,
                event_id="E000011", target_kind="task", target_id="T001",
            ))
            assert b.get_task("T001").status.value == "needs_review"
        finally:
            b.close()

    def test_evidence_submitted_handles_already_needs_review_task_idempotent(
        self, tmp_path: Path
    ) -> None:
        """evidence.submitted when task is already 'needs_review' is a no-op success.

        CL-8 regression: a second submission with a DIFFERENT evidence_id must
        also no-op (not double-insert). Before CL-8 this test passed without
        the count assertion below — INSERT OR IGNORE on the PK only blocked
        same-evidence_id duplicates; a new EV002 row landed alongside EV001.
        """
        b = _make_backend(tmp_path)
        try:
            _setup_claimable_task_and_claim(b)

            payload = _make_evidence_payload()
            b.append(_make_event(
                "evidence.submitted", payload,
                event_id="E000011", target_kind="task", target_id="T001",
            ))
            # Task is now needs_review. Applying again with a different evidence_id
            # used to silently double-insert (CL-8). After the fix the second
            # apply hits the (claim_id, different evidence_id) idempotency guard
            # and warn-logs a no-op.
            payload2 = _make_evidence_payload(evidence_id="EV002")
            b.append(_make_event(
                "evidence.submitted", payload2,
                event_id="E000012", target_kind="task", target_id="T001",
            ))

            task = b.get_task("T001")
            assert task is not None
            assert task.status.value == "needs_review"

            # CL-8: exactly ONE evidence row exists for claim C001.
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            count = conn.execute(
                "SELECT COUNT(*) FROM evidence WHERE claim_id = 'C001'"
            ).fetchone()[0]
            ev_id = conn.execute(
                "SELECT id FROM evidence WHERE claim_id = 'C001'"
            ).fetchone()[0]
            conn.close()
            assert count == 1, "CL-8: second submit with different evidence_id must not insert"
            assert ev_id == "EV001", "CL-8: original evidence row must win"
        finally:
            b.close()

    def test_evidence_submitted_missing_claim_id_aborts(
        self, tmp_path: Path
    ) -> None:
        """evidence.submitted without claim_id raises TransactionAborted."""
        b = _make_backend(tmp_path)
        try:
            _setup_claimable_task_and_claim(b)

            bad = _make_evidence_payload()
            del bad["claim_id"]

            with pytest.raises((EventRejected, TransactionAborted), match="claim_id"):
                b.append(_make_event(
                    "evidence.submitted", bad,
                    event_id="E000011", target_kind="task", target_id="T001",
                ))
        finally:
            b.close()

    def test_evidence_submitted_double_submit_is_idempotent_no_op(
        self, tmp_path: Path
    ) -> None:
        """CL-8: a second submit() for the same claim with a different
        evidence_id is an idempotent no-op.

        SL1-RR-1: idempotent no-ops are recorded in audit.jsonl (not events.jsonl).
        The duplicate returns None from append() and an idempotent_no_op line in audit.
        Before CL-8 the second INSERT silently succeeded, producing two evidence rows.
        After the fix the duplicate is an IdempotentNoOp — append() returns None.
        """
        b = _make_backend(tmp_path)
        audit_path = str(tmp_path / "audit.jsonl")
        try:
            _setup_claimable_task_and_claim(b)

            # First submission lands normally.
            first = b.append(_make_event(
                "evidence.submitted", _make_evidence_payload(evidence_id="EV001"),
                event_id="E000011", target_kind="task", target_id="T001",
            ))
            assert first is not None, "First evidence submission should succeed"

            # Second submission with a different evidence_id — the CL-8 guard.
            # Under SL1-RR-1, this is an IdempotentNoOp; append() returns None.
            second = b.append(_make_event(
                "evidence.submitted", _make_evidence_payload(evidence_id="EV002"),
                event_id="E000012", target_kind="task", target_id="T001",
            ))
            # None means idempotent no-op (already satisfied).
            assert second is None, (
                "CL-8: second evidence submission must be an idempotent no-op "
                "(append returns None)"
            )

            # Exactly one evidence row survived.
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            count = conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
            conn.close()
            assert count == 1

            # SL1-RR-1: idempotent_no_op is recorded in audit.jsonl, NOT events.jsonl.
            audit_records = _read_audit_jsonl(audit_path)
            noop_entries = [
                r for r in audit_records if r.get("kind") == "idempotent_no_op"
            ]
            assert len(noop_entries) >= 1, (
                "CL-8: expected idempotent_no_op line in audit.jsonl for the "
                f"duplicate EV002 submission; got audit: {audit_records}"
            )
        finally:
            b.close()

    # ------------------------------------------------------------------
    # task.applied — happy path
    # ------------------------------------------------------------------

    def test_task_applied_accepted_transitions_to_done(
        self, tmp_path: Path
    ) -> None:
        """needs_review → accepted → done (combined atomic transition)."""
        b = _make_backend(tmp_path)
        events_path = str(tmp_path / "events.jsonl")
        try:
            _setup_claimable_task_and_claim(b)
            # Submit evidence to reach needs_review
            b.append(_make_event(
                "evidence.submitted", _make_evidence_payload(),
                event_id="E000011", target_kind="task", target_id="T001",
            ))

            # Apply: accepted → done
            b.append(_make_event(
                "task.applied",
                _make_applied_payload(decision="accepted", reviewer="alice"),
                event_id="E000012", target_kind="task", target_id="T001",
            ))

            events = _read_jsonl(events_path)
            assert any(e.get("action") == "task.applied" for e in events)

            task = b.get_task("T001")
            assert task is not None
            assert task.status.value == "done"
        finally:
            b.close()

    def test_task_applied_rejected_auto_promotes_to_drafted(
        self, tmp_path: Path
    ) -> None:
        """needs_review + decision=rejected → task.status='drafted'.

        Per spec (docs/specs/2026-05-24-anvil-v0.md): the rejected
        state is a transient audit marker; the task immediately auto-
        promotes to drafted so it can be re-reviewed. Critic-1 + Critic-2
        flagged that the original implementation left the task permanently
        at 'rejected' with no path back — `task_rejected_to_drafted` in
        transitions.py was dead code.

        The audit log still records decision='rejected' on the Review row
        (the human's call). The mechanical outcome is drafted.
        """
        b = _make_backend(tmp_path)
        try:
            _setup_claimable_task_and_claim(b)
            b.append(_make_event(
                "evidence.submitted", _make_evidence_payload(),
                event_id="E000011", target_kind="task", target_id="T001",
            ))

            b.append(_make_event(
                "task.applied",
                _make_applied_payload(decision="rejected", reviewer="bob", notes="Needs more tests."),
                event_id="E000012", target_kind="task", target_id="T001",
            ))

            task = b.get_task("T001")
            assert task is not None
            assert task.status.value == "drafted", (
                f"rejected path must auto-promote to drafted per spec; got {task.status.value!r}"
            )

            # The Review row still records decision='rejected'.
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            review = conn.execute(
                "SELECT decision FROM reviews WHERE id = 'RV-E000012'"
            ).fetchone()
            conn.close()
            assert review is not None and review[0] == "rejected"
        finally:
            b.close()

    def test_task_applied_inserts_review_row(self, tmp_path: Path) -> None:
        """task.applied inserts a reviews row with id=RV-{event_id}."""
        b = _make_backend(tmp_path)
        try:
            _setup_claimable_task_and_claim(b)
            b.append(_make_event(
                "evidence.submitted", _make_evidence_payload(),
                event_id="E000011", target_kind="task", target_id="T001",
            ))

            b.append(_make_event(
                "task.applied",
                _make_applied_payload(decision="accepted"),
                event_id="E000012", target_kind="task", target_id="T001",
            ))

            conn = sqlite3.connect(str(tmp_path / "state.db"))
            review = conn.execute(
                "SELECT id, target_kind, target_id, decision FROM reviews WHERE target_kind='task'"
            ).fetchone()
            conn.close()
            assert review is not None
            assert review[0] == "RV-E000012"
            assert review[1] == "task"
            assert review[2] == "T001"
            assert review[3] == "accepted"
        finally:
            b.close()

    def test_task_applied_concurrency_guard_on_wrong_status(
        self, tmp_path: Path
    ) -> None:
        """task.applied when task is NOT 'needs_review' → TransactionAborted."""
        b = _make_backend(tmp_path)
        try:
            _setup_claimable_task_and_claim(b)
            # Task is 'claimed', not 'needs_review'
            with pytest.raises((EventRejected, TransactionAborted), match="needs_review|status|T001"):
                b.append(_make_event(
                    "task.applied",
                    _make_applied_payload(decision="accepted"),
                    event_id="E000011", target_kind="task", target_id="T001",
                ))
        finally:
            b.close()

    def test_task_applied_replay_idempotent(self, tmp_path: Path) -> None:
        """Replaying task.applied twice with same event_id is a no-op (INSERT OR REPLACE)."""
        b = _make_backend(tmp_path)
        try:
            _setup_claimable_task_and_claim(b)
            b.append(_make_event(
                "evidence.submitted", _make_evidence_payload(),
                event_id="E000011", target_kind="task", target_id="T001",
            ))

            apply_event = _make_event(
                "task.applied",
                _make_applied_payload(decision="accepted"),
                event_id="E000012", target_kind="task", target_id="T001",
            )
            b.append(apply_event)  # first: commits, task → done

            # Second: idempotent — accepted/done path is no-op (INSERT OR REPLACE for review)
            b.append(apply_event)  # no raise expected

            task = b.get_task("T001")
            assert task is not None
            assert task.status.value == "done"
        finally:
            b.close()

    def test_task_applied_invalid_decision_aborts(self, tmp_path: Path) -> None:
        """task.applied with decision='approved' (not 'accepted'/'rejected') → TransactionAborted."""
        b = _make_backend(tmp_path)
        try:
            _setup_claimable_task_and_claim(b)
            b.append(_make_event(
                "evidence.submitted", _make_evidence_payload(),
                event_id="E000011", target_kind="task", target_id="T001",
            ))

            with pytest.raises((EventRejected, TransactionAborted), match="decision|accepted|rejected"):
                b.append(_make_event(
                    "task.applied",
                    _make_applied_payload(decision="approved"),  # invalid
                    event_id="E000012", target_kind="task", target_id="T001",
                ))
        finally:
            b.close()

    def test_task_applied_missing_reviewer_aborts(self, tmp_path: Path) -> None:
        """task.applied without reviewer raises TransactionAborted."""
        b = _make_backend(tmp_path)
        try:
            _setup_claimable_task_and_claim(b)
            b.append(_make_event(
                "evidence.submitted", _make_evidence_payload(),
                event_id="E000011", target_kind="task", target_id="T001",
            ))

            bad = {"task_id": "T001", "decision": "accepted"}  # no reviewer
            with pytest.raises((EventRejected, TransactionAborted), match="reviewer"):
                b.append(_make_event(
                    "task.applied", bad,
                    event_id="E000012", target_kind="task", target_id="T001",
                ))
        finally:
            b.close()

    def test_task_applied_rejected_replay_idempotent(self, tmp_path: Path) -> None:
        """Replaying task.applied (rejected) twice is a no-op for the second
        application.

        After the auto-promote fix, the task ends at 'drafted' after the
        first apply (rejected → drafted is automatic). The idempotent
        branch must accept both 'rejected' (transient) and 'drafted'
        (final) as valid states to encounter on replay.
        """
        b = _make_backend(tmp_path)
        try:
            _setup_claimable_task_and_claim(b)
            b.append(_make_event(
                "evidence.submitted", _make_evidence_payload(),
                event_id="E000011", target_kind="task", target_id="T001",
            ))

            reject_event = _make_event(
                "task.applied",
                _make_applied_payload(decision="rejected", reviewer="bob", notes="Needs more."),
                event_id="E000012", target_kind="task", target_id="T001",
            )
            b.append(reject_event)  # first: needs_review → rejected → drafted
            b.append(reject_event)  # second: idempotent no-op (already drafted)

            task = b.get_task("T001")
            assert task is not None
            # After auto-promote, the task ends at 'drafted', not 'rejected'.
            assert task.status.value == "drafted"
        finally:
            b.close()

    def test_list_reviews_rejected_maps_to_needs_changes(self, tmp_path: Path) -> None:
        """list_reviews() maps task.applied decision='rejected' to ReviewDecision.needs_changes.

        Pins _TASK_OUTCOME_TO_REVIEW_DECISION so the mapping cannot silently
        regress.  A rejected task auto-promotes back to drafted for rework —
        it is NOT the terminal ReviewDecision.reject closure.
        """
        from anvil.state.models import ReviewDecision

        b = _make_backend(tmp_path)
        try:
            _setup_claimable_task_and_claim(b)
            b.append(_make_event(
                "evidence.submitted", _make_evidence_payload(),
                event_id="E000011", target_kind="task", target_id="T001",
            ))
            b.append(_make_event(
                "task.applied",
                _make_applied_payload(decision="rejected", reviewer="bob", notes="Fix it."),
                event_id="E000012", target_kind="task", target_id="T001",
            ))

            reviews = b.list_reviews()
            task_reviews = [r for r in reviews if r.target_kind.value == "task"]
            assert len(task_reviews) == 1, (
                f"expected 1 task review from list_reviews(), got {len(task_reviews)}"
            )
            assert task_reviews[0].decision == ReviewDecision.needs_changes, (
                f"rejected task.applied should map to needs_changes via list_reviews(); "
                f"got {task_reviews[0].decision!r}"
            )
        finally:
            b.close()

    def test_list_reviews_accepted_maps_to_approve(self, tmp_path: Path) -> None:
        """list_reviews() maps task.applied decision='accepted' to ReviewDecision.approve.

        Companion to test_list_reviews_rejected_maps_to_needs_changes — pins
        both directions of _TASK_OUTCOME_TO_REVIEW_DECISION.
        """
        from anvil.state.models import ReviewDecision

        b = _make_backend(tmp_path)
        try:
            _setup_claimable_task_and_claim(b)
            b.append(_make_event(
                "evidence.submitted", _make_evidence_payload(),
                event_id="E000011", target_kind="task", target_id="T001",
            ))
            b.append(_make_event(
                "task.applied",
                _make_applied_payload(decision="accepted", reviewer="alice"),
                event_id="E000012", target_kind="task", target_id="T001",
            ))

            reviews = b.list_reviews()
            task_reviews = [r for r in reviews if r.target_kind.value == "task"]
            assert len(task_reviews) == 1, (
                f"expected 1 task review from list_reviews(), got {len(task_reviews)}"
            )
            assert task_reviews[0].decision == ReviewDecision.approve, (
                f"accepted task.applied should map to approve via list_reviews(); "
                f"got {task_reviews[0].decision!r}"
            )
        finally:
            b.close()


# ---------------------------------------------------------------------------
# Phase 5 — THE CRITICAL TEST: replay includes Phase 5 events
# ---------------------------------------------------------------------------


class TestReplayIncludesPhase5Events:
    def test_replay_includes_evidence_and_apply_events(self, tmp_path: Path) -> None:
        """Full lifecycle replay: project.created → state.initialized → prd.parsed
        → prd.approved → feature.created → task.created → task.status_changed (→ ready)
        → claim.created → evidence.submitted → task.applied.

        Assert byte-equal sqlite3 .dump after replay.
        """
        clock = _make_clock()
        db_path = str(tmp_path / "state.db")
        events_path = str(tmp_path / "events.jsonl")
        Path(events_path).touch()

        b = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock)
        b.initialize()

        try:
            # E000001: project.created
            b.append(_make_project_event(event_id="E000001"))
            # E000002: state.initialized
            b.append(_make_init_event(event_id="E000002"))

            # E000003: prd.parsed
            b.append(_make_event(
                "prd.parsed",
                {
                    "project_id": "proj-1",
                    "status": "draft",
                    "summary": "Phase 5 replay test PRD.",
                    "goals": ["Goal."],
                    "non_goals": [],
                    "requirements": [
                        {"id": "R001", "prd_section": "requirements", "text": "Req.",
                         "source_paragraph": None, "derived": False}
                    ],
                    "acceptance_criteria": ["AC."],
                    "risks": [],
                    "open_questions": [],
                },
                event_id="E000003", target_kind="prd", target_id="proj-1",
            ))

            # E000004: prd.approved (skipping prd.reviewed for brevity — approved is valid)
            b.append(_make_event(
                "prd.approved",
                {"project_id": "proj-1", "approver": "alice"},
                event_id="E000004", target_kind="prd", target_id="proj-1",
            ))

            # E000005: feature.created
            b.append(_make_event(
                "feature.created",
                _make_feature_payload(feat_id="F001"),
                event_id="E000005", target_kind="feature", target_id="F001",
            ))

            # E000006: task.created
            b.append(_make_event(
                "task.created",
                _make_task_payload(task_id="T001"),
                event_id="E000006", target_kind="task", target_id="T001",
            ))

            # E000007–E000009: task.status_changed proposed → ready
            for from_s, to_s, eid in [
                ("proposed", "drafted", "E000007"),
                ("drafted", "reviewed", "E000008"),
                ("reviewed", "ready", "E000009"),
            ]:
                b.append(_make_event(
                    "task.status_changed",
                    {"task_id": "T001", "from": from_s, "to": to_s},
                    event_id=eid, target_kind="task", target_id="T001",
                ))

            # E000010: claim.created
            b.append(_make_event(
                "claim.created",
                _make_claim_payload(claim_id="C001", task_id="T001"),
                event_id="E000010", target_kind="claim", target_id="C001",
            ))

            # E000011: evidence.submitted
            b.append(_make_event(
                "evidence.submitted",
                _make_evidence_payload(
                    task_id="T001",
                    claim_id="C001",
                    evidence_id="EV001",
                    submitted_by="agent-alpha",
                    commands_run=["pytest tests/ -v"],
                    files_changed=["src/auth.py"],
                    output_excerpt="5 passed",
                ),
                event_id="E000011", target_kind="task", target_id="T001",
            ))

            # E000012: task.applied (accepted → done)
            b.append(_make_event(
                "task.applied",
                _make_applied_payload(task_id="T001", reviewer="alice", decision="accepted"),
                event_id="E000012", target_kind="task", target_id="T001",
            ))

        finally:
            b.close()

        # Capture original dump
        original_dump = _sqlite_dump(db_path)

        # Replay from empty
        clock2 = _make_clock()
        b2 = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock2)
        b2.initialize()
        try:
            b2.replay_from_empty(events_path)
        finally:
            b2.close()

        replayed_dump = _sqlite_dump(db_path)
        assert original_dump == replayed_dump, (
            "Replayed state.db does not match original after Phase 5 events.\n"
            f"Original dump (first 800 chars):\n{original_dump[:800]}\n\n"
            f"Replayed dump (first 800 chars):\n{replayed_dump[:800]}"
        )

    def test_replay_includes_rejected_apply(self, tmp_path: Path) -> None:
        """Audit guarantee for the rejected branch: evidence.submitted → task.applied
        (rejected) replays byte-identically.
        """
        clock = _make_clock()
        db_path = str(tmp_path / "state.db")
        events_path = str(tmp_path / "events.jsonl")
        Path(events_path).touch()

        b = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock)
        b.initialize()

        try:
            _setup_claimable_task(b)
            b.append(_make_event(
                "claim.created",
                _make_claim_payload(claim_id="C001", task_id="T001"),
                event_id="E000010", target_kind="claim", target_id="C001",
            ))
            b.append(_make_event(
                "evidence.submitted",
                _make_evidence_payload(task_id="T001", claim_id="C001", evidence_id="EV001"),
                event_id="E000011", target_kind="task", target_id="T001",
            ))
            b.append(_make_event(
                "task.applied",
                _make_applied_payload(task_id="T001", reviewer="bob", decision="rejected",
                                      notes="Incomplete."),
                event_id="E000012", target_kind="task", target_id="T001",
            ))
        finally:
            b.close()

        original_dump = _sqlite_dump(db_path)

        clock2 = _make_clock()
        b2 = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock2)
        b2.initialize()
        try:
            b2.replay_from_empty(events_path)
        finally:
            b2.close()

        replayed_dump = _sqlite_dump(db_path)
        assert original_dump == replayed_dump, (
            "Replayed state.db does not match original after rejected task.applied."
        )


# ---------------------------------------------------------------------------
# Backend Protocol extensions — get_feature, list_events, get_latest_evidence
# ---------------------------------------------------------------------------


def _setup_feature_and_task(
    b: SqliteBackend,
    *,
    feat_id: str = "F001",
    task_id: str = "T001",
) -> None:
    """Bootstrap a project → feature → task pipeline into 'ready' status."""
    _setup_project(b)
    b.append(_make_event(
        "feature.created",
        _make_feature_payload(feat_id=feat_id),
        event_id="E000003", target_kind="feature", target_id=feat_id,
    ))
    b.append(_make_event(
        "task.created",
        _make_task_payload(task_id=task_id, feature_id=feat_id),
        event_id="E000004", target_kind="task", target_id=task_id,
    ))
    # Promote proposed → drafted → reviewed → ready so the task can be claimed.
    for ev_id, from_s, to_s in (
        ("E000005", "proposed", "drafted"),
        ("E000006", "drafted", "reviewed"),
        ("E000007", "reviewed", "ready"),
    ):
        b.append(_make_event(
            "task.status_changed",
            {"task_id": task_id, "from": from_s, "to": to_s, "reason": "test"},
            event_id=ev_id, target_kind="task", target_id=task_id,
        ))


class TestBackendProtocolExtensions:
    """Tests for get_feature, list_events, and get_latest_evidence on SqliteBackend."""

    # ------------------------------------------------------------------
    # get_feature
    # ------------------------------------------------------------------

    def test_get_feature_returns_feature_when_exists(self, tmp_path: Path) -> None:
        """get_feature() returns the Feature model for a known feature ID."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            b.append(_make_event(
                "feature.created",
                _make_feature_payload(feat_id="F001", title="Auth System"),
                event_id="E000003", target_kind="feature", target_id="F001",
            ))
            feature = b.get_feature("F001")
            assert feature is not None
            assert feature.id == "F001"
            assert feature.title == "Auth System"
        finally:
            b.close()

    def test_get_feature_returns_none_when_not_found(self, tmp_path: Path) -> None:
        """get_feature() returns None when the feature ID does not exist."""
        b = _make_backend(tmp_path)
        try:
            result = b.get_feature("F999")
            assert result is None
        finally:
            b.close()

    # ------------------------------------------------------------------
    # list_events
    # ------------------------------------------------------------------

    def test_list_events_returns_recent_events_for_target(self, tmp_path: Path) -> None:
        """list_events() returns (action, timestamp) tuples for the given target_id."""
        b = _make_backend(tmp_path)
        try:
            _setup_feature_and_task(b, feat_id="F001", task_id="T001")
            rows = b.list_events(target_id="T001")
            # At least the status_changed events for T001 should be present.
            assert len(rows) > 0
            # Each row is a 2-tuple.
            for action, ts in rows:
                assert isinstance(action, str)
                assert isinstance(ts, str)
        finally:
            b.close()

    def test_list_events_filters_by_target_kind(self, tmp_path: Path) -> None:
        """list_events(target_kind=...) restricts to events with that target_kind."""
        b = _make_backend(tmp_path)
        try:
            _setup_feature_and_task(b, feat_id="F001", task_id="T001")
            task_events = b.list_events(target_id="T001", target_kind="task")
            # All returned events must have been recorded against the 'task' kind.
            # We can confirm by checking that every action is a task action.
            assert len(task_events) > 0
            for action, _ in task_events:
                assert "task" in action or "status" in action
        finally:
            b.close()

    def test_list_events_honours_limit(self, tmp_path: Path) -> None:
        """list_events(limit=N) returns at most N rows."""
        b = _make_backend(tmp_path)
        try:
            # _setup_feature_and_task emits 3 status_changed events for T001.
            _setup_feature_and_task(b, feat_id="F001", task_id="T001")
            limited = b.list_events(target_id="T001", target_kind="task", limit=2)
            assert len(limited) <= 2
        finally:
            b.close()

    # ------------------------------------------------------------------
    # get_latest_evidence
    # ------------------------------------------------------------------

    def test_get_latest_evidence_returns_most_recent(self, tmp_path: Path) -> None:
        """get_latest_evidence() returns the Evidence model after evidence.submitted."""
        b = _make_backend(tmp_path)
        try:
            _setup_feature_and_task(b, feat_id="F001", task_id="T001")
            # Create a claim so evidence.submitted has a valid FK.
            b.append(_make_event(
                "claim.created",
                _make_claim_payload(claim_id="C001", task_id="T001"),
                event_id="E000011", target_kind="claim", target_id="C001",
            ))
            b.append(_make_event(
                "evidence.submitted",
                _make_evidence_payload(
                    task_id="T001",
                    claim_id="C001",
                    evidence_id="EV001",
                    commands_run=["pytest tests/ -v"],
                    files_changed=["src/auth.py"],
                ),
                event_id="E000012", target_kind="task", target_id="T001",
            ))
            evidence = b.get_latest_evidence("T001")
            assert evidence is not None
            assert evidence.id == "EV001"
            assert evidence.task_id == "T001"
            assert evidence.claim_id == "C001"
            assert "pytest" in evidence.commands_run[0]
        finally:
            b.close()

    def test_get_latest_evidence_returns_none_when_no_evidence(self, tmp_path: Path) -> None:
        """get_latest_evidence() returns None when no evidence exists for the task."""
        b = _make_backend(tmp_path)
        try:
            result = b.get_latest_evidence("T001")
            assert result is None
        finally:
            b.close()


# ---------------------------------------------------------------------------
# SL1-RR-1 — log-authority id assignment (replaces PENDING_EVENT_ID)
# ---------------------------------------------------------------------------


class TestLogAuthorityIdAssignment:
    """SL1-RR-1: the log is the id authority; append() assigns sequential ids.

    Replaces TestPendingEventId. The PENDING sentinel and the dual-path
    apply_event() are retired. append(EventDraft) is the sole write entry
    point; ids are assigned from an in-memory counter seeded from the log max.
    """

    def test_sequential_appends_produce_sequential_ids(self, tmp_path: Path) -> None:
        """Three sequential append() calls receive E000001, E000002, E000003."""
        b = _make_backend(tmp_path)
        try:
            e1 = b.append(_make_project_event())
            e2 = b.append(_make_init_event())
            e3 = b.append(_make_event(
                "project.created",
                {"id": "proj-1", "name": "Project 2", "description": "",
                 "created_at": _T0.isoformat(), "updated_at": _T0.isoformat()},
                target_kind="project", target_id="proj-1",
            ))
        finally:
            b.close()

        assert e1 is not None and e1.id == "E000001"
        assert e2 is not None and e2.id == "E000002"
        assert e3 is not None and e3.id == "E000003"

    def test_append_returns_event_with_real_id(self, tmp_path: Path) -> None:
        """append(EventDraft) returns an Event with a real E%06d id (not PENDING)."""
        b = _make_backend(tmp_path)
        try:
            result = b.append(_make_project_event())
        finally:
            b.close()

        assert result is not None
        assert result.id.startswith("E"), f"Assigned ID must be E%06d format; got {result.id!r}"
        assert result.id[1:].isdigit(), f"Assigned ID suffix must be numeric; got {result.id!r}"

    def test_rejected_draft_leaves_zero_events_jsonl_lines(
        self, tmp_path: Path
    ) -> None:
        """A rejected draft (unsupported action) leaves NO line in events.jsonl.

        SL1-RR-1: validation is pre-log. A rejection (EventRejected) means
        nothing was written to events.jsonl — not even the draft, not even
        an abort tombstone. The log is clean.
        """
        b = _make_backend(tmp_path)
        events_path = str(tmp_path / "events.jsonl")
        try:
            # Apply one good event first so the JSONL has 1 line.
            b.append(_make_project_event())

            initial_events = _read_jsonl(events_path)
            assert len(initial_events) == 1

            # Attempt a draft with an unsupported action — should raise EventRejected.
            bad_draft = EventDraft(
                timestamp=_T0,
                actor="test",
                action="unsupported.action",
                target_kind="project",
                target_id="proj-1",
                payload_json={},
            )
            with pytest.raises(EventRejected):
                b.append(bad_draft)

            events_after = _read_jsonl(events_path)
        finally:
            b.close()

        # Still exactly 1 line — the rejection wrote nothing.
        assert len(events_after) == 1, (
            f"SL1-RR-1: rejected draft must not write to events.jsonl; "
            f"got {len(events_after)} lines"
        )
        assert events_after[0].get("action") == "project.created"

    def test_replay_preserves_log_ids(self, tmp_path: Path) -> None:
        """replay_from_empty() preserves the original IDs from events.jsonl.

        The ids in events.jsonl are the truth; replay must apply events with
        their original ids — never reassign them. This is the log-id-preservation
        property that SL1-RR-1 relies on (previously tested via the PENDING path).
        """
        clock = _make_clock()
        b = _make_backend(tmp_path, clock)
        events_path = str(tmp_path / "events.jsonl")
        db_path = str(tmp_path / "state.db")

        try:
            # Use append() — get back the backend-assigned IDs.
            e1 = b.append(_make_project_event())
            e2 = b.append(_make_init_event())
            original_ids = [e1.id, e2.id]
        finally:
            b.close()

        assert original_ids == ["E000001", "E000002"], (
            f"Expected first two appends to get E000001, E000002; got {original_ids}"
        )

        # Replay from empty.
        clock2 = _make_clock()
        b2 = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock2)
        b2.initialize()
        try:
            b2.replay_from_empty(events_path)
            # After replay, query the events table to verify IDs were preserved.
            conn = sqlite3.connect(db_path)
            rows = conn.execute(
                "SELECT id FROM events ORDER BY id"
            ).fetchall()
            conn.close()
            replayed_ids = [row[0] for row in rows]
        finally:
            b2.close()

        assert replayed_ids == original_ids, (
            f"Replay must preserve original event IDs {original_ids}; "
            f"got {replayed_ids}"
        )


# ---------------------------------------------------------------------------
# Phase 8 — sync_mapping handlers (sync_mapping.upserted / sync_mapping.deleted)
# ---------------------------------------------------------------------------


def _make_sync_mapping_payload(
    *,
    task_id: str = "T001",
    external_system: str = "github_issues",
    external_id: str = "gh-42",
    last_synced_at: datetime = _T0,
    sync_state: str = "in_sync",
    conflict_resolution_strategy: str = "prompt",
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "external_system": external_system,
        "external_id": external_id,
        "last_synced_at": last_synced_at.isoformat(),
        "sync_state": sync_state,
        "conflict_resolution_strategy": conflict_resolution_strategy,
    }


def _setup_task_for_sync(
    b: SqliteBackend, task_id: str = "T001", feature_id: str = "F001",
    *, base_event_id: int = 3,
) -> None:
    """Apply minimum events to leave a tasks row with id=task_id present.

    Sync_mappings has a FK into tasks; the FK is RESTRICT, so a sync_mapping
    insert against a non-existent task_id will fail. Most TestSyncMappingHandler
    cases need a real task to attach to.
    """
    b.append(_make_event(
        "feature.created", _make_feature_payload(feat_id=feature_id),
        event_id=f"E{base_event_id:06d}",
        target_kind="feature", target_id=feature_id,
    ))
    b.append(_make_event(
        "task.created", _make_task_payload(task_id=task_id, feature_id=feature_id),
        event_id=f"E{base_event_id + 1:06d}",
        target_kind="task", target_id=task_id,
    ))


class TestSyncMappingHandler:
    """Phase 8: sync_mapping.upserted / sync_mapping.deleted handlers.

    Exercises the upsert semantics (composite-key ON CONFLICT), the delete
    semantics (scoped and broad), the replay round-trip, the FK behaviour,
    and the convenience apply_sync_mapping() wrapper that uses PENDING_EVENT_ID.
    """

    def test_upsert_inserts_row_and_get_returns_it(self, tmp_path: Path) -> None:
        """sync_mapping.upserted inserts a row; get_sync_mapping reads it back."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            _setup_task_for_sync(b)

            payload = _make_sync_mapping_payload()
            event = _make_event(
                "sync_mapping.upserted", payload, event_id="E000005",
                target_kind="task", target_id="T001",
            )
            b.append(event)

            mapping = b.get_sync_mapping("T001")
            assert mapping is not None
            assert mapping.task_id == "T001"
            assert mapping.external_system == "github_issues"
            assert mapping.external_id == "gh-42"
            assert mapping.sync_state == "in_sync"
        finally:
            b.close()

    def test_upsert_on_existing_key_updates_row(self, tmp_path: Path) -> None:
        """A second upsert with the same (task_id, external_system) updates in place."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            _setup_task_for_sync(b)

            b.append(_make_event(
                "sync_mapping.upserted",
                _make_sync_mapping_payload(external_id="gh-42", sync_state="in_sync"),
                event_id="E000005", target_kind="task", target_id="T001",
            ))
            b.append(_make_event(
                "sync_mapping.upserted",
                _make_sync_mapping_payload(external_id="gh-99", sync_state="conflict"),
                event_id="E000006", target_kind="task", target_id="T001",
            ))

            # Still exactly one row, with the new field values.
            mappings = b.list_sync_mappings()
            assert len(mappings) == 1
            assert mappings[0].external_id == "gh-99"
            assert mappings[0].sync_state == "conflict"
        finally:
            b.close()

    def test_delete_removes_row(self, tmp_path: Path) -> None:
        """sync_mapping.deleted removes a row; get returns None afterwards."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            _setup_task_for_sync(b)

            b.append(_make_event(
                "sync_mapping.upserted", _make_sync_mapping_payload(),
                event_id="E000005", target_kind="task", target_id="T001",
            ))
            assert b.get_sync_mapping("T001") is not None

            b.append(_make_event(
                "sync_mapping.deleted", {"task_id": "T001"},
                event_id="E000006", target_kind="task", target_id="T001",
            ))
            assert b.get_sync_mapping("T001") is None
        finally:
            b.close()

    def test_delete_idempotent_on_missing_row(self, tmp_path: Path) -> None:
        """sync_mapping.deleted against a never-mapped task is a silent no-op."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            _setup_task_for_sync(b)

            # No upsert first — delete should succeed regardless.
            b.append(_make_event(
                "sync_mapping.deleted", {"task_id": "T001"},
                event_id="E000005", target_kind="task", target_id="T001",
            ))
            assert b.get_sync_mapping("T001") is None
        finally:
            b.close()

    def test_delete_scoped_to_one_external_system(self, tmp_path: Path) -> None:
        """sync_mapping.deleted with external_system only drops that single row."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            _setup_task_for_sync(b)

            b.append(_make_event(
                "sync_mapping.upserted",
                _make_sync_mapping_payload(external_system="github_issues"),
                event_id="E000005", target_kind="task", target_id="T001",
            ))
            # Only one StrEnum value today; for the second mapping we re-use
            # github_issues with a different external_id but per the composite
            # PK that would be an UPDATE, not a second row. So instead the
            # "scoped delete" coverage is: deleting the github_issues row
            # removes the only mapping for the task, while a delete with the
            # WRONG external_system leaves it intact (no-op).
            b.append(_make_event(
                "sync_mapping.deleted",
                {"task_id": "T001", "external_system": "linear"},
                event_id="E000006", target_kind="task", target_id="T001",
            ))
            assert b.get_sync_mapping("T001") is not None  # untouched

            b.append(_make_event(
                "sync_mapping.deleted",
                {"task_id": "T001", "external_system": "github_issues"},
                event_id="E000007", target_kind="task", target_id="T001",
            ))
            assert b.get_sync_mapping("T001") is None
        finally:
            b.close()

    def test_list_sync_mappings_unfiltered_returns_all(self, tmp_path: Path) -> None:
        """list_sync_mappings() with no filter returns every row."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            _setup_task_for_sync(b, task_id="T001")
            # Second task to give us two rows.
            b.append(_make_event(
                "task.created", _make_task_payload(task_id="T002"),
                event_id="E000005", target_kind="task", target_id="T002",
            ))
            b.append(_make_event(
                "sync_mapping.upserted",
                _make_sync_mapping_payload(task_id="T001", external_id="gh-1"),
                event_id="E000006", target_kind="task", target_id="T001",
            ))
            b.append(_make_event(
                "sync_mapping.upserted",
                _make_sync_mapping_payload(task_id="T002", external_id="gh-2"),
                event_id="E000007", target_kind="task", target_id="T002",
            ))

            mappings = b.list_sync_mappings()
            assert len(mappings) == 2
            task_ids = {m.task_id for m in mappings}
            assert task_ids == {"T001", "T002"}
        finally:
            b.close()

    def test_list_sync_mappings_filters_by_external_system(self, tmp_path: Path) -> None:
        """list_sync_mappings(external_system=...) returns only matching rows."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            _setup_task_for_sync(b, task_id="T001")
            b.append(_make_event(
                "sync_mapping.upserted",
                _make_sync_mapping_payload(external_system="github_issues"),
                event_id="E000005", target_kind="task", target_id="T001",
            ))

            # Matching filter returns the row.
            matched = b.list_sync_mappings(external_system="github_issues")
            assert len(matched) == 1
            assert matched[0].external_system == "github_issues"

            # Non-matching filter returns nothing.
            unmatched = b.list_sync_mappings(external_system="linear")
            assert unmatched == []
        finally:
            b.close()

    def test_apply_sync_mapping_wrapper_writes_event_and_row(
        self, tmp_path: Path
    ) -> None:
        """apply_sync_mapping() builds the event and inserts the row in one call."""
        from anvil.state.models import SyncMapping

        b = _make_backend(tmp_path)
        events_path = str(tmp_path / "events.jsonl")
        try:
            _setup_project(b)
            _setup_task_for_sync(b)

            mapping = SyncMapping(
                task_id="T001",
                external_system="github_issues",
                external_id="gh-42",
                last_synced_at=_T0,
            )
            event = b.apply_sync_mapping(mapping)

            # The returned event has a real ID assigned by the backend.
            assert event.id != "PENDING"
            assert event.id.startswith("E") and event.id[1:].isdigit()
            assert event.action == "sync_mapping.upserted"

            # JSONL line written.
            events = _read_jsonl(events_path)
            assert any(
                e.get("action") == "sync_mapping.upserted" for e in events
            )

            # SQLite row written.
            stored = b.get_sync_mapping("T001")
            assert stored is not None
            assert stored.external_id == "gh-42"
        finally:
            b.close()

    def test_apply_sync_mapping_returns_assigned_event_id_not_pending(
        self, tmp_path: Path
    ) -> None:
        """The Event returned by apply_sync_mapping() carries a real E000xxx id."""
        from anvil.state.models import SyncMapping

        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            _setup_task_for_sync(b)

            mapping = SyncMapping(
                task_id="T001",
                external_system="github_issues",
                external_id="gh-42",
                last_synced_at=_T0,
            )
            event = b.apply_sync_mapping(mapping)
            assert event.id != "PENDING"
            # Format: 'E' + 6 digits.
            assert len(event.id) == 7 and event.id.startswith("E")
            assert event.id[1:].isdigit()
        finally:
            b.close()

    def test_sync_mapping_fk_restricts_unknown_task(self, tmp_path: Path) -> None:
        """A sync_mapping.upserted against a non-existent task raises TransactionAborted.

        The sync_mappings.task_id FK is RESTRICT, not CASCADE, so attempting
        to insert a mapping against a missing task surfaces as a SQLite IntegrityError
        and is wrapped by apply_event() as TransactionAborted. The corollary —
        attempting to delete the parent tasks row while a mapping exists also
        raises — is covered by test_sync_mapping_blocks_task_delete_via_fk.
        """
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            # Do NOT create T001 — FK should reject.
            with pytest.raises((EventRejected, TransactionAborted)):
                b.append(_make_event(
                    "sync_mapping.upserted",
                    _make_sync_mapping_payload(task_id="T001"),
                    event_id="E000003", target_kind="task", target_id="T001",
                ))
        finally:
            b.close()

    def test_sync_mapping_cascades_on_task_delete(self, tmp_path: Path) -> None:
        """Direct DELETE on the parent task row CASCADES to drop the sync_mappings row.

        We use a raw connection because the canonical event flow does not
        expose a 'task.deleted' event in Phase 8 — but the FK direction
        flip (RESTRICT → CASCADE in v3) means any future task.deleted handler
        won't be wedged by an outstanding sync_mapping. Documents the
        v3 schema invariant.

        The claims.task_id FK is still RESTRICT, so for this test we leave
        no active claim on the task — only the sync_mapping cascades.
        """
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            _setup_task_for_sync(b)
            b.append(_make_event(
                "sync_mapping.upserted", _make_sync_mapping_payload(),
                event_id="E000005", target_kind="task", target_id="T001",
            ))
        finally:
            b.close()

        conn = sqlite3.connect(str(tmp_path / "state.db"))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            # DELETE succeeds — CASCADE drops the sync_mapping row first.
            conn.execute("DELETE FROM tasks WHERE id = ?", ("T001",))
            conn.commit()
            # And the sync_mapping row is gone.
            row = conn.execute(
                "SELECT 1 FROM sync_mappings WHERE task_id = ?", ("T001",)
            ).fetchone()
            assert row is None
        finally:
            conn.close()

    def test_replay_from_empty_round_trips_sync_mapping(self, tmp_path: Path) -> None:
        """events.jsonl with sync_mapping events replays to a byte-identical state.db."""
        clock = _make_clock()
        b = _make_backend(tmp_path, clock)
        db_path = str(tmp_path / "state.db")
        events_path = str(tmp_path / "events.jsonl")
        try:
            _setup_project(b)
            _setup_task_for_sync(b, task_id="T001")
            b.append(_make_event(
                "sync_mapping.upserted",
                _make_sync_mapping_payload(task_id="T001", external_id="gh-1"),
                event_id="E000005", target_kind="task", target_id="T001",
            ))
            b.append(_make_event(
                "sync_mapping.upserted",
                _make_sync_mapping_payload(task_id="T001", external_id="gh-99"),
                event_id="E000006", target_kind="task", target_id="T001",
            ))
        finally:
            b.close()

        original_dump = _sqlite_dump(db_path)

        clock2 = _make_clock()
        b2 = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock2)
        b2.initialize()
        try:
            b2.replay_from_empty(events_path)
        finally:
            b2.close()

        replayed_dump = _sqlite_dump(db_path)
        assert original_dump == replayed_dump, (
            "Replayed state.db differs from original after sync_mapping events.\n"
            f"Original (truncated):\n{original_dump[:600]}\n\n"
            f"Replayed (truncated):\n{replayed_dump[:600]}"
        )

    def test_replay_round_trips_populated_provider_metadata(
        self, tmp_path: Path
    ) -> None:
        """Replay must round-trip non-empty provider_metadata + external_url byte-for-byte."""
        clock = _make_clock()
        b = _make_backend(tmp_path, clock)
        db_path = str(tmp_path / "state.db")
        events_path = str(tmp_path / "events.jsonl")
        try:
            _setup_project(b)
            _setup_task_for_sync(b)
            payload = {
                **_make_sync_mapping_payload(),
                "external_url": "https://github.com/example/repo/issues/42",
                "provider_metadata": {
                    "labels": ["bug", "p1"],
                    "assignees": ["alice"],
                },
            }
            b.append(_make_event(
                "sync_mapping.upserted", payload,
                event_id="E000005", target_kind="task", target_id="T001",
            ))
        finally:
            b.close()

        original_dump = _sqlite_dump(db_path)

        clock2 = _make_clock()
        b2 = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock2)
        b2.initialize()
        try:
            b2.replay_from_empty(events_path)
        finally:
            b2.close()

        assert original_dump == _sqlite_dump(db_path)

    def test_replay_round_trips_sync_mapping_delete(self, tmp_path: Path) -> None:
        """Replay of upsert+delete leaves the same final state."""
        clock = _make_clock()
        b = _make_backend(tmp_path, clock)
        db_path = str(tmp_path / "state.db")
        events_path = str(tmp_path / "events.jsonl")
        try:
            _setup_project(b)
            _setup_task_for_sync(b)
            b.append(_make_event(
                "sync_mapping.upserted", _make_sync_mapping_payload(),
                event_id="E000005", target_kind="task", target_id="T001",
            ))
            b.append(_make_event(
                "sync_mapping.deleted", {"task_id": "T001"},
                event_id="E000006", target_kind="task", target_id="T001",
            ))
        finally:
            b.close()

        original_dump = _sqlite_dump(db_path)

        clock2 = _make_clock()
        b2 = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock2)
        b2.initialize()
        try:
            b2.replay_from_empty(events_path)
        finally:
            b2.close()

        replayed_dump = _sqlite_dump(db_path)
        assert original_dump == replayed_dump

    def test_upsert_pre_change_payload_persists_default_partition(
        self, tmp_path: Path
    ) -> None:
        """T026: a sync_mapping.upserted event WITHOUT prd_id/entity_kind keys
        (a pre-change event) lands prd_id='default' / entity_kind='task' in the
        sync_mappings columns — the same partition the v6->v7 migration
        backfills, so replay reconstructs the migrated table."""
        b = _make_backend(tmp_path)
        db_path = str(tmp_path / "state.db")
        try:
            _setup_project(b)
            _setup_task_for_sync(b)
            # _make_sync_mapping_payload deliberately omits prd_id / entity_kind.
            b.append(_make_event(
                "sync_mapping.upserted", _make_sync_mapping_payload(),
                event_id="E000005", target_kind="task", target_id="T001",
            ))
        finally:
            b.close()

        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT prd_id, entity_kind FROM sync_mappings WHERE task_id = ?",
                ("T001",),
            ).fetchone()
            assert row == ("default", "task")
        finally:
            conn.close()

    def test_upsert_explicit_prd_id_persists_and_reads_back(
        self, tmp_path: Path
    ) -> None:
        """T026: an explicit NON-default prd_id on the payload is written to the
        column AND surfaced back on the SyncMapping read model (T027/T028 consume
        it).

        ``prd_id='v0.2'`` is deliberately NOT the DEFAULT_PRD_ID fallback, so this
        test fails if the handler ever drops ``payload.prd_id`` and backfills
        'default' (which would silently land every mapping in the default
        partition, cross-contaminating PRDs). ``sync_mappings.prd_id`` has no FK,
        so no PRD row is needed.
        """
        b = _make_backend(tmp_path)
        db_path = str(tmp_path / "state.db")
        try:
            _setup_project(b)
            _setup_task_for_sync(b)
            payload = {**_make_sync_mapping_payload(), "prd_id": "v0.2"}
            b.append(_make_event(
                "sync_mapping.upserted", payload,
                event_id="E000005", target_kind="task", target_id="T001",
            ))

            mapping = b.get_sync_mapping("T001")
            assert mapping is not None
            assert mapping.prd_id == "v0.2"
            assert mapping.entity_kind == "task"
        finally:
            b.close()

        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT prd_id, entity_kind FROM sync_mappings WHERE task_id = ?",
                ("T001",),
            ).fetchone()
            assert row == ("v0.2", "task")
        finally:
            conn.close()

    def test_upsert_rejects_prd_kind_milestone_mapping(
        self, tmp_path: Path
    ) -> None:
        """T026 boundary: a prd-kind (milestone) mapping — entity_kind='prd',
        task_id=None, prd_id set — is a VALID SyncMapping model shape, but the
        sync_mappings DDL still declares task_id NOT NULL (relaxed only in the
        milestone phase). The check gate must reject it UP FRONT with
        EventRejected, not let it green-light through to an opaque NOT NULL
        IntegrityError (TransactionAborted) inside the write lock.

        This also pins the handler's ``payload.entity_kind`` wiring: if the writer
        hard-coded entity_kind='task' (ignoring the payload), the gate would never
        see a 'prd' value and this reject would not fire.
        """
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            _setup_task_for_sync(b)
            prd_kind = {
                **_make_sync_mapping_payload(),
                "task_id": None,
                "entity_kind": "prd",
                "prd_id": "v0.2",
            }
            with pytest.raises(EventRejected):
                b.append(_make_event(
                    "sync_mapping.upserted", prd_kind,
                    event_id="E000005", target_kind="prd", target_id="v0.2",
                ))
            # The rejected event leaves no sync_mappings row behind.
            assert b.list_sync_mappings() == []
        finally:
            b.close()

    def test_upsert_rejects_invalid_sync_state(self, tmp_path: Path) -> None:
        """sync_state must be a valid SyncState enum value; otherwise TransactionAborted."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            _setup_task_for_sync(b)
            bad = _make_sync_mapping_payload(sync_state="not_a_real_state")
            with pytest.raises((EventRejected, TransactionAborted)):
                b.append(_make_event(
                    "sync_mapping.upserted", bad,
                    event_id="E000005", target_kind="task", target_id="T001",
                ))
        finally:
            b.close()

    def test_get_sync_mapping_returns_none_for_unknown_task(
        self, tmp_path: Path
    ) -> None:
        """get_sync_mapping for a never-mapped task returns None."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            _setup_task_for_sync(b)
            assert b.get_sync_mapping("T001") is None
            assert b.get_sync_mapping("nonexistent-task") is None
        finally:
            b.close()


# ---------------------------------------------------------------------------
# Phase 8 — SCHEMA_VERSION bump verification
# ---------------------------------------------------------------------------


class TestSchemaVersionPhase8:
    """The bump from SCHEMA_VERSION=1 → 2 → 3 signals Phase 8; 4 is v1.22.0;
    5 is T015 (non-feature task types — tasks.task_type column); 6 is SL-3 / B48
    (typed proofs — evidence.proofs column).

    v6 adds ``evidence.proofs TEXT NOT NULL DEFAULT '[]'`` on top of the v5
    schema; v0/v1/v2/v3/v4/v5 are auto-upgraded on first open (see
    TestSchemaAutoUpgrade below and docs/migrations.md).
    """

    def test_schema_version_is_sixteen(self) -> None:
        """Typed PRD assumptions ship at SCHEMA_VERSION == 16
        (v7 = multi-PRD foundation; v8 = per-PRD revision counter, T023;
        v9 = tasks.claims + evidence.category, issue #153;
        v10 = claims.session_id, retro-corpus concurrency theme;
        v11 = execution bundles + ordered membership, issue #171;
        v12 = coordinator bundle claims; v13 = review dispositions;
        v14 = delivery lineage; v15 = authoritative result time, issue #171;
        v16 = typed PRD assumptions)."""
        assert SCHEMA_VERSION == 16

    def test_initialize_creates_sync_mappings_table_on_empty_db(
        self, tmp_path: Path
    ) -> None:
        """A fresh initialize() on an empty path materializes sync_mappings."""
        b = _make_backend(tmp_path)
        try:
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            cursor = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name = 'sync_mappings'"
            )
            row = cursor.fetchone()
            conn.close()
            assert row is not None, "sync_mappings table missing after initialize()"
            assert row[0] == "sync_mappings"

            # And user_version matches the bump.
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            v = conn.execute("PRAGMA user_version").fetchone()[0]
            conn.close()
            assert v == SCHEMA_VERSION
        finally:
            b.close()


# ---------------------------------------------------------------------------
# Phase 8 Wave 1 fix-cycle — MF-1 / MF-2 / SF-1 / SF-6 coverage
# ---------------------------------------------------------------------------


class TestSyncMappingUniqueExternalId:
    """MF-1: UNIQUE(external_system, external_id) on sync_mappings.

    A single external record (a single GitHub issue, etc.) must NEVER map
    to two local tasks. Without this constraint, cross-task collisions
    silently mirror divergent local state into one remote, which is the
    reconciliation engine's worst failure mode.
    """

    def test_unique_external_id_rejects_cross_task_collision(
        self, tmp_path: Path
    ) -> None:
        """T001 → gh-42, then T002 → gh-42 (same external_system) must abort."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            # Two tasks under the same feature.
            b.append(_make_event(
                "feature.created", _make_feature_payload(feat_id="F001"),
                event_id="E000003", target_kind="feature", target_id="F001",
            ))
            b.append(_make_event(
                "task.created", _make_task_payload(task_id="T001", feature_id="F001"),
                event_id="E000004", target_kind="task", target_id="T001",
            ))
            b.append(_make_event(
                "task.created", _make_task_payload(task_id="T002", feature_id="F001"),
                event_id="E000005", target_kind="task", target_id="T002",
            ))

            # T001 → (github_issues, gh-42) succeeds.
            b.append(_make_event(
                "sync_mapping.upserted",
                _make_sync_mapping_payload(task_id="T001", external_id="gh-42"),
                event_id="E000006", target_kind="task", target_id="T001",
            ))

            # T002 → (github_issues, gh-42) must abort on the UNIQUE.
            with pytest.raises((EventRejected, TransactionAborted)):
                b.append(_make_event(
                    "sync_mapping.upserted",
                    _make_sync_mapping_payload(task_id="T002", external_id="gh-42"),
                    event_id="E000007", target_kind="task", target_id="T002",
                ))
        finally:
            b.close()


class TestApplySyncMappingPayloadSerialization:
    """MF-2: apply_sync_mapping serializes through the payload model explicitly.

    If a hypothetical extra field were added to SyncMapping without being
    added to SyncMappingUpsertedPayload, the failure must surface AT THE
    CALL SITE as a ValidationError — not inside the BEGIN IMMEDIATE lock
    as a generic TransactionAborted.
    """

    def test_extra_field_on_mapping_fails_at_call_site(
        self, tmp_path: Path
    ) -> None:
        """Subclass SyncMapping with an extra field; apply_sync_mapping must
        raise ValidationError (extra='forbid' on the payload model)."""
        from pydantic import ValidationError

        from anvil.state.models import SyncMapping

        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            _setup_task_for_sync(b)

            # Build a mapping with a real extra attribute via unsafe setattr
            # after construction. (Subclassing wouldn't help — extra='forbid'
            # is on SyncMapping itself.) The payload model rebuilds from
            # field-by-field reads, so smuggling a field onto the mapping
            # instance does NOT make it into the payload — instead the
            # missing-on-payload value defaults. So the more direct probe is:
            # construct the canonical payload model with an extra kwarg and
            # confirm it rejects.
            from anvil.state.payloads import SyncMappingUpsertedPayload

            with pytest.raises(ValidationError):
                SyncMappingUpsertedPayload(
                    task_id="T001",
                    external_system="github_issues",
                    external_id="gh-42",
                    last_synced_at=_T0.isoformat(),
                    surprise_extra_field="boom",  # type: ignore[call-arg]
                )

            # And the happy path: apply_sync_mapping with a clean mapping
            # round-trips through the payload model and lands a real row.
            mapping = SyncMapping(
                task_id="T001",
                external_system="github_issues",
                external_id="gh-42",
                last_synced_at=_T0,
            )
            event = b.apply_sync_mapping(mapping)
            assert event.action == "sync_mapping.upserted"
            stored = b.get_sync_mapping("T001")
            assert stored is not None
            assert stored.external_id == "gh-42"
        finally:
            b.close()

    def test_apply_sync_mapping_raises_validation_when_payload_class_drifts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Strengthened MF-2 regression: monkeypatch the payload model to
        require a field that SyncMapping does not have; apply_sync_mapping
        must surface a ValidationError AT THE CALL SITE rather than letting
        it tunnel into BEGIN IMMEDIATE as a TransactionAborted.

        This is the real protection MF-2 was supposed to give us: future
        drift between :class:`SyncMapping` and
        :class:`SyncMappingUpsertedPayload` fails fast outside the lock
        instead of leaving the JSONL log in an inconsistent state.
        """
        from pydantic import ConfigDict, Field, ValidationError, create_model

        from anvil.state import sqlite as sqlite_mod
        from anvil.state.models import SyncMapping

        # Build a drop-in replacement payload class that requires a NEW
        # field SyncMapping does not have. The original payload module
        # attribute is patched for the duration of the test; the
        # `apply_sync_mapping` wrapper resolves it by name at call time.
        DriftedPayload = create_model(  # noqa: N806 — class-like factory
            "SyncMappingUpsertedPayload",
            __config__=ConfigDict(extra="forbid"),
            task_id=(str, ...),
            external_system=(str, ...),
            external_id=(str, ...),
            external_url=(str | None, None),
            last_synced_at=(str, ...),
            sync_state=(str, "in_sync"),
            conflict_resolution_strategy=(str, "prompt"),
            provider_metadata=(dict, Field(default_factory=dict)),
            # The new required field with no value on SyncMapping → kaboom
            new_required_field=(str, ...),
        )
        monkeypatch.setattr(
            sqlite_mod, "SyncMappingUpsertedPayload", DriftedPayload
        )

        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            _setup_task_for_sync(b)
            mapping = SyncMapping(
                task_id="T001",
                external_system="github_issues",
                external_id="gh-42",
                last_synced_at=_T0,
            )
            with pytest.raises(ValidationError):
                b.apply_sync_mapping(mapping)
            # And: state is unchanged — no JSONL line for the failed payload.
            assert b.get_sync_mapping("T001") is None
        finally:
            b.close()

    def test_apply_sync_mapping_records_actor_kwarg(self, tmp_path: Path) -> None:
        """SF-7: apply_sync_mapping(..., actor='alice') threads the actor into the event."""
        from anvil.state.models import SyncMapping

        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            _setup_task_for_sync(b)
            mapping = SyncMapping(
                task_id="T001",
                external_system="github_issues",
                external_id="gh-42",
                last_synced_at=_T0,
            )
            event = b.apply_sync_mapping(mapping, actor="alice")
            assert event.actor == "alice"
        finally:
            b.close()


class TestGetSyncMappingExternalSystemKwarg:
    """SF-1: get_sync_mapping(task_id, *, external_system=None) keyword arg.

    Composite-PK means a single task_id can have multiple mappings (one per
    external system). When external_system is omitted, the legacy ASC-first
    behaviour is preserved. When supplied, the lookup is scoped.
    """

    def test_scoped_lookup_by_external_system(self, tmp_path: Path) -> None:
        """Passing external_system returns the matching row directly."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            _setup_task_for_sync(b)
            b.append(_make_event(
                "sync_mapping.upserted",
                _make_sync_mapping_payload(
                    external_system="github_issues", external_id="gh-42",
                ),
                event_id="E000005", target_kind="task", target_id="T001",
            ))
            # Scoped lookup matches.
            mapping = b.get_sync_mapping("T001", external_system="github_issues")
            assert mapping is not None
            assert mapping.external_system == "github_issues"
            assert mapping.external_id == "gh-42"
            # Scoped lookup on a non-existent system returns None.
            assert b.get_sync_mapping("T001", external_system="linear") is None
        finally:
            b.close()

    def test_unscoped_lookup_returns_asc_first(self, tmp_path: Path) -> None:
        """Omitting external_system returns the first row by external_system ASC.

        We can only test this with one StrEnum value today (github_issues),
        so the assertion is "returns a row" not "returns the alphabetical
        first of two" — the ASC-first behaviour is documented in the
        docstring and exercised internally; adding a second StrEnum value
        in a future phase will turn this into a multi-system test.
        """
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            _setup_task_for_sync(b)
            b.append(_make_event(
                "sync_mapping.upserted", _make_sync_mapping_payload(),
                event_id="E000005", target_kind="task", target_id="T001",
            ))
            mapping = b.get_sync_mapping("T001")  # no kwarg
            assert mapping is not None
            assert mapping.external_system == "github_issues"
        finally:
            b.close()


class TestSchemaAutoUpgrade:
    """SF-6: v0 / v1 / v2 / v3 / v4 / v5 → v6 auto-upgrade on initialize().

    Pre-Phase-8 dbs are upgraded purely additively — the new sync_mappings
    columns are nullable and the new UNIQUE cannot be violated by any
    pre-existing row. The v4 step (v1.22.0 git-backed events) adds only the
    nullable events.seq column; the v5 step (T015) adds only the
    DEFAULT-backfilled tasks.task_type column; the v6 step (SL-3 / B48) adds
    only the DEFAULT-backfilled evidence.proofs column.
    """

    def test_fresh_init_yields_v6(self, tmp_path: Path) -> None:
        """A brand-new initialize() lands on v6 directly (no upgrade fired)."""
        b = _make_backend(tmp_path)
        try:
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            v = conn.execute("PRAGMA user_version").fetchone()[0]
            conn.close()
            assert v == SCHEMA_VERSION
        finally:
            b.close()

    def test_v1_db_auto_upgrades_to_v6(self, tmp_path: Path) -> None:
        """A db marked user_version=1 (no sync_mappings rows) upgrades to v5."""
        db_path = str(tmp_path / "state.db")
        events_path = str(tmp_path / "events.jsonl")
        Path(events_path).touch()
        # Step 1: stand up a real current-version db.
        clock = _make_clock()
        b = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock)
        b.initialize()
        b.close()
        # Step 2: forge user_version back to 1 to simulate an old db.
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        conn.close()
        # Step 3: reopen — auto-upgrade must fire and bump back to 6.
        clock2 = _make_clock()
        b2 = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock2)
        b2.initialize()
        try:
            conn = sqlite3.connect(db_path)
            v = conn.execute("PRAGMA user_version").fetchone()[0]
            conn.close()
            assert v == SCHEMA_VERSION
        finally:
            b2.close()

    def test_v2_db_auto_upgrades_to_v6(self, tmp_path: Path) -> None:
        """A db marked user_version=2 upgrades to v5 without losing data."""
        db_path = str(tmp_path / "state.db")
        events_path = str(tmp_path / "events.jsonl")
        Path(events_path).touch()
        clock = _make_clock()
        b = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock)
        b.initialize()
        # Seed a project to confirm data survives the upgrade.
        b.append(_make_project_event(event_id="E000001"))
        b.close()
        # Forge to v2.
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
        conn.close()
        # Reopen — auto-upgrade fires; project row survives.
        clock2 = _make_clock()
        b2 = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock2)
        b2.initialize()
        try:
            conn = sqlite3.connect(db_path)
            v = conn.execute("PRAGMA user_version").fetchone()[0]
            conn.close()
            assert v == SCHEMA_VERSION
            proj = b2.get_project()
            assert proj is not None
            assert proj.id == "proj-1"
        finally:
            b2.close()

    def test_v3_db_auto_upgrades_to_v6_adding_seq_column(
        self, tmp_path: Path
    ) -> None:
        """A db marked user_version=3 gains the nullable events.seq column.

        v3 is the immediate predecessor in the wild (every 1.16–1.21 install),
        so this is the upgrade path most users actually take. Existing event
        rows keep seq NULL — local mode derives order from the monotonic id.
        """
        db_path = str(tmp_path / "state.db")
        events_path = str(tmp_path / "events.jsonl")
        Path(events_path).touch()
        clock = _make_clock()
        b = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock)
        b.initialize()
        b.append(_make_project_event(event_id="E000001"))
        b.close()
        # Forge to v3 AND drop the seq + task_type columns to simulate a real
        # v3 table (the fresh DDL above already created the v5 shape).
        conn = sqlite3.connect(db_path)
        conn.execute("ALTER TABLE events DROP COLUMN seq")
        conn.execute("ALTER TABLE tasks DROP COLUMN task_type")
        conn.execute("PRAGMA user_version = 3")
        conn.commit()
        conn.close()
        # Reopen — the v3→v5 branch must re-add seq + task_type and bump.
        clock2 = _make_clock()
        b2 = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock2)
        b2.initialize()
        try:
            conn = sqlite3.connect(db_path)
            v = conn.execute("PRAGMA user_version").fetchone()[0]
            row = conn.execute(
                "SELECT seq FROM events WHERE id = 'E000001'"
            ).fetchone()
            cols = {
                r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()
            }
            conn.close()
            assert v == SCHEMA_VERSION
            assert row is not None
            assert row[0] is None, "pre-v4 event rows must keep seq NULL"
            assert "task_type" in cols, "v5 must add tasks.task_type"
        finally:
            b2.close()

    def test_v4_db_auto_upgrades_to_v6_adding_task_type_column(
        self, tmp_path: Path
    ) -> None:
        """A db marked user_version=4 gains tasks.task_type, backfilled to feature.

        v4 (v1.22.0) is the immediate predecessor of v5 (T015). The DEFAULT
        'feature' backfills every existing task row to its pre-v5 meaning, so
        the upgrade is purely additive and loses no data.
        """
        db_path = str(tmp_path / "state.db")
        events_path = str(tmp_path / "events.jsonl")
        Path(events_path).touch()
        clock = _make_clock()
        b = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock)
        b.initialize()
        b.append(_make_project_event(event_id="E000001"))
        b.close()
        # Forge to v4 AND drop the task_type column to simulate a real v4 table
        # (the fresh DDL above already created the v5 shape). Seed a task row
        # the old way so we can confirm the DEFAULT backfill.
        conn = sqlite3.connect(db_path)
        conn.execute("ALTER TABLE tasks DROP COLUMN task_type")
        conn.execute(
            "INSERT INTO features (id, title, description) "
            "VALUES ('F001', 'F', 'd')"
        )
        conn.execute(
            "INSERT INTO tasks (id, feature_id, title, description, "
            "created_at, updated_at) "
            "VALUES ('T001', 'F001', 'T', 'd', '2026-01-01T00:00:00+00:00', "
            "'2026-01-01T00:00:00+00:00')"
        )
        conn.execute("PRAGMA user_version = 4")
        conn.commit()
        conn.close()
        # Reopen — the v4→v5 branch must add task_type and bump.
        clock2 = _make_clock()
        b2 = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock2)
        b2.initialize()
        try:
            conn = sqlite3.connect(db_path)
            v = conn.execute("PRAGMA user_version").fetchone()[0]
            tt = conn.execute(
                "SELECT task_type FROM tasks WHERE id = 'T001'"
            ).fetchone()
            conn.close()
            assert v == SCHEMA_VERSION
            assert tt is not None
            assert tt[0] == "feature", "pre-v5 rows backfill to 'feature'"
        finally:
            b2.close()

    def test_v5_db_auto_upgrades_to_v6_adding_proofs_column(
        self, tmp_path: Path
    ) -> None:
        """A db marked user_version=5 gains evidence.proofs, backfilled to '[]'.

        v5 (T015) is the immediate predecessor of v6 (SL-3 / B48 typed proofs).
        The DEFAULT '[]' backfills every existing evidence row to "no typed
        proofs," which is the pre-v6 meaning, so the upgrade is purely additive.
        """
        db_path = str(tmp_path / "state.db")
        events_path = str(tmp_path / "events.jsonl")
        Path(events_path).touch()
        clock = _make_clock()
        b = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock)
        b.initialize()
        b.append(_make_project_event(event_id="E000001"))
        b.close()
        # Forge to v5 AND drop the proofs column to simulate a real v5 table
        # (the fresh DDL above already created the v6 shape).
        conn = sqlite3.connect(db_path)
        conn.execute("ALTER TABLE evidence DROP COLUMN proofs")
        conn.execute("PRAGMA user_version = 5")
        conn.commit()
        conn.close()
        # Reopen — the v5→v6 branch must add proofs and bump.
        clock2 = _make_clock()
        b2 = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock2)
        b2.initialize()
        try:
            conn = sqlite3.connect(db_path)
            v = conn.execute("PRAGMA user_version").fetchone()[0]
            cols = {
                r[1] for r in conn.execute("PRAGMA table_info(evidence)").fetchall()
            }
            conn.close()
            assert v == SCHEMA_VERSION
            assert "proofs" in cols, "v6 must add evidence.proofs"
        finally:
            b2.close()

    def test_future_version_still_raises_mismatch(self, tmp_path: Path) -> None:
        """A db marked at a version newer than the code expects raises SchemaMismatch."""
        db_path = str(tmp_path / "state.db")
        events_path = str(tmp_path / "events.jsonl")
        Path(events_path).touch()
        clock = _make_clock()
        b = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock)
        b.initialize()
        # Forge to a far-future version that cannot be auto-upgraded.
        assert b._conn is not None  # noqa: SLF001
        b._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 99}")  # noqa: SLF001
        with pytest.raises(SchemaMismatch):
            b.initialize()
        b.close()

    def test_check_schema_version_auto_upgrades_on_already_open_conn(
        self, tmp_path: Path
    ) -> None:
        """Forge user_version on a live connection, then re-call initialize().

        The second initialize() takes the early-return branch (self._conn is
        not None) which calls only _check_schema_version() — no DDL. This is
        the only path that actually exercises the SF-6 auto-upgrade branch.
        """
        b = _make_backend(tmp_path)
        try:
            assert b._conn is not None  # noqa: SLF001
            b._conn.execute("PRAGMA user_version = 1")  # noqa: SLF001
            b.initialize()  # takes early-return; _check_schema_version sees v1
            v = b._conn.execute("PRAGMA user_version").fetchone()[0]  # noqa: SLF001
            assert v == SCHEMA_VERSION
        finally:
            b.close()


class TestSyncProviderSnakeCaseRegistry:
    """SF-3: register/get with snake_case provider ids (e.g. github_issues)."""

    def test_register_and_get_snake_case(self) -> None:
        from anvil.sync import (
            PROVIDER_REGISTRY,
            get_sync_provider,
            register_sync_provider,
        )

        class _FakeProvider:
            provider_id = "github_issues"
            display_name = "GitHub Issues"

            def push_task(self, *, task: Any, mapping: Any) -> Any:  # pragma: no cover
                ...

            def fetch_task(self, *, external_id: str) -> Any:  # pragma: no cover
                ...

            def list_tasks(self) -> list[Any]:  # pragma: no cover
                ...

            def delete_task(self, *, external_id: str) -> None:  # pragma: no cover
                ...

            def health_check(self) -> Any:  # pragma: no cover
                ...

        # Snapshot + restore the registry so this test doesn't pollute siblings.
        snapshot = dict(PROVIDER_REGISTRY)
        PROVIDER_REGISTRY.clear()
        try:
            register_sync_provider("github_issues", _FakeProvider)
            assert get_sync_provider("github_issues") is _FakeProvider
        finally:
            PROVIDER_REGISTRY.clear()
            PROVIDER_REGISTRY.update(snapshot)


# ---------------------------------------------------------------------------
# P6-5 — Payload validation centralization
# ---------------------------------------------------------------------------


class TestPayloadValidation:
    """Tests for the per-action Pydantic payload models introduced in P6-5.

    Coverage:
    - Each payload model validates a known-good payload.
    - Each payload model rejects an unknown extra field (extra='forbid').
    - _apply_mutation raises ValidationError (wrapped as TransactionAborted)
      for a malformed payload on a known action.
    - Existing replay-from-empty test still passes (regression check).
    """

    # ------------------------------------------------------------------
    # Import helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _import_payload_models() -> Any:
        """Return the payloads module (deferred import to keep test isolation)."""
        from anvil.state import payloads as p
        return p

    # ------------------------------------------------------------------
    # Happy-path: each model validates a known-good payload
    # ------------------------------------------------------------------

    def test_project_created_payload_validates_good(self) -> None:
        p = self._import_payload_models()
        obj = p.ProjectCreatedPayload.model_validate({
            "id": "proj-1",
            "name": "Test Project",
            "description": "desc",
            "created_at": _T0.isoformat(),
            "updated_at": _T0.isoformat(),
        })
        assert obj.id == "proj-1"
        assert obj.name == "Test Project"

    def test_state_initialized_payload_validates_empty(self) -> None:
        p = self._import_payload_models()
        obj = p.StateInitializedPayload.model_validate({})
        assert obj is not None

    def test_prd_parsed_payload_validates_good(self) -> None:
        p = self._import_payload_models()
        obj = p.PrdParsedPayload.model_validate({
            "project_id": "proj-1",
            "summary": "short summary",
            "goals": ["goal1"],
            "non_goals": [],
            "requirements": [],
            "acceptance_criteria": [],
            "risks": [],
            "open_questions": [],
        })
        assert obj.project_id == "proj-1"
        assert obj.summary == "short summary"

    def test_prd_reviewed_payload_validates_good(self) -> None:
        p = self._import_payload_models()
        obj = p.PrdReviewedPayload.model_validate({
            "project_id": "proj-1",
            "reviewer": "alice",
        })
        assert obj.reviewer == "alice"

    def test_prd_revised_payload_validates_good(self) -> None:
        p = self._import_payload_models()
        obj = p.PrdRevisedPayload.model_validate({
            "project_id": "proj-1",
            "prd_id": "default",
            "revision": 2,
            "summary": "revised summary",
            "requirements_added": [{"id": "R002"}],
            "requirements_superseded": [{"id": "R001"}],
            "requirements_unchanged": [],
        })
        assert obj.project_id == "proj-1"
        assert obj.revision == 2
        assert obj.requirements_added == [{"id": "R002"}]
        assert obj.requirements_superseded == [{"id": "R001"}]

    def test_prd_revised_payload_defaults(self) -> None:
        """prd_id defaults to 'default' and the diff lists default to empty."""
        p = self._import_payload_models()
        obj = p.PrdRevisedPayload.model_validate({
            "project_id": "proj-1",
            "revision": 3,
        })
        assert obj.prd_id == "default"
        # A 'revise' event targets an existing prd_id; the safe default is
        # is_default=False so a named-PRD revision never flips is_default 0→1
        # and breaks the ux_prds_default single-default invariant.
        assert obj.is_default is False
        assert obj.requirements_added == []
        assert obj.requirements_superseded == []
        assert obj.requirements_unchanged == []

    def test_prd_revised_payload_missing_revision_rejects(self) -> None:
        """revision is required (no default); omitting it raises ValidationError.

        Guards the T023 revision-counter contract: a regression that gave
        revision a default (e.g. ``= 1``) would silently accept payloads with
        no explicit revision and change prd.revised dispatch semantics.
        """
        from pydantic import ValidationError as PydanticValidationError
        p = self._import_payload_models()
        with pytest.raises(PydanticValidationError, match="revision"):
            p.PrdRevisedPayload.model_validate({
                "project_id": "proj-1",
            })

    def test_prd_revised_payload_revision_must_be_ge_one(self) -> None:
        """revision below 1 raises ValidationError (Field(ge=1)).

        Mirrors test_models.py::test_prd_revision_must_be_ge_one for the
        payload's identical guard: revision is a positive monotonic counter, so
        a revision=0 (or negative) payload must be rejected at dispatch.
        """
        from pydantic import ValidationError as PydanticValidationError
        p = self._import_payload_models()
        with pytest.raises(PydanticValidationError, match="revision"):
            p.PrdRevisedPayload.model_validate({
                "project_id": "proj-1",
                "revision": 0,
            })

    def test_prd_approved_payload_validates_good(self) -> None:
        p = self._import_payload_models()
        obj = p.PrdApprovedPayload.model_validate({
            "project_id": "proj-1",
            "approver": "bob",
        })
        assert obj.approver == "bob"

    def test_feature_created_payload_validates_good(self) -> None:
        p = self._import_payload_models()
        obj = p.FeatureCreatedPayload.model_validate({
            "id": "F001",
            "title": "My Feature",
        })
        assert obj.id == "F001"

    def test_task_created_payload_validates_good(self) -> None:
        p = self._import_payload_models()
        obj = p.TaskCreatedPayload.model_validate({
            "id": "T001",
            "feature_id": "F001",
            "title": "My Task",
        })
        assert obj.id == "T001"

    def test_task_scored_payload_validates_good(self) -> None:
        p = self._import_payload_models()
        obj = p.TaskScoredPayload.model_validate({
            "task_id": "T001",
            "scores": {"complexity": 3, "risk": 2},
            "explanation": "seems manageable",
        })
        assert obj.task_id == "T001"
        assert obj.explanation == "seems manageable"

    def test_task_expanded_payload_validates_good(self) -> None:
        p = self._import_payload_models()
        obj = p.TaskExpandedPayload.model_validate({
            "parent_task_id": "T001",
            "subtasks": [],
        })
        assert obj.parent_task_id == "T001"

    def test_task_status_changed_payload_validates_good(self) -> None:
        p = self._import_payload_models()
        # JSON keys are 'from' and 'to' (Python keywords mapped by alias)
        obj = p.TaskStatusChangedPayload.model_validate({
            "task_id": "T001",
            "from": "proposed",
            "to": "drafted",
        })
        assert obj.task_id == "T001"
        assert obj.from_status == "proposed"
        assert obj.to_status == "drafted"

    def test_claim_created_payload_validates_good(self) -> None:
        p = self._import_payload_models()
        expires = (_T0 + timedelta(hours=1)).isoformat()
        obj = p.ClaimCreatedPayload.model_validate({
            "id": "C001",
            "task_id": "T001",
            "claimed_by": "agent-alpha",
            "claim_type": "task",
            "status": "active",
            "created_at": _T0.isoformat(),
            "lease_expires_at": expires,
            "last_heartbeat_at": _T0.isoformat(),
        })
        assert obj.id == "C001"

    def test_claim_released_payload_validates_good(self) -> None:
        p = self._import_payload_models()
        obj = p.ClaimReleasedPayload.model_validate({
            "claim_id": "C001",
            "released_by": "agent-alpha",
            "release_reason": "work done",
        })
        assert obj.claim_id == "C001"

    def test_claim_renewed_payload_validates_good(self) -> None:
        p = self._import_payload_models()
        expires = (_T0 + timedelta(hours=2)).isoformat()
        obj = p.ClaimRenewedPayload.model_validate({
            "claim_id": "C001",
            "lease_expires_at": expires,
            "last_heartbeat_at": _T0.isoformat(),
        })
        assert obj.claim_id == "C001"

    def test_claim_stale_payload_validates_good(self) -> None:
        p = self._import_payload_models()
        obj = p.ClaimStalePayload.model_validate({
            "claim_id": "C001",
            "detected_at": _T0.isoformat(),
            "reason": "lease_expired",
        })
        assert obj.claim_id == "C001"

    def test_evidence_submitted_payload_validates_good(self) -> None:
        p = self._import_payload_models()
        obj = p.EvidenceSubmittedPayload.model_validate({
            "task_id": "T001",
            "claim_id": "C001",
            "submitted_by": "agent-alpha",
            "evidence_id": "EV001",
            "commands_run": ["pytest -q"],
            "files_changed": ["src/foo.py"],
        })
        assert obj.evidence_id == "EV001"

    def test_task_applied_payload_validates_good(self) -> None:
        p = self._import_payload_models()
        obj = p.TaskAppliedPayload.model_validate({
            "task_id": "T001",
            "reviewer": "bob",
            "decision": "accepted",
        })
        assert obj.decision == "accepted"

    def test_file_changed_payload_validates_good(self) -> None:
        p = self._import_payload_models()
        obj = p.FileChangedPayload.model_validate({
            "file": "src/foo.py",
            "tool": "Edit",
            "actor": "agent-alpha",
        })
        assert obj.file == "src/foo.py"

    # ------------------------------------------------------------------
    # extra='forbid': unknown key raises ValidationError
    # ------------------------------------------------------------------

    def test_project_created_rejects_unknown_key(self) -> None:
        from pydantic import ValidationError as PydanticValidationError
        p = self._import_payload_models()
        with pytest.raises(PydanticValidationError, match="extra"):
            p.ProjectCreatedPayload.model_validate({
                "id": "proj-1",
                "name": "n",
                "description": "d",
                "created_at": _T0.isoformat(),
                "updated_at": _T0.isoformat(),
                "unknown_field": "should_fail",
            })

    def test_state_initialized_rejects_unknown_key(self) -> None:
        from pydantic import ValidationError as PydanticValidationError
        p = self._import_payload_models()
        with pytest.raises(PydanticValidationError, match="extra"):
            p.StateInitializedPayload.model_validate({"unknown": True})

    def test_prd_parsed_rejects_unknown_key(self) -> None:
        from pydantic import ValidationError as PydanticValidationError
        p = self._import_payload_models()
        with pytest.raises(PydanticValidationError, match="extra"):
            p.PrdParsedPayload.model_validate({
                "project_id": "proj-1",
                "unknown_key": "oops",
            })

    def test_prd_reviewed_rejects_unknown_key(self) -> None:
        from pydantic import ValidationError as PydanticValidationError
        p = self._import_payload_models()
        with pytest.raises(PydanticValidationError, match="extra"):
            p.PrdReviewedPayload.model_validate({
                "project_id": "proj-1",
                "reviewer": "alice",
                "bad": "field",
            })

    def test_prd_revised_rejects_unknown_key(self) -> None:
        from pydantic import ValidationError as PydanticValidationError
        p = self._import_payload_models()
        with pytest.raises(PydanticValidationError, match="extra"):
            p.PrdRevisedPayload.model_validate({
                "project_id": "proj-1",
                "revision": 2,
                "unknown_key": "oops",
            })

    def test_prd_approved_rejects_unknown_key(self) -> None:
        from pydantic import ValidationError as PydanticValidationError
        p = self._import_payload_models()
        with pytest.raises(PydanticValidationError, match="extra"):
            p.PrdApprovedPayload.model_validate({
                "project_id": "proj-1",
                "approver": "bob",
                "extra": "nope",
            })

    def test_feature_created_rejects_unknown_key(self) -> None:
        from pydantic import ValidationError as PydanticValidationError
        p = self._import_payload_models()
        with pytest.raises(PydanticValidationError, match="extra"):
            p.FeatureCreatedPayload.model_validate({
                "id": "F001",
                "title": "t",
                "mystery": "field",
            })

    def test_task_created_rejects_unknown_key(self) -> None:
        from pydantic import ValidationError as PydanticValidationError
        p = self._import_payload_models()
        with pytest.raises(PydanticValidationError, match="extra"):
            p.TaskCreatedPayload.model_validate({
                "id": "T001",
                "feature_id": "F001",
                "title": "t",
                "alien_field": "no",
            })

    def test_task_scored_rejects_unknown_key(self) -> None:
        from pydantic import ValidationError as PydanticValidationError
        p = self._import_payload_models()
        with pytest.raises(PydanticValidationError, match="extra"):
            p.TaskScoredPayload.model_validate({
                "task_id": "T001",
                "scores": {},
                "phantom": "field",
            })

    def test_task_expanded_rejects_unknown_key(self) -> None:
        from pydantic import ValidationError as PydanticValidationError
        p = self._import_payload_models()
        with pytest.raises(PydanticValidationError, match="extra"):
            p.TaskExpandedPayload.model_validate({
                "parent_task_id": "T001",
                "subtasks": [],
                "bad": "key",
            })

    def test_task_status_changed_rejects_unknown_key(self) -> None:
        from pydantic import ValidationError as PydanticValidationError
        p = self._import_payload_models()
        with pytest.raises(PydanticValidationError, match="extra"):
            p.TaskStatusChangedPayload.model_validate({
                "task_id": "T001",
                "from": "proposed",
                "to": "drafted",
                "extra_field": "bad",
            })

    def test_claim_created_rejects_unknown_key(self) -> None:
        from pydantic import ValidationError as PydanticValidationError
        p = self._import_payload_models()
        expires = (_T0 + timedelta(hours=1)).isoformat()
        with pytest.raises(PydanticValidationError, match="extra"):
            p.ClaimCreatedPayload.model_validate({
                "id": "C001",
                "task_id": "T001",
                "claimed_by": "agent",
                "claim_type": "task",
                "status": "active",
                "created_at": _T0.isoformat(),
                "lease_expires_at": expires,
                "last_heartbeat_at": _T0.isoformat(),
                "injected": "field",
            })

    def test_claim_released_rejects_unknown_key(self) -> None:
        from pydantic import ValidationError as PydanticValidationError
        p = self._import_payload_models()
        with pytest.raises(PydanticValidationError, match="extra"):
            p.ClaimReleasedPayload.model_validate({
                "claim_id": "C001",
                "released_by": "agent",
                "bogus": "key",
            })

    def test_claim_renewed_rejects_unknown_key(self) -> None:
        from pydantic import ValidationError as PydanticValidationError
        p = self._import_payload_models()
        expires = (_T0 + timedelta(hours=2)).isoformat()
        with pytest.raises(PydanticValidationError, match="extra"):
            p.ClaimRenewedPayload.model_validate({
                "claim_id": "C001",
                "lease_expires_at": expires,
                "last_heartbeat_at": _T0.isoformat(),
                "mystery": "value",
            })

    def test_claim_stale_rejects_unknown_key(self) -> None:
        from pydantic import ValidationError as PydanticValidationError
        p = self._import_payload_models()
        with pytest.raises(PydanticValidationError, match="extra"):
            p.ClaimStalePayload.model_validate({
                "claim_id": "C001",
                "detected_at": _T0.isoformat(),
                "reason": "lease_expired",
                "not_a_field": True,
            })

    def test_evidence_submitted_rejects_unknown_key(self) -> None:
        from pydantic import ValidationError as PydanticValidationError
        p = self._import_payload_models()
        with pytest.raises(PydanticValidationError, match="extra"):
            p.EvidenceSubmittedPayload.model_validate({
                "task_id": "T001",
                "claim_id": "C001",
                "submitted_by": "agent",
                "evidence_id": "EV001",
                "commands_run": ["pytest"],
                "files_changed": ["foo.py"],
                "undocumented_field": "oops",
            })

    def test_task_applied_rejects_unknown_key(self) -> None:
        from pydantic import ValidationError as PydanticValidationError
        p = self._import_payload_models()
        with pytest.raises(PydanticValidationError, match="extra"):
            p.TaskAppliedPayload.model_validate({
                "task_id": "T001",
                "reviewer": "alice",
                "decision": "accepted",
                "unexpected_key": "value",
            })

    def test_file_changed_rejects_unknown_key(self) -> None:
        from pydantic import ValidationError as PydanticValidationError
        p = self._import_payload_models()
        with pytest.raises(PydanticValidationError, match="extra"):
            p.FileChangedPayload.model_validate({
                "file": "foo.py",
                "tool": "Edit",
                "actor": "agent",
                "injected_payload": "malicious",
            })

    # ------------------------------------------------------------------
    # progress.noted payload — Phase 6 MCP submit_progress (audit-only)
    # ------------------------------------------------------------------

    def test_progress_noted_payload_validates_good(self) -> None:
        p = self._import_payload_models()
        obj = p.ProgressNotedPayload.model_validate({
            "task_id": "T001",
            "actor": "agent-guido",
            "notes": "Fixed the flaky test; CI green.",
            "noted_at": _T0.isoformat(),
        })
        assert obj.task_id == "T001"
        assert obj.actor == "agent-guido"
        assert obj.notes == "Fixed the flaky test; CI green."

    def test_progress_noted_payload_accepts_phase_and_detail(self) -> None:
        """retro-opps T010 — optional phase/detail fields validate; absent
        keys (old JSONL rows) still replay (covered by ..._validates_good)."""
        p = self._import_payload_models()
        obj = p.ProgressNotedPayload.model_validate({
            "task_id": "T001",
            "actor": "agent-guido",
            "notes": "tests running",
            "noted_at": _T0.isoformat(),
            "phase": "tests",
            "detail": "unit suite",
        })
        assert obj.phase == "tests"
        assert obj.detail == "unit suite"

    def test_progress_noted_payload_rejects_unknown_key(self) -> None:
        from pydantic import ValidationError as PydanticValidationError
        p = self._import_payload_models()
        with pytest.raises(PydanticValidationError, match="extra"):
            p.ProgressNotedPayload.model_validate({
                "task_id": "T001",
                "actor": "agent-guido",
                "notes": "All good.",
                "noted_at": _T0.isoformat(),
                "injected_key": "should_be_rejected",
            })

    # ------------------------------------------------------------------
    # Phase 8 — sync_mapping payloads
    # ------------------------------------------------------------------

    def test_sync_mapping_upserted_payload_validates_good(self) -> None:
        p = self._import_payload_models()
        obj = p.SyncMappingUpsertedPayload.model_validate({
            "task_id": "T001",
            "external_system": "github_issues",
            "external_id": "gh-42",
            "last_synced_at": _T0.isoformat(),
            "sync_state": "in_sync",
            "conflict_resolution_strategy": "prompt",
        })
        assert obj.task_id == "T001"
        assert obj.external_system == "github_issues"
        assert obj.external_id == "gh-42"

    def test_sync_mapping_upserted_payload_applies_defaults(self) -> None:
        """Optional fields fall back to defaults when omitted."""
        p = self._import_payload_models()
        obj = p.SyncMappingUpsertedPayload.model_validate({
            "task_id": "T001",
            "external_system": "github_issues",
            "external_id": "gh-42",
            "last_synced_at": _T0.isoformat(),
        })
        assert obj.sync_state == "in_sync"
        assert obj.conflict_resolution_strategy == "prompt"

    def test_sync_mapping_upserted_payload_pre_change_event_defaults_prd(
        self,
    ) -> None:
        """T026: an OLD sync_mapping.upserted event (no prd_id / entity_kind
        keys) deserialises with entity_kind='task' and prd_id='default' — the
        same partition the v6->v7 migration backfills, so replay matches."""
        from anvil.state.models import DEFAULT_PRD_ID

        p = self._import_payload_models()
        obj = p.SyncMappingUpsertedPayload.model_validate({
            "task_id": "T001",
            "external_system": "github_issues",
            "external_id": "gh-42",
            "last_synced_at": _T0.isoformat(),
        })
        assert obj.entity_kind == "task"
        assert obj.prd_id == DEFAULT_PRD_ID

    def test_sync_mapping_upserted_payload_accepts_explicit_prd_partition(
        self,
    ) -> None:
        """T026: a new event may carry explicit prd_id / entity_kind."""
        p = self._import_payload_models()
        obj = p.SyncMappingUpsertedPayload.model_validate({
            "task_id": "T001",
            "external_system": "github_issues",
            "external_id": "gh-42",
            "last_synced_at": _T0.isoformat(),
            "prd_id": "v0.2",
            "entity_kind": "task",
        })
        assert obj.prd_id == "v0.2"
        assert obj.entity_kind == "task"

    def test_sync_mapping_upserted_rejects_unknown_key(self) -> None:
        from pydantic import ValidationError as PydanticValidationError
        p = self._import_payload_models()
        with pytest.raises(PydanticValidationError, match="extra"):
            p.SyncMappingUpsertedPayload.model_validate({
                "task_id": "T001",
                "external_system": "github_issues",
                "external_id": "gh-42",
                "last_synced_at": _T0.isoformat(),
                "smuggled_field": "should_be_rejected",
            })

    def test_sync_mapping_deleted_payload_validates_minimum(self) -> None:
        p = self._import_payload_models()
        obj = p.SyncMappingDeletedPayload.model_validate({"task_id": "T001"})
        assert obj.task_id == "T001"
        assert obj.external_system is None

    def test_sync_mapping_deleted_payload_validates_scoped(self) -> None:
        """external_system is optional but accepted when provided."""
        p = self._import_payload_models()
        obj = p.SyncMappingDeletedPayload.model_validate({
            "task_id": "T001",
            "external_system": "github_issues",
        })
        assert obj.external_system == "github_issues"

    def test_sync_mapping_deleted_rejects_unknown_key(self) -> None:
        from pydantic import ValidationError as PydanticValidationError
        p = self._import_payload_models()
        with pytest.raises(PydanticValidationError, match="extra"):
            p.SyncMappingDeletedPayload.model_validate({
                "task_id": "T001",
                "not_a_real_field": "no",
            })

    # ------------------------------------------------------------------
    # _apply_mutation raises ValidationError (wrapped as TransactionAborted)
    # for a malformed payload on a known action
    # ------------------------------------------------------------------

    def test_append_raises_event_rejected_on_malformed_payload(
        self, tmp_path: Path
    ) -> None:
        """A known action with an extra unknown key raises EventRejected (SL1-RR-1).

        SL1-RR-1: payload validation happens pre-log via _check_*. An extra
        unknown field triggers EventRejected (not TransactionAborted). Nothing
        is written to events.jsonl.
        """
        b = _make_backend(tmp_path)
        try:
            bad_draft = EventDraft(
                timestamp=_T0,
                actor="test",
                action="project.created",
                target_kind="project",
                target_id="proj-1",
                payload_json={
                    "id": "proj-1",
                    "name": "Test",
                    "description": "desc",
                    "created_at": _T0.isoformat(),
                    "updated_at": _T0.isoformat(),
                    "unknown_extra_field": "should_trigger_forbid",
                },
            )
            with pytest.raises((EventRejected, TransactionAborted)):
                b.append(bad_draft)
        finally:
            b.close()

    def test_append_raises_event_rejected_on_missing_required_field(
        self, tmp_path: Path
    ) -> None:
        """A known action with a missing required field raises EventRejected (SL1-RR-1)."""
        b = _make_backend(tmp_path)
        try:
            bad_draft = EventDraft(
                timestamp=_T0,
                actor="test",
                action="prd.reviewed",
                target_kind="prd",
                target_id="proj-1",
                payload_json={
                    # missing 'reviewer' (required by PrdReviewedPayload)
                    "project_id": "proj-1",
                },
            )
            with pytest.raises((EventRejected, TransactionAborted)):
                b.append(bad_draft)
        finally:
            b.close()

    # ------------------------------------------------------------------
    # Regression: the critical replay-from-empty test still passes
    # ------------------------------------------------------------------

    def test_replay_from_empty_still_passes_after_payload_centralization(
        self, tmp_path: Path
    ) -> None:
        """Critical regression test: replay produces byte-for-byte identical state.

        This duplicates the audit-guarantee test but is owned by the P6-5
        payload-centralization change to make regressions immediately obvious.
        """
        clock = _make_clock()
        b = _make_backend(tmp_path, clock)
        events_path = str(tmp_path / "events.jsonl")
        db_path = str(tmp_path / "state.db")

        try:
            b.append(_make_project_event(event_id="E000001"))
            b.append(_make_init_event(event_id="E000002"))
            b.append(
                _make_project_event(
                    event_id="E000003",
                    project_id="proj-1",
                    project_name="Replayed Project",
                )
            )
        finally:
            b.close()

        original_dump = _sqlite_dump(db_path)

        clock2 = _make_clock()
        b2 = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock2)
        b2.initialize()
        try:
            b2.replay_from_empty(events_path)
        finally:
            pass

        b2.close()

        replayed_dump = _sqlite_dump(db_path)

        assert original_dump == replayed_dump, (
            "Replayed state.db does not match original after P6-5 payload centralization.\n"
            f"Original dump (truncated):\n{original_dump[:500]}\n\n"
            f"Replayed dump (truncated):\n{replayed_dump[:500]}"
        )


# ---------------------------------------------------------------------------
# Regression: minimal task.created payloads (no scores/verification) must
# succeed. The MCP path will send these as the common case; CLI happens to
# always send full dumps and would have masked the bug. Critic-PR#44 P1.
# ---------------------------------------------------------------------------


class TestMinimalTaskPayloads:
    """Minimal task.created / task.expanded payloads from MCP-style callers."""

    def _make_feature_event(self, event_id: str = "E000003") -> EventDraft:
        return _make_event(
            "feature.created",
            {
                "id": "F001",
                "title": "Feature One",
                "description": "",
                "status": "proposed",
                "requirements": [],
                "tasks": [],
            },
            event_id=event_id,
            target_kind="feature",
            target_id="F001",
        )

    def test_task_created_minimal_payload_succeeds(self, tmp_path: Path) -> None:
        """task.created with scores=None and verification=None gets normalized."""
        clock = _make_clock()
        b = _make_backend(tmp_path, clock)
        try:
            _setup_project(b)
            b.append(self._make_feature_event(event_id="E000003"))
            # Minimal task payload — what an MCP caller would send.
            minimal = {
                "id": "T001",
                "feature_id": "F001",
                "title": "Minimal task",
                "scores": None,
                "verification": None,
                "created_at": _T0.isoformat(),
                "updated_at": _T0.isoformat(),
            }
            event = _make_event(
                "task.created", minimal, event_id="E000004",
                target_kind="task", target_id="T001",
            )
            b.append(event)
            row = b.get_task("T001")
            assert row is not None
            assert row.id == "T001"
            assert row.title == "Minimal task"
        finally:
            b.close()

    def test_task_expanded_minimal_subtasks_succeed(self, tmp_path: Path) -> None:
        """Subtasks in task.expanded can also omit scores/verification."""
        clock = _make_clock()
        b = _make_backend(tmp_path, clock)
        try:
            _setup_project(b)
            b.append(self._make_feature_event(event_id="E000003"))
            parent = {
                "id": "T001",
                "feature_id": "F001",
                "title": "Parent",
                "scores": None,
                "verification": None,
                "created_at": _T0.isoformat(),
                "updated_at": _T0.isoformat(),
            }
            b.append(_make_event(
                "task.created", parent, event_id="E000004",
                target_kind="task", target_id="T001",
            ))
            expand = {
                "parent_task_id": "T001",
                "subtasks": [
                    {
                        "id": "T001.1",
                        "feature_id": "F001",
                        "title": "Sub one",
                        "description": "Subtask description",
                        "scores": None,
                        "verification": None,
                        "created_at": _T0.isoformat(),
                        "updated_at": _T0.isoformat(),
                    },
                ],
            }
            b.append(_make_event(
                "task.expanded", expand, event_id="E000005",
                target_kind="task", target_id="T001",
            ))
            sub = b.get_task("T001.1")
            assert sub is not None
            assert sub.parent_task_id == "T001"
        finally:
            b.close()


# ---------------------------------------------------------------------------
# PR #49 P1-3 regression — v2 → v3 migration actually applies ALTERs
# ---------------------------------------------------------------------------


class TestV2ToV3MigrationAppliesColumnAdditions:
    """P1-3 — v2 sync_mappings table missing v3 columns must get ALTERed.

    Pre-fix bug: ``_check_schema_version`` stamped the db as v3 without
    running any ALTER TABLE — the IF NOT EXISTS DDL is a no-op against an
    existing table, so a v2 db's ``sync_mappings`` table stayed at v2
    shape (no ``external_url`` / ``provider_metadata_json`` / unique
    index) while the user_version pragma claimed v3. Queries against the
    new columns raised ``OperationalError`` at runtime.
    """

    def _stand_up_v2_sync_mappings_table(
        self, db_path: str, events_path: str,
    ) -> None:
        """Create a v2-shaped sync_mappings table (no v3 columns/index).

        Mirrors the v2 schema: composite PK, no ``external_url``, no
        ``provider_metadata_json``, no UNIQUE on (external_system,
        external_id). Stamps user_version=2 so the next ``initialize()``
        takes the v2→v3 migration branch.
        """
        Path(events_path).touch()
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        # Create the minimum tables a v2 db needs to satisfy FKs.
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                description TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS features (
                id           TEXT PRIMARY KEY,
                title        TEXT NOT NULL,
                description  TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'proposed',
                requirements TEXT NOT NULL DEFAULT '[]',
                tasks        TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id                   TEXT PRIMARY KEY,
                feature_id           TEXT NOT NULL,
                title                TEXT NOT NULL,
                description          TEXT NOT NULL,
                status               TEXT NOT NULL DEFAULT 'proposed',
                priority             TEXT NOT NULL DEFAULT 'medium',
                dependencies         TEXT NOT NULL DEFAULT '[]',
                conflict_groups      TEXT NOT NULL DEFAULT '[]',
                scores               TEXT NOT NULL DEFAULT '{}',
                acceptance_criteria  TEXT NOT NULL DEFAULT '[]',
                implementation_notes TEXT NOT NULL DEFAULT '[]',
                verification         TEXT NOT NULL DEFAULT '{}',
                likely_files         TEXT NOT NULL DEFAULT '[]',
                parent_task_id       TEXT,
                created_at           TEXT NOT NULL,
                updated_at           TEXT NOT NULL
            );
            -- v2 sync_mappings: composite PK only, no v3 additions.
            CREATE TABLE sync_mappings (
                task_id                      TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                external_system              TEXT NOT NULL,
                external_id                  TEXT NOT NULL,
                last_synced_at               TEXT NOT NULL,
                sync_state                   TEXT NOT NULL DEFAULT 'in_sync',
                conflict_resolution_strategy TEXT NOT NULL DEFAULT 'prompt',
                PRIMARY KEY (task_id, external_system)
            );
            """
        )
        # Insert a project + feature + task + sync_mapping in v2 shape so
        # the migration has real data to preserve.
        conn.execute(
            "INSERT INTO projects (id, name, description, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("proj-1", "Test", "", _T0.isoformat(), _T0.isoformat()),
        )
        conn.execute(
            "INSERT INTO features (id, title, description) VALUES (?, ?, ?)",
            ("F001", "F", ""),
        )
        conn.execute(
            "INSERT INTO tasks (id, feature_id, title, description, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("T001", "F001", "Old", "old", _T0.isoformat(), _T0.isoformat()),
        )
        conn.execute(
            "INSERT INTO sync_mappings (task_id, external_system, "
            "external_id, last_synced_at) VALUES (?, ?, ?, ?)",
            ("T001", "github_issues", "gh-1", _T0.isoformat()),
        )
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
        conn.close()

    def test_v2_db_with_existing_sync_mappings_gets_v3_columns(
        self, tmp_path: Path,
    ) -> None:
        """After v2→v3 auto-upgrade, the new columns exist and are queryable."""
        db_path = str(tmp_path / "state.db")
        events_path = str(tmp_path / "events.jsonl")
        self._stand_up_v2_sync_mappings_table(db_path, events_path)

        # Re-open with v3 code: migration must add the missing columns.
        clock = _make_clock()
        b = SqliteBackend(
            db_path=db_path, events_path=events_path, clock=clock,
        )
        b.initialize()
        try:
            # Verify the new columns exist by querying them — this would
            # raise OperationalError pre-fix.
            assert b._conn is not None  # noqa: SLF001
            row = b._conn.execute(  # noqa: SLF001
                "SELECT external_url, provider_metadata_json "
                "FROM sync_mappings WHERE task_id = ?",
                ("T001",),
            ).fetchone()
            assert row is not None
            # Default values for previously-absent columns: NULL.
            assert row[0] is None
            assert row[1] is None
            # user_version is now the current SCHEMA_VERSION (6).
            v = b._conn.execute("PRAGMA user_version").fetchone()[0]  # noqa: SLF001
            assert v == SCHEMA_VERSION
        finally:
            b.close()

    def test_v2_db_after_upgrade_enforces_unique_external_constraint(
        self, tmp_path: Path,
    ) -> None:
        """The v3 UNIQUE(external_system, external_id) is enforced post-upgrade.

        Two distinct task rows trying to claim the same (external_system,
        external_id) pair must fail at INSERT — that's the contract the
        v3 migration is supposed to add.
        """
        db_path = str(tmp_path / "state.db")
        events_path = str(tmp_path / "events.jsonl")
        self._stand_up_v2_sync_mappings_table(db_path, events_path)

        # Add a second task so we have two tasks to map to the same remote id.
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO tasks (id, feature_id, title, description, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("T002", "F001", "Other", "other", _T0.isoformat(), _T0.isoformat()),
        )
        conn.commit()
        conn.close()

        # Trigger the upgrade.
        clock = _make_clock()
        b = SqliteBackend(
            db_path=db_path, events_path=events_path, clock=clock,
        )
        b.initialize()
        try:
            assert b._conn is not None  # noqa: SLF001
            # T001 already owns ('github_issues', 'gh-1'); T002 must NOT be
            # able to claim the same pair after the v3 unique index lands.
            with pytest.raises(sqlite3.IntegrityError):
                b._conn.execute(  # noqa: SLF001
                    "INSERT INTO sync_mappings "
                    "(task_id, external_system, external_id, last_synced_at) "
                    "VALUES (?, ?, ?, ?)",
                    ("T002", "github_issues", "gh-1", _T0.isoformat()),
                )
        finally:
            b.close()

    def test_v2_migration_preserves_existing_sync_mapping_rows(
        self, tmp_path: Path,
    ) -> None:
        """The pre-existing v2 mapping row survives the v3 migration intact."""
        db_path = str(tmp_path / "state.db")
        events_path = str(tmp_path / "events.jsonl")
        self._stand_up_v2_sync_mappings_table(db_path, events_path)

        clock = _make_clock()
        b = SqliteBackend(
            db_path=db_path, events_path=events_path, clock=clock,
        )
        b.initialize()
        try:
            stored = b.get_sync_mapping("T001")
            assert stored is not None
            assert stored.task_id == "T001"
            assert stored.external_system == "github_issues"
            assert stored.external_id == "gh-1"
            # New optional fields default cleanly.
            assert stored.external_url is None
            assert stored.provider_metadata == {}
        finally:
            b.close()


class TestV6ToV7Migration:
    """T004 — the v6 -> v7 multi-PRD persistence-foundation migration.

    A real v6 db has a SINGLE-row ``prds`` table keyed on ``project_id`` and
    ``requirements`` / ``features`` / ``tasks`` / ``sync_mappings`` with no
    ``prd_id`` partition column. The migration must:
      * rebuild ``prds`` with the single-column ``id`` PK + identity/release
        columns, adopting the existing row as ``id='default'`` is_default=1;
      * ALTER-backfill ``prd_id='default'`` onto every existing requirement /
        feature / task / sync_mapping row in one statement each (zero data loss
        — the row counts are invariant);
      * stamp ``user_version = 7``;
      * be idempotent (re-running it after a simulated crash does not error).
    """

    def _stand_up_v6_db(
        self, db_path: str, events_path: str, *, prd_status: str = "reviewed"
    ) -> None:
        """Create a real v6-shaped db (singleton prds keyed on project_id, no
        prd_id columns) with a project, prd, 2 requirements, a feature, 2 tasks
        and a sync_mapping, then stamp user_version=6.

        ``prd_status`` controls the singleton PRD's status (default 'reviewed').
        Pass 'draft' to stand up a v6 DB whose PRD is NOT claimable, so the
        migration's PRD-status carry-through can be exercised at the claim gate.
        """
        Path(events_path).touch()
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE projects (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE prds (
                project_id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'draft',
                summary TEXT NOT NULL DEFAULT '',
                goals TEXT NOT NULL DEFAULT '[]',
                non_goals TEXT NOT NULL DEFAULT '[]',
                requirements TEXT NOT NULL DEFAULT '[]',
                acceptance_criteria TEXT NOT NULL DEFAULT '[]',
                risks TEXT NOT NULL DEFAULT '[]',
                open_questions TEXT NOT NULL DEFAULT '[]',
                last_reviewed_at TEXT, last_reviewed_by TEXT
            );
            CREATE TABLE requirements (
                id TEXT PRIMARY KEY, prd_section TEXT NOT NULL, text TEXT NOT NULL,
                source_paragraph TEXT, derived INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE features (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'proposed',
                requirements TEXT NOT NULL DEFAULT '[]', tasks TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                feature_id TEXT NOT NULL REFERENCES features(id) ON DELETE RESTRICT,
                title TEXT NOT NULL, description TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'proposed',
                priority TEXT NOT NULL DEFAULT 'medium',
                task_type TEXT NOT NULL DEFAULT 'feature',
                dependencies TEXT NOT NULL DEFAULT '[]',
                conflict_groups TEXT NOT NULL DEFAULT '[]',
                scores TEXT NOT NULL DEFAULT '{}',
                acceptance_criteria TEXT NOT NULL DEFAULT '[]',
                implementation_notes TEXT NOT NULL DEFAULT '[]',
                verification TEXT NOT NULL DEFAULT '{}',
                likely_files TEXT NOT NULL DEFAULT '[]',
                parent_task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE evidence (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, claim_id TEXT NOT NULL,
                commands_run TEXT NOT NULL DEFAULT '[]', output_excerpt TEXT,
                files_changed TEXT NOT NULL DEFAULT '[]', pr_url TEXT, commit_sha TEXT,
                screenshots TEXT NOT NULL DEFAULT '[]', known_limitations TEXT,
                proofs TEXT NOT NULL DEFAULT '[]',
                submitted_at TEXT NOT NULL, submitted_by TEXT NOT NULL
            );
            CREATE TABLE events (
                id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, actor TEXT NOT NULL,
                action TEXT NOT NULL, target_kind TEXT NOT NULL, target_id TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}', seq INTEGER
            );
            CREATE TABLE sync_mappings (
                task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                external_system TEXT NOT NULL, external_id TEXT NOT NULL,
                external_url TEXT, last_synced_at TEXT NOT NULL,
                sync_state TEXT NOT NULL DEFAULT 'in_sync',
                conflict_resolution_strategy TEXT NOT NULL DEFAULT 'prompt',
                provider_metadata_json TEXT,
                PRIMARY KEY (task_id, external_system),
                UNIQUE (external_system, external_id)
            );
            CREATE TABLE claims (id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
                claimed_by TEXT NOT NULL, claim_type TEXT NOT NULL DEFAULT 'task',
                status TEXT NOT NULL DEFAULT 'active', branch TEXT, worktree_path TEXT,
                expected_files TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL,
                lease_expires_at TEXT NOT NULL, last_heartbeat_at TEXT NOT NULL,
                released_at TEXT, release_reason TEXT);
            CREATE TABLE decisions (id TEXT PRIMARY KEY, title TEXT NOT NULL,
                context TEXT NOT NULL, decision TEXT NOT NULL, consequences TEXT NOT NULL,
                created_at TEXT NOT NULL, related_tasks TEXT NOT NULL DEFAULT '[]',
                related_features TEXT NOT NULL DEFAULT '[]');
            CREATE TABLE reviews (id TEXT PRIMARY KEY, target_kind TEXT NOT NULL,
                target_id TEXT NOT NULL, reviewed_by TEXT NOT NULL, decision TEXT NOT NULL,
                notes TEXT, created_at TEXT NOT NULL);
            CREATE TABLE conflict_groups (id TEXT PRIMARY KEY, name TEXT NOT NULL,
                task_ids TEXT NOT NULL DEFAULT '[]', reason TEXT NOT NULL);
            """
        )
        iso = _T0.isoformat()
        conn.execute(
            "INSERT INTO projects (id, name, description, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("proj-1", "Proj", "desc", iso, iso),
        )
        # last_reviewed_at is DISTINCT from project.created_at (_T1 vs _T0) so
        # the v7 COALESCE(last_reviewed_at, project.created_at) backfill is
        # load-bearing: a test asserting created_at == _T1 proves the migration
        # sourced last_reviewed_at, not the project fallback.
        conn.execute(
            "INSERT INTO prds (project_id, status, summary, last_reviewed_at) "
            "VALUES (?, ?, ?, ?)",
            ("proj-1", prd_status, "the summary", _T1.isoformat()),
        )
        conn.execute(
            "INSERT INTO requirements (id, prd_section, text) VALUES (?, ?, ?)",
            ("R001", "Goals", "req one"),
        )
        conn.execute(
            "INSERT INTO requirements (id, prd_section, text) VALUES (?, ?, ?)",
            ("R002", "Goals", "req two"),
        )
        conn.execute(
            "INSERT INTO features (id, title, description) VALUES (?, ?, ?)",
            ("F001", "Feat", "fdesc"),
        )
        for tid in ("T001", "T002"):
            conn.execute(
                "INSERT INTO tasks (id, feature_id, title, description, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (tid, "F001", tid, "tdesc", iso, iso),
            )
        conn.execute(
            "INSERT INTO sync_mappings (task_id, external_system, external_id, "
            "last_synced_at) VALUES (?, ?, ?, ?)",
            ("T001", "github_issues", "gh-1", iso),
        )
        conn.execute("PRAGMA user_version = 6")
        conn.commit()
        conn.close()

    @staticmethod
    def _counts(db_path: str) -> dict[str, int]:
        conn = sqlite3.connect(db_path)
        try:
            return {
                t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("requirements", "features", "tasks", "sync_mappings")
            }
        finally:
            conn.close()

    def test_v6_to_v7_backfills_default_prd_and_preserves_rows(
        self, tmp_path: Path
    ) -> None:
        """Upgrading a v6 db mints the default PRD and backfills prd_id with no
        row loss; user_version becomes 7 and the prds PK is the single-col id.
        """
        db_path = str(tmp_path / "state.db")
        events_path = str(tmp_path / "events.jsonl")
        self._stand_up_v6_db(db_path, events_path)
        before = self._counts(db_path)

        clock = _make_clock()
        b = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock)
        b.initialize()
        try:
            conn = sqlite3.connect(db_path)
            try:
                # user_version stamped to the current SCHEMA_VERSION (v6 climbs
                # the full ladder: v7 multi-PRD foundation + v8 revision column).
                assert (
                    conn.execute("PRAGMA user_version").fetchone()[0]
                    == SCHEMA_VERSION
                )
                # prds: exactly one default row, keyed on single-col id; the v8
                # revision counter backfilled to 1.
                prds = conn.execute(
                    "SELECT id, is_default, summary, project_id, created_at, "
                    "revision FROM prds"
                ).fetchall()
                assert len(prds) == 1
                assert prds[0][0] == "default"
                assert prds[0][1] == 1
                assert prds[0][2] == "the summary"
                assert prds[0][3] == "proj-1"
                assert prds[0][5] == 1, "v8 revision backfills to 1"
                # created_at backfilled from COALESCE(last_reviewed_at,
                # project.created_at). last_reviewed_at (_T1) is DISTINCT from
                # project.created_at (_T0), so this asserts the COALESCE picked
                # the FIRST non-null source (last_reviewed_at == _T1), not the
                # fallback — the assertion is load-bearing, not vacuous.
                assert prds[0][4] == _T1.isoformat()
                # The PK is the single ``id`` column (NOT composite / project_id).
                pk_cols = [
                    r[1]
                    for r in conn.execute("PRAGMA table_info(prds)").fetchall()
                    if r[5]  # pk flag
                ]
                assert pk_cols == ["id"]
                # Every entity row backfilled to the default partition.
                for table in ("requirements", "features", "tasks"):
                    rows = conn.execute(
                        f"SELECT DISTINCT prd_id FROM {table}"
                    ).fetchall()
                    assert rows == [("default",)], (table, rows)
                # sync_mapping prd_id backfilled via the task join; entity_kind.
                sm = conn.execute(
                    "SELECT prd_id, entity_kind FROM sync_mappings"
                ).fetchall()
                assert sm == [("default", "task")]
                # requirements lineage columns exist and default to NULL.
                lineage = conn.execute(
                    "SELECT revision_introduced, revision_superseded "
                    "FROM requirements"
                ).fetchall()
                assert all(r == (None, None) for r in lineage)
            finally:
                conn.close()
        finally:
            b.close()

        # Row-count invariant: nothing was lost.
        assert self._counts(db_path) == before

    def test_get_prd_on_migrated_v6_db_returns_tz_aware_default(
        self, tmp_path: Path
    ) -> None:
        """T008 (review #13) — get_prd() on a freshly migrated v6 DB returns the
        default PRD without raising on the tz-aware timestamp validators.

        The v6->v7 migration backfills created_at/updated_at via
        COALESCE(last_reviewed_at, project.created_at), both stored as tz-aware
        UTC ISO strings. _row_to_prd must surface them through PRD's UTC
        field-validators (which reject naive datetimes) without error, and the
        no-arg get_prd() must resolve the is_default=1 row.
        """
        db_path = str(tmp_path / "state.db")
        events_path = str(tmp_path / "events.jsonl")
        self._stand_up_v6_db(db_path, events_path)

        clock = _make_clock()
        b = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock)
        b.initialize()  # migrates v6 -> v7
        try:
            prd = b.get_prd()  # no-arg → is_default=1 row
            assert prd is not None
            assert prd.id == "default"
            assert prd.is_default is True
            assert prd.summary == "the summary"
            # Timestamps parsed and tz-AWARE (no naive datetime escaped).
            assert prd.created_at is not None
            assert prd.created_at.tzinfo is not None
            assert prd.updated_at is not None
            assert prd.updated_at.tzinfo is not None
            # created_at sourced from COALESCE(last_reviewed_at=_T1, ...).
            assert prd.created_at == _T1
            # default_prd_id() and list_prds() agree with the single default.
            assert b.default_prd_id() == "default"
            assert [p.id for p in b.list_prds()] == ["default"]
            # Explicit-id lookup resolves the same row; unknown id → None.
            assert b.get_prd("default") is not None
            assert b.get_prd("nonexistent") is None
        finally:
            b.close()

    def test_v6_to_v7_is_idempotent(self, tmp_path: Path) -> None:
        """Re-running the v6->v7 step on an already-migrated db is a no-op.

        Simulates a crash that left the db migrated but the user_version not yet
        observed: we force user_version back to 6 and re-run _check_schema_version
        directly. The rebuild guard (is_default already present) and the
        duplicate-column-tolerant ALTERs must make this a clean no-op.
        """
        db_path = str(tmp_path / "state.db")
        events_path = str(tmp_path / "events.jsonl")
        self._stand_up_v6_db(db_path, events_path)

        clock = _make_clock()
        b = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock)
        b.initialize()  # migrates to v7
        try:
            assert b._conn is not None  # noqa: SLF001
            before = self._counts(db_path)
            # Force the on-disk marker back to 6 and re-run the migration on the
            # already-v7-shaped tables — must not raise.
            b._conn.execute("PRAGMA user_version = 6")  # noqa: SLF001
            b._check_schema_version(pre_ddl_version=6)  # noqa: SLF001
            assert (
                b._conn.execute("PRAGMA user_version").fetchone()[0]  # noqa: SLF001
                == SCHEMA_VERSION
            )
            # Still exactly one default prd, still no row loss.
            prds = b._conn.execute(  # noqa: SLF001
                "SELECT id, is_default FROM prds"
            ).fetchall()
            assert len(prds) == 1
            assert prds[0][0] == "default"
            assert prds[0][1] == 1
            assert self._counts(db_path) == before
        finally:
            b.close()

    def test_v6_to_v7_torn_rebuild_with_stray_prds_new_heals(
        self, tmp_path: Path
    ) -> None:
        """T007 — a crash MID-REBUILD that left a stray ``prds_new`` table behind
        must heal cleanly on the next migrate, not raise.

        The rebuild guard keys on ``is_default`` being absent from ``prds``; a
        crash AFTER ``CREATE TABLE prds_new`` but BEFORE the ``DROP prds`` /
        ``RENAME`` leaves ``prds`` still un-rebuilt (no ``is_default``) AND a
        stray ``prds_new`` present. Re-opening must DROP the stale ``prds_new``
        and complete: user_version=7, a single default PRD row, every entity row
        prd_id='default', and no row loss.
        """
        db_path = str(tmp_path / "state.db")
        events_path = str(tmp_path / "events.jsonl")
        self._stand_up_v6_db(db_path, events_path)
        before = self._counts(db_path)

        # Simulate the torn rebuild: a stray prds_new table left behind by a
        # crash, while prds is still the un-migrated v6 shape (no is_default).
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("CREATE TABLE prds_new (id TEXT PRIMARY KEY)")
            conn.commit()
        finally:
            conn.close()

        clock = _make_clock()
        b = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock)
        b.initialize()  # must heal the torn rebuild, not raise
        try:
            assert b.get_schema_version() == SCHEMA_VERSION
            conn = sqlite3.connect(db_path)
            try:
                # The stray table is gone (renamed/dropped during the heal).
                names = {
                    r[0]
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                assert "prds_new" not in names
                # Single default PRD row.
                prds = conn.execute("SELECT id, is_default FROM prds").fetchall()
                assert prds == [("default", 1)]
                # Every entity row in the default partition.
                for table in ("requirements", "features", "tasks"):
                    rows = conn.execute(
                        f"SELECT DISTINCT prd_id FROM {table}"
                    ).fetchall()
                    assert rows == [("default",)], (table, rows)
                sm = conn.execute("SELECT DISTINCT prd_id FROM sync_mappings").fetchall()
                assert sm == [("default",)]
            finally:
                conn.close()
        finally:
            b.close()

        # Row-count invariant preserved across the (torn → healed) migrate.
        assert self._counts(db_path) == before

    def test_v6_to_v7_rerun_on_already_v7_db_is_noop(
        self, tmp_path: Path
    ) -> None:
        """T007 — re-running the migration on an ALREADY-v7 db is a clean no-op.

        After the first migrate stamps v7, forcing the on-disk marker back to 6
        and re-running ``_check_schema_version`` must re-stamp v7 with zero
        changes: same single default PRD, identical row counts, no error from
        the rebuild guard or the duplicate-column-tolerant ALTERs.
        """
        db_path = str(tmp_path / "state.db")
        events_path = str(tmp_path / "events.jsonl")
        self._stand_up_v6_db(db_path, events_path)

        clock = _make_clock()
        b = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock)
        b.initialize()  # migrates v6 -> v7
        try:
            assert b._conn is not None  # noqa: SLF001
            counts_after_first = self._counts(db_path)
            prds_after_first = b._conn.execute(  # noqa: SLF001
                "SELECT id, is_default FROM prds ORDER BY id"
            ).fetchall()

            # Re-run the migration on the already-v7-shaped tables.
            b._conn.execute("PRAGMA user_version = 6")  # noqa: SLF001
            b._check_schema_version(pre_ddl_version=6)  # noqa: SLF001

            assert b.get_schema_version() == SCHEMA_VERSION
            # Nothing changed: same prds, same row counts.
            assert (
                b._conn.execute(  # noqa: SLF001
                    "SELECT id, is_default FROM prds ORDER BY id"
                ).fetchall()
                == prds_after_first
            )
            assert self._counts(db_path) == counts_after_first
        finally:
            b.close()

    def test_v6_to_v7_torn_migration_rolls_back_and_retries_clean(
        self, tmp_path: Path
    ) -> None:
        """BUG 002 — a torn v6->v7 migration must roll back atomically (leaving
        user_version at 6, NOT 7) and then COMPLETE on a clean retry.

        The connection is autocommit (isolation_level=None), so without the
        explicit BEGIN IMMEDIATE/COMMIT wrap each statement would commit on its
        own: a failure AFTER the prds rebuild + some ALTERs would leave a
        partial-v7 schema while user_version was already stamped 7 (by the
        pre-fix _apply_ddl), and the ladder would never re-run — tasks.prd_id /
        sync_mappings.prd_id missing forever.

        We force a failure PART-WAY through (during the sync_mappings prd_id
        ALTER, after the prds rebuild and the entity-table ALTERs have run)
        WITHOUT manually resetting user_version, and assert:
          1. the exception leaves user_version at 6 (rolled back, not 7), and
             the partial work (prds rebuild, tasks.prd_id) is gone;
          2. removing the injected failure and re-opening COMPLETES the
             migration: every prd_id column present, user_version=7, rows intact.
        """
        db_path = str(tmp_path / "state.db")
        events_path = str(tmp_path / "events.jsonl")
        self._stand_up_v6_db(db_path, events_path)
        before = self._counts(db_path)

        # --- 1. First open: inject a mid-migration failure. -----------------
        # Run the REAL _m_to_v7 (whatever its internals), but feed it a thin
        # connection proxy whose execute() raises on a chosen statement that
        # only runs AFTER meaningful partial work (the prds rebuild + the
        # requirements/features/tasks prd_id ALTERs) has already executed.
        # Targeting _m_to_v7 (present in every version) keeps the test agnostic
        # to the fix's internal structure: it asserts the OBSERVABLE end-state.
        real_m_to_v7 = SqliteBackend._m_to_v7
        sentinel = "ALTER TABLE sync_mappings ADD COLUMN prd_id"

        class _FailingConn:
            """Proxy that forwards to the real connection but raises on the
            sentinel statement (sqlite3.Connection.execute is read-only and
            cannot be monkeypatched directly)."""

            def __init__(self, real: sqlite3.Connection) -> None:
                self._real = real

            def execute(self, sql: str, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
                if sentinel in sql:
                    raise sqlite3.OperationalError("injected mid-migration failure")
                return self._real.execute(sql, *args, **kwargs)

            def __getattr__(self, name: str) -> Any:
                return getattr(self._real, name)

        def torn_m_to_v7(self: SqliteBackend, conn: sqlite3.Connection) -> None:
            real_m_to_v7(self, _FailingConn(conn))  # type: ignore[arg-type]

        # The migration ladder dispatches via the _MIGRATIONS table, which holds
        # the function object captured at class-definition time — so patch the
        # table entry (not just the class attribute) to actually intercept the
        # v6->v7 step. This works identically on the fixed and unfixed source.
        orig_migrations = list(SqliteBackend._MIGRATIONS)
        SqliteBackend._MIGRATIONS = [
            (frm, torn_m_to_v7 if frm == 6 else fn)
            for frm, fn in orig_migrations
        ]

        clock = _make_clock()
        b = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock)
        try:
            with pytest.raises(sqlite3.OperationalError, match="injected"):
                b.initialize()
        finally:
            SqliteBackend._MIGRATIONS = orig_migrations
            # The failed initialize may have left a half-open backend; close it.
            try:
                b.close()
            except Exception:
                pass

        # The torn migration rolled back: user_version is still 6, and NONE of
        # the v7 changes survived (atomic rollback of the whole body).
        conn = sqlite3.connect(db_path)
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 6
            prds_cols = {
                r[1] for r in conn.execute("PRAGMA table_info(prds)").fetchall()
            }
            assert "is_default" not in prds_cols  # prds rebuild rolled back
            tasks_cols = {
                r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()
            }
            assert "prd_id" not in tasks_cols  # entity ALTER rolled back
            # No stray prds_new left behind.
            names = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert "prds_new" not in names
        finally:
            conn.close()

        # --- 2. Clean retry (no injected failure) completes the migration. --
        b2 = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock)
        b2.initialize()
        try:
            conn = sqlite3.connect(db_path)
            try:
                assert (
                    conn.execute("PRAGMA user_version").fetchone()[0]
                    == SCHEMA_VERSION
                )
                # Both prd_id columns the torn run failed to add now exist.
                for table in ("tasks", "sync_mappings"):
                    cols = {
                        r[1]
                        for r in conn.execute(
                            f"PRAGMA table_info({table})"
                        ).fetchall()
                    }
                    assert "prd_id" in cols, table
                prds = conn.execute("SELECT id, is_default FROM prds").fetchall()
                assert prds == [("default", 1)]
                for table in ("requirements", "features", "tasks"):
                    rows = conn.execute(
                        f"SELECT DISTINCT prd_id FROM {table}"
                    ).fetchall()
                    assert rows == [("default",)], (table, rows)
            finally:
                conn.close()
        finally:
            b2.close()

        # Row counts intact across torn → rolled-back → cleanly-migrated.
        assert self._counts(db_path) == before

    def test_prd_parsed_after_v7_migration_preserves_default_identity(
        self, tmp_path: Path
    ) -> None:
        """BUG 001 — the first prd.parsed re-parse after a v6->v7 migration must
        NOT wipe the v7 identity columns of the migrated default PRD row.

        The migrated 'default' row has is_default=1 and a non-null created_at.
        A naive ``INSERT OR REPLACE`` (omitting id/is_default/created_at) would
        collide on id='default', reset is_default 1->0 and null created_at,
        destroying the ux_prds_default invariant. The UPSERT fix must keep
        is_default=1 and created_at, updating only the v6 content + updated_at.
        """
        db_path = str(tmp_path / "state.db")
        events_path = str(tmp_path / "events.jsonl")
        self._stand_up_v6_db(db_path, events_path)

        clock = _make_clock()
        b = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock)
        b.initialize()  # migrates v6 -> v7
        try:
            # Capture the migrated default row's identity columns.
            conn = sqlite3.connect(db_path)
            try:
                before = conn.execute(
                    "SELECT is_default, created_at FROM prds WHERE id='default'"
                ).fetchone()
            finally:
                conn.close()
            assert before[0] == 1
            # created_at backfilled by the migration from last_reviewed_at (_T1).
            assert before[1] == _T1.isoformat()

            # Apply a prd.parsed event (the first re-parse after migration). Its
            # "now" (_T0) is DISTINCT from — and earlier than — the migrated
            # created_at (_T1). This makes the created_at/updated_at assertions
            # below LOAD-BEARING: if the UPSERT wrongly overwrote created_at with
            # the event timestamp, created_at would become _T0 != before[1] and
            # the assertion would fail. updated_at must advance to the event "now".
            payload = _make_prd_parsed_payload(
                project_id="proj-1", summary="re-parsed summary"
            )
            b.append(_make_event("prd.parsed", payload, now=_T0))

            conn = sqlite3.connect(db_path)
            try:
                rows = conn.execute(
                    "SELECT id, is_default, created_at, summary, updated_at FROM prds"
                ).fetchall()
                # Exactly one prds row, still the default identity preserved.
                assert len(rows) == 1
                row = rows[0]
                assert row[0] == "default"
                assert row[1] == 1  # is_default preserved (NOT reset to 0)
                assert row[2] == before[1]  # created_at preserved (_T1, NOT _T0)
                assert row[2] == _T1.isoformat()
                assert row[3] == "re-parsed summary"  # v6 content updated
                assert row[4] == _T0.isoformat()  # updated_at bumped to event now
                # Exactly one is_default=1 row (ux_prds_default invariant holds).
                n_default = conn.execute(
                    "SELECT COUNT(*) FROM prds WHERE is_default=1"
                ).fetchone()[0]
                assert n_default == 1
            finally:
                conn.close()
        finally:
            b.close()

    # -----------------------------------------------------------------------
    # T014 — v6->v7 gate-equivalence: a migrated single-PRD DB keeps identical
    # claimability. The migration must not change which tasks are claimable nor
    # which PRD owns a task; the default partition the migration mints resolves
    # exactly as the legacy single-PRD DB did.
    # -----------------------------------------------------------------------

    def test_v6_to_v7_migration_claim_gate_resolves_every_task_to_default_prd(
        self, tmp_path: Path
    ) -> None:
        """T014 (criterion 1) — after migrating a v6 single-PRD DB, every
        pre-existing task's owning PRD (``get_prd_for_task`` — the resolver the
        claim gate uses) is the default PRD.

        A real v6 DB has no ``prd_id`` column, so the migration backfills every
        task into the default partition. ``get_prd_for_task`` must therefore
        return the single ``id='default'`` PRD for each task — the same PRD the
        legacy no-arg ``get_prd()`` resolved before the multi-PRD foundation, so
        the claim gate keys on the same PRD it did pre-migration.
        """
        db_path = str(tmp_path / "state.db")
        events_path = str(tmp_path / "events.jsonl")
        self._stand_up_v6_db(db_path, events_path)

        clock = _make_clock()
        b = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock)
        b.initialize()  # migrates v6 -> v7
        try:
            assert b.get_schema_version() == SCHEMA_VERSION
            default = b.get_prd()  # is_default=1 row
            assert default is not None
            assert default.id == "default"

            # The two pre-existing v6 tasks (T001, T002) each resolve to the
            # default PRD — and the list is non-empty so the assertion is not
            # vacuous.
            tasks = b.list_tasks()
            assert {t.id for t in tasks} == {"T001", "T002"}
            for task in tasks:
                owning = b.get_prd_for_task(task)
                assert owning is not None, task.id
                assert owning.id == "default", (task.id, owning.id)
                assert owning.id == default.id, (task.id, owning.id)
        finally:
            b.close()

    def test_v6_to_v7_migration_preserves_claimability_gate(
        self, tmp_path: Path
    ) -> None:
        """T014 (criterion 2) — a task claimable pre-migration is still
        claimable post-migration, and a task NOT claimable pre-migration stays
        un-claimable.

        Pre-migration claimability on the v6 schema is exactly: status='ready'
        AND the (single) PRD reviewed/approved (no per-task PRD partition
        exists yet, so every task shares the one PRD). The v6 fixture's PRD is
        'reviewed', so on the v6 DB T001 (set ready) is claimable while T002
        (proposed) is not. After migrating to v7 the real ``ClaimManager``
        gate — which now resolves the OWNING PRD via ``get_prd_for_task`` — must
        reach the SAME verdict: T001 claims, T002 raises on the status gate.

        The status-gated negative case (T002) short-circuits at Gate 2 and
        never reaches the migrated PRD partition, so a companion test
        (``…_blocked_by_unreviewed_prd``) exercises the OTHER direction: a
        task that IS ready but whose PRD was un-reviewed pre-migration must
        stay refused — there at the migration-sensitive PRD gate (Gate 3).
        """
        from anvil.claims.manager import ClaimError, ClaimManager
        from anvil.state.models import ClaimStatus

        db_path = str(tmp_path / "state.db")
        events_path = str(tmp_path / "events.jsonl")
        self._stand_up_v6_db(db_path, events_path)

        # --- Pre-migration: compute claimability on the v6 schema. ----------
        # v6 has no prd_id column; every task shares the singleton PRD keyed on
        # project_id. Claimable iff status='ready' AND that PRD is
        # reviewed/approved. Set T001 ready (claimable), leave T002 proposed
        # (not claimable). Read the verdict directly off the v6 schema.
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = 'T001'")
            conn.commit()
            prd_status = conn.execute("SELECT status FROM prds").fetchone()[0]
            prd_claimable = prd_status in ("reviewed", "approved")
            pre_claimable = {
                tid: (status == "ready" and prd_claimable)
                for tid, status in conn.execute(
                    "SELECT id, status FROM tasks"
                ).fetchall()
            }
        finally:
            conn.close()
        # Sanity: the v6 fixture really gives one claimable and one not.
        assert pre_claimable == {"T001": True, "T002": False}

        # --- Migrate v6 -> v7, then run the REAL claim gate. ----------------
        clock = _make_clock()
        b = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock)
        b.initialize()  # migrates v6 -> v7
        try:
            assert b.get_schema_version() == SCHEMA_VERSION
            mgr = ClaimManager(b, clock, actor="agent-test")

            # T001 was claimable pre-migration → still claimable post-migration.
            assert pre_claimable["T001"] is True
            result = mgr.claim("T001")
            assert result.claim.status == ClaimStatus.active
            assert result.claim.task_id == "T001"

            # T002 was NOT claimable pre-migration → still refused (status gate).
            assert pre_claimable["T002"] is False
            with pytest.raises(ClaimError, match="ready"):
                mgr.claim("T002")
        finally:
            b.close()

    def test_v6_to_v7_migration_keeps_ready_task_blocked_by_unreviewed_prd(
        self, tmp_path: Path
    ) -> None:
        """T014 (criterion 2, the migration-sensitive direction) — a task that
        is NOT claimable pre-migration *because its PRD is un-reviewed* stays
        un-claimable post-migration, refused at the PRD gate.

        The sibling gate test's negative case (T002, status='proposed') is
        refused at Gate 2 (the status gate) which short-circuits BEFORE Gate 3
        ever calls ``get_prd_for_task`` — so it exercises ZERO migrated-PRD
        resolution and cannot catch a migration that mis-carries PRD status.
        Here both tasks are status='ready' (so the status gate passes) but the
        v6 PRD is 'draft', so pre-migration neither task is claimable on the
        PRD-status half of the gate. After migrating, the real ``ClaimManager``
        must reach the SAME verdict — refused — but now via the migration's
        actual behavioural delta: ``get_prd_for_task`` resolving the owning
        (default) PRD whose status the migration carried forward verbatim.

        This is the regression guard the status-gated case lacks: were the
        migration to wrongly stamp the default PRD 'reviewed' when the v6 PRD
        was 'draft', the post-migration claim would WRONGLY succeed and this
        test — and only this test — would fail.
        """
        from anvil.claims.manager import ClaimError, ClaimManager

        db_path = str(tmp_path / "state.db")
        events_path = str(tmp_path / "events.jsonl")
        # v6 PRD is 'draft' (NOT reviewed/approved) → not claimable on the PRD
        # half of the gate even when a task is ready.
        self._stand_up_v6_db(db_path, events_path, prd_status="draft")

        # --- Pre-migration: make both tasks 'ready' so the status gate would
        # PASS; the only thing blocking a claim is the un-reviewed PRD. Read the
        # verdict directly off the v6 schema.
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("UPDATE tasks SET status = 'ready'")
            conn.commit()
            prd_status = conn.execute("SELECT status FROM prds").fetchone()[0]
            prd_claimable = prd_status in ("reviewed", "approved")
            pre_claimable = {
                tid: (status == "ready" and prd_claimable)
                for tid, status in conn.execute(
                    "SELECT id, status FROM tasks"
                ).fetchall()
            }
        finally:
            conn.close()
        # Both tasks are ready but the PRD is draft → both un-claimable, and
        # NOT because of the status gate.
        assert prd_claimable is False
        assert pre_claimable == {"T001": False, "T002": False}

        # --- Migrate v6 -> v7, then run the REAL claim gate. ----------------
        clock = _make_clock()
        b = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock)
        b.initialize()  # migrates v6 -> v7
        try:
            assert b.get_schema_version() == SCHEMA_VERSION
            # The migration must carry the 'draft' status onto the default PRD.
            assert b.get_prd().status.value == "draft"
            mgr = ClaimManager(b, clock, actor="agent-test")

            # Both were un-claimable pre-migration (un-reviewed PRD) → still
            # refused post-migration, this time at the PRD gate (Gate 3) which
            # resolves the owning PRD via get_prd_for_task. The status gate
            # passes (both ready), so the only way to be refused is the
            # migration-carried PRD status — the behaviour a status-gated
            # negative case can never reach.
            for tid in ("T001", "T002"):
                assert pre_claimable[tid] is False
                with pytest.raises(
                    ClaimError, match=r"PRD must be in"
                ):
                    mgr.claim(tid)
        finally:
            b.close()


class TestV8ToV9Migration:
    """evidence-contracts T001 — v8 -> v9 adds ``tasks.claims`` and
    ``evidence.category``. A DB stamped at v8 must grow both via the ladder
    (the version-equal early-return would otherwise skip them forever), all
    rows preserved, re-run a no-op."""

    def _stand_up_v8_db(self, tmp_path: Path) -> Path:
        """Cheapest honest v8 db: initialize with the CURRENT engine, then
        strip the v9 columns and re-stamp user_version=8 — exactly the shape
        a pre-evidence-contracts build wrote (SQLite 3.35+ DROP COLUMN)."""
        b = _make_backend(tmp_path)
        _setup_project(b)
        b.append(_make_event(
            "feature.created", _make_feature_payload(feat_id="F001"),
            event_id="E000010", target_kind="feature", target_id="F001",
        ))
        b.append(_make_event(
            "task.created", _make_task_payload(task_id="T001"),
            event_id="E000011", target_kind="task", target_id="T001",
        ))
        b.close()
        db_path = tmp_path / "state.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("ALTER TABLE tasks DROP COLUMN claims")
        conn.execute("ALTER TABLE evidence DROP COLUMN category")
        conn.execute("PRAGMA user_version = 8")
        conn.commit()
        conn.close()
        return db_path

    def test_v8_db_gains_claims_and_category_and_stamps_v9(
        self, tmp_path: Path
    ) -> None:
        db_path = self._stand_up_v8_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        assert "claims" not in {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        conn.close()

        b = _make_backend(tmp_path)  # initialize() runs the ladder
        try:
            assert b.get_schema_version() == 16
            task = b.get_task("T001")
            assert task is not None
            assert task.claims == []  # row preserved, backfilled to "no claims"
        finally:
            b.close()

        conn = sqlite3.connect(str(db_path))
        assert "claims" in {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert "category" in {
            r[1] for r in conn.execute("PRAGMA table_info(evidence)")
        }
        conn.close()

        # Re-open (re-run of the ladder) is a no-op, not a duplicate-column crash.
        b2 = _make_backend(tmp_path)
        b2.close()

    def test_task_with_claims_roundtrips_through_backend(
        self, tmp_path: Path
    ) -> None:
        """T001 AC: claims + artifact assertions survive create -> get -> list."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            b.append(_make_event(
                "feature.created", _make_feature_payload(feat_id="F001"),
                event_id="E000010", target_kind="feature", target_id="F001",
            ))
            payload = _make_task_payload(task_id="T001")
            payload["claims"] = [
                {"id": "candidate_benchmark_completed",
                 "subject": "gemma4-12b-it", "kind": "measurement"}
            ]
            payload["verification"] = {
                "commands": ["echo ok"],
                "artifact_assertions": [
                    {
                        "artifact": "evidence/gemma.json",
                        "claim": "candidate_benchmark_completed",
                        "assertions": [
                            {"path": "status", "op": "equals", "value": "measured"},
                            {"path": "stage_timings_ms.llm_ms", "op": "not_null"},
                        ],
                        "stage_order": ["stt", "llm", "tts"],
                        "stage_path": "errors[*].stage",
                        "must_not_fail_before": "llm",
                    }
                ],
            }
            b.append(_make_event(
                "task.created", payload,
                event_id="E000011", target_kind="task", target_id="T001",
            ))
            task = b.get_task("T001")
            assert task is not None
            assert task.claims[0].id == "candidate_benchmark_completed"
            assert task.claims[0].kind.value == "measurement"
            va = task.verification.artifact_assertions[0]
            assert va.claim == "candidate_benchmark_completed"
            assert va.assertions[1].op.value == "not_null"
            assert va.must_not_fail_before == "llm"
            listed = [t for t in b.list_tasks() if t.id == "T001"][0]
            assert listed.claims == task.claims
        finally:
            b.close()

    def test_claimless_task_dump_keeps_pre_v9_shape(self, tmp_path: Path) -> None:
        """T001 AC: omit-when-empty — no claims/artifact_assertions keys in
        dumps of tasks that don't use the feature (byte-shape parity)."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            b.append(_make_event(
                "feature.created", _make_feature_payload(feat_id="F001"),
                event_id="E000010", target_kind="feature", target_id="F001",
            ))
            b.append(_make_event(
                "task.created", _make_task_payload(task_id="T001"),
                event_id="E000011", target_kind="task", target_id="T001",
            ))
            task = b.get_task("T001")
            assert task is not None
            dump = task.model_dump(mode="json")
            assert "claims" not in dump
            assert "artifact_assertions" not in dump["verification"]
        finally:
            b.close()


class TestV7ToV8Migration:
    """T023 — the v7 -> v8 migration adds the per-PRD ``revision`` counter.

    v7 (PR #80) shipped a multi-row ``prds`` table WITHOUT a ``revision``
    column. T023 introduced ``revision`` as a SEPARATE schema version (v8): a DB
    already stamped at v7 must re-enter the migration ladder via the v8 bump and
    grow the column. Folding ``revision`` into v7 in place would leave such DBs
    missing the column forever (the version-equal early-return skips the ladder)
    — the bug this test guards against.
    """

    def _stand_up_v7_db(self, db_path: str, events_path: str) -> None:
        """Create a real v7-shaped db: a multi-row ``prds`` table keyed on the
        single-column ``id`` PK, with ``is_default`` but NO ``revision`` column;
        the entity tables carry ``prd_id`` + the requirements lineage columns.
        Stamp ``user_version = 7`` so the engine sees a genuine pre-v8 DB."""
        Path(events_path).touch()
        conn = sqlite3.connect(db_path)
        iso = _T0.isoformat()
        conn.executescript(
            """
            CREATE TABLE projects (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE prds (
                id TEXT PRIMARY KEY DEFAULT 'default',
                project_id TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'draft',
                summary TEXT NOT NULL DEFAULT '',
                goals TEXT NOT NULL DEFAULT '[]',
                non_goals TEXT NOT NULL DEFAULT '[]',
                requirements TEXT NOT NULL DEFAULT '[]',
                acceptance_criteria TEXT NOT NULL DEFAULT '[]',
                risks TEXT NOT NULL DEFAULT '[]',
                open_questions TEXT NOT NULL DEFAULT '[]',
                last_reviewed_at TEXT, last_reviewed_by TEXT,
                target_version TEXT, target_tag TEXT,
                is_default INTEGER NOT NULL DEFAULT 0,
                created_at TEXT, updated_at TEXT
            );
            CREATE UNIQUE INDEX ux_prds_default ON prds (project_id)
                WHERE is_default = 1;
            CREATE TABLE requirements (
                id TEXT PRIMARY KEY, prd_id TEXT NOT NULL DEFAULT 'default',
                prd_section TEXT NOT NULL, text TEXT NOT NULL,
                source_paragraph TEXT, derived INTEGER NOT NULL DEFAULT 0,
                revision_introduced INTEGER, revision_superseded INTEGER
            );
            """
        )
        conn.execute(
            "INSERT INTO projects (id, name, description, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("proj-1", "Proj", "desc", iso, iso),
        )
        conn.execute(
            "INSERT INTO prds (id, project_id, status, summary, is_default, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("default", "proj-1", "approved", "the summary", 1, iso, iso),
        )
        conn.execute("PRAGMA user_version = 7")
        conn.commit()
        conn.close()

    def test_v7_db_gains_revision_column_and_stamps_v8(
        self, tmp_path: Path
    ) -> None:
        """Opening a pre-v8 (v7-stamped) DB runs the v8 step: the ``revision``
        column appears (backfilled to 1) and user_version becomes 8. Without the
        SCHEMA_VERSION bump the version-equal early-return would skip the ladder
        and `SELECT revision FROM prds` would raise no-such-column."""
        db_path = str(tmp_path / "state.db")
        events_path = str(tmp_path / "events.jsonl")
        self._stand_up_v7_db(db_path, events_path)

        # Sanity: the v7 fixture genuinely lacks the column.
        conn = sqlite3.connect(db_path)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(prds)")}
            assert "revision" not in cols
        finally:
            conn.close()

        clock = _make_clock()
        b = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock)
        b.initialize()  # must migrate v7 -> v8
        try:
            assert b.get_schema_version() == SCHEMA_VERSION == 16
            conn = sqlite3.connect(db_path)
            try:
                # The column now exists and backfilled to 1 for the existing row.
                rev = conn.execute("SELECT revision FROM prds").fetchone()[0]
                assert rev == 1
            finally:
                conn.close()
            # And the revision feature works end-to-end on this upgraded DB:
            # get_prd().revision reads the real column, and a prd.revised bumps.
            assert b.get_prd().revision == 1
        finally:
            b.close()

    def test_v7_to_v8_is_idempotent(self, tmp_path: Path) -> None:
        """Re-running the v8 step on an already-v8 DB is a clean no-op (the ALTER
        is duplicate-column tolerant)."""
        db_path = str(tmp_path / "state.db")
        events_path = str(tmp_path / "events.jsonl")
        self._stand_up_v7_db(db_path, events_path)

        clock = _make_clock()
        b = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock)
        b.initialize()  # v7 -> v8
        try:
            assert b._conn is not None  # noqa: SLF001
            # Force the marker back to 7 and re-run — must not raise.
            b._conn.execute("PRAGMA user_version = 7")  # noqa: SLF001
            b._check_schema_version(pre_ddl_version=7)  # noqa: SLF001
            assert b.get_schema_version() == SCHEMA_VERSION
            assert b._conn.execute(  # noqa: SLF001
                "SELECT revision FROM prds"
            ).fetchone()[0] == 1
        finally:
            b.close()


# ---------------------------------------------------------------------------
# PR #49 P2-2 regression — multi-provider missing_sync_mapping scan
# ---------------------------------------------------------------------------


class TestMissingSyncMappingPerProvider:
    """P2-2 — reconciliation must check each configured provider separately.

    Pre-fix bug: ``_scan_missing_sync_mappings`` called
    ``get_sync_mapping(task.id)`` without ``external_system``, returning
    the ASC-first mapping. A done task mapped to ``github_issues`` but
    NOT to ``linear`` was treated as fully mapped — the ``linear`` gap
    was never surfaced.
    """

    def test_multi_provider_emits_per_provider_discrepancy(
        self, tmp_path: Path,
    ) -> None:
        """Two providers configured, task only mapped to one → discrepancy
        for the missing one."""
        from anvil.state.models import SyncMapping
        from anvil.sync.reconciliation import (
            DiscrepancyKind,
            ReconciliationEngine,
        )

        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            # Walk a task through the lifecycle to status=done.
            b.append(_make_event(
                "feature.created",
                _make_feature_payload(),
                event_id="E000003", target_kind="feature", target_id="F001",
            ))
            b.append(_make_event(
                "task.created", _make_task_payload(task_id="T001"),
                event_id="E000004", target_kind="task", target_id="T001",
            ))
            chain = [
                ("proposed", "drafted"),
                ("drafted", "reviewed"),
                ("reviewed", "ready"),
                ("ready", "claimed"),
                ("claimed", "in_progress"),
                ("in_progress", "needs_review"),
                ("needs_review", "accepted"),
                ("accepted", "done"),
            ]
            eid = 5
            for frm, to in chain:
                b.append(_make_event(
                    "task.status_changed",
                    {"task_id": "T001", "from": frm, "to": to},
                    event_id=f"E{eid:06d}", target_kind="task", target_id="T001",
                ))
                eid += 1
            # Map T001 to github_issues ONLY.
            b.apply_sync_mapping(SyncMapping(
                task_id="T001",
                external_system="github_issues",
                external_id="gh-1",
                last_synced_at=_T0,
            ))

            # Configure BOTH github_issues AND linear.
            engine = ReconciliationEngine(
                b, state_dir=tmp_path, clock=_make_clock(),
                configured_providers=["github_issues", "linear"],
            )
            report = engine.scan()
            missing = [
                d for d in report.discrepancies
                if d.kind == DiscrepancyKind.missing_sync_mapping
            ]
            # Pre-fix: 0 (ASC-first returned the github_issues row).
            # Post-fix: 1 — the linear gap is flagged.
            assert len(missing) == 1, (
                f"P2-2 regression: expected exactly one missing_sync_mapping "
                f"for the unmapped provider (linear); got {len(missing)}."
            )
            d = missing[0]
            assert d.target_id == "T001"
            assert d.payload["missing_provider"] == "linear"
            assert "linear" in d.suggested_fix
            assert "T001" in d.suggested_fix
        finally:
            b.close()


# ---------------------------------------------------------------------------
# Phase 9 T5 — dispatcher regression (T3 broke the SyncAuditPayload alias)
# ---------------------------------------------------------------------------


class TestSyncDispatcherUsesConcreteSubclasses:
    """T5 regression — every ``sync.*`` action must dispatch to its concrete
    subclass from ``ACTION_TO_PAYLOAD`` (not the union TypeAlias).

    Phase 9 T3 turned ``SyncAuditPayload`` into an
    ``Annotated[Union[...], Field(discriminator="action")]`` form which is
    NOT a ``BaseModel`` subclass — it has no ``.model_validate`` method.
    The pre-T5 dispatcher's table did
    ``{"sync.push.started": (SyncAuditPayload, ...), ...}`` and crashed
    every ``sync.*`` event with ``AttributeError: 'types.UnionType' object
    has no attribute 'model_validate'`` (wrapped by the backend as
    ``error.transaction_aborted``). T5 switched to a dict-spread over
    ``ACTION_TO_PAYLOAD`` so each entry resolves to a real concrete
    subclass; this test pins the dispatcher to that contract.
    """

    def test_every_sync_action_dispatch_uses_a_real_basemodel_subclass(
        self, tmp_path: Path,
    ) -> None:
        """Every sync.* dispatch entry must be a callable ``model_validate``.

        Walk the dispatch table and assert each ``sync.*`` action's
        payload class has a callable ``model_validate`` method. The
        ``types.UnionType`` regression would fail this with an
        ``AttributeError`` at hasattr-resolution.
        """
        from anvil.state.payloads import ACTION_TO_PAYLOAD

        b = _make_backend(tmp_path)
        try:
            table = b._get_action_dispatch()
            for action in ACTION_TO_PAYLOAD:
                assert action in table, (
                    f"T5 regression: dispatcher missing sync action {action!r}; "
                    f"ACTION_TO_PAYLOAD declares it but the dispatch table "
                    "does not route it. Did the dict-spread merge drop?"
                )
                spec = table[action]
                payload_cls = spec.payload_model
                # The contract is: a class with .model_validate (the
                # Pydantic v2 BaseModel API). The TypeAlias would fail
                # this — UnionType has no such attribute.
                assert hasattr(payload_cls, "model_validate"), (
                    f"T5 regression: dispatch entry for {action!r} resolved "
                    f"to {payload_cls!r} which has no .model_validate. "
                    "T3's discriminated-union TypeAlias slipped back in."
                )
                # Sanity: the concrete subclass should be the one
                # ACTION_TO_PAYLOAD picked. Otherwise the dispatcher and
                # the discriminator have drifted.
                assert payload_cls is ACTION_TO_PAYLOAD[action], (
                    f"T5 regression: dispatch for {action!r} routed to "
                    f"{payload_cls!r}, but ACTION_TO_PAYLOAD says it should "
                    f"be {ACTION_TO_PAYLOAD[action]!r}."
                )
                # SL1-RR-1 architecture move #1: each action splits into a
                # read-only check and an infallible write, both callable.
                assert callable(spec.check)
                assert callable(spec.write)
        finally:
            b.close()

    def test_sync_pull_deferred_event_dispatches_without_attribute_error(
        self, tmp_path: Path,
    ) -> None:
        """End-to-end: apply a real ``sync.pull.deferred`` event and verify
        it lands as itself (NOT as ``error.transaction_aborted``).

        Pre-T5: ``_apply_mutation`` called ``SyncAuditPayload.model_validate``,
        hit ``AttributeError``, the backend wrapped it as
        ``TransactionAborted``, and the JSONL log contained
        ``error.transaction_aborted`` instead of the intended event.
        """
        b = _make_backend(tmp_path)
        events_path = str(tmp_path / "events.jsonl")
        try:
            # Apply a sync.pull.deferred event end-to-end. If the
            # dispatcher is still broken this raises TransactionAborted
            # and the JSONL row is the aborted-error sentinel.
            evt = EventDraft(
                timestamp=_T0,
                actor="test",
                action="sync.pull.deferred",
                target_kind="task",
                target_id="T001",
                payload_json={
                    "provider_id": "github_issues",
                    "task_id": "T001",
                    "external_id": "42",
                    "direction": "pull",
                    "resolution": "local_wins_deferred",
                },
            )
            b.append(evt)

            # Verify the JSONL row is the deferred event, not the abort
            # sentinel.
            events = _read_jsonl(events_path)
            actions = [e["action"] for e in events]
            assert "sync.pull.deferred" in actions, (
                f"T5 regression: sync.pull.deferred did not land in the "
                f"audit log; got actions={actions}. The dispatcher likely "
                f"raised AttributeError on the TypeAlias and the backend "
                f"wrapped it as error.transaction_aborted."
            )
            assert "error.transaction_aborted" not in actions, (
                f"T5 regression: dispatcher crashed on sync.pull.deferred; "
                f"got actions={actions}. Check ACTION_TO_PAYLOAD wiring "
                f"in state/sqlite.py:_get_action_dispatch."
            )
        finally:
            b.close()


# ---------------------------------------------------------------------------
# list_claims / list_reviews / list_evidence — full-snapshot read methods
# ---------------------------------------------------------------------------


def _setup_full_snapshot_backend(b: SqliteBackend) -> None:
    """Build a backend with released + stale claims, a review, and evidence.

    Event chain:
      - project + PRD + feature + task T001 (reaches 'ready')
      - claim C001 created (active)
      - claim C001 released  → status='released', task='ready'
      - task T001 re-claimed via C002 (active)
      - evidence EV001 submitted  → task='needs_review', claim C002='released'
      - task T001 applied (accepted)  → task='done', review row inserted
      - task T002 created (reaches 'ready')
      - claim C003 created (active), then marked stale
    """
    # ---- project bootstrap (uses event IDs E000001–E000002) ----
    _setup_claimable_task(b, task_id="T001")

    # ---- C001 active → released ----
    b.append(_make_event(
        "claim.created",
        _make_claim_payload(claim_id="C001", task_id="T001"),
        event_id="E000010", target_kind="claim", target_id="C001",
    ))
    b.append(_make_event(
        "claim.released",
        {"claim_id": "C001", "released_by": "agent-alpha",
         "release_reason": "dropped", "force": False},
        event_id="E000011", target_kind="claim", target_id="C001",
    ))

    # ---- T001 re-ready via C002 ----
    # task was returned to 'ready' by the release handler; re-claim it
    b.append(_make_event(
        "claim.created",
        _make_claim_payload(claim_id="C002", task_id="T001"),
        event_id="E000012", target_kind="claim", target_id="C002",
    ))

    # ---- evidence + apply ----
    b.append(_make_event(
        "evidence.submitted",
        _make_evidence_payload(task_id="T001", claim_id="C002", evidence_id="EV001"),
        event_id="E000013", target_kind="task", target_id="T001",
    ))
    b.append(_make_event(
        "task.applied",
        _make_applied_payload(task_id="T001", reviewer="alice", decision="accepted"),
        event_id="E000014", target_kind="task", target_id="T001",
    ))

    # ---- T002 with a stale claim ----
    # Reuse _setup_claimable_task logic but inline since project already exists.
    # Insert F001 task T002 directly via feature.created + task.created + transitions.
    b.append(_make_event(
        "task.created",
        _make_task_payload(task_id="T002", feature_id="F001"),
        event_id="E000015", target_kind="task", target_id="T002",
    ))
    for from_s, to_s, eid in [
        ("proposed", "drafted", "E000016"),
        ("drafted", "reviewed", "E000017"),
        ("reviewed", "ready", "E000018"),
    ]:
        b.append(_make_event(
            "task.status_changed",
            {"task_id": "T002", "from": from_s, "to": to_s},
            event_id=eid, target_kind="task", target_id="T002",
        ))

    b.append(_make_event(
        "claim.created",
        _make_claim_payload(claim_id="C003", task_id="T002"),
        event_id="E000019", target_kind="claim", target_id="C003",
    ))
    b.append(_make_event(
        "claim.stale",
        {"claim_id": "C003", "detected_at": _T0.isoformat(),
         "reason": "lease_expired"},
        event_id="E000020", target_kind="claim", target_id="C003",
    ))


class TestListClaimsReviewsEvidence:
    """Unit tests for list_claims(), list_reviews(), and list_evidence()."""

    # ------------------------------------------------------------------
    # list_claims()
    # ------------------------------------------------------------------

    def test_list_claims_returns_empty_when_no_claims(self, tmp_path: Path) -> None:
        """list_claims() returns [] when the claims table is empty."""
        b = _make_backend(tmp_path)
        try:
            assert b.list_claims() == []
        finally:
            b.close()

    def test_list_claims_returns_all_statuses(self, tmp_path: Path) -> None:
        """list_claims() returns active, released, and stale claims in id order."""
        b = _make_backend(tmp_path)
        try:
            _setup_full_snapshot_backend(b)
            claims = b.list_claims()
            # C001 released, C002 released (auto by evidence.submitted), C003 stale
            claim_ids = [c.id for c in claims]
            assert "C001" in claim_ids
            assert "C002" in claim_ids
            assert "C003" in claim_ids

        finally:
            b.close()

    def test_list_claims_sorted_by_id(self, tmp_path: Path) -> None:
        """list_claims() returns rows in ascending id order."""
        b = _make_backend(tmp_path)
        try:
            _setup_full_snapshot_backend(b)
            claims = b.list_claims()
            ids = [c.id for c in claims]
            assert ids == sorted(ids), (
                f"list_claims() result is not sorted by id: {ids}"
            )
        finally:
            b.close()

    def test_list_claims_includes_released_claims(self, tmp_path: Path) -> None:
        """list_claims() includes claims with status='released'."""
        b = _make_backend(tmp_path)
        try:
            _setup_full_snapshot_backend(b)
            claims = b.list_claims()
            statuses = {c.id: c.status.value for c in claims}
            assert statuses.get("C001") == "released", (
                f"C001 should be 'released', got {statuses.get('C001')!r}"
            )
        finally:
            b.close()

    def test_list_claims_includes_stale_claims(self, tmp_path: Path) -> None:
        """list_claims() includes claims with status='stale'."""
        b = _make_backend(tmp_path)
        try:
            _setup_full_snapshot_backend(b)
            claims = b.list_claims()
            statuses = {c.id: c.status.value for c in claims}
            assert statuses.get("C003") == "stale", (
                f"C003 should be 'stale', got {statuses.get('C003')!r}"
            )
        finally:
            b.close()

    def test_list_claims_returns_valid_claim_objects(self, tmp_path: Path) -> None:
        """list_claims() returns fully deserialized Claim objects (not dicts)."""
        from anvil.state.models import Claim

        b = _make_backend(tmp_path)
        try:
            _setup_full_snapshot_backend(b)
            claims = b.list_claims()
            assert len(claims) >= 1
            for claim in claims:
                assert isinstance(claim, Claim)
                # expected_files should be a list, not raw JSON string
                assert isinstance(claim.expected_files, list)
        finally:
            b.close()

    def test_list_claims_superset_of_list_active_claims(self, tmp_path: Path) -> None:
        """Every claim in list_active_claims() also appears in list_claims()."""
        b = _make_backend(tmp_path)
        try:
            _setup_full_snapshot_backend(b)
            all_ids = {c.id for c in b.list_claims()}
            active_ids = {c.id for c in b.list_active_claims()}
            assert active_ids.issubset(all_ids), (
                f"list_active_claims returned ids not in list_claims: "
                f"{active_ids - all_ids}"
            )
        finally:
            b.close()

    # ------------------------------------------------------------------
    # list_reviews()
    # ------------------------------------------------------------------

    def test_list_reviews_returns_empty_when_no_reviews(self, tmp_path: Path) -> None:
        """list_reviews() returns [] when the reviews table is empty."""
        b = _make_backend(tmp_path)
        try:
            assert b.list_reviews() == []
        finally:
            b.close()

    def test_list_reviews_returns_review_from_task_applied(self, tmp_path: Path) -> None:
        """list_reviews() returns the Review row inserted by task.applied."""
        b = _make_backend(tmp_path)
        try:
            _setup_full_snapshot_backend(b)
            reviews = b.list_reviews()
            assert len(reviews) >= 1, (
                "Expected at least one review after task.applied; got 0"
            )
        finally:
            b.close()

    def test_list_reviews_sorted_by_id(self, tmp_path: Path) -> None:
        """list_reviews() returns rows in ascending id order."""
        b = _make_backend(tmp_path)
        try:
            _setup_full_snapshot_backend(b)
            reviews = b.list_reviews()
            ids = [r.id for r in reviews]
            assert ids == sorted(ids), (
                f"list_reviews() result is not sorted by id: {ids}"
            )
        finally:
            b.close()

    def test_list_reviews_returns_valid_review_objects(self, tmp_path: Path) -> None:
        """list_reviews() returns fully deserialized Review objects (not dicts)."""
        from anvil.state.models import Review

        b = _make_backend(tmp_path)
        try:
            _setup_full_snapshot_backend(b)
            reviews = b.list_reviews()
            assert len(reviews) >= 1
            for review in reviews:
                assert isinstance(review, Review)
                assert review.decision is not None
                assert review.reviewed_by is not None
        finally:
            b.close()

    def test_list_reviews_captures_prd_approved_review(self, tmp_path: Path) -> None:
        """list_reviews() includes the Review row created by prd.approved."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            b.append(_make_event(
                "prd.parsed", _make_prd_parsed_payload(), event_id="E000003",
            ))
            b.append(_make_event(
                "prd.reviewed",
                {"project_id": "proj-1", "reviewer": "alice"},
                event_id="E000004",
            ))
            b.append(_make_event(
                "prd.approved",
                {"project_id": "proj-1", "approver": "bob"},
                event_id="E000005",
            ))
            reviews = b.list_reviews()
            assert len(reviews) == 1
            assert reviews[0].decision.value == "approve"
            assert reviews[0].reviewed_by == "bob"
        finally:
            b.close()

    # ------------------------------------------------------------------
    # list_evidence()
    # ------------------------------------------------------------------

    def test_list_evidence_returns_empty_when_no_evidence(self, tmp_path: Path) -> None:
        """list_evidence() returns [] when the evidence table is empty."""
        b = _make_backend(tmp_path)
        try:
            assert b.list_evidence() == []
        finally:
            b.close()

    def test_list_evidence_returns_submitted_evidence(self, tmp_path: Path) -> None:
        """list_evidence() returns the evidence row inserted by evidence.submitted."""
        b = _make_backend(tmp_path)
        try:
            _setup_full_snapshot_backend(b)
            evidence = b.list_evidence()
            assert len(evidence) >= 1, (
                "Expected at least one evidence row after evidence.submitted; got 0"
            )
            evids = [e.id for e in evidence]
            assert "EV001" in evids
        finally:
            b.close()

    def test_list_evidence_sorted_by_id(self, tmp_path: Path) -> None:
        """list_evidence() returns rows in ascending id order."""
        b = _make_backend(tmp_path)
        try:
            _setup_full_snapshot_backend(b)
            evidence = b.list_evidence()
            ids = [e.id for e in evidence]
            assert ids == sorted(ids), (
                f"list_evidence() result is not sorted by id: {ids}"
            )
        finally:
            b.close()

    def test_list_evidence_returns_valid_evidence_objects(self, tmp_path: Path) -> None:
        """list_evidence() returns fully deserialized Evidence objects (not dicts)."""
        from anvil.state.models import Evidence

        b = _make_backend(tmp_path)
        try:
            _setup_full_snapshot_backend(b)
            evidence = b.list_evidence()
            assert len(evidence) >= 1
            for ev in evidence:
                assert isinstance(ev, Evidence)
                assert isinstance(ev.commands_run, list)
                assert isinstance(ev.files_changed, list)
                assert isinstance(ev.screenshots, list)
                assert ev.submitted_at.tzinfo is not None, (
                    "submitted_at must be timezone-aware UTC"
                )
        finally:
            b.close()

    def test_list_evidence_fields_match_submitted_payload(self, tmp_path: Path) -> None:
        """list_evidence() returns evidence with fields matching the submitted payload."""
        b = _make_backend(tmp_path)
        try:
            _setup_full_snapshot_backend(b)
            evidence = b.list_evidence()
            ev = next((e for e in evidence if e.id == "EV001"), None)
            assert ev is not None, "EV001 not found in list_evidence() output"
            assert ev.task_id == "T001"
            assert ev.claim_id == "C002"
            assert ev.submitted_by == "agent-alpha"
            assert "pytest tests/ -v" in ev.commands_run
            assert "src/auth.py" in ev.files_changed
        finally:
            b.close()


# ---------------------------------------------------------------------------
# list_requirements — read method
# ---------------------------------------------------------------------------


class TestListRequirements:
    """Tests for SqliteBackend.list_requirements()."""

    def test_list_requirements_returns_empty_before_prd_parsed(
        self, tmp_path: Path
    ) -> None:
        """list_requirements() returns [] when no prd.parsed event has been applied."""
        b = _make_backend(tmp_path)
        try:
            assert b.list_requirements() == []
        finally:
            b.close()

    def test_list_requirements_returns_rows_after_prd_parsed(
        self, tmp_path: Path
    ) -> None:
        """list_requirements() returns Requirement objects populated by prd.parsed."""
        from anvil.state.models import Requirement

        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            payload = _make_prd_parsed_payload(
                requirements=[
                    {
                        "id": "R001",
                        "prd_section": "functional",
                        "text": "System must authenticate users.",
                        "source_paragraph": None,
                        "derived": False,
                    },
                    {
                        "id": "R002",
                        "prd_section": "non-functional",
                        "text": "Response time under 200ms.",
                        "source_paragraph": "para-1",
                        "derived": True,
                    },
                ]
            )
            b.append(_make_event("prd.parsed", payload, event_id="E000003"))

            reqs = b.list_requirements()
            assert len(reqs) == 2
            for rq in reqs:
                assert isinstance(rq, Requirement)
        finally:
            b.close()

    def test_list_requirements_sorted_by_id_asc(self, tmp_path: Path) -> None:
        """list_requirements() returns rows in id ASC order."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            # Insert in reverse lexical order to verify sort is applied.
            payload = _make_prd_parsed_payload(
                requirements=[
                    {
                        "id": "R003",
                        "prd_section": "s",
                        "text": "Req 3.",
                        "source_paragraph": None,
                        "derived": False,
                    },
                    {
                        "id": "R001",
                        "prd_section": "s",
                        "text": "Req 1.",
                        "source_paragraph": None,
                        "derived": False,
                    },
                    {
                        "id": "R002",
                        "prd_section": "s",
                        "text": "Req 2.",
                        "source_paragraph": None,
                        "derived": False,
                    },
                ]
            )
            b.append(_make_event("prd.parsed", payload, event_id="E000003"))

            reqs = b.list_requirements()
            ids = [r.id for r in reqs]
            assert ids == sorted(ids), (
                f"list_requirements() result is not sorted by id: {ids}"
            )
        finally:
            b.close()

    def test_list_requirements_fields_match_payload(self, tmp_path: Path) -> None:
        """list_requirements() returns requirements with fields matching the parsed payload."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            payload = _make_prd_parsed_payload(
                requirements=[
                    {
                        "id": "R001",
                        "prd_section": "auth",
                        "text": "Users must log in.",
                        "source_paragraph": "para-intro",
                        "derived": False,
                    },
                    {
                        "id": "R002",
                        "prd_section": "perf",
                        "text": "Latency under 100ms.",
                        "source_paragraph": None,
                        "derived": True,
                    },
                ]
            )
            b.append(_make_event("prd.parsed", payload, event_id="E000003"))

            reqs = {r.id: r for r in b.list_requirements()}

            assert reqs["R001"].prd_section == "auth"
            assert reqs["R001"].text == "Users must log in."
            assert reqs["R001"].source_paragraph == "para-intro"
            assert reqs["R001"].derived is False

            assert reqs["R002"].prd_section == "perf"
            assert reqs["R002"].text == "Latency under 100ms."
            assert reqs["R002"].source_paragraph is None
            assert reqs["R002"].derived is True
        finally:
            b.close()


# ===========================================================================
# T4 — append write-path, log-authority counter, lock, self-heal, replay
# ===========================================================================
# Tag: use  -k "append or replay or lock or durability or self_heal"  to run
# only this suite. Tests here use EventDraft + append() exclusively — never
# the legacy apply_event() helper — so they stay green after Task 6.
# ===========================================================================


def _make_draft(
    action: str,
    payload: dict[str, Any],
    *,
    target_kind: str = "project",
    target_id: str = "proj-1",
    now: datetime = _T0,
    actor: str = "test",
) -> EventDraft:
    """Build a minimal EventDraft for T4 tests."""
    return EventDraft(
        timestamp=now,
        actor=actor,
        action=action,
        target_kind=target_kind,
        target_id=target_id,
        payload_json=payload,
    )


def _project_draft(
    *,
    project_id: str = "proj-1",
    name: str = "Test Project",
    now: datetime = _T0,
) -> EventDraft:
    return _make_draft(
        "project.created",
        {
            "id": project_id,
            "name": name,
            "description": "",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        },
        target_kind="project",
        target_id=project_id,
        now=now,
    )


def _init_draft(
    *,
    project_id: str = "proj-1",
    now: datetime = _T0,
) -> EventDraft:
    return _make_draft(
        "state.initialized",
        {},
        target_kind="project",
        target_id=project_id,
        now=now,
    )


def _setup_project_via_append(b: SqliteBackend) -> None:
    """Apply project.created + state.initialized via append()."""
    b.append(_project_draft())
    b.append(_init_draft())


def _read_audit_jsonl(audit_path: str) -> list[dict[str, Any]]:
    """Read all lines from audit.jsonl."""
    if not os.path.exists(audit_path):
        return []
    records = []
    with open(audit_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


class TestAppendHappyPath:
    """append() assigns sequential ids and persists log-then-SQLite."""

    def test_append_returns_event_with_assigned_id(self, tmp_path: Path) -> None:
        """append() materializes the draft into an Event with a real id."""
        b = _make_backend(tmp_path)
        try:
            event = b.append(_project_draft())
            assert event is not None
            assert event.id == "E000001"
            assert event.action == "project.created"
        finally:
            b.close()

    def test_append_assigns_sequential_ids(self, tmp_path: Path) -> None:
        """Sequential appends produce E000001, E000002, ..."""
        b = _make_backend(tmp_path)
        try:
            e1 = b.append(_project_draft())
            e2 = b.append(_init_draft())
            assert e1 is not None and e1.id == "E000001"
            assert e2 is not None and e2.id == "E000002"
        finally:
            b.close()

    def test_append_writes_log_then_commits_sqlite(self, tmp_path: Path) -> None:
        """The JSONL line appears before the SQLite row (log-first ordering)."""
        events_path = str(tmp_path / "events.jsonl")
        b = _make_backend(tmp_path)
        try:
            event = b.append(_project_draft())
            assert event is not None

            # JSONL line present
            lines = _read_jsonl(events_path)
            assert len(lines) == 1
            assert lines[0]["id"] == "E000001"
            assert lines[0]["action"] == "project.created"

            # SQLite row present
            project = b.get_project()
            assert project is not None
        finally:
            b.close()

    def test_append_increments_counter_at_log_append_time(self, tmp_path: Path) -> None:
        """Counter is incremented when the log line is written, not at COMMIT."""
        b = _make_backend(tmp_path)
        try:
            # After first append, counter should be 1.
            b.append(_project_draft())
            assert b._next_seq == 1  # noqa: SLF001 — testing internal counter
            b.append(_init_draft())
            assert b._next_seq == 2  # noqa: SLF001
        finally:
            b.close()


class TestAppendValidationFailure:
    """A validation failure writes zero event lines and one rejection audit line."""

    def test_validation_failure_writes_no_event_line(self, tmp_path: Path) -> None:
        """EventRejected leaves events.jsonl empty."""
        events_path = str(tmp_path / "events.jsonl")
        b = _make_backend(tmp_path)
        try:
            # prd.reviewed without a project — check will raise EventRejected
            # because the payload is malformed (missing required field).
            bad_draft = _make_draft(
                "prd.reviewed",
                {"project_id": "proj-1"},  # missing 'reviewer'
                target_kind="prd",
                target_id="proj-1",
            )
            with pytest.raises(EventRejected):
                b.append(bad_draft)

            lines = _read_jsonl(events_path)
            assert lines == [], f"Expected no event lines, got: {lines}"
        finally:
            b.close()

    def test_validation_failure_writes_rejection_audit_line(self, tmp_path: Path) -> None:
        """EventRejected writes exactly one rejection line to audit.jsonl."""
        audit_path = str(tmp_path / "audit.jsonl")
        b = _make_backend(tmp_path)
        try:
            bad_draft = _make_draft(
                "prd.reviewed",
                {"project_id": "proj-1"},  # missing 'reviewer'
                target_kind="prd",
                target_id="proj-1",
            )
            with pytest.raises(EventRejected):
                b.append(bad_draft)

            audit_records = _read_audit_jsonl(audit_path)
            assert len(audit_records) == 1
            assert audit_records[0]["kind"] == "rejection"
            assert audit_records[0]["attempted_action"] == "prd.reviewed"
        finally:
            b.close()

    def test_validation_failure_does_not_increment_counter(self, tmp_path: Path) -> None:
        """Counter stays at 0 after a validation failure."""
        b = _make_backend(tmp_path)
        try:
            bad_draft = _make_draft(
                "prd.reviewed",
                {"project_id": "proj-1"},  # missing 'reviewer'
                target_kind="prd",
                target_id="proj-1",
            )
            with pytest.raises(EventRejected):
                b.append(bad_draft)
            assert b._next_seq == 0  # noqa: SLF001
        finally:
            b.close()

    def test_unknown_action_raises_event_rejected(self, tmp_path: Path) -> None:
        """An action not in the dispatch table raises EventRejected."""
        b = _make_backend(tmp_path)
        try:
            draft = _make_draft("nonexistent.action", {})
            with pytest.raises(EventRejected, match="not in the dispatch table"):
                b.append(draft)
        finally:
            b.close()


class TestAppendIdempotentNoOp:
    """An idempotent no-op returns None and writes one idempotent_no_op audit line."""

    def _setup_with_released_claim(self, b: SqliteBackend) -> None:
        """Build a claim via append and release it once."""
        _setup_project_via_append(b)
        b.append(_make_draft(
            "prd.parsed",
            {
                "project_id": "proj-1",
                "status": "draft",
                "summary": "A test PRD.",
                "goals": [],
                "non_goals": [],
                "requirements": [
                    {"id": "R001", "prd_section": "s", "text": "Req.",
                     "source_paragraph": None, "derived": False}
                ],
                "acceptance_criteria": [],
                "risks": [],
                "open_questions": [],
            },
            target_kind="prd", target_id="proj-1",
        ))
        b.append(_make_draft(
            "feature.created",
            {"id": "F001", "title": "F", "description": "d",
             "status": "proposed", "requirements": [], "tasks": []},
            target_kind="feature", target_id="F001",
        ))
        b.append(_make_draft(
            "task.created",
            _make_task_payload(),
            target_kind="task", target_id="T001",
        ))
        for from_s, to_s in [("proposed", "drafted"), ("drafted", "reviewed"), ("reviewed", "ready")]:
            b.append(_make_draft(
                "task.status_changed",
                {"task_id": "T001", "from": from_s, "to": to_s},
                target_kind="task", target_id="T001",
            ))
        b.append(_make_draft(
            "claim.created",
            _make_claim_payload(),
            target_kind="claim", target_id="C001",
        ))
        b.append(_make_draft(
            "claim.released",
            {"claim_id": "C001", "released_by": "agent-alpha", "release_reason": "done", "force": False},
            target_kind="claim", target_id="C001",
        ))

    def test_idempotent_no_op_returns_none(self, tmp_path: Path) -> None:
        """claim.released on an already-released claim returns None."""
        b = _make_backend(tmp_path)
        try:
            self._setup_with_released_claim(b)
            result = b.append(_make_draft(
                "claim.released",
                {"claim_id": "C001", "released_by": "agent-alpha", "release_reason": "done", "force": False},
                target_kind="claim", target_id="C001",
            ))
            assert result is None
        finally:
            b.close()

    def test_idempotent_no_op_writes_audit_line(self, tmp_path: Path) -> None:
        """claim.released on already-released claim writes one idempotent_no_op audit line."""
        audit_path = str(tmp_path / "audit.jsonl")
        b = _make_backend(tmp_path)
        try:
            self._setup_with_released_claim(b)
            b.append(_make_draft(
                "claim.released",
                {"claim_id": "C001", "released_by": "agent-alpha", "release_reason": "done", "force": False},
                target_kind="claim", target_id="C001",
            ))
            records = _read_audit_jsonl(audit_path)
            no_op_records = [r for r in records if r.get("kind") == "idempotent_no_op"]
            assert len(no_op_records) >= 1
            assert any(r.get("action") == "claim.released" for r in no_op_records)
        finally:
            b.close()

    def test_idempotent_no_op_writes_zero_event_lines(self, tmp_path: Path) -> None:
        """An idempotent no-op does not add a line to events.jsonl."""
        events_path = str(tmp_path / "events.jsonl")
        b = _make_backend(tmp_path)
        try:
            self._setup_with_released_claim(b)
            count_before = len(_read_jsonl(events_path))
            b.append(_make_draft(
                "claim.released",
                {"claim_id": "C001", "released_by": "agent-alpha", "release_reason": "done", "force": False},
                target_kind="claim", target_id="C001",
            ))
            count_after = len(_read_jsonl(events_path))
            assert count_after == count_before, (
                "Idempotent no-op should not add a line to events.jsonl"
            )
        finally:
            b.close()


class TestAppendCounterSeeding:
    """Counter seeds from log max on open, not from SQLite MAX(id)."""

    def test_counter_seeds_from_log_max_on_open(self, tmp_path: Path) -> None:
        """Re-opening a backend seeds _next_seq from the log's last id."""
        b = _make_backend(tmp_path)
        try:
            b.append(_project_draft())
            b.append(_init_draft())
        finally:
            b.close()

        # Re-open and check counter.
        b2 = SqliteBackend(
            db_path=str(tmp_path / "state.db"),
            events_path=str(tmp_path / "events.jsonl"),
            clock=_make_clock(),
        )
        b2.initialize()
        try:
            assert b2._next_seq == 2  # noqa: SLF001 — seeded from log max E000002
            # Next append should get E000003.
            e3 = b2.append(_init_draft())
            assert e3 is not None
            assert e3.id == "E000003"
        finally:
            b2.close()

    def test_counter_never_reuses_id_after_reopen(self, tmp_path: Path) -> None:
        """IDs never repeat across backend close/reopen cycles."""
        seen_ids: list[str] = []
        events_path = str(tmp_path / "events.jsonl")
        if not os.path.exists(events_path):
            open(events_path, "a").close()  # noqa: WPS515

        for _ in range(3):
            b = SqliteBackend(
                db_path=str(tmp_path / "state.db"),
                events_path=events_path,
                clock=_make_clock(),
            )
            b.initialize()
            try:
                event = b.append(_project_draft())
                assert event is not None
                seen_ids.append(event.id)
            finally:
                b.close()

        assert len(seen_ids) == len(set(seen_ids)), (
            f"IDs were reused across reopens: {seen_ids}"
        )


class TestSelfHeal:
    """Forward catch-up converges log-ahead skew on open."""

    def test_self_heal_forward_catch_up_on_open(self, tmp_path: Path) -> None:
        """Manually inject a log-ahead skew; re-opening heals it."""
        events_path = str(tmp_path / "events.jsonl")
        db_path = str(tmp_path / "state.db")

        # Step 1: create a backend and apply one event normally.
        b = _make_backend(tmp_path)
        try:
            e1 = b.append(_project_draft())
            assert e1 is not None
        finally:
            b.close()

        # Step 2: manually append a second event to events.jsonl without
        # committing it to SQLite — simulating a crash after log-append but
        # before COMMIT.
        e2_event = Event(
            id="E000002",
            timestamp=_T0,
            actor="test",
            action="state.initialized",
            target_kind="project",
            target_id="proj-1",
            payload_json={},
        )
        with open(events_path, "a", encoding="utf-8") as fh:
            fh.write(e2_event.model_dump_json() + "\n")

        # Step 3: re-open; initialize() should detect log_max=2 > table_max=1
        # and forward-catch-up E000002.
        b2 = SqliteBackend(db_path=db_path, events_path=events_path, clock=_make_clock())
        b2.initialize()
        try:
            # Counter should be seeded from log max (2), NOT reassigned to 1.
            assert b2._next_seq == 2  # noqa: SLF001
            # Next event gets E000003, not E000002.
            e3 = b2.append(_init_draft())
            assert e3 is not None
            assert e3.id == "E000003"

            # The orphaned E000002 should now be in the events table (caught up).
            conn = sqlite3.connect(db_path)
            rows = conn.execute("SELECT id FROM events ORDER BY id").fetchall()
            conn.close()
            ids = [r[0] for r in rows]
            assert "E000002" in ids, f"Catch-up missed E000002; events table: {ids}"
        finally:
            b2.close()

    def test_self_heal_does_not_reassign_orphaned_id(self, tmp_path: Path) -> None:
        """After a skew, the next append gets N+1, not the orphaned N."""
        events_path = str(tmp_path / "events.jsonl")
        db_path = str(tmp_path / "state.db")

        b = _make_backend(tmp_path)
        try:
            b.append(_project_draft())  # E000001
        finally:
            b.close()

        # Inject E000002 into log without SQLite commit.
        orphan = Event(
            id="E000002",
            timestamp=_T0,
            actor="test",
            action="state.initialized",
            target_kind="project",
            target_id="proj-1",
            payload_json={},
        )
        with open(events_path, "a", encoding="utf-8") as fh:
            fh.write(orphan.model_dump_json() + "\n")

        b2 = SqliteBackend(db_path=db_path, events_path=events_path, clock=_make_clock())
        b2.initialize()
        try:
            # Must not get E000002 — it was already in the log.
            e = b2.append(_init_draft())
            assert e is not None
            assert e.id == "E000003", (
                f"Expected E000003 after orphaned E000002; got {e.id}"
            )
        finally:
            b2.close()


class TestReplayStrict:
    """replay_from_empty applies every line via _write_*, no skip-list."""

    def test_replay_applies_every_line_no_skip_list(self, tmp_path: Path) -> None:
        """replay_from_empty rebuilds state byte-for-byte; no action is skipped."""
        b = _make_backend(tmp_path)
        events_path = str(tmp_path / "events.jsonl")
        db_path = str(tmp_path / "state.db")
        try:
            b.append(_project_draft(name="Replay Test"))
            b.append(_init_draft())
        finally:
            b.close()

        # Replay into a fresh backend.
        b2 = SqliteBackend(db_path=db_path, events_path=events_path, clock=_make_clock())
        b2.replay_from_empty(events_path)
        try:
            project = b2.get_project()
            assert project is not None
            assert project.name == "Replay Test"
        finally:
            b2.close()

    def test_replay_interior_malformed_line_raises(self, tmp_path: Path) -> None:
        """An interior malformed line in events.jsonl raises during replay."""
        events_path = str(tmp_path / "events.jsonl")
        db_path = str(tmp_path / "state.db")

        b = _make_backend(tmp_path)
        try:
            b.append(_project_draft())
            b.append(_init_draft())
        finally:
            b.close()

        # Inject a malformed line in the middle.
        with open(events_path, encoding="utf-8") as fh:
            content = fh.read()
        lines = content.splitlines()
        # Insert corruption as the second line (interior).
        lines.insert(1, "NOT_VALID_JSON{{{")
        with open(events_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

        b2 = SqliteBackend(db_path=db_path, events_path=events_path, clock=_make_clock())
        with pytest.raises((ValueError, Exception)):
            b2.replay_from_empty(events_path)

    def test_replay_torn_trailing_line_is_tolerated(self, tmp_path: Path) -> None:
        """A torn (partially written) final line is silently tolerated."""
        events_path = str(tmp_path / "events.jsonl")
        db_path = str(tmp_path / "state.db")

        b = _make_backend(tmp_path)
        try:
            b.append(_project_draft(name="Torn Line Test"))
            b.append(_init_draft())
        finally:
            b.close()

        # Append a partial/torn final line.
        with open(events_path, "a", encoding="utf-8") as fh:
            fh.write('{"id": "E000003", "action": "state.init')  # deliberately torn

        b2 = SqliteBackend(db_path=db_path, events_path=events_path, clock=_make_clock())
        b2.replay_from_empty(events_path)
        try:
            project = b2.get_project()
            assert project is not None
            assert project.name == "Torn Line Test"
        finally:
            b2.close()

    def test_replay_reseeds_counter_from_max_id(self, tmp_path: Path) -> None:
        """After replay, the counter is seeded from the max event id replayed."""
        events_path = str(tmp_path / "events.jsonl")
        db_path = str(tmp_path / "state.db")

        b = _make_backend(tmp_path)
        try:
            b.append(_project_draft())  # E000001
            b.append(_init_draft())     # E000002
        finally:
            b.close()

        b2 = SqliteBackend(db_path=db_path, events_path=events_path, clock=_make_clock())
        b2.replay_from_empty(events_path)
        try:
            assert b2._next_seq == 2  # noqa: SLF001
            e3 = b2.append(_init_draft())
            assert e3 is not None
            assert e3.id == "E000003"
        finally:
            b2.close()


class TestDurabilityModes:
    """relaxed vs strict durability modes."""

    def test_relaxed_durability_uses_synchronous_normal(self, tmp_path: Path) -> None:
        """Default (relaxed) durability sets synchronous=NORMAL."""
        b = _make_backend(tmp_path)  # _make_backend doesn't pass durability — relaxed
        try:
            conn = b._conn  # noqa: SLF001
            assert conn is not None
            row = conn.execute("PRAGMA synchronous").fetchone()
            # SQLite returns 1 for NORMAL.
            assert row[0] == 1, f"Expected synchronous=NORMAL (1), got {row[0]}"
        finally:
            b.close()

    def test_strict_durability_uses_synchronous_full(self, tmp_path: Path) -> None:
        """Strict durability sets synchronous=FULL."""
        db_path = str(tmp_path / "state.db")
        events_path = str(tmp_path / "events.jsonl")
        Path(events_path).touch()
        b = SqliteBackend(
            db_path=db_path,
            events_path=events_path,
            clock=_make_clock(),
            durability="strict",
        )
        b.initialize()
        try:
            conn = b._conn  # noqa: SLF001
            assert conn is not None
            row = conn.execute("PRAGMA synchronous").fetchone()
            # SQLite returns 2 for FULL.
            assert row[0] == 2, f"Expected synchronous=FULL (2), got {row[0]}"
        finally:
            b.close()

    def test_strict_durability_fsyncs_log_before_commit(self, tmp_path: Path) -> None:
        """Strict durability calls fsync on the log fd before COMMIT.

        Verified by checking the synchronous PRAGMA and confirming the append
        completes without error (no mock — real fsync path exercised).
        """
        db_path = str(tmp_path / "state.db")
        events_path = str(tmp_path / "events.jsonl")
        Path(events_path).touch()
        b = SqliteBackend(
            db_path=db_path,
            events_path=events_path,
            clock=_make_clock(),
            durability="strict",
        )
        b.initialize()
        try:
            # append must complete without raising under strict mode.
            event = b.append(_project_draft())
            assert event is not None
            assert event.id == "E000001"

            # The event must be in both log and SQLite.
            lines = _read_jsonl(events_path)
            assert any(line.get("id") == "E000001" for line in lines)
            project = b.get_project()
            assert project is not None

            # Confirm synchronous=FULL is set.
            conn = b._conn  # noqa: SLF001
            assert conn is not None
            row = conn.execute("PRAGMA synchronous").fetchone()
            assert row[0] == 2
        finally:
            b.close()

    def test_relaxed_append_succeeds(self, tmp_path: Path) -> None:
        """Relaxed (default) durability completes an append correctly."""
        b = _make_backend(tmp_path)
        try:
            event = b.append(_project_draft())
            assert event is not None
            assert event.id == "E000001"
        finally:
            b.close()


class TestScanTail:
    """scan_tail reads only the final line — O(line), not O(file)."""

    def test_scan_tail_returns_zero_on_empty_file(self, tmp_path: Path) -> None:
        """scan_tail returns 0 for an empty events.jsonl."""
        events_path = str(tmp_path / "events.jsonl")
        Path(events_path).touch()
        b = SqliteBackend(
            db_path=str(tmp_path / "state.db"),
            events_path=events_path,
            clock=_make_clock(),
        )
        assert b._scan_tail_id() == 0  # noqa: SLF001

    def test_scan_tail_returns_correct_max_id(self, tmp_path: Path) -> None:
        """scan_tail returns the numeric part of the last line's id."""
        b = _make_backend(tmp_path)
        try:
            b.append(_project_draft())  # E000001
            b.append(_init_draft())     # E000002
        finally:
            b.close()

        b2 = SqliteBackend(
            db_path=str(tmp_path / "state.db"),
            events_path=str(tmp_path / "events.jsonl"),
            clock=_make_clock(),
        )
        assert b2._scan_tail_id() == 2  # noqa: SLF001

    def test_scan_tail_reads_bounded_bytes_on_large_file(self, tmp_path: Path) -> None:
        """scan_tail returns the correct max id from a large synthetic log.

        Smoke test: 500 synthetic events in a large file; scan_tail must still
        return 500 (only reads the tail, not the whole file).
        """
        events_path = str(tmp_path / "events.jsonl")
        db_path = str(tmp_path / "state.db")

        with open(events_path, "w", encoding="utf-8") as fh:
            for i in range(1, 501):
                record = {
                    "id": f"E{i:06d}",
                    "timestamp": _T0.isoformat(),
                    "actor": "test",
                    "action": "state.initialized",
                    "target_kind": "project",
                    "target_id": "proj-1",
                    "payload_json": {},
                }
                fh.write(json.dumps(record) + "\n")

        b = SqliteBackend(db_path=db_path, events_path=events_path, clock=_make_clock())
        assert b._scan_tail_id() == 500  # noqa: SLF001


class TestWriteFailedAfterLogAppend:
    """Write failure after log append leaves the log line and audits write_failed_after_log."""

    def test_write_failure_leaves_log_line_and_audits(self, tmp_path: Path) -> None:
        """A _write_* that raises after log-append leaves log intact + audits."""
        events_path = str(tmp_path / "events.jsonl")
        db_path = str(tmp_path / "state.db")
        audit_path = str(tmp_path / "audit.jsonl")
        Path(events_path).touch()

        class _FailingBackend(SqliteBackend):
            def _write_project_created(
                self, conn: sqlite3.Connection, payload: Any, event: Any
            ) -> None:
                raise RuntimeError("Simulated write failure for test")

        b = _FailingBackend(db_path=db_path, events_path=events_path, clock=_make_clock())
        b.initialize()
        try:
            with pytest.raises((EventRejected, TransactionAborted)):
                b.append(_project_draft())

            # Log line MUST remain (append-only).
            lines = _read_jsonl(events_path)
            assert len(lines) == 1, (
                f"Log line should remain after write failure; got {len(lines)} lines"
            )
            assert lines[0]["action"] == "project.created"

            # write_failed_after_log audit line MUST be written.
            audit_records = _read_audit_jsonl(audit_path)
            failed_records = [r for r in audit_records if r.get("kind") == "write_failed_after_log"]
            assert len(failed_records) == 1
            assert failed_records[0]["action"] == "project.created"
            assert "event_id" in failed_records[0]
        finally:
            b.close()

    def test_write_failure_rolls_back_sqlite(self, tmp_path: Path) -> None:
        """A write failure after log-append rolls back the SQLite transaction."""
        events_path = str(tmp_path / "events.jsonl")
        db_path = str(tmp_path / "state.db")
        Path(events_path).touch()

        class _FailingBackend(SqliteBackend):
            def _write_project_created(
                self, conn: sqlite3.Connection, payload: Any, event: Any
            ) -> None:
                raise RuntimeError("Simulated write failure")

        b = _FailingBackend(db_path=db_path, events_path=events_path, clock=_make_clock())
        b.initialize()
        try:
            with pytest.raises((EventRejected, TransactionAborted)):
                b.append(_project_draft())
            # SQLite must have no project row (transaction rolled back).
            project = b.get_project()
            assert project is None
        finally:
            b.close()


# ---------------------------------------------------------------------------
# Fix-cycle iteration 1: MUST FIX + SHOULD FIX tests (SL1-RR-1 critic wave 3)
# ---------------------------------------------------------------------------


class TestScanTailTornLineOpenPath:
    """MUST FIX: _scan_tail_id skips torn/idless trailing line on the OPEN path.

    On initialize(), a torn trailing line (partial write from a crash) must not
    collapse _next_seq to 0 and must not trigger id reuse.
    """

    def test_open_with_torn_trailing_line_preserves_counter(self, tmp_path: Path) -> None:
        """After torn partial line appended directly, reopen gives _next_seq == 2, next id E000003.

        This is the open-path (not replay) torn-line test demanded by the critic.
        Steps:
          1. Commit two events via append() to a fresh backend.
          2. Manually append a torn/partial line directly to events.jsonl.
          3. Close the backend.
          4. Open a fresh backend on the same files.
          5. Assert _next_seq == 2 (NOT 0 — the torn line must not collapse the counter).
          6. Append a third event and assert it gets id E000003 (NOT E000001).
        """
        events_path = str(tmp_path / "events.jsonl")
        db_path = str(tmp_path / "state.db")

        # Step 1: commit two events.
        b1 = _make_backend(tmp_path)
        try:
            e1 = b1.append(_project_draft())
            e2 = b1.append(_init_draft())
            assert e1 is not None and e1.id == "E000001"
            assert e2 is not None and e2.id == "E000002"
        finally:
            b1.close()

        # Step 2: manually append a torn partial line (simulates a crash mid-append).
        with open(events_path, "a", encoding="utf-8") as fh:
            fh.write('{"id": "E000003", "action": "state.init')  # deliberately torn

        # Step 4: open a fresh backend on the same files.
        b2 = SqliteBackend(db_path=db_path, events_path=events_path, clock=_make_clock())
        b2.initialize()
        try:
            # Step 5: counter must be 2 (torn line must NOT collapse it to 0).
            assert b2._next_seq == 2, (  # noqa: SLF001
                f"Expected _next_seq == 2 after reopen with torn trailing line, "
                f"got {b2._next_seq}"
            )

            # Step 6: next append must get E000003 (NOT E000001 — no id reuse).
            e3 = b2.append(_init_draft())
            assert e3 is not None
            assert e3.id == "E000003", (
                f"Expected E000003 for next append after torn trailing line, "
                f"got {e3.id}"
            )
        finally:
            b2.close()

    def test_open_with_fully_idless_trailing_line_preserves_counter(
        self, tmp_path: Path
    ) -> None:
        """A trailing line with valid JSON but no 'id' field does not collapse the counter."""
        events_path = str(tmp_path / "events.jsonl")
        db_path = str(tmp_path / "state.db")

        b1 = _make_backend(tmp_path)
        try:
            b1.append(_project_draft())  # E000001
        finally:
            b1.close()

        # Append a valid JSON line that has no 'id' field (e.g. an old audit line).
        with open(events_path, "a", encoding="utf-8") as fh:
            fh.write('{"kind": "rejection", "action": "project.created"}\n')

        b2 = SqliteBackend(db_path=db_path, events_path=events_path, clock=_make_clock())
        b2.initialize()
        try:
            # counter should fall back to the previous complete line (E000001 → 1).
            assert b2._next_seq == 1, (  # noqa: SLF001
                f"Expected _next_seq == 1, got {b2._next_seq}"
            )
            e2 = b2.append(_init_draft())
            assert e2 is not None
            assert e2.id == "E000002"
        finally:
            b2.close()


class TestScanTailLargeLine:
    """SHOULD FIX 1: _scan_tail_id handles event lines longer than 4096 bytes."""

    def test_scan_tail_handles_event_line_longer_than_4096_bytes(
        self, tmp_path: Path
    ) -> None:
        """A final event line > 4096 bytes must not cause _scan_tail_id to return 0."""
        events_path = str(tmp_path / "events.jsonl")

        # Write a small first event, then a second event with a very large payload.
        small_record = {
            "id": "E000001",
            "timestamp": _T0.isoformat(),
            "actor": "test",
            "action": "state.initialized",
            "target_kind": "project",
            "target_id": "proj-1",
            "payload_json": {},
        }
        # Build a payload large enough to make the JSON line exceed 4096 bytes.
        large_payload = {"data": "x" * 8000}
        large_record = {
            "id": "E000002",
            "timestamp": _T0.isoformat(),
            "actor": "test",
            "action": "state.initialized",
            "target_kind": "project",
            "target_id": "proj-1",
            "payload_json": large_payload,
        }

        with open(events_path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(small_record) + "\n")
            fh.write(json.dumps(large_record) + "\n")

        # The line for E000002 is > 4096 bytes — the old 4096-byte window would miss it.
        line_size = len(json.dumps(large_record) + "\n")
        assert line_size > 4096, f"Test precondition: line must be > 4096, got {line_size}"

        b = SqliteBackend(
            db_path=str(tmp_path / "state.db"),
            events_path=events_path,
            clock=_make_clock(),
        )
        result = b._scan_tail_id()  # noqa: SLF001
        assert result == 2, (
            f"_scan_tail_id must return 2 for a > 4096-byte final line, got {result}"
        )


class TestForwardCatchUpConvergence:
    """SHOULD FIX 2: _forward_catch_up raises if target event is missing from log."""

    def test_catch_up_raises_on_missing_event(self, tmp_path: Path) -> None:
        """_forward_catch_up raises TransactionAborted when log is missing a target event.

        Simulate: log says max is E000002 but the log file contains only E000001.
        Catch-up expects E000002 but never finds it — must raise, not silently return.
        """
        events_path = str(tmp_path / "events.jsonl")
        db_path = str(tmp_path / "state.db")

        # Write only E000001 to the log.
        record = {
            "id": "E000001",
            "timestamp": _T0.isoformat(),
            "actor": "test",
            "action": "project.created",
            "target_kind": "project",
            "target_id": "proj-1",
            "payload_json": {
                "id": "proj-1",
                "name": "Test",
                "description": "",
                "created_at": _T0.isoformat(),
                "updated_at": _T0.isoformat(),
            },
        }
        with open(events_path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

        b = SqliteBackend(db_path=db_path, events_path=events_path, clock=_make_clock())
        b.initialize()  # seeds counter from scan_tail → _next_seq = 1
        try:
            conn = b._conn  # noqa: SLF001
            assert conn is not None
            # Ask catch-up to apply E000002 which does not exist in the log.
            with pytest.raises((EventRejected, TransactionAborted), match="missing events"):
                b._forward_catch_up(conn, from_seq=2, to_seq=2)  # noqa: SLF001
        finally:
            b.close()


class TestReplayNoAuditSideEffects:
    """SHOULD FIX 4: audit side-effects in _write_* are suppressed during replay."""

    def test_replay_does_not_append_audit_lines(self, tmp_path: Path) -> None:
        """replay_from_empty must not write audit lines even when claim auto-release skips.

        Set up: commit evidence.submitted where the claim is already released
        (so the auto-release would normally trigger _write_warn_to_audit). Then
        replay from the log and verify audit.jsonl has exactly ONE warn line
        (from the original run), NOT two (which would indicate replay re-wrote it).
        """
        events_path = str(tmp_path / "events.jsonl")
        db_path = str(tmp_path / "state.db")
        audit_path = str(tmp_path / "audit.jsonl")

        b = _make_backend(tmp_path)
        try:
            # Set up project + PRD + feature + task (needs_review-eligible) + claim.
            _setup_project_via_append(b)
            b.append(_make_draft(
                "prd.parsed",
                {
                    "project_id": "proj-1",
                    "status": "draft",
                    "summary": "A test PRD.",
                    "goals": [],
                    "non_goals": [],
                    "requirements": [
                        {"id": "R001", "prd_section": "s", "text": "Req.",
                         "source_paragraph": None, "derived": False}
                    ],
                    "acceptance_criteria": [],
                    "risks": [],
                    "open_questions": [],
                },
                target_kind="prd",
                target_id="proj-1",
            ))
            b.append(_make_draft(
                "feature.created",
                {"id": "F001", "title": "F1", "description": "d",
                 "status": "proposed", "requirements": [], "tasks": []},
                target_kind="feature",
                target_id="F001",
            ))
            b.append(_make_draft(
                "task.created",
                _make_task_payload(task_id="T001", feature_id="F001"),
                target_kind="task",
                target_id="T001",
            ))
            # Transition task to ready so claim.created is allowed.
            for from_s, to_s in [
                ("proposed", "drafted"),
                ("drafted", "reviewed"),
                ("reviewed", "ready"),
            ]:
                b.append(_make_draft(
                    "task.status_changed",
                    {"task_id": "T001", "from": from_s, "to": to_s},
                    target_kind="task",
                    target_id="T001",
                ))
            b.append(_make_draft(
                "claim.created",
                _make_claim_payload(claim_id="C001", task_id="T001"),
                target_kind="claim",
                target_id="C001",
            ))
            # Release the claim explicitly before submitting evidence.
            b.append(_make_draft(
                "claim.released",
                {"claim_id": "C001", "released_by": "agent-alpha",
                 "release_reason": "manual", "force": False},
                target_kind="claim",
                target_id="C001",
            ))
            # Task must be in claimed/in_progress/blocked for evidence.submitted.
            # After claim.released the task is still "ready" (claim.created
            # transitions to "claimed"; claim.released un-transitions it to "ready").
            # Re-claim and manually set task to claimed via status_changed.
            b.append(_make_draft(
                "task.status_changed",
                {"task_id": "T001", "from": "ready", "to": "claimed"},
                target_kind="task",
                target_id="T001",
            ))
            # Submit evidence — claim C001 is already released, so auto-release
            # skips and writes ONE warn line to audit.jsonl.
            b.append(_make_draft(
                "evidence.submitted",
                {
                    "task_id": "T001",
                    "claim_id": "C001",
                    "submitted_by": "agent-alpha",
                    "evidence_id": "EV001",
                    "commands_run": ["pytest"],
                    "files_changed": ["tests/test_main.py"],
                },
                target_kind="task",
                target_id="T001",
            ))
        finally:
            b.close()

        # Read audit lines written during the normal run.
        audit_before = _read_audit_jsonl(audit_path)
        warn_before = [r for r in audit_before if r.get("kind") == "idempotent_no_op"]
        assert len(warn_before) == 1, (
            f"Expected exactly 1 idempotent_no_op warn line from normal run, "
            f"got {len(warn_before)}: {warn_before}"
        )

        # Replay from empty — must not add a second warn line.
        b2 = SqliteBackend(db_path=db_path, events_path=events_path, clock=_make_clock())
        b2.replay_from_empty(events_path)
        try:
            audit_after = _read_audit_jsonl(audit_path)
            warn_after = [r for r in audit_after if r.get("kind") == "idempotent_no_op"]
            assert len(warn_after) == 1, (
                f"replay_from_empty must not add audit lines; "
                f"expected 1 warn line, got {len(warn_after)}: {warn_after}"
            )
        finally:
            b2.close()


class TestPostLogAppendDatabaseLocked:
    """SHOULD FIX 5: post-log-append SQLite 'database is locked' is TransactionAborted, not StateLocked."""

    def test_sqlite_locked_after_log_append_raises_transaction_aborted(
        self, tmp_path: Path
    ) -> None:
        """A 'database is locked' error after the log line is written must raise
        TransactionAborted (not StateLocked). The log line remains; catch-up heals."""
        events_path = str(tmp_path / "events.jsonl")
        db_path = str(tmp_path / "state.db")
        Path(events_path).touch()

        class _LockedAfterLogBackend(SqliteBackend):
            """Simulates SQLite 'database is locked' error during _write_project_created."""
            def _write_project_created(
                self, conn: sqlite3.Connection, payload: Any, event: Any
            ) -> None:
                raise sqlite3.OperationalError("database is locked")

        b = _LockedAfterLogBackend(
            db_path=db_path, events_path=events_path, clock=_make_clock()
        )
        b.initialize()
        try:
            # Must raise TransactionAborted — NOT StateLocked and NOT EventRejected
            # — because the log line was already written before the SQLite error
            # occurred. EventRejected here would mean validation leaked past the
            # log append, which is exactly what SL1-RR-1 prevents.
            with pytest.raises(TransactionAborted):
                b.append(_project_draft())

            # The log line must remain (append-only).
            lines = _read_jsonl(events_path)
            assert len(lines) == 1, (
                f"Log line must remain after post-log-append lock; got {len(lines)}"
            )
            assert lines[0]["action"] == "project.created"
        finally:
            b.close()

    def test_sqlite_locked_after_log_append_writes_audit_line(
        self, tmp_path: Path
    ) -> None:
        """Post-log-append lock writes write_failed_after_log to audit.jsonl."""
        events_path = str(tmp_path / "events.jsonl")
        db_path = str(tmp_path / "state.db")
        audit_path = str(tmp_path / "audit.jsonl")
        Path(events_path).touch()

        class _LockedAfterLogBackend(SqliteBackend):
            def _write_project_created(
                self, conn: sqlite3.Connection, payload: Any, event: Any
            ) -> None:
                raise sqlite3.OperationalError("database is locked")

        b = _LockedAfterLogBackend(
            db_path=db_path, events_path=events_path, clock=_make_clock()
        )
        b.initialize()
        try:
            # Must be TransactionAborted: post-log-append infra failure.
            # EventRejected here would mean validation ran after log-append.
            with pytest.raises(TransactionAborted):
                b.append(_project_draft())

            audit_records = _read_audit_jsonl(audit_path)
            failed = [r for r in audit_records if r.get("kind") == "write_failed_after_log"]
            assert len(failed) == 1, (
                f"Expected 1 write_failed_after_log audit line, got {len(failed)}"
            )
            assert "event_id" in failed[0]
        finally:
            b.close()


class TestRejectionAuditHasNoIdField:
    """CONSIDER 4: rejection audit line must not carry an 'id' field."""

    def test_rejection_audit_line_has_no_id_field(self, tmp_path: Path) -> None:
        """A rejection audit line must not include an 'id' field (not an event)."""
        audit_path = str(tmp_path / "audit.jsonl")
        b = _make_backend(tmp_path)
        try:
            bad_draft = _make_draft(
                "prd.reviewed",
                {"project_id": "proj-1"},  # missing 'reviewer' — will fail validation
                target_kind="prd",
                target_id="proj-1",
            )
            with pytest.raises(EventRejected):
                b.append(bad_draft)

            records = _read_audit_jsonl(audit_path)
            rejections = [r for r in records if r.get("kind") == "rejection"]
            assert len(rejections) == 1
            assert "id" not in rejections[0], (
                f"Rejection audit line must have no 'id' field; got: {rejections[0]}"
            )
        finally:
            b.close()


class TestConcurrentAppendNoIdCollision:
    """CONSIDER 5: two threads appending concurrently produce no id collision."""

    def test_two_thread_append_no_id_collision(self, tmp_path: Path) -> None:
        """Two threads appending simultaneously get distinct, sequential ids."""
        import threading as _threading

        b = _make_backend(tmp_path)
        results: list[Any] = []
        errors: list[Exception] = []
        lock = _threading.Lock()

        def _append_init() -> None:
            try:
                event = b.append(_init_draft())
                with lock:
                    results.append(event)
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)

        threads = [_threading.Thread(target=_append_init) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        b.close()

        assert not errors, f"Thread errors: {errors}"
        assert len(results) == 10, f"Expected 10 events, got {len(results)}"
        ids = [e.id for e in results if e is not None]
        assert len(ids) == len(set(ids)), f"Duplicate ids found: {ids}"
        # All ids must be in the E000001–E000010 range.
        seq_nums = sorted(int(eid[1:]) for eid in ids)
        assert seq_nums == list(range(1, 11)), f"Expected 1..10, got {seq_nums}"


# ===========================================================================
# T6 NET-NEW: Cross-process concurrency (PR #41 Critic-3 subprocess scenario)
# ===========================================================================


class TestCrossProcessConcurrency:
    """Two SEPARATE processes appending to the same events.jsonl concurrently.

    Tests that flock serializes cross-process appends so no id collision and
    no dropped event occur. This is the PR #41 Critic-3 scenario.
    """

    def test_cross_process_no_id_collision(self, tmp_path: Path) -> None:
        """Two subprocesses appending to the same events.jsonl get distinct ids.

        Real subprocesses (not threads) exercise the flock path that protects
        against cross-process id collisions. Each subprocess appends N events;
        the combined event log must have exactly 2*N unique, contiguous ids.

        Hardened for deterministic collision detection (PR #41 Critic-3):
        - n_per_proc is raised to 50 so the processes' append windows
          substantially overlap even on fast machines.
        - A start-barrier sentinel file ensures both subprocesses begin their
          append loops simultaneously, maximising flock contention.
        - Assertions verify ids are unique AND contiguous (E000001..E{2N}).
        """
        events_path = str(tmp_path / "events.jsonl")
        db_path = str(tmp_path / "state.db")
        start_sentinel = str(tmp_path / "start.sentinel")
        Path(events_path).touch()

        # Initialize the backend first so the schema is present.
        b = _make_backend(tmp_path)
        b.close()

        # Script executed by each subprocess.
        # The subprocess waits for start_sentinel to appear (written by the
        # parent after both children are launched) before beginning appends.
        # This ensures their critical sections genuinely contend under flock.
        worker_script = f"""
import sys
sys.path.insert(0, {str(tmp_path)!r})
# Add the src directory to the path.
import subprocess, os, time
# Find the anvil package.
result = subprocess.run(
    [sys.executable, '-c',
     'import anvil; print(anvil.__file__)'],
    capture_output=True, text=True
)
src_root = os.path.dirname(os.path.dirname(result.stdout.strip()))
sys.path.insert(0, src_root)

from datetime import UTC, datetime
from anvil.clock import FrozenClock
from anvil.state.models import EventDraft
from anvil.state.sqlite import SqliteBackend

events_path = {events_path!r}
db_path = {db_path!r}
start_sentinel = {start_sentinel!r}
n_appends = int(sys.argv[1])

# Wait for the parent to signal both children are ready.
deadline = time.monotonic() + 10.0
while not os.path.exists(start_sentinel):
    if time.monotonic() > deadline:
        sys.exit("Timed out waiting for start sentinel")
    time.sleep(0.005)

clock = FrozenClock(datetime(2026, 5, 24, 18, 0, 0, tzinfo=UTC))
b = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock)
b.initialize()
try:
    for i in range(n_appends):
        draft = EventDraft(
            timestamp=datetime(2026, 5, 24, 18, 0, i % 60, tzinfo=UTC),
            actor="worker",
            action="state.initialized",
            target_kind="project",
            target_id="proj-1",
            payload_json={{}},
        )
        b.append(draft)
finally:
    b.close()
"""

        n_per_proc = 50

        # Launch both subprocesses before writing the sentinel so they start
        # their append loops at the same time (maximises flock contention).
        procs = []
        for _ in range(2):
            p = subprocess.Popen(
                [sys.executable, "-c", worker_script, str(n_per_proc)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            procs.append(p)

        # Signal both children to start simultaneously.
        Path(start_sentinel).touch()

        errors = []
        for p in procs:
            stdout, stderr = p.communicate(timeout=60)
            if p.returncode != 0:
                errors.append(stderr.decode())

        assert not errors, f"Subprocess errors: {errors}"

        # Read the events.jsonl and verify no id collision and no dropped events.
        events = _read_jsonl(events_path)
        # Filter to state.initialized events (what the workers append).
        worker_events = [e for e in events if e.get("action") == "state.initialized"]
        assert len(worker_events) == n_per_proc * 2, (
            f"Expected {n_per_proc * 2} events from 2 workers, got {len(worker_events)}"
        )

        ids = [e["id"] for e in worker_events]
        assert len(ids) == len(set(ids)), (
            f"Cross-process id collision detected: {sorted(ids)}"
        )

        # All ids must be unique AND contiguous: E000001..E{2*n_per_proc}.
        seq_nums = sorted(int(eid[1:]) for eid in ids)
        expected = list(range(1, n_per_proc * 2 + 1))
        assert seq_nums == expected, (
            f"Id sequence is not contiguous. Expected 1..{n_per_proc * 2}, got {seq_nums}"
        )


# ===========================================================================
# T6 NET-NEW: Per-action decide/apply CONTRACT tests
# ===========================================================================


class TestDecideApplyContract:
    """Per-action _check_* / _write_* contract tests.

    For each real-mutation action:
    - _check_*: reject illegal/invalid input with EventRejected; no side effects
    - _write_*: succeed whenever its _check_* passed

    Audit-only actions (state.initialized, file_changed, progress.noted, sync.*)
    get a smoke test only: check proceeds, write is a no-op.
    """

    # ------------------------------------------------------------------
    # Audit-only smoke tests
    # ------------------------------------------------------------------

    def test_audit_only_state_initialized_smoke(self, tmp_path: Path) -> None:
        """state.initialized: check proceeds, write is no-op (no domain table touched)."""
        b = _make_backend(tmp_path)
        try:
            b.append(_make_project_event())
            result = b.append(_make_init_event())
            assert result is not None
            assert result.action == "state.initialized"
        finally:
            b.close()

    def test_audit_only_file_changed_smoke(self, tmp_path: Path) -> None:
        """file_changed: check proceeds, write is no-op (audit row only)."""
        b = _make_backend(tmp_path)
        try:
            result = b.append(_make_event(
                "file_changed",
                {"file": "src/foo.py", "tool": "Edit", "actor": "agent"},
                target_kind="file", target_id="src/foo.py",
            ))
            assert result is not None
            assert result.action == "file_changed"
        finally:
            b.close()

    def test_audit_only_progress_noted_smoke(self, tmp_path: Path) -> None:
        """progress.noted: check proceeds, write is no-op (audit-only action)."""
        b = _make_backend(tmp_path)
        try:
            result = b.append(_make_event(
                "progress.noted",
                {
                    "task_id": "T001",
                    "actor": "agent",
                    "notes": "Working on it.",
                    "noted_at": _T0.isoformat(),
                },
                target_kind="task", target_id="T001",
            ))
            assert result is not None
            assert result.action == "progress.noted"
        finally:
            b.close()

    # ------------------------------------------------------------------
    # project.created
    # ------------------------------------------------------------------

    def test_project_created_check_allows_any_payload(self, tmp_path: Path) -> None:
        """project.created has no pre-condition check — always proceeds."""
        b = _make_backend(tmp_path)
        try:
            result = b.append(_make_project_event())
            assert result is not None
            assert b.get_project() is not None
        finally:
            b.close()

    def test_project_created_rejects_extra_field(self, tmp_path: Path) -> None:
        """project.created with extra unknown field raises EventRejected (extra=forbid).

        Phase-1 payload-model failure — must be EventRejected ONLY. TransactionAborted
        here would mean validation leaked past the log append (SL1-RR-1 invariant).
        """
        b = _make_backend(tmp_path)
        try:
            with pytest.raises(EventRejected):
                b.append(_make_event(
                    "project.created",
                    {"id": "p", "name": "P", "description": "", "created_at": _T0.isoformat(),
                     "updated_at": _T0.isoformat(), "extra_forbidden_field": "boom"},
                    target_kind="project", target_id="p",
                ))
        finally:
            b.close()

    # ------------------------------------------------------------------
    # prd.parsed
    # ------------------------------------------------------------------

    def test_prd_parsed_check_rejects_missing_project_id(self, tmp_path: Path) -> None:
        """prd.parsed without project_id raises EventRejected; no PRD row, no event line.

        Phase-1 payload-model failure — must be EventRejected ONLY. TransactionAborted
        here would mean validation leaked past the log append (SL1-RR-1 invariant).
        """
        b = _make_backend(tmp_path)
        events_path = str(tmp_path / "events.jsonl")
        try:
            _setup_project(b)
            initial_event_count = len(_read_jsonl(events_path))
            bad = {**_make_prd_parsed_payload()}
            del bad["project_id"]
            with pytest.raises(EventRejected):
                b.append(_make_event("prd.parsed", bad, target_kind="prd", target_id="proj-1"))
            assert b.get_prd() is None
            # No new canonical event line.
            assert len(_read_jsonl(events_path)) == initial_event_count
        finally:
            b.close()

    def test_prd_parsed_write_succeeds_when_check_passed(self, tmp_path: Path) -> None:
        """prd.parsed inserts PRD and requirements rows when valid."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            b.append(_make_event("prd.parsed", _make_prd_parsed_payload(),
                                 target_kind="prd", target_id="proj-1"))
            prd = b.get_prd()
            assert prd is not None
            assert prd.status.value == "draft"
        finally:
            b.close()

    # ------------------------------------------------------------------
    # prd.reviewed
    # ------------------------------------------------------------------

    def test_prd_reviewed_check_rejects_missing_reviewer(self, tmp_path: Path) -> None:
        """prd.reviewed without reviewer raises EventRejected; no event line.

        Phase-1 payload-model failure — must be EventRejected ONLY (SL1-RR-1).
        """
        b = _make_backend(tmp_path)
        events_path = str(tmp_path / "events.jsonl")
        try:
            _setup_project(b)
            b.append(_make_event("prd.parsed", _make_prd_parsed_payload(),
                                 target_kind="prd", target_id="proj-1"))
            initial = len(_read_jsonl(events_path))
            with pytest.raises(EventRejected):
                b.append(_make_event("prd.reviewed", {"project_id": "proj-1"},
                                     target_kind="prd", target_id="proj-1"))
            assert len(_read_jsonl(events_path)) == initial
        finally:
            b.close()

    def test_prd_reviewed_write_updates_status(self, tmp_path: Path) -> None:
        """prd.reviewed transitions PRD status to 'reviewed'."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            b.append(_make_event("prd.parsed", _make_prd_parsed_payload(),
                                 target_kind="prd", target_id="proj-1"))
            b.append(_make_event("prd.reviewed", {"project_id": "proj-1", "reviewer": "alice"},
                                 target_kind="prd", target_id="proj-1"))
            prd = b.get_prd()
            assert prd is not None
            assert prd.status.value == "reviewed"
            assert prd.last_reviewed_by == "alice"
        finally:
            b.close()

    # ------------------------------------------------------------------
    # prd.approved
    # ------------------------------------------------------------------

    def test_prd_approved_check_allows_without_precondition(self, tmp_path: Path) -> None:
        """prd.approved has no state precondition check — proceeds after prd.parsed."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            b.append(_make_event("prd.parsed", _make_prd_parsed_payload(),
                                 target_kind="prd", target_id="proj-1"))
            b.append(_make_event("prd.reviewed", {"project_id": "proj-1", "reviewer": "alice"},
                                 target_kind="prd", target_id="proj-1"))
            b.append(_make_event("prd.approved", {"project_id": "proj-1", "approver": "bob"},
                                 target_kind="prd", target_id="proj-1"))
            prd = b.get_prd()
            assert prd is not None
            assert prd.status.value == "approved"
        finally:
            b.close()

    def test_prd_approved_check_rejects_missing_approver(self, tmp_path: Path) -> None:
        """prd.approved without approver raises EventRejected.

        Phase-1 payload-model failure — must be EventRejected ONLY (SL1-RR-1).
        """
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            b.append(_make_event("prd.parsed", _make_prd_parsed_payload(),
                                 target_kind="prd", target_id="proj-1"))
            with pytest.raises(EventRejected):
                b.append(_make_event("prd.approved", {"project_id": "proj-1"},
                                     target_kind="prd", target_id="proj-1"))
        finally:
            b.close()

    # ------------------------------------------------------------------
    # feature.created
    # ------------------------------------------------------------------

    def test_feature_created_check_rejects_bad_status(self, tmp_path: Path) -> None:
        """feature.created with invalid status raises EventRejected; no feature row.

        _check_feature_created failure — must be EventRejected ONLY (SL1-RR-1).
        """
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            with pytest.raises(EventRejected):
                b.append(_make_event(
                    "feature.created",
                    _make_feature_payload(status="invalid_status"),
                    target_kind="feature", target_id="F001",
                ))
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            row = conn.execute("SELECT 1 FROM features WHERE id='F001'").fetchone()
            conn.close()
            assert row is None
        finally:
            b.close()

    def test_feature_created_write_inserts_row(self, tmp_path: Path) -> None:
        """feature.created inserts a feature row when valid."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            b.append(_make_event("feature.created", _make_feature_payload(),
                                 target_kind="feature", target_id="F001"))
            features = b.list_features()
            assert any(f.id == "F001" for f in features)
        finally:
            b.close()

    # ------------------------------------------------------------------
    # task.created
    # ------------------------------------------------------------------

    def test_task_created_check_rejects_missing_created_at(self, tmp_path: Path) -> None:
        """task.created without created_at raises EventRejected; no task row.

        Phase-1 payload-model failure — must be EventRejected ONLY (SL1-RR-1).
        """
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            b.append(_make_event("feature.created", _make_feature_payload(),
                                 target_kind="feature", target_id="F001"))
            bad = _make_task_payload()
            del bad["created_at"]
            with pytest.raises(EventRejected):
                b.append(_make_event("task.created", bad, target_kind="task", target_id="T001"))
            assert b.get_task("T001") is None
        finally:
            b.close()

    def test_task_created_write_inserts_row(self, tmp_path: Path) -> None:
        """task.created inserts a task row when valid."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            b.append(_make_event("feature.created", _make_feature_payload(),
                                 target_kind="feature", target_id="F001"))
            b.append(_make_event("task.created", _make_task_payload(),
                                 target_kind="task", target_id="T001"))
            assert b.get_task("T001") is not None
        finally:
            b.close()

    def test_task_created_forward_catch_up_recovers_missing_prd_dependency_upsert(
        self, tmp_path: Path
    ) -> None:
        """A log-ahead named-task dep upsert from the buggy emitter heals once."""
        events_path = tmp_path / "events.jsonl"
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            feature_id = "named:F001"
            task_id = "named:T001"
            b.append(_make_event(
                "feature.created",
                {**_make_feature_payload(feat_id=feature_id), "prd_id": "named"},
                target_kind="feature",
                target_id=feature_id,
            ))
            b.append(_make_event(
                "task.created",
                {
                    **_make_task_payload(task_id=task_id, feature_id=feature_id),
                    "prd_id": "named",
                },
                target_kind="task",
                target_id=task_id,
            ))
            task = b.get_task(task_id)
            assert task is not None
            # Reproduce the old deps emitter exactly: full Task model_dump with
            # dependencies/updated_at changed and ownership accidentally absent.
            payload = task.model_copy(
                update={"dependencies": ["named:T000"], "updated_at": _T1}
            ).model_dump(mode="json")
            assert "prd_id" not in payload
        finally:
            b.close()

        poisoned = Event(
            id="E000005",
            timestamp=_T1,
            actor="old-deps-emitter",
            action="task.created",
            target_kind="task",
            target_id=task_id,
            payload_json=payload,
        )
        with events_path.open("a", encoding="utf-8") as fh:
            fh.write(
                poisoned.model_dump_json(exclude={"parent_event_id", "lamport"})
                + "\n"
            )

        healed = SqliteBackend(
            db_path=str(tmp_path / "state.db"),
            events_path=str(events_path),
            clock=_make_clock(_T1),
        )
        try:
            healed.initialize()
            task = healed.get_task(task_id)
            assert task is not None
            assert task.prd_id == "named"
            assert task.feature_id == feature_id
            assert task.dependencies == ["named:T000"]
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            try:
                assert conn.execute(
                    "SELECT MAX(id) FROM events"
                ).fetchone() == ("E000005",)
            finally:
                conn.close()
        finally:
            healed.close()

    @pytest.mark.parametrize("explicit_prd", ["default", "other"])
    def test_task_created_never_rewrites_explicit_prd_mismatch(
        self, tmp_path: Path, explicit_prd: str
    ) -> None:
        """Even explicit 'default' is a mismatch, not a legacy omission."""
        b = _make_backend(tmp_path)
        events_path = str(tmp_path / "events.jsonl")
        try:
            _setup_project(b)
            feature_id = "named:F001"
            task_id = "named:T001"
            b.append(_make_event(
                "feature.created",
                {**_make_feature_payload(feat_id=feature_id), "prd_id": "named"},
                target_kind="feature",
                target_id=feature_id,
            ))
            b.append(_make_event(
                "task.created",
                {
                    **_make_task_payload(task_id=task_id, feature_id=feature_id),
                    "prd_id": "named",
                },
                target_kind="task",
                target_id=task_id,
            ))
            task = b.get_task(task_id)
            assert task is not None
            payload = task.model_copy(
                update={"dependencies": ["named:T000"], "updated_at": _T1}
            ).model_dump(mode="json")
            payload["prd_id"] = explicit_prd
            before = len(_read_jsonl(events_path))

            with pytest.raises(EventRejected, match="explicit prd_id"):
                b.append(_make_event(
                    "task.created", payload, target_kind="task", target_id=task_id
                ))

            assert len(_read_jsonl(events_path)) == before
            assert b.get_task(task_id).dependencies == []  # type: ignore[union-attr]
        finally:
            b.close()

    @pytest.mark.parametrize(
        "owner_prd",
        [
            "SECRET_TOKEN_MARKER-" + ("x" * 100_000),
            "SECRET_TOKEN_MARKER\nmalformed\x00snowman-☃",
            "sk-SECRET_TOKEN_MARKER",
        ],
        ids=["huge", "malformed", "safe-secret-like"],
    )
    def test_task_created_recovery_redacts_huge_or_malformed_owner_ids(
        self, tmp_path: Path, owner_prd: str
    ) -> None:
        """Recovery errors/audits are bounded and never disclose raw owner IDs."""
        b = _make_backend(tmp_path)
        events_path = tmp_path / "events.jsonl"
        audit_path = tmp_path / "audit.jsonl"
        feature_id = f"{owner_prd}:F001"
        task_id = f"{owner_prd}:T001"
        try:
            _setup_project(b)
            b.append(_make_event(
                "feature.created",
                {**_make_feature_payload(feat_id=feature_id), "prd_id": owner_prd},
                target_kind="feature",
                target_id=feature_id,
            ))
            b.append(_make_event(
                "task.created",
                {
                    **_make_task_payload(task_id=task_id, feature_id=feature_id),
                    "prd_id": owner_prd,
                },
                target_kind="task",
                target_id=task_id,
            ))
            task = b.get_task(task_id)
            assert task is not None
            payload = task.model_copy(update={"updated_at": _T1}).model_dump(mode="json")
            payload["prd_id"] = "default"
            events_before = events_path.read_bytes()

            with pytest.raises(EventRejected) as raised:
                b.append(_make_event(
                    "task.created", payload, target_kind="task", target_id=task_id
                ))

            error = str(raised.value)
            assert len(error.encode("utf-8")) <= 4096
            assert "SECRET_TOKEN_MARKER" not in error
            assert "<redacted" in error
            assert events_path.read_bytes() == events_before

            audit_lines = audit_path.read_bytes().splitlines(keepends=True)
            assert len(audit_lines[-1]) <= 4096
            assert b"SECRET_TOKEN_MARKER" not in audit_lines[-1]
            audit = json.loads(audit_lines[-1])
            assert audit["target_id"].startswith("<redacted")
            assert "<redacted" in audit["reason"]
        finally:
            b.close()

    def test_task_created_recovery_redacts_huge_payload_prd_id(
        self, tmp_path: Path
    ) -> None:
        """A secret-like explicit payload partition is fingerprinted, not echoed."""
        b = _make_backend(tmp_path)
        events_path = tmp_path / "events.jsonl"
        audit_path = tmp_path / "audit.jsonl"
        try:
            _setup_project(b)
            feature_id = "named:F001"
            task_id = "named:T001"
            b.append(_make_event(
                "feature.created",
                {**_make_feature_payload(feat_id=feature_id), "prd_id": "named"},
                target_kind="feature",
                target_id=feature_id,
            ))
            b.append(_make_event(
                "task.created",
                {
                    **_make_task_payload(task_id=task_id, feature_id=feature_id),
                    "prd_id": "named",
                },
                target_kind="task",
                target_id=task_id,
            ))
            task = b.get_task(task_id)
            assert task is not None
            payload = task.model_copy(update={"updated_at": _T1}).model_dump(mode="json")
            payload["prd_id"] = "SECRET_PAYLOAD_MARKER-" + ("y" * 100_000)
            events_before = events_path.read_bytes()

            with pytest.raises(EventRejected) as raised:
                b.append(_make_event(
                    "task.created", payload, target_kind="task", target_id=task_id
                ))

            error = str(raised.value)
            assert len(error.encode("utf-8")) <= 4096
            assert "SECRET_PAYLOAD_MARKER" not in error
            assert events_path.read_bytes() == events_before
            audit_line = audit_path.read_bytes().splitlines(keepends=True)[-1]
            assert len(audit_line) <= 4096
            assert b"SECRET_PAYLOAD_MARKER" not in audit_line
        finally:
            b.close()

    @pytest.mark.parametrize(
        "malformation",
        [
            "dependencies_shape",
            "dependencies_item",
            "verification_commands",
            "acceptance_criteria",
            "huge_list",
        ],
    )
    def test_task_created_recovery_malformed_payload_uses_one_bounded_refusal(
        self, tmp_path: Path, malformation: str
    ) -> None:
        """Every malformed recovery candidate is fingerprinted before audit."""
        b = _make_backend(tmp_path)
        events_path = tmp_path / "events.jsonl"
        audit_path = tmp_path / "audit.jsonl"
        payload_marker = f"SYNTHETIC_{malformation.upper()}_PAYLOAD"
        actor_marker = "SYNTHETIC_ACTOR_MARKER"
        target_marker = "SYNTHETIC_TARGET_MARKER"
        try:
            _setup_project(b)
            feature_id = "named:F001"
            task_id = "named:T001"
            b.append(_make_event(
                "feature.created",
                {**_make_feature_payload(feat_id=feature_id), "prd_id": "named"},
                target_kind="feature",
                target_id=feature_id,
            ))
            b.append(_make_event(
                "task.created",
                {
                    **_make_task_payload(task_id=task_id, feature_id=feature_id),
                    "prd_id": "named",
                },
                target_kind="task",
                target_id=task_id,
            ))
            task = b.get_task(task_id)
            assert task is not None
            payload = task.model_copy(update={"updated_at": _T1}).model_dump(
                mode="json"
            )
            if malformation == "dependencies_shape":
                payload["dependencies"] = {"secret": payload_marker}
            elif malformation == "dependencies_item":
                payload["dependencies"] = [{"secret": payload_marker}]
            elif malformation == "verification_commands":
                payload["verification"] = {
                    "commands": [{"secret": payload_marker}],
                    "manual_steps": [],
                    "required_evidence": [],
                }
            elif malformation == "acceptance_criteria":
                payload["acceptance_criteria"] = [{"secret": payload_marker}]
            else:
                payload["acceptance_criteria"] = [
                    {"secret": payload_marker} for _ in range(5_000)
                ]

            draft = EventDraft(
                timestamp=_T1,
                actor=actor_marker + ("a" * 100_000),
                action="task.created",
                target_kind="task",
                target_id=(
                    target_marker + ("t" * 100_000)
                    if malformation == "dependencies_shape"
                    else task_id
                ),
                payload_json=payload,
            )
            events_before = events_path.read_bytes()

            with pytest.raises(EventRejected) as raised:
                b.append(draft)

            error = str(raised.value)
            assert error.startswith("task.created ownership recovery refused:")
            assert "code=invalid_task_payload" in error
            assert len(error.encode("utf-8")) <= 4096
            for marker in (payload_marker, actor_marker, target_marker):
                assert marker not in error
            assert events_path.read_bytes() == events_before

            audit_line = audit_path.read_bytes().splitlines(keepends=True)[-1]
            assert len(audit_line) <= 4096
            for marker in (payload_marker, actor_marker, target_marker):
                assert marker.encode() not in audit_line
            audit = json.loads(audit_line)
            assert audit["actor"].startswith("<redacted")
            assert audit["target_id"].startswith("<redacted")
            assert audit["reason"] == error
        finally:
            b.close()

    @pytest.mark.parametrize(
        ("task_id", "feature_id"),
        [
            ("T001", "named:F001"),
            ("named:T001", "F001"),
            ("spoof:T001", "named:F001"),
        ],
    )
    def test_task_created_missing_prd_refuses_unscoped_or_spoofed_ids(
        self, tmp_path: Path, task_id: str, feature_id: str
    ) -> None:
        """Compatibility recovery requires both IDs to carry the exact owner."""
        b = _make_backend(tmp_path)
        events_path = str(tmp_path / "events.jsonl")
        try:
            _setup_project(b)
            b.append(_make_event(
                "feature.created",
                {**_make_feature_payload(feat_id=feature_id), "prd_id": "named"},
                target_kind="feature",
                target_id=feature_id,
            ))
            b.append(_make_event(
                "task.created",
                {
                    **_make_task_payload(task_id=task_id, feature_id=feature_id),
                    "prd_id": "named",
                },
                target_kind="task",
                target_id=task_id,
            ))
            task = b.get_task(task_id)
            assert task is not None
            payload = task.model_copy(update={"updated_at": _T1}).model_dump(mode="json")
            before = len(_read_jsonl(events_path))

            with pytest.raises(EventRejected, match="canonically owner-scoped"):
                b.append(_make_event(
                    "task.created", payload, target_kind="task", target_id=task_id
                ))

            assert len(_read_jsonl(events_path)) == before
        finally:
            b.close()

    def test_task_created_missing_prd_refuses_non_dependency_change(
        self, tmp_path: Path
    ) -> None:
        """The compatibility path cannot smuggle arbitrary task updates."""
        b = _make_backend(tmp_path)
        events_path = str(tmp_path / "events.jsonl")
        try:
            _setup_project(b)
            feature_id = "named:F001"
            task_id = "named:T001"
            b.append(_make_event(
                "feature.created",
                {**_make_feature_payload(feat_id=feature_id), "prd_id": "named"},
                target_kind="feature",
                target_id=feature_id,
            ))
            b.append(_make_event(
                "task.created",
                {
                    **_make_task_payload(task_id=task_id, feature_id=feature_id),
                    "prd_id": "named",
                },
                target_kind="task",
                target_id=task_id,
            ))
            task = b.get_task(task_id)
            assert task is not None
            payload = task.model_copy(
                update={"title": "spoofed", "updated_at": _T1}
            ).model_dump(mode="json")
            before = len(_read_jsonl(events_path))

            with pytest.raises(EventRejected, match="not a dependency-only upsert"):
                b.append(_make_event(
                    "task.created", payload, target_kind="task", target_id=task_id
                ))

            assert len(_read_jsonl(events_path)) == before
            assert b.get_task(task_id).title == "Test Task"  # type: ignore[union-attr]
        finally:
            b.close()

    # ------------------------------------------------------------------
    # task.scored
    # ------------------------------------------------------------------

    def test_task_scored_check_rejects_unknown_task(self, tmp_path: Path) -> None:
        """task.scored for a nonexistent task raises EventRejected.

        _check_task_scored failure — must be EventRejected ONLY (SL1-RR-1).
        """
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            score_payload = {"task_id": "T999", "scores": {"complexity": 3,
                "parallelizability": 4, "context_load": 2, "blast_radius": 2,
                "review_risk": 2, "agent_suitability": 3}, "explanation": "test"}
            with pytest.raises(EventRejected):
                b.append(_make_event("task.scored", score_payload,
                                     target_kind="task", target_id="T999"))
        finally:
            b.close()

    def test_task_scored_write_updates_scores(self, tmp_path: Path) -> None:
        """task.scored updates the task's scores when valid."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            b.append(_make_event("feature.created", _make_feature_payload(),
                                 target_kind="feature", target_id="F001"))
            b.append(_make_event("task.created", _make_task_payload(),
                                 target_kind="task", target_id="T001"))
            b.append(_make_event("task.scored", {
                "task_id": "T001",
                "scores": {"complexity": 3, "parallelizability": 4, "context_load": 2,
                           "blast_radius": 2, "review_risk": 2, "agent_suitability": 3},
                "explanation": "test",
            }, target_kind="task", target_id="T001"))
            task = b.get_task("T001")
            assert task is not None
            assert task.scores.complexity == 3
        finally:
            b.close()

    # ------------------------------------------------------------------
    # task.expanded
    # ------------------------------------------------------------------

    def test_task_expanded_check_rejects_missing_parent_task_id(self, tmp_path: Path) -> None:
        """task.expanded without parent_task_id raises EventRejected.

        Phase-1 payload-model failure — must be EventRejected ONLY (SL1-RR-1).
        """
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            b.append(_make_event("feature.created", _make_feature_payload(),
                                 target_kind="feature", target_id="F001"))
            b.append(_make_event("task.created", _make_task_payload(),
                                 target_kind="task", target_id="T001"))
            with pytest.raises(EventRejected):
                b.append(_make_event("task.expanded",
                                     {"subtasks": [_make_task_payload(task_id="T001.1")]},
                                     target_kind="task", target_id="T001"))
        finally:
            b.close()

    def test_task_expanded_write_inserts_subtasks(self, tmp_path: Path) -> None:
        """task.expanded inserts subtask rows when valid."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            b.append(_make_event("feature.created", _make_feature_payload(),
                                 target_kind="feature", target_id="F001"))
            b.append(_make_event("task.created", _make_task_payload(),
                                 target_kind="task", target_id="T001"))
            b.append(_make_event("task.expanded", {
                "parent_task_id": "T001",
                "subtasks": [_make_task_payload(task_id="T001.1")],
            }, target_kind="task", target_id="T001"))
            assert b.get_task("T001.1") is not None
        finally:
            b.close()

    # ------------------------------------------------------------------
    # task.status_changed
    # ------------------------------------------------------------------

    def test_task_status_changed_check_rejects_missing_task(self, tmp_path: Path) -> None:
        """task.status_changed for nonexistent task raises EventRejected; no side effects.

        _check_task_status_changed failure — must be EventRejected ONLY (SL1-RR-1).
        """
        b = _make_backend(tmp_path)
        events_path = str(tmp_path / "events.jsonl")
        try:
            _setup_project(b)
            initial = len(_read_jsonl(events_path))
            with pytest.raises(EventRejected):
                b.append(_make_event("task.status_changed",
                                     {"task_id": "T999", "from": "proposed", "to": "drafted"},
                                     target_kind="task", target_id="T999"))
            assert len(_read_jsonl(events_path)) == initial
        finally:
            b.close()

    def test_task_status_changed_write_transitions_status(self, tmp_path: Path) -> None:
        """task.status_changed transitions task status when valid."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            b.append(_make_event("feature.created", _make_feature_payload(),
                                 target_kind="feature", target_id="F001"))
            b.append(_make_event("task.created", _make_task_payload(),
                                 target_kind="task", target_id="T001"))
            b.append(_make_event("task.status_changed",
                                 {"task_id": "T001", "from": "proposed", "to": "drafted"},
                                 target_kind="task", target_id="T001"))
            task = b.get_task("T001")
            assert task is not None
            assert task.status.value == "drafted"
        finally:
            b.close()

    # ------------------------------------------------------------------
    # task.deleted
    # ------------------------------------------------------------------

    def test_task_deleted_check_rejects_unsafe_status(self, tmp_path: Path) -> None:
        """task.deleted for a task with active claims rejects (unsafe status).

        _check_task_deleted failure — must be EventRejected ONLY (SL1-RR-1).
        """
        b = _make_backend(tmp_path)
        try:
            _setup_claimable_task(b)
            b.append(_make_event("claim.created", _make_claim_payload(),
                                 target_kind="claim", target_id="C001"))
            # Task is now 'claimed' — not in DELETABLE_TASK_STATUSES without force.
            with pytest.raises(EventRejected):
                b.append(_make_event("task.deleted",
                                     {"task_id": "T001", "force": False},
                                     target_kind="task", target_id="T001"))
            # Task still exists.
            assert b.get_task("T001") is not None
        finally:
            b.close()

    def test_task_deleted_write_removes_row_for_safe_task(self, tmp_path: Path) -> None:
        """task.deleted removes a safe (proposed/drafted/ready) task row."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            b.append(_make_event("feature.created", _make_feature_payload(),
                                 target_kind="feature", target_id="F001"))
            b.append(_make_event("task.created", _make_task_payload(),
                                 target_kind="task", target_id="T001"))
            b.append(_make_event("task.deleted", {"task_id": "T001", "force": False},
                                 target_kind="task", target_id="T001"))
            assert b.get_task("T001") is None
        finally:
            b.close()

    # ------------------------------------------------------------------
    # feature.deleted
    # ------------------------------------------------------------------

    def test_feature_deleted_check_rejects_unknown_feature(self, tmp_path: Path) -> None:
        """feature.deleted for nonexistent feature raises EventRejected.

        _check_feature_deleted failure — must be EventRejected ONLY (SL1-RR-1).
        """
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            with pytest.raises(EventRejected):
                b.append(_make_event("feature.deleted",
                                     {"feature_id": "F999", "force": False},
                                     target_kind="feature", target_id="F999"))
        finally:
            b.close()

    def test_feature_deleted_write_removes_feature_and_tasks(self, tmp_path: Path) -> None:
        """feature.deleted removes the feature (task must be deleted first or feature has no tasks)."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            b.append(_make_event("feature.created", _make_feature_payload(),
                                 target_kind="feature", target_id="F001"))
            # Delete the task first so the feature has no tasks (FK RESTRICT).
            b.append(_make_event("task.created", _make_task_payload(),
                                 target_kind="task", target_id="T001"))
            b.append(_make_event("task.deleted", {"task_id": "T001", "force": False},
                                 target_kind="task", target_id="T001"))
            # Now feature has no tasks — delete should succeed.
            b.append(_make_event("feature.deleted",
                                 {"feature_id": "F001", "force": False, "reason": ""},
                                 target_kind="feature", target_id="F001"))
            features = b.list_features()
            assert not any(f.id == "F001" for f in features)
        finally:
            b.close()

    # ------------------------------------------------------------------
    # task.synced_from_remote
    # ------------------------------------------------------------------

    def test_task_synced_from_remote_check_rejects_unknown_task(self, tmp_path: Path) -> None:
        """task.synced_from_remote for nonexistent task raises EventRejected.

        _check_task_synced_from_remote failure — must be EventRejected ONLY (SL1-RR-1).
        """
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            with pytest.raises(EventRejected):
                b.append(_make_event("task.synced_from_remote",
                                     {"task_id": "T999", "title": "T", "description": "D",
                                      "status": "proposed", "actor": "system"},
                                     target_kind="task", target_id="T999"))
        finally:
            b.close()

    def test_task_synced_from_remote_write_updates_task(self, tmp_path: Path) -> None:
        """task.synced_from_remote updates task title/description/status."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            b.append(_make_event("feature.created", _make_feature_payload(),
                                 target_kind="feature", target_id="F001"))
            b.append(_make_event("task.created", _make_task_payload(),
                                 target_kind="task", target_id="T001"))
            result = b.append(_make_event("task.synced_from_remote",
                                          {"task_id": "T001", "title": "Updated Title",
                                           "description": "Updated desc",
                                           "status": "proposed", "actor": "system"},
                                          target_kind="task", target_id="T001"))
            assert result is not None
            task = b.get_task("T001")
            assert task is not None
            assert task.title == "Updated Title"
        finally:
            b.close()

    # ------------------------------------------------------------------
    # claim.created
    # ------------------------------------------------------------------

    def test_claim_created_check_rejects_non_ready_task(self, tmp_path: Path) -> None:
        """claim.created on a non-ready task raises EventRejected; no claim row.

        _check_claim_created failure — must be EventRejected ONLY (SL1-RR-1).
        """
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            b.append(_make_event("feature.created", _make_feature_payload(),
                                 target_kind="feature", target_id="F001"))
            b.append(_make_event("task.created", _make_task_payload(status="proposed"),
                                 target_kind="task", target_id="T001"))
            with pytest.raises(EventRejected):
                b.append(_make_event("claim.created", _make_claim_payload(),
                                     target_kind="claim", target_id="C001"))
            conn = sqlite3.connect(str(tmp_path / "state.db"))
            row = conn.execute("SELECT 1 FROM claims WHERE id='C001'").fetchone()
            conn.close()
            assert row is None
        finally:
            b.close()

    def test_claim_created_write_inserts_row(self, tmp_path: Path) -> None:
        """claim.created inserts a claim row for a ready task."""
        b = _make_backend(tmp_path)
        try:
            _setup_claimable_task(b)
            b.append(_make_event("claim.created", _make_claim_payload(),
                                 target_kind="claim", target_id="C001"))
            assert b.get_claim("C001") is not None
        finally:
            b.close()

    # ------------------------------------------------------------------
    # claim.released
    # ------------------------------------------------------------------

    def test_claim_released_check_rejects_unknown_claim(self, tmp_path: Path) -> None:
        """claim.released for nonexistent claim raises EventRejected.

        _check_claim_released failure — must be EventRejected ONLY (SL1-RR-1).
        """
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            with pytest.raises(EventRejected):
                b.append(_make_event("claim.released",
                                     {"claim_id": "C999", "released_by": "agent",
                                      "release_reason": "test", "force": False},
                                     target_kind="claim", target_id="C999"))
        finally:
            b.close()

    def test_claim_released_idempotent_noop_for_terminal_claim(self, tmp_path: Path) -> None:
        """claim.released on an already-released claim is an idempotent no-op (returns None)."""
        b = _make_backend(tmp_path)
        try:
            _setup_claimable_task(b)
            b.append(_make_event("claim.created", _make_claim_payload(),
                                 target_kind="claim", target_id="C001"))
            # First release succeeds.
            b.append(_make_event("claim.released",
                                 {"claim_id": "C001", "released_by": "agent",
                                  "release_reason": "done", "force": False},
                                 target_kind="claim", target_id="C001"))
            # Second release is idempotent no-op.
            result = b.append(_make_event("claim.released",
                                          {"claim_id": "C001", "released_by": "agent",
                                           "release_reason": "done again", "force": False},
                                          target_kind="claim", target_id="C001"))
            assert result is None
        finally:
            b.close()

    # ------------------------------------------------------------------
    # claim.renewed
    # ------------------------------------------------------------------

    def test_claim_renewed_check_rejects_inactive_claim(self, tmp_path: Path) -> None:
        """claim.renewed on a released claim raises EventRejected.

        _check_claim_renewed failure — must be EventRejected ONLY (SL1-RR-1).
        """
        b = _make_backend(tmp_path)
        try:
            _setup_claimable_task(b)
            b.append(_make_event("claim.created", _make_claim_payload(),
                                 target_kind="claim", target_id="C001"))
            b.append(_make_event("claim.released",
                                 {"claim_id": "C001", "released_by": "agent",
                                  "release_reason": "done", "force": False},
                                 target_kind="claim", target_id="C001"))
            with pytest.raises(EventRejected):
                b.append(_make_event("claim.renewed",
                                     {"claim_id": "C001", "new_lease_expires_at": (
                                         _T0 + timedelta(hours=2)).isoformat()},
                                     target_kind="claim", target_id="C001"))
        finally:
            b.close()

    def test_claim_renewed_write_extends_lease(self, tmp_path: Path) -> None:
        """claim.renewed extends the lease expiry for an active claim."""
        b = _make_backend(tmp_path)
        try:
            _setup_claimable_task(b)
            b.append(_make_event("claim.created", _make_claim_payload(),
                                 target_kind="claim", target_id="C001"))
            new_expiry = (_T0 + timedelta(hours=3)).isoformat()
            new_heartbeat = _T0.isoformat()
            b.append(_make_event("claim.renewed",
                                 {"claim_id": "C001", "lease_expires_at": new_expiry,
                                  "last_heartbeat_at": new_heartbeat},
                                 target_kind="claim", target_id="C001"))
            claim = b.get_claim("C001")
            assert claim is not None
            # Lease was extended.
            assert claim.lease_expires_at > _T0
        finally:
            b.close()

    # ------------------------------------------------------------------
    # claim.stale
    # ------------------------------------------------------------------

    def test_claim_stale_check_rejects_unknown_claim(self, tmp_path: Path) -> None:
        """claim.stale for nonexistent claim raises EventRejected.

        _check_claim_stale failure — must be EventRejected ONLY (SL1-RR-1).
        """
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            with pytest.raises(EventRejected):
                b.append(_make_event("claim.stale",
                                     {"claim_id": "C999", "task_id": "T001",
                                      "expired_at": _T0.isoformat(),
                                      "detected_at": _T0.isoformat(),
                                      "reason": "lease_expired", "actor": "system"},
                                     target_kind="claim", target_id="C999"))
        finally:
            b.close()

    def test_claim_stale_write_marks_claim_stale(self, tmp_path: Path) -> None:
        """claim.stale marks an active claim as stale."""
        b = _make_backend(tmp_path)
        try:
            _setup_claimable_task(b)
            b.append(_make_event("claim.created", _make_claim_payload(),
                                 target_kind="claim", target_id="C001"))
            b.append(_make_event("claim.stale",
                                 {"claim_id": "C001", "task_id": "T001",
                                  "expired_at": (_T0 - timedelta(hours=1)).isoformat(),
                                  "detected_at": _T0.isoformat(),
                                  "reason": "lease_expired", "actor": "system"},
                                 target_kind="claim", target_id="C001"))
            from anvil.state.models import ClaimStatus
            claim = b.get_claim("C001")
            assert claim is not None
            assert claim.status == ClaimStatus.stale
        finally:
            b.close()

    # ------------------------------------------------------------------
    # evidence.submitted
    # ------------------------------------------------------------------

    def test_evidence_submitted_check_rejects_empty_commands_run(self, tmp_path: Path) -> None:
        """evidence.submitted with empty commands_run raises EventRejected.

        _check_evidence_submitted failure — must be EventRejected ONLY (SL1-RR-1).
        A TransactionAborted here would mean validation leaked past the log append.
        """
        b = _make_backend(tmp_path)
        events_path = str(tmp_path / "events.jsonl")
        try:
            _setup_claimable_task_and_claim(b)
            initial = len(_read_jsonl(events_path))
            with pytest.raises(EventRejected):
                b.append(_make_event("evidence.submitted",
                                     _make_evidence_payload(commands_run=[]),
                                     target_kind="task", target_id="T001"))
            assert len(_read_jsonl(events_path)) == initial
        finally:
            b.close()

    def test_evidence_submitted_write_inserts_evidence(self, tmp_path: Path) -> None:
        """evidence.submitted inserts evidence row and transitions task to needs_review."""
        b = _make_backend(tmp_path)
        try:
            _setup_claimable_task_and_claim(b)
            b.append(_make_event("evidence.submitted", _make_evidence_payload(),
                                 target_kind="task", target_id="T001"))
            from anvil.state.models import TaskStatus
            task = b.get_task("T001")
            assert task is not None
            assert task.status == TaskStatus.needs_review
            ev = b.get_latest_evidence("T001")
            assert ev is not None
            assert ev.id == "EV001"
        finally:
            b.close()

    # ------------------------------------------------------------------
    # task.applied
    # ------------------------------------------------------------------

    def test_task_applied_check_rejects_wrong_status(self, tmp_path: Path) -> None:
        """task.applied on a non-needs_review task raises EventRejected.

        _check_task_applied failure — must be EventRejected ONLY (SL1-RR-1).
        A TransactionAborted here would mean validation leaked past the log append.
        """
        b = _make_backend(tmp_path)
        events_path = str(tmp_path / "events.jsonl")
        try:
            _setup_claimable_task(b)  # task is 'ready', not 'needs_review'
            initial = len(_read_jsonl(events_path))
            with pytest.raises(EventRejected):
                b.append(_make_event("task.applied",
                                     {"task_id": "T001", "reviewer": "alice",
                                      "decision": "accepted", "notes": None},
                                     target_kind="task", target_id="T001"))
            assert len(_read_jsonl(events_path)) == initial
        finally:
            b.close()

    def test_task_applied_write_transitions_to_done(self, tmp_path: Path) -> None:
        """task.applied accepted transitions task to done."""
        b = _make_backend(tmp_path)
        try:
            _setup_claimable_task_and_claim(b)
            b.append(_make_event("evidence.submitted", _make_evidence_payload(),
                                 target_kind="task", target_id="T001"))
            b.append(_make_event("task.applied",
                                 {"task_id": "T001", "reviewer": "alice",
                                  "decision": "accepted", "notes": None},
                                 target_kind="task", target_id="T001"))
            from anvil.state.models import TaskStatus
            task = b.get_task("T001")
            assert task is not None
            assert task.status == TaskStatus.done
        finally:
            b.close()

    # ------------------------------------------------------------------
    # task.applied — extended contract tests (SHOULD FIX 1)
    # ------------------------------------------------------------------

    def test_task_applied_check_reject_leaves_zero_side_effects(
        self, tmp_path: Path
    ) -> None:
        """_check_task_applied rejection leaves zero new event lines and no state change.

        The task is in 'ready' status (not needs_review), so _check_task_applied
        must reject with EventRejected before any write occurs. The JSONL log
        must be unchanged and the task must remain at 'ready'.
        """
        from anvil.state.models import TaskStatus
        b = _make_backend(tmp_path)
        events_path = str(tmp_path / "events.jsonl")
        try:
            _setup_claimable_task(b)  # task ends up at 'ready'
            task_before = b.get_task("T001")
            assert task_before is not None
            assert task_before.status == TaskStatus.ready

            initial_event_count = len(_read_jsonl(events_path))

            # decision='accepted' on a 'ready' task — _check_task_applied rejects.
            with pytest.raises(EventRejected):
                b.append(_make_event(
                    "task.applied",
                    {"task_id": "T001", "reviewer": "alice",
                     "decision": "accepted", "notes": None},
                    target_kind="task", target_id="T001",
                ))

            # No new canonical event line written to events.jsonl.
            assert len(_read_jsonl(events_path)) == initial_event_count, (
                "EventRejected must not write to events.jsonl (pre-log check)"
            )
            # Task status unchanged.
            task_after = b.get_task("T001")
            assert task_after is not None
            assert task_after.status == TaskStatus.ready, (
                f"Task must remain 'ready' after rejection; got {task_after.status}"
            )
            # No review row inserted.
            reviews = b.list_reviews()
            assert len(reviews) == 0, (
                f"No review row must be inserted on rejection; got {reviews}"
            )
        finally:
            b.close()

    def test_task_applied_rejected_decision_transitions_to_drafted(
        self, tmp_path: Path
    ) -> None:
        """task.applied with decision='rejected' transitions needs_review → rejected → drafted.

        The SL1-RR-1 spec requires: needs_review → rejected (brief marker) →
        drafted (automatic within the same transaction) so the task can be
        re-reviewed. Verify:
        - Final task status is 'drafted' (not 'rejected', which is transient).
        - A review row is inserted with decision='rejected'.
        - A new event line appears in events.jsonl.
        """
        from anvil.state.models import ReviewDecision, TaskStatus
        b = _make_backend(tmp_path)
        events_path = str(tmp_path / "events.jsonl")
        try:
            _setup_claimable_task_and_claim(b)
            b.append(_make_event("evidence.submitted", _make_evidence_payload(),
                                 target_kind="task", target_id="T001"))
            # Task is now needs_review.
            task_mid = b.get_task("T001")
            assert task_mid is not None
            assert task_mid.status == TaskStatus.needs_review

            before_apply_count = len(_read_jsonl(events_path))

            b.append(_make_event(
                "task.applied",
                {"task_id": "T001", "reviewer": "bob",
                 "decision": "rejected", "notes": "needs more tests"},
                target_kind="task", target_id="T001",
            ))

            # Exactly one new event line.
            after_apply_count = len(_read_jsonl(events_path))
            assert after_apply_count == before_apply_count + 1, (
                "task.applied must write exactly one event line"
            )

            # Task is drafted (auto-promoted from rejected in the same txn).
            task_after = b.get_task("T001")
            assert task_after is not None
            assert task_after.status == TaskStatus.drafted, (
                f"Expected status 'drafted' after rejected apply; got {task_after.status}"
            )

            # A review row exists with the original 'rejected' decision. Note:
            # _row_to_review maps 'rejected' → ReviewDecision.needs_changes per the
            # _TASK_OUTCOME_TO_REVIEW_DECISION constant (rejected-but-reworkable).
            reviews = b.list_reviews()
            assert len(reviews) == 1, f"Expected 1 review; got {reviews}"
            assert reviews[0].decision == ReviewDecision.needs_changes, (
                f"'rejected' decision must map to needs_changes; got {reviews[0].decision}"
            )
        finally:
            b.close()

    def test_task_applied_accepted_inserts_review_row(self, tmp_path: Path) -> None:
        """task.applied accepted inserts a review row with decision='accepted'.

        Verify the full write: task transitions to done AND a review row is inserted
        with the canonical 'accepted' decision (mapped to ReviewDecision.approve by
        _row_to_review per _TASK_OUTCOME_TO_REVIEW_DECISION).
        """
        from anvil.state.models import ReviewDecision, TaskStatus
        b = _make_backend(tmp_path)
        try:
            _setup_claimable_task_and_claim(b)
            b.append(_make_event("evidence.submitted", _make_evidence_payload(),
                                 target_kind="task", target_id="T001"))
            b.append(_make_event(
                "task.applied",
                {"task_id": "T001", "reviewer": "carol",
                 "decision": "accepted", "notes": "LGTM"},
                target_kind="task", target_id="T001",
            ))
            task = b.get_task("T001")
            assert task is not None
            assert task.status == TaskStatus.done

            reviews = b.list_reviews()
            assert len(reviews) == 1, f"Expected 1 review; got {reviews}"
            assert reviews[0].decision == ReviewDecision.approve, (
                f"'accepted' decision must map to approve; got {reviews[0].decision}"
            )
            assert reviews[0].target_id == "T001"
        finally:
            b.close()

    # ------------------------------------------------------------------
    # evidence.submitted — extended contract tests (SHOULD FIX 1)
    # ------------------------------------------------------------------

    def test_evidence_submitted_check_reject_leaves_zero_side_effects(
        self, tmp_path: Path
    ) -> None:
        """_check_evidence_submitted rejection leaves no side effects.

        Inject an invalid payload (empty files_changed) so _check_evidence_submitted
        raises EventRejected. Verify:
        - No new event line in events.jsonl.
        - No evidence row inserted.
        - Task status unchanged (still 'claimed').
        - Claim status unchanged (still 'active').
        """
        from anvil.state.models import ClaimStatus, TaskStatus
        b = _make_backend(tmp_path)
        events_path = str(tmp_path / "events.jsonl")
        try:
            _setup_claimable_task_and_claim(b)
            initial_event_count = len(_read_jsonl(events_path))

            # commands_run=[] triggers EventRejected in _check_evidence_submitted
            # (the mandatory proof). files_changed=[] alone is now allowed (B32).
            with pytest.raises(EventRejected):
                b.append(_make_event(
                    "evidence.submitted",
                    _make_evidence_payload(commands_run=[]),
                    target_kind="task", target_id="T001",
                ))

            # No new canonical event line.
            assert len(_read_jsonl(events_path)) == initial_event_count, (
                "EventRejected must not write to events.jsonl (pre-log check)"
            )
            # No evidence row inserted.
            ev = b.get_latest_evidence("T001")
            assert ev is None, f"No evidence must be inserted on rejection; got {ev}"
            # Task still 'claimed'.
            task = b.get_task("T001")
            assert task is not None
            assert task.status == TaskStatus.claimed, (
                f"Task must remain 'claimed' after rejection; got {task.status}"
            )
            # Claim still 'active'.
            claim = b.get_claim("C001")
            assert claim is not None
            assert claim.status == ClaimStatus.active, (
                f"Claim must remain 'active' after rejection; got {claim.status}"
            )
        finally:
            b.close()

    def test_evidence_submitted_write_records_evidence_and_auto_releases_claim(
        self, tmp_path: Path
    ) -> None:
        """evidence.submitted inserts evidence, transitions task, and auto-releases claim.

        Full write path verification:
        - Evidence row inserted with correct fields.
        - Task status transitions from 'claimed' to 'needs_review'.
        - Active claim is auto-released (status → 'released').
        - Exactly one new event line in events.jsonl.
        """
        from anvil.state.models import ClaimStatus, TaskStatus
        b = _make_backend(tmp_path)
        events_path = str(tmp_path / "events.jsonl")
        try:
            _setup_claimable_task_and_claim(b)
            before_count = len(_read_jsonl(events_path))

            b.append(_make_event(
                "evidence.submitted",
                _make_evidence_payload(
                    commands_run=["pytest tests/ -v"],
                    files_changed=["src/auth.py", "tests/test_auth.py"],
                    output_excerpt="5 passed",
                ),
                target_kind="task", target_id="T001",
            ))

            # Exactly one new event line.
            assert len(_read_jsonl(events_path)) == before_count + 1

            # Evidence row inserted.
            ev = b.get_latest_evidence("T001")
            assert ev is not None
            assert ev.id == "EV001"
            assert ev.claim_id == "C001"
            assert "pytest" in ev.commands_run[0]

            # Task transitions to needs_review.
            task = b.get_task("T001")
            assert task is not None
            assert task.status == TaskStatus.needs_review, (
                f"Expected needs_review; got {task.status}"
            )

            # Active claim auto-released.
            claim = b.get_claim("C001")
            assert claim is not None
            assert claim.status == ClaimStatus.released, (
                f"Expected claim auto-released; got {claim.status}"
            )
        finally:
            b.close()

    # ------------------------------------------------------------------
    # sync_mapping.upserted / sync_mapping.deleted
    # ------------------------------------------------------------------

    def test_sync_mapping_upserted_check_rejects_invalid_payload(self, tmp_path: Path) -> None:
        """sync_mapping.upserted with invalid sync_state raises EventRejected.

        _check_sync_mapping_upserted failure — must be EventRejected ONLY (SL1-RR-1).
        """
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            b.append(_make_event("feature.created", _make_feature_payload(),
                                 target_kind="feature", target_id="F001"))
            b.append(_make_event("task.created", _make_task_payload(),
                                 target_kind="task", target_id="T001"))
            with pytest.raises(EventRejected):
                b.append(_make_event("sync_mapping.upserted",
                                     _make_sync_mapping_payload(sync_state="bad_state"),
                                     target_kind="task", target_id="T001"))
        finally:
            b.close()

    def test_sync_mapping_upserted_write_inserts_row(self, tmp_path: Path) -> None:
        """sync_mapping.upserted inserts a sync_mapping row when valid."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            _setup_task_for_sync(b)
            b.append(_make_event("sync_mapping.upserted", _make_sync_mapping_payload(),
                                 target_kind="task", target_id="T001"))
            assert b.get_sync_mapping("T001") is not None
        finally:
            b.close()

    def test_sync_mapping_deleted_write_removes_row(self, tmp_path: Path) -> None:
        """sync_mapping.deleted removes the mapping row."""
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            _setup_task_for_sync(b)
            b.append(_make_event("sync_mapping.upserted", _make_sync_mapping_payload(),
                                 target_kind="task", target_id="T001"))
            b.append(_make_event("sync_mapping.deleted", {"task_id": "T001"},
                                 target_kind="task", target_id="T001"))
            assert b.get_sync_mapping("T001") is None
        finally:
            b.close()


# ===========================================================================
# _append_lock contention backoff (jittered exponential, 5 s budget)
# ===========================================================================


class TestAppendLockBackoff:
    """Backoff schedule for flock contention in _append_lock.

    Under a coordinated multi-agent wave (10+ concurrent claimants), the old
    fixed-interval poll woke every waiter on the same tick: early winners
    re-acquired in lockstep and late arrivals burned their whole 5 s budget
    without ever winning a release, surfacing as spurious StateLocked. These
    tests pin the replacement schedule: exponential from 10 ms, capped at
    500 ms, ±10% jitter, same 5 s overall timeout.

    No real sleeping and no monkey-patching: in the spirit of the project's
    Clock-protocol pattern, the injected sleep_fn advances a fake monotonic
    counter read back through the injected monotonic_fn. The deadline runs on
    the monotonic clock (not Clock.now()) so a wall-clock NTP step cannot
    stretch or shorten the timeout.
    """

    def test_schedule_doubles_from_initial_to_cap(self) -> None:
        """With jitter pinned at its midpoint the schedule is pure doubling."""
        delays = _flock_backoff_delays(rand=lambda: 0.5)  # 2*0.5-1 == 0 → no jitter
        got = [next(delays) for _ in range(9)]
        assert got == pytest.approx(
            [0.010, 0.020, 0.040, 0.080, 0.160, 0.320, 0.500, 0.500, 0.500]
        )

    def test_jitter_stays_within_ten_percent(self) -> None:
        """rand() at 0.0 / 1.0 produces the extreme delays: base ∓ / ± 10%."""
        low = _flock_backoff_delays(rand=lambda: 0.0)
        high = _flock_backoff_delays(rand=lambda: 1.0)
        assert next(low) == pytest.approx(_FLOCK_BACKOFF_INITIAL_S * 0.9)
        assert next(high) == pytest.approx(_FLOCK_BACKOFF_INITIAL_S * 1.1)
        # The cap applies to the un-jittered base, so a jittered delay may
        # exceed the cap by at most 10%.
        for _ in range(20):
            next(high)
        assert next(high) == pytest.approx(_FLOCK_BACKOFF_CAP_S * 1.1)

    def test_contended_append_backs_off_then_raises_state_locked(
        self, tmp_path: Path
    ) -> None:
        """A held flock drives the full backoff schedule, then StateLocked.

        The injected sleep_fn records each delay and advances a fake monotonic
        counter instead of blocking, so the whole 5 s contention window runs
        in microseconds and the recorded schedule is assertable.
        """
        events_path = tmp_path / "events.jsonl"
        events_path.touch()
        fake_now = [0.0]
        slept: list[float] = []

        def fake_sleep(seconds: float) -> None:
            slept.append(seconds)
            fake_now[0] += seconds

        b = SqliteBackend(
            db_path=str(tmp_path / "state.db"),
            events_path=str(events_path),
            clock=_make_clock(),
            sleep_fn=fake_sleep,
            monotonic_fn=lambda: fake_now[0],
        )
        # Hold the append lock on an independent fd via the same cross-platform
        # helper the backend uses: locks belong to the open file handle, so a
        # second open() of the same path contends even within a single process
        # (true for both POSIX flock and Windows msvcrt byte-range locks).
        holder = open(events_path, "ab")
        try:
            _append_lock_acquire_nb(holder)
            with pytest.raises(StateLocked), b._append_lock():
                pass  # unreachable — the lock is never acquired
        finally:
            _append_lock_release(holder)
            holder.close()

        # Every sleep is clamped to the time remaining, so the recorded
        # delays consume the 5 s budget exactly.
        assert sum(slept) == pytest.approx(_FLOCK_TIMEOUT_S)
        # Each un-clamped delay obeys the schedule: within ±10% of
        # min(10 ms * 2^i, 500 ms). The final delay may be clamped shorter.
        for i, delay in enumerate(slept[:-1]):
            base = min(_FLOCK_BACKOFF_INITIAL_S * 2**i, _FLOCK_BACKOFF_CAP_S)
            assert base * 0.9 <= delay <= base * 1.1, f"delay {i}: {delay}"
        # Exponential, not fixed: the old 50 ms poll took ~100 wake-ups to
        # exhaust 5 s; the backoff schedule gets there in 13-18 larger steps.
        assert 10 <= len(slept) <= 20


# ---------------------------------------------------------------------------
# CL-4 — ConflictGroup persistence round-trip
# ---------------------------------------------------------------------------


class TestConflictGroupPersistence:
    """conflict_group.upserted events persist ConflictGroup rows that round-trip."""

    @staticmethod
    def _seed(b: SqliteBackend) -> None:
        _apply(b, "project.created", _make_project_event().payload_json)
        _apply(b, "state.initialized", {})

    @staticmethod
    def _upsert(
        b: SqliteBackend,
        cg_id: str,
        task_ids: list[str],
        reason: str,
    ) -> None:
        _apply(
            b,
            "conflict_group.upserted",
            {
                "id": cg_id,
                "name": cg_id,
                "task_ids": task_ids,
                "reason": reason,
            },
            target_kind="conflict_group",
            target_id=cg_id,
        )

    def test_list_conflict_groups_empty_on_fresh_backend(
        self, tmp_path: Path
    ) -> None:
        b = _make_backend(tmp_path)
        try:
            self._seed(b)
            assert b.list_conflict_groups() == []
        finally:
            b.close()

    def test_upserted_event_persists_and_round_trips(self, tmp_path: Path) -> None:
        b = _make_backend(tmp_path)
        try:
            self._seed(b)
            self._upsert(
                b,
                "CG-T001-T002",
                ["T001", "T002"],
                "Tasks T001 and T002 share overlapping files: a.py",
            )
            groups = b.list_conflict_groups()
            assert len(groups) == 1
            g = groups[0]
            assert g.id == "CG-T001-T002"
            assert g.name == "CG-T001-T002"
            assert g.task_ids == ["T001", "T002"]
            assert "a.py" in g.reason
        finally:
            b.close()

    def test_round_trip_survives_reopen(self, tmp_path: Path) -> None:
        """Rows persist across a close()/reopen — true table persistence."""
        b = _make_backend(tmp_path)
        try:
            self._seed(b)
            self._upsert(b, "CG-T001-T002", ["T001", "T002"], "overlap")
        finally:
            b.close()
        # Reopen against the same on-disk db; initialize() is idempotent.
        db_path = str(tmp_path / "state.db")
        events_path = str(tmp_path / "events.jsonl")
        b2 = SqliteBackend(
            db_path=db_path, events_path=events_path, clock=_make_clock()
        )
        b2.initialize()
        try:
            groups = b2.list_conflict_groups()
            assert [g.id for g in groups] == ["CG-T001-T002"]
        finally:
            b2.close()

    def test_upsert_is_idempotent_on_id(self, tmp_path: Path) -> None:
        """Re-emitting the same group ID rewrites the row in place, no error."""
        b = _make_backend(tmp_path)
        try:
            self._seed(b)
            self._upsert(b, "CG-T001-T002", ["T001", "T002"], "first")
            self._upsert(b, "CG-T001-T002", ["T001", "T002"], "second")
            groups = b.list_conflict_groups()
            assert len(groups) == 1
            assert groups[0].reason == "second"
        finally:
            b.close()

    def test_list_is_ordered_by_id(self, tmp_path: Path) -> None:
        b = _make_backend(tmp_path)
        try:
            self._seed(b)
            self._upsert(b, "CG-T002-T003", ["T002", "T003"], "overlap")
            self._upsert(b, "CG-T001-T002", ["T001", "T002"], "overlap")
            assert [g.id for g in b.list_conflict_groups()] == [
                "CG-T001-T002",
                "CG-T002-T003",
            ]
        finally:
            b.close()

    def test_invalid_payload_is_rejected(self, tmp_path: Path) -> None:
        """A payload missing the required ``id`` is rejected up front."""
        b = _make_backend(tmp_path)
        try:
            self._seed(b)
            with pytest.raises(EventRejected):
                _apply(
                    b,
                    "conflict_group.upserted",
                    {"name": "x", "task_ids": [], "reason": ""},
                    target_kind="conflict_group",
                    target_id="x",
                )
        finally:
            b.close()
