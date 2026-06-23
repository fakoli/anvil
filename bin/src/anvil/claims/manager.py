"""ClaimManager — stateless orchestrator for the claim/lock/lease primitive.

Design contract
---------------
ClaimManager produces EventDrafts; the Backend's _check_claim_*/_write_claim_*
phases do the actual mutation atomically via append().

This module never calls datetime.now() directly. All timestamps flow through
self._clock.now() so every time-sensitive code path is deterministically testable
with FrozenClock without monkey-patching or sleep().

Event actions emitted by this module (welder maps these to SQL handlers):
  claim.created   — new active claim; task moves ready → claimed
  claim.released  — voluntary or forced release; task moves claimed → ready
  claim.renewed   — heartbeat; lease_expires_at extended, last_heartbeat_at updated
  claim.stale     — lease expired; task moves claimed/in_progress → stale → ready

Payload shapes are documented in each method's docstring so welder can implement
the SQL handlers against a stable contract.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from anvil.clock import Clock
from anvil.state import transitions
from anvil.state.backend import BackendError
from anvil.state.models import (
    Claim,
    ClaimStatus,
    ClaimType,
    EventDraft,
    Task,
    TaskPriority,
    TaskStatus,
)

if TYPE_CHECKING:
    from anvil.claims.metrics import AcceptRateMetrics
    from anvil.state.backend import Backend

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Priority ordering for next_claimable() — higher number = higher priority.
# ---------------------------------------------------------------------------

_PRIORITY_ORDER: dict[str, int] = {
    TaskPriority.critical: 4,
    TaskPriority.high: 3,
    TaskPriority.medium: 2,
    TaskPriority.low: 1,
}


# ---------------------------------------------------------------------------
# Public data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClaimResult:
    """Result of a successful claim() — what the agent needs to start work."""

    claim: Claim
    task: Task
    branch: str | None  # set by git_ops integration; None at this layer
    worktree_path: str | None


@dataclass(frozen=True)
class ConflictWarning:
    """Returned by check_conflicts when a proposed claim overlaps with an active one."""

    other_claim_id: str
    other_actor: str
    overlapping_files: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ClaimError(Exception):
    """Base for claim-flow errors (gate failures, missing tasks, etc.)."""


# ---------------------------------------------------------------------------
# ClaimManager
# ---------------------------------------------------------------------------


class ClaimManager:
    """Stateless orchestrator for claim/lock/lease operations.

    Takes a Backend + Clock + actor identity. Issues events to the Backend;
    the Backend's _check_claim_*/_write_claim_* phases do the actual SQLite
    mutation atomically inside append().

    Args:
        backend:                  Backend instance (SQLite or in-memory).
        clock:                    Clock instance for all timestamp generation.
        actor:                    Identity string for this agent (e.g. "agent-guido").
        default_lease_minutes:    How many minutes a fresh claim lease lasts.
                                  Accepts a float so sub-minute leases work
                                  (e.g. 0.5 → a 30-second lease); the lease is
                                  computed via timedelta(minutes=value).
        default_heartbeat_minutes: Not used directly; present for future rate-limiting.
    """

    # B46 — default hard max-claim-age as a multiple of the base lease when no
    # explicit ``max_claim_age_minutes`` is supplied. So the cap applies even
    # for projects without a config.yaml.
    DEFAULT_MAX_CLAIM_AGE_MULTIPLIER = 4.0

    def __init__(
        self,
        backend: Backend,
        clock: Clock,
        *,
        actor: str,
        default_lease_minutes: float = 60,
        default_heartbeat_minutes: float = 5,
        max_claim_age_minutes: float | None = None,
    ) -> None:
        self._backend = backend
        self._clock = clock
        self._actor = actor
        self._default_lease = default_lease_minutes
        self._default_heartbeat = default_heartbeat_minutes
        # B46: a wedged agent that keeps heartbeating must still lose its claim
        # eventually. ``renew()`` refuses once a claim is older than this, so the
        # lease then expires and the stale reaper takes it. None -> 4x the lease.
        self._max_claim_age_minutes = (
            max_claim_age_minutes
            if max_claim_age_minutes is not None
            else default_lease_minutes * self.DEFAULT_MAX_CLAIM_AGE_MULTIPLIER
        )

    # ------------------------------------------------------------------
    # Main flow
    # ------------------------------------------------------------------

    def next_claimable(
        self,
        *,
        task_type: str | None = None,
        max_blast: int | None = None,
        max_review_risk: int | None = None,
        metrics: AcceptRateMetrics | None = None,
        prd_id: str | None = None,
    ) -> Task | None:
        """Pick the highest-priority claimable Task.

        Ordering: priority desc (critical > high > medium > low),
                  then complexity asc (lower score = simpler = first),
                  then created_at asc (oldest first for fairness).

        Filters out tasks that:
          - are not in 'ready' status
          - have unmet dependencies (any dependency not in 'done' status)
          - have an active claim (any claim with status='active') by any actor
          - belong to a conflict_group that already has an active claim

        ``task_type`` (T015): when given, restrict the candidate pool to that
        type (feature / bugfix / refactor / modify). Omitting it keeps the
        pre-T015 behaviour (all types eligible).

        ``max_blast`` / ``max_review_risk`` (B45): risk-axis ceilings for a
        low-risk runner. SAFE-BY-CONSTRUCTION — when a ceiling is given, a task
        is eligible only if that dimension is CONFIRMED (human/LLM, not the
        filename-regex heuristic) AND scored at or below the ceiling. An
        unscored, unconfirmed, or over-ceiling task is treated as frontier-only
        (ineligible), so the filter fails safe rather than routing weakly-scored
        risk to a local runner. Omitting both keeps the pre-B45 behaviour.

        ``prd_id`` (T019): when given, restrict the *candidate pool* (the final
        ready tasks we might return) to that PRD partition. The EXCLUSION sets
        — active claims, done-dependency set, and active conflict groups — are
        ALWAYS built from ALL PRDs first, then the candidates are narrowed.
        Coordination is cross-PRD: a ``--prd v0.1`` pick must still skip a v0.1
        task whose conflict_group is held by an active v0.2 claim. Omitting it
        keeps the pre-T019 behaviour (all PRDs eligible as candidates).

        Returns None if no task is claimable.
        """
        ready_tasks = self._backend.list_tasks(
            status=TaskStatus.ready, task_type=task_type, prd_id=prd_id
        )
        if not ready_tasks:
            return None

        # B49 — accept-rate governor: refuse new work when the human review
        # queue is saturated, or when THIS runner's recent accept-rate is below
        # the floor (it stops pulling until its already-submitted work clears
        # review). A runner with no track record yet is given the benefit of the
        # doubt for base-floor work (see AcceptRateMetrics).
        if metrics is not None:
            if metrics.review_queue_saturated():
                return None
            if metrics.actor_below_floor(self._actor):
                return None

        active_claims = self._backend.list_active_claims()
        claimed_task_ids: set[str] = {c.task_id for c in active_claims}

        # Build a set of all task_ids that are done, for dep checking.
        all_tasks = self._backend.list_tasks()
        done_task_ids: set[str] = {t.id for t in all_tasks if t.status == TaskStatus.done}

        # Build a set of conflict-group IDs that have an active claim, for
        # group-level conflict detection.
        active_conflict_groups: set[str] = set()
        for t in all_tasks:
            if t.id in claimed_task_ids:
                for cg_id in t.conflict_groups:
                    active_conflict_groups.add(cg_id)

        candidates: list[Task] = []
        for task in ready_tasks:
            # Skip tasks that are directly claimed.
            if task.id in claimed_task_ids:
                continue

            # Skip tasks with unmet dependencies.
            if any(dep_id not in done_task_ids for dep_id in task.dependencies):
                continue

            # Skip tasks whose conflict_group already has an active claim.
            if any(cg_id in active_conflict_groups for cg_id in task.conflict_groups):
                continue

            # B45 — risk-axis ceilings, safe-by-construction. A ceilinged caller
            # gets a task only if the dimension is CONFIRMED and within the
            # ceiling; unscored / unconfirmed / over-ceiling are frontier-only.
            if max_blast is not None:
                blast = task.scores.blast_radius
                if (
                    blast is None
                    or blast > max_blast
                    or not task.scores.blast_radius_confirmed
                ):
                    continue
            if max_review_risk is not None:
                risk = task.scores.review_risk
                if (
                    risk is None
                    or risk > max_review_risk
                    or not task.scores.review_risk_confirmed
                ):
                    continue

            # B49 — escalate a chronically-rejected task past a runner whose
            # accept-rate doesn't meet the (raised) bar, so it goes to a proven
            # actor or a human instead of recirculating to the same weak runner.
            if metrics is not None and metrics.task_blocked_for_actor(
                task.id, self._actor
            ):
                continue

            candidates.append(task)

        if not candidates:
            return None

        def _sort_key(t: Task) -> tuple[int, int, datetime.datetime]:
            priority_rank = _PRIORITY_ORDER.get(t.priority, 0)
            # Complexity: lower score = simpler = preferred. None → treat as 6 (worst).
            complexity = t.scores.complexity if t.scores.complexity is not None else 6
            return (-priority_rank, complexity, t.created_at)

        candidates.sort(key=_sort_key)
        return candidates[0]

    def next_ready_excluding_active_files(
        self, *, prd_id: str | None = None
    ) -> Task | None:
        """Pick the next claimable Task, also excluding file-conflict overlaps.

        Identical to :meth:`next_claimable` (priority desc, complexity asc,
        created_at asc; respecting status, dependencies, active claims, and
        conflict_groups) with ONE additional exclusion: a candidate is skipped
        when its declared ``likely_files`` overlap the ``expected_files`` of any
        active claim held by another actor. (``Task`` carries only the
        planner-populated ``likely_files`` hint; the concrete ``expected_files``
        list lives on a ``Claim`` and does not exist until the task is claimed.)

        This is the helper behind the ``next_ready`` field returned by the
        finish/submit surfaces (T014): after an agent finishes task A, the
        named "next ready" task must never be one whose files collide with
        work another agent is already holding a claim on.

        ``next_claimable`` deliberately keeps its narrower contract (it does
        not file-exclude) because the ``next`` command pairs with a follow-up
        ``check_conflicts`` / ``claim --force`` flow; this method is the
        stricter variant used where we surface a single safe suggestion.

        ``prd_id`` (T019): scopes the CANDIDATE pool to one PRD partition while
        the exclusion sets (active claims, locked files, done-deps, conflict
        groups) still span ALL PRDs — same cross-PRD coordination contract as
        :meth:`next_claimable`. Omitting it keeps the all-PRDs behaviour.

        Returns None if no task is claimable.
        """
        base = self.next_claimable(prd_id=prd_id)
        if base is None:
            return None

        active_claims = self._backend.list_active_claims()
        # Files locked by an active claim, keyed by owning actor so we can skip
        # our own claims (re-suggesting work we already hold is not a conflict).
        # Spans ALL PRDs: a foreign lock in another partition still excludes a
        # candidate in the requested one (T019 cross-PRD coordination).
        locked_by_others: set[str] = set()
        for claim in active_claims:
            if claim.claimed_by == self._actor:
                continue
            locked_by_others.update(claim.expected_files)

        if not locked_by_others:
            # No foreign locks at all — the base pick is already safe.
            return base

        # Re-run the candidate scan so we can fall through to the next-best
        # task when the top pick collides. Mirrors next_claimable's filters.
        # The candidate pool is prd_id-scoped; the exclusion sets below are not.
        ready_tasks = self._backend.list_tasks(
            status=TaskStatus.ready, prd_id=prd_id
        )
        if not ready_tasks:
            return None

        claimed_task_ids: set[str] = {c.task_id for c in active_claims}
        all_tasks = self._backend.list_tasks()
        done_task_ids: set[str] = {
            t.id for t in all_tasks if t.status == TaskStatus.done
        }
        active_conflict_groups: set[str] = set()
        for t in all_tasks:
            if t.id in claimed_task_ids:
                for cg_id in t.conflict_groups:
                    active_conflict_groups.add(cg_id)

        candidates: list[Task] = []
        for task in ready_tasks:
            if task.id in claimed_task_ids:
                continue
            if any(dep_id not in done_task_ids for dep_id in task.dependencies):
                continue
            if any(cg_id in active_conflict_groups for cg_id in task.conflict_groups):
                continue
            if set(task.likely_files) & locked_by_others:
                continue
            candidates.append(task)

        if not candidates:
            return None

        def _sort_key(t: Task) -> tuple[int, int, datetime.datetime]:
            priority_rank = _PRIORITY_ORDER.get(t.priority, 0)
            complexity = t.scores.complexity if t.scores.complexity is not None else 6
            return (-priority_rank, complexity, t.created_at)

        candidates.sort(key=_sort_key)
        return candidates[0]

    def claim(
        self,
        task_id: str,
        *,
        expected_files: list[str] | None = None,
        claim_type: ClaimType = ClaimType.task,
        force: bool = False,
        branch: str | None = None,
    ) -> ClaimResult:
        """Atomically claim a task.

        Gates (in order):
        1. Task must exist; raises ClaimError if not.
        2. Task.status must be 'ready'; raises ClaimError with current status.
        3. PRD must be reviewed or approved (via transitions.task_ready_to_claimed).
        4. Active claims by OTHER actors with overlapping expected_files:
           - force=False: raise ClaimError with ConflictWarning details
           - force=True: log warning and proceed
        5. Any task in same conflict_group already claimed: same warn/force pattern.

        On success:
          Emits claim.created event + task.status_changed event (ready → claimed).
          Returns ClaimResult with the new Claim. ``branch`` is None unless a
          caller-supplied branch name is passed (T027), in which case it is
          recorded on the Claim and persisted to the claims row.

        Event payloads (for welder's SQL handlers):

        claim.created payload_json:
          {
            "id": str,                  # C### or UUID fallback
            "task_id": str,
            "claimed_by": str,
            "claim_type": str,          # ClaimType value
            "status": "active",
            "branch": null,
            "worktree_path": null,
            "expected_files": list[str],
            "created_at": str,          # ISO 8601 UTC
            "lease_expires_at": str,    # ISO 8601 UTC
            "last_heartbeat_at": str,   # ISO 8601 UTC
            "released_at": null,
            "release_reason": null,
          }

        task.status_changed payload_json:
          {
            "task_id": str,
            "from": "ready",
            "to": "claimed",
            "reason": str | null,
          }

        Args:
            task_id:        ID of the Task to claim.
            expected_files: Files this claim intends to modify (used for conflict detection).
            claim_type:     Type of claim (default: ClaimType.task).
            force:          If True, proceed despite file-overlap or group conflicts.
            branch:         Optional caller-supplied branch name to record on the
                            claim (T027). When None (default) the claim carries no
                            branch at this layer — git_ops/the CLI may attach an
                            auto-generated branch separately.

        Returns:
            ClaimResult with the new Claim and the updated Task.

        Raises:
            ClaimError: If any gate fails (task not found, wrong status, PRD gate,
                        or conflict with force=False).
        """
        files: list[str] = expected_files or []

        # Gate 1: task must exist.
        task = self._backend.get_task(task_id)
        if task is None:
            raise ClaimError(f"Task '{task_id}' not found.")

        # Gate 2: task must be ready.
        if task.status != TaskStatus.ready:
            raise ClaimError(
                f"Task '{task_id}' cannot be claimed: status is '{task.status}', "
                "expected 'ready'."
            )

        # Gate 3: the task's OWNING PRD must be reviewed or approved (T011).
        # Resolve via task.prd_id so a task in an approved PRD is claimable while
        # a sibling in a draft PRD is refused; the transition gate stays pure.
        prd = self._backend.get_prd_for_task(task)
        if prd is None:
            raise ClaimError(
                f"Task '{task_id}' cannot be claimed: no PRD found. "
                "Parse and review the PRD before claiming tasks."
            )
        now = self._clock.now()
        # Build a temporary Claim shell for the transition gate check.
        # The transition only uses claim for context in error messages; the
        # actual Claim model is constructed after all gates pass.
        _temp_claim = self._build_claim_model(
            claim_id="__probe__",
            task_id=task_id,
            expected_files=files,
            claim_type=claim_type,
            now=now,
        )
        try:
            transitions.task_ready_to_claimed(task, _temp_claim, prd, now)
        except transitions.TransitionError as exc:
            raise ClaimError(str(exc)) from exc

        # Gate 4 + 5: file overlap and conflict_group checks.
        conflicts = self.check_conflicts(task_id, files)
        if conflicts:
            if not force:
                conflict_summary = "; ".join(
                    f"claim {c.other_claim_id} by {c.other_actor} "
                    f"(files: {c.overlapping_files})"
                    for c in conflicts
                )
                raise ClaimError(
                    f"Task '{task_id}' conflicts with active claims: {conflict_summary}. "
                    "Use force=True to override."
                )
            for conflict in conflicts:
                logger.warning(
                    "Forced claim on task %r: conflict with claim %r by %r "
                    "(overlapping files: %r)",
                    task_id,
                    conflict.other_claim_id,
                    conflict.other_actor,
                    conflict.overlapping_files,
                )

        # Conflict_group check for tasks in the same group.
        group_conflicts = self._check_group_conflicts(task)
        if group_conflicts:
            group_summary = "; ".join(
                f"task {gc_task_id} claimed by {gc_actor}"
                for gc_task_id, gc_actor in group_conflicts
            )
            if not force:
                raise ClaimError(
                    f"Task '{task_id}' shares a conflict_group with already-claimed "
                    f"tasks: {group_summary}. Use force=True to override."
                )
            for gc_task_id, gc_actor in group_conflicts:
                logger.warning(
                    "Forced claim on task %r: conflict_group overlap with task %r "
                    "claimed by %r",
                    task_id,
                    gc_task_id,
                    gc_actor,
                )

        # All gates passed — generate IDs and timestamps.
        claim_id = self._generate_claim_id()
        claim = self._build_claim_model(
            claim_id=claim_id,
            task_id=task_id,
            expected_files=files,
            claim_type=claim_type,
            now=now,
            branch=branch,
        )

        # Emit claim.created event.
        #
        # TOCTOU fix: the pre-check above (check_conflicts / _check_group_conflicts)
        # runs OUTSIDE the atomic claim transaction, so two concurrent claims on
        # different task rows with overlapping expected_files can both pass it and
        # both reach append(). The authoritative re-check now runs INSIDE
        # _check_claim_created (the claim.created write-spec validation), guarded
        # by the same flock + BEGIN IMMEDIATE that writes the claim row. We thread
        # the force flag through the payload so `claim --force` still overrides
        # the in-transaction overlap rejection exactly like it overrides the
        # pre-check. force is NOT a field of the Claim model, so we inject it into
        # the payload dict directly.
        claim_payload = claim.model_dump(mode="json")
        claim_payload["force"] = force
        claim_draft = EventDraft(
            timestamp=now,
            actor=self._actor,
            action="claim.created",
            target_kind="claim",
            target_id=claim_id,
            payload_json=claim_payload,
        )
        try:
            self._backend.append(claim_draft)
        except BackendError as exc:
            # The in-transaction guard (status race OR expected_files overlap
            # race) rejected this claim — a concurrent claim won. Surface it as
            # the engine's rejection error so the CLI/MCP layers report a clean
            # "lost the race" failure instead of leaking a backend exception.
            raise ClaimError(str(exc)) from exc

        # DO NOT emit a separate task.status_changed event here.
        # _handle_claim_created already transitions the task to 'claimed'
        # atomically with the INSERT INTO claims, so a second event is
        # always either (a) an idempotent no-op (the WHERE status='ready'
        # guard matches 0 rows on the second go) producing 25-30% audit
        # noise, or (b) a real race condition during concurrent operation.
        # Critic-2 + Critic-3 flagged this on PR #41 — same fix pattern
        # as release().

        return ClaimResult(
            claim=claim,
            task=task,
            branch=branch,
            worktree_path=None,
        )

    def release(
        self,
        claim_id: str,
        *,
        force: bool = False,
        reason: str | None = None,
    ) -> None:
        """Release a claim, returning the task to 'ready'.

        Without force: claim's actor must match self._actor; claim.status
                       must be 'active'.
        With force:    any actor can release; claim.status can be any
                       non-terminal status (active, stale).

        Emits claim.released event + task.status_changed event (claimed → ready).

        Event payloads (for welder's SQL handlers):

        claim.released payload_json:
          {
            "claim_id": str,
            "released_by": str,
            "released_at": str,   # ISO 8601 UTC
            "release_reason": str | null,
            "force": bool,
          }

        task.status_changed payload_json:
          {
            "task_id": str,
            "from": str,          # actual current task status (claimed, in_progress, etc.)
            "to": "ready",
            "reason": str | null,
          }

        Args:
            claim_id: ID of the Claim to release.
            force:    If True, bypass actor-ownership and allow non-active status.
            reason:   Optional human-readable reason for the release.

        Raises:
            ClaimError: If claim not found, actor mismatch (without force), or
                        claim already in a terminal status.
        """
        claim = self._backend.get_claim(claim_id)
        if claim is None:
            raise ClaimError(f"Claim '{claim_id}' not found.")

        terminal_statuses = {ClaimStatus.released, ClaimStatus.force_released}

        if force:
            if claim.status in terminal_statuses:
                raise ClaimError(
                    f"Claim '{claim_id}' is already in terminal status "
                    f"'{claim.status}'; cannot release."
                )
        else:
            if claim.claimed_by != self._actor:
                raise ClaimError(
                    f"Claim '{claim_id}' belongs to '{claim.claimed_by}', "
                    f"not '{self._actor}'. Use force=True to release another actor's claim."
                )
            if claim.status != ClaimStatus.active:
                raise ClaimError(
                    f"Claim '{claim_id}' has status '{claim.status}', "
                    "expected 'active'. Use force=True to release non-active claims."
                )

        task = self._backend.get_task(claim.task_id)
        now = self._clock.now()

        # Emit claim.released event.
        # append() may return None for an idempotent no-op (already-released
        # claim) — this is a legal outcome, not an error. The claim was
        # already released, so the task state is already correct.
        release_draft = EventDraft(
            timestamp=now,
            actor=self._actor,
            action="claim.released",
            target_kind="claim",
            target_id=claim_id,
            payload_json={
                "claim_id": claim_id,
                "released_by": self._actor,
                "released_at": now.isoformat(),
                "release_reason": reason,
                "force": force,
            },
        )
        self._backend.append(release_draft)

        # DO NOT emit a separate task.status_changed event here.
        # _handle_claim_released already transitions the task back to 'ready'
        # atomically with the claim status update — emitting a second event
        # with from=task.status (read BEFORE the handler ran) would either
        # (a) hit the idempotent no-op path (audit noise; 25-30% of events
        # in a busy workflow are redundant), OR (b) for tasks already at
        # needs_review (mid-evidence-submission), silently reset them to
        # ready, destroying the link to the submitted Evidence row.
        # Critic-3 + Critic-2 both flagged this on PR #41.
        _ = task  # retained in signature for future audit/event payload use

    def renew(self, claim_id: str) -> Claim:
        """Heartbeat a claim — extend the lease and record last_heartbeat_at.

        Gates:
          - Claim must exist.
          - Current actor must own the claim.
          - Claim must be active (not stale, released, or force_released).
          - Lease must not already be expired (raise ClaimError — caller should re-claim).

        Emits claim.renewed event. Returns the updated Claim as a model
        (the Backend's handler persists the change; we return the locally-updated
        version for the caller's convenience).

        Event payload (for welder's SQL handler):

        claim.renewed payload_json:
          {
            "claim_id": str,
            "renewed_by": str,
            "last_heartbeat_at": str,   # ISO 8601 UTC (= now)
            "lease_expires_at": str,    # ISO 8601 UTC (= now + default_lease_minutes)
          }

        Args:
            claim_id: ID of the Claim to renew.

        Returns:
            The locally-updated Claim model (status, lease, heartbeat updated).

        Raises:
            ClaimError: If claim not found, actor mismatch, non-active status,
                        or lease already expired.
        """
        claim = self._backend.get_claim(claim_id)
        if claim is None:
            raise ClaimError(f"Claim '{claim_id}' not found.")

        if claim.claimed_by != self._actor:
            raise ClaimError(
                f"Claim '{claim_id}' belongs to '{claim.claimed_by}', "
                f"not '{self._actor}'. Only the owning actor can renew a claim."
            )

        if claim.status != ClaimStatus.active:
            raise ClaimError(
                f"Claim '{claim_id}' has status '{claim.status}'; "
                "only active claims can be renewed."
            )

        now = self._clock.now()

        if claim.lease_expires_at < now:
            raise ClaimError(
                f"Claim '{claim_id}' lease expired at "
                f"{claim.lease_expires_at.isoformat()} "
                f"(now: {now.isoformat()}). "
                "The lease has already expired; please re-claim the task."
            )

        # B46 — hard max-claim-age cutoff. A wedged agent that keeps heartbeating
        # must still lose its claim: once the claim is older than its max age,
        # renewal is refused regardless of progress, so the lease expires and the
        # stale reaper takes it. This bounds how long one stuck runner can hold a
        # task (and its conflict group) to max_claim_age, not forever.
        max_age_deadline = claim.created_at + datetime.timedelta(
            minutes=self._max_claim_age_minutes
        )
        if now >= max_age_deadline:
            raise ClaimError(
                f"Claim '{claim_id}' has exceeded its max age of "
                f"{self._max_claim_age_minutes:g} min "
                f"(created {claim.created_at.isoformat()}, now {now.isoformat()}); "
                "renewal refused. Release and re-claim to continue. This guard "
                "stops a stuck agent from holding a lease forever."
            )

        new_expires = now + datetime.timedelta(minutes=self._default_lease)

        # Emit claim.renewed event.
        # append() may return None for an idempotent no-op (already-renewed
        # with same expiry) — treat as success; the lease is already extended.
        renew_draft = EventDraft(
            timestamp=now,
            actor=self._actor,
            action="claim.renewed",
            target_kind="claim",
            target_id=claim_id,
            payload_json={
                "claim_id": claim_id,
                "renewed_by": self._actor,
                "last_heartbeat_at": now.isoformat(),
                "lease_expires_at": new_expires.isoformat(),
            },
        )
        self._backend.append(renew_draft)

        # Return the locally-updated Claim model so callers don't need an
        # extra get_claim() round-trip.
        return claim.model_copy(
            update={
                "last_heartbeat_at": now,
                "lease_expires_at": new_expires,
            }
        )

    def check_conflicts(
        self, task_id: str, expected_files: list[str]
    ) -> list[ConflictWarning]:
        """Pre-claim conflict check — returns warnings without mutating state.

        Used both by claim() internally and by the CLI to surface warnings
        before the user runs claim --force.

        A conflict exists when an active claim by a DIFFERENT actor has at
        least one file in common with expected_files.  The owning actor's own
        active claim is not a conflict (re-claiming is handled by the gate in
        claim() that checks task.status).

        Args:
            task_id:        ID of the task being considered for claiming.
            expected_files: Files the new claim intends to modify.

        Returns:
            List of ConflictWarning — one per conflicting active claim.
            Empty list means no conflicts detected.
        """
        if not expected_files:
            return []

        files_set = set(expected_files)
        active_claims = self._backend.list_active_claims()
        warnings: list[ConflictWarning] = []

        for active_claim in active_claims:
            # Skip same-task re-claims (task status gate handles those).
            if active_claim.task_id == task_id:
                continue
            # Skip our own claims — we don't conflict with ourselves.
            if active_claim.claimed_by == self._actor:
                continue

            overlap = sorted(files_set & set(active_claim.expected_files))
            if overlap:
                warnings.append(
                    ConflictWarning(
                        other_claim_id=active_claim.id,
                        other_actor=active_claim.claimed_by,
                        overlapping_files=overlap,
                    )
                )

        return warnings

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _generate_claim_id(self) -> str:
        """Generate a collision-safe claim ID: 'C' + 8 hex chars from UUID4.

        The original implementation tried to produce sequential C### IDs by
        incrementing the max of currently-ACTIVE claims. Greptile flagged
        the silent-collision risk: if a historical (released/stale) claim
        shares the same ID — possible when sequential allocation collides
        with previously-issued numbers the active-only scan can't see —
        the SQL handler's INSERT OR IGNORE would silently no-op, leaving
        the task associated with the OLD claim row while the user is
        told the new claim succeeded.

        Always using UUID-derived hex makes collision statistically
        impossible (32 bits, ~4 billion-to-one) and removes the
        scan-and-increment race entirely. We lose human-readable
        sequential numbering in exchange for correctness — fine for an
        internal identifier.
        """
        return "C" + uuid.uuid4().hex[:8].upper()

    def _build_claim_model(
        self,
        *,
        claim_id: str,
        task_id: str,
        expected_files: list[str],
        claim_type: ClaimType,
        now: datetime.datetime,
        branch: str | None = None,
    ) -> Claim:
        """Construct a Claim Pydantic model from the current timestamp and params."""
        lease_expires = now + datetime.timedelta(minutes=self._default_lease)
        return Claim(
            id=claim_id,
            task_id=task_id,
            claimed_by=self._actor,
            claim_type=claim_type,
            status=ClaimStatus.active,
            branch=branch,
            worktree_path=None,
            expected_files=expected_files,
            created_at=now,
            lease_expires_at=lease_expires,
            last_heartbeat_at=now,
            released_at=None,
            release_reason=None,
        )

    def _check_group_conflicts(
        self, task: Task
    ) -> list[tuple[str, str]]:
        """Return (task_id, actor) pairs for conflict_group members already claimed.

        Iterates all active claims, checks whether the claimed task shares any
        conflict_group ID with the given task. Returns at most one entry per
        claimed task (deduplication by task_id).

        PS-1: prefetch the full task table once and build a local
        ``{task_id: Task}`` map instead of calling ``backend.get_task`` per
        active claim. With N concurrent agents the old code did 1 +
        ``list_active_claims`` + N ``get_task`` round-trips; the new shape is
        2 round-trips total (``list_active_claims`` + ``list_tasks``).
        """
        if not task.conflict_groups:
            return []

        task_groups = set(task.conflict_groups)
        active_claims = self._backend.list_active_claims()
        if not active_claims:
            return []

        # Single bulk fetch of every task, then index in memory.  The previous
        # per-claim ``get_task`` call was an N+1 query (PS-1).
        all_tasks = self._backend.list_tasks()
        tasks_by_id: dict[str, Task] = {t.id: t for t in all_tasks}

        conflicts: list[tuple[str, str]] = []
        seen_task_ids: set[str] = set()

        for active_claim in active_claims:
            if active_claim.task_id == task.id:
                continue
            if active_claim.task_id in seen_task_ids:
                continue

            other_task = tasks_by_id.get(active_claim.task_id)
            if other_task is None:
                continue

            if task_groups & set(other_task.conflict_groups):
                conflicts.append((active_claim.task_id, active_claim.claimed_by))
                seen_task_ids.add(active_claim.task_id)

        return conflicts
