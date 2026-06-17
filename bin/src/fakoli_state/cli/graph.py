"""``fakoli-state graph`` — emit the persisted task graph as a diagram.

Backlog T019/F008: auto-emit a Mermaid dependency / state diagram derived
from the current task graph. Read-only, deterministic for a given state.

The command surfaces the same node/edge/ready-to-claim view that the MCP
``get_dependency_graph`` tool already exposes, but renders it as a
copy-pasteable Mermaid ``graph`` (flowchart) so a human or a docs pipeline
can drop it into a Markdown file, a PR description, or a Mermaid live editor.

Output formats
--------------
``--format mermaid``
    A Mermaid ``graph LR`` block. Each task is a node labelled
    ``T001\\nstatus`` and coloured by status via ``classDef`` rules; each
    dependency is a directed edge ``dep --> task`` (the dependency points at
    the task that needs it, matching ``get_dependency_graph``'s
    ``from=dep, to=task`` orientation). Deterministic: nodes are emitted in
    task-ID order and edges in (from, to) order, so the same state always
    produces byte-identical output.

``--format text`` (default)
    A short human-readable summary (node count, edge count, ready-to-claim
    list) — keeps the bare ``graph`` invocation friendly without forcing a
    diagram into a terminal.

``--json`` / ``--format json``
    The v1.24 machine-readable envelope: ``{"ok": true, "command": "graph",
    "data": {"format": "...", "nodes": [...], "edges": [...],
    "ready_to_claim": [...], "diagram": "<mermaid>" | null}}``. When the
    requested ``--format`` is ``mermaid`` the rendered diagram is included
    under ``data.diagram`` so a non-Claude host gets both the structured
    graph AND the ready-to-render text in one call.

Scope
-----
``--scope all`` (default) renders the whole project. ``--scope feature
--target F001`` narrows to one feature. ``--scope task --target T007``
renders T007 plus its transitive dependencies. Mirrors the MCP tool's scope
semantics exactly so the two surfaces never diverge.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from fakoli_state.cli._helpers import (
    StateRootError,
    _open_backend,
    _require_state_dir,
    _resolve_state_dir,
)
from fakoli_state.cli._json import JSON_OPTION, emit_success, fail

if TYPE_CHECKING:
    from fakoli_state.state.models import Claim, Task

__all__ = ["graph"]

_COMMAND = "graph"

# Status → Mermaid class. One class per status keeps the classDef block small
# and the colour mapping stable. Statuses not listed fall through to the
# default node style (no class assigned).
_STATUS_CLASSES: dict[str, str] = {
    "proposed": "proposed",
    "drafted": "drafted",
    "reviewed": "reviewed",
    "ready": "ready",
    "claimed": "claimed",
    "in_progress": "inprogress",
    "blocked": "blocked",
    "needs_review": "needsreview",
    "accepted": "accepted",
    "done": "done",
    "rejected": "rejected",
}

# classDef styling, emitted in a fixed order so the diagram is deterministic.
# Colours are advisory; what matters for the acceptance criteria is that the
# block is valid Mermaid and stable across runs.
_CLASS_DEFS: list[tuple[str, str]] = [
    ("proposed", "fill:#eee,stroke:#999,color:#333"),
    ("drafted", "fill:#e7eefc,stroke:#6c8ebf,color:#1a2a4a"),
    ("reviewed", "fill:#dae8fc,stroke:#6c8ebf,color:#1a2a4a"),
    ("ready", "fill:#d5e8d4,stroke:#82b366,color:#1a3a1a"),
    ("claimed", "fill:#fff2cc,stroke:#d6b656,color:#5a4a00"),
    ("inprogress", "fill:#ffe6cc,stroke:#d79b00,color:#5a3a00"),
    ("blocked", "fill:#f8cecc,stroke:#b85450,color:#5a0000"),
    ("needsreview", "fill:#e1d5e7,stroke:#9673a6,color:#3a1a4a"),
    ("accepted", "fill:#d5e8d4,stroke:#82b366,color:#1a3a1a"),
    ("done", "fill:#b9e0a5,stroke:#4a7a2a,color:#0a2a0a"),
    ("rejected", "fill:#f8cecc,stroke:#b85450,color:#5a0000"),
]


def graph(
    fmt: str = typer.Option(  # noqa: B008
        "text",
        "--format",
        "-f",
        help="Output format: 'text' (default human summary), 'mermaid' "
        "(a Mermaid flowchart of the dependency/state graph), or 'json'.",
    ),
    scope: str = typer.Option(  # noqa: B008
        "all",
        "--scope",
        help="Graph scope: 'all' (default), 'feature' (needs --target), or "
        "'task' (the task plus its transitive dependencies; needs --target).",
    ),
    target: str | None = typer.Option(  # noqa: B008
        None,
        "--target",
        help="Feature ID (scope=feature) or task ID (scope=task).",
    ),
    json_output: bool = JSON_OPTION,
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory. Defaults to the current working directory.",
        hidden=True,
    ),
) -> None:
    """Emit the task dependency/state graph (Mermaid, JSON, or a text summary).

    Read-only and deterministic: the same persisted state always yields the
    same diagram. ``--format mermaid`` prints a Mermaid flowchart whose nodes
    are tasks (coloured by status) and whose edges are dependencies. With
    ``--json`` (or ``--format json``) the v1.24 envelope is emitted instead.
    """
    # Treat --json as an explicit JSON format request even if --format was left
    # at its default, so consumers can pipe `graph --json` like every other
    # command. An explicit `--format json` is equivalent.
    want_json = json_output or fmt == "json"

    # Pipe-safe state-root resolution (mirrors `drift` / `status`): a bad
    # FAKOLI_STATE_ROOT under --json must still produce a parseable envelope.
    try:
        state_dir = _resolve_state_dir(cwd)
    except StateRootError as exc:
        if want_json:
            fail(_COMMAND, str(exc), code="state_root_invalid")
        raise
    _require_state_dir(state_dir, command=_COMMAND, json_output=want_json)

    # Validate scope / format early with the same envelope contract.
    if scope not in {"all", "feature", "task"}:
        if want_json:
            fail(_COMMAND, f"unknown scope '{scope}'.", code="bad_request")
        typer.echo(f"Error: unknown scope '{scope}'.", err=True)
        raise typer.Exit(code=2)
    if fmt not in {"text", "mermaid", "json"}:
        if want_json:
            fail(_COMMAND, f"unknown format '{fmt}'.", code="bad_request")
        typer.echo(f"Error: unknown format '{fmt}'.", err=True)
        raise typer.Exit(code=2)
    if scope in {"feature", "task"} and not target:
        msg = f"--target is required when --scope {scope}."
        if want_json:
            fail(_COMMAND, msg, code="bad_request")
        typer.echo(f"Error: {msg}", err=True)
        raise typer.Exit(code=2)

    backend = _open_backend(state_dir)
    try:
        all_tasks = backend.list_tasks()
        active_claims = backend.list_active_claims()
    finally:
        backend.close()

    nodes, edges, ready_to_claim = build_graph(
        all_tasks, active_claims, scope=scope, target=target
    )

    if want_json:
        diagram = render_mermaid(nodes, edges) if fmt == "mermaid" else None
        emit_success(
            _COMMAND,
            {
                "format": fmt if fmt != "text" else "json",
                "scope": scope,
                "target": target,
                "nodes": [_node_to_json(n) for n in nodes],
                "edges": [{"from": a, "to": b} for a, b in edges],
                "ready_to_claim": ready_to_claim,
                "diagram": diagram,
            },
        )
        return

    if fmt == "mermaid":
        typer.echo(render_mermaid(nodes, edges))
        return

    _print_text_summary(nodes, edges, ready_to_claim)


# ---------------------------------------------------------------------------
# Graph construction (pure) — reuses the same semantics as the MCP
# get_dependency_graph tool so the two surfaces never drift.
# ---------------------------------------------------------------------------


class _Node:
    """A graph node: just the task fields the diagram needs."""

    __slots__ = ("feature_id", "id", "priority", "status", "title")

    def __init__(
        self, *, id: str, title: str, status: str, priority: str, feature_id: str
    ) -> None:
        self.id = id
        self.title = title
        self.status = status
        self.priority = priority
        self.feature_id = feature_id


def build_graph(
    all_tasks: list[Task],
    active_claims: list[Claim],
    *,
    scope: str = "all",
    target: str | None = None,
) -> tuple[list[_Node], list[tuple[str, str]], list[str]]:
    """Compute (nodes, edges, ready_to_claim) for the requested scope.

    Deterministic: nodes are returned sorted by task ID, edges sorted by
    ``(from, to)``, and ready_to_claim sorted. Edge orientation is
    ``dep --> task`` — identical to the MCP ``get_dependency_graph`` tool
    (``from=dep_id, to=task.id``) so a consumer reading both surfaces sees the
    same arrows.
    """
    task_map = {t.id: t for t in all_tasks}
    claimed_task_ids = {c.task_id for c in active_claims}
    done_task_ids = {t.id for t in all_tasks if t.status.value == "done"}

    if scope == "all":
        scoped_tasks = list(all_tasks)
    elif scope == "feature":
        scoped_tasks = [t for t in all_tasks if t.feature_id == target]
    else:  # scope == "task": target plus transitive dependencies
        visited: set[str] = set()
        queue = [target] if target else []
        while queue:
            tid = queue.pop()
            if tid in visited:
                continue
            visited.add(tid)
            t = task_map.get(tid)
            if t is None:
                continue
            for dep_id in t.dependencies:
                if dep_id not in visited:
                    queue.append(dep_id)
        scoped_tasks = [task_map[tid] for tid in visited if tid in task_map]

    scoped_tasks.sort(key=lambda t: t.id)
    scoped_ids = {t.id for t in scoped_tasks}

    nodes = [
        _Node(
            id=t.id,
            title=t.title,
            status=t.status.value,
            priority=t.priority.value,
            feature_id=t.feature_id,
        )
        for t in scoped_tasks
    ]

    edges: list[tuple[str, str]] = []
    for t in scoped_tasks:
        for dep_id in t.dependencies:
            if dep_id in scoped_ids:
                edges.append((dep_id, t.id))
    edges.sort()

    ready_to_claim: list[str] = []
    for t in scoped_tasks:
        if t.status.value != "ready":
            continue
        if t.id in claimed_task_ids:
            continue
        if any(dep_id not in done_task_ids for dep_id in t.dependencies):
            continue
        ready_to_claim.append(t.id)
    ready_to_claim.sort()

    return nodes, edges, ready_to_claim


def _node_to_json(n: _Node) -> dict[str, Any]:
    return {
        "id": n.id,
        "title": n.title,
        "status": n.status,
        "priority": n.priority,
        "feature_id": n.feature_id,
    }


# ---------------------------------------------------------------------------
# Mermaid rendering (pure)
# ---------------------------------------------------------------------------


def _mermaid_node_id(task_id: str) -> str:
    """Sanitize a task ID into a Mermaid-safe node identifier.

    Mermaid node IDs must be alphanumeric / underscore; a subtask ID like
    ``T001.1`` contains a dot which would break the syntax. We map every
    non-alphanumeric character to ``_`` so ``T001.1`` → ``T001_1``. The human
    label inside the node keeps the original ID.
    """
    return "".join(ch if ch.isalnum() else "_" for ch in task_id)


def _escape_label(text: str) -> str:
    """Escape a label for use inside a Mermaid ``["..."]`` node.

    Double quotes are the node-label delimiter, so any quote in the title is
    replaced with ``&quot;`` (Mermaid renders HTML entities in labels).
    Newlines are collapsed so a multi-line title never breaks the one-node
    one-line invariant the renderer relies on.
    """
    return text.replace('"', "&quot;").replace("\n", " ").replace("\r", " ")


def render_mermaid(nodes: list[_Node], edges: list[tuple[str, str]]) -> str:
    """Render (nodes, edges) into a deterministic Mermaid ``graph LR`` block.

    Layout:

    * Header ``graph LR``.
    * One line per node: ``T001["T001<br/>title<br/>(status)"]`` using a
      quoted label so titles with spaces/punctuation are always valid.
    * One line per edge: ``T001 --> T002``.
    * ``classDef`` lines for every status colour, then one ``class`` line per
      node assigning its status class.

    An empty graph still returns a valid (renderable) diagram: just the
    ``graph LR`` header with a single placeholder comment, so ``--format
    mermaid`` on a fresh project never emits a syntactically broken block.
    """
    lines: list[str] = ["graph LR"]

    if not nodes:
        # A header-only Mermaid graph is invalid in some strict parsers; emit
        # a harmless comment node-free body via a Mermaid comment line.
        lines.append("  %% no tasks in scope")
        return "\n".join(lines)

    for n in nodes:
        nid = _mermaid_node_id(n.id)
        label = f"{n.id}<br/>{_escape_label(n.title)}<br/>({n.status})"
        lines.append(f'  {nid}["{label}"]')

    for src, dst in edges:
        lines.append(f"  {_mermaid_node_id(src)} --> {_mermaid_node_id(dst)}")

    # classDef block — fixed order for determinism.
    for class_name, style in _CLASS_DEFS:
        lines.append(f"  classDef {class_name} {style};")

    # Assign each node its status class (skip statuses with no mapping).
    for n in nodes:
        status_class = _STATUS_CLASSES.get(n.status)
        if status_class is not None:
            lines.append(f"  class {_mermaid_node_id(n.id)} {status_class};")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Human-readable text summary
# ---------------------------------------------------------------------------


def _print_text_summary(
    nodes: list[_Node],
    edges: list[tuple[str, str]],
    ready_to_claim: list[str],
) -> None:
    """Print a short, non-diagram summary for the bare ``graph`` invocation."""
    if not nodes:
        typer.echo("No tasks in scope.")
        typer.echo("Run `fakoli-state graph --format mermaid` for a diagram.")
        return

    typer.echo(f"Task graph: {len(nodes)} node(s), {len(edges)} edge(s).")
    typer.echo("")
    by_status: dict[str, int] = {}
    for n in nodes:
        by_status[n.status] = by_status.get(n.status, 0) + 1
    typer.echo("By status:")
    for status, count in sorted(by_status.items()):
        typer.echo(f"  {status}: {count}")
    typer.echo("")
    if ready_to_claim:
        typer.echo(f"Ready to claim: {', '.join(ready_to_claim)}")
    else:
        typer.echo("Ready to claim: (none)")
    typer.echo("")
    typer.echo("Render a diagram with `fakoli-state graph --format mermaid`.")
