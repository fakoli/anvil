"""Shared helpers used across all anvil CLI command modules.

This module must NOT import from any sibling command module — it is the
common dependency, not a consumer. Circular imports are impossible by design.
"""

from __future__ import annotations

import base64
import binascii
import errno
import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
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

# Version 1 admits at most 2 MiB of exact PRD source bytes.  Read paths use a
# bounded ``limit + 1`` probe so an over-limit source is refused before
# unbounded allocation growth or UTF-8 decoding.
MAX_PRD_SOURCE_BYTES_V1 = 2_097_152
_WINDOWS_RESERVED_PRD_STEMS = {
    "AUX",
    "CLOCK$",
    "CON",
    "CONIN$",
    "CONOUT$",
    "NUL",
    "PRN",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}

# The PRD ids that denote the implicit/default PRD. Both ``DEFAULT_PRD_ID``
# ('default', the stored model id) and the parse-time sentinel ('prd', what
# every single-PRD caller passes) map to the bare ``.anvil/prd.md`` source so
# the default PRD keeps its pre-multi-PRD on-disk location.
_DEFAULT_PRD_IDS = ("default", "prd")
_OPEN_SUPPORTS_DIR_FD = os.open in os.supports_dir_fd
_STAT_SUPPORTS_DIR_FD = os.stat in os.supports_dir_fd

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
def _session_discriminator() -> str | None:
    """A per-loop session id — shared across ONE loop's subprocesses but
    distinct between sibling loops — or None when no session env is set.
    Delegates to the leaf ``anvil.naming`` module (the claims engine uses the
    SAME resolution for the distinct-actor fail-fast), sliced to 12 chars
    here because this feeds the human-readable actor suffix; the fail-fast
    compares the FULL id."""
    from anvil.naming import session_discriminator

    value = session_discriminator()
    return value[:12] if value else None


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


class PrdSourceIngestError(click.ClickException):
    """Bounded, path-safe refusal from PRD source identity or ingestion."""

    exit_code = 1

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class IngestedPrdSource:
    """One exact, bounded PRD source read with no retained filesystem path."""

    source_bytes: bytes
    markdown: str
    source_sha256: str
    source_size_bytes: int
    source_encoding: str = "utf-8"


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
    # isinstance guard: a programmatic caller that forgets to pass the CLI
    # ``prd`` arg leaks a Typer OptionInfo sentinel here — degrade to the
    # default resolution instead of an invisible AttributeError (see
    # hooks._status_hook_line).
    if isinstance(explicit, str) and explicit.strip():
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


def validate_prd_id(prd_id: str) -> str:
    """Return a plain canonical source identifier or refuse before path use.

    The public Version 1 scoped-reference DTO is the single acceptance
    authority.  Filesystem-specific restrictions must be handled by the
    reversible storage mapping, never by silently narrowing this wire ID set.
    """
    if not isinstance(prd_id, str):
        raise PrdSourceIngestError("invalid_prd_id", "PRD id is invalid")
    plain_prd_id = str.__str__(prd_id)
    from anvil.read_contracts import PrdScopedRefV1

    try:
        validated_id = PrdScopedRefV1(prd_id=plain_prd_id).prd_id
    except ValueError as exc:
        raise PrdSourceIngestError("invalid_prd_id", "PRD id is invalid") from exc
    return validated_id


def prd_source_filename(prd_id: str) -> str:
    """Map one accepted PRD ID to a portable, collision-free filename."""
    validated_id = validate_prd_id(prd_id)
    windows_stem = validated_id.partition(".")[0].upper()
    if (
        windows_stem in _WINDOWS_RESERVED_PRD_STEMS
        or validated_id != validated_id.lower()
    ):
        # Base32 is reversible, case-insensitive-filesystem safe, and keeps the
        # longest accepted 128-byte ID below Windows' 255-character component
        # ceiling.  Hex would expand that valid wire ID beyond the ceiling.
        encoded_id = base64.b32encode(validated_id.encode("ascii")).decode("ascii")
        encoded_id = encoded_id.rstrip("=")
        return f"_anvil-prd-{encoded_id}.md"
    return f"{validated_id}.md"


