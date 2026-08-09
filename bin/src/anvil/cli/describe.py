"""``anvil describe`` — self-describing command surface (backlog T012).

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

from collections.abc import Mapping
from typing import Any

import typer

from anvil import __version__
from anvil.build_identity import get_build_identity
from anvil.cli._json import emit_success
from anvil.state.schema import get_schema_version

__all__ = [
    "API_VERSION",
    "OPERATION_CATALOG_VERSION",
    "build_manifest",
    "describe",
    "provider_operation_catalog",
]

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
#
# Bumped to "10" when task offers and rejection responses added the complete
# accept-rate governor projection, and MCP ``get_next_task`` changed from a bare
# task/null to a typed task+governor envelope. Consumers of API 9 must
# deliberately accept the new response shapes.
API_VERSION = "13"

OPERATION_CATALOG_VERSION = 1
_PROVIDER_READ_RESOURCE_ROOT = "contracts/provider-reads/v1"

_SNAPSHOT_ERROR_CODES = (
    "dependency_cycle",
    "duplicate_edge",
    "invalid_hierarchy",
    "invalid_request",
    "limit_exceeded",
    "missing_target",
    "projection_not_converged",
    "schema_incompatible",
    "state_unavailable",
)
_CONTENT_ERROR_CODES = (
    "content_unavailable",
    "invalid_identifier",
    "invalid_request",
    "invalid_section",
    "invalid_utf8",
    "limit_exceeded",
    "prd_not_found",
    "projection_not_converged",
    "schema_incompatible",
    "source_drift",
    "stale_digest",
    "state_unavailable",
)


def provider_operation_catalog() -> dict[str, Any]:
    """Return the deterministic version-1 provider-read operation catalog.

    Resource identifiers are package-relative paths below ``anvil._data``.
    T006.1 packages every advertised resource at exactly these paths, allowing
    installed consumers to resolve them with ``importlib.resources`` without
    learning a checkout or state-storage path.
    """

    snapshot_limits = sorted(
        [
        "max_acceptance_criteria_per_task",
        "max_canonical_json_depth",
        "max_dependency_edges",
        "max_dependencies_per_task",
        "max_diagnostic_bytes",
        "max_features",
        "max_prds",
        "max_response_bytes",
        "max_snapshot_bytes",
        "max_string_bytes",
        "max_tasks",
        "max_verification_summaries_per_task",
        "max_verification_summary_label_bytes",
        ]
    )
    content_limits = [
        "max_diagnostic_bytes",
        "max_prd_content_bytes",
        "max_response_bytes",
    ]
    operations = [
        {
            "operation_id": "state.prd.content",
            "operation_version": 1,
            "effect": "read",
            "transport": {
                "kind": "cli",
                "command": "prd show",
                "json_required": True,
            },
            "schema_resources": {
                "input": (
                    f"{_PROVIDER_READ_RESOURCE_ROOT}/"
                    "prd-content-input.schema.json"
                ),
                "output": (
                    f"{_PROVIDER_READ_RESOURCE_ROOT}/"
                    "prd-content-output.schema.json"
                ),
                "error": (
                    f"{_PROVIDER_READ_RESOURCE_ROOT}/read-error.schema.json"
                ),
            },
            "fixture_resources": {
                "input": (
                    f"{_PROVIDER_READ_RESOURCE_ROOT}/"
                    "prd-content-input.canonical.json"
                ),
                "output": (
                    f"{_PROVIDER_READ_RESOURCE_ROOT}/"
                    "prd-content-output.canonical.json"
                ),
                "error": (
                    f"{_PROVIDER_READ_RESOURCE_ROOT}/read-error.canonical.json"
                ),
                "digests": (
                    f"{_PROVIDER_READ_RESOURCE_ROOT}/"
                    "provider-read-digests.v1.json"
                ),
            },
            "limits": content_limits,
            "error_codes": list(_CONTENT_ERROR_CODES),
        },
        {
            "operation_id": "state.project.snapshot",
            "operation_version": 1,
            "effect": "read",
            "transport": {
                "kind": "cli",
                "command": "project snapshot",
                "json_required": True,
            },
            "schema_resources": {
                "input": (
                    f"{_PROVIDER_READ_RESOURCE_ROOT}/"
                    "project-snapshot-input.schema.json"
                ),
                "output": (
                    f"{_PROVIDER_READ_RESOURCE_ROOT}/"
                    "project-snapshot-output.schema.json"
                ),
                "error": (
                    f"{_PROVIDER_READ_RESOURCE_ROOT}/read-error.schema.json"
                ),
            },
            "fixture_resources": {
                "input": (
                    f"{_PROVIDER_READ_RESOURCE_ROOT}/"
                    "project-snapshot-input.canonical.json"
                ),
                "output": (
                    f"{_PROVIDER_READ_RESOURCE_ROOT}/"
                    "project-snapshot-output.canonical.json"
                ),
                "error": (
                    f"{_PROVIDER_READ_RESOURCE_ROOT}/read-error.canonical.json"
                ),
                "digests": (
                    f"{_PROVIDER_READ_RESOURCE_ROOT}/"
                    "provider-read-digests.v1.json"
                ),
            },
            "limits": snapshot_limits,
            "error_codes": list(_SNAPSHOT_ERROR_CODES),
        },
    ]
    return {
        "catalog_version": OPERATION_CATALOG_VERSION,
        "operations": operations,
    }


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
          "api_version": "13",
          "engine_version": "0.6.4",
          "display_version": "0.6.4",
          "schema_version": 20,
          "envelope": "v1.24",
          "build_kind": "release_artifact",
          "commit": "abcdef123456",
          "tag": "v0.6.4",
          "tag_distance": 0,
          "dirty": false,
          "cli": {
            "commands": ["apply", ..., "prd parse", ...],
            "options": {"prd parse": ["--file", "--prd", ...]},
            "contracts": [
              {"path": [], "kind": "group", "flags": ["--version"]},
              ...
            ],
            "contract_count": 76,
            "count": 68
          },
          "mcp": {"tools": ["claim_task", ...], "count": 36},
          "operation_catalog": {
            "catalog_version": 1,
            "operations": [{"operation_id": "state.prd.content", ...}, ...]
          }
        }

    Both lists are sorted for stable, diffable output. ``cli.commands`` are leaf
    commands with grouped commands rendered space-joined (``"prd parse"``) so a
    consumer sees the exact invocation path.
    """
    # Compute each list exactly once so ``count`` can never disagree with the
    # list it counts.
    cli_contracts = cli_command_contracts()
    cli_options = {
        " ".join(item["path"]): item["flags"]
        for item in cli_contracts
        if item["kind"] == "command"
    }
    cli_commands = sorted(cli_options)
    mcp_tools = mcp_tool_names()
    identity = get_build_identity()
    return {
        "api_version": API_VERSION,
        "engine_version": __version__,
        "display_version": identity.display_version,
        "build_kind": identity.build_kind,
        "commit": identity.commit,
        "tag": identity.tag,
        "tag_distance": identity.tag_distance,
        "dirty": identity.dirty,
        "schema_version": get_schema_version(),
        # The CLI/MCP wire contract these commands speak. v1.24 is the
        # ``{"ok", "command", "data"/"error"}`` envelope + ANVIL_ROOT.
        "envelope": "v1.24",
        "cli": {
            "commands": cli_commands,
            "options": cli_options,
            "contracts": cli_contracts,
            "contract_count": len(cli_contracts),
            "count": len(cli_commands),
        },
        "mcp": {
            "tools": mcp_tools,
            "count": len(mcp_tools),
        },
        "operation_catalog": provider_operation_catalog(),
    }


