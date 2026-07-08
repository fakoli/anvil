"""``anvil progress`` — record a structured progress phase for a task.

retro-opps:T011. The CLI twin of the MCP ``submit_progress`` tool: appends
one ``progress.noted`` audit event carrying an optional structured ``phase``
label plus free-text detail. Audit-only — task status never changes, no
claim is required. The read side (``anvil status`` / ``notify-digest``)
lands in T012.
"""

from __future__ import annotations

from pathlib import Path

import typer

from anvil.cli._helpers import (
    StateRootError,
    _open_backend,
    _require_state_dir,
    _resolve_state_dir,
    resolve_actor,
)
from anvil.cli._json import JSON_OPTION, emit_success, fail

_COMMAND = "progress"


def progress(
    task_id: str = typer.Argument(..., help="Task the progress note is for."),
    phase: str = typer.Argument(
        ...,
        help=(
            "Structured phase label for the heartbeat bus "
            '(e.g. "build", "tests", "review-fixes").'
        ),
    ),
    detail: str | None = typer.Option(  # noqa: B008
        None, "--detail", help="Free-text elaboration for the phase."
    ),
    actor: str | None = typer.Option(  # noqa: B008
        None,
        "--actor",
        help="Actor identity (default: $ANVIL_ACTOR / $USER derivation).",
    ),
    json_output: bool = JSON_OPTION,
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory. Defaults to the current working directory.",
        hidden=True,
    ),
) -> None:
    """Record a progress phase for TASK_ID as a ``progress.noted`` audit event.

    Does NOT change task status and does not require an active claim —
    mirrors the MCP ``submit_progress`` tool so agents and humans share one
    event shape. ``anvil status`` surfaces the latest phase per active claim
    (T012).
    """
    resolved_actor = resolve_actor(actor)

    try:
        state_dir = _resolve_state_dir(cwd)
    except StateRootError as exc:
        if json_output:
            fail(_COMMAND, str(exc), code="state_root_invalid")
        raise
    _require_state_dir(state_dir, command=_COMMAND, json_output=json_output)

    backend = _open_backend(state_dir)
    try:
        from anvil.clock import SystemClock
        from anvil.state.models import EventDraft

        task = backend.get_task(task_id)
        if task is None:
            if json_output:
                fail(
                    _COMMAND,
                    f"task '{task_id}' not found.",
                    code="task_not_found",
                )
            typer.echo(f"Error: task '{task_id}' not found.", err=True)
            raise typer.Exit(code=1)

        now = SystemClock().now()
        draft = EventDraft(
            timestamp=now,
            actor=resolved_actor,
            action="progress.noted",
            target_kind="task",
            target_id=task_id,
            payload_json={
                "task_id": task_id,
                "actor": resolved_actor,
                "notes": detail or phase,
                "noted_at": now.isoformat(),
                # Same omit-when-None discipline as the MCP tool (T010) —
                # here phase is always present by construction.
                "phase": phase,
                **({"detail": detail} if detail is not None else {}),
            },
        )
        backend.append(draft)
    finally:
        backend.close()

    if json_output:
        emit_success(
            _COMMAND,
            {
                "task_id": task_id,
                "actor": resolved_actor,
                "phase": phase,
                "detail": detail,
                "recorded": True,
            },
        )
        return
    detail_note = f" — {detail}" if detail else ""
    typer.echo(f"Progress recorded for '{task_id}': {phase}{detail_note}")
