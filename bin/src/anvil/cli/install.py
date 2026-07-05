"""``anvil install <harness> [--write|--rollback]`` — deliver anvil to a harness.

``mcp-config`` builds the right MCP envelope per client (reusing
:data:`anvil.cli.mcp_config.CLIENTS` + :func:`build_config`) but only *prints*.
``install`` closes the delivery gap, per harness, by the safest available route:

anvil officially supports three harnesses end-to-end — **claude-code** (the plugin),
**codex**, and **openclaw**; every other harness is **MCP-only best-effort**: it gets
the anvil MCP server and nothing else. Tiers:

- **Native CLI harnesses** (codex, openclaw): drive the harness's OWN tooling
  (``codex mcp add`` / ``openclaw mcp add`` + plugin install). The harness writes
  its own config — anvil never hand-edits it. codex additionally gets anvil's
  ``AGENTS.md`` spliced into a marked, *removable* block; openclaw's plugin ships it.
- **MCP-only harnesses** (cursor, windsurf, …): best-effort — anvil merges only its
  MCP block into the harness's JSON config (the ``anvil`` server id replaced in
  place → idempotent). No instruction splice, no skills drop.

Default (no flag) is a **dry-run** that prints what it would do. ``--write``
performs it; every modified file is backed up and logged so ``--rollback`` is
exact. The engine is untouched; hook wiring remains harness-owned.
"""

from __future__ import annotations

import hashlib
import importlib.resources
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer

from anvil.cli._json import JSON_OPTION, emit_success, fail
from anvil.cli.mcp_config import (
    _SERVER_ID,
    CLIENTS,
    _server_spec,
    build_config,
)

__all__ = ["install", "HARNESSES", "Harness"]

_COMMAND = "install"

# Project-root env override, mirroring the rest of the CLI (ANVIL_ROOT > cwd).
_STATE_ROOT_ENV = "ANVIL_ROOT"

# Suffix for the byte-exact safety copy we keep beside any file we modify.
_BAK_SUFFIX = ".anvil-bak"
# Markers wrapping anvil's content inside a user-owned instruction file. We only
# ever touch text between these; everything outside is the user's, untouched.
_BLOCK_BEGIN = "<!-- BEGIN ANVIL (managed by `anvil install` — safe to delete this block) -->"
_BLOCK_END = "<!-- END ANVIL -->"


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
    # When set, install drives the harness's OWN CLI (e.g. `codex mcp add`) instead
    # of hand-editing its config file — safer, and the harness owns its own state.
    native_installer: str | None = None
    # Whether `--automations` applies (codex's scheduled-run system). Don't proxy
    # this off native_installer — a future native harness need not have one.
    supports_automations: bool = False
    # Whether install splices anvil's instructions into the harness's instruction
    # file. Default False = MCP-only (best-effort harnesses get just the MCP server);
    # True only on a supported harness that splices (codex). openclaw's plugin ships
    # the instructions itself.
    writes_instructions: bool = False


