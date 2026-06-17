"""fakoli-state CLI package.

Assembles the Typer app from per-command modules.  Each module owns its
command bodies verbatim; this file is the wiring layer only.
"""

from __future__ import annotations

import typer

from fakoli_state import __version__
from fakoli_state.cli.claim import claim, next, release, renew
from fakoli_state.cli.describe import describe
from fakoli_state.cli.doctor import doctor
from fakoli_state.cli.drift import drift
from fakoli_state.cli.graph import graph
from fakoli_state.cli.hooks import hook_app
from fakoli_state.cli.init_status import init, status
from fakoli_state.cli.migrate import migrate_app, migrate_events
from fakoli_state.cli.packet_apply import apply, packet, submit
from fakoli_state.cli.plan import expand, list_tasks, plan, review_app, score, show
from fakoli_state.cli.prd import prd_app
from fakoli_state.cli.replay import replay
from fakoli_state.cli.scan import scan
from fakoli_state.cli.sync import sync_app

# ---------------------------------------------------------------------------
# Root application
# ---------------------------------------------------------------------------

app = typer.Typer(
    name="fakoli-state",
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
    """fakoli-state — local-first project state engine."""
    if version:
        # Report engine version AND the SQLite schema version (T012): a host
        # pinning behaviour needs both — ``__version__`` identifies the build,
        # ``schema N`` identifies the on-disk state format the engine speaks.
        # The first token stays ``fakoli-state {__version__}`` for backward
        # compatibility with existing parsers / tests.
        from fakoli_state.state.schema import get_schema_version

        typer.echo(f"fakoli-state {__version__} (schema {get_schema_version()})")
        raise typer.Exit()


# ---------------------------------------------------------------------------
# Register top-level commands
# ---------------------------------------------------------------------------

app.command()(init)
app.command()(status)
app.command()(describe)
app.command()(scan)
app.command()(drift)
app.command()(graph)
app.command()(doctor)
app.command()(plan)
app.command()(score)
app.command()(expand)
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
app.command("migrate-events")(migrate_events)

# ---------------------------------------------------------------------------
# Module entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app()
