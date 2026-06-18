"""B03 — concurrency regression suite: ZERO double-claims under contention.

This is the canonical concurrency test module for the claim/lock primitive.
It consolidates coverage from the earlier ``test_claim_concurrency.py`` (which
proved winner-count == 1) and adds loser-side hardening:

  - EXACTLY ONE winner exists per contended task,
  - every loser returns a CLEAN failed/blocked outcome (a caught ``ClaimError``,
    never a leaked/unexpected exception),
  - no loser ever produces a second successful claim,
  - the persisted DB agrees: at most one ACTIVE claim per task, and no two
    active claims share a file, and
  - ``force=True`` still overrides the in-transaction overlap guard, and
  - the CLI honours ``default_lease_minutes`` from config (B02).

The atomicity fix (B01) moved the ``expected_files`` / ``conflict_group``
pre-check INSIDE the ``BEGIN IMMEDIATE`` transaction that writes the claim row,
so exactly one of N contending claims can ever win. If any assertion here fails
it means B01 is not actually atomic — the failure output names the contention
shape and the offending winner/claim count so a human can re-open B01.

NO mocking: every test drives a real ``SqliteBackend``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from anvil.claims.manager import ClaimError, ClaimManager
from anvil.clock import SystemClock
from anvil.state.models import EventDraft
from anvil.state.sqlite import SqliteBackend

_UTC = UTC
_T0 = datetime(2026, 5, 24, 18, 0, 0, tzinfo=_UTC)

# >=8 threads, per the B03 spec. More threads + more iterations widen the race
# window so a non-atomic claim path is very likely to be caught.
_N_THREADS = 12
_ITERATIONS = 200

# MySQL backend parameter (spec §4.1). Skipped cleanly when MYSQL_TEST_URL is
# unset (developer laptop with no MySQL) or PyMySQL is not installed; the SQLite
# params always run. The single-winner assertions are byte-for-byte identical
# across both backends — only the DB-poking setup helpers branch on dialect.
_MYSQL_URL = os.environ.get("MYSQL_TEST_URL")
_mysql_param = pytest.param(
    "mysql",
    marks=pytest.mark.skipif(
        not _MYSQL_URL,
        reason="set MYSQL_TEST_URL to run the MySQL backend concurrency tests",
    ),
)


# ---------------------------------------------------------------------------
# Backend abstraction — the test bodies are backend-agnostic; only these two
# helpers (backend construction + raw DB poking) branch on dialect. Everything
# downstream (the race harness, the winner-count assertions) stays identical.
# ---------------------------------------------------------------------------


class _DBAccess:
    """Dialect-thin shim for the raw DB setup/poke the tests do directly.

    The SQLite tests open ``sqlite3.connect(db_path)`` to insert features/tasks
    and to count active claims. The MySQL path runs the SAME logical operations
    against the configured server. Only this shim differs per dialect; the
    assertions that consume its results do not.
    """

    def insert_feature(self, feat_id: str = "F001") -> None:
        raise NotImplementedError

    def insert_task(
        self,
        *,
        task_id: str,
        status: str = "ready",
        conflict_groups: list[str] | None = None,
        likely_files: list[str] | None = None,
    ) -> None:
        raise NotImplementedError

    def reset_tasks_to_ready(self, task_ids: list[str]) -> None:
        raise NotImplementedError

    def active_claim_count_per_task(self) -> dict[str, int]:
        raise NotImplementedError

    def active_claims_with_files(self) -> list[tuple[str, str, list[str]]]:
        raise NotImplementedError


class _SqliteDBAccess(_DBAccess):
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    def insert_feature(self, feat_id: str = "F001") -> None:
        conn = sqlite3.connect(self._db_path)
        try:
            _insert_feature(conn, feat_id)
        finally:
            conn.close()

    def insert_task(
        self,
        *,
        task_id: str,
        status: str = "ready",
        conflict_groups: list[str] | None = None,
        likely_files: list[str] | None = None,
    ) -> None:
        conn = sqlite3.connect(self._db_path)
        try:
            _insert_task(
                conn,
                task_id=task_id,
                status=status,
                conflict_groups=conflict_groups,
                likely_files=likely_files,
            )
        finally:
            conn.close()

    def reset_tasks_to_ready(self, task_ids: list[str]) -> None:
        conn = sqlite3.connect(self._db_path)
        try:
            _reset_tasks_to_ready(conn, task_ids)
        finally:
            conn.close()

    def active_claim_count_per_task(self) -> dict[str, int]:
        return _active_claim_count_per_task(self._db_path)

    def active_claims_with_files(self) -> list[tuple[str, str, list[str]]]:
        return _active_claims_with_files(self._db_path)


class _MySQLDBAccess(_DBAccess):
    """MySQL raw-DB shim. Opens its own pymysql connection for setup/poke."""

    def __init__(self, dsn: str) -> None:
        import pymysql

        from anvil.state.mysql import parse_mysql_dsn

        self._connect = lambda: pymysql.connect(**parse_mysql_dsn(dsn))

    def insert_feature(self, feat_id: str = "F001") -> None:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT IGNORE INTO features "
                "(id, title, description, status, requirements, tasks) "
                "VALUES (%s, %s, 'desc', 'proposed', '[]', '[]')",
                (feat_id, f"Feature {feat_id}"),
            )
            conn.commit()
        finally:
            conn.close()

    def insert_task(
        self,
        *,
        task_id: str,
        status: str = "ready",
        conflict_groups: list[str] | None = None,
        likely_files: list[str] | None = None,
    ) -> None:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO tasks
                (id, feature_id, title, description, status, priority,
                 dependencies, conflict_groups, scores, acceptance_criteria,
                 implementation_notes, verification, likely_files,
                 created_at, updated_at)
                VALUES (%s, 'F001', %s, 'desc', %s, 'medium', '[]', %s, '{}',
                        '[]', '[]', '{}', %s, %s, %s)""",
                (
                    task_id,
                    f"Task {task_id}",
                    status,
                    json.dumps(conflict_groups or []),
                    json.dumps(likely_files or []),
                    _T0.isoformat(),
                    _T0.isoformat(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def reset_tasks_to_ready(self, task_ids: list[str]) -> None:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM claims")
            for tid in task_ids:
                cur.execute(
                    "UPDATE tasks SET status = 'ready' WHERE id = %s", (tid,)
                )
            conn.commit()
        finally:
            conn.close()

    def active_claim_count_per_task(self) -> dict[str, int]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT task_id, COUNT(*) FROM claims "
                "WHERE status = 'active' GROUP BY task_id"
            )
            rows = cur.fetchall()
        finally:
            conn.close()
        return {task_id: count for task_id, count in rows}

    def active_claims_with_files(self) -> list[tuple[str, str, list[str]]]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, task_id, expected_files FROM claims "
                "WHERE status = 'active'"
            )
            rows = cur.fetchall()
        finally:
            conn.close()
        out: list[tuple[str, str, list[str]]] = []
        for claim_id, task_id, files_json in rows:
            files = json.loads(files_json) if files_json else []
            out.append((claim_id, task_id, files))
        return out


