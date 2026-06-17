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
    "BatchDepError",
    "BatchDepPlan",
    "DepEdge",
    "OrphanClassification",
    "PruneResult",
    "classify_orphans",
    "emit_batch_dep_events",
    "emit_prune_events",
    "has_tasks_section",
    "parse_dep_edge",
    "plan_batch_dep_edits",
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


# ---------------------------------------------------------------------------
# Batch dependency-edit primitive (backlog T022/F007)
# ---------------------------------------------------------------------------
#
# A single atomic, cycle-detecting primitive shared by the CLI ``deps`` command
# and the MCP ``edit_dependencies`` tool. The contract:
#
#   * Edges are ``(source, target)`` pairs meaning *source depends on target*
#     (``target`` is appended to ``source.dependencies`` — the same orientation
#     ``Task.dependencies`` already uses).
#   * ``add`` / ``remove`` operations and multiple sources/targets are accepted
#     in one call.
#   * The WHOLE batch is validated before any mutation is emitted: unknown task
#     IDs, self-loops, and any edit that would introduce a dependency *cycle*
#     reject the entire batch with NO partial application.
#
# Atomicity note: the SQLite backend commits one event per ``append`` call, so
# there is no multi-event SQL transaction. Atomicity is therefore enforced at
# *this* layer — ``plan_batch_dep_edits`` does all validation up front and
# raises ``BatchDepError`` before a single event is emitted, so a rejected
# batch never mutates state. Only after planning succeeds does the caller emit
# the (already-validated) per-task upserts.


@dataclass(frozen=True)
class DepEdge:
    """One dependency edge in a batch edit.

    ``op`` is ``"add"`` or ``"remove"``. ``source`` depends on ``target`` —
    i.e. ``target`` is the entry added to / removed from
    ``source.dependencies``. This matches the orientation of
    :attr:`fakoli_state.state.models.Task.dependencies` (a task lists the IDs
    it depends on) and the ``dep --> task`` edge orientation used by the graph
    renderer (``target --> source``).
    """

    op: str
    source: str
    target: str


class BatchDepError(Exception):
    """A batch dependency edit was rejected before any mutation was applied.

    Carries a user-actionable ``message`` plus a short stable ``code`` token so
    the CLI ``--json`` envelope and the MCP ``ToolError`` can both branch on the
    reason without re-parsing prose. ``code`` is one of: ``bad_request`` (malformed
    edge spec / unknown op), ``unknown_task`` (an edge references a task that does
    not exist), ``self_loop`` (source == target), ``cycle`` (the resulting graph
    would contain a dependency cycle).
    """

    def __init__(self, message: str, *, code: str = "bad_request") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass(frozen=True)
class BatchDepPlan:
    """The validated, ready-to-emit result of :func:`plan_batch_dep_edits`.

    Attributes:
        new_dependencies: ``task_id -> new dependency list`` for every task
            whose dependency set actually changed. Deterministic ordering:
            keys iterate in task-ID order and each list preserves the task's
            original dependency order with additions appended in edge order.
        added: the ``(source, target)`` edges that were newly added (an edge
            that already existed is a no-op and excluded).
        removed: the ``(source, target)`` edges that were actually removed (an
            edge that was not present is a no-op and excluded).
    """

    new_dependencies: dict[str, list[str]] = field(default_factory=dict)
    added: list[tuple[str, str]] = field(default_factory=list)
    removed: list[tuple[str, str]] = field(default_factory=list)


def parse_dep_edge(raw: str, op: str) -> DepEdge:
    """Parse a ``"SOURCE:TARGET"`` (or ``"SOURCE->TARGET"``) edge spec.

    Accepts the two human-friendly separators a CLI user is likely to type:
    a colon (``T002:T001``) or an arrow (``T002->T001``). Whitespace around the
    IDs is stripped. Raises :class:`BatchDepError` (``code="bad_request"``) when
    the spec is not exactly two non-empty tokens.
    """
    if "->" in raw:
        parts = raw.split("->", 1)
    elif ":" in raw:
        parts = raw.split(":", 1)
    else:
        raise BatchDepError(
            f"invalid edge spec {raw!r}: expected 'SOURCE:TARGET' "
            "(source depends on target).",
            code="bad_request",
        )
    source, target = parts[0].strip(), parts[1].strip()
    if not source or not target:
        raise BatchDepError(
            f"invalid edge spec {raw!r}: both SOURCE and TARGET are required.",
            code="bad_request",
        )
    return DepEdge(op=op, source=source, target=target)


