"""T006 — concurrency proof for fan_out.

Proves the wedge holds for tasks created through the WF-3 fan_out path: N
parallel claims over overlapping ``expected_files`` yield exactly one winner per
contended item (every loser clean, zero double-claims), and concurrent evidence
submission across distinct items loses nothing. Mirrors the rigor of
``test_claims_concurrency.py`` (>=8 threads, >=200 iterations).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from anvil.claims.manager import ClaimError, ClaimManager
from anvil.clock import SystemClock
from anvil.state.models import EventDraft
from anvil.state.sqlite import SqliteBackend
from anvil.workflows.tasks import create_workflow_task, submit_workflow_evidence

_T0 = datetime(2026, 5, 24, 18, 0, 0, tzinfo=UTC)
_N_THREADS = 8
_ITERATIONS = 200


def _make_backend(state_dir: Path) -> SqliteBackend:
    db_path = str(state_dir / "state.db")
    events_path = str(state_dir / "events.jsonl")
    Path(events_path).touch()
    b = SqliteBackend(db_path=db_path, events_path=events_path, clock=SystemClock())
    b.initialize()
    return b


def _setup_prd(b: SqliteBackend) -> None:
    def ev(action: str, payload: dict[str, Any], kind: str) -> EventDraft:
        return EventDraft(
            timestamp=_T0, actor="test", action=action,
            target_kind=kind, target_id="proj-1", payload_json=payload,
        )

    b.append(ev("project.created",
               {"id": "proj-1", "name": "P", "description": "",
                "created_at": _T0.isoformat(), "updated_at": _T0.isoformat()}, "project"))
    b.append(ev("state.initialized", {}, "project"))
    b.append(ev("prd.parsed",
               {"project_id": "proj-1", "status": "draft", "summary": "S.",
                "goals": ["G."], "non_goals": [],
                "requirements": [{"id": "R001", "prd_section": "requirements",
                                  "text": "R.", "source_paragraph": None, "derived": False}],
                "acceptance_criteria": ["AC."], "risks": [], "open_questions": []}, "prd"))
    b.append(ev("prd.reviewed", {"project_id": "proj-1", "reviewer": "a"}, "prd"))


@dataclass
class _Outcome:
    actor: str
    won: bool
    claim_id: str | None
    dirty_error: BaseException | None


def _race_claims(
    backend: SqliteBackend, attempts: list[tuple[str, str, list[str]]]
) -> list[_Outcome]:
    """Fire one claim per thread from a single barrier; record every outcome."""
    barrier = threading.Barrier(len(attempts))
    outcomes: list[_Outcome] = []
    lock = threading.Lock()

    def worker(actor: str, task_id: str, files: list[str]) -> None:
        mgr = ClaimManager(backend, SystemClock(), actor=actor)
        won, claim_id, dirty = False, None, None
        barrier.wait()
        try:
            claim_id = mgr.claim(task_id, expected_files=files).claim.id
            won = True
        except ClaimError:
            won = False
        except BaseException as exc:  # noqa: BLE001 — any leak is a contract violation
            dirty = exc
        with lock:
            outcomes.append(_Outcome(actor, won, claim_id, dirty))

    threads = [
        threading.Thread(target=worker, args=a) for a in attempts
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return outcomes


def test_fan_out_overlapping_item_exactly_one_winner(tmp_path: Path) -> None:
    """N runners fanning out over the SAME item → exactly one winner per round."""
    b = _make_backend(tmp_path)
    _setup_prd(b)
    setup_clock = SystemClock()

    for i in range(_ITERATIONS):
        # N distinct workflow tasks, all scoped to the SAME file — the fan-out
        # collision shape (two runs fanning out over the same item).
        task_ids = [
            create_workflow_task(
                b, title=f"fix-{i}-{n}", description="d",
                actor="setup", clock=setup_clock, likely_files=["shared.py"],
            )
            for n in range(_N_THREADS)
        ]
        attempts = [(f"agent-{n}", task_ids[n], ["shared.py"]) for n in range(_N_THREADS)]
        outcomes = _race_claims(b, attempts)

        winners = [o for o in outcomes if o.won]
        assert len(winners) == 1, (
            f"iter {i}: file-overlap race produced {len(winners)} winners "
            f"(expected exactly 1)"
        )
        assert all(o.dirty_error is None for o in outcomes), (
            f"iter {i}: a loser raised a non-ClaimError: "
            f"{[o.dirty_error for o in outcomes if o.dirty_error]}"
        )
        # release the winner so the shared file frees up for the next round
        ClaimManager(b, SystemClock(), actor=winners[0].actor).release(
            winners[0].claim_id  # type: ignore[arg-type]
        )


def test_concurrent_evidence_submission_loses_nothing(tmp_path: Path) -> None:
    """N winners on distinct items submit evidence concurrently → none lost."""
    b = _make_backend(tmp_path)
    _setup_prd(b)
    clock = SystemClock()
    n = _N_THREADS

    task_ids = [
        create_workflow_task(
            b, title=f"t{k}", description="d", actor="setup", clock=clock,
            likely_files=[f"f{k}.py"],
        )
        for k in range(n)
    ]

    barrier = threading.Barrier(n)
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker(k: int) -> None:
        try:
            mgr = ClaimManager(b, SystemClock(), actor=f"agent-{k}")
            claim = mgr.claim(task_ids[k], expected_files=[f"f{k}.py"]).claim
            barrier.wait()  # all submit as close to simultaneously as possible
            submit_workflow_evidence(
                b, task_id=task_ids[k], claim_id=claim.id, actor=f"agent-{k}",
                clock=SystemClock(), commands=["check"], files_changed=[f"f{k}.py"],
            )
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(k,)) for k in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent submit raised: {errors}"
    ev_task_ids = {e.task_id for e in b.list_evidence() if e.task_id in set(task_ids)}
    assert ev_task_ids == set(task_ids), "evidence rows were lost under concurrency"