def _drop_all_mysql_tables(dsn: str) -> None:
    """Drop every table in the target MySQL database (per-test clean slate)."""
    import pymysql

    from anvil.state.mysql import parse_mysql_dsn

    conn = pymysql.connect(**parse_mysql_dsn(dsn))
    try:
        cur = conn.cursor()
        cur.execute("SET FOREIGN_KEY_CHECKS = 0")
        cur.execute("SHOW TABLES")
        for (table,) in cur.fetchall():
            cur.execute(f"DROP TABLE IF EXISTS `{table}`")
        cur.execute("SET FOREIGN_KEY_CHECKS = 1")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(params=["sqlite", _mysql_param])
def backend_and_db(request: pytest.FixtureRequest, tmp_path: Path):  # type: ignore[no-untyped-def]
    """Yield ``(backend, db_access)`` for the parameterized backend.

    sqlite runs always; mysql runs only when MYSQL_TEST_URL is set (and PyMySQL
    is importable — otherwise skipped, never failed). Each MySQL run gets a clean
    slate by dropping every table first; ``tmp_path`` still supplies the local
    events.jsonl shadow.
    """
    if request.param == "mysql":
        pytest.importorskip("pymysql")
        from anvil.state.mysql import MySQLBackend

        assert _MYSQL_URL is not None
        _drop_all_mysql_tables(_MYSQL_URL)
        events_path = str(tmp_path / "events.jsonl")
        Path(events_path).touch()
        b: Any = MySQLBackend(
            dsn=_MYSQL_URL, events_path=events_path, clock=SystemClock()
        )
        b.initialize()
        db: _DBAccess = _MySQLDBAccess(_MYSQL_URL)
        try:
            yield b, db
        finally:
            b.close()
    else:
        db_path = str(tmp_path / "state.db")
        events_path = str(tmp_path / "events.jsonl")
        Path(events_path).touch()
        b = SqliteBackend(
            db_path=db_path, events_path=events_path, clock=SystemClock()
        )
        b.initialize()
        try:
            yield b, _SqliteDBAccess(db_path)
        finally:
            b.close()


