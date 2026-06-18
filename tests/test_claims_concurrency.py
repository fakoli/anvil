"""B03 — concurrency regression suite: ZERO double-claims under contention.

This suite is the acceptance harness for B01's atomicity fix (the TOCTOU race
in ``ClaimManager.claim`` where the file-overlap / conflict_group pre-check ran
OUTSIDE the atomic claim transaction). B01 moved the authoritative re-check
INSIDE the ``BEGIN IMMEDIATE`` transaction that writes the claim row, so exactly
one of N contending claims can ever win.

Where ``test_claim_concurrency.py`` proves the *winner count is 1*, this suite
hardens the contract from the loser's side: with >=8 threads all racing the
SAME and OVERLAPPING tasks, it asserts that

  - EXACTLY ONE winner exists per contended task,
  - every loser returns a CLEAN failed/blocked outcome (a caught ``ClaimError``,
    never a leaked/unexpected exception),
  - no loser ever produces a second successful claim, and
  - the persisted DB agrees: at most one ACTIVE claim per task, and no two
    active claims share a file.

NO mocking: every test drives a real ``SqliteBackend`` (the repo forbids mocking
the backend). All threads share the ONE backend instance and are released from a
single ``threading.Barrier`` so they contend as tightly as the GIL allows.

If any assertion here fails it means B01 is not actually atomic — the failure
output names the contention shape and the offending winner/claim count so a
human can re-open B01.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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


# ---------------------------------------------------------------------------
# Setup helpers (real SqliteBackend — no mocking)
# ---------------------------------------------------------------------------


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


def test_same_task_zero_double_claims(tmp_path: Path) -> None:
    """N>=8 threads claim the SAME task. One wins; all losers fail cleanly.

    Verified every iteration:
      - exactly one thread reports a successful claim,
      - every other thread caught a ClaimError (no leaked exception),
      - the DB holds exactly one ACTIVE claim for the task.
    """
    db_path = str(tmp_path / "state.db")
    b = _make_backend(tmp_path)
    try:
        _setup_project_and_prd(b)
        conn = sqlite3.connect(db_path)
        _insert_feature(conn)
        _insert_task(conn, task_id="T001", likely_files=["a.py"])
        conn.close()

        conn = sqlite3.connect(db_path)
        for i in range(_ITERATIONS):
            _reset_tasks_to_ready(conn, ["T001"])
            attempts = [
                (f"agent-{n}", "T001", ["a.py"]) for n in range(_N_THREADS)
            ]
            outcomes = _run_race(b, attempts)

            _assert_no_dirty_errors(outcomes)
            winners = [o for o in outcomes if o.won]
            assert len(winners) == 1, (
                f"iter {i}: same-task race produced {len(winners)} winners "
                f"(expected exactly 1): {[w.actor for w in winners]}"
            )
            # Persisted state agrees: one active claim for the task.
            assert _active_claim_count_per_task(db_path) == {"T001": 1}
        conn.close()
    finally:
        b.close()


# ---------------------------------------------------------------------------
# (2) >=8 threads, OVERLAPPING tasks (shared file) — exactly one winner.
# ---------------------------------------------------------------------------


def test_overlapping_files_zero_double_claims(tmp_path: Path) -> None:
    """N>=8 threads split across two tasks sharing a file. One wins overall.

    This is the headline B01 TOCTOU shape: two DIFFERENT task rows whose
    expected_files overlap on ``shared.py``. Pre-fix both could commit (two
    agents holding the same file); post-fix the in-transaction guard admits
    exactly one. Every loser must fail cleanly, and the DB must never show two
    active claims touching ``shared.py``.
    """
    db_path = str(tmp_path / "state.db")
    b = _make_backend(tmp_path)
    try:
        _setup_project_and_prd(b)
        conn = sqlite3.connect(db_path)
        _insert_feature(conn)
        _insert_task(conn, task_id="T001", likely_files=["shared.py", "one.py"])
        _insert_task(conn, task_id="T002", likely_files=["shared.py", "two.py"])
        conn.close()

        conn = sqlite3.connect(db_path)
        for i in range(_ITERATIONS):
            _reset_tasks_to_ready(conn, ["T001", "T002"])
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
            active = _active_claims_with_files(db_path)
            seen_files: set[str] = set()
            for claim_id, _task_id, files in active:
                clash = seen_files & set(files)
                assert not clash, (
                    f"iter {i}: claim {claim_id} holds files {sorted(clash)} "
                    "already locked by another ACTIVE claim — double-claim!"
                )
                seen_files.update(files)
        conn.close()
    finally:
        b.close()


# ---------------------------------------------------------------------------
# (3) >=8 threads, MULTIPLE distinct tasks each over-subscribed — one winner
#     PER task, every loser clean, no cross-task file double-claim.
# ---------------------------------------------------------------------------


def test_multi_task_one_winner_each_losers_clean(tmp_path: Path) -> None:
    """N>=8 threads spread across 4 independent tasks (no shared files).

    Each task is over-subscribed (3 threads per task). Because the tasks touch
    DISJOINT files there is no cross-task conflict, so the correct outcome is
    exactly one winner per task (4 winners total). Every other thread must lose
    cleanly via ClaimError, and the DB must show exactly one active claim per
    task.
    """
    db_path = str(tmp_path / "state.db")
    task_ids = ["T001", "T002", "T003", "T004"]
    b = _make_backend(tmp_path)
    try:
        _setup_project_and_prd(b)
        conn = sqlite3.connect(db_path)
        _insert_feature(conn)
        for tid in task_ids:
            _insert_task(conn, task_id=tid, likely_files=[f"{tid.lower()}.py"])
        conn.close()

        conn = sqlite3.connect(db_path)
        for i in range(_ITERATIONS):
            _reset_tasks_to_ready(conn, task_ids)
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
            assert _active_claim_count_per_task(db_path) == {
                tid: 1 for tid in task_ids
            }
        conn.close()
    finally:
        b.close()
