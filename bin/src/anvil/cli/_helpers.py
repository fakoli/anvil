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
_PRDS_DIR_NAME = "prds"

# The PRD ids that denote the implicit/default PRD. Both ``DEFAULT_PRD_ID``
# ('default', the stored model id) and the parse-time sentinel ('prd', what
# every single-PRD caller passes) map to the bare ``.anvil/prd.md`` source so
# the default PRD keeps its pre-multi-PRD on-disk location.
_DEFAULT_PRD_IDS = ("default", "prd")

_ACTOR_FALLBACK = "agent"

# T018: env override naming the PRD partition a mutating command targets, the
# PRD analogue of ``ANVIL_ACTOR``. Resolution precedence (applied identically
# by the CLI ``resolve_prd_id`` and the MCP ``_resolve_prd_id``):
#
#     explicit arg/flag (--prd)  >  ANVIL_PRD  >  single PRD | default | error
#
# When neither an explicit id nor the env supplies one, a project with exactly
# one PRD resolves to it, a project with a marked default PRD resolves to that,
# and a project with several non-default PRDs is AMBIGUOUS — we error rather
# than silently picking one, the whole point of a shared resolver.
_PRD_ENV = "ANVIL_PRD"


# ---------------------------------------------------------------------------
# Actor identity (B47) — ONE resolver for claim / heartbeat / gate / guard / MCP
# ---------------------------------------------------------------------------


# Env vars that, when set, disambiguate concurrent agent loops on ONE
# machine/user (B47/#103). Prefer the anvil-owned id; fall back to the Claude
# Code harness session id. Every subprocess of a single loop inherits the same
# value, so claim/heartbeat/gate/guard still resolve the SAME actor.
_SESSION_ENV_VARS = ("ANVIL_SESSION_ID", "CLAUDE_CODE_SESSION_ID")


def _session_discriminator() -> str | None:
    """A per-loop session id — shared across ONE loop's subprocesses but
    distinct between sibling loops — or None when no session env is set. Sliced
    short so the composed actor id stays readable."""
    for var in _SESSION_ENV_VARS:
        value = os.environ.get(var)
        if value and value.strip():
            return value.strip()[:12]
    return None


def _base_default_actor() -> str:
    """The derived default identity when no explicit actor is given: ``$USER``,
    else the per-runner signing-key fingerprint, else ``"agent"``. Resolved
    lazily and fault-tolerantly — any failure (no crypto, unwritable key dir)
    falls through rather than breaking a claim/gate."""
    value = os.environ.get("USER")
    if value and value.strip():
        return value.strip()
    try:
        from anvil import signing

        _, _, signer_id = signing.load_or_create_signer()
        if signer_id:
            return signer_id
    except Exception:  # noqa: BLE001 — actor resolution must never crash a claim/gate
        pass
    return _ACTOR_FALLBACK


def resolve_actor(explicit: str | None = None) -> str:
    """Resolve the actor identity used for claims, heartbeats, and gates.

    B47: every surface that touches a claim must resolve the SAME identity, or a
    claim made under one actor is heartbeated/gated under another — renewal then
    renews zero leases and the finish-gate (seeing no matching claim) fails
    silently OPEN.

    Precedence::

        explicit arg > $ANVIL_ACTOR > $ANVIL_GATE_ACTOR (legacy) >
        (($USER | signing-key fingerprint | "agent") + session discriminator)

    The first three are returned verbatim — the intentional coordination knobs.
    The DERIVED default (``$USER`` / fingerprint / ``"agent"``) instead gets a
    per-loop **session discriminator** appended when ``$ANVIL_SESSION_ID`` or
    ``$CLAUDE_CODE_SESSION_ID`` is set (#103/B47): without it, two concurrent
    loops on one machine/user collapse to the SAME derived id, so a second
    loop's ``claim`` is treated as the owner and RENEWS the lease instead of
    conflicting — lease mutual-exclusion becomes a no-op between siblings. The
    discriminator is stable across ONE loop's subprocesses (they inherit the
    same env) but differs between siblings, so in-loop hooks still agree while
    sibling loops are distinguishable by default. Set ``$ANVIL_ACTOR`` to pin an
    explicit identity and opt a loop out.

    Always returns a non-empty, stripped string.
    """
    if explicit and explicit.strip():
        return explicit.strip()
    for env_var in ("ANVIL_ACTOR", "ANVIL_GATE_ACTOR"):
        value = os.environ.get(env_var)
        if value and value.strip():
            return value.strip()
    base = _base_default_actor()
    session = _session_discriminator()
    return f"{base}-{session}" if session else base


# ---------------------------------------------------------------------------
# PRD partition resolution (T018) — ONE resolver shared by CLI + MCP
# ---------------------------------------------------------------------------