def _make_backend(state_dir: Path) -> SqliteBackend:
    db_path = str(state_dir / "state.db")
    events_path = str(state_dir / "events.jsonl")
    Path(events_path).touch()
    # SystemClock so each claim's timestamps are distinct; the single-winner
    # property is enforced by the in-transaction guard, not by the clock.
    b = SqliteBackend(db_path=db_path, events_path=events_path, clock=SystemClock())
    b.initialize()
    return b


def _setup_project_and_prd(b: SqliteBackend) -> None:
    """Stand up a reviewed PRD so the claim PRD-gate (gate 3) passes."""
    b.append(
        EventDraft(
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
        )
    )
    b.append(
        EventDraft(
            timestamp=_T0,
            actor="test",
            action="state.initialized",
            target_kind="project",
            target_id="proj-1",
            payload_json={},
        )
    )
    prd_payload: dict[str, Any] = {
        "project_id": "proj-1",
        "status": "draft",
        "summary": "Test PRD.",
        "goals": ["Goal one."],
        "non_goals": [],
        "requirements": [
            {
                "id": "R001",
                "prd_section": "requirements",
                "text": "Req 1.",
                "source_paragraph": None,
                "derived": False,
            }
        ],
        "acceptance_criteria": ["AC one."],
        "risks": [],
        "open_questions": [],
    }
    b.append(
        EventDraft(
            timestamp=_T0,
            actor="test",
            action="prd.parsed",
            target_kind="prd",
            target_id="proj-1",
            payload_json=prd_payload,
        )
    )
    b.append(
        EventDraft(
            timestamp=_T0,
            actor="test",
            action="prd.reviewed",
            target_kind="prd",
            target_id="proj-1",
            payload_json={"project_id": "proj-1", "reviewer": "alice"},
        )
    )


def _insert_feature(conn: sqlite3.Connection, feat_id: str = "F001") -> None:
    conn.execute(
        "INSERT OR IGNORE INTO features "
        "(id, title, description, status, requirements, tasks) "
        "VALUES (?, ?, 'desc', 'proposed', '[]', '[]')",
        (feat_id, f"Feature {feat_id}"),
    )
    conn.commit()


def _insert_task(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    status: str = "ready",
    conflict_groups: list[str] | None = None,
    likely_files: list[str] | None = None,
) -> None:
    conn.execute(
        """INSERT INTO tasks
        (id, feature_id, title, description, status, priority,
         dependencies, conflict_groups, scores, acceptance_criteria,
         implementation_notes, verification, likely_files,
         created_at, updated_at)
        VALUES (?, 'F001', ?, 'desc', ?, 'medium', '[]', ?, '{}', '[]', '[]', '{}', ?, ?, ?)""",
        (
            task_id,
            f"Task {task_id}",
            status,
            json.dumps(conflict_groups or []),
            json.dumps(likely_files or []),
            _T0.isoformat(),
            _T0.isoformat(),
        ),
    )
    conn.commit()


def _reset_tasks_to_ready(conn: sqlite3.Connection, task_ids: list[str]) -> None:
    """Wipe all claims and return the tasks to 'ready' between iterations."""
    conn.execute("DELETE FROM claims")
    for tid in task_ids:
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
    conn.commit()


# ---------------------------------------------------------------------------
# Race harness — records every thread's outcome so we can assert that EVERY
# loser failed cleanly, not just that the winner count is 1.
# ---------------------------------------------------------------------------


@dataclass
class _Outcome:
    actor: str
    task_id: str
    won: bool
    claim_id: str | None
    # Any exception that was NOT a clean ClaimError. Should always be None;
    # a populated value means a loser blew up instead of being told "no".
    dirty_error: BaseException | None