HARNESSES: dict[str, Harness] = {
    # --- VERIFIED formats (full write support) ---
    # Codex manages its own config via `codex mcp add` / `codex plugin marketplace
    # add` — we NEVER text-edit ~/.codex/config.toml (doing so corrupted it). The
    # plugin ships anvil's skills natively; codex is the one supported harness that
    # still splices anvil's AGENTS.md.
    "codex": Harness(
        "codex", None, None,
        "home", "none", "AGENTS.md", "project",
        note=(
            "native install via the codex CLI (plugin marketplace + mcp add); "
            "config.toml is written by Codex, not anvil."
        ),
        native_installer="codex",
        supports_automations=True,
        writes_instructions=True,
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
    # OpenClaw is its OWN platform (not a Claude bundle): it manages MCP, skills,
    # and plugins through the `openclaw` CLI. We never hand-edit
    # ~/.openclaw/openclaw.json, and the plugin ships anvil's skills + instructions,
    # so we skip the instruction splice.
    "openclaw": Harness(
        "openclaw", None, None,
        "home", "none", "AGENTS.md", "project",
        note=(
            "native install via the openclaw CLI (mcp add --no-probe + plugins "
            "install); openclaw owns its config at ~/.openclaw/."
        ),
        native_installer="openclaw",
        writes_instructions=False,
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
    "roo": Harness(
        "roo", "roo", ".roo/mcp.json",
        "project", "json", "AGENTS.md", "project",
        note="Roo Code reads project MCP servers from .roo/mcp.json (mcpServers).",
    ),
    "amp": Harness(
        "amp", "amp", "~/.config/amp/settings.json",
        "home", "json", "AGENTS.md", "project",
        note=(
            "Amp uses a flat `amp.mcpServers` settings key (VS Code-style); "
            "`amp mcp add` is the CLI equivalent."
        ),
    ),
    # YAML configs: no in-place merge writer, so install drops AGENTS.md and
    # points at `anvil mcp-config <harness>` + the committed reference (same
    # posture as gemini/openhands).
    "continue": Harness(
        "continue", None, None,
        "project", "none", "AGENTS.md", "project",
        note=(
            "Continue reads a per-server YAML file at "
            ".continue/mcpServers/anvil.yaml — run `anvil mcp-config continue` "
            "and save the block there (see packaging/continue/)."
        ),
    ),
    "goose": Harness(
        "goose", None, None,
        "home", "none", "AGENTS.md", "project",
        note=(
            "Goose MCP servers live under `extensions` in "
            "~/.config/goose/config.yaml (global only) — run "
            "`anvil mcp-config goose` and merge the block (see packaging/goose/)."
        ),
    ),
}

# `vscode` is an accepted alias for the copilot row: VS Code (with Copilot) reads
# the same .vscode/mcp.json the copilot harness writes. Aliasing keeps the
# documented `anvil install vscode` working without a second, near-identical row.
# Listing it here also feeds the `--help` text and the "unknown harness" error,
# which both derive from `HARNESSES` — one source of truth.
# ponytail: alias, not a distinct row. Split it out only if a non-Copilot VS Code
# ever needs AGENTS.md instead of .github/copilot-instructions.md.
HARNESSES["vscode"] = HARNESSES["copilot"]


def _project_root() -> Path:
    """Project root: ``ANVIL_ROOT`` env else cwd (mirrors the rest of the CLI)."""
    env_root = os.environ.get(_STATE_ROOT_ENV)
    if env_root is not None and env_root.strip() != "":
        return Path(env_root).expanduser().resolve()
    return Path.cwd().resolve()


def _data_dir() -> Path:
    """Runtime data shipped INSIDE the ``anvil`` package, under ``anvil/_data/``:
    the canonical ``AGENTS.md`` (instruction-splice source) and the Codex
    automation templates.

    Resolved via ``importlib.resources`` so it works for BOTH a source checkout
    (``anvil/_data/`` sits next to the code) and a wheel install (uv tool/pipx/pip
    unpack package data into site-packages). Replaces the old ``_repo_root()``
    4-parent walk, which resolved outside any installed package and silently broke
    the AGENTS.md splice + automations for wheel installs.

    # ponytail: assumes an unpacked install (the only way anvil ships). Switch to
    # importlib.resources.as_file() if anvil is ever distributed as a zipimport.
    """
    return Path(str(importlib.resources.files("anvil"))) / "_data"


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

    existing: dict[str, Any]
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
        if not isinstance(existing, dict):
            existing = {}
    else:
        # Fresh file: seed from the generated block so non-server top-level keys
        # (e.g. opencode's $schema) are written too — matching `mcp-config`
        # output and the committed reference. No-op for clients whose block is
        # only the server table.
        existing = block

    servers = existing.get(top_key)
    if not isinstance(servers, dict):
        servers = {}
    servers[_SERVER_ID] = server_spec
    existing[top_key] = servers
    return json.dumps(existing, indent=2) + "\n"


# Codex marketplace source: the public GitHub slug, not the local checkout. It
# resolves for every install method (curl source checkout AND a pip wheel, which
# has no local marketplace.json) and is version-stable.
# anvil's public, Claude-compatible plugin marketplace — resolves for every
# install method (source checkout AND wheel) and is consumed by both Codex's
# `plugin marketplace add` and OpenClaw's `plugins install --marketplace`.
_ANVIL_MARKETPLACE = "fakoli/anvil"

# Human-facing label per native installer (for output lines).
_NATIVE_LABEL = {"codex": "Codex", "openclaw": "OpenClaw"}


def _codex_install_commands(*, use_uv_run: bool, root: str | None) -> list[list[str]]:
    """The argv lists `anvil install codex` runs (or prints) — Codex writes its own
    config, so we never touch ~/.codex/config.toml. `marketplace add` registers the
    anvil plugin (skills + commands + Plugins-panel entry); `mcp add` wires the MCP
    server (`-c mcp.*` overwrite semantics make it idempotent)."""
    # Codex should never depend on a bare shell wrapper from a source checkout:
    # on Windows it can resolve to System32/WSL bash and hang, and on every host
    # MCP stdio startup is cleaner when uv launches the Python module directly.
    spec = _server_spec(True, root)
    mcp_add = ["codex", "mcp", "add", _SERVER_ID]
    for k, v in spec.get("env", {}).items():
        mcp_add += ["--env", f"{k}={v}"]
    mcp_add += ["--", spec["command"], *spec["args"]]
    return [
        ["codex", "plugin", "marketplace", "add", _ANVIL_MARKETPLACE],
        mcp_add,
    ]


def _codex_rollback_commands() -> list[list[str]]:
    """Codex owns its config, so undo is via Codex's own removers."""
    return [
        ["codex", "mcp", "remove", _SERVER_ID],
        ["codex", "plugin", "marketplace", "remove", "anvil"],
    ]


def _codex_automation_plan() -> list[dict[str, Any]]:
    """Render anvil's automation templates for THIS project. Each becomes an
    isolated `~/.codex/automations/<id>/` dir (own file — no shared state to
    corrupt), namespaced per project so two projects don't clobber each other's
    schedule. Returns ``{id, dir, toml}`` per automation; empty only if the
    templates are somehow absent. Codex reads these on its next scan; we ship them
    ``status = "PAUSED"`` so nothing runs until the user activates it."""
    src = _data_dir() / "packaging" / "codex" / "automations"
    if not src.is_dir():
        return []
    root = str(_project_root())
    # Namespace per project: a legible basename PLUS a short path hash, so two
    # projects sharing a basename ("app", "web", …) never collide into one dir.
    slug = re.sub(r"[^A-Za-z0-9_-]", "-", _project_root().name) or "project"
    slug = f"{slug}-{hashlib.sha256(root.encode()).hexdigest()[:8]}"
    ts = str(int(time.time() * 1000))
    cwds = json.dumps(root)
    plan = []
    for tmpl in sorted(src.glob("*/automation.toml")):
        aid = f"{tmpl.parent.name}-{slug}"
        # Substitute {{CWDS}} LAST so a literal "{{TS}}"/"{{ID}}" inside the project
        # path can't be re-scanned and corrupted by a later replace.
        rendered = (
            tmpl.read_text(encoding="utf-8")
            .replace("{{ID}}", aid)
            .replace("{{TS}}", ts)
            .replace("{{CWDS}}", cwds)
        )
        plan.append(
            {"id": aid, "dir": Path.home() / ".codex" / "automations" / aid,
             "toml": rendered}
        )
    return plan


def _openclaw_install_commands(*, use_uv_run: bool, root: str | None) -> list[list[str]]:
    """OpenClaw is a standalone platform with its own CLI — never the Claude
    `.mcp.json` bundle the old row assumed. `mcp add` registers the server (with
    `--no-probe`, so a cold-start `uv sync` can't time out the save); `plugins
    install --marketplace` pulls anvil's skills/commands from its Claude-compatible
    marketplace. OpenClaw owns its config at ~/.openclaw/openclaw.json."""
    spec = _server_spec(use_uv_run, root)
    # `--no-probe`: save the server WITHOUT connecting first. The probe has a 30s
    # timeout, and anvil's wrapper runs `uv sync` on first launch — on a cold venv
    # that overruns the probe, leaving the server unsaved while the plugin still
    # installs (a half-install reported as success). OpenClaw validates the server
    # on first real use; `openclaw mcp doctor`/`probe` are there to check manually.
    mcp_add = ["openclaw", "mcp", "add", _SERVER_ID, "--no-probe",
               "--command", spec["command"]]
    for a in spec["args"]:
        # Use the --arg=value form so uv flags like "--quiet", "--project", and
        # "-m" stay values for OpenClaw's parser instead of being read as
        # OpenClaw options.
        mcp_add.append(f"--arg={a}")
    for k, v in spec.get("env", {}).items():
        mcp_add += ["--env", f"{k}={v}"]
    # `--force`: refresh the plugin on re-run. Without it OpenClaw prints "plugin
    # already exists" and exits 0 WITHOUT updating — a re-install silently keeps the
    # stale version. (mcp add already overwrites; this matches that idempotency.)
    return [
        mcp_add,
        ["openclaw", "plugins", "install", _SERVER_ID,
         "--marketplace", _ANVIL_MARKETPLACE, "--force"],
    ]


def _openclaw_rollback_commands() -> list[list[str]]:
    """OpenClaw owns its config, so undo is via its own removers."""
    return [
        ["openclaw", "mcp", "unset", _SERVER_ID],
        ["openclaw", "plugins", "uninstall", _SERVER_ID, "--force"],
    ]


# OpenClaw runs anvil tools through a sandbox when sandboxing is on; without an
# allowlist entry the 24 MCP tools silently vanish in sandboxed turns. A one-line
# prerequisite note, surfaced on every openclaw install.
_OPENCLAW_SANDBOX_NOTE = (
    "# OpenClaw sandbox: if you enable sandboxing, add anvil's MCP tools to "
    "`sandbox.tools.allow` (else the 24 anvil tools vanish in sandboxed turns)."
)


def _openclaw_cron_recipes(project_root: str) -> str:
    """Printed, OPT-IN OpenClaw Gateway cron recipes for anvil — copy/paste the ones
    you want. anvil NEVER registers these (the no-files contract); the Gateway runs
    them with zero active agents at no model cost, and anvil's exclusive lease keeps
    them safe alongside human work. Channels are placeholders to fill in."""
    cwd = project_root
    return "\n".join(
        [
            "# Opt-in OpenClaw Gateway cron recipes (copy/paste — anvil registers nothing):",
            "#",
            "# 1. Queue probe — ready work? (no model cost; `anvil next -q` exits 3 on empty)",
            f"openclaw cron add --every 10m --command-cwd '{cwd}' \\",
            "    --command 'anvil next -q'",
            "#",
            "# 2. Nightly reconcile — sync state + report drift (`;` so a failed step",
            "#    doesn't skip the rest; e.g. no GitHub token still runs --fix + drift)",
            f"openclaw cron add --cron '0 3 * * *' --command-cwd '{cwd}' --announce '<channel>' \\",
            "    --command 'anvil sync github ; anvil sync --fix --yes ; anvil drift --json'",
            "#",
            "# 3. Lease watchdog — surface/repair stale claims (every 15m)",
            f"openclaw cron add --every 15m --command-cwd '{cwd}' \\",
            "    --command 'anvil doctor --json || anvil sync --fix --yes'",
            "#",
            "# 4. Finish-gate nudge — ping a channel ONLY when review/blockers are pending",
            f"openclaw cron add --every 30m --command-cwd '{cwd}' --announce '<channel>' \\",
            "    --command 'anvil notify-digest'",
        ]
    )


def _openclaw_finish_gate_recipe() -> str:
    """Printed, OPT-IN recipe to install anvil's native ``before_agent_finalize``
    finish-gate plugin into the OpenClaw Gateway. anvil links/registers NOTHING
    (the no-files contract) — copy/paste the steps you want. The gate blocks an
    agent from finalizing while its claimed anvil task lacks submitted evidence;
    it is DEFAULT-OPEN (no anvil project / no claim / ``anvil`` missing => the
    agent finalizes normally)."""
    # The finish-gate plugin (a Gateway-side TS plugin) ships with the anvil SOURCE
    # tree, not the Python wheel — resolve it relative to this module when present
    # (full repo / plugin-cache layout); from a wheel install there is no checkout,
    # so tell the user where to get it rather than printing a dead local path.
    plugin_dir = Path(__file__).resolve().parents[4] / "packaging" / "openclaw" / "plugin"
    # From a wheel install there is no checkout; point the user at the source clone
    # rather than a dead local path (the plugin is not in the pip package).
    shown = (
        str(plugin_dir)
        if plugin_dir.is_dir()
        else "<clone fakoli/anvil>/packaging/openclaw/plugin"
    )
    pid = "anvil-finish-gate"
    return "\n".join(
        [
            "# Opt-in: install anvil's native finish-gate plugin (anvil registers nothing):",
            "#",
            "# 1. Link + enable the plugin",
            f"openclaw plugins install --link '{shown}'",
            f"openclaw plugins enable {pid}",
            "#",
            "# 2. REQUIRED — before_agent_finalize only fires for a non-bundled plugin",
            "#    once allowConversationAccess is set:",
            f"openclaw config set plugins.entries.{pid}.hooks.allowConversationAccess true --strict-json",  # noqa: E501
            "#",
            "# 3. Restart the Gateway so it loads the plugin",
            "openclaw gateway restart",
            "#",
            "# Note: the Gateway spawns `anvil` from its PATH — ensure anvil is installed",
            "#   there (e.g. install.sh --path).",
        ]
    )


def _native_install_commands(
    installer: str, *, use_uv_run: bool, root: str | None
) -> list[list[str]]:
    if installer == "codex":
        return _codex_install_commands(use_uv_run=use_uv_run, root=root)
    if installer == "openclaw":
        return _openclaw_install_commands(use_uv_run=use_uv_run, root=root)
    return []


def _native_rollback_commands(installer: str) -> list[list[str]]:
    if installer == "codex":
        return _codex_rollback_commands()
    if installer == "openclaw":
        return _openclaw_rollback_commands()
    return []


def _run_or_print(cmds: list[list[str]], *, run: bool) -> list[dict[str, Any]]:
    """Run each native command (when its CLI is present) or just report it. Never
    raises — a missing/failing CLI degrades to printed instructions. The binary is
    each command's own argv[0] (codex / openclaw), not a hardcoded name."""
    results = []
    for cmd in cmds:
        printed = " ".join(cmd)
        if run and shutil.which(cmd[0]) is not None:
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                ok = proc.returncode == 0
                results.append(
                    {"cmd": printed, "ran": True, "ok": ok,
                     "detail": (proc.stderr or proc.stdout).strip()[:200]}
                )
            except (OSError, subprocess.SubprocessError) as e:
                results.append({"cmd": printed, "ran": True, "ok": False, "detail": str(e)})
        else:
            results.append({"cmd": printed, "ran": False, "ok": None, "detail": ""})
    return results


# A managed block is BEGIN and END markers, each alone on its line, in order.
# Anchored so a stray marker in the user's prose / a code fence can't be mistaken
# for our block (it must be a well-formed pair). DOTALL for the body, MULTILINE
# for the line anchors.
_BLOCK_RE = re.compile(
    re.escape(_BLOCK_BEGIN) + r"\n.*?\n[ \t]*" + re.escape(_BLOCK_END) + r"[ \t]*",
    re.DOTALL,
)


def _merge_instruction(path: Path, content: str) -> str:
    """Splice anvil's instructions into a (possibly user-owned) instruction file.

    We NEVER overwrite the file wholesale — people curate these. Our content lives
    inside a clearly-marked block appended after the user's text; re-running
    replaces only that one block (idempotent), and ``--rollback`` removes it.

    Raises ``ValueError`` rather than risk corruption when the existing file has
    ambiguous markers (a stray BEGIN/END, or more than one block) — better to ask
    the user to clean up than to silently mangle their file.
    """
    # Our own content must be marker-free, else the wrapped block self-collides.
    if _BLOCK_BEGIN in content or _BLOCK_END in content:
        raise ValueError("anvil's instruction content contains a managed-block marker")
    block = f"{_BLOCK_BEGIN}\n{content.strip()}\n{_BLOCK_END}\n"
    if not path.is_file():
        return block
    existing = path.read_text(encoding="utf-8")
    matches = list(_BLOCK_RE.finditer(existing))
    stray = (_BLOCK_BEGIN in existing or _BLOCK_END in existing) and not matches
    if len(matches) > 1 or stray:
        raise ValueError(
            f"{path} has ambiguous anvil markers ({len(matches)} well-formed "
            f"block(s); stray marker={stray}). Remove them and re-run."
        )
    if matches:  # exactly one: replace it in place, byte-faithful around it
        m = matches[0]
        return existing[: m.start()] + block.rstrip("\n") + existing[m.end() :]
    body = existing.rstrip("\n")
    return f"{body}\n\n{block}" if body else block


def _strip_instruction(text: str) -> str | None:
    """Remove our managed block from instruction text, preserving the surrounding
    bytes faithfully (user content before AND after our block survives). Returns the
    remaining text, or ``None`` if nothing of the user's is left → delete the file."""
    m = _BLOCK_RE.search(text)
    if m is None:
        return text  # nothing well-formed of ours; leave it exactly as-is
    before, after = text[: m.start()], text[m.end() :]
    if not after.strip():  # our block was appended at EOF — drop the "\n\n" seam
        before = before.rstrip("\n")
        return before + "\n" if before else None
    remaining = before + after  # block was mid-file — rejoin the user's bytes as-is
    return remaining if remaining.strip() else None


# --- backups + an install log so changes are always reversible ----------------
#
# The manifest has two tables. ``paths`` records each absolute path's PRE-anvil
# state ONCE (first touch wins) plus a refcount of which installs reference it —
# so a project-root AGENTS.md shared by two harnesses is backed up
# once and only restored when the LAST harness rolls back. ``installs`` maps an
# install key (project::harness) to the paths it touched, so two projects (or two
# harnesses) never clobber each other's rollback record.


def _manifest_path() -> Path:
    """Cross-project install log. Distinct from a project's own ``.anvil/`` state
    dir — this lives at ``~/.anvil/install-log.json`` and is install-only."""
    return Path.home() / ".anvil" / "install-log.json"


def _install_key(harness: str) -> str:
    return f"{_project_root()}::{harness}"


def _load_manifest() -> dict[str, Any]:
    p = _manifest_path()
    if not p.is_file():
        return {"installs": {}, "paths": {}}
    try:
        m = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"installs": {}, "paths": {}}
    m.setdefault("installs", {})
    m.setdefault("paths", {})
    return m


def _save_manifest(manifest: dict[str, Any]) -> None:
    p = _manifest_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def _backup(path: Path) -> str | None:
    """Copy ``path`` (file OR directory) to ``<path>.anvil-bak`` before we modify
    it; return the backup path. Never clobbers an existing backup — the FIRST copy
    is the pristine, pre-anvil original, kept across re-runs. None if nothing yet.

    The refcount manifest records each path's backup once (first touch wins), so a
    shared AGENTS.md backed up here always holds the USER's content, never ours."""
    if not path.exists():
        return None
    bak = Path(str(path) + _BAK_SUFFIX)
    if not bak.exists():
        if path.is_dir():
            shutil.copytree(path, bak, symlinks=True)
        else:
            shutil.copy2(path, bak)
    return str(bak)


def _remove(path: Path) -> None:
    """Delete a file, symlink, or directory tree if present (symlink-safe: never
    rmtree through a symlink-to-dir, which raises)."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _restore(path: Path, backup: Path) -> None:
    """Put the backed-up content back at ``path``. For dirs: stage a copy, move the
    live dir aside, rename the copy in, then drop the old — so a crash never leaves
    the user with nothing (the live dir or the backup is always intact)."""
    if backup.is_dir():
        staging = Path(str(path) + ".anvil-restoring")
        old = Path(str(path) + ".anvil-old")
        _remove(staging)
        _remove(old)
        shutil.copytree(backup, staging, symlinks=True)
        if path.exists():
            os.replace(path, old)
        os.replace(staging, path)
        _remove(old)
    else:
        # Write THROUGH a symlinked path (preserve the link, update its target).
        shutil.copy2(backup, path)


def _record_writes(harness: str, touched: list[dict[str, Any]]) -> None:
    """Persist (path state + refcount) for everything an install touched. Called
    BEFORE the writes so a crash mid-write still leaves a complete, reversible
    record (each path's pre-anvil state and backup are known)."""
    manifest = _load_manifest()
    key = _install_key(harness)
    for t in touched:
        rec = manifest["paths"].setdefault(
            t["path"],
            {"created": t["created"], "backup": t["backup"],
             "kind": t["kind"], "refs": []},
        )
        if key not in rec["refs"]:
            rec["refs"].append(key)
    manifest["installs"][key] = {
        "ts": datetime.now(UTC).isoformat(),
        "paths": [t["path"] for t in touched],
    }
    _save_manifest(manifest)


def _rollback(harness: str) -> dict[str, Any]:
    """Undo a prior ``install <harness> --write`` for THIS project: for each path,
    drop this install's reference; only when the last reference is gone do we
    restore the user's original (or delete what anvil created)."""
    manifest = _load_manifest()
    key = _install_key(harness)
    entry = manifest["installs"].get(key)
    if not entry:
        return {"harness": harness, "restored": [], "note": "nothing recorded"}

    results: list[dict[str, Any]] = []
    for path_str in entry.get("paths", []):
        rec = manifest["paths"].get(path_str)
        path = Path(path_str)
        if rec is None:
            results.append({"path": path_str, "action": "absent"})
            continue
        if key in rec["refs"]:
            rec["refs"].remove(key)
        if rec["refs"]:  # another harness still wants this shared file/dir
            results.append({"path": path_str, "action": "kept (shared)"})
            continue
        bak = Path(rec["backup"]) if rec.get("backup") else None
        if rec.get("kind") == "instruction" and path.is_file():
            # Surgically remove ONLY our block — preserves the user's content,
            # whether they wrote it before OR after our install. Never blanket-
            # delete a file the user may have adopted.
            remaining = _strip_instruction(path.read_text(encoding="utf-8"))
            if remaining is None:
                _remove(path)
                action = "deleted"
            else:
                path.write_text(remaining, encoding="utf-8")
                action = "stripped"
            if bak is not None:
                _remove(bak)
            results.append({"path": path_str, "action": action})
        elif rec.get("created"):
            existed = path.exists() or path.is_symlink()
            _remove(path)
            if bak is not None:
                _remove(bak)
            results.append(
                {"path": path_str, "action": "deleted" if existed else "absent"}
            )
        elif bak is not None and bak.exists():
            _restore(path, bak)
            _remove(bak)
            results.append({"path": path_str, "action": "restored"})
        else:
            results.append({"path": path_str, "action": "skipped"})
        del manifest["paths"][path_str]

    del manifest["installs"][key]
    _save_manifest(manifest)
    return {"harness": harness, "restored": results}


def _plan_actions(
    h: Harness, *, use_uv_run: bool, root: str | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Compute (mcp_action, instr_action) dicts describing what install would do.

    Each action carries ``path`` (resolved, as str) and the *content* to write,
    plus the ``action`` verb. MCP ``action`` is one of wrote/merged/skipped;
    instruction ``action`` is wrote (new file) or merged (block spliced into the
    user's existing file).
    """
    # --- MCP artifact (JSON merge only; codex/none harnesses skip this) ---
    if h.mcp_merge != "json" or h.mcp_client is None or h.mcp_path is None:
        mcp = {"path": None, "action": "skipped", "content": None, "note": h.note}
    else:
        mcp_dest = _resolve(h.mcp_path, h.mcp_scope)
        existed = mcp_dest.is_file()
        content = _merge_json(mcp_dest, h.mcp_client, use_uv_run=use_uv_run, root=root)
        mcp = {
            "path": str(mcp_dest),
            "action": "merged" if existed else "wrote",
            "content": content,
            "note": h.note,
        }

    # --- Instruction artifact (AGENTS.md content, spliced non-destructively) ---
    # Skip entirely for harnesses whose plugin already ships anvil's instructions
    # (openclaw) — we never touch their files. AGENTS.md ships as package data
    # (anvil/_data/), so it resolves for both checkout and wheel installs; the
    # is_file() guard stays defensive (degrade to "skipped" rather than crash).
    instr_dest = _resolve(h.instr_path, h.instr_scope)
    agents_src = _data_dir() / "AGENTS.md"
    if not h.writes_instructions or not agents_src.is_file():
        return mcp, {"path": str(instr_dest), "action": "skipped", "content": None}
    agents_text = agents_src.read_text(encoding="utf-8")
    instr = {
        "path": str(instr_dest),
        "action": "merged" if instr_dest.is_file() else "wrote",
        "content": _merge_instruction(instr_dest, agents_text),
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
            "Perform the writes (idempotent merge for configs; the instruction "
            "file gets a marked, removable anvil block spliced in — your own "
            "content is never overwritten). Every modified file is backed up to "
            "`<file>.anvil-bak` first. Omit for a safe dry-run."
        ),
    ),
    rollback: bool = typer.Option(  # noqa: B008
        False,
        "--rollback",
        help=(
            "Undo a previous `--write` for this harness: restore each modified "
            "file from its backup and delete any file anvil created."
        ),
    ),
    use_uv_run: bool = typer.Option(  # noqa: B008
        False,
        "--uv-run",
        help=(
            "Emit the explicit `uv run` invocation instead of the bash wrapper "
            "in the written MCP block (automatic on Windows; useful elsewhere "
            "on hosts without bash)."
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
    automations: bool = typer.Option(  # noqa: B008
        False,
        "--automations",
        help=(
            "Codex only: also install anvil's scheduled-automation templates into "
            "~/.codex/automations/ (PAUSED — you activate them in the Codex app). "
            "Removed by --rollback."
        ),
    ),
    cron_recipes: bool = typer.Option(  # noqa: B008
        False,
        "--cron-recipes",
        help=(
            "OpenClaw only: also PRINT opt-in Gateway cron recipes (queue probe, "
            "nightly reconcile, lease watchdog, finish-gate nudge). anvil never runs "
            "or registers them — copy/paste the ones you want."
        ),
    ),
    finish_gate: bool = typer.Option(  # noqa: B008
        False,
        "--finish-gate",
        help=(
            "OpenClaw only: also PRINT the opt-in recipe to install anvil's native "
            "before_agent_finalize finish-gate plugin (blocks finalizing a claimed "
            "task that has no submitted evidence). anvil links/registers nothing."
        ),
    ),
    json_output: bool = JSON_OPTION,
) -> None:
    """Write Anvil's MCP config + instruction file for a target harness.

    Default is a dry-run (prints paths + bytes, mutates nothing); ``--write``
    performs idempotent writes. The MCP envelope is reused from ``mcp-config``'s
    ``CLIENTS`` table (never re-encoded); the instruction file gets anvil's
    ``AGENTS.md`` spliced into a marked, removable block (the user's own content
    is preserved). Every modified file is backed up first and logged so
    ``--rollback`` can undo it. Hook wiring remains harness-owned: Codex/Claude
    discover bundled hooks from the plugin, while install never fakes hook shims
    for MCP-only harnesses.
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

    if rollback:
        # Whether THIS project ever recorded a native install (gate the global
        # removers on it — otherwise rolling back a project that never installed
        # this harness would rip out the global registration another depends on).
        had_install = _install_key(harness) in _load_manifest()["installs"]
        result = _rollback(harness)  # undo file-side footprint (strip AGENTS.md)
        # A native harness's MCP + plugin are GLOBAL. Only remove them when no
        # OTHER project still has an install recorded (refcount across projects).
        others = any(
            k.endswith(f"::{harness}") for k in _load_manifest()["installs"]
        )
        label = _NATIVE_LABEL.get(h.native_installer or "", h.native_installer)
        cli = []
        note = None
        if h.native_installer and had_install and not others:
            cli = _run_or_print(_native_rollback_commands(h.native_installer), run=True)
        elif h.native_installer and others:
            note = f"kept global {label} registration — another project still uses it"

        if json_output:
            emit_success(
                _COMMAND,
                {"harness": harness, "rollback": result, "native": cli, "note": note},
            )
            return
        for c in cli:
            typer.echo(f"# {'ran' if c['ran'] else 'run'}: {c['cmd']}", err=True)
        if note:
            typer.echo(f"# {note}", err=True)
        restored = result.get("restored", [])
        if not restored and not cli and not note:
            typer.echo(f"# Nothing to roll back for {harness}.", err=True)
        for r in restored:
            typer.echo(f"# {r['action']}: {r['path']}", err=True)
        return

    try:
        mcp, instr = _plan_actions(h, use_uv_run=use_uv_run, root=root)
    except ValueError as e:  # ambiguous markers / self-collision — refuse cleanly
        if json_output:
            fail(_COMMAND, str(e), code="bad_request", exit_code=2)
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=2) from e
    if automations and not h.supports_automations:
        msg = f"--automations is Codex-only; {harness} has no automation system."
        if json_output:
            fail(_COMMAND, msg, code="bad_request", exit_code=2)
        typer.echo(f"Error: {msg}", err=True)
        raise typer.Exit(code=2)
    if cron_recipes and h.native_installer != "openclaw":
        msg = f"--cron-recipes is OpenClaw-only; {harness} has no Gateway cron."
        if json_output:
            fail(_COMMAND, msg, code="bad_request", exit_code=2)
        typer.echo(f"Error: {msg}", err=True)
        raise typer.Exit(code=2)
    if finish_gate and h.native_installer != "openclaw":
        msg = f"--finish-gate is OpenClaw-only; {harness} has no native plugin hooks."
        if json_output:
            fail(_COMMAND, msg, code="bad_request", exit_code=2)
        typer.echo(f"Error: {msg}", err=True)
        raise typer.Exit(code=2)

    # Native harnesses (codex, openclaw) install via their own CLI.
    native_cmds = (
        _native_install_commands(h.native_installer, use_uv_run=use_uv_run, root=root)
        if h.native_installer
        else []
    )
    autos = _codex_automation_plan() if (automations and h.supports_automations) else []

    if write:
        # Refuse a DANGLING symlinked dest: writing through it creates a file the
        # user never had, and rollback can only unlink the link — leaving the
        # created target as an un-removable footprint. A *valid* symlink is fine
        # (write + restore both pass through it faithfully). Remove the link + re-run.
        for label, act in (("MCP config", mcp), ("instruction", instr)):
            p = act.get("path")
            if act.get("action") in ("wrote", "merged") and p:
                pp = Path(p)
                if pp.is_symlink() and not pp.exists():
                    msg = f"{label} target {p} is a broken symlink; refusing to write."
                    if json_output:
                        fail(_COMMAND, msg, code="bad_request", exit_code=2)
                    typer.echo(f"Error: {msg}", err=True)
                    raise typer.Exit(code=2)

        # Plan every path's PRE-anvil state and PERSIST it BEFORE writing, so a
        # crash mid-write still leaves a complete, reversible record.
        manifest = _load_manifest()

        def _track(path: Path, kind: str) -> dict[str, Any]:
            existing = manifest["paths"].get(str(path))
            if existing is not None:  # first touch wins — keep true pre-anvil state
                return {"path": str(path), "backup": existing["backup"],
                        "created": existing["created"], "kind": existing.get("kind", kind)}
            created = not path.exists()  # a dangling symlink is refused above
            return {"path": str(path), "backup": None if created else _backup(path),
                    "created": created, "kind": kind}

        touched: list[dict[str, Any]] = []
        if mcp["action"] in ("wrote", "merged"):
            touched.append(_track(Path(mcp["path"]), "config"))
        if instr["action"] in ("wrote", "merged"):
            touched.append(_track(Path(instr["path"]), "instruction"))
        # Automations: track ours (so rollback stays exact), but only WRITE dirs we
        # create fresh — never clobber an existing automation's accrued memory.md or
        # the user's edits to its schedule. A same-named dir we don't own is left
        # entirely alone.
        auto_writes = []
        for a in autos:
            exists = a["dir"].exists()
            if exists and str(a["dir"]) not in manifest["paths"]:
                continue  # not ours — do not touch
            touched.append(_track(a["dir"], "automation"))
            if not exists:
                auto_writes.append(a)
        _record_writes(harness, touched)  # crash-safe: recorded before any write

        if mcp["action"] in ("wrote", "merged"):
            dest = Path(mcp["path"])
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(mcp["content"], encoding="utf-8")
        if instr["action"] in ("wrote", "merged"):
            instr_dest = Path(instr["path"])
            instr_dest.parent.mkdir(parents=True, exist_ok=True)
            instr_dest.write_text(instr["content"], encoding="utf-8")
        for a in auto_writes:
            a["dir"].mkdir(parents=True, exist_ok=True)
            (a["dir"] / "automation.toml").write_text(a["toml"], encoding="utf-8")
            (a["dir"] / "memory.md").write_text("", encoding="utf-8")
        native_results = _run_or_print(native_cmds, run=True)
    else:
        native_results = _run_or_print(native_cmds, run=False)

    if json_output:
        emit_success(
            _COMMAND,
            {
                "harness": harness,
                "write": write,
                "mcp": {"path": mcp["path"], "action": mcp["action"], "note": mcp["note"]},
                "instruction": {"path": instr["path"], "action": instr["action"]},
                "native": native_results,
                # Only present when --automations actually installed something, so a
                # caller can't confuse "none requested" with "installed and paused".
                **(
                    {"automations": {"status": "PAUSED",
                                     "dirs": [str(a["dir"]) for a in autos]}}
                    if autos else {}
                ),
            },
        )
        return

    if native_cmds:
        label = _NATIVE_LABEL.get(h.native_installer or "", h.native_installer)
        cli_bin = native_cmds[0][0]
        # Derive the header from whether commands ACTUALLY ran, not the write flag —
        # `--write` on a host without the harness CLI only prints them.
        if write and not any(c["ran"] for c in native_results):
            head = f"Run these yourself ({cli_bin} not on PATH — {label} writes its own config)"
        elif write:
            head = "Ran"
        else:
            head = f"Run these ({label} writes its own config)"
        typer.echo(f"# {head}:", err=True)
        for c in native_results:
            suffix = "" if c["ok"] in (None, True) else f"  ⚠ {c['detail']}"
            typer.echo(f"    {c['cmd']}{suffix}", err=True)
    # Dry-run output must never claim "(wrote)" — reproducing the install flow
    # on 0.3.0 showed a new user reads that as "install completed" when nothing
    # was written. Map the verbs through the write flag.
    def _display_action(action: str) -> str:
        if write:
            return action
        return {"wrote": "would write", "merged": "would merge"}.get(action, action)

    if mcp["action"] in ("wrote", "merged"):
        typer.echo(
            f"# MCP config ({_display_action(mcp['action'])}) → {mcp['path']}",
            err=True,
        )
        if not write:
            typer.echo(mcp["content"], nl=False)
    elif not native_cmds:
        typer.echo(f"# MCP config: skipped — {mcp['note']}", err=True)

    if instr["action"] in ("wrote", "merged"):
        typer.echo(
            f"# Instruction file ({_display_action(instr['action'])}, "
            f"anvil block from AGENTS.md) → {instr['path']}",
            err=True,
        )
    if autos:
        did = "Wrote" if write else "Would write"
        typer.echo(
            f"# Automations ({did} {len(autos)} PAUSED) "
            f"→ ~/.codex/automations/{', '.join(a['id'] for a in autos)}",
            err=True,
        )
        typer.echo(
            "#   These will NOT run until you activate them in the Codex app "
            "(Automations). Remove with --rollback.",
            err=True,
        )
    if h.native_installer == "openclaw":
        typer.echo(_OPENCLAW_SANDBOX_NOTE, err=True)
        if cron_recipes:
            typer.echo(_openclaw_cron_recipes(str(_project_root())), err=True)
        else:
            typer.echo(
                "#   Tip: --cron-recipes prints opt-in OpenClaw Gateway cron recipes "
                "(queue probe, nightly reconcile, lease watchdog, finish-gate nudge).",
                err=True,
            )
        if finish_gate:
            typer.echo(_openclaw_finish_gate_recipe(), err=True)
        else:
            typer.echo(
                "#   Tip: --finish-gate prints the opt-in recipe to install anvil's "
                "native before_agent_finalize finish-gate plugin (blocks finalizing a "
                "claimed task that has no submitted evidence).",
                err=True,
            )
    if write:
        typer.echo(
            f"# Backed up originals to <file>{_BAK_SUFFIX}. "
            f"Undo with: anvil install {harness} --rollback",
            err=True,
        )
    else:
        typer.echo(
            "# dry-run — nothing was written. Re-run with --write to apply.",
            err=True,
        )
