"""``anvil install <harness> [--write]`` — deliver MCP config + instruction file.

``mcp-config`` already builds the right MCP envelope per client (reusing
:data:`anvil.cli.mcp_config.CLIENTS` + :func:`build_config`) and resolves the
real ``bin/anvil-mcp`` absolute path — but it only *prints*. ``install`` is the
thin writer that closes the delivery gap: per harness it writes/merges the MCP
config into the harness's real config path and drops the instruction file
(``AGENTS.md`` or the harness's rule-file name) where that harness reads it.

Two artifacts per harness:

1. **MCP config** → written/merged into the harness's real config path, with the
   server block produced by ``build_config(harness.mcp_client, ...)``.
2. **Instruction file** → ``AGENTS.md`` copied byte-for-byte to where the harness
   reads it (e.g. ``.github/copilot-instructions.md`` for Copilot).

Default (no ``--write``) is a **dry-run**: it prints, per target, the exact file
path and the bytes it *would* write — the natural extension of ``mcp-config``'s
"print only" posture. ``--write`` performs the writes: idempotent merge for
JSON/TOML configs (the ``anvil`` server id is replaced in place), overwrite for
the instruction file (regenerated from ``AGENTS.md``).

The engine is untouched; hooks stay Claude-Code-only (no fake shims for other
harnesses, matching ``AGENTS.md`` Notes). No new runtime deps: stdlib ``json``,
``tomllib``, ``shutil``, ``pathlib`` only — TOML write splices the fixed
``_to_toml`` block rather than pulling ``tomli-w``.
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer

import anvil
from anvil.cli._json import JSON_OPTION, emit_success, fail
from anvil.cli.mcp_config import (
    _SERVER_ID,
    CLIENTS,
    _server_spec,
    _to_toml,
    build_config,
)

__all__ = ["install", "HARNESSES", "Harness"]

_COMMAND = "install"

# Project-root env override, mirroring the rest of the CLI (ANVIL_ROOT > cwd).
_STATE_ROOT_ENV = "ANVIL_ROOT"


@dataclass(frozen=True)
class Harness:
    """One row per harness. Adding a harness later is a row, not a rewrite.

    The MCP side reuses ``mcp-config``'s ``CLIENTS`` table via ``mcp_client``
    rather than re-encoding envelopes.
    """

    name: str
    # which CLIENTS row to reuse for the server envelope (None = no MCP write,
    # e.g. copilot coding-agent whose MCP lives in a GitHub Settings UI).
    mcp_client: str | None
    # MCP config destination, relative to home (~) or project root.
    mcp_path: str | None  # e.g. "~/.codex/config.toml"
    mcp_scope: str  # "home" | "project"
    mcp_merge: str  # "json" | "toml" | "none"
    # instruction file destination + name the harness reads.
    instr_path: str  # e.g. "AGENTS.md", ".github/copilot-instructions.md"
    instr_scope: str  # "home" | "project"
    note: str = ""  # surfaced in dry-run output


HARNESSES: dict[str, Harness] = {
    # --- VERIFIED formats (full write support) ---
    "codex": Harness(
        "codex", "codex", "~/.codex/config.toml",
        "home", "toml", "AGENTS.md", "project",
    ),
    "copilot": Harness(
        "copilot", "vscode", ".vscode/mcp.json",
        "project", "json", ".github/copilot-instructions.md", "project",
        note=(
            "coding-agent MCP is a GitHub Settings UI, not a file; "
            "IDE Copilot uses .vscode/mcp.json (written here)."
        ),
    ),
    "gemini": Harness(
        "gemini", None, None,
        "home", "none", "AGENTS.md", "project",
        note=(
            "MCP ships INSIDE the gemini-extension.json manifest "
            "(see packaging/gemini/); install only drops AGENTS.md as contextFile."
        ),
    ),
    "openclaw": Harness(
        "openclaw", "claude-code", ".mcp.json",
        "project", "json", "AGENTS.md", "project",
        note="manifestless Claude bundle; .mcp.json already present.",
    ),
    # --- already-working print-only clients keep working via mcp-config; ---
    # --- rows added here only as their instruction-file dest is verified.  ---
    "cursor": Harness(
        "cursor", "cursor", "~/.cursor/mcp.json",
        "home", "json", "AGENTS.md", "project",
    ),
    "windsurf": Harness(
        "windsurf", "windsurf", "~/.codeium/windsurf/mcp_config.json",
        "home", "json", "AGENTS.md", "project",
    ),
    "cline": Harness(
        "cline", "cline", None,
        "home", "none", "AGENTS.md", "project",
        note="Cline MCP settings path is editor-managed; STUB — see packaging/cline/.",
    ),
    "zed": Harness(
        "zed", "zed", "~/.config/zed/settings.json",
        "home", "json", "AGENTS.md", "project",
    ),
    "openhands": Harness(
        "openhands", None, None,
        "project", "none", "AGENTS.md", "project",
        note=(
            "OpenHands MCP config uses a `stdio_servers` array in `[mcp]` of "
            "project-root config.toml — not a sub-table; use the snippet at "
            "packaging/openhands/config.toml.snippet and merge by hand. "
            "Instruction file is the project-root AGENTS.md (the current OpenHands "
            "convention; the .openhands/microagents/ path is deprecated V0)."
        ),
    ),
    "opencode": Harness(
        "opencode", "opencode", "opencode.json",
        "project", "json", "AGENTS.md", "project",
        note=(
            "opencode.json `mcp` entry uses a single argv array under `command` "
            "and puts env vars under `environment` (not `env`); AGENTS.md is "
            "read natively."
        ),
    ),
}


def _project_root() -> Path:
    """Project root: ``ANVIL_ROOT`` env else cwd (mirrors the rest of the CLI)."""
    env_root = os.environ.get(_STATE_ROOT_ENV)
    if env_root is not None and env_root.strip() != "":
        return Path(env_root).expanduser().resolve()
    return Path.cwd().resolve()


def _repo_root() -> Path:
    """The Anvil checkout root (holds AGENTS.md).

    ``anvil.__file__`` = ``<repo>/bin/src/anvil/__init__.py`` → four ``parent``
    hops reach the checkout root. This is where the canonical ``AGENTS.md`` lives
    (the instruction-file source), independent of the user's project root.
    """
    return Path(anvil.__file__).resolve().parent.parent.parent.parent


def _resolve(dest: str, scope: str) -> Path:
    """Resolve a harness destination to an absolute path.

    ``scope == "home"`` → under ``Path.home()``; ``scope == "project"`` → under
    the project root (ANVIL_ROOT > cwd). A leading ``~/`` is stripped and
    re-rooted at the scope base (we resolve ``~`` via ``Path.home()`` rather than
    ``os.path.expanduser`` so a monkeypatched home is honoured in tests).
    """
    base = Path.home() if scope == "home" else _project_root()
    rel = dest
    if rel.startswith("~/"):
        rel = rel[2:]
    elif rel == "~":
        return base
    p = Path(rel)
    if p.is_absolute():
        return p
    return base / rel


def _merge_json(path: Path, client: str, *, use_uv_run: bool, root: str | None) -> str:
    """Return the merged JSON text: load existing (if any), splice the anvil
    server under its top key, serialize with ``indent=2``.

    Re-running replaces the ``anvil`` server in place (server id is the shared
    ``_SERVER_ID``) → idempotent. A pre-existing unrelated server is preserved.
    """
    top_key, _, _ = CLIENTS[client]
    block = json.loads(build_config(client, use_uv_run=use_uv_run, root=root))
    server_spec = block[top_key][_SERVER_ID]

    existing: dict[str, Any] = {}
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
    if not isinstance(existing, dict):
        existing = {}

    servers = existing.get(top_key)
    if not isinstance(servers, dict):
        servers = {}
    servers[_SERVER_ID] = server_spec
    existing[top_key] = servers
    return json.dumps(existing, indent=2) + "\n"


def _merge_toml(path: Path, client: str, *, use_uv_run: bool, root: str | None) -> str:
    """Return the merged TOML text: keep every existing table except the anvil
    server table, then append the freshly-built ``[mcp_servers.anvil]`` block.

    The block we emit is fixed-shape, so we splice it via ``mcp_config._to_toml``
    rather than pulling a ``tomli-w`` dependency. A pre-existing unrelated
    ``[mcp_servers.other]`` table survives; re-running replaces ours → idempotent.
    """
    top_key, extra, _ = CLIENTS[client]
    spec = {**extra, **_server_spec(use_uv_run, root)}
    anvil_block = _to_toml(top_key, spec)

    existing: dict[str, Any] = {}
    if path.is_file():
        try:
            existing = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError:
            existing = {}

    # Drop only OUR server table; everything else is re-emitted verbatim-ish.
    servers = existing.get(top_key)
    if isinstance(servers, dict):
        servers = {k: v for k, v in servers.items() if k != _SERVER_ID}
    else:
        servers = {}

    lines: list[str] = []
    # Re-emit top-level scalars (not the mcp_servers table).
    for key, value in existing.items():
        if key == top_key:
            continue
        if isinstance(value, dict):
            continue  # other tables handled below
        lines.append(f"{key} = {json.dumps(value)}")
    if lines:
        lines.append("")

    # Re-emit OTHER tables under top_key (e.g. [mcp_servers.other]).
    for sub_name, sub_val in servers.items():
        if not isinstance(sub_val, dict):
            continue
        lines.append(f"[{top_key}.{sub_name}]")
        for k, v in sub_val.items():
            if isinstance(v, dict):
                lines.append(f"\n[{top_key}.{sub_name}.{k}]")
                for ik, iv in v.items():
                    lines.append(f"{ik} = {json.dumps(iv)}")
            else:
                lines.append(f"{k} = {json.dumps(v)}")
        lines.append("")

    # Re-emit other top-level tables (anything that isn't top_key).
    for key, value in existing.items():
        if key == top_key or not isinstance(value, dict):
            continue
        lines.append(f"[{key}]")
        for k, v in value.items():
            lines.append(f"{k} = {json.dumps(v)}")
        lines.append("")

    text = "\n".join(lines)
    if text and not text.endswith("\n"):
        text += "\n"
    if text and not text.endswith("\n\n"):
        text += "\n"  # blank line separating prior content from our block
    return text + anvil_block


def _plan_actions(
    h: Harness, *, use_uv_run: bool, root: str | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Compute (mcp_action, instr_action) dicts describing what install would do.

    Each action carries ``path`` (resolved, as str) and the *content* to write,
    plus the ``action`` verb. MCP ``action`` is one of wrote/merged/skipped;
    instruction ``action`` is wrote.
    """
    # --- MCP artifact ---
    if h.mcp_merge == "none" or h.mcp_client is None or h.mcp_path is None:
        mcp = {
            "path": None,
            "action": "skipped",
            "content": None,
            "note": h.note,
        }
    else:
        mcp_dest = _resolve(h.mcp_path, h.mcp_scope)
        existed = mcp_dest.is_file()
        if h.mcp_merge == "json":
            content = _merge_json(
                mcp_dest, h.mcp_client, use_uv_run=use_uv_run, root=root
            )
        else:  # toml
            content = _merge_toml(
                mcp_dest, h.mcp_client, use_uv_run=use_uv_run, root=root
            )
        mcp = {
            "path": str(mcp_dest),
            "action": "merged" if existed else "wrote",
            "content": content,
            "note": h.note,
        }

    # --- Instruction artifact (always AGENTS.md bytes) ---
    instr_dest = _resolve(h.instr_path, h.instr_scope)
    instr = {
        "path": str(instr_dest),
        "action": "wrote",
        "content": None,  # raw bytes copied from AGENTS.md at write time
    }
    return mcp, instr