def _run_race(
    backend: SqliteBackend,
    attempts: list[tuple[str, str, list[str]]],
) -> list[_Outcome]:
    """Fire one claim attempt per thread simultaneously; return every outcome.

    ``attempts`` is a list of ``(actor, task_id, expected_files)``. All threads
    are released from a single barrier so they contend as tightly as possible.
    A successful ``claim()`` is a clean win; a ``ClaimError`` is a clean loss;
    any other exception is captured as a ``dirty_error`` (a contract violation).
    """
    barrier = threading.Barrier(len(attempts))
    outcomes: list[_Outcome] = []
    outcomes_lock = threading.Lock()

    # The MySQL backend opens one connection per thread (so threads contend in
    # InnoDB, not in a host-local lock — the whole point). Release each worker's
    # connection when it finishes so many iterations × threads do not exhaust
    # the server's max_connections. SqliteBackend has no such method; guard.
    _discard = getattr(backend, "discard_thread_connection", None)

    def _worker(actor: str, task_id: str, files: list[str]) -> None:
        manager = ClaimManager(backend, SystemClock(), actor=actor)
        won = False
        claim_id: str | None = None
        dirty: BaseException | None = None
        barrier.wait()
        try:
            result = manager.claim(task_id, expected_files=files)
            won = True
            claim_id = result.claim.id
        except ClaimError:
            # Clean, expected "you lost the race" failure — no exception, no
            # successful claim. Exactly what a loser must get.
            won = False
        except BaseException as exc:  # noqa: BLE001 — we WANT to catch and report any leak
            dirty = exc
        finally:
            if _discard is not None:
                _discard()
        with outcomes_lock:
            outcomes.append(
                _Outcome(
                    actor=actor,
                    task_id=task_id,
                    won=won,
                    claim_id=claim_id,
                    dirty_error=dirty,
                )
            )

    threads = [
        threading.Thread(target=_worker, args=(actor, task_id, files))
        for actor, task_id, files in attempts
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return outcomes


def _assert_no_dirty_errors(outcomes: list[_Outcome]) -> None:
    dirty = [o for o in outcomes if o.dirty_error is not None]
    assert not dirty, (
        "losers must fail cleanly (ClaimError only); got "
        f"{[(o.actor, repr(o.dirty_error)) for o in dirty]}"
    )


def _active_claim_count_per_task(db_path: str) -> dict[str, int]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT task_id, COUNT(*) FROM claims "
            "WHERE status = 'active' GROUP BY task_id"
        ).fetchall()
    finally:
        conn.close()
    return {task_id: count for task_id, count in rows}