def _has_cycle(dep_map: dict[str, list[str]]) -> list[str] | None:
    """Return a cycle (list of task IDs) if ``dep_map`` has one, else ``None``.

    ``dep_map`` maps ``task -> list of tasks it depends on``. A back-edge during
    DFS over this relation is a dependency cycle (A depends on B depends on … on
    A). Deterministic: nodes and edges are visited in sorted order so the same
    graph always reports the same cycle path.
    """
    WHITE, GREY, BLACK = 0, 1, 2
    colour: dict[str, int] = {node: WHITE for node in dep_map}
    stack_path: list[str] = []

    def visit(node: str) -> list[str] | None:
        colour[node] = GREY
        stack_path.append(node)
        for dep in sorted(dep_map.get(node, [])):
            if dep not in colour:
                # Edge to a task with no outgoing deps recorded — treat as a
                # leaf; it cannot start a cycle on its own.
                continue
            if colour[dep] == GREY:
                # Back-edge: slice the path from the first occurrence of `dep`.
                idx = stack_path.index(dep)
                return [*stack_path[idx:], dep]
            if colour[dep] == WHITE:
                found = visit(dep)
                if found is not None:
                    return found
        stack_path.pop()
        colour[node] = BLACK
        return None

    for node in sorted(dep_map):
        if colour[node] == WHITE:
            found = visit(node)
            if found is not None:
                return found
    return None


def plan_batch_dep_edits(
    tasks: list[Task],
    edges: list[DepEdge],
) -> BatchDepPlan:
    """Validate a batch of dependency edits and return the resulting changes.

    PURE — does not touch the backend. The caller passes the already-loaded
    tasks plus the parsed edge list; this computes the post-edit dependency map,
    rejects the whole batch on any invalid edge (unknown task, self-loop,
    unknown op) or any resulting cycle, and returns a :class:`BatchDepPlan`
    naming exactly the tasks whose dependency set changed.

    Determinism: the returned ``new_dependencies`` map and the ``added`` /
    ``removed`` lists are stable for a given (tasks, edges) input so the caller
    emits events — and the ``--json`` envelope reports them — in a fixed order.

    Raises:
        BatchDepError: on any invalid edge or a resulting cycle. Because this
            runs to completion BEFORE the caller emits any event, a raised error
            guarantees no partial application.
    """
    known_ids = {t.id for t in tasks}
    # Work on a mutable copy of every task's dependency list, preserving order.
    dep_map: dict[str, list[str]] = {t.id: list(t.dependencies) for t in tasks}
    original: dict[str, list[str]] = {t.id: list(t.dependencies) for t in tasks}

    added: list[tuple[str, str]] = []
    removed: list[tuple[str, str]] = []

    for edge in edges:
        if edge.op not in {"add", "remove"}:
            raise BatchDepError(
                f"unknown dependency op {edge.op!r}: expected 'add' or 'remove'.",
                code="bad_request",
            )
        for tid, role in ((edge.source, "source"), (edge.target, "target")):
            if tid not in known_ids:
                raise BatchDepError(
                    f"{role} task {tid!r} does not exist.",
                    code="unknown_task",
                )
        if edge.source == edge.target:
            raise BatchDepError(
                f"self-dependency rejected: task {edge.source!r} cannot depend "
                "on itself.",
                code="self_loop",
            )

        deps = dep_map[edge.source]
        if edge.op == "add":
            if edge.target not in deps:
                deps.append(edge.target)
                added.append((edge.source, edge.target))
        else:  # remove
            if edge.target in deps:
                deps.remove(edge.target)
                removed.append((edge.source, edge.target))

    cycle = _has_cycle(dep_map)
    if cycle is not None:
        raise BatchDepError(
            "dependency cycle rejected: " + " -> ".join(cycle) + ".",
            code="cycle",
        )

    # Only tasks whose dependency set actually changed get an upsert.
    new_dependencies = {
        tid: deps
        for tid, deps in dep_map.items()
        if deps != original[tid]
    }
    return BatchDepPlan(
        new_dependencies={tid: new_dependencies[tid] for tid in sorted(new_dependencies)},
        added=added,
        removed=removed,
    )


def emit_batch_dep_events(
    backend: Backend,
    tasks_by_id: dict[str, Task],
    plan: BatchDepPlan,
    *,
    actor: str,
    clock: Clock,
) -> list[str]:
    """Emit one ``task.created`` upsert per changed task in ``plan``.

    The ``task.created`` upsert deliberately omits ``status`` from its SQL
    ``ON CONFLICT DO UPDATE`` set, so re-emitting a task with a new
    ``dependencies`` list updates *only* the dependency column (and
    ``updated_at``) — it never regresses a claimed / in-progress task. This is
    why the batch primitive reuses ``task.created`` rather than inventing a new
    event type: dependency-only edits are exactly what that upsert already
    supports safely.

    Returns the list of task IDs that were upserted (== ``plan.new_dependencies``
    keys, in their deterministic order).
    """
    from fakoli_state.state.models import EventDraft

    upserted: list[str] = []
    for task_id, new_deps in plan.new_dependencies.items():
        task = tasks_by_id[task_id]
        now = clock.now()
        task_data = task.model_dump(mode="json")
        task_data["dependencies"] = list(new_deps)
        task_data["updated_at"] = now.isoformat()
        draft = EventDraft(
            timestamp=now,
            actor=actor,
            action="task.created",
            target_kind="task",
            target_id=task_id,
            payload_json=task_data,
        )
        backend.append(draft)
        upserted.append(task_id)
    return upserted