def cli_command_names() -> list[str]:
    """Return every leaf CLI command path, sorted.

    Introspects the live Typer ``app`` via ``typer.main.get_command`` (the same
    resolution Typer uses at runtime, so hyphenation and group names match the
    real invocation) and walks the click command tree. Grouped commands are
    rendered as their full invocation path joined by spaces, e.g. ``prd parse``,
    ``sync github``. The root ``describe`` command itself is included.
    """
    return sorted(cli_command_options())


def cli_command_options() -> dict[str, list[str]]:
    """Return sorted long-option names for every leaf CLI command.

    The option inventory lets release tooling validate the exact command/flag
    citations in shipped skills against a separately installed wheel.  It is
    derived from the same Click tree as command discovery; no second manifest
    is maintained by hand.
    """

    return {
        " ".join(item["path"]): item["flags"]
        for item in cli_command_contracts()
        if item["kind"] == "command"
    }


def cli_command_contracts() -> list[dict[str, Any]]:
    """Return exact long options for root, groups, and leaf commands.

    Short aliases such as ``-V`` are intentionally outside this release
    contract; shipped skills use stable, self-documenting long options.
    """

    import click

    context = click.get_current_context(silent=True)
    root = context.find_root().command if context is not None else None
    if not isinstance(getattr(root, "commands", None), Mapping):
        from typer.main import get_command

        from anvil.cli import app

        root = get_command(app)
    if not isinstance(getattr(root, "commands", None), Mapping):
        return []
    return _walk_click_contracts(root, path=())