def _active_claims_with_files(db_path: str) -> list[tuple[str, str, list[str]]]:
    """Return (claim_id, task_id, expected_files) for every ACTIVE claim row."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, task_id, expected_files FROM claims WHERE status = 'active'"
        ).fetchall()
    finally:
        conn.close()
    out: list[tuple[str, str, list[str]]] = []
    for claim_id, task_id, files_json in rows:
        files = json.loads(files_json) if files_json else []
        out.append((claim_id, task_id, files))
    return out


# ---------------------------------------------------------------------------
# (1) >=8 threads, SAME task — exactly one winner; every loser clean.
# ---------------------------------------------------------------------------


def test_same_task_zero_double_claims(backend_and_db) -> None:  # type: ignore[no-untyped-def]
    """N>=8 threads claim the SAME task. One wins; all losers fail cleanly.

    Verified every iteration:
      - exactly one thread reports a successful claim,
      - every other thread caught a ClaimError (no leaked exception),
      - the DB holds exactly one ACTIVE claim for the task.

    Parameterized over backends: SQLite always; MySQL when MYSQL_TEST_URL is set.
    The assertions are byte-for-byte identical across backends — only the
    setup/poke helpers (``db``) differ by dialect.
    """
    b, db = backend_and_db
    _setup_project_and_prd(b)
    db.insert_feature()
    db.insert_task(task_id="T001", likely_files=["a.py"])

    for i in range(_ITERATIONS):
        db.reset_tasks_to_ready(["T001"])
        attempts = [(f"agent-{n}", "T001", ["a.py"]) for n in range(_N_THREADS)]
        outcomes = _run_race(b, attempts)

        _assert_no_dirty_errors(outcomes)
        winners = [o for o in outcomes if o.won]
        assert len(winners) == 1, (
            f"iter {i}: same-task race produced {len(winners)} winners "
            f"(expected exactly 1): {[w.actor for w in winners]}"
        )
        # Persisted state agrees: one active claim for the task.
        assert db.active_claim_count_per_task() == {"T001": 1}


# ---------------------------------------------------------------------------
# (2) >=8 threads, OVERLAPPING tasks (shared file) — exactly one winner.
# ---------------------------------------------------------------------------


def test_overlapping_files_zero_double_claims(backend_and_db) -> None:  # type: ignore[no-untyped-def]
    """N>=8 threads split across two tasks sharing a file. One wins overall.

    This is the headline B01 TOCTOU shape: two DIFFERENT task rows whose
    expected_files overlap on ``shared.py``. Pre-fix both could commit (two
    agents holding the same file); post-fix the in-transaction guard admits
    exactly one. Every loser must fail cleanly, and the DB must never show two
    active claims touching ``shared.py``.

    Parameterized over backends (SQLite always; MySQL when MYSQL_TEST_URL set).
    """
    b, db = backend_and_db
    _setup_project_and_prd(b)
    db.insert_feature()
    db.insert_task(task_id="T001", likely_files=["shared.py", "one.py"])
    db.insert_task(task_id="T002", likely_files=["shared.py", "two.py"])

    for i in range(_ITERATIONS):
        db.reset_tasks_to_ready(["T001", "T002"])
        attempts: list[tuple[str, str, list[str]]] = []
        for n in range(_N_THREADS):
            if n % 2 == 0:
                attempts.append((f"agent-{n}", "T001", ["shared.py", "one.py"]))
            else:
                attempts.append((f"agent-{n}", "T002", ["shared.py", "two.py"]))
        outcomes = _run_race(b, attempts)

        _assert_no_dirty_errors(outcomes)
        winners = [o for o in outcomes if o.won]
        assert len(winners) == 1, (
            f"iter {i}: file-overlap race produced {len(winners)} winners "
            f"(expected exactly 1 — overlapping files must serialize): "
            f"{[(w.actor, w.task_id) for w in winners]}"
        )
        # No two ACTIVE claims may share a file.
        active = db.active_claims_with_files()
        seen_files: set[str] = set()
        for claim_id, _task_id, files in active:
            clash = seen_files & set(files)
            assert not clash, (
                f"iter {i}: claim {claim_id} holds files {sorted(clash)} "
                "already locked by another ACTIVE claim — double-claim!"
            )
            seen_files.update(files)


# ---------------------------------------------------------------------------
# (3) >=8 threads, MULTIPLE distinct tasks each over-subscribed — one winner
#     PER task, every loser clean, no cross-task file double-claim.
# ---------------------------------------------------------------------------


def test_multi_task_one_winner_each_losers_clean(backend_and_db) -> None:  # type: ignore[no-untyped-def]
    """N>=8 threads spread across 4 independent tasks (no shared files).

    Each task is over-subscribed (3 threads per task). Because the tasks touch
    DISJOINT files there is no cross-task conflict, so the correct outcome is
    exactly one winner per task (4 winners total). Every other thread must lose
    cleanly via ClaimError, and the DB must show exactly one active claim per
    task.

    Parameterized over backends (SQLite always; MySQL when MYSQL_TEST_URL set).
    """
    task_ids = ["T001", "T002", "T003", "T004"]
    b, db = backend_and_db
    _setup_project_and_prd(b)
    db.insert_feature()
    for tid in task_ids:
        db.insert_task(task_id=tid, likely_files=[f"{tid.lower()}.py"])

    for i in range(_ITERATIONS):
        db.reset_tasks_to_ready(task_ids)
        # 12 threads / 4 tasks = 3 contenders per task.
        attempts: list[tuple[str, str, list[str]]] = []
        for n in range(_N_THREADS):
            tid = task_ids[n % len(task_ids)]
            attempts.append((f"agent-{n}", tid, [f"{tid.lower()}.py"]))
        outcomes = _run_race(b, attempts)

        _assert_no_dirty_errors(outcomes)

        # Exactly one winner per task.
        winners_by_task: dict[str, int] = {}
        for o in outcomes:
            if o.won:
                winners_by_task[o.task_id] = winners_by_task.get(o.task_id, 0) + 1
        assert winners_by_task == {tid: 1 for tid in task_ids}, (
            f"iter {i}: expected exactly one winner per task, got "
            f"{winners_by_task}"
        )
        # The losers: every non-winner caught a ClaimError (already asserted
        # no dirty errors); confirm the count adds up.
        assert sum(1 for o in outcomes if o.won) == len(task_ids)
        assert sum(1 for o in outcomes if not o.won) == _N_THREADS - len(task_ids)
        # DB agrees: one active claim per task, none doubled.
        assert db.active_claim_count_per_task() == {tid: 1 for tid in task_ids}


# ---------------------------------------------------------------------------
# (4) force=True overrides the in-transaction overlap guard
# ---------------------------------------------------------------------------


def test_force_overrides_in_transaction_overlap(tmp_path: Path) -> None:
    """`force=True` claim succeeds despite an active overlapping claim.

    Migrated from test_claim_concurrency.py to preserve coverage.
    """
    b = _make_backend(tmp_path)
    try:
        _setup_project_and_prd(b)
        conn = sqlite3.connect(str(tmp_path / "state.db"))
        _insert_feature(conn)
        _insert_task(conn, task_id="T001", likely_files=["shared.py"])
        _insert_task(conn, task_id="T002", likely_files=["shared.py"])
        conn.close()

        import pytest as _pytest

        from anvil.claims.manager import ClaimError, ClaimManager

        m1 = ClaimManager(b, SystemClock(), actor="agent-a")
        m1.claim("T001", expected_files=["shared.py"])

        m2 = ClaimManager(b, SystemClock(), actor="agent-b")
        # Without force this must raise ClaimError.
        with _pytest.raises(ClaimError):
            m2.claim("T002", expected_files=["shared.py"])
        # With force it must succeed.
        result = m2.claim("T002", expected_files=["shared.py"], force=True)
        assert result.claim.task_id == "T002"
    finally:
        b.close()


# ---------------------------------------------------------------------------
# (5) CLI claim honours config default_lease_minutes (B02 — sub-minute lease)
# ---------------------------------------------------------------------------


def test_cli_claim_honours_fractional_lease_minutes(tmp_path: Path) -> None:
    """A config with default_lease_minutes: 0.5 yields a ~30 s lease, not 60 min.

    Migrated from test_claim_concurrency.py to preserve B02 coverage.
    """
    import sqlite3 as _sqlite3
    from datetime import UTC, datetime, timedelta

    from typer.testing import CliRunner

    from anvil.cli import app

    state_dir = tmp_path / ".anvil"
    state_dir.mkdir()

    (state_dir / "config.yaml").write_text(
        "project_name: demo\n"
        "project_id: 11111111-1111-1111-1111-111111111111\n"
        "default_lease_minutes: 0.5\n",
        encoding="utf-8",
    )

    b = _make_backend(state_dir)
    try:
        _setup_project_and_prd(b)
        conn = _sqlite3.connect(str(state_dir / "state.db"))
        _insert_feature(conn)
        _insert_task(conn, task_id="T001", likely_files=["a.py"])
        conn.close()
    finally:
        b.close()

    runner = CliRunner()
    before = datetime.now(UTC)
    result = runner.invoke(
        app,
        ["claim", "T001", "--actor", "tester", "--cwd", str(tmp_path)],
    )
    after = datetime.now(UTC)
    assert result.exit_code == 0, result.output

    conn = _sqlite3.connect(str(state_dir / "state.db"))
    row = conn.execute(
        "SELECT created_at, lease_expires_at FROM claims WHERE task_id = 'T001'"
    ).fetchone()
    conn.close()
    assert row is not None
    created_at = datetime.fromisoformat(row[0])
    lease_expires_at = datetime.fromisoformat(row[1])
    lease_seconds = (lease_expires_at - created_at).total_seconds()

    # 0.5 minutes == 30 seconds.
    assert 29.0 <= lease_seconds <= 31.0, (
        f"expected ~30 s lease from default_lease_minutes: 0.5, got {lease_seconds} s"
    )
    assert lease_seconds < 120, "lease must not fall back to the 60-min default"
    assert before - timedelta(seconds=5) <= created_at <= after + timedelta(seconds=5)