# Shared ``--prd`` Typer option. Defined here so every mutating command imports
# the SAME flag/envvar wiring (``--prd`` / ``ANVIL_PRD``) instead of each
# re-declaring it, and so the CLI flag and the MCP ``prd_id`` argument resolve
# through the identical precedence (:func:`resolve_prd_id`).
PRD_OPTION = typer.Option(  # noqa: B008
    None,
    "--prd",
    envvar=_PRD_ENV,
    help=(
        "PRD partition to target (multi-PRD). Precedence: this flag > "
        "$ANVIL_PRD > the single PRD / the default PRD. With several "
        "non-default PRDs and none chosen, the command errors rather than "
        "guessing. Omit on a single-PRD project for unchanged behaviour."
    ),
)


class PrdAmbiguityError(click.ClickException):
    """No PRD could be resolved unambiguously (T018).

    Raised when neither an explicit ``--prd``/arg nor ``$ANVIL_PRD`` names a
    PRD and the project has several PRDs with no single default to fall back
    on. A ``click.ClickException`` so Typer/Click print a clean ``Error: ...``
    line and exit non-zero instead of a traceback.
    """

    exit_code = 1


def resolve_prd_id(backend: SqliteBackend, explicit: str | None = None) -> str:
    """Resolve which PRD partition a command targets.

    The ONE resolver shared by the CLI (every mutating command's ``--prd``
    flag) and the MCP server (:func:`mcp_server._resolve_prd_id`), so both
    surfaces pick the identical PRD for identical DB + env inputs.

    Precedence::

        explicit arg/flag (--prd)  >  $ANVIL_PRD  >
            single PRD | default PRD | ambiguity-error

    1. ``explicit`` — a non-empty, stripped ``--prd`` value (or positional arg)
       always wins. Returned verbatim; existence is the caller's concern.
    2. ``$ANVIL_PRD`` — the env override, consulted only when no explicit id was
       passed. (``PRD_OPTION`` wires ``envvar=ANVIL_PRD`` so the CLI flag already
       reads it; the env tier here is for callers that pass ``explicit=None``
       directly, e.g. the MCP server.)
    3. Otherwise inspect the DB: exactly one PRD → that PRD's id; several PRDs
       with one marked default → the default id; several PRDs with no default →
       :class:`PrdAmbiguityError` (we never silently pick one).

    Args:
        backend: An initialized backend for the project being targeted.
        explicit: The ``--prd`` value (or positional override). ``None``/blank
            defers to ``$ANVIL_PRD`` then the DB tiers.

    Returns:
        The resolved PRD id (always a non-empty, stripped string).

    Raises:
        PrdAmbiguityError: Several PRDs exist, none is the default, and neither
            an explicit id nor ``$ANVIL_PRD`` chose one.
    """
    if explicit and explicit.strip():
        return explicit.strip()

    env_value = os.environ.get(_PRD_ENV)
    if env_value and env_value.strip():
        return env_value.strip()

    prds = backend.list_prds()
    if len(prds) == 1:
        return prds[0].id

    default_id = backend.default_prd_id()
    if default_id is not None:
        return default_id

    # Two distinct failure shapes land here; describe each one honestly rather
    # than claiming "multiple" for both. ZERO PRDs (a freshly-initialized
    # project before the first parse_prd) needs "create/parse a PRD first";
    # SEVERAL non-default PRDs need "pick one with --prd/$ANVIL_PRD".
    if not prds:
        raise PrdAmbiguityError(
            "No PRD exists yet, so no partition can be resolved. "
            "Parse a PRD first (e.g. `anvil prd parse`), or pass --prd <id> / "
            f"set {_PRD_ENV} to name one."
        )
    available = ", ".join(p.id for p in prds)
    raise PrdAmbiguityError(
        "Multiple PRDs exist and none is the default; cannot pick one "
        f"automatically. Available PRDs: {available}. "
        f"Pass --prd <id> or set {_PRD_ENV}."
    )


