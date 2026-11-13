"""Short native commands for owner-configured harness execution."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import typer

from anvil.cli._helpers import _resolve_project_dir, resolve_actor
from anvil.cli._json import JSON_OPTION, emit_success, fail_with
from anvil.config import (
    confirm_execution_adapter_upgrade,
    execution_config_mapping,
    execution_file_bytes,
    global_config_path,
    preview_execution_adapter_upgrade,
    register_execution_adapter,
    write_execution_adapter_config,
)
from anvil.execution import (
    ExecutionError,
    RunOptions,
    confirm_continue,
    confirm_run,
    inspect_execution_preview,
    inspect_run,
    preview_continue,
    preview_run,
    read_execution_capabilities,
    refresh_execution_capabilities,
)


def run_configure(
    script: Path | None = typer.Option(None, "--script"),  # noqa: B008
    interpreter: Path | None = typer.Option(None, "--interpreter"),  # noqa: B008
    profile: Path | None = typer.Option(None, "--profile"),  # noqa: B008
    write: bool = typer.Option(False, "--write"),  # noqa: B008
    upgrade: bool = typer.Option(False, "--upgrade"),  # noqa: B008
    preview: bool = typer.Option(False, "--preview"),  # noqa: B008
    confirm: str | None = typer.Option(None, "--confirm"),  # noqa: B008
    json_output: bool = JSON_OPTION,
) -> None:
    """Register once, or explicitly preview and confirm a pinned adapter upgrade."""
    try:
        if upgrade:
            if write or preview == (confirm is not None):
                raise ValueError("Upgrade requires --preview or --confirm PREVIEW; never --write")
            if confirm is not None and any(
                value is not None for value in (script, interpreter, profile)
            ):
                raise ValueError("Candidate installation paths belong to the upgrade preview")
            result = (
                preview_execution_adapter_upgrade(script, interpreter, profile)
                if preview else confirm_execution_adapter_upgrade(confirm)
            )
            if json_output:
                emit_success("run-configure", result)
            elif preview:
                typer.echo(
                    f"Upgrade preview {result['preview_id']} expires {result['expires_at']}."
                )
                typer.echo(f"Expected registration: {result['expected_registration_sha256']}")
                typer.echo(f"Configuration bytes: {result['config_sha256']}")
                for label in ("previous", "candidate"):
                    wrapper = result[label]
                    registration = wrapper["registration"]
                    typer.echo(
                        f"{label.title()}: {registration['adapter_id']} {registration['version']} "
                        f"contract {registration['contract_version']}"
                    )
                    typer.echo(f"  Interpreter: {registration['interpreter']} "
                               f"sha256={wrapper['interpreter_sha256']}")
                    typer.echo(f"  Script: {registration['script']} "
                               f"sha256={registration['code_sha256']['script']}")
                    typer.echo(f"  Extension sha256: {registration['code_sha256']['extension']}")
                    typer.echo(f"  Profile: {registration['profile']} "
                               f"sha256={registration['profile_sha256']}")
                typer.echo(
                    f"Confirm: anvil run-configure --upgrade --confirm {result['preview_id']}"
                )
            else:
                typer.echo(
                    "Execution adapter upgraded."
                    if result["changed"] else "Registration unchanged."
                )
            return
        if preview or confirm is not None:
            raise ValueError("Use --upgrade for a registration upgrade preview or confirmation")
        if any(value is None for value in (script, interpreter, profile)):
            raise ValueError("Initial registration requires --script, --interpreter and --profile")
        registration = register_execution_adapter(script, interpreter, profile)
        path = global_config_path()
        original = execution_file_bytes(path, 1024 * 1024, missing_ok=True) or b""
        existing = execution_config_mapping(original)
        data = dataclasses.asdict(registration)
        previous = existing.get("execution_adapter")
        if previous is not None and previous != data:
            raise ValueError(
                "A different execution adapter is registered; review it before replacing it"
            )
        changed = previous is None
        if write and changed:
            write_execution_adapter_config(path, registration, original)
        result = {
            "changed": changed,
            "written": write and changed,
            "registration": data,
            "runtime_qualified": False,
        }
        if json_output:
            emit_success("run-configure", result)
        else:
            typer.echo(
                "Execution adapter registered."
                if write
                else "Registration preview; use --write to save."
            )
    except (ValueError, OSError) as error:
        fail_with("run-configure", str(error), code=getattr(error, "code", "invalid_registration"))


def _print_execution_preview(result: dict, confirm_command: str) -> None:
    typer.echo(f"Preview {result['preview_id']} expires {result['expires_at']}.")
    typer.echo(f"Task: {result['task_id']}  Actor: {result['actor']}")
    typer.echo(f"Source: {result['source_sha']}")
    typer.echo(
        f"Budget: {result['wall_seconds']} seconds for the adapter, "
        "plus 180 seconds for checks/cleanup."
    )
    options = result["options"]
    typer.echo(
        "Model: "
        + (
            f"{options['provider']}/{options['model']} (explicit)"
            if "model" in options
            else "configured default"
        )
    )
    typer.echo(
        "Thinking: "
        + (
            f"{options['thinking']} (explicit)"
            if "thinking" in options
            else "configured default"
        )
    )
    typer.echo("Writable files:")
    for path in result["writable_paths"]:
        typer.echo(f"  {path}")
    typer.echo("Literal verification commands:")
    for command in result["commands"]:
        typer.echo(f"  {command}")
    typer.echo(f"Confirm: {confirm_command}")


def run(
    task_id: str = typer.Argument(...),  # noqa: B008
    preview: bool = typer.Option(False, "--preview"),  # noqa: B008
    confirm: str | None = typer.Option(None, "--confirm"),  # noqa: B008
    provider: str | None = typer.Option(None, "--provider"),  # noqa: B008
    model: str | None = typer.Option(None, "--model"),  # noqa: B008
    thinking: str | None = typer.Option(None, "--thinking"),  # noqa: B008
    wall_seconds: int | None = typer.Option(None, "--wall-seconds"),  # noqa: B008
    actor: str | None = typer.Option(None, "--actor"),  # noqa: B008
    cwd: Path | None = typer.Option(None, "--cwd", hidden=True),  # noqa: B008
    json_output: bool = JSON_OPTION,
) -> None:
    """Preview a ready task, then confirm that exact request to dispatch Run."""
    try:
        if preview == (confirm is not None):
            raise ExecutionError("bad_request", "Choose --preview or --confirm PREVIEW")
        if confirm is not None and any(
            value is not None for value in (provider, model, thinking, wall_seconds)
        ):
            raise ExecutionError(
                "bad_request", "Run options belong to the preview, not confirmation"
            )
        directory = _resolve_project_dir(cwd)
        owner = resolve_actor(actor)
        if preview:
            result = preview_run(
                directory,
                task_id,
                owner,
                RunOptions(provider, model, thinking),
                wall_seconds if wall_seconds is not None else 600,
            )
        else:
            result = confirm_run(directory, task_id, owner, confirm)
        if json_output:
            emit_success("run", result)
        elif preview:
            _print_execution_preview(
                result, f"anvil run {task_id} --confirm {result['preview_id']}"
            )
        else:
            typer.echo(
                f"Run {result['run_id']} dispatched. Inspect: anvil run-status {result['run_id']}"
            )
    except (ValueError, OSError) as error:
        fail_with("run", str(error), code=getattr(error, "code", "execution_refused"))


def continue_run(
    run_id: str = typer.Argument(...),  # noqa: B008
    preview: bool = typer.Option(False, "--preview"),  # noqa: B008
    confirm: str | None = typer.Option(None, "--confirm"),  # noqa: B008
    task_id: str | None = typer.Option(None, "--task"),  # noqa: B008
    provider: str | None = typer.Option(None, "--provider"),  # noqa: B008
    model: str | None = typer.Option(None, "--model"),  # noqa: B008
    thinking: str | None = typer.Option(None, "--thinking"),  # noqa: B008
    wall_seconds: int | None = typer.Option(None, "--wall-seconds"),  # noqa: B008
    actor: str | None = typer.Option(None, "--actor"),  # noqa: B008
    cwd: Path | None = typer.Option(None, "--cwd", hidden=True),  # noqa: B008
    json_output: bool = JSON_OPTION,
) -> None:
    """Preview and confirm fresh work in an exact saved conversation."""
    try:
        if preview == (confirm is not None):
            raise ExecutionError("bad_request", "Choose --preview or --confirm PREVIEW")
        if confirm is not None and any(
            value is not None for value in (task_id, provider, model, thinking, wall_seconds)
        ):
            raise ExecutionError("bad_request", "Continue options belong to its preview")
        directory = _resolve_project_dir(cwd)
        owner = resolve_actor(actor)
        result = (
            preview_continue(directory, run_id, owner, RunOptions(provider, model, thinking),
                             wall_seconds if wall_seconds is not None else 600, task_id=task_id)
            if preview else confirm_continue(directory, run_id, owner, confirm)
        )
        if json_output:
            emit_success("continue", result)
        elif preview:
            typer.echo(f"Continue from: {run_id}")
            _print_execution_preview(
                result, f"anvil continue {run_id} --confirm {result['preview_id']}"
            )
        else:
            typer.echo(
                f"Run {result['run_id']} dispatched. Inspect: anvil run-status {result['run_id']}"
            )
    except (ValueError, OSError) as error:
        fail_with("continue", str(error), code=getattr(error, "code", "execution_refused"))


def run_status(
    run_id: str | None = typer.Argument(None),  # noqa: B008
    preview: str | None = typer.Option(None, "--preview"),  # noqa: B008
    actor: str | None = typer.Option(None, "--actor"),  # noqa: B008
    cwd: Path | None = typer.Option(None, "--cwd", hidden=True),  # noqa: B008
    json_output: bool = JSON_OPTION,
) -> None:
    """Inspect retained execution status without changing native task state."""
    try:
        if (run_id is None) == (preview is None):
            raise ExecutionError("bad_request", "Choose RUN_ID or --preview PREVIEW")
        if run_id is not None and actor is not None:
            raise ExecutionError("bad_request", "--actor belongs to exact preview inspection")
        directory = _resolve_project_dir(cwd)
        result = (inspect_execution_preview(directory, preview, resolve_actor(actor))
                  if preview is not None else inspect_run(directory, run_id))
        if json_output:
            emit_success("run-status", result)
        else:
            typer.echo(f"{run_id or preview}: {result['state']} (acceptance not granted)")
            if preview is not None and result.get("run_id"):
                typer.echo(f"Inspect: anvil run-status {result['run_id']}")
    except (ValueError, OSError) as error:
        fail_with("run-status", str(error), code=getattr(error, "code", "execution_refused"))


def run_capabilities(
    refresh: bool = typer.Option(False, "--refresh"),  # noqa: B008
    provider: str | None = typer.Option(None, "--provider"),  # noqa: B008
    model: str | None = typer.Option(None, "--model"),  # noqa: B008
    cwd: Path | None = typer.Option(None, "--cwd", hidden=True),  # noqa: B008
    json_output: bool = JSON_OPTION,
) -> None:
    """Read verified model choices, or explicitly dispatch a no-prompt refresh."""
    try:
        RunOptions(provider, model)
        if not refresh and (provider is not None or model is not None):
            raise ExecutionError("bad_request", "Model selection requires --refresh")
        directory = _resolve_project_dir(cwd)
        result = (
            refresh_execution_capabilities(directory, provider=provider, model=model)
            if refresh
            else read_execution_capabilities(directory)
        )
        if json_output:
            emit_success("run-capabilities", result)
        elif refresh:
            typer.echo(f"Metadata refresh {result['run_id']} dispatched; use anvil run-status.")
        else:
            typer.echo(f"Capabilities: {result['status']}. Use --json for verified choices.")
    except (ValueError, OSError) as error:
        fail_with(
            "run-capabilities", str(error), code=getattr(error, "code", "capabilities_refused")
        )
