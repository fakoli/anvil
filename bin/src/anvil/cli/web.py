"""Native read-only web interface; enrollment never depends on launch cwd."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer

from anvil.cli._json import JSON_OPTION, emit_success, fail_with
from anvil.web import (
    WebError,
    catalog,
    default_config_path,
    enroll_repository,
    load_config,
    make_server,
    save_config,
)

web_app = typer.Typer(
    help="Read-only machine, repository and campaign interface.", invoke_without_command=True
)


@web_app.callback()
def web(
    context: typer.Context,
    config: Path | None = typer.Option(  # noqa: B008
        None, "--config", help="Owner-private web enrollment file."
    ),
    port: int | None = typer.Option(
        None, "--port", min=1, max=65535, help="Override configured loopback port."
    ),
) -> None:
    """Start the loopback viewer; explicit subcommands manage presentation enrollment."""
    context.ensure_object(dict)
    context.obj["web_config"] = config or default_config_path()
    if context.invoked_subcommand is None:
        try:
            server = make_server(context.obj["web_config"], port)
        except (OSError, WebError) as error:
            typer.echo(f"Cannot start Anvil web: {error}", err=True)
            raise typer.Exit(1) from error
        typer.echo(f"Anvil web: http://127.0.0.1:{server.server_port}/")
        typer.echo(f"Enrollment: {context.obj['web_config']}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


@web_app.command("enroll")
def enroll(
    context: typer.Context,
    root: Path = typer.Argument(  # noqa: B008
        ..., help="Existing Anvil project repository."
    ),
    label: str | None = typer.Option(None, "--label", help="Repository label in navigation."),
    reader_argv: str | None = typer.Option(
        None,
        "--reader-argv",
        help="Explicit private JSON argv for a compatible snapshot reader; no shell.",
    ),
    reader_label: str | None = typer.Option(
        None, "--reader-label", help="Configured reader/version/source provenance label."
    ),
    json_output: bool = JSON_OPTION,
) -> None:
    """Enroll one existing project; no task state is initialized or migrated."""
    try:
        argv = json.loads(reader_argv) if reader_argv is not None else None
        if reader_argv is not None and (not isinstance(argv, list) or not argv):
            raise WebError("invalid_reader", "Explicit reader argv must be a nonempty JSON list")
        repository = enroll_repository(context.obj["web_config"], root, label, argv, reader_label)
    except (ValueError, OSError) as error:
        fail_with("web enroll", str(error), code=getattr(error, "code", "invalid_enrollment"))
    if json_output:
        emit_success("web enroll", {"repository": repository.model_dump()})
    else:
        typer.echo(f"Enrolled {repository.label}: {repository.id}")


@web_app.command("list")
def list_repositories(context: typer.Context, json_output: bool = JSON_OPTION) -> None:
    """List explicit local enrollment without opening any project state."""
    try:
        data = catalog(load_config(context.obj["web_config"]))
    except WebError as error:
        fail_with("web list", str(error), code=error.code)
    if json_output:
        emit_success("web list", data)
    else:
        typer.echo(f"Machine: {data['machine']['label']} (local)")
        for repository in data["repositories"]:
            typer.echo(f"{repository['id']}  {repository['label']}  {repository['root']}")


@web_app.command("remove")
def remove_repository(
    context: typer.Context, repository_id: str, json_output: bool = JSON_OPTION
) -> None:
    """Remove presentation enrollment only; canonical tasks/evidence remain intact."""
    try:
        config = load_config(context.obj["web_config"])
        matches = [item for item in config.repositories if item.id == repository_id]
        if not matches:
            raise WebError("repository_not_found", "Repository is not enrolled")
        config.repositories = [item for item in config.repositories if item.id != repository_id]
        save_config(context.obj["web_config"], config)
    except (ValueError, OSError) as error:
        fail_with("web remove", str(error), code=getattr(error, "code", "invalid_config"))
    if json_output:
        emit_success("web remove", {"repository_id": repository_id, "removed": True})
    else:
        typer.echo(f"Removed enrollment {repository_id}; task state unchanged")


def main() -> None:
    """Dedicated installed entrypoint, without replacing an older global CLI."""
    from anvil.cli import app

    app(args=["web", *sys.argv[1:]])
