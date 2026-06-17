"""Shared `plan` helpers consumed by both the CLI and the MCP server.

Before v1.15.0 (post-greptile), the CLI's `fakoli-state plan` command and the
MCP `plan_tasks` tool each carried their own copies of:

- the `## Tasks` markdown idempotency regex + helper
- the orphan-prune classification logic (safe vs unsafe vs feature orphans)
- the `SAFE_DELETE_STATUSES` frozenset (the third copy of the same constant
  that already lived in `state.sqlite._DELETABLE_TASK_STATUSES`)
- the event-emission loops that translate the classification into
  `task.deleted` / `feature.deleted` events

Multiple critics flagged this. Worse, the CLI loop was missing
`try/except TransactionAborted` (which the MCP loop had), so a handler-level
rejection (e.g. feature with referencing tasks) surfaced as a raw Python
traceback in the CLI while the MCP path correctly surfaced it as a
`ToolError`. The greptile review made this the headline finding.

This module collapses both paths into one. The CLI and MCP both call the
same `classify_orphans()` + `emit_prune_events()` and surface
`TransactionAborted` in a layer-appropriate way (typer.Exit / ToolError).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fakoli_state.clock import Clock
    from fakoli_state.state.backend import Backend
    from fakoli_state.state.models import Feature, Task

__all__ = [
    "SAFE_DELETE_STATUSES",
    "OrphanClassification",
    "PruneResult",
    "classify_orphans",
    "emit_prune_events",
    "has_tasks_section",
]


# Single source of truth for which task statuses can be deleted without an
# explicit `force=True`. Mirrors (and is intentionally identical to)
# `state.sqlite.SqliteBackend._DELETABLE_TASK_STATUSES` — the SQL handler
# enforces the guarantee at apply-time; this constant lets callers
# pre-classify orphans so they can fail loudly with a helpful error before
# the apply attempt rather than catching a generic TransactionAborted.
SAFE_DELETE_STATUSES: frozenset[str] = frozenset({
    "proposed", "drafted", "ready",
})


# Case-insensitive `## Tasks` H2 heading detection. Used by the LLM
# task-generation backstop to enforce idempotency — once the heading is
# present in `prd.md`, re-running plan must NOT re-append the section.
_TASKS_HEADING_RE = re.compile(r"^##\s+tasks\s*$", re.IGNORECASE | re.MULTILINE)


def has_tasks_section(markdown: str) -> bool:
    """True when `markdown` contains an H2 `## Tasks` heading (any case)."""
    return _TASKS_HEADING_RE.search(markdown) is not None


@dataclass(frozen=True)
class OrphanClassification:
    """Output of :func:`classify_orphans`.

    Attributes:
        safe_task_orphans: tasks present in state.db but absent from the
            new parse, AND in a status that can be deleted without
            ``force=True`` (proposed / drafted / ready).
        unsafe_task_orphans: same as above but in a status (claimed,
            in_progress, needs_review, etc.) that requires
            ``--prune-force`` to delete. Callers MUST gate on this list
            being empty (or prune_force=True) before calling
            :func:`emit_prune_events` — the handler will refuse otherwise.
        feature_orphans: IDs of features present in state.db but absent
            from the new parse. Always considered safe at the
            classification level — the SQLite handler still enforces a
            referencing-task pre-check at apply time (FK RESTRICT).
    """

    safe_task_orphans: list[Task] = field(default_factory=list)
    unsafe_task_orphans: list[Task] = field(default_factory=list)
    feature_orphans: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PruneResult:
    """Output of :func:`emit_prune_events`.

    Attributes:
        pruned_task_ids: IDs of tasks for which a ``task.deleted`` event
            was successfully emitted.
        pruned_feature_ids: IDs of features for which a ``feature.deleted``
            event was successfully emitted.
    """

    pruned_task_ids: list[str] = field(default_factory=list)
    pruned_feature_ids: list[str] = field(default_factory=list)


def classify_orphans(
    existing_tasks: list[Task],
    new_task_ids: set[str],
    existing_features: list[Feature],
    new_feature_ids: set[str],
) -> OrphanClassification:
    """Compute the diff between state.db and the new parse.

    Pure: does not touch the backend. Callers pass the already-loaded
    existing entities + the new ID sets; this is fast even on large
    projects.
    """
    orphan_tasks = [t for t in existing_tasks if t.id not in new_task_ids]
    safe = [
        t for t in orphan_tasks
        if t.status.value in SAFE_DELETE_STATUSES
    ]
    unsafe = [
        t for t in orphan_tasks
        if t.status.value not in SAFE_DELETE_STATUSES
    ]
    feature_orphans = [
        f.id for f in existing_features if f.id not in new_feature_ids
    ]
    return OrphanClassification(
        safe_task_orphans=safe,
        unsafe_task_orphans=unsafe,
        feature_orphans=feature_orphans,
    )


def emit_prune_events(
    backend: Backend,
    classification: OrphanClassification,
    *,
    actor: str,
    clock: Clock,
    prune_force: bool,
) -> PruneResult:
    """Emit ``task.deleted`` and ``feature.deleted`` events for orphans.

    Order is deliberate: tasks first, then features. The schema's
    ``tasks.feature_id ... ON DELETE RESTRICT`` foreign key would block
    a feature delete while any task still references it, so tasks must
    land first.

    Args:
        backend: Backend to apply events through.
        classification: Output of :func:`classify_orphans`. Callers MUST
            gate on ``classification.unsafe_task_orphans`` being empty
            (or ``prune_force=True``) before calling this — the SQL
            handler will raise ``TransactionAborted`` otherwise, and
            the caller should surface that as a layer-appropriate error
            (typer.Exit for CLI, ToolError for MCP).
        actor: Identity to record on the event (``fakoli-state-cli`` /
            ``fakoli-state-mcp``).
        clock: Source of timestamps.
        prune_force: When True, emit task.deleted with ``force=True``
            for tasks in unsafe statuses. The handler bypasses its
            status check in that case (but still enforces the
            claims/evidence FK pre-check unconditionally — even
            ``force=True`` cannot bypass that).

    Returns:
        :class:`PruneResult` with the IDs that were successfully pruned.

    Raises:
        EventRejected: When the SQLite handler refuses a deletion
            (e.g. feature with referencing tasks, or claim/evidence rows
            exist on a task). Callers should catch and surface in a
            layer-appropriate way — the handler's message is
            user-actionable as-is.
    """
    from fakoli_state.state.models import EventDraft

    pruned_task_ids: list[str] = []
    to_delete = classification.safe_task_orphans + (
        classification.unsafe_task_orphans if prune_force else []
    )
    for task in to_delete:
        now = clock.now()
        draft = EventDraft(
            timestamp=now,
            actor=actor,
            action="task.deleted",
            target_kind="task",
            target_id=task.id,
            payload_json={
                "task_id": task.id,
                "force": (
                    prune_force
                    and task.status.value not in SAFE_DELETE_STATUSES
                ),
                "reason": "plan: removed from prd.md (orphan cleanup)",
            },
        )
        backend.append(draft)
        pruned_task_ids.append(task.id)

    pruned_feature_ids: list[str] = []
    for feature_id in classification.feature_orphans:
        now = clock.now()
        draft = EventDraft(
            timestamp=now,
            actor=actor,
            action="feature.deleted",
            target_kind="feature",
            target_id=feature_id,
            payload_json={
                "feature_id": feature_id,
                "force": False,
                "reason": "plan: removed from prd.md (orphan cleanup)",
            },
        )
        backend.append(draft)
        pruned_feature_ids.append(feature_id)

    return PruneResult(
        pruned_task_ids=pruned_task_ids,
        pruned_feature_ids=pruned_feature_ids,
    )
