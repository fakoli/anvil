"""Small, audited task transitions shared by board previews and the engine."""

from __future__ import annotations

from datetime import UTC, datetime

from anvil.actors import canonicalize_new_actor
from anvil.state.backend import Backend, EventRejected, TransactionAborted
from anvil.state.models import (
    TERMINAL_TASK_STATUSES,
    Claim,
    ClaimStatus,
    EventDraft,
    Task,
    TaskClosure,
    TaskStatus,
)
from anvil.state.payloads import TaskClosedPayload, TaskTransitionedPayload
from anvil.state.transitions import (
    TransitionError,
    task_drafted_to_reviewed,
    task_reviewed_to_ready,
)


def permitted_task_actions(
    task: Task, actor: str, claims: list[Claim], bundle_owned: bool, now: datetime,
) -> list[str]:
    """Pure policy, repeated under the append lock before any event is logged."""
    if task.status in TERMINAL_TASK_STATUSES or bundle_owned:
        return []
    task_claims = [claim for claim in claims if claim.task_id == task.id]
    # Root-set work retains owner-controller authority even after its lease.
    if any(claim.root_set is not None for claim in task_claims):
        return []
    active = [claim for claim in task_claims if claim.status == ClaimStatus.active]
    if active:
        if len(active) != 1 or active[0].claimed_by != actor or active[0].lease_expires_at <= now:
            return []
        if active[0].bundle_claim_id is not None:
            return []
        return {
            TaskStatus.claimed: ["start", "block"],
            TaskStatus.in_progress: ["block"],
            TaskStatus.blocked: ["resume"],
        }.get(task.status, [])
    actions = ["close"]
    if task.status == TaskStatus.ready:
        actions.insert(0, "backlog")
    elif task.status in {TaskStatus.drafted, TaskStatus.reviewed}:
        try:
            reviewed = (
                task_drafted_to_reviewed(task, now) if task.status == TaskStatus.drafted else task
            )
            task_reviewed_to_ready(reviewed, now)
        except TransitionError:
            pass
        else:
            actions.insert(0, "ready")
    return actions


def available_task_actions(backend: Backend, task: Task, actor: str) -> list[str]:
    """Advisory availability; committing always rechecks custody and task version."""
    actor = canonicalize_new_actor(actor)
    if task.status in TERMINAL_TASK_STATUSES:
        return []
    claims, bundle_owned = backend.task_action_context(task.id)
    return permitted_task_actions(task, actor, claims, bundle_owned, datetime.now(UTC))


def transition_task(
    backend: Backend,
    task_id: str,
    action: str,
    actor: str,
    *,
    expected_status: str,
    expected_updated_at: str | datetime,
    reason: str | None = None,
    resolution: str | None = None,
    duplicate_of: str | None = None,
) -> Task:
    """Record one allowed action; never claim work or fabricate evidence."""
    actor = canonicalize_new_actor(actor)
    now = datetime.now(UTC)
    common = {
        "task_id": task_id,
        "expected_status": expected_status,
        "expected_updated_at": expected_updated_at,
    }
    payload: TaskClosedPayload | TaskTransitionedPayload
    if action == "close":
        closure = TaskClosure.model_validate({
            "resolution": resolution, "reason": reason, "closed_by": actor,
            "closed_at": now, "duplicate_of": duplicate_of,
        })
        payload = TaskClosedPayload.model_validate({**common, "closure": closure})
        event_action = "task.closed"
    else:
        if resolution is not None or duplicate_of is not None:
            raise EventRejected("closure fields require the close action")
        payload = TaskTransitionedPayload.model_validate(
            {**common, "action": action, "reason": reason}
        )
        event_action = "task.transitioned"
    backend.append(EventDraft(
        timestamp=now, actor=actor, action=event_action,
        target_kind="task", target_id=task_id, payload_json=payload.model_dump(mode="json"),
    ))
    task = backend.get_task(task_id)
    if task is None:
        raise TransactionAborted(
            "task disappeared after transition append; the event may already be durable"
        )
    return task
