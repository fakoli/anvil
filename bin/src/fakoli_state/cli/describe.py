"""``fakoli-state describe`` — self-describing command surface (backlog T012).

Emits a machine-readable manifest of the engine's *public surface* so a host
(Codex/Cursor/CI/another agent) can discover what the tool can do without
parsing ``--help`` text or hardcoding a tool list:

* every CLI subcommand (leaf commands, including grouped ones like
  ``prd parse``);
* every FastMCP tool name;
* the engine version, the SQLite schema version, and a **stable
  ``api_version``** that consumers can pin against.

Why a stable ``api_version``
----------------------------
``__version__`` bumps on every release (often metadata-only). The shape of the
*command surface* — which CLI commands and MCP tools exist and the envelope
they speak — changes far less often. ``api_version`` is the contract a
non-Claude host pins to: it only changes when the surface changes in a way
consumers must react to (a command/tool added, renamed, or removed; the
envelope shape changing). Bumping ``__version__`` for a docs fix does NOT bump
``api_version``.

Drift guard
-----------
The whole point of T012 is that the *described* surface cannot silently drift
from the *registered* surface. ``describe`` does not hand-maintain a list — it
introspects the live Typer app (via ``typer.main.get_command``) and the live
FastMCP instance (via ``mcp.list_tools()``) at call time, so the manifest is
always generated from the same objects the CLI and MCP server actually expose.
A test (``tests/test_cli.py::TestDescribe``) asserts the two agree, so CI fails
if a command/tool is added or renamed without the surface staying coherent.

Output
------
``describe`` is inherently machine-readable, so its DEFAULT output is the same
``{"ok": true, "command": "describe", "data": {...}}`` envelope every other
command emits under ``--json`` (one compact line to stdout, pipeable into
``jq``). ``--json`` is accepted for symmetry with the rest of the CLI and is a
no-op (the output is already the envelope). ``--human`` prints a short readable
summary instead. ``describe`` needs no project — it never opens a backend and
works from any directory, even before ``init``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import typer

from fakoli_state import __version__
from fakoli_state.cli._json import emit_success
from fakoli_state.state.schema import get_schema_version

if TYPE_CHECKING:
    import click

__all__ = ["API_VERSION", "build_manifest", "describe"]

_COMMAND = "describe"

# ---------------------------------------------------------------------------
# Stable API version
# ---------------------------------------------------------------------------
#
# Bump this ONLY when the externally observable command surface changes in a
# way consumers must react to: a CLI command or MCP tool is added, renamed, or
# removed, or the --json envelope shape changes. Do NOT bump it for a plain
# ``__version__`` release (bug fix, docs, internals) that leaves the surface
# identical. Consumers pin on ``api_version``; ``engine_version`` tells them the
# exact build.
API_VERSION = "1"


def describe(
    human: bool = typer.Option(  # noqa: B008
        False,
        "--human",
        help="Print a short human-readable summary instead of the JSON envelope.",
    ),
    json_output: bool = typer.Option(  # noqa: B008
        False,
        "--json",
        help=(
            "Emit the machine-readable JSON envelope (the default for "
            "`describe`; accepted for symmetry with other commands)."
        ),
    ),
) -> None:
    """Emit a machine-readable manifest of the CLI/MCP command surface.

    Reports the engine version, the SQLite schema version, a stable
    ``api_version`` consumers can pin to, every CLI subcommand, and every MCP
    tool name. Introspects the live Typer app and FastMCP instance, so the
    manifest never drifts from what is actually registered. Needs no project —
    works from any directory, even before ``init``.
    """
    manifest = build_manifest()

    # --human is the only non-default rendering; --json is the (already-default)
    # envelope, so it is a harmless no-op kept for flag symmetry.
    if human and not json_output:
        _print_human(manifest)
        return

    emit_success(_COMMAND, manifest)


# ---------------------------------------------------------------------------
# Manifest construction (introspection — no hand-maintained lists)
# ---------------------------------------------------------------------------


def build_manifest() -> dict[str, Any]:
    """Build the describe manifest from the live CLI and MCP surfaces.

    Returns a JSON-safe dict::

        {
          "api_version": "1",
          "engine_version": "1.28.0",
          "schema_version": 4,
          "envelope": "v1.24",
          "cli": {"commands": ["apply", ..., "prd parse", ...], "count": 29},
          "mcp": {"tools": ["claim_task", ...], "count": 22}
        }

    Both lists are sorted for stable, diffable output. ``cli.commands`` are leaf
    commands with grouped commands rendered space-joined (``"prd parse"``) so a
    consumer sees the exact invocation path.
    """
    # Compute each list exactly once so ``count`` can never disagree with the
    # list it counts.
    cli_commands = cli_command_names()
    mcp_tools = mcp_tool_names()
    return {
        "api_version": API_VERSION,
        "engine_version": __version__,
        "schema_version": get_schema_version(),
        # The CLI/MCP wire contract these commands speak. v1.24 is the
        # ``{"ok", "command", "data"/"error"}`` envelope + FAKOLI_STATE_ROOT.
        "envelope": "v1.24",
        "cli": {
            "commands": cli_commands,
            "count": len(cli_commands),
        },
        "mcp": {
            "tools": mcp_tools,
            "count": len(mcp_tools),
        },
    }


def cli_command_names() -> list[str]:
    """Return every leaf CLI command path, sorted.

    Introspects the live Typer ``app`` via ``typer.main.get_command`` (the same
    resolution Typer uses at runtime, so hyphenation and group names match the
    real invocation) and walks the click command tree. Grouped commands are
    rendered as their full invocation path joined by spaces, e.g. ``prd parse``,
    ``sync github``. The root ``describe`` command itself is included.
    """
    import click
    from typer.main import get_command

    from fakoli_state.cli import app

    root = get_command(app)
    names = _walk_click_group(root, prefix="") if isinstance(root, click.Group) else []
    return sorted(names)


def _walk_click_group(group: click.Group, prefix: str) -> list[str]:
    """Recursively collect leaf command paths under *group*."""
    import click

    out: list[str] = []
    for name, sub in group.commands.items():
        full = f"{prefix}{name}"
        if isinstance(sub, click.Group):
            out.extend(_walk_click_group(sub, prefix=full + " "))
        else:
            out.append(full)
    return out


def mcp_tool_names() -> list[str]:
    """Return every registered FastMCP tool name, sorted.

    Imported lazily so the CLI does not pull in ``fastmcp`` unless ``describe``
    is actually run. ``mcp.list_tools()`` is async; this helper drives it to
    completion whether or not an event loop is already running:

    * from the CLI (``describe`` is a plain sync Typer command, no loop) it uses
      ``asyncio.run``;
    * from *inside* the MCP server's ``describe_surface`` tool (a loop IS
      running) ``asyncio.run`` would raise ``RuntimeError``, so the coroutine is
      run to completion on a throwaway loop in a worker thread.

    Either way the same registry is introspected, so both surfaces report the
    identical tool list.
    """
    from fakoli_state.mcp_server import mcp

    tools = _run_coro_blocking(mcp.list_tools())
    return sorted(t.name for t in tools)


def _run_coro_blocking(coro: Any) -> Any:
    """Run *coro* to completion from sync code, loop-running or not."""
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No loop running — the common CLI path.
        return asyncio.run(coro)

    # A loop is already running (we are inside an MCP tool call). Spin a fresh
    # loop in a worker thread so we never re-enter the running one.
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


# ---------------------------------------------------------------------------
# Human-readable rendering
# ---------------------------------------------------------------------------


def _print_human(manifest: dict[str, Any]) -> None:
    """Print a compact readable summary of the manifest."""
    typer.echo(f"fakoli-state describe (api_version {manifest['api_version']})")
    typer.echo(f"  engine_version: {manifest['engine_version']}")
    typer.echo(f"  schema_version: {manifest['schema_version']}")
    typer.echo(f"  envelope:       {manifest['envelope']}")
    typer.echo("")
    typer.echo(f"CLI commands ({manifest['cli']['count']}):")
    for name in manifest["cli"]["commands"]:
        typer.echo(f"  {name}")
    typer.echo("")
    typer.echo(f"MCP tools ({manifest['mcp']['count']}):")
    for name in manifest["mcp"]["tools"]:
        typer.echo(f"  {name}")