def prd_id_from_source_filename(filename: str) -> str:
    """Reverse :func:`prd_source_filename` without accepting aliases."""
    prefix = "_anvil-prd-"
    suffix = ".md"
    if filename.startswith(prefix) and filename.endswith(suffix):
        encoded_id = filename[len(prefix) : -len(suffix)]
        try:
            padding = "=" * (-len(encoded_id) % 8)
            prd_id = base64.b32decode(encoded_id + padding).decode("ascii")
        except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
            raise PrdSourceIngestError("invalid_prd_id", "PRD id is invalid") from exc
        validated_id = validate_prd_id(prd_id)
        if prd_source_filename(validated_id) != filename:
            raise PrdSourceIngestError("invalid_prd_id", "PRD id is invalid")
        return validated_id
    if not filename.endswith(suffix):
        raise PrdSourceIngestError("invalid_prd_id", "PRD id is invalid")
    validated_id = validate_prd_id(filename[: -len(suffix)])
    if prd_source_filename(validated_id) != filename:
        raise PrdSourceIngestError("invalid_prd_id", "PRD id is invalid")
    return validated_id

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
    every named PRD lives under a ``prds/`` collection. Most names retain the
    legacy ``<prd_id>.md`` spelling; Windows-reserved stems and IDs containing
    uppercase characters use a reversible, length-bounded Base32 filename.
    Encoding uppercase IDs preserves distinct wire identities such as ``A``
    and ``a`` on case-insensitive filesystems. Both the stored model id
    (``'default'``) and the parse-time sentinel (``'prd'``) resolve to the bare
    ``prd.md`` so callers can pass either without special-casing.

    Args:
        state_dir: Absolute path to the ``.anvil/`` directory.
        prd_id: The PRD partition id. ``'default'``/``'prd'`` → ``prd.md``;
            any other id → its portable filename under ``prds/``.

    Returns:
        Absolute Path to the PRD's markdown source.
    """
    validated_id = validate_prd_id(prd_id)
    if validated_id in _DEFAULT_PRD_IDS:
        return state_dir / _PRD_FILENAME
    return state_dir / _PRDS_DIR_NAME / prd_source_filename(validated_id)


def _open_verified_prd_source(
    source_path: Path,
    *,
    containment_root: Path | None,
    required_parent: Path | None,
) -> tuple[int, os.stat_result]:
    """Open once, then bind the handle to a regular contained source."""
    if (containment_root is None) != (required_parent is None):
        raise ValueError("containment_root and required_parent must be provided together")

    if containment_root is not None and required_parent is not None:
        if (
            os.name != "nt"
            and hasattr(os, "O_DIRECTORY")
            and _OPEN_SUPPORTS_DIR_FD
            and _STAT_SUPPORTS_DIR_FD
        ):
            return _open_contained_prd_source_posix(
                source_path,
                containment_root=containment_root,
                required_parent=required_parent,
            )
        if os.name != "nt":
            raise PrdSourceIngestError(
                "source_unavailable",
                "platform cannot securely open a contained PRD source",
            )

    descriptor, opened = _open_prd_source_path(source_path)
    if containment_root is None or required_parent is None:
        return descriptor, opened

    try:
        resolved_root = containment_root.resolve(strict=True)
        relative_parent = required_parent.relative_to(containment_root)
        expected_source = resolved_root / relative_parent / source_path.name
        opened_source = _windows_final_path_for_descriptor(descriptor)
        if os.path.normcase(os.path.normpath(str(opened_source.parent))) != os.path.normcase(
            os.path.normpath(str(expected_source.parent))
        ):
            raise PrdSourceIngestError(
                "source_outside_prd_directory",
                "PRD source escapes its contained source directory",
            )
        if opened_source.name != expected_source.name:
            raise PrdSourceIngestError(
                "source_case_alias",
                "PRD source spelling aliases another wire identity",
            )
        return descriptor, opened
    except PrdSourceIngestError:
        os.close(descriptor)
        raise
    except (OSError, ValueError) as exc:
        os.close(descriptor)
        raise PrdSourceIngestError(
            "source_unavailable",
            "cannot verify PRD source containment",
        ) from exc


def _prd_source_open_flags() -> int:
    flags = os.O_RDONLY
    for flag_name in ("O_BINARY", "O_CLOEXEC", "O_NOINHERIT", "O_NONBLOCK", "O_NOFOLLOW"):
        flags |= getattr(os, flag_name, 0)
    return flags


def _refuse_non_regular_source(source_stat: os.stat_result) -> None:
    if stat.S_ISREG(source_stat.st_mode):
        return
    code = (
        "source_outside_prd_directory"
        if stat.S_ISLNK(source_stat.st_mode)
        else "source_not_regular"
    )
    raise PrdSourceIngestError(code, "PRD source is not a regular contained file")


def _open_prd_source_path(source_path: Path) -> tuple[int, os.stat_result]:
    """Open an explicit source and bind the descriptor to its current path."""
    try:
        before_open = os.stat(source_path, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise PrdSourceIngestError(
            "source_not_found",
            "PRD source not found",
        ) from exc
    except OSError as exc:
        raise PrdSourceIngestError(
            "source_unavailable",
            "cannot inspect PRD source",
        ) from exc
    _refuse_non_regular_source(before_open)
    try:
        descriptor = os.open(source_path, _prd_source_open_flags())
    except FileNotFoundError as exc:
        raise PrdSourceIngestError(
            "source_not_found",
            "PRD source not found",
        ) from exc
    except OSError as exc:
        code = (
            "source_outside_prd_directory"
            if exc.errno == errno.ELOOP
            else "source_unavailable"
        )
        raise PrdSourceIngestError(code, "cannot open verified PRD source") from exc

    try:
        opened = os.fstat(descriptor)
        after_open = os.stat(source_path, follow_symlinks=False)
        _refuse_non_regular_source(opened)
        _refuse_non_regular_source(after_open)
        if not os.path.samestat(opened, after_open):
            raise PrdSourceIngestError(
                "source_changed",
                "PRD source changed during verified open",
            )
        return descriptor, opened
    except PrdSourceIngestError:
        os.close(descriptor)
        raise
    except (OSError, ValueError) as exc:
        os.close(descriptor)
        raise PrdSourceIngestError(
            "source_unavailable",
            "cannot verify PRD source",
        ) from exc


def _open_contained_prd_source_posix(
    source_path: Path,
    *,
    containment_root: Path,
    required_parent: Path,
) -> tuple[int, os.stat_result]:
    """Open a managed source relative to pinned directory descriptors."""
    root_descriptor = -1
    parent_descriptor = -1
    descriptor = -1
    try:
        relative_parent = required_parent.relative_to(containment_root)
        if len(relative_parent.parts) > 1:
            raise ValueError("managed PRD source parent must be direct")
        resolved_root = containment_root.resolve(strict=True)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        for flag_name in ("O_CLOEXEC", "O_NOFOLLOW"):
            directory_flags |= getattr(os, flag_name, 0)
        root_descriptor = os.open(resolved_root, directory_flags)
        parent_descriptor = root_descriptor
        if relative_parent.parts:
            parent_descriptor = os.open(
                relative_parent.parts[0],
                directory_flags,
                dir_fd=root_descriptor,
            )

        before_open = os.stat(
            source_path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        _refuse_non_regular_source(before_open)
        descriptor = os.open(
            source_path.name,
            _prd_source_open_flags(),
            dir_fd=parent_descriptor,
        )
        opened = os.fstat(descriptor)
        after_open = os.stat(
            source_path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        _refuse_non_regular_source(opened)
        _refuse_non_regular_source(after_open)
        if not os.path.samestat(opened, after_open):
            raise PrdSourceIngestError(
                "source_changed",
                "PRD source changed during verified open",
            )
        result = descriptor, opened
        descriptor = -1
        return result
    except FileNotFoundError as exc:
        raise PrdSourceIngestError("source_not_found", "PRD source not found") from exc
    except PrdSourceIngestError:
        raise
    except OSError as exc:
        code = (
            "source_outside_prd_directory"
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}
            else "source_unavailable"
        )
        raise PrdSourceIngestError(code, "cannot open contained PRD source") from exc
    except ValueError as exc:
        raise PrdSourceIngestError(
            "source_unavailable",
            "cannot verify PRD source containment",
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent_descriptor >= 0 and parent_descriptor != root_descriptor:
            os.close(parent_descriptor)
        if root_descriptor >= 0:
            os.close(root_descriptor)


def _windows_final_path_for_descriptor(descriptor: int) -> Path:
    """Return the authoritative final path for an already-open Windows handle."""
    if os.name != "nt":
        raise OSError("Windows handle path resolution is unavailable")
    import ctypes
    import msvcrt
    from ctypes import wintypes

    get_final_path = ctypes.WinDLL("kernel32", use_last_error=True).GetFinalPathNameByHandleW
    get_final_path.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    get_final_path.restype = wintypes.DWORD
    handle = wintypes.HANDLE(msvcrt.get_osfhandle(descriptor))
    buffer_size = 512
    while True:
        buffer = ctypes.create_unicode_buffer(buffer_size)
        length = get_final_path(handle, buffer, buffer_size, 0)
        if length == 0:
            raise OSError(ctypes.get_last_error(), "cannot resolve open file handle")
        if length < buffer_size:
            final_path = buffer.value
            break
        buffer_size = length + 1
    if final_path.startswith("\\\\?\\UNC\\"):
        final_path = "\\\\" + final_path[8:]
    elif final_path.startswith("\\\\?\\"):
        final_path = final_path[4:]
    return Path(final_path)


def _lock_prd_source_descriptor(descriptor: int, *, byte_count: int) -> None:
    """Hold a best available non-blocking read-stability lock until close."""
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        class Overlapped(ctypes.Structure):
            _fields_ = [
                ("Internal", ctypes.c_size_t),
                ("InternalHigh", ctypes.c_size_t),
                ("Offset", wintypes.DWORD),
                ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE),
            ]

        lock_file = ctypes.WinDLL("kernel32", use_last_error=True).LockFileEx
        lock_file.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(Overlapped),
        ]
        lock_file.restype = wintypes.BOOL
        handle = wintypes.HANDLE(msvcrt.get_osfhandle(descriptor))
        overlapped = Overlapped()
        lock_fail_immediately = 0x00000001
        locked = lock_file(
            handle,
            lock_fail_immediately,
            0,
            byte_count & 0xFFFFFFFF,
            byte_count >> 32,
            ctypes.byref(overlapped),
        )
        if not locked:
            error_code = ctypes.get_last_error()
            raise PrdSourceIngestError(
                "source_busy",
                "PRD source cannot be locked for a stable read",
            ) from OSError(error_code, "cannot acquire shared source lock")
    elif os.name == "posix":
        import fcntl

        try:
            # Windows typeshed omits these POSIX-only attributes even though
            # this runtime branch is unreachable there.
            flock = getattr(fcntl, "flock")  # noqa: B009
            shared_nonblocking = getattr(fcntl, "LOCK_SH") | getattr(  # noqa: B009
                fcntl,
                "LOCK_NB",  # noqa: B009
            )
            flock(descriptor, shared_nonblocking)
        except OSError as exc:
            raise PrdSourceIngestError(
                "source_busy",
                "PRD source cannot be locked for a stable read",
            ) from exc


def ingest_prd_source(
    source_path: Path,
    *,
    max_bytes: int = MAX_PRD_SOURCE_BYTES_V1,
    containment_root: Path | None = None,
    required_parent: Path | None = None,
) -> IngestedPrdSource:
    """Read, bound, hash, and strict-decode one exact PRD source.

    ``Path.read_text`` is intentionally forbidden here because it applies
    universal-newline translation.  A single binary ``limit + 1`` probe keeps
    allocation bounded and proves an overrun before decoding or any caller
    opens a mutation-capable backend.
    """
    if (
        type(max_bytes) is not int
        or max_bytes < 1
        or max_bytes > MAX_PRD_SOURCE_BYTES_V1
    ):
        raise ValueError("PRD source byte limit must be within the Version 1 ceiling")

    descriptor, opened = _open_verified_prd_source(
        source_path,
        containment_root=containment_root,
        required_parent=required_parent,
    )
    try:
        _lock_prd_source_descriptor(descriptor, byte_count=max_bytes + 1)
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            source_bytes = stream.read(max_bytes + 1)
            after_read = os.fstat(stream.fileno())
    except OSError as exc:
        raise PrdSourceIngestError(
            "source_unavailable",
            "cannot read PRD source",
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    if (
        not os.path.samestat(opened, after_read)
        or opened.st_size != after_read.st_size
        or opened.st_mtime_ns != after_read.st_mtime_ns
        or opened.st_ctime_ns != after_read.st_ctime_ns
    ):
        raise PrdSourceIngestError(
            "source_changed",
            "PRD source changed during bounded read",
        )

    if len(source_bytes) > max_bytes:
        raise PrdSourceIngestError(
            "source_limit_exceeded",
            "PRD source exceeds the configured byte limit",
        )
    try:
        markdown = source_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise PrdSourceIngestError(
            "source_invalid_utf8",
            "PRD source is not valid UTF-8",
        ) from exc

    return IngestedPrdSource(
        source_bytes=source_bytes,
        markdown=markdown,
        source_sha256=hashlib.sha256(source_bytes).hexdigest(),
        source_size_bytes=len(source_bytes),
    )


def selected_prd_source_path(state_dir: Path, prd_id: str) -> Path:
    """Return the managed source selected by portable/legacy policy."""
    validated_id = validate_prd_id(prd_id)
    source_path = prd_source_path(state_dir, validated_id)
    required_parent = (
        state_dir
        if validated_id in _DEFAULT_PRD_IDS
        else state_dir / _PRDS_DIR_NAME
    )
    if validated_id not in _DEFAULT_PRD_IDS:
        legacy_path = required_parent / f"{validated_id}.md"
        if legacy_path != source_path:
            try:
                canonical_present = os.path.lexists(source_path)
                legacy_present = False
                if required_parent.exists():
                    collection_before = os.stat(
                        required_parent,
                        follow_symlinks=False,
                    )
                    if not stat.S_ISDIR(collection_before.st_mode):
                        raise PrdSourceIngestError(
                            "source_outside_prd_directory",
                            "PRD source collection is not a contained directory",
                        )
                    with os.scandir(required_parent) as entries:
                        legacy_present = any(
                            entry.name == legacy_path.name for entry in entries
                        )
                    collection_after = os.stat(
                        required_parent,
                        follow_symlinks=False,
                    )
                    if not os.path.samestat(collection_before, collection_after):
                        raise PrdSourceIngestError(
                            "source_changed",
                            "PRD source collection changed during inspection",
                        )
            except PrdSourceIngestError:
                raise
            except OSError as exc:
                raise PrdSourceIngestError(
                    "source_unavailable",
                    "cannot inspect PRD source collection",
                ) from exc
            if legacy_present:
                try:
                    canonical_present = canonical_present or os.path.lexists(
                        source_path
                    )
                except OSError as exc:
                    raise PrdSourceIngestError(
                        "source_unavailable",
                        "cannot recheck portable PRD source",
                    ) from exc
                if canonical_present:
                    raise PrdSourceIngestError(
                        "source_ambiguous",
                        "both portable and legacy PRD sources exist",
                    )
                raise PrdSourceIngestError(
                    "legacy_source_migration_required",
                    "legacy PRD source requires portable filename migration",
                )
    # Enforce the case-insensitive namespace on every host.  Linux commonly
    # has case-sensitive storage while macOS commonly does not; accepting an
    # alias only on the former would create state that cannot move safely to
    # the latter.  Scanning actual directory entries also avoids relying on
    # ``os.path.normcase``, which only reflects Windows behavior.
    try:
        collection_before = os.stat(required_parent, follow_symlinks=False)
    except FileNotFoundError:
        case_alias = False
    except OSError as exc:
        raise PrdSourceIngestError(
            "source_unavailable",
            "cannot inspect PRD source collection",
        ) from exc
    else:
        if not stat.S_ISDIR(collection_before.st_mode):
            raise PrdSourceIngestError(
                "source_outside_prd_directory",
                "PRD source collection is not a contained directory",
            )
        try:
            with os.scandir(required_parent) as entries:
                case_alias = any(
                    entry.name.casefold() == source_path.name.casefold()
                    and entry.name != source_path.name
                    for entry in entries
                )
            collection_after = os.stat(required_parent, follow_symlinks=False)
        except OSError as exc:
            raise PrdSourceIngestError(
                "source_unavailable",
                "cannot inspect PRD source collection",
            ) from exc
        if not os.path.samestat(collection_before, collection_after):
            raise PrdSourceIngestError(
                "source_changed",
                "PRD source collection changed during inspection",
            )
        if case_alias:
            raise PrdSourceIngestError(
                "source_case_alias",
                "PRD source spelling aliases another wire identity",
            )
    return source_path


def ingest_prd_source_for_id(
    state_dir: Path,
    prd_id: str,
    *,
    max_bytes: int = MAX_PRD_SOURCE_BYTES_V1,
) -> IngestedPrdSource:
    """Resolve and atomically ingest one contained default or named source."""
    validated_id = validate_prd_id(prd_id)
    source_path = selected_prd_source_path(state_dir, validated_id)
    required_parent = (
        state_dir
        if validated_id in _DEFAULT_PRD_IDS
        else state_dir / _PRDS_DIR_NAME
    )
    portable_path = prd_source_path(state_dir, validated_id)
    legacy_path = (
        None
        if validated_id in _DEFAULT_PRD_IDS
        else required_parent / f"{validated_id}.md"
    )
    ingested = ingest_prd_source(
        source_path,
        max_bytes=max_bytes,
        containment_root=state_dir,
        required_parent=required_parent,
    )
    if legacy_path is not None and legacy_path != portable_path:
        try:
            portable_present = os.path.lexists(portable_path)
            legacy_present = False
            if required_parent.exists():
                with os.scandir(required_parent) as entries:
                    legacy_present = any(
                        entry.name == legacy_path.name for entry in entries
                    )
        except OSError as exc:
            raise PrdSourceIngestError(
                "source_unavailable",
                "cannot recheck PRD source collection",
            ) from exc
        if portable_present and legacy_present:
            raise PrdSourceIngestError(
                "source_ambiguous",
                "both portable and legacy PRD sources exist",
            )
    return ingested


def replace_prd_source_for_id(
    state_dir: Path,
    prd_id: str,
    *,
    expected_sha256: str,
    markdown: str,
    max_bytes: int = MAX_PRD_SOURCE_BYTES_V1,
) -> IngestedPrdSource:
    """Atomically replace one verified managed source without following links.

    The caller supplies the digest from its last bounded read.  We verify that
    exact source again before staging and immediately before replacement to
    detect stale input observed before the atomic swap.  ``os.replace``
    replaces the directory entry itself; it never opens or writes through a
    link substituted at the destination.
    """
    if type(markdown) is not str:
        raise ValueError("replacement PRD source must be plain text")
    if type(expected_sha256) is not str or not re.fullmatch(
        r"[0-9a-f]{64}", expected_sha256
    ):
        raise ValueError("expected PRD source digest must be lowercase SHA-256")
    if (
        type(max_bytes) is not int
        or max_bytes < 1
        or max_bytes > MAX_PRD_SOURCE_BYTES_V1
    ):
        raise ValueError("PRD source byte limit must be within the Version 1 ceiling")

    try:
        source_bytes = markdown.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise PrdSourceIngestError(
            "source_invalid_utf8",
            "PRD source is not valid UTF-8",
        ) from exc
    if len(source_bytes) > max_bytes:
        raise PrdSourceIngestError(
            "source_limit_exceeded",
            "PRD source exceeds the configured byte limit",
        )

    validated_id = validate_prd_id(prd_id)
    current = ingest_prd_source_for_id(
        state_dir,
        validated_id,
        max_bytes=max_bytes,
    )
    if current.source_sha256 != expected_sha256:
        raise PrdSourceIngestError(
            "source_changed",
            "PRD source changed before verified replacement",
        )

    source_path = selected_prd_source_path(state_dir, validated_id)
    required_parent = (
        state_dir
        if validated_id in _DEFAULT_PRD_IDS
        else state_dir / _PRDS_DIR_NAME
    )
    temp_path: Path | None = None
    descriptor = -1
    try:
        descriptor, raw_temp_path = tempfile.mkstemp(
            prefix=f".{source_path.name}.",
            suffix=".tmp",
            dir=required_parent,
        )
        temp_path = Path(raw_temp_path)
        opened_temp = os.fstat(descriptor)
        _refuse_non_regular_source(opened_temp)
        if os.name == "nt":
            opened_temp_path = _windows_final_path_for_descriptor(descriptor)
            expected_parent = required_parent.resolve(strict=True)
            if os.path.normcase(os.path.normpath(str(opened_temp_path.parent))) != (
                os.path.normcase(os.path.normpath(str(expected_parent)))
            ):
                raise PrdSourceIngestError(
                    "source_outside_prd_directory",
                    "replacement source escapes its contained source directory",
                )

        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(source_bytes)
            stream.flush()
            os.fsync(stream.fileno())

        latest = ingest_prd_source_for_id(
            state_dir,
            validated_id,
            max_bytes=max_bytes,
        )
        if latest.source_sha256 != expected_sha256:
            raise PrdSourceIngestError(
                "source_changed",
                "PRD source changed before verified replacement",
            )

        try:
            source_mode = stat.S_IMODE(os.stat(source_path, follow_symlinks=False).st_mode)
            os.chmod(temp_path, source_mode)
            os.replace(temp_path, source_path)
        except OSError as exc:
            raise PrdSourceIngestError(
                "source_unavailable",
                "cannot replace verified PRD source",
            ) from exc
        temp_path = None
    except PrdSourceIngestError:
        raise
    except OSError as exc:
        raise PrdSourceIngestError(
            "source_unavailable",
            "cannot stage verified PRD source replacement",
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    return IngestedPrdSource(
        source_bytes=source_bytes,
        markdown=markdown,
        source_sha256=hashlib.sha256(source_bytes).hexdigest(),
        source_size_bytes=len(source_bytes),
    )


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
