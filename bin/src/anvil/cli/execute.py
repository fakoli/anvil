"""Short native commands for owner-configured harness execution."""

from __future__ import annotations

import dataclasses
import os
import uuid
from pathlib import Path

import typer
import yaml

from anvil.cli._helpers import _resolve_project_dir, resolve_actor
from anvil.cli._json import JSON_OPTION, emit_success, fail_with
from anvil.config import (
    execution_config_mapping,
    execution_file_bytes,
    global_config_path,
    register_execution_adapter,
)
from anvil.execution import (
    ExecutionError,
    RunOptions,
    confirm_run,
    inspect_run,
    preview_run,
    private_directory,
    read_execution_capabilities,
    refresh_execution_capabilities,
    sync_directory,
)


def run_configure(
    script: Path = typer.Option(..., "--script"),  # noqa: B008
    interpreter: Path = typer.Option(..., "--interpreter"),  # noqa: B008
    profile: Path = typer.Option(..., "--profile"),  # noqa: B008
    write: bool = typer.Option(False, "--write"),  # noqa: B008
    json_output: bool = JSON_OPTION,
) -> None:
    """Register trusted coordination v2 once; dry-run unless --write."""
    try:
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
        candidate = {**existing, "execution_adapter": data}
        appended = original + (b"\n" if original and not original.endswith(b"\n") else b"")
        appended += yaml.safe_dump({"execution_adapter": data}, sort_keys=True).encode()
        try:
            preserve_bytes = yaml.safe_load(appended) == candidate
        except yaml.YAMLError:
            preserve_bytes = False
        raw = appended if preserve_bytes else yaml.safe_dump(candidate, sort_keys=False).encode()
        if yaml.safe_load(raw) != candidate:
            raise ValueError("Execution registration cannot preserve the owner configuration")
        if write and changed:
            private_directory(path.parent)
            # Newly created intermediate directories inherit the caller's umask.
            # Recheck the complete prefix before any lock/configuration write.
            if (execution_file_bytes(path, 1024 * 1024, missing_ok=True) or b"") != original:
                raise ValueError("Global configuration changed during registration")
            lock = path.with_name(path.name + ".execution-registration.lock")
            with lock.open("xb") as guard:
                os.chmod(lock, 0o600)
                guard.write(b"registration\n")
                guard.flush()
                os.fsync(guard.fileno())
            try:
                if (execution_file_bytes(path, 1024 * 1024, missing_ok=True) or b"") != original:
                    raise ValueError("Global configuration changed during registration")
                if original:
                    backup = path.with_name(path.name + ".before-execution-" + uuid.uuid4().hex)
                    with backup.open("xb") as stream:
                        os.chmod(backup, 0o600)
                        stream.write(original)
                        stream.flush()
                        os.fsync(stream.fileno())
                temporary = path.with_name(path.name + ".execution-" + uuid.uuid4().hex)
                with temporary.open("xb") as stream:
                    os.chmod(temporary, 0o600)
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                if (execution_file_bytes(path, 1024 * 1024, missing_ok=True) or b"") != original:
                    raise ValueError("Global configuration changed before registration publication")
                os.replace(temporary, path)
                sync_directory(path.parent)
            finally:
                lock.unlink()
                sync_directory(path.parent)
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
            typer.echo(f"Confirm: anvil run {task_id} --confirm {result['preview_id']}")
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
    json_output: bool = JSON_OPTION,
) -> None:
    """Reserved saved-session continuation; never starts a new conversation."""
    fail_with(
        "continue",
        "The registered adapter does not yet support qualified saved-session continuation.",
        code="continuation_unsupported",
    )


def run_status(
    run_id: str = typer.Argument(...),  # noqa: B008
    cwd: Path | None = typer.Option(None, "--cwd", hidden=True),  # noqa: B008
    json_output: bool = JSON_OPTION,
) -> None:
    """Inspect retained execution status without changing native task state."""
    try:
        result = inspect_run(_resolve_project_dir(cwd), run_id)
        if json_output:
            emit_success("run-status", result)
        else:
            typer.echo(f"{run_id}: {result['state']} (acceptance not granted)")
    except (ValueError, OSError) as error:
        fail_with("run-status", str(error), code=getattr(error, "code", "execution_refused"))


def run_capabilities(
    refresh: bool = typer.Option(False, "--refresh"),  # noqa: B008
    cwd: Path | None = typer.Option(None, "--cwd", hidden=True),  # noqa: B008
    json_output: bool = JSON_OPTION,
) -> None:
    """Read verified model choices, or explicitly dispatch a no-prompt refresh."""
    try:
        directory = _resolve_project_dir(cwd)
        result = (
            refresh_execution_capabilities(directory)
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
