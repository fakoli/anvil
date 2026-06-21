"""``anvil mcp-config <client>`` — print paste-ready MCP server config.

Anvil's MCP server is harness-neutral, but every MCP client wants the config in a
slightly different envelope. This command prints the ready-to-paste block for a
target client so any MCP-capable harness gets the full 24-tool surface. The server
command adapts to the install method (see ``_server_spec``): from a source checkout
or plugin bundle it points at that tree's bin/anvil-mcp by absolute path; from an
installed package (uv tool/pipx/pip) it emits the ``anvil-mcp`` console script on
PATH.

Read-only and project-free: it never opens a backend and works from any
directory (mirrors ``describe``). It only *prints* config — it never mutates the
user's ``~/.cursor/mcp.json`` etc.; the user pastes the block themselves.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
import yaml

import anvil
from anvil.cli._json import JSON_OPTION, emit_success, fail

__all__ = ["mcp_config", "CLIENTS", "build_config"]

_COMMAND = "mcp-config"

# server id used in every client's config
_SERVER_ID = "anvil"

# OpenCode's config carries a $schema hint and a uniquely-shaped server entry
# (argv-array command, `environment` instead of `env`) — see _build_opencode.
_OPENCODE_SCHEMA = "https://opencode.ai/config.json"

# (top_key, per_server_extra_dict, fmt) — fmt is "json" or "toml". A handful of
# clients (opencode) have a server shape that doesn't fit the uniform
# {command, args[, env]} spec; build_config special-cases those before using the
# table. Their CLIENTS row still records the top_key so the install merge works.
CLIENTS: dict[str, tuple[str, dict, str]] = {
    "claude-code": ("mcpServers", {"type": "stdio"}, "json"),
    "cursor": ("mcpServers", {}, "json"),
    "windsurf": ("mcpServers", {}, "json"),
    "cline": ("mcpServers", {}, "json"),
    "vscode": ("servers", {"type": "stdio"}, "json"),
    "zed": ("context_servers", {"source": "custom"}, "json"),
    "codex": ("mcp_servers", {}, "toml"),
    "opencode": ("mcp", {}, "json"),
    "roo": ("mcpServers", {}, "json"),
    # Amp uses a single flat dotted settings key (VS Code-style), not a nested
    # table — so the top_key IS "amp.mcpServers" and the uniform builder works.
    "amp": ("amp.mcpServers", {}, "json"),
    # YAML clients are special-cased in build_config; the row records the
    # nominal top key + the "yaml" format label for the --json envelope.
    "continue": ("mcpServers", {}, "yaml"),
    "goose": ("extensions", {}, "yaml"),
}

_TARGET_FILE = {
    "claude-code": ".mcp.json (project root)",
    "cursor": "~/.cursor/mcp.json (or .cursor/mcp.json in project)",
    "windsurf": "~/.codeium/windsurf/mcp_config.json",
    "cline": "Cline MCP settings (cline_mcp_settings.json)",
    "vscode": ".vscode/mcp.json",
    "zed": "~/.config/zed/settings.json",
    "codex": "~/.codex/config.toml",
    "opencode": "opencode.json (project) or ~/.config/opencode/opencode.json",
    "roo": ".roo/mcp.json (project)",
    "amp": "~/.config/amp/settings.json (key amp.mcpServers)",
    "continue": ".continue/mcpServers/anvil.yaml (project)",
    "goose": "~/.config/goose/config.yaml (extensions; global only)",
}


def _wrapper_path() -> Path:
    # anvil/__file__ = <repo>/bin/src/anvil/__init__.py
    # parents: anvil/ src/ bin/  -> bin/anvil-mcp
    return Path(anvil.__file__).resolve().parent.parent.parent / "anvil-mcp"


def _server_spec(use_uv_run: bool, root: str | None) -> dict:
    wrapper = _wrapper_path()
    spec: dict
    if wrapper.is_file():
        # Source checkout / plugin bundle: the bin/anvil-mcp bash wrapper exists
        # and self-syncs uv deps. Default to it; --uv-run emits the explicit uv
        # invocation for bash-less hosts.
        bin_dir = wrapper.parent
        if use_uv_run:
            spec = {
                "command": "uv",
                "args": ["run", "--project", str(bin_dir), "python", "-m", "anvil.mcp_server"],
            }
        else:
            spec = {"command": "bash", "args": [str(wrapper)]}
    else:
        # Installed package (uv tool / pipx / pip): no checkout, so no bash
        # wrapper on disk. The `anvil-mcp` console script is on PATH and deps are
        # already installed — emit the bare command (nothing to sync, so --uv-run
        # is moot here). Mirrors how the openclaw plugin spawns bare `anvil`.
        spec = {"command": "anvil-mcp", "args": []}
    if root:
        spec["env"] = {"ANVIL_ROOT": root}
    return spec


def _to_toml(top_key: str, spec: dict) -> str:
    lines = [
        f"[{top_key}.{_SERVER_ID}]",
        f'command = {json.dumps(spec["command"])}',
        f'args = {json.dumps(spec["args"])}',
    ]
    if "env" in spec:
        lines.append(f"\n[{top_key}.{_SERVER_ID}.env]")
        for k, v in spec["env"].items():
            lines.append(f"{k} = {json.dumps(v)}")
    return "\n".join(lines) + "\n"


def _opencode_block(use_uv_run: bool, root: str | None) -> str:
    """OpenCode's ``opencode.json`` uses a uniquely-shaped MCP entry.

    Unlike the uniform ``{command, args[, env]}`` spec, OpenCode wants a single
    argv array under ``command``, an ``enabled`` flag, ``type: "local"``, and
    env vars under ``environment`` (not ``env``). It also carries a ``$schema``.
    """
    base = _server_spec(use_uv_run, root)
    spec: dict = {
        "type": "local",
        "command": [base["command"], *base["args"]],
        "enabled": True,
    }
    if "env" in base:
        spec["environment"] = base["env"]
    return (
        json.dumps(
            {"$schema": _OPENCODE_SCHEMA, "mcp": {_SERVER_ID: spec}}, indent=2
        )
        + "\n"
    )


def _continue_block(use_uv_run: bool, root: str | None) -> str:
    """Continue.dev reads a per-server YAML file at .continue/mcpServers/<n>.yaml.

    The file is a small block doc (``name``/``version``/``schema: v1``) with an
    ``mcpServers`` list; each entry is ``{name, command, args[, env]}``.
    """
    base = _server_spec(use_uv_run, root)
    server: dict = {
        "name": _SERVER_ID,
        "command": base["command"],
        "args": list(base["args"]),
    }
    if "env" in base:
        server["env"] = base["env"]
    doc = {
        "name": "Anvil",
        "version": "0.0.1",
        "schema": "v1",
        "mcpServers": [server],
    }
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)


def _goose_block(use_uv_run: bool, root: str | None) -> str:
    """Goose lists MCP servers under ``extensions`` in ~/.config/goose/config.yaml.

    A stdio extension uses ``cmd`` (not ``command``), ``type: stdio``, and env
    *values* under ``envs`` (not ``env``).
    """
    base = _server_spec(use_uv_run, root)
    ext: dict = {
        "name": _SERVER_ID,
        "type": "stdio",
        "cmd": base["command"],
        "args": list(base["args"]),
        "enabled": True,
        "timeout": 300,
    }
    if "env" in base:
        ext["envs"] = base["env"]
    return yaml.safe_dump(
        {"extensions": {_SERVER_ID: ext}}, sort_keys=False, default_flow_style=False
    )


def build_config(client: str, *, use_uv_run: bool, root: str | None) -> str:
    """Build the paste-ready config text for *client*.

    The inner server spec is usually ``{command, args[, env]}`` pointed at this
    checkout's ``bin/anvil-mcp``; only the envelope differs per client (top key,
    per-server extras, JSON vs TOML). A few clients have a different server shape
    entirely and are special-cased.
    """
    if client == "opencode":
        return _opencode_block(use_uv_run, root)
    if client == "continue":
        return _continue_block(use_uv_run, root)
    if client == "goose":
        return _goose_block(use_uv_run, root)
    top_key, extra, fmt = CLIENTS[client]
    spec = {**extra, **_server_spec(use_uv_run, root)}
    if fmt == "toml":
        return _to_toml(top_key, spec)
    return json.dumps({top_key: {_SERVER_ID: spec}}, indent=2) + "\n"


def mcp_config(
    client: str = typer.Argument(  # noqa: B008
        ...,
        help="Target MCP client: " + ", ".join(CLIENTS),
    ),
    use_uv_run: bool = typer.Option(  # noqa: B008
        False,
        "--uv-run",
        help=(
            "Emit the explicit `uv run` invocation instead of the bash wrapper "
            "(use on hosts without bash, e.g. Windows)."
        ),
    ),
    root: str | None = typer.Option(  # noqa: B008
        None,
        "--root",
        help=(
            "Pin ANVIL_ROOT to this project dir in the printed config. "
            "Omit to let the client's cwd decide."
        ),
    ),
    json_output: bool = JSON_OPTION,
) -> None:
    """Print paste-ready MCP server config for a target client.

    Read-only and project-free (mirrors ``describe``): never opens a backend,
    works from any directory. The printed block points the ``anvil`` server at
    this checkout's ``bin/anvil-mcp`` by absolute path so any MCP-capable harness
    gets the full tool surface — no ``${CLAUDE_PLUGIN_ROOT}`` token.
    """
    if client not in CLIENTS:
        msg = f"unknown client '{client}'. Choose one of: {', '.join(CLIENTS)}."
        if json_output:
            fail(_COMMAND, msg, code="bad_request", exit_code=2)
        typer.echo(f"Error: {msg}", err=True)
        raise typer.Exit(code=2)

    text = build_config(client, use_uv_run=use_uv_run, root=root)
    _, _, fmt = CLIENTS[client]

    if json_output:
        emit_success(
            _COMMAND,
            {
                "client": client,
                "target_file": _TARGET_FILE[client],
                "format": fmt,
                "config_text": text,
            },
        )
        return

    # hint to stderr so stdout stays paste-clean
    typer.echo(f"# paste into {_TARGET_FILE[client]}", err=True)
    typer.echo(text, nl=False)
