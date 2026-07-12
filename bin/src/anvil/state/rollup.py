"""Per-PRD status rollup (v0.3 multi-PRD, T020).

A single pure helper, :func:`compute_prd_rollup`, that partitions the already-
fetched project entities (PRDs, tasks, active claims) into one rollup entry per
PRD plus a PROJECT TOTAL. Both ``anvil status`` (CLI) and the
``get_project_status`` / ``get_project_summary`` MCP tools consume it so the
per-PRD numbers never drift between the two surfaces.

Design:
- A task belongs to a PRD via ``task.prd_id`` (T011 partition column).
- A claim belongs to whatever PRD owns its ``task_id``.
- On a single-PRD DB every task/claim maps to the one PRD, so that PRD's entry
  carries the same numbers the legacy flat totals did (acceptance criterion 1).
- A v6 DB that had tasks but no ``prds`` row (``anvil init`` mints tasks-capable
  state without a PRD) migrates to v7 with ``tasks.prd_id = 'default'`` yet still
  no PRD row to carry forward. Those tasks carry the *canonical* default id, so
  they surface as a real default-PRD block (status ``"none"``, sorted among the
  known PRDs) — NOT a trailing orphan — keeping the documented single-PRD shape.
- Tasks whose ``prd_id`` is empty/NULL or names a non-existent PRD are still
  counted in the project total; they are also surfaced as their own trailing
  synthetic entry (status ``"none"``) so the rollup is exhaustive and a
  mis-migrated row never silently vanishes from the per-PRD view.

The helper does NO I/O — callers fetch the entities and pass them in.
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from anvil.state.models import (
        PRD,
        BundleClaim,
        BundleReviewVerdict,
        Claim,
        ExecutionBundle,
        Task,
    )

# Every TaskStatus value, in declaration order, so the per-status counts dict is
# deterministic and exhaustive regardless of which statuses are present.
_TASK_STATUS_ORDER = (
    "proposed",
    "drafted",
    "reviewed",
    "ready",
    "claimed",
    "in_progress",
    "blocked",
    "needs_review",
    "accepted",
    "done",
    "rejected",
)


class PrdRollupEntry(BaseModel):
    """One per-PRD slice of project state for the status rollup (T020)."""

    model_config = ConfigDict(extra="forbid")

    prd_id: str
    status: str
    total_tasks: int = 0
    ready_task_count: int = 0
    active_claim_count: int = 0
    task_counts: dict[str, int] = Field(default_factory=dict)


class BundleRollupEntry(BaseModel):
    """Compact integration-focused status for one execution bundle."""

    model_config = ConfigDict(extra="forbid")

    bundle_id: str
    prd_id: str
    status: str
    coordinator: str
    member_counts: dict[str, int]
    coordinator_claim: dict[str, Any] | None = None
    delegated_agents: list[dict[str, Any]] = Field(default_factory=list)
    critical_path_stage: int = 0
    critical_path_depth: int = 0
    review_usage: dict[str, int] = Field(default_factory=dict)
    last_result_at: datetime.datetime | None = None
    elapsed_since_result_seconds: int | None = None
    checkpoint: dict[str, Any] | None = None
    checkpoint_warning: str | None = None
    superseded_by: str | None = None


def compute_bundle_rollup(
    bundles: list[ExecutionBundle],
    tasks: list[Task],
    bundle_claims: list[BundleClaim],
    reviews: list[BundleReviewVerdict],
    *,
    now: datetime.datetime,
    result_times: dict[str, datetime.datetime] | None = None,
) -> list[BundleRollupEntry]:
    tasks_by_id = {task.id: task for task in tasks}
    claims_by_bundle: dict[str, BundleClaim] = {}
    for claim in sorted(
        bundle_claims,
        key=lambda item: (
            item.bundle_id,
            item.status.value == "active",
            item.created_at,
            item.id,
        ),
    ):
        claims_by_bundle[claim.bundle_id] = claim
    result_times = result_times or {}
    entries: list[BundleRollupEntry] = []
    result_statuses = {
        "reviewed_unintegrated",
        "integrated",
        "merged",
        "completed",
    }
    done_statuses = {"accepted", "done"}
    for bundle in sorted(bundles, key=lambda item: item.id):
        members = [tasks_by_id[task_id] for task_id in bundle.task_ids if task_id in tasks_by_id]
        counts: dict[str, int] = {}
        for task in members:
            counts[task.status.value] = counts.get(task.status.value, 0) + 1
        member_ids = {task.id for task in members}
        memo: dict[str, int] = {}

        def depth(
            task_id: str,
            visiting: frozenset[str] = frozenset(),
            *,
            memo: dict[str, int] = memo,
            member_ids: set[str] = member_ids,
            tasks_by_id: dict[str, Task] = tasks_by_id,
        ) -> int:
            if task_id in memo:
                return memo[task_id]
            if task_id in visiting:
                return 0
            task = tasks_by_id[task_id]
            value = 1 + max(
                (
                    depth(dep, visiting | {task_id})
                    for dep in task.dependencies
                    if dep in member_ids
                ),
                default=0,
            )
            memo[task_id] = value
            return value

        critical_depth = max((depth(task.id) for task in members), default=0)
        completion_memo: dict[str, bool] = {}

        def dependency_closed(
            task_id: str,
            *,
            completion_memo: dict[str, bool] = completion_memo,
            member_ids: set[str] = member_ids,
            tasks_by_id: dict[str, Task] = tasks_by_id,
        ) -> bool:
            if task_id in completion_memo:
                return completion_memo[task_id]
            task = tasks_by_id[task_id]
            complete = task.status.value in done_statuses and all(
                dependency_closed(dep)
                for dep in task.dependencies
                if dep in member_ids
            )
            completion_memo[task_id] = complete
            return complete

        completed_depth = max(
            (depth(task.id) for task in members if dependency_closed(task.id)),
            default=0,
        )
        current_reviews = [
            review
            for review in reviews
            if review.bundle_id == bundle.id
            and review.disposition_event_id == bundle.review_disposition_event_id
        ]
        latest_round = max((review.review_round for review in current_reviews), default=0)
        round_reviews = [
            review for review in current_reviews if review.review_round == latest_round
        ]
        last_result = (
            result_times.get(bundle.id)
            if bundle.status.value in result_statuses
            else None
        )
        checkpoint = (
            bundle.checkpoint.model_dump(mode="json")
            if bundle.checkpoint is not None
            else None
        )
        warning = None
        if bundle.status.value in result_statuses and checkpoint is None:
            warning = (
                "Reviewed bundle has no delivery checkpoint; record a commit or PR."
            )
        claim = claims_by_bundle.get(bundle.id)
        entries.append(
            BundleRollupEntry(
                bundle_id=bundle.id,
                prd_id=bundle.prd_id,
                status=bundle.status.value,
                coordinator=bundle.coordinator,
                member_counts=counts,
                coordinator_claim=(claim.model_dump(mode="json") if claim else None),
                delegated_agents=[
                    observation.model_dump(mode="json")
                    for observation in bundle.delegated_agents
                ],
                critical_path_stage=completed_depth,
                critical_path_depth=critical_depth,
                review_usage={
                    "round": latest_round,
                    "reviews": len(round_reviews),
                    "rereviews": max(latest_round - 1, 0),
                },
                last_result_at=last_result,
                elapsed_since_result_seconds=(
                    int((now - last_result).total_seconds())
                    if last_result is not None
                    else None
                ),
                checkpoint=checkpoint,
                checkpoint_warning=warning,
                superseded_by=bundle.superseded_by,
            )
        )
    return entries


def _empty_counts() -> dict[str, int]:
    return {status: 0 for status in _TASK_STATUS_ORDER}


def compute_prd_rollup(
    prds: list[PRD],
    tasks: list[Task],
    active_claims: list[Claim],
) -> list[PrdRollupEntry]:
    """Return one :class:`PrdRollupEntry` per PRD, ordered by ``prd_id`` ASC.

    ``prds`` should be the result of ``backend.list_prds()`` (already ordered by
    id). ``tasks`` / ``active_claims`` are the unfiltered project-wide lists.
    Every PRD gets an entry even when it owns no tasks (so a freshly-parsed PRD
    still shows up with zeroed counts). Tasks carrying the canonical default id
    (``"default"``) with no matching PRD row — the migrated-no-PRD-row case —
    surface as a real default-PRD block. Tasks pointing at any other unknown
    ``prd_id`` are collected into a trailing synthetic entry with status
    ``"none"``.
    """
    # task_id -> prd_id, so claims (which only carry task_id) can be partitioned.
    task_prd: dict[str, str] = {t.id: (t.prd_id or "") for t in tasks}

    # Seed an entry per known PRD so empty PRDs still surface.
    entries: dict[str, PrdRollupEntry] = {}
    known_prd_ids: set[str] = set()
    for prd in prds:
        known_prd_ids.add(prd.id)
        entries[prd.id] = PrdRollupEntry(
            prd_id=prd.id,
            status=prd.status.value,
            task_counts=_empty_counts(),
        )

    def _ensure_entry(prd_id: str) -> PrdRollupEntry:
        """Lazily create an entry for a ``prd_id`` that has no PRD row.

        The canonical default id (``"default"``) on a DB with no PRD row is the
        migrated-no-PRD-row case (a v6 project that had tasks but never a parsed
        PRD): it surfaces as a real default-PRD block. Any other unknown
        id (empty/NULL or a non-existent named PRD) becomes a synthetic orphan.
        Either way status is ``"none"`` (there is no PRD row to read it from);
        the distinction is which id the block carries, which keeps the migrated
        single-PRD rollup a real ``default`` block rather than a stray orphan.
        """
        entry = entries.get(prd_id)
        if entry is None:
            entry = PrdRollupEntry(
                prd_id=prd_id,
                status="none",
                task_counts=_empty_counts(),
            )
            entries[prd_id] = entry
        return entry

    for task in tasks:
        prd_id = task.prd_id or ""
        if prd_id in known_prd_ids:
            entry = entries[prd_id]
        else:
            entry = _ensure_entry(prd_id)
        entry.total_tasks += 1
        status_val = task.status.value
        # Defensive: only count statuses we know about (future-proof against an
        # unexpected literal); ready gets its own running tally.
        if status_val in entry.task_counts:
            entry.task_counts[status_val] += 1
        if status_val == "ready":
            entry.ready_task_count += 1

    for claim in active_claims:
        # Claim references a task we never saw (defensive): bucket it under an
        # unknown-PRD entry keyed by empty id rather than dropping it.
        claim_prd_id = task_prd.get(claim.task_id) or ""
        if claim_prd_id in known_prd_ids:
            entry = entries[claim_prd_id]
        else:
            entry = _ensure_entry(claim_prd_id)
        entry.active_claim_count += 1

    # Deterministic order: known PRDs by id, orphan/synthetic entries last (also
    # by id). list_prds already returns id-sorted, so a plain sort by id matches
    # that ordering while keeping orphan ids (which sort among the rest) stable.
    return [entries[pid] for pid in sorted(entries, reverse=True)]
