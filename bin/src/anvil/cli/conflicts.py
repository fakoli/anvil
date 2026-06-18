"""``anvil conflicts`` — surface persisted conflict groups (file overlap).

CL-5 (depends on CL-4): conflict groups are computed during planning/inference
and persisted to the ``conflict_groups`` table by ``conflict_group.upserted``
events. This command reads them back and lists the file-overlap groupings among
tasks so a human (or agent) can see which tasks cannot be claimed concurrently
without a warning.

A conflict group is a named set of tasks whose ``likely_files`` overlap but
where neither is a strict subset of the other (a strict subset is treated as a
dependency, not a conflict — see ``planning/inference.py``). Claiming one task
in a group while another is active is allowed but warned (see
``ClaimManager._check_group_conflicts``).

Output formats
--------------
``text`` (default)
    A human-readable list: one block per group with its ID, the member task
    IDs, and the reason (which overlapping files triggered the grouping).

``--json`` / ``--format json``
    The v1.24 machine-readable envelope: ``{"ok": true, "command":
    "conflicts", "data": {"conflict_groups": [{"id", "name", "task_ids",
    "reason"}, ...], "count": N}}``. Mirrors the read-only envelope contract
    used by ``graph``/``status`` so a non-Claude host can parse it.

Read-only and deterministic: groups are returned in ID order, so the same
persisted state always yields byte-identical output.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from anvil.cli._helpers import (
    StateRootError,
    _open_backend,
    _require_state_dir,
    _resolve_state_dir,
)
from anvil.cli._json import JSON_OPTION, emit_success, fail

if TYPE_CHECKING:
    from anvil.state.models import ConflictGroup

__all__ = ["conflicts"]

_COMMAND = "conflicts"


def conflicts(
    fmt: str = typer.Option(  # noqa: B008
        "text",
        "--format",
        "-f",
        help="Output format: 'text' (default human list) or 'json'.",
    ),
    json_output: bool = JSON_OPTION,
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory. Defaults to the current working directory.",
        hidden=True,
    ),
) -> None:
    """List the persisted conflict groups (tasks whose likely_files overlap).

    Read-only and deterministic. Conflict groups are produced by planning
    inference and persisted to the ``conflict_groups`` table; this command
    reads them back. With ``--json`` (or ``--format json``) the v1.24 envelope
    is emitted instead of the human-readable list.
    """
    want_json = json_output or fmt == "json"

    # Pipe-safe state-root resolution (mirrors `graph`/`status`): a bad
    # ANVIL_ROOT under --json must still produce a parseable envelope.
    try:
        state_dir = _resolve_state_dir(cwd)
    except StateRootError as exc:
        if want_json:
            fail(_COMMAND, str(exc), code="state_root_invalid")
        raise
    _require_state_dir(state_dir, command=_COMMAND, json_output=want_json)

    if fmt not in {"text", "json"}:
        if want_json:
            fail(_COMMAND, f"unknown format '{fmt}'.", code="bad_request")
        typer.echo(f"Error: unknown format '{fmt}'.", err=True)
        raise typer.Exit(code=2)

    backend = _open_backend(state_dir)
    try:
        groups = backend.list_conflict_groups()
    finally:
        backend.close()

    if want_json:
        emit_success(
            _COMMAND,
            {
                "conflict_groups": [_group_to_json(g) for g in groups],
                "count": len(groups),
            },
        )
        return

    _print_text(groups)


def _group_to_json(g: ConflictGroup) -> dict[str, Any]:
    return {
        "id": g.id,
        "name": g.name,
        "task_ids": list(g.task_ids),
        "reason": g.reason,
    }


def _print_text(groups: list[ConflictGroup]) -> None:
    """Print a human-readable list of conflict groups."""
    if not groups:
        typer.echo("No conflict groups.")
        typer.echo(
            "Conflict groups are created by `anvil plan` when tasks' "
            "likely_files overlap."
        )
        return

    typer.echo(f"{len(groups)} conflict group(s):")
    typer.echo("")
    for g in groups:
        members = ", ".join(g.task_ids) if g.task_ids else "(none)"
        typer.echo(f"{g.id}: {members}")
        if g.reason:
            typer.echo(f"  {g.reason}")
        typer.echo("")
