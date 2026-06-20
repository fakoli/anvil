"""Shared helpers used across all anvil CLI command modules.

This module must NOT import from any sibling command module — it is the
common dependency, not a consumer. Circular imports are impossible by design.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

import click
import typer

if TYPE_CHECKING:
    from anvil.config import Config
    from anvil.state.models import Task
    from anvil.state.sqlite import SqliteBackend

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_STATE_DIR_NAME = ".anvil"
_PLUGIN_MANIFEST = ".claude-plugin/plugin.json"
_PRD_FILENAME = "prd.md"

# T005/B07: env override pointing at the PROJECT ROOT (the directory that
# *contains* .anvil/). Resolution precedence — applied identically by
# the CLI here and by the MCP server (mcp_server._resolve_state_dir):
#
#     explicit path arg/flag (e.g. --cwd)  >  ANVIL_ROOT  >  cwd / walk-up
#
# When ANVIL_ROOT is set but does not contain a valid .anvil/ we
# FAIL with a clear error rather than silently falling back to cwd — a wrong
# env value is a misconfiguration that must surface, not be masked.
_STATE_ROOT_ENV = "ANVIL_ROOT"

# State LAYOUT: where the default (no ANVIL_ROOT, no explicit cwd) state dir lives.
#   "workspace" (default) — a per-project workspace in the user's HOME, keyed by the
#     canonical git repo, so EVERY git worktree of a project shares ONE state.db
#     (fixes state stranded inside individual worktrees).
#   "local" — the legacy in-repo `<cwd>/.anvil` (opt-in via ANVIL_STATE_LAYOUT=local;
#     also what the test suite uses so cwd-relative fixtures keep working).
# ANVIL_ROOT always wins and is always literal (`<ANVIL_ROOT>/.anvil`), in either layout.
_STATE_LAYOUT_ENV = "ANVIL_STATE_LAYOUT"


def _canonical_project_root(loc: Path) -> Path:
    """The shared project root for a location: the MAIN git worktree root (so all
    worktrees of one repo map to the same workspace), else ``loc`` itself."""
    import subprocess

    try:
        r = subprocess.run(
            ["git", "-C", str(loc), "rev-parse",
             "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            # --git-common-dir points at the MAIN worktree's `.git`; its parent is
            # the canonical repo root shared by every worktree.
            return Path(r.stdout.strip()).parent.resolve()
    except (OSError, subprocess.SubprocessError):
        pass
    return loc.resolve()


def _workspace_key(root: Path) -> str:
    """Collision-proof workspace key for a canonical repo root: a legible basename
    PLUS a short hash of the absolute path, so two projects sharing a basename
    (``app``, ``web``) never collide. Mirrors the slug+sha256 recipe in
    ``install.py`` so the two code paths share one convention."""
    import hashlib
    import re

    slug = re.sub(r"[^A-Za-z0-9_-]", "-", root.name) or "project"
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}"


def _home_workspace_base(loc: Path) -> Path:
    """The HOME-dir base (the dir that CONTAINS ``.anvil/``) for a project's shared
    workspace under ``~/.anvil/workspaces/``, keyed by the canonical repo.

    DUAL-KEY (B44), fully backward-compatible: a pre-existing **bare-name**
    workspace (``<repo-name>/``, created by the original #42 code) keeps resolving
    so its db is never orphaned. New projects get the collision-proof hashed key
    (:func:`_workspace_key`). Only one extra ``exists()`` check, on the default
    no-explicit-cwd path."""
    root = _canonical_project_root(loc)
    workspaces = Path.home() / ".anvil" / "workspaces"
    legacy = workspaces / (root.name or "project")
    if (legacy / ".anvil" / "state.db").exists():
        return legacy
    return workspaces / _workspace_key(root)


def _is_local_layout() -> bool:
    """True when ``ANVIL_STATE_LAYOUT=local`` — the legacy in-repo ``<cwd>/.anvil``
    layout (opt-in / tests). Default is the HOME workspace."""
    return os.environ.get(_STATE_LAYOUT_ENV, "workspace").strip().lower() == "local"


class StateRootError(click.ClickException):
    """ANVIL_ROOT is set but does not point at a valid project root.

    A ``click.ClickException`` so Typer/Click print a clean ``Error: ...`` line
    to stderr and exit non-zero (instead of a traceback), for both real CLI
    invocations and ``CliRunner`` tests.
    """

    exit_code = 1


# ---------------------------------------------------------------------------
# Backend helpers
# ---------------------------------------------------------------------------


def _resolve_base_dir(cwd: Path | None) -> Path:
    """Return the absolute PROJECT ROOT (the dir that contains .anvil/).

    Centralizes the base-directory precedence so reads (``_resolve_state_dir``)
    and writes (``init`` / MCP ``init_project``) agree on *which* directory the
    project lives in — the whole point of MUST-FIX 1 (no silent divergence
    where ``init`` writes to cwd while ``status`` reads from the env root).

    Resolution precedence (T005/B07):

    1. ``cwd`` — an explicit path arg/flag (e.g. ``--cwd``) always wins.
    2. ``ANVIL_ROOT`` env var — points at the project root. Used only
       when no explicit ``cwd`` was passed. An empty/whitespace value is
       treated as unset.
    3. ``Path.cwd()`` — the original/default behaviour.

    Unlike :func:`_resolve_state_dir`, this does NOT require
    ``<base>/.anvil`` to already exist — ``init`` calls it to decide
    where to *create* the project. The existence check (fail-loud on a wrong
    env value) lives in :func:`_resolve_state_dir`, which the read commands use.
    """
    local = _is_local_layout()

    # 1. Explicit cwd wins. In workspace layout it maps to THAT project's shared
    #    home workspace (so the MCP server + CLI agree from any worktree).
    if cwd is not None:
        return cwd.resolve() if local else _home_workspace_base(cwd)

    # 2. ANVIL_ROOT is always a literal override (`<ANVIL_ROOT>/.anvil`).
    env_root = os.environ.get(_STATE_ROOT_ENV)
    if env_root is not None and env_root.strip() != "":
        return Path(env_root).expanduser().resolve()

    # 3. Default: the home workspace shared across all worktrees of the project,
    #    unless ANVIL_STATE_LAYOUT=local keeps state in-repo (legacy / tests).
    return Path.cwd().resolve() if local else _home_workspace_base(Path.cwd())


def _resolve_state_dir(cwd: Path | None) -> Path:
    """Return the absolute path to the .anvil/ directory.

    Resolution precedence (T005/B07):

    1. ``cwd`` — an explicit path arg/flag (e.g. ``--cwd``) always wins.
    2. ``ANVIL_ROOT`` env var — points at the project root (the dir
       containing ``.anvil/``). Used only when no explicit ``cwd`` was
       passed. If it is set but ``<root>/.anvil`` does not exist we raise
       :class:`StateRootError` — we never silently fall back to cwd, because a
       wrong env value is a misconfiguration that must be surfaced.
    3. ``Path.cwd()`` — the original/default behaviour (unchanged when the env
       var is unset and no ``cwd`` was passed).

    Args:
        cwd: Explicit working directory. ``None`` defers to the env var, then
            ``Path.cwd()``.

    Returns:
        Absolute Path pointing at ``<base>/.anvil/``.

    Raises:
        StateRootError: ``ANVIL_ROOT`` is set (and no explicit ``cwd``
            was given) but does not contain a ``.anvil/`` directory.
    """
    base = _resolve_base_dir(cwd)
    state_dir = base / _STATE_DIR_NAME

    # Fail-loud ONLY when the env var supplied the base (no explicit cwd and the
    # env var is set/non-empty): a wrong env value is a misconfiguration that
    # must surface, not silently fall back to cwd. When cwd was unset and the
    # env var is unset we are on the plain cwd path, where a missing state dir
    # is the normal "not initialized yet" case the callers handle themselves.
    env_root = os.environ.get(_STATE_ROOT_ENV)
    env_supplied_base = (
        cwd is None and env_root is not None and env_root.strip() != ""
    )
    if env_supplied_base and not state_dir.is_dir():
        raise StateRootError(
            f"{_STATE_ROOT_ENV}={env_root!r} does not contain a "
            f"'.anvil/' directory (looked at {state_dir}). "
            "Point it at the project root, or unset it to use the current "
            "working directory."
        )

    return state_dir


def _open_backend(state_dir: Path) -> SqliteBackend:
    """Instantiate a SqliteBackend, call initialize(), and return it.

    The caller is responsible for calling .close() when done — use a try/finally.

    Args:
        state_dir: Absolute path to the .anvil/ directory.

    Returns:
        An initialized SqliteBackend ready for queries and mutations.
    """
    from anvil.clock import SystemClock
    from anvil.config import read_events_storage
    from anvil.state.sqlite import SqliteBackend as _SqliteBackend

    db_path = str(state_dir / "state.db")
    events_path = str(state_dir / "events.jsonl")
    backend = _SqliteBackend(
        db_path=db_path,
        events_path=events_path,
        clock=SystemClock(),
        # v1.22.0: the storage mode decides the event-id format and the
        # replay strategy, so it must be resolved BEFORE the backend opens —
        # not by whichever command happens to read config.yaml later.
        events_storage=read_events_storage(state_dir / "config.yaml"),
    )
    backend.initialize()
    return backend


def _slug(text: str) -> str:
    """Convert a human-readable name to a URL-safe lowercase slug.

    Example: "My Project" → "my-project"
    """
    lowered = text.lower()
    stripped = re.sub(r"[^a-z0-9]+", "-", lowered)
    return stripped.strip("-") or "project"


def _is_plugin_root(directory: Path) -> bool:
    """Return True if *directory* is the anvil plugin root.

    Detects the plugin root by checking for a .claude-plugin/plugin.json that
    declares name == "anvil".  This prevents accidental initialisation
    inside the plugin directory itself.
    """
    manifest = directory / _PLUGIN_MANIFEST
    if not manifest.exists():
        return False
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        return bool(data.get("name") == "anvil")
    except (json.JSONDecodeError, OSError):
        return False


def _require_state_dir(
    state_dir: Path,
    *,
    command: str | None = None,
    json_output: bool = False,
) -> None:
    """Exit 1 with a helpful message if the state directory does not exist.

    When ``json_output`` is True a JSON error envelope (code
    ``"not_initialized"``) is emitted instead of the human stderr line, so
    commands that support ``--json`` keep stdout pipeable even on the
    not-initialized path. ``command`` names the envelope's ``command`` field;
    it is required whenever ``json_output`` is True.
    """
    if not state_dir.exists():
        if json_output:
            from anvil.cli._json import fail

            fail(
                command or "",
                "anvil not initialized in this project. "
                "Run `anvil init` first.",
                code="not_initialized",
            )
        typer.echo(
            "Error: anvil not initialized in this project. "
            "Run `anvil init` first.",
            err=True,
        )
        raise typer.Exit(code=1)


def _get_project_id(backend: SqliteBackend) -> str:
    """Return the project ID from the backend, or 'project' as a fallback."""
    project = backend.get_project()
    if project is not None:
        return project.id
    return "project"


# ---------------------------------------------------------------------------
# Stale-claim reaper helper (shared by all mutating commands)
# ---------------------------------------------------------------------------


def _reap_stale_claims(backend: SqliteBackend) -> None:
    """Run the stale-claim detector against *backend*.

    Called at the start of claim/release/renew/next so users always see
    consistent state without having to think about expiry.  Operational
    failures (e.g. ``StateLocked`` from a concurrent writer holding the
    busy_timeout, ``TransactionAborted`` from a transient race) are
    swallowed — reaping is best-effort and a stale claim that slips through
    will be caught on the next invocation.

    ``SchemaMismatch`` (CL-3) is **not** swallowed: a DB whose
    ``user_version`` does not match the code's ``SCHEMA_VERSION`` is a
    genuine "your install needs migration" signal, not a transient hiccup.
    Hiding it behind a confusing secondary error from the primary command
    leaves users debugging the wrong layer.  Let it propagate so the CLI's
    top-level error handler can surface the clean schema message.
    """
    from anvil.claims.stale import detect_and_release_stale
    from anvil.clock import SystemClock
    from anvil.state.backend import (
        SchemaMismatch,
        StateLocked,
        TransactionAborted,
    )

    try:
        detect_and_release_stale(backend, SystemClock())
    except SchemaMismatch:
        raise  # CL-3: surface DB-version drift; do not mask
    except (StateLocked, TransactionAborted):
        pass  # operational; reaping is best-effort and self-healing
    except Exception:  # noqa: BLE001
        # Greptile PR #48 P2: raw sqlite3.OperationalError ("unable to open
        # database file", disk full, etc.) and any other unwrapped exception
        # must not block the primary command. Reaping is opportunistic — if
        # it fails for unexpected reasons we still log nothing here (per the
        # "never noisy" contract) and let the primary op proceed. The next
        # invocation will retry.
        pass


# ---------------------------------------------------------------------------
# Config-load helper (shared soft-load used by claim/renew and others)
# ---------------------------------------------------------------------------


def _load_config_optional(state_dir: Path) -> Config | None:
    """Load ``.anvil/config.yaml`` if present; return None on miss/error.

    Soft-load contract shared across the CLI: an absent or unreadable config
    never blocks a command — callers fall back to dataclass defaults. A bad
    config emits a stderr warning so the user notices without a hard error.
    Mirrors ``cli/plan.py::_load_config_optional`` but lives here so command
    modules (claim, renew, …) can reuse it without importing a sibling.

    T016/B17 — the global-config layer
    (``~/.config/anvil/config.yaml``) is merged UNDER the project
    config: a project key overrides the same global key, a global-only key
    supplies a default, and a key in neither falls through to the dataclass
    default. The project config must still exist for this command to read a
    config at all (returning a Config requires project_name/project_id, which
    the global layer is not required to carry).
    """
    import yaml

    config_path = state_dir / "config.yaml"
    if not config_path.exists():
        return None
    try:
        from anvil.config import load_merged_config

        return load_merged_config(config_path)
    except (FileNotFoundError, OSError, ValueError, yaml.YAMLError) as exc:
        typer.echo(
            f"Warning: config.yaml load failed "
            f"({type(exc).__name__}: {exc}); proceeding with defaults. "
            "Fix config.yaml and re-run to use config.",
            err=True,
        )
        return None


def _lease_manager_kwargs(
    config: Config | None,
    *,
    lease_override: float | None = None,
) -> dict[str, float]:
    """Return ClaimManager lease kwargs from *config*, or empty for defaults.

    BUG 2: the CLI claim/renew commands previously built ClaimManager without
    threading default_lease_minutes / default_heartbeat_minutes from
    config.yaml, so a configured lease was silently ignored and every CLI
    claim used the 60-minute ClaimManager default (the MCP path did wire it).
    When *config* is None (no/broken config.yaml) we return an empty dict so
    ClaimManager keeps its own 60/5 defaults — preserving prior behaviour for
    projects without a config.

    T016/B17 — lease precedence: explicit CLI arg > project config > global
    config > built-in default. *config* is the already-merged
    project-over-global config (see :func:`_load_config_optional`), so it
    collapses the middle two levels. *lease_override* is the explicit
    ``--lease`` CLI flag; when not None it wins over both the configured lease
    AND, when *config* is None, the ClaimManager default — so ``--lease`` works
    even in a project without a config.yaml.
    """
    if config is None and lease_override is None:
        return {}

    kwargs: dict[str, float] = {}
    if config is not None:
        kwargs["default_lease_minutes"] = config.default_lease_minutes
        kwargs["default_heartbeat_minutes"] = config.default_heartbeat_minutes
    if lease_override is not None:
        kwargs["default_lease_minutes"] = lease_override
    return kwargs


# ---------------------------------------------------------------------------
# Score helper (used by plan.py and init_status.py)
# ---------------------------------------------------------------------------


def _scores_complete(task: Task) -> bool:
    """Return True if all six score dimensions are populated."""
    s = task.scores
    return all(
        v is not None
        for v in (
            s.complexity,
            s.parallelizability,
            s.context_load,
            s.blast_radius,
            s.review_risk,
            s.agent_suitability,
        )
    )
