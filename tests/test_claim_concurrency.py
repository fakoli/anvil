"""Concurrency regression tests for the claim/lock primitive (BUG 1 + BUG 2).

BUG 1 (TOCTOU race): the file-overlap conflict check ran in
``ClaimManager.check_conflicts`` BEFORE ``backend.append()`` — outside the
atomic claim transaction. Two concurrent claims on different task rows whose
``expected_files`` overlap could both pass the pre-check and both commit, so
two agents ended up holding leases on overlapping files. The fix re-checks
``expected_files`` (and conflict_group) overlap INSIDE the same
``BEGIN IMMEDIATE`` transaction that writes the claim, so exactly one of two
contending claims wins.

These tests spin N>=8 threads against ONE shared SqliteBackend and assert that
across >=300 iterations there is EXACTLY ONE winner for each contention shape:

  (a) the same task (status guard — passed pre-fix and post-fix),
  (b) two file-overlapping tasks (the headline TOCTOU bug — FAILS pre-fix),
  (c) two tasks in the same conflict_group (TOCTOU — FAILS pre-fix).

BUG 2 (CLI ignores config lease): a small test asserts a CLI claim under a
config with ``default_lease_minutes: 0.5`` yields a ~30 s lease, not 60 min.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from fakoli_state.claims.manager import ClaimError, ClaimManager
from fakoli_state.clock import SystemClock
from fakoli_state.state.models import EventDraft
from fakoli_state.state.sqlite import SqliteBackend

_UTC = UTC
_T0 = datetime(2026, 5, 24, 18, 0, 0, tzinfo=_UTC)

_N_THREADS = 8
_ITERATIONS = 300


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------


def _make_backend(state_dir: Path) -> SqliteBackend:
    db_path = str(state_dir / "state.db")
    events_path = str(state_dir / "events.jsonl")
    Path(events_path).touch()
    # SystemClock so each claim's timestamps are distinct — irrelevant to the
    # single-winner property, which is enforced by the in-transaction guard.
    b = SqliteBackend(db_path=db_path, events_path=events_path, clock=SystemClock())
    b.initialize()
    return b


def _setup_project_and_prd(b: SqliteBackend) -> None:
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


def _reset_tasks_to_ready(
    conn: sqlite3.Connection, task_ids: list[str]
) -> None:
    """Return tasks to 'ready' and wipe all claims between iterations."""
    conn.execute("DELETE FROM claims")
    for tid in task_ids:
        conn.execute(
            "UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,)
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Core race harness
# ---------------------------------------------------------------------------


def _run_race(
    backend: SqliteBackend,
    attempts: list[tuple[str, str, list[str]]],
) -> int:
    """Fire one claim attempt per thread simultaneously; return winner count.

    ``attempts`` is a list of (actor, task_id, expected_files). All threads are
    released from a single barrier so they contend as tightly as possible.
    A successful ``claim()`` counts as one winner; a ClaimError (lost the race)
    does not.
    """
    barrier = threading.Barrier(len(attempts))
    winners: list[str] = []
    winners_lock = threading.Lock()

    def _worker(actor: str, task_id: str, files: list[str]) -> None:
        manager = ClaimManager(backend, SystemClock(), actor=actor)
        barrier.wait()
        try:
            manager.claim(task_id, expected_files=files)
        except ClaimError:
            return
        with winners_lock:
            winners.append(f"{actor}:{task_id}")

    threads = [
        threading.Thread(target=_worker, args=(actor, task_id, files))
        for actor, task_id, files in attempts
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return len(winners)


# ---------------------------------------------------------------------------
# (a) same task
# ---------------------------------------------------------------------------


def test_concurrent_same_task_single_winner(tmp_path: Path) -> None:
    """N threads claim the SAME task — exactly one wins, every iteration."""
    b = _make_backend(tmp_path)
    try:
        _setup_project_and_prd(b)
        conn = sqlite3.connect(str(tmp_path / "state.db"))
        _insert_feature(conn)
        _insert_task(conn, task_id="T001", likely_files=["a.py"])
        conn.close()

        conn = sqlite3.connect(str(tmp_path / "state.db"))
        for _ in range(_ITERATIONS):
            _reset_tasks_to_ready(conn, ["T001"])
            attempts = [
                (f"agent-{i}", "T001", ["a.py"]) for i in range(_N_THREADS)
            ]
            winners = _run_race(b, attempts)
            assert winners == 1, f"expected 1 winner, got {winners}"
        conn.close()
    finally:
        b.close()


# ---------------------------------------------------------------------------
# (b) two file-overlapping tasks  (the headline BUG 1)
# ---------------------------------------------------------------------------


def test_concurrent_file_overlap_single_winner(tmp_path: Path) -> None:
    """Two DIFFERENT tasks share a file; concurrent claims → exactly one wins.

    This is the proven ~8% dual-claim race. Pre-fix the overlap check ran
    outside the transaction so both claims (on T001 and T002) could commit;
    post-fix the in-transaction re-check rejects the loser.
    """
    b = _make_backend(tmp_path)
    try:
        _setup_project_and_prd(b)
        conn = sqlite3.connect(str(tmp_path / "state.db"))
        _insert_feature(conn)
        # Two distinct tasks that both touch shared.py.
        _insert_task(conn, task_id="T001", likely_files=["shared.py", "one.py"])
        _insert_task(conn, task_id="T002", likely_files=["shared.py", "two.py"])
        conn.close()

        conn = sqlite3.connect(str(tmp_path / "state.db"))
        for _ in range(_ITERATIONS):
            _reset_tasks_to_ready(conn, ["T001", "T002"])
            # Half the threads go for T001, half for T002 — different task rows,
            # different actors, all overlapping on shared.py.
            attempts: list[tuple[str, str, list[str]]] = []
            for i in range(_N_THREADS):
                if i % 2 == 0:
                    attempts.append((f"agent-{i}", "T001", ["shared.py", "one.py"]))
                else:
                    attempts.append((f"agent-{i}", "T002", ["shared.py", "two.py"]))
            winners = _run_race(b, attempts)
            assert winners == 1, (
                f"file-overlap race produced {winners} winners "
                "(expected exactly 1 — overlapping files must serialize)"
            )
        conn.close()
    finally:
        b.close()


# ---------------------------------------------------------------------------
# (c) two tasks in the same conflict group
# ---------------------------------------------------------------------------


def test_concurrent_conflict_group_single_winner(tmp_path: Path) -> None:
    """Two DIFFERENT tasks in one conflict_group → concurrent claims, one wins."""
    b = _make_backend(tmp_path)
    try:
        _setup_project_and_prd(b)
        conn = sqlite3.connect(str(tmp_path / "state.db"))
        _insert_feature(conn)
        # Distinct files (so the file-overlap guard does NOT mask the group
        # guard) but a shared conflict_group "auth".
        _insert_task(
            conn,
            task_id="T001",
            conflict_groups=["auth"],
            likely_files=["t1.py"],
        )
        _insert_task(
            conn,
            task_id="T002",
            conflict_groups=["auth"],
            likely_files=["t2.py"],
        )
        conn.close()

        conn = sqlite3.connect(str(tmp_path / "state.db"))
        for _ in range(_ITERATIONS):
            _reset_tasks_to_ready(conn, ["T001", "T002"])
            attempts: list[tuple[str, str, list[str]]] = []
            for i in range(_N_THREADS):
                if i % 2 == 0:
                    attempts.append((f"agent-{i}", "T001", ["t1.py"]))
                else:
                    attempts.append((f"agent-{i}", "T002", ["t2.py"]))
            winners = _run_race(b, attempts)
            assert winners == 1, (
                f"conflict_group race produced {winners} winners "
                "(expected exactly 1 — a conflict_group admits one active claim)"
            )
        conn.close()
    finally:
        b.close()


# ---------------------------------------------------------------------------
# force override still wins both files and groups
# ---------------------------------------------------------------------------


def test_force_overrides_in_transaction_overlap(tmp_path: Path) -> None:
    """`force=True` claim still succeeds despite an active overlapping claim."""
    b = _make_backend(tmp_path)
    try:
        _setup_project_and_prd(b)
        conn = sqlite3.connect(str(tmp_path / "state.db"))
        _insert_feature(conn)
        _insert_task(conn, task_id="T001", likely_files=["shared.py"])
        _insert_task(conn, task_id="T002", likely_files=["shared.py"])
        conn.close()

        m1 = ClaimManager(b, SystemClock(), actor="agent-a")
        m1.claim("T001", expected_files=["shared.py"])

        m2 = ClaimManager(b, SystemClock(), actor="agent-b")
        # Without force this would raise; with force it must succeed.
        with pytest.raises(ClaimError):
            m2.claim("T002", expected_files=["shared.py"])
        result = m2.claim("T002", expected_files=["shared.py"], force=True)
        assert result.claim.task_id == "T002"
    finally:
        b.close()


# ---------------------------------------------------------------------------
# BUG 2 — CLI claim honours config default_lease_minutes (sub-minute lease)
# ---------------------------------------------------------------------------


def test_cli_claim_honours_fractional_lease_minutes(tmp_path: Path) -> None:
    """A config with default_lease_minutes: 0.5 yields a ~30 s lease, not 60 min."""
    from typer.testing import CliRunner

    from fakoli_state.cli import app

    state_dir = tmp_path / ".fakoli-state"
    state_dir.mkdir()

    # Minimal config with a fractional lease.
    (state_dir / "config.yaml").write_text(
        "project_name: demo\n"
        "project_id: 11111111-1111-1111-1111-111111111111\n"
        "default_lease_minutes: 0.5\n",
        encoding="utf-8",
    )

    b = _make_backend(state_dir)
    try:
        _setup_project_and_prd(b)
        conn = sqlite3.connect(str(state_dir / "state.db"))
        _insert_feature(conn)
        _insert_task(conn, task_id="T001", likely_files=["a.py"])
        conn.close()
    finally:
        b.close()

    runner = CliRunner()
    before = datetime.now(_UTC)
    result = runner.invoke(
        app,
        ["claim", "T001", "--actor", "tester", "--cwd", str(tmp_path)],
    )
    after = datetime.now(_UTC)
    assert result.exit_code == 0, result.output

    # Read the created claim's lease from the DB.
    conn = sqlite3.connect(str(state_dir / "state.db"))
    row = conn.execute(
        "SELECT created_at, lease_expires_at FROM claims WHERE task_id = 'T001'"
    ).fetchone()
    conn.close()
    assert row is not None
    created_at = datetime.fromisoformat(row[0])
    lease_expires_at = datetime.fromisoformat(row[1])
    lease_seconds = (lease_expires_at - created_at).total_seconds()

    # 0.5 minutes == 30 seconds. Assert it's ~30 s and NOT the 60-min default.
    assert 29.0 <= lease_seconds <= 31.0, (
        f"expected ~30 s lease from default_lease_minutes: 0.5, got "
        f"{lease_seconds} s"
    )
    assert lease_seconds < 120, "lease must not fall back to the 60-min default"
    # Sanity: the claim was created within the wall-clock window of the call.
    assert before - timedelta(seconds=5) <= created_at <= after + timedelta(seconds=5)
