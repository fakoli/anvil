"""Shared `plan` helpers consumed by both the CLI and the MCP server.

Before v1.15.0 (post-greptile), the CLI's `anvil plan` command and the
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
from collections import defaultdict, deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from anvil.clock import Clock
    from anvil.state.backend import Backend
    from anvil.state.models import Feature, Task

__all__ = [
    "SAFE_DELETE_STATUSES",
    "DEPENDENCY_BAD_OP_MESSAGE",
    "DEPENDENCY_BATCH_LIMIT_MESSAGE",
    "BatchDepError",
    "BatchDepPlan",
    "DEPENDENCY_CYCLE_MESSAGE",
    "DEPENDENCY_EDGE_FORMAT_MESSAGE",
    "DEPENDENCY_EDGE_LIST_FORMAT_MESSAGE",
    "DEPENDENCY_PAIR_FORMAT_MESSAGE",
    "DEPENDENCY_SELF_LOOP_MESSAGE",
    "DEPENDENCY_UNKNOWN_SOURCE_MESSAGE",
    "DEPENDENCY_UNKNOWN_TARGET_MESSAGE",
    "DepEdge",
    "MAX_DEPENDENCY_EDGES_PER_BATCH",
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
        actor: Identity to record on the event (``anvil-cli`` /
            ``anvil-mcp``).
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
    from anvil.state.models import EventDraft

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
# A single cycle-detecting primitive shared by the CLI ``deps`` command
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
# Atomicity boundary: ``plan_batch_dep_edits`` validates the entire requested
# graph before mutation, so a validation rejection never partially applies.
# The SQLite backend still commits one event per ``append`` call, however, so a
# failure while emitting an already-validated multi-task plan can leave earlier
# task upserts committed. True whole-batch atomicity requires a single batch
# event whose write handler revalidates the prior dependencies/graph cursor and
# applies every task update in one SQLite transaction.

DEPENDENCY_EVENT_REJECTED_CODE = "event_rejected"
DEPENDENCY_EVENT_REJECTED_MESSAGE = (
    "dependency update was rejected by state validation."
)
DEPENDENCY_EDGE_FORMAT_MESSAGE = (
    "invalid dependency edge: use exactly one 'SOURCE->TARGET' separator; "
    "'SOURCE:TARGET' is supported only for unscoped IDs."
)
DEPENDENCY_PAIR_FORMAT_MESSAGE = (
    "invalid dependency edge: expected a two-element [source, target] string pair."
)
DEPENDENCY_EDGE_LIST_FORMAT_MESSAGE = (
    "invalid dependency edges: expected a list of [source, target] string pairs."
)
MAX_DEPENDENCY_EDGES_PER_BATCH = 10_000
DEPENDENCY_BATCH_LIMIT_MESSAGE = (
    "dependency update exceeds the maximum batch size of 10000 edges."
)
DEPENDENCY_BAD_OP_MESSAGE = (
    "invalid dependency operation: expected 'add' or 'remove'."
)
DEPENDENCY_UNKNOWN_SOURCE_MESSAGE = (
    "dependency update references an unknown source task."
)
DEPENDENCY_UNKNOWN_TARGET_MESSAGE = (
    "dependency update references an unknown target task."
)
DEPENDENCY_SELF_LOOP_MESSAGE = (
    "self-dependency rejected: a task cannot depend on itself."
)
DEPENDENCY_CYCLE_MESSAGE = (
    "dependency cycle rejected: the resulting graph contains a cycle."
)


@dataclass(frozen=True)
class DepEdge:
    """One dependency edge in a batch edit.

    ``op`` is ``"add"`` or ``"remove"``. ``source`` depends on ``target`` —
    i.e. ``target`` is the entry added to / removed from
    ``source.dependencies``. This matches the orientation of
    :attr:`anvil.state.models.Task.dependencies` (a task lists the IDs
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
    """Parse a canonical ``"SOURCE->TARGET"`` dependency edge.

    The arrow is required when IDs are PRD-scoped and therefore contain ``:``.
    For backward compatibility, the colon shorthand (``T002:T001``) remains
    supported where both IDs are unscoped. Whitespace around IDs is stripped.
    Raises :class:`BatchDepError` (``code="bad_request"``) when the spec is not
    exactly two non-empty tokens.
    """
    arrow_count = raw.count("->")
    if arrow_count == 1:
        parts = raw.split("->", 1)
    elif arrow_count > 1:
        raise BatchDepError(
            DEPENDENCY_EDGE_FORMAT_MESSAGE,
            code="bad_request",
        )
    elif raw.count(":") == 1:
        parts = raw.split(":", 1)
    elif ":" in raw:
        raise BatchDepError(
            DEPENDENCY_EDGE_FORMAT_MESSAGE,
            code="bad_request",
        )
    else:
        raise BatchDepError(
            DEPENDENCY_EDGE_FORMAT_MESSAGE,
            code="bad_request",
        )
    source, target = parts[0].strip(), parts[1].strip()
    if not source or not target:
        raise BatchDepError(
            DEPENDENCY_EDGE_FORMAT_MESSAGE,
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

    # Use an explicit DFS stack rather than Python recursion. Real task graphs
    # can legitimately exceed the interpreter recursion limit; depth alone is
    # not a cycle and must not turn a valid edit into an internal error.
    for start in sorted(dep_map):
        if colour[start] != WHITE:
            continue
        colour[start] = GREY
        path = [start]
        path_index = {start: 0}
        stack: list[tuple[str, Iterator[str]]] = [
            (start, iter(sorted(dep_map.get(start, []))))
        ]
        while stack:
            node, edge_iter = stack[-1]
            try:
                dep = next(edge_iter)
            except StopIteration:
                stack.pop()
                path_index.pop(node, None)
                path.pop()
                colour[node] = BLACK
                continue

            if dep not in colour:
                # Edge to a task with no outgoing deps recorded — treat as a
                # leaf; it cannot start a cycle on its own.
                continue
            if colour[dep] == GREY:
                idx = path_index[dep]
                return [*path[idx:], dep]
            if colour[dep] == WHITE:
                colour[dep] = GREY
                path_index[dep] = len(path)
                path.append(dep)
                stack.append((dep, iter(sorted(dep_map.get(dep, [])))))
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
    if len(edges) > MAX_DEPENDENCY_EDGES_PER_BATCH:
        raise BatchDepError(DEPENDENCY_BATCH_LIMIT_MESSAGE, code="bad_request")

    known_ids = {t.id for t in tasks}
    # Each task keeps an append-only ordered entry list plus per-target queues
    # of live positions. Membership, add, and remove are O(1), while the final
    # compaction preserves exact list semantics (including duplicate legacy
    # entries and remove-then-readd moving an edge to the end).
    removed_sentinel = object()
    dep_entries: dict[str, list[str | object]] = {}
    dep_positions: dict[str, dict[str, deque[int]]] = {}
    for task in tasks:
        entries: list[str | object] = list(task.dependencies)
        positions: dict[str, deque[int]] = defaultdict(deque)
        for index, dependency in enumerate(task.dependencies):
            positions[dependency].append(index)
        dep_entries[task.id] = entries
        dep_positions[task.id] = positions
    original: dict[str, list[str]] = {t.id: list(t.dependencies) for t in tasks}

    added: list[tuple[str, str]] = []
    removed: list[tuple[str, str]] = []

    for edge in edges:
        if edge.op not in {"add", "remove"}:
            raise BatchDepError(
                DEPENDENCY_BAD_OP_MESSAGE,
                code="bad_request",
            )
        for tid, role in ((edge.source, "source"), (edge.target, "target")):
            if tid not in known_ids:
                raise BatchDepError(
                    (
                        DEPENDENCY_UNKNOWN_SOURCE_MESSAGE
                        if role == "source"
                        else DEPENDENCY_UNKNOWN_TARGET_MESSAGE
                    ),
                    code="unknown_task",
                )
        if edge.source == edge.target:
            raise BatchDepError(
                DEPENDENCY_SELF_LOOP_MESSAGE,
                code="self_loop",
            )

        entries = dep_entries[edge.source]
        positions = dep_positions[edge.source]
        if edge.op == "add":
            if not positions.get(edge.target):
                positions[edge.target].append(len(entries))
                entries.append(edge.target)
                added.append((edge.source, edge.target))
        else:  # remove
            live_positions = positions.get(edge.target)
            if live_positions:
                entries[live_positions.popleft()] = removed_sentinel
                removed.append((edge.source, edge.target))

    dep_map = {
        task_id: [
            dependency
            for dependency in entries
            if dependency is not removed_sentinel and isinstance(dependency, str)
        ]
        for task_id, entries in dep_entries.items()
    }
    cycle = _has_cycle(dep_map)
    if cycle is not None:
        raise BatchDepError(
            DEPENDENCY_CYCLE_MESSAGE,
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
    from anvil.state.models import EventDraft

    upserted: list[str] = []
    for task_id, new_deps in plan.new_dependencies.items():
        task = tasks_by_id[task_id]
        now = clock.now()
        task_data = task.model_dump(mode="json")
        # Task.prd_id is intentionally excluded from model_dump() so legacy API
        # snapshots stay byte-identical. Event payloads are different: prd_id is
        # ownership-critical and must be carried explicitly for named PRDs.
        task_data["prd_id"] = task.prd_id
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
