"""Workflow-origin task lifecycle (T003.1) — the seam the WF-3 runner needs.

The runner needs a claimable Task per step so `Evidence` has something to attach
to. PRD-derived tasks come from parse/plan; *workflow-origin* tasks are created
here — directly at `ready`, marked so they are distinguishable from PRD tasks and
never pollute the PRD queue.

These are thin wrappers over the same engine events the CLI emits
(`task.created` / `evidence.submitted` / `task.applied`); claiming reuses
`ClaimManager`. No new engine method is added — this is the missing *creation*
seam plus convenience submit/apply the runner (T003) drives per step.

    # ponytail: workflow tasks hang off one sentinel feature (FK requires a
    # feature row); they're marked by id-prefix + a notes sentinel rather than a
    # new schema column — a column is the upgrade path if querying them grows.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from anvil.state.models import EventDraft, Feature, Task, TaskStatus, Verification

if TYPE_CHECKING:
    from anvil.clock import Clock
    from anvil.state.sqlite import SqliteBackend

__all__ = [
    "WORKFLOW_FEATURE_ID",
    "WORKFLOW_TASK_PREFIX",
    "apply_workflow_task",
    "create_workflow_task",
    "is_workflow_task",
    "submit_workflow_evidence",
]

WORKFLOW_TASK_PREFIX = "WT"
WORKFLOW_FEATURE_ID = "FWORKFLOW"
_ORIGIN_MARKER = "workflow-origin"


def is_workflow_task(task: Task) -> bool:
    """True if a task was created by the workflow runner, not the PRD."""
    return task.id.startswith(WORKFLOW_TASK_PREFIX) or any(
        n.startswith(_ORIGIN_MARKER) for n in task.implementation_notes
    )


def _ensure_workflow_feature(backend: SqliteBackend, actor: str, clock: Clock) -> None:
    """Idempotently ensure the sentinel feature workflow tasks hang off exists."""
    if backend.get_feature(WORKFLOW_FEATURE_ID) is not None:
        return
    feature = Feature(
        id=WORKFLOW_FEATURE_ID,
        title="Workflow-origin tasks",
        description="Sentinel feature for tasks created by the WF-3 runner.",
    )
    backend.append(
        EventDraft(
            timestamp=clock.now(),
            actor=actor,
            action="feature.created",
            target_kind="feature",
            target_id=WORKFLOW_FEATURE_ID,
            payload_json=feature.model_dump(mode="json"),
        )
    )


def create_workflow_task(
    backend: SqliteBackend,
    *,
    title: str,
    description: str,
    actor: str,
    clock: Clock,
    acceptance_criteria: list[str] | None = None,
    verification: Verification | None = None,
    likely_files: list[str] | None = None,
    run_name: str = "",
    step_id: str = "",
) -> str:
    """Create a claimable workflow-origin task at `ready`; return its id.

    Emits one `task.created` event. The task is marked (id prefix + an
    `implementation_notes` sentinel) so :func:`is_workflow_task` can tell it
    apart from PRD-derived tasks.
    """
    _ensure_workflow_feature(backend, actor, clock)
    now = clock.now()
    task_id = f"{WORKFLOW_TASK_PREFIX}-{uuid.uuid4().hex[:8].upper()}"
    task = Task(
        id=task_id,
        feature_id=WORKFLOW_FEATURE_ID,
        title=title,
        description=description,
        status=TaskStatus.ready,
        acceptance_criteria=acceptance_criteria or [],
        verification=verification or Verification(),
        likely_files=likely_files or [],
        implementation_notes=[f"{_ORIGIN_MARKER}:{run_name}:{step_id}"],
        created_at=now,
        updated_at=now,
    )
    backend.append(
        EventDraft(
            timestamp=now,
            actor=actor,
            action="task.created",
            target_kind="task",
            target_id=task_id,
            payload_json=task.model_dump(mode="json"),
        )
    )
    return task_id


def submit_workflow_evidence(
    backend: SqliteBackend,
    *,
    task_id: str,
    claim_id: str,
    actor: str,
    clock: Clock,
    commands: list[str],
    files_changed: list[str] | None = None,
    output_excerpt: str | None = None,
) -> str:
    """Submit evidence for a workflow task. The engine auto-releases the claim
    and moves the task to `needs_review`. Returns the evidence id.

    ``commands`` must be non-empty — the engine rejects empty evidence.
    """
    if not commands:
        raise ValueError("submit_workflow_evidence requires at least one command")
    now = clock.now()
    evidence_id = "EV" + uuid.uuid4().hex[:8].upper()
    backend.append(
        EventDraft(
            timestamp=now,
            actor=actor,
            action="evidence.submitted",
            target_kind="task",
            target_id=task_id,
            payload_json={
                "task_id": task_id,
                "claim_id": claim_id,
                "submitted_by": actor,
                "evidence_id": evidence_id,
                "commands_run": commands,
                "files_changed": files_changed or [],
                "output_excerpt": output_excerpt,
                "pr_url": None,
                "commit_sha": None,
                "screenshots": [],
                "known_limitations": None,
            },
        )
    )
    return evidence_id


def apply_workflow_task(
    backend: SqliteBackend,
    *,
    task_id: str,
    reviewer: str,
    clock: Clock,
    decision: str = "accepted",
    notes: str | None = None,
) -> None:
    """Apply a review decision for a workflow task (accepted → done)."""
    backend.append(
        EventDraft(
            timestamp=clock.now(),
            actor=reviewer,
            action="task.applied",
            target_kind="task",
            target_id=task_id,
            payload_json={
                "task_id": task_id,
                "reviewer": reviewer,
                "decision": decision,
                "notes": notes,
            },
        )
    )