def _node_flags(node: Any) -> list[str]:
    flags: set[str] = set()
    for parameter in getattr(node, "params", ()):
        primary = getattr(parameter, "opts", ())
        secondary = getattr(parameter, "secondary_opts", ())
        for option in (*primary, *secondary):
            if isinstance(option, str) and option.startswith("--"):
                flags.add(option)
    return sorted(flags)


def _walk_click_contracts(
    node: Any, path: tuple[str, ...]
) -> list[dict[str, Any]]:
    """Recursively collect a stable, duplicate-detectable node contract."""

    commands = getattr(node, "commands", None)
    is_group = isinstance(commands, Mapping)
    out: list[dict[str, Any]] = [
        {
            "path": list(path),
            "kind": "group" if is_group else "command",
            "flags": _node_flags(node),
        }
    ]
    if is_group:
        for name in sorted(commands):
            out.extend(_walk_click_contracts(commands[name], path + (name,)))
    return out


def mcp_tool_names() -> list[str]:
    """Return every REGISTERED FastMCP tool name, sorted — the full engine
    surface, independent of the live-server visibility gate.

    Imported lazily so the CLI does not pull in ``fastmcp`` unless ``describe``
    is actually run. We read the LOCAL provider (``mcp.local_provider.
    list_tools()``) rather than the server-level ``mcp.list_tools()``: the local
    provider applies transforms but does NOT *filter* disabled components (it
    returns them flagged), whereas the server-level call filters them out. So the
    local provider yields the complete 36-tool surface even when the L2 planning
    gate has hidden the 12 planning tools from the per-turn wire
    (``ANVIL_MCP_PLANNING`` unset). ``describe`` answers "what can this engine
    do", which never shrinks; the gate only changes what a default execution
    client is *served* on the wire.

    ``local_provider.list_tools()`` is async; this helper drives it to completion
    whether or not an event loop is already running:

    * from the CLI (``describe`` is a plain sync Typer command, no loop) it uses
      ``asyncio.run``;
    * from *inside* the MCP server's ``describe_surface`` tool (a loop IS
      running) ``asyncio.run`` would raise ``RuntimeError``, so the coroutine is
      run to completion on a throwaway loop in a worker thread.
    """
    from anvil.mcp_server import mcp

    tools = _run_coro_blocking(mcp.local_provider.list_tools())
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
    typer.echo(f"anvil describe (api_version {manifest['api_version']})")
    typer.echo(f"  engine_version: {manifest['engine_version']}")
    typer.echo(f"  display_version: {manifest['display_version']}")
    typer.echo(f"  build_kind:     {manifest['build_kind']}")
    typer.echo(f"  commit:         {manifest['commit'] or '-'}")
    typer.echo(f"  tag:            {manifest['tag'] or '-'}")
    typer.echo(f"  tag_distance:   {manifest['tag_distance']}")
    typer.echo(f"  dirty:          {manifest['dirty']}")
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
    typer.echo("")
    catalog = manifest["operation_catalog"]
    typer.echo(
        f"Provider operations v{catalog['catalog_version']} "
        f"({len(catalog['operations'])}):"
    )
    for operation in catalog["operations"]:
        typer.echo(
            f"  {operation['operation_id']} v{operation['operation_version']} "
            f"({operation['effect']})"
        )
