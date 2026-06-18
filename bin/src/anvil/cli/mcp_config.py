"""``anvil mcp-config <client>`` — print paste-ready MCP server config.

Anvil's MCP server (bin/anvil-mcp) is harness-neutral, but every MCP client
wants the config in a slightly different envelope. This command prints the
ready-to-paste block for a target client, with the server pointed at THIS
checkout's bin/anvil-mcp by absolute path (not ${CLAUDE_PLUGIN_ROOT}), so any
MCP-capable harness gets the full 24-tool surface.

Read-only and project-free: it never opens a backend and works from any
directory (mirrors ``describe``). It only *prints* config — it never mutates the
user's ``~/.cursor/mcp.json`` etc.; the user pastes the block themselves.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

import anvil
from anvil.cli._json import JSON_OPTION, emit_success, fail

__all__ = ["mcp_config", "CLIENTS", "build_config"]

_COMMAND = "mcp-config"

# server id used in every client's config
_SERVER_ID = "anvil"

# (top_key, per_server_extra_dict, fmt) — fmt is "json" or "toml"
CLIENTS: dict[str, tuple[str, dict, str]] = {
    "claude-code": ("mcpServers", {"type": "stdio"}, "json"),
    "cursor": ("mcpServers", {}, "json"),
    "windsurf": ("mcpServers", {}, "json"),
    "cline": ("mcpServers", {}, "json"),
    "vscode": ("servers", {"type": "stdio"}, "json"),
    "zed": ("context_servers", {"source": "custom"}, "json"),
    "codex": ("mcp_servers", {}, "toml"),
}

_TARGET_FILE = {
    "claude-code": ".mcp.json (project root)",
    "cursor": "~/.cursor/mcp.json (or .cursor/mcp.json in project)",
    "windsurf": "~/.codeium/windsurf/mcp_config.json",
    "cline": "Cline MCP settings (cline_mcp_settings.json)",
    "vscode": ".vscode/mcp.json",
    "zed": "~/.config/zed/settings.json",
    "codex": "~/.codex/config.toml",
}


def _wrapper_path() -> Path:
    # anvil/__file__ = <repo>/bin/src/anvil/__init__.py
    # parents: anvil/ src/ bin/  -> bin/anvil-mcp
    return Path(anvil.__file__).resolve().parent.parent.parent / "anvil-mcp"


def _server_spec(use_uv_run: bool, root: str | None) -> dict:
    wrapper = _wrapper_path()
    bin_dir = wrapper.parent
    if use_uv_run:
        spec: dict = {
            "command": "uv",
            "args": [
                "run",
                "--project",
                str(bin_dir),
                "python",
                "-m",
                "anvil.mcp_server",
            ],
        }
    else:
        spec = {"command": "bash", "args": [str(wrapper)]}
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


def build_config(client: str, *, use_uv_run: bool, root: str | None) -> str:
    """Build the paste-ready config text for *client*.

    The inner server spec is always ``{command, args[, env]}`` pointed at this
    checkout's ``bin/anvil-mcp``; only the envelope differs per client (top key,
    per-server extras, JSON vs TOML).
    """
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
