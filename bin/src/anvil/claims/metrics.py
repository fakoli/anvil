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

Scope: the governor gates the PULL seams (`anvil next`, MCP `get_next_task`, and
ClaimManager.next_claimable) only. A direct `anvil claim <id>` is intentionally
NOT gated — an agent that already knows a task id can claim it regardless. A
governed fleet loop must therefore pull through one of the offer surfaces.
"""

from __future__ import annotations

import datetime
import math
from fractions import Fraction
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
        as_of: datetime.datetime | None = None,
    ) -> None:
        if not math.isfinite(window_days) or window_days <= 0:
            raise ValueError("accept-rate window_days must be finite and positive")
        if not math.isfinite(floor) or not 0 <= floor <= 1:
            raise ValueError("accept-rate floor must be between 0 and 1")
        if needs_review_cap < 0:
            raise ValueError("needs_review_cap must be non-negative")
        observed_at = as_of if as_of is not None else clock.now()
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("accept-rate as_of must be timezone-aware")
        self._backend = backend
        self._clock = clock
        self._window = datetime.timedelta(days=window_days)
        self.window_days = window_days
        self.as_of = observed_at.astimezone(datetime.UTC)
        self._floor_fraction = Fraction(str(floor))
        self.floor = float(self._floor_fraction)
        self.needs_review_cap = needs_review_cap
        # (task_id, decision, decision_dt, stable review/event id) in window.
        self._decisions: list[tuple[str, str, datetime.datetime, str]] | None = None
        # task_id -> sorted [(submitted_at, evidence id, submitter)] so a decision can be
        # attributed to the submission that was actually current when it landed.
        self._submissions: dict[
            str, list[tuple[datetime.datetime, str, str]]
        ] | None = None

    # -- lazy loaders -----------------------------------------------------

    def _load(self) -> None:
        if self._decisions is not None:
            return
        cutoff = self.as_of - self._window
        decisions: list[tuple[str, str, datetime.datetime, str]] = []
        for index, raw in enumerate(self._backend.list_task_review_decisions()):
            if len(raw) == 4:
                task_id, decision, created_at_iso, event_id = raw
            else:  # compatibility for small test/provider stubs
                task_id, decision, created_at_iso = raw
                event_id = f"legacy-{index:020d}"
            try:
                ts = datetime.datetime.fromisoformat(created_at_iso)
            except ValueError:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=datetime.UTC)
            ts = ts.astimezone(datetime.UTC)
            if cutoff <= ts <= self.as_of:
                decisions.append((task_id, decision, ts, event_id))
        decisions.sort(key=lambda item: (item[2], item[3]))
        self._decisions = decisions
        # task -> chronologically-sorted submissions, so under a rework cycle
        # (runner A rejected, runner B re-submits and is accepted) each decision
        # is credited to the runner whose work it actually reviewed — not the
        # task's latest submitter.
        submissions: dict[str, list[tuple[datetime.datetime, str, str]]] = {}
        for index, ev in enumerate(self._backend.list_evidence()):
            submissions.setdefault(ev.task_id, []).append(
                (
                    ev.submitted_at.astimezone(datetime.UTC),
                    getattr(ev, "id", f"legacy-{index:020d}"),
                    ev.submitted_by,
                )
            )
        for subs in submissions.values():
            subs.sort(key=lambda item: (item[0], item[1]))
        self._submissions = submissions

    def _submitter_at(self, task_id: str, when: datetime.datetime) -> str | None:
        """The runner whose evidence submission was current when ``when``'s
        decision landed — the latest submission at or before the decision."""
        assert self._submissions is not None
        subs = self._submissions.get(task_id, [])
        chosen: str | None = None
        for submitted_at, _evidence_id, submitter in subs:
            if submitted_at <= when:
                chosen = submitter
            else:
                break
        # Clock-skew fallback: a decision earlier than any recorded submission
        # is still attributed to the first (and only plausible) submitter.
        if chosen is None and subs:
            chosen = subs[0][2]
        return chosen

    # -- review debt ------------------------------------------------------

    def needs_review_depth(self) -> int:
        from anvil.state.models import TaskStatus

        return len(self._backend.list_tasks(status=TaskStatus.needs_review))

    def review_queue_saturated(self) -> bool:
        return self.needs_review_depth() >= self.needs_review_cap

    # -- accept rate ------------------------------------------------------

    def acceptance_counts(self, actor: str) -> tuple[int, int]:
        """Return exact accepted/total finalized counting attempts for actor."""
        self._load()
        assert self._decisions is not None
        accepted = total = 0
        for task_id, decision, when, _event_id in self._decisions:
            if self._submitter_at(task_id, when) != actor:
                continue
            total += 1
            if decision == "accepted":
                accepted += 1
        return accepted, total

    def accept_rate(self, actor: str) -> float | None:
        """Fraction of *this runner's* reviewed submissions (in the window) that
        were accepted, or None if the runner has no reviewed history yet. Each
        decision is attributed to the runner whose submission it reviewed, so a
        reworked task's rejection stays with the runner who earned it."""
        accepted, total = self.acceptance_counts(actor)
        if total == 0:
            return None
        return accepted / total

    def rejection_count(self, task_id: str) -> int:
        self._load()
        assert self._decisions is not None
        return sum(
            1
            for tid, decision, _when, _event_id in self._decisions
            if tid == task_id and decision == "rejected"
        )

    def projection(self, actor: str, *, task_id: str | None = None) -> dict[str, object]:
        """Return the complete, deterministic public governor calculation."""
        numerator, denominator = self.acceptance_counts(actor)
        rate = numerator / denominator if denominator else None
        floor = self.required_floor(task_id) if task_id is not None else self.floor
        return {
            "as_of": self.as_of.isoformat().replace("+00:00", "Z"),
            "window_days": self.window_days,
            "window_start": (self.as_of - self._window)
            .isoformat()
            .replace("+00:00", "Z"),
            "numerator": numerator,
            "denominator": denominator,
            "rate": rate,
            "floor": floor,
            "configured_floor": self.floor,
            "needs_review_depth": self.needs_review_depth(),
            "needs_review_cap": self.needs_review_cap,
            "guidance": (
                "Clearing the review queue alone does not restore offer eligibility. "
                "Eligibility recovers through accepted finalized reviews, expiry of "
                "older reviews from the configured window, or a configured floor "
                "change. A direct claim of a known task ID bypasses only offer "
                "throttling; ownership, conflict, PRD, risk, and evidence gates "
                "remain enforced."
            ),
        }

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
        numerator, denominator = self.acceptance_counts(actor)
        if denominator == 0:
            return False
        return Fraction(numerator, denominator) < self._floor_fraction

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
        numerator, denominator = self.acceptance_counts(actor)
        if required > self.floor:  # escalated: must be proven at/above the floor
            return denominator == 0 or Fraction(numerator, denominator) < Fraction(
                str(required)
            )
        return denominator != 0 and Fraction(numerator, denominator) < Fraction(
            str(required)
        )