def install(
    harness: str = typer.Argument(  # noqa: B008
        ...,
        help="Target harness: " + ", ".join(HARNESSES),
    ),
    write: bool = typer.Option(  # noqa: B008
        False,
        "--write",
        help=(
            "Perform the writes (idempotent merge for JSON/TOML configs, "
            "overwrite for the instruction file). Omit for a safe dry-run that "
            "only prints the paths and bytes it WOULD write."
        ),
    ),
    use_uv_run: bool = typer.Option(  # noqa: B008
        False,
        "--uv-run",
        help=(
            "Emit the explicit `uv run` invocation instead of the bash wrapper "
            "in the written MCP block (use on hosts without bash)."
        ),
    ),
    root: str | None = typer.Option(  # noqa: B008
        None,
        "--root",
        help=(
            "Pin ANVIL_ROOT to this project dir in the written MCP config. "
            "Omit to let the harness's cwd decide."
        ),
    ),
    json_output: bool = JSON_OPTION,
) -> None:
    """Write Anvil's MCP config + instruction file for a target harness.

    Default is a dry-run (prints paths + bytes, mutates nothing); ``--write``
    performs idempotent writes. The MCP envelope is reused from ``mcp-config``'s
    ``CLIENTS`` table (never re-encoded); the instruction file is the canonical
    ``AGENTS.md`` bytes. Hooks remain Claude-Code-only — install never fakes
    hook shims for other harnesses.
    """
    if harness not in HARNESSES:
        msg = (
            f"unknown harness '{harness}'. Choose one of: "
            f"{', '.join(HARNESSES)}."
        )
        if json_output:
            fail(_COMMAND, msg, code="bad_request", exit_code=2)
        typer.echo(f"Error: {msg}", err=True)
        raise typer.Exit(code=2)

    h = HARNESSES[harness]
    mcp, instr = _plan_actions(h, use_uv_run=use_uv_run, root=root)
    agents_bytes = (_repo_root() / "AGENTS.md").read_bytes()

    if write:
        if mcp["action"] in ("wrote", "merged"):
            dest = Path(mcp["path"])
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(mcp["content"], encoding="utf-8")
        instr_dest = Path(instr["path"])
        instr_dest.parent.mkdir(parents=True, exist_ok=True)
        instr_dest.write_bytes(agents_bytes)

    if json_output:
        emit_success(
            _COMMAND,
            {
                "harness": harness,
                "write": write,
                "mcp": {
                    "path": mcp["path"],
                    "action": mcp["action"],
                    "note": mcp["note"],
                },
                "instruction": {
                    "path": instr["path"],
                    "action": instr["action"],
                },
            },
        )
        return

    verb = "Wrote" if write else "[dry-run] would write"
    if mcp["action"] in ("wrote", "merged"):
        typer.echo(f"# MCP config ({mcp['action']}) → {mcp['path']}", err=True)
        if not write:
            typer.echo(mcp["content"], nl=False)
    else:
        typer.echo(f"# MCP config: skipped — {mcp['note']}", err=True)

    typer.echo(
        f"# Instruction file ({verb}, from AGENTS.md) → {instr['path']}", err=True
    )
    if write:
        typer.echo(
            f"{verb}: {mcp['path'] or '(no MCP write)'} + {instr['path']}",
            err=True,
        )
