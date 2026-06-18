"""anvil CLI package.

Assembles the Typer app from per-command modules.  Each module owns its
command bodies verbatim; this file is the wiring layer only.
"""

from __future__ import annotations

import typer

from anvil import __version__
from anvil.cli.claim import claim, next, release, renew
from anvil.cli.conflicts import conflicts
from anvil.cli.describe import describe
from anvil.cli.doctor import doctor
from anvil.cli.drift import drift
from anvil.cli.graph import graph
from anvil.cli.hooks import hook_app
from anvil.cli.init_status import init, status
from anvil.cli.mcp_config import mcp_config
from anvil.cli.migrate import migrate_app, migrate_events
from anvil.cli.packet_apply import apply, packet, submit
from anvil.cli.plan import (
    deps,
    expand,
    list_tasks,
    plan,
    review_app,
    score,
    show,
)
from anvil.cli.prd import prd_app
from anvil.cli.backup import backup, restore
from anvil.cli.replay import replay
from anvil.cli.scan import scan
from anvil.cli.sync import sync_app

# ---------------------------------------------------------------------------
# Root application
# ---------------------------------------------------------------------------

app = typer.Typer(
    name="anvil",
    help=(
        "Local-first project state engine: turn rough ideas and PRDs into reviewed, "
        "lockable, evidence-backed work packets that humans and AI agents can "
        "coordinate on without conflicts."
    ),
    no_args_is_help=True,
)

# ---------------------------------------------------------------------------
# Sub-apps
# ---------------------------------------------------------------------------

app.add_typer(prd_app, name="prd")
app.add_typer(review_app, name="review")
app.add_typer(hook_app, name="hook")
app.add_typer(sync_app, name="sync")
app.add_typer(migrate_app, name="migrate")

# ---------------------------------------------------------------------------
# --version callback
# ---------------------------------------------------------------------------


@app.callback(invoke_without_command=True)
def main(
    version: bool = typer.Option(  # noqa: B008
        False,
        "--version",
        "-V",
        help="Print the version and exit.",
        is_eager=True,
    ),
) -> None:
    """anvil — local-first project state engine."""
    if version:
        # Report engine version AND the SQLite schema version (T012): a host
        # pinning behaviour needs both — ``__version__`` identifies the build,
        # ``schema N`` identifies the on-disk state format the engine speaks.
        # The first token stays ``anvil {__version__}`` for backward
        # compatibility with existing parsers / tests.
        from anvil.state.schema import get_schema_version

        typer.echo(f"anvil {__version__} (schema {get_schema_version()})")
        raise typer.Exit()


# ---------------------------------------------------------------------------
# Register top-level commands
# ---------------------------------------------------------------------------

app.command()(init)
app.command()(status)
app.command()(describe)
app.command("mcp-config")(mcp_config)
app.command()(scan)
app.command()(drift)
app.command()(graph)
app.command()(conflicts)
app.command()(doctor)
app.command()(plan)
app.command()(score)
app.command()(expand)
app.command()(deps)
app.command("list")(list_tasks)
app.command()(show)
app.command()(claim)
app.command()(release)
app.command()(renew)
app.command()(next)
app.command()(packet)
app.command()(submit)
app.command()(apply)
app.command()(replay)
app.command()(backup)
app.command()(restore)
app.command("migrate-events")(migrate_events)

# ---------------------------------------------------------------------------
# Module entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app()