def canonical_prd_id(prd_id: str) -> str:
    """Collapse a default-PRD sentinel to the STORED model id (``'default'``).

    ``resolve_prd_id`` returns an explicit ``--prd``/``$ANVIL_PRD`` value
    verbatim, so the parse-time sentinel ``'prd'`` (a documented spelling of the
    default PRD, per ``_DEFAULT_PRD_IDS`` / ``prd_source_path`` / ``parse_prd``)
    survives unchanged. But every persisted row stores the default PRD with
    ``id='default'`` (the write path collapses ``'prd'`` via
    ``template._model_prd_id``). Read/filter/review surfaces that look a PRD up
    by id (``get_prd``) or compare against a stored ``prd_id``
    (``list_tasks(prd_id=...)``, ``task.prd_id != ...``) must therefore collapse
    the sentinel first, or ``--prd prd`` spuriously misses the default partition.
    Named (non-default) ids pass through unchanged.
    """
    from anvil.state.models import DEFAULT_PRD_ID

    return DEFAULT_PRD_ID if prd_id in _DEFAULT_PRD_IDS else prd_id

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
    (``app``, ``web``) never collide. Modeled on the slug+sha256 recipe in
    ``install.py`` (same collision convention)."""
    import hashlib
    import re

    slug = re.sub(r"[^A-Za-z0-9_-]", "-", root.name) or "project"
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}"


def _home_dir() -> Path:
    """Return the user home Anvil should use for workspace state.

    Prefer HOME when explicitly set so tests and Unix-like shells can isolate
    the workspace location consistently on Windows too, where ``Path.home()``
    normally follows USERPROFILE instead.
    """
    path_home = Path.home()
    path_home_resolved = path_home.resolve()
    home = os.environ.get("HOME")
    userprofile = os.environ.get("USERPROFILE")
    if userprofile is not None and userprofile.strip():
        try:
            if path_home_resolved != Path(userprofile).expanduser().resolve():
                return path_home
        except OSError:
            return path_home

    if home is not None and home.strip():
        home_path = Path(home).expanduser().resolve()
        if userprofile is None and path_home_resolved != home_path:
            return path_home
        return home_path
    return path_home


def _home_workspace_base(loc: Path) -> Path:
    """The HOME-dir base (the dir that CONTAINS ``.anvil/``) for a project's shared
    workspace under ``~/.anvil/workspaces/``, keyed by the canonical repo.

    DUAL-KEY (B44), fully backward-compatible: a pre-existing **bare-name**
    workspace (``<repo-name>/``, created by the original #42 code) keeps resolving
    so its db is never orphaned. New projects get the collision-proof hashed key
    (:func:`_workspace_key`). Only one extra ``exists()`` check, on the default
    no-explicit-cwd path."""
    root = _canonical_project_root(loc)
    workspaces = _home_dir() / ".anvil" / "workspaces"
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


def _resolve_project_dir(cwd: Path | None) -> Path:
    """Return the absolute path to the user's PROJECT directory — the place
    git operations (branch/worktree creation) must run.

    This is deliberately NOT :func:`_resolve_base_dir`: in the default
    workspace layout the base dir maps to ``~/.anvil/workspaces/<key>``,
    which is never a git repository, so any git op resolved through it
    silently no-ops ("git branch not created — not a git repository").
    State lives in the workspace; the *work* lives in the project.

    Precedence mirrors the state resolvers minus the workspace mapping:

    1. ``cwd`` — an explicit path arg/flag always wins.
    2. ``ANVIL_ROOT`` env var — the declared project root.
    3. ``Path.cwd()`` — where the user invoked anvil.

    In ``ANVIL_STATE_LAYOUT=local`` this returns the same directory as
    :func:`_resolve_base_dir`, so legacy-layout behaviour is unchanged.
    """
    if cwd is not None:
        return cwd.resolve()
    env_root = os.environ.get(_STATE_ROOT_ENV)
    if env_root is not None and env_root.strip() != "":
        return Path(env_root).expanduser().resolve()
    return Path.cwd().resolve()


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


def _resolve_project_root(cwd: Path | None) -> Path:
    """Return the project CHECKOUT root — where plan-declared ``likely_files``
    live. Like :func:`_resolve_base_dir` (explicit ``cwd`` > ``ANVIL_ROOT`` >
    ``Path.cwd()``) but NEVER remapped to the shared ``~/.anvil/workspaces``
    state dir, which under the HOME-workspace layout is not the git checkout.
    """
    if cwd is not None:
        return cwd.resolve()
    env_root = os.environ.get(_STATE_ROOT_ENV)
    if env_root is not None and env_root.strip() != "":
        return Path(env_root).expanduser().resolve()
    return Path.cwd().resolve()


def prd_source_path(state_dir: Path, prd_id: str) -> Path:
    """Return the on-disk markdown source for a PRD partition (T016).

    The default PRD keeps its pre-multi-PRD location ``<state_dir>/prd.md``;
    every named PRD lives under a ``prds/`` collection as
    ``<state_dir>/prds/<prd_id>.md``. Both the stored model id (``'default'``)
    and the parse-time sentinel (``'prd'``) resolve to the bare ``prd.md`` so
    callers can pass either without special-casing.

    Args:
        state_dir: Absolute path to the ``.anvil/`` directory.
        prd_id: The PRD partition id. ``'default'``/``'prd'`` → ``prd.md``;
            any other id → ``prds/<prd_id>.md``.

    Returns:
        Absolute Path to the PRD's markdown source.
    """
    if prd_id in _DEFAULT_PRD_IDS:
        return state_dir / _PRD_FILENAME
    return state_dir / _PRDS_DIR_NAME / f"{prd_id}.md"


def display_path(path: Path) -> str:
    """Render paths portably in user-facing CLI/MCP messages."""
    return path.as_posix()


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
    # B46 — max-claim-age = effective lease x configured multiplier. Computed
    # from whichever lease wins (override > config) so --lease scales the cap
    # too. When config is None, ClaimManager applies its own 4x default.
    if config is not None and "default_lease_minutes" in kwargs:
        kwargs["max_claim_age_minutes"] = (
            kwargs["default_lease_minutes"] * config.max_claim_age_multiplier
        )
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
