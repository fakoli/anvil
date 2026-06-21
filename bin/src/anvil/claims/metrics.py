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

Scope: the governor gates the PULL seam (`anvil next` / ClaimManager.
next_claimable) only. A direct `anvil claim <id>` is intentionally NOT gated —
an agent that already knows a task id can claim it regardless. A governed fleet
loop must therefore PULL via `anvil next`, not claim by id. (Unifying the MCP
get_next_task advisory path onto this gate is a tracked follow-up.)
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
        # (task_id, decision, decision_dt) within the window.
        self._decisions: list[tuple[str, str, datetime.datetime]] | None = None
        # task_id -> sorted [(submitted_at, submitter)] so a decision can be
        # attributed to the submission that was actually current when it landed.
        self._submissions: dict[str, list[tuple[datetime.datetime, str]]] | None = None

    # -- lazy loaders -----------------------------------------------------

    def _load(self) -> None:
        if self._decisions is not None:
            return
        cutoff = self._clock.now() - self._window
        decisions: list[tuple[str, str, datetime.datetime]] = []
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
                decisions.append((task_id, decision, ts))
        self._decisions = decisions
        # task -> chronologically-sorted submissions, so under a rework cycle
        # (runner A rejected, runner B re-submits and is accepted) each decision
        # is credited to the runner whose work it actually reviewed — not the
        # task's latest submitter.
        submissions: dict[str, list[tuple[datetime.datetime, str]]] = {}
        for ev in self._backend.list_evidence():
            submissions.setdefault(ev.task_id, []).append(
                (ev.submitted_at, ev.submitted_by)
            )
        for subs in submissions.values():
            subs.sort(key=lambda pair: pair[0])
        self._submissions = submissions

    def _submitter_at(self, task_id: str, when: datetime.datetime) -> str | None:
        """The runner whose evidence submission was current when ``when``'s
        decision landed — the latest submission at or before the decision."""
        assert self._submissions is not None
        subs = self._submissions.get(task_id, [])
        chosen: str | None = None
        for submitted_at, submitter in subs:
            if submitted_at <= when:
                chosen = submitter
            else:
                break
        # Clock-skew fallback: a decision earlier than any recorded submission
        # is still attributed to the first (and only plausible) submitter.
        if chosen is None and subs:
            chosen = subs[0][1]
        return chosen

    # -- review debt ------------------------------------------------------

    def needs_review_depth(self) -> int:
        from anvil.state.models import TaskStatus

        return len(self._backend.list_tasks(status=TaskStatus.needs_review))

    def review_queue_saturated(self) -> bool:
        return self.needs_review_depth() >= self.needs_review_cap

    # -- accept rate ------------------------------------------------------

    def accept_rate(self, actor: str) -> float | None:
        """Fraction of *this runner's* reviewed submissions (in the window) that
        were accepted, or None if the runner has no reviewed history yet. Each
        decision is attributed to the runner whose submission it reviewed, so a
        reworked task's rejection stays with the runner who earned it."""
        self._load()
        assert self._decisions is not None
        accepted = total = 0
        for task_id, decision, when in self._decisions:
            if self._submitter_at(task_id, when) != actor:
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
            for tid, decision, _when in self._decisions
            if tid == task_id and decision == "rejected"
        )

    # -- gates ------------------------------------------------------------

    def withhold_reason(self, actor: str) -> str | None:
        """Why the governor would withhold new work from ``actor`` right now, or
        None if it wouldn't — so a caller can distinguish a governed withhold
        from a genuinely empty queue. Mirrors the gates in
        :meth:`ClaimManager.next_claimable`.
        """
        if self.review_queue_saturated():
            return "review_queue_saturated"
        if self.actor_below_floor(actor):
            return "actor_below_floor"
        return None

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
