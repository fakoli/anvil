"""Direct, ledger-backed task moves for agents and operators."""

from enum import StrEnum
from pathlib import Path

import typer
from pydantic import ValidationError

from anvil.actors import ActorIdentityError, canonicalize_new_actor
from anvil.cli._helpers import _open_backend, _require_state_dir, _resolve_state_dir
from anvil.cli._json import JSON_OPTION, emit_success, fail_with
from anvil.state.backend import EventRejected
from anvil.task_actions import transition_task


class TaskAction(StrEnum):
    ready = "ready"
    backlog = "backlog"
    start = "start"
    block = "block"
    resume = "resume"
    close = "close"


def task_action(
    task_id: str = typer.Argument(..., help="Canonical task ID (PRD:T001 for a named campaign)."),
    action: TaskAction = typer.Argument(...),  # noqa: B008
    actor: str = typer.Option(..., "--actor", help="Exact owner or operator identity."),
    reason: str | None = typer.Option(None, "--reason"),
    resolution: str | None = typer.Option(None, "--resolution", help="Non-success close reason."),
    duplicate_of: str | None = typer.Option(
        None, "--duplicate-of", help="Canonical duplicate target."
    ),
    json_output: bool = JSON_OPTION,
    cwd: Path | None = typer.Option(None, "--cwd", hidden=True),  # noqa: B008
) -> None:
    """Apply an allowed move directly, with custody checks and an atomic ledger event.

    No browser session or preview round trip is needed. Claim/submit/apply remain
    the paths for acquiring work, recording evidence and human approval.
    """
    state_dir = _resolve_state_dir(cwd)
    _require_state_dir(state_dir, command="task-action", json_output=json_output)
    try:
        actor = canonicalize_new_actor(actor)
    except ActorIdentityError as error:
        fail_with("task-action", str(error), code="invalid_actor")
    backend = _open_backend(state_dir, project_root=cwd or Path.cwd())
    try:
        task = backend.get_task(task_id)
        if task is None:
            fail_with("task-action", "Task not found.", code="not_found")
        result = transition_task(
            backend,
            task_id,
            action,
            actor,
            expected_status=task.status.value,
            expected_updated_at=task.updated_at,
            reason=reason,
            resolution=resolution,
            duplicate_of=duplicate_of,
        )
        if json_output:
            emit_success(
                "task-action",
                {
                    "task_id": result.id,
                    "status": result.status.value,
                    "task": result.model_dump(mode="json"),
                },
            )
        else:
            typer.echo(f"{result.id}: {result.status.value}")
    except (EventRejected, ValidationError) as error:
        fail_with("task-action", str(error), code="transition_refused")
    finally:
        backend.close()
