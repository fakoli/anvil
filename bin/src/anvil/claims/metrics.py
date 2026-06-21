"""Accept-rate governor + review-debt metrics (B49).

The binding constraint on an unattended fleet is HUMAN review: routing the most
volume through the weakest executor under the least-proven gate can produce
"fast dumb work" that swamps the human's review queue. These metrics let the
pull seam (`anvil next`) refuse new work when:

  - the human review queue is saturated (needs_review depth >= a cap), or
  - the requesting runner's recent accept-rate is below a floor, or
  - a task has been rejected so many times it should ESCALATE to a proven actor
    (or a human) instead of recirculating to the same weak runner.

Everything is computed LIVE from engine state on each call (no persistence, no
async jobs). The accept-rate is per RUNNER (the task's evidence.submitted_by —
the actor who did the work), NOT per reviewer.
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from anvil.clock import Clock
    from anvil.state.backend import Backend

__all__ = ["AcceptRateMetrics"]

# Escalation policy (hard-coded for the MVP; promote to config if the B50
# bake-off shows these are wrong).
_ESCALATION_REJECT_THRESHOLD = 3
_ESCALATION_FLOOR = 0.95


class AcceptRateMetrics:
    """Live accept-rate / review-debt computation for the pull seam (B49)."""

    def __init__(
        self,
        backend: Backend,
        clock: Clock,
        *,
        window_days: float = 7.0,
        floor: float = 0.80,
        needs_review_cap: int = 10,
    ) -> None:
        self._backend = backend
        self._clock = clock
        self._window = datetime.timedelta(days=window_days)
        self.floor = floor
        self.needs_review_cap = needs_review_cap
        self._decisions: list[tuple[str, str]] | None = None  # (task_id, decision)
        self._work_actor: dict[str, str] | None = None  # task_id -> submitter

    # -- lazy loaders -----------------------------------------------------

    def _load(self) -> None:
        if self._decisions is not None:
            return
        cutoff = self._clock.now() - self._window
        decisions: list[tuple[str, str]] = []
        for task_id, decision, created_at_iso in (
            self._backend.list_task_review_decisions()
        ):
            try:
                ts = datetime.datetime.fromisoformat(created_at_iso)
            except ValueError:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=datetime.UTC)
            if ts >= cutoff:
                decisions.append((task_id, decision))
        self._decisions = decisions
        # task -> work actor (latest evidence submitter; list_evidence is id-asc
        # so the last write for a task wins).
        actor_by_task: dict[str, str] = {}
        for ev in self._backend.list_evidence():
            actor_by_task[ev.task_id] = ev.submitted_by
        self._work_actor = actor_by_task

    # -- review debt ------------------------------------------------------

    def needs_review_depth(self) -> int:
        from anvil.state.models import TaskStatus

        return len(self._backend.list_tasks(status=TaskStatus.needs_review))

    def review_queue_saturated(self) -> bool:
        return self.needs_review_depth() >= self.needs_review_cap

    # -- accept rate ------------------------------------------------------

    def accept_rate(self, actor: str) -> float | None:
        """Fraction of *this runner's* reviewed tasks (in the window) that were
        accepted, or None if the runner has no reviewed history yet."""
        self._load()
        assert self._decisions is not None and self._work_actor is not None
        accepted = total = 0
        for task_id, decision in self._decisions:
            if self._work_actor.get(task_id) != actor:
                continue
            total += 1
            if decision == "accepted":
                accepted += 1
        if total == 0:
            return None
        return accepted / total

    def rejection_count(self, task_id: str) -> int:
        self._load()
        assert self._decisions is not None
        return sum(
            1
            for tid, decision in self._decisions
            if tid == task_id and decision == "rejected"
        )

    # -- gates ------------------------------------------------------------

    def actor_below_floor(self, actor: str) -> bool:
        """True if the runner has a track record AND it is below the floor. A
        new runner (no history) gets the benefit of the doubt for base work."""
        rate = self.accept_rate(actor)
        return rate is not None and rate < self.floor

    def required_floor(self, task_id: str) -> float:
        """The accept-rate a runner must meet to claim this task — escalated
        once it has been rejected >= the threshold, so a chronically-rejected
        task goes to a proven actor (or a human) instead of recirculating."""
        if self.rejection_count(task_id) >= _ESCALATION_REJECT_THRESHOLD:
            return _ESCALATION_FLOOR
        return self.floor

    def task_blocked_for_actor(self, task_id: str, actor: str) -> bool:
        """True if this task's (possibly escalated) required floor exceeds the
        runner's proven accept-rate.

        A new runner (no history) may take base-floor tasks but NOT escalated
        ones — an escalated task must go to a *proven* high-accept-rate runner.
        """
        required = self.required_floor(task_id)
        rate = self.accept_rate(actor)
        if required > self.floor:  # escalated: must be proven at/above the floor
            return rate is None or rate < required
        return rate is not None and rate < required
