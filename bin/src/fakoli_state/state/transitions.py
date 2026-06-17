"""Pure state-machine transitions for fakoli-state entities.

Design rules (enforced by this module's structure):
- NO I/O. NO database. NO Pydantic side-effects beyond model_copy().
- Each transition function takes the current entity + context, returns the
  updated entity (a new Pydantic model instance via model_copy(update=...)).
- Gates are checked before mutation; TransitionError is raised if a gate fails.
- Private helpers (_can_*, _evidence_complete, …) return bool or raise directly.
  They are the named "gates" referenced in TransitionError.gate_name.

Naming: every public function is named <entity>_<from>_to_<to> so the full
transition table is greppable. The one exception is <entity>_to_<state> for
transitions that can originate from multiple source states (e.g. prd_to_rejected).
"""

from __future__ import annotations

import datetime

from fakoli_state.review.gates import evidence_complete
from fakoli_state.state.models import (
    PRD,
    Claim,
    Evidence,
    PRDStatus,
    Task,
    TaskStatus,
)

__all__ = [
    "TransitionError",
    # PRD transitions
    "prd_draft_to_reviewed",
    "prd_reviewed_to_approved",
    "prd_to_rejected",
    # Task transitions
    "task_proposed_to_drafted",
    "task_drafted_to_reviewed",
    "task_reviewed_to_ready",
    "task_ready_to_claimed",
    "task_claimed_to_in_progress",
    "task_in_progress_to_blocked",
    "task_blocked_to_in_progress",
    "task_in_progress_to_needs_review",
    "task_needs_review_to_accepted",
    "task_accepted_to_done",
    "task_needs_review_to_rejected",
    "task_rejected_to_drafted",
]


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class TransitionError(Exception):
    """Raised when a state transition cannot proceed.

    Attributes:
        code:           Machine-readable error code (e.g. "wrong_status",
                        "gate_failed").
        gate_name:      The name of the gate check that failed, or None when
                        the error is a simple wrong-status check.
        current_status: The entity's status at the time of the failed transition.
        message:        Human-readable explanation.
    """

    def __init__(
        self,
        code: str,
        current_status: str,
        message: str,
        *,
        gate_name: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.gate_name = gate_name
        self.current_status = current_status
        self.message = message

    def __repr__(self) -> str:
        return (
            f"TransitionError(code={self.code!r}, gate_name={self.gate_name!r}, "
            f"current_status={self.current_status!r}, message={self.message!r})"
        )


# ---------------------------------------------------------------------------
# Private gate helpers
# ---------------------------------------------------------------------------


def _assert_prd_status(prd: PRD, expected: PRDStatus, transition: str) -> None:
    """Raise TransitionError if prd.status is not expected."""
    if prd.status is not expected:
        raise TransitionError(
            code="wrong_status",
            current_status=prd.status.value,
            message=(
                f"PRD transition '{transition}' requires status "
                f"'{expected.value}', got '{prd.status.value}'."
            ),
        )


def _assert_task_status(task: Task, expected: TaskStatus, transition: str) -> None:
    """Raise TransitionError if task.status is not expected."""
    if task.status is not expected:
        raise TransitionError(
            code="wrong_status",
            current_status=task.status.value,
            message=(
                f"Task transition '{transition}' requires status "
                f"'{expected.value}', got '{task.status.value}'."
            ),
        )


def _can_claim_task(task: Task, prd: PRD) -> None:
    """Gate: PRD must be reviewed or approved before a task can be claimed.

    Raises TransitionError with gate_name='prd_status_gate' on failure.
    """
    claimable_prd_statuses = {PRDStatus.reviewed, PRDStatus.approved}
    if prd.status not in claimable_prd_statuses:
        raise TransitionError(
            code="gate_failed",
            gate_name="prd_status_gate",
            current_status=task.status.value,
            message=(
                f"Task '{task.id}' cannot be claimed: PRD must be in "
                f"{{'reviewed', 'approved'}}, got '{prd.status.value}'. "
                "Review and approve the PRD before claiming tasks."
            ),
        )


def _can_review_task(task: Task) -> None:
    """Gate: task must have acceptance_criteria and verification.commands to be reviewed.

    Raises TransitionError with gate_name='readiness_gate' on failure.
    """
    missing: list[str] = []
    if not task.acceptance_criteria:
        missing.append("acceptance_criteria (must be non-empty)")
    if not task.verification.commands:
        missing.append("verification.commands (must be non-empty)")

    if missing:
        raise TransitionError(
            code="gate_failed",
            gate_name="readiness_gate",
            current_status=task.status.value,
            message=(
                f"Task '{task.id}' cannot move to 'reviewed': "
                + "; ".join(missing)
                + "."
            ),
        )


def _evidence_complete(task: Task, evidence: Evidence) -> None:
    """Gate: evidence must satisfy task.verification.required_evidence.

    Single source of truth: this gate delegates to
    :func:`fakoli_state.review.gates.evidence_complete`, the same predicate the
    ``apply`` command uses to preview the gate verdict for the reviewer. The
    enforcing transition and the advisory preview can therefore never diverge.

    Operating-model principle: this is fakoli-style **P1** (advisory and enforcing
    share one code path). See ``plugins/fakoli-style/docs/fakoli-style.md``.

    This previously used a raw, case-sensitive substring match against a
    flattened corpus of every Evidence field (commands_run, files_changed,
    output_excerpt, pr_url, commit_sha, screenshots). That logic both
    disagreed with the review gate ``apply`` shows the reviewer and was
    trivially satisfiable by free text — writing the literal required string
    into any field passed the gate. ``evidence_complete`` instead routes by
    intent (test requirements check for a real test runner and reject
    ``--collect-only``; PR requirements check ``pr_url`` with word-boundary
    matching; etc.).

    Raises TransitionError with gate_name='evidence_gate' on failure.
    """
    passed, missing = evidence_complete(task, evidence)
    if not passed:
        raise TransitionError(
            code="gate_failed",
            gate_name="evidence_gate",
            current_status=task.status.value,
            message=(
                f"Task '{task.id}' cannot be accepted: "
                f"required evidence not satisfied: {missing!r}. "
                "Submit evidence that covers all required_evidence items."
            ),
        )


# ---------------------------------------------------------------------------
# PRD transitions
# ---------------------------------------------------------------------------


def prd_draft_to_reviewed(prd: PRD, reviewer: str, now: datetime.datetime) -> PRD:
    """Transition PRD: draft → reviewed.

    Args:
        prd:      The current PRD (must be in 'draft' status).
        reviewer: Identity of the reviewer.
        now:      Current UTC timestamp.

    Returns:
        A new PRD instance with status='reviewed' and audit fields updated.

    Raises:
        TransitionError: If prd.status is not 'draft'.
    """
    _assert_prd_status(prd, PRDStatus.draft, "draft → reviewed")
    return prd.model_copy(
        update={
            "status": PRDStatus.reviewed,
            "last_reviewed_at": now,
            "last_reviewed_by": reviewer,
        }
    )


def prd_reviewed_to_approved(
    prd: PRD, approver: str, now: datetime.datetime
) -> PRD:
    """Transition PRD: reviewed → approved.

    Args:
        prd:      The current PRD (must be in 'reviewed' status).
        approver: Identity of the approver.
        now:      Current UTC timestamp.

    Returns:
        A new PRD instance with status='approved' and audit fields updated.

    Raises:
        TransitionError: If prd.status is not 'reviewed'.
    """
    _assert_prd_status(prd, PRDStatus.reviewed, "reviewed → approved")
    return prd.model_copy(
        update={
            "status": PRDStatus.approved,
            "last_reviewed_at": now,
            "last_reviewed_by": approver,
        }
    )


def prd_to_rejected(
    prd: PRD, reviewer: str, reason: str, now: datetime.datetime
) -> PRD:
    """Transition PRD: (draft | reviewed) → rejected.

    Args:
        prd:      The current PRD (may be 'draft' or 'reviewed').
        reviewer: Identity of the reviewer.
        reason:   Freeform rejection reason (stored in open_questions for audit).
        now:      Current UTC timestamp.

    Returns:
        A new PRD instance with status='rejected'.

    Raises:
        TransitionError: If prd.status is already 'approved' or 'rejected'.
    """
    if prd.status in {PRDStatus.approved, PRDStatus.rejected}:
        raise TransitionError(
            code="wrong_status",
            current_status=prd.status.value,
            message=(
                f"PRD cannot be rejected from status '{prd.status.value}'. "
                "Only 'draft' and 'reviewed' PRDs can be rejected."
            ),
        )
    updated_questions = list(prd.open_questions) + [f"Rejected by {reviewer}: {reason}"]
    return prd.model_copy(
        update={
            "status": PRDStatus.rejected,
            "last_reviewed_at": now,
            "last_reviewed_by": reviewer,
            "open_questions": updated_questions,
        }
    )


# ---------------------------------------------------------------------------
# Task transitions
# ---------------------------------------------------------------------------


def task_proposed_to_drafted(task: Task, now: datetime.datetime) -> Task:
    """Transition Task: proposed → drafted.

    Args:
        task: The current Task (must be in 'proposed' status).
        now:  Current UTC timestamp.

    Returns:
        A new Task instance with status='drafted' and updated_at set.

    Raises:
        TransitionError: If task.status is not 'proposed'.
    """
    _assert_task_status(task, TaskStatus.proposed, "proposed → drafted")
    return task.model_copy(update={"status": TaskStatus.drafted, "updated_at": now})


def task_drafted_to_reviewed(task: Task, now: datetime.datetime) -> Task:
    """Transition Task: drafted → reviewed.

    Gate: task.acceptance_criteria must be non-empty AND
          task.verification.commands must be non-empty.

    Args:
        task: The current Task (must be in 'drafted' status).
        now:  Current UTC timestamp.

    Returns:
        A new Task instance with status='reviewed'.

    Raises:
        TransitionError: If task.status is not 'drafted', or if the readiness
                         gate fails.
    """
    _assert_task_status(task, TaskStatus.drafted, "drafted → reviewed")
    _can_review_task(task)
    return task.model_copy(update={"status": TaskStatus.reviewed, "updated_at": now})


def task_reviewed_to_ready(task: Task, now: datetime.datetime) -> Task:
    """Transition Task: reviewed → ready.

    Args:
        task: The current Task (must be in 'reviewed' status).
        now:  Current UTC timestamp.

    Returns:
        A new Task instance with status='ready'.

    Raises:
        TransitionError: If task.status is not 'reviewed'.
    """
    _assert_task_status(task, TaskStatus.reviewed, "reviewed → ready")
    return task.model_copy(update={"status": TaskStatus.ready, "updated_at": now})


def task_ready_to_claimed(
    task: Task,
    claim: Claim,
    prd: PRD,
    now: datetime.datetime,
) -> Task:
    """Transition Task: ready → claimed.

    Gate: prd.status must be 'reviewed' or 'approved'.

    Args:
        task:  The current Task (must be in 'ready' status).
        claim: The Claim being created (used for context in error messages).
        prd:   The project PRD whose status gates claimability.
        now:   Current UTC timestamp.

    Returns:
        A new Task instance with status='claimed'.

    Raises:
        TransitionError: If task.status is not 'ready', or if the PRD status
                         gate fails.
    """
    _assert_task_status(task, TaskStatus.ready, "ready → claimed")
    _can_claim_task(task, prd)
    return task.model_copy(update={"status": TaskStatus.claimed, "updated_at": now})


def task_claimed_to_in_progress(task: Task, now: datetime.datetime) -> Task:
    """Transition Task: claimed → in_progress.

    Args:
        task: The current Task (must be in 'claimed' status).
        now:  Current UTC timestamp.

    Returns:
        A new Task instance with status='in_progress'.

    Raises:
        TransitionError: If task.status is not 'claimed'.
    """
    _assert_task_status(task, TaskStatus.claimed, "claimed → in_progress")
    return task.model_copy(update={"status": TaskStatus.in_progress, "updated_at": now})


def task_in_progress_to_blocked(
    task: Task, reason: str, now: datetime.datetime
) -> Task:
    """Transition Task: in_progress → blocked.

    Args:
        task:   The current Task (must be in 'in_progress' status).
        reason: Freeform explanation of why the task is blocked (appended to
                implementation_notes for audit).
        now:    Current UTC timestamp.

    Returns:
        A new Task instance with status='blocked'.

    Raises:
        TransitionError: If task.status is not 'in_progress'.
    """
    _assert_task_status(task, TaskStatus.in_progress, "in_progress → blocked")
    updated_notes = list(task.implementation_notes) + [f"Blocked: {reason}"]
    return task.model_copy(
        update={
            "status": TaskStatus.blocked,
            "implementation_notes": updated_notes,
            "updated_at": now,
        }
    )


def task_blocked_to_in_progress(task: Task, now: datetime.datetime) -> Task:
    """Transition Task: blocked → in_progress.

    Args:
        task: The current Task (must be in 'blocked' status).
        now:  Current UTC timestamp.

    Returns:
        A new Task instance with status='in_progress'.

    Raises:
        TransitionError: If task.status is not 'blocked'.
    """
    _assert_task_status(task, TaskStatus.blocked, "blocked → in_progress")
    return task.model_copy(update={"status": TaskStatus.in_progress, "updated_at": now})


def task_in_progress_to_needs_review(
    task: Task, evidence: Evidence, now: datetime.datetime
) -> Task:
    """Transition Task: in_progress → needs_review.

    Submitting evidence moves the task into the human/agent review queue.

    Args:
        task:     The current Task (must be in 'in_progress' status).
        evidence: The Evidence record just submitted. Stored for later gate checks.
        now:      Current UTC timestamp.

    Returns:
        A new Task instance with status='needs_review'.

    Raises:
        TransitionError: If task.status is not 'in_progress'.
    """
    _assert_task_status(task, TaskStatus.in_progress, "in_progress → needs_review")
    # Evidence is intentionally not embedded on Task — the Claims manager / backend
    # persists it separately. This transition only changes the task status.
    return task.model_copy(update={"status": TaskStatus.needs_review, "updated_at": now})


def task_needs_review_to_accepted(
    task: Task, reviewer: str, evidence: Evidence, now: datetime.datetime
) -> Task:
    """Transition Task: needs_review → accepted.

    Gate: all items in task.verification.required_evidence must be present in
    the evidence record.

    Args:
        task:     The current Task (must be in 'needs_review' status).
        reviewer: Identity of the reviewer accepting the task.
        evidence: The Evidence record to check against required_evidence.
        now:      Current UTC timestamp.

    Returns:
        A new Task instance with status='accepted'.

    Raises:
        TransitionError: If task.status is not 'needs_review', or if the
                         evidence gate fails.
    """
    _assert_task_status(task, TaskStatus.needs_review, "needs_review → accepted")
    _evidence_complete(task, evidence)
    updated_notes = list(task.implementation_notes) + [f"Accepted by {reviewer}"]
    return task.model_copy(
        update={
            "status": TaskStatus.accepted,
            "implementation_notes": updated_notes,
            "updated_at": now,
        }
    )


def task_accepted_to_done(task: Task, now: datetime.datetime) -> Task:
    """Transition Task: accepted → done.

    Args:
        task: The current Task (must be in 'accepted' status).
        now:  Current UTC timestamp.

    Returns:
        A new Task instance with status='done'.

    Raises:
        TransitionError: If task.status is not 'accepted'.
    """
    _assert_task_status(task, TaskStatus.accepted, "accepted → done")
    return task.model_copy(update={"status": TaskStatus.done, "updated_at": now})


def task_needs_review_to_rejected(
    task: Task, reviewer: str, reason: str, now: datetime.datetime
) -> Task:
    """Transition Task: needs_review → rejected.

    Args:
        task:     The current Task (must be in 'needs_review' status).
        reviewer: Identity of the reviewer rejecting the task.
        reason:   Freeform rejection reason (appended to implementation_notes).
        now:      Current UTC timestamp.

    Returns:
        A new Task instance with status='rejected'.

    Raises:
        TransitionError: If task.status is not 'needs_review'.
    """
    _assert_task_status(task, TaskStatus.needs_review, "needs_review → rejected")
    updated_notes = list(task.implementation_notes) + [
        f"Rejected by {reviewer}: {reason}"
    ]
    return task.model_copy(
        update={
            "status": TaskStatus.rejected,
            "implementation_notes": updated_notes,
            "updated_at": now,
        }
    )


def task_rejected_to_drafted(task: Task, now: datetime.datetime) -> Task:
    """Transition Task: rejected → drafted.

    Allows a rejected task to re-enter the review cycle after the agent revises it.

    Args:
        task: The current Task (must be in 'rejected' status).
        now:  Current UTC timestamp.

    Returns:
        A new Task instance with status='drafted'.

    Raises:
        TransitionError: If task.status is not 'rejected'.
    """
    _assert_task_status(task, TaskStatus.rejected, "rejected → drafted")
    return task.model_copy(update={"status": TaskStatus.drafted, "updated_at": now})


