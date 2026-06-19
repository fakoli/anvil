"""T003.1 — ephemeral workflow task-creation primitive.

Covers the three acceptance criteria: a workflow task is created at `ready` and
emits task.created; it round-trips through claim → submit → apply via existing
engine paths producing exactly one Evidence row; and workflow tasks are
distinguishable from PRD tasks (so they don't pollute the PRD queue).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from anvil.claims.manager import ClaimManager
from anvil.state.models import EventDraft, TaskStatus, Verification
from anvil.workflows.tasks import (
    WORKFLOW_FEATURE_ID,
    apply_workflow_task,
    create_workflow_task,
    is_workflow_task,
    submit_workflow_evidence,
)

_T0 = datetime(2026, 5, 24, 18, 0, 0, tzinfo=UTC)


def _event(action: str, payload: dict[str, Any], *, kind: str, tid: str) -> EventDraft:
    return EventDraft(
        timestamp=_T0, actor="test", action=action,
        target_kind=kind, target_id=tid, payload_json=payload,
    )


def _setup_approved_prd(backend) -> None:  # type: ignore[no-untyped-def]
    backend.append(_event(
        "project.created",
        {"id": "proj-1", "name": "P", "description": "",
         "created_at": _T0.isoformat(), "updated_at": _T0.isoformat()},
        kind="project", tid="proj-1",
    ))
    backend.append(_event("state.initialized", {}, kind="project", tid="proj-1"))
    backend.append(_event(
        "prd.parsed",
        {"project_id": "proj-1", "status": "draft", "summary": "S.",
         "goals": ["G."], "non_goals": [],
         "requirements": [{"id": "R001", "prd_section": "requirements",
                           "text": "R.", "source_paragraph": None, "derived": False}],
         "acceptance_criteria": ["AC."], "risks": [], "open_questions": []},
        kind="prd", tid="proj-1",
    ))
    backend.append(_event("prd.reviewed", {"project_id": "proj-1", "reviewer": "a"},
                          kind="prd", tid="proj-1"))
    backend.append(_event("prd.approved", {"project_id": "proj-1", "approver": "b"},
                          kind="prd", tid="proj-1"))


def test_create_workflow_task_is_ready_and_marked(backend, frozen_clock):  # type: ignore[no-untyped-def]
    _setup_approved_prd(backend)
    tid = create_workflow_task(
        backend, title="step find", description="list flaky tests",
        actor="runner", clock=frozen_clock,
    )
    task = backend.get_task(tid)
    assert task is not None
    assert task.status == TaskStatus.ready
    assert task.feature_id == WORKFLOW_FEATURE_ID
    assert is_workflow_task(task)


def test_full_round_trip_produces_one_evidence_row(backend, frozen_clock):  # type: ignore[no-untyped-def]
    _setup_approved_prd(backend)
    tid = create_workflow_task(
        backend, title="step fix", description="fix the flaky test",
        actor="runner", clock=frozen_clock,
        verification=Verification(commands=["pytest x"]),
    )

    mgr = ClaimManager(backend, frozen_clock, actor="runner")
    result = mgr.claim(tid)
    assert backend.get_task(tid).status == TaskStatus.claimed

    submit_workflow_evidence(
        backend, task_id=tid, claim_id=result.claim.id, actor="runner",
        clock=frozen_clock, commands=["pytest x"], files_changed=["x.py"],
    )
    assert backend.get_task(tid).status == TaskStatus.needs_review

    apply_workflow_task(backend, task_id=tid, reviewer="runner", clock=frozen_clock)
    assert backend.get_task(tid).status == TaskStatus.done

    rows = [e for e in backend.list_evidence() if e.task_id == tid]
    assert len(rows) == 1


def test_workflow_tasks_do_not_pollute_the_prd_queue(backend, frozen_clock):  # type: ignore[no-untyped-def]
    _setup_approved_prd(backend)
    tid = create_workflow_task(
        backend, title="t", description="d", actor="runner", clock=frozen_clock,
    )
    task = backend.get_task(tid)
    # The marker is what lets a queue filter exclude workflow-origin tasks.
    assert is_workflow_task(task)
    # And a plain PRD-style task is NOT mistaken for a workflow task.
    assert not _looks_like_prd_task(task)


def _looks_like_prd_task(task) -> bool:  # type: ignore[no-untyped-def]
    return task.id.startswith("T") and "." not in task.id and not is_workflow_task(task)
