"""Config loader for anvil.

Reads a config.yaml file and returns a frozen Config dataclass.
Also provides write_default_config() and config_template() for the
`anvil init` command to scaffold a starter config.

All fields are minimal for Phase 2; extend in later phases without
breaking existing callers (add keyword-only args with defaults only).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal

import yaml

logger = logging.getLogger(__name__)

# v1.21.0 — complexity score at/above which a task is queued for sub-task
# expansion. Single source of truth: the Config dataclass default, the
# scoring engine's expansion-queue builder, and every CLI/MCP call site that
# runs without a config.yaml all read this constant. Mirrors the historical
# hardcoded ``complexity >= 4`` gate in ``planning.inference.expand_task``.
DEFAULT_AUTO_EXPAND_THRESHOLD: Final[int] = 4

# T020 — fast-lane (right-size-by-score) ceilings. A task whose complexity and
# blast_radius scores are BOTH at/below these values routes to a minimal,
# single-step work packet with a trimmed required-evidence set. These defaults
# mirror the conservative built-in ceilings the packet renderer ships with
# (``context.packets.LIGHTWEIGHT_COMPLEXITY_MAX`` /
# ``LIGHTWEIGHT_BLAST_RADIUS_MAX``); config.yaml can raise or lower them per
# project. Keeping the constants here lets the CLI/MCP call sites read a single
# source of truth without importing the (heavier) packets module just to learn
# the default.
DEFAULT_FAST_LANE_COMPLEXITY_MAX: Final[int] = 2
DEFAULT_FAST_LANE_BLAST_RADIUS_MAX: Final[int] = 2

# retro-opps T001 — review-tier thresholds. The derived light/standard/max
# review tier (``planning.scoring.review_tier``) is a pure projection over the
# six-dim score plus the B45 confirmation flags; these two knobs set where the
# tier boundaries sit. ``review_tier_max_min`` is the score at/above which
# review_risk or blast_radius forces the max tier; ``review_tier_light_risk_max``
# is the highest confirmed review_risk that still earns the light tier (which
# additionally requires the fast-lane gate and BOTH confirmation flags).
DEFAULT_REVIEW_TIER_MAX_MIN: Final[int] = 4
DEFAULT_REVIEW_TIER_LIGHT_RISK_MAX: Final[int] = 2


@dataclass(frozen=True)
class Config:
    """Parsed representation of config.yaml.

    Frozen so that callers cannot accidentally mutate it.  All fields have
    sensible defaults so that minimal configs work without specifying every key.
    """

    project_name: str
    project_id: str

    # ---------------------------------------------------------------------
    # LLM provider selection.
    #
    # Precedence applied by ``planning.llm_planner.resolve_planner_provider``:
    #
    # 1. ``llm_provider`` explicit in this config → wins. One of
    #    ``agent-sdk`` / ``anthropic`` / ``bedrock`` / ``custom``.
    # 2. Otherwise the default is ``agent-sdk`` — the Claude Agent SDK over
    #    the logged-in subscription (no API key). anvil is capacity-bound,
    #    not per-token-cost bound, so the subscription path is the default.
    # 3. ``llm_fallback: true`` (default false) restores the legacy env
    #    auto-detect chain *before* falling through to ``agent-sdk``:
    #    - ANTHROPIC_API_KEY  → "anthropic"  (direct API)
    #    - AWS_REGION (or AWS_DEFAULT_REGION) with no ANTHROPIC_API_KEY
    #      AND ``anthropic[bedrock]`` installed → "bedrock"
    #    - CUSTOM_LLM_BASE_URL → "custom"
    #    - else → "agent-sdk"
    #
    # We do NOT silent-fail across providers once one is chosen — community
    # consensus (research/2026) is that silent fallback breaks cost
    # predictability and surprises ops teams during incidents. Pick one per
    # process; re-launch to switch.
    # ---------------------------------------------------------------------
    llm_provider: Literal["agent-sdk", "anthropic", "bedrock", "custom"] | None = None

    # Opt back into env-based provider auto-detection (the pre-agent-sdk
    # behavior). Default ``False``: with no explicit ``llm_provider``, anvil
    # uses ``agent-sdk`` and does NOT consult ANTHROPIC_API_KEY / AWS_REGION /
    # CUSTOM_LLM_BASE_URL. Set ``True`` to let those env vars pick the provider
    # again (with ``agent-sdk`` as the final fallback).
    llm_fallback: bool = False

    # Explicit model id (overrides ``llm_tier``). Pass when you need a
    # specific Anthropic-API id (``claude-opus-4-7-20260124``), a Bedrock
    # inference-profile id (``us.anthropic.claude-opus-4-7``), or a
    # custom-endpoint route name (``anthropic/claude-sonnet-4-6`` on
    # OpenRouter). Leave blank to use ``llm_tier``.
    llm_model: str | None = None

    # Logical tier (``opus``/``sonnet``/``haiku``) used when ``llm_model``
    # is blank. Defaults to None → providers fall back to their own
    # ``DEFAULT_TIER`` (``sonnet``). Set this in config so a project's
    # default tier is stable across provider switches.
    llm_tier: Literal["opus", "sonnet", "haiku"] | None = None

    # Bedrock-specific knobs. Only consulted when ``llm_provider`` resolves
    # to ``bedrock``. ``aws_region`` falls through to AWS_REGION /
    # AWS_DEFAULT_REGION env vars and finally to a clear SDK error — we
    # do NOT pick a silent default like ``us-east-1`` because that would
    # hide latency / billing surprises.
    bedrock_region: str | None = None
    bedrock_profile: str | None = None

    # Custom-endpoint knobs. Only consulted when ``llm_provider`` resolves
    # to ``custom``. ``base_url`` is REQUIRED for the custom path (either
    # here or via CUSTOM_LLM_BASE_URL env). ``api_key_env`` names the env
    # var to read the bearer token from — defaults to ``CUSTOM_LLM_API_KEY``,
    # which the resolver also tries before falling back to ``OPENAI_API_KEY``.
    custom_base_url: str | None = None
    custom_api_key_env: str | None = None

    # ---------------------------------------------------------------------------
    # S3 durable storage (optional). Only consulted when durable_store == "s3".
    #
    # Run `anvil backup` to push events.jsonl; `anvil restore` to pull + replay.
    # Requires boto3: pip install 'anvil-state[s3]'
    #
    # ponytail: Literal["none","s3"] only — widen to "gcs"|"azure" when
    # DurableStore impls exist for those providers.
    # ---------------------------------------------------------------------------
    durable_store: Literal["none", "s3"] = "none"
    s3_bucket: str | None = None
    s3_prefix: str = ""           # key prefix within the bucket, e.g. "anvil/my-project"
    s3_region: str | None = None  # falls back to AWS_REGION env; mirrors bedrock_region
    s3_profile: str | None = None # named AWS profile; mirrors bedrock_profile

    # Lease / heartbeat durations in MINUTES. Stored as float so sub-minute
    # values (e.g. ``default_lease_minutes: 0.5`` → 30 s) round-trip without
    # being truncated to whole minutes. ClaimManager computes the lease via
    # ``timedelta(minutes=float)``, so a fractional value yields a fractional
    # lease. Whole-number configs still load as ``60.0`` / ``5.0`` and behave
    # identically to the pre-float ints.
    # 240 (was 60): real sessions showed >15-min workflows silently losing
    # their lease mid-task under the old default (post-session findings);
    # the author's standing workaround was `--lease 240` — now the default.
    default_lease_minutes: float = 240.0
    default_heartbeat_minutes: float = 5.0
    # B46 — hard max-claim-age as a multiple of the base lease. After
    # ``default_lease_minutes * max_claim_age_multiplier`` since a claim was
    # created, ``renew()`` refuses (even if heartbeating), so a wedged agent
    # cannot hold a lease forever — the claim's lease then expires and the
    # stale-claim reaper takes it. Default 4x; raise it for legitimately
    # long-running tasks.
    max_claim_age_multiplier: float = 4.0
    # B49 — accept-rate governor + review-debt cap. The pull seam (`anvil next`)
    # refuses new work when the human review queue is saturated
    # (needs_review depth >= needs_review_cap) or the requesting runner's recent
    # accept-rate (over the trailing accept_rate_window_days) is below
    # accept_rate_floor. Guards against "fast dumb work" swamping human review.
    accept_rate_floor: float = 0.80
    needs_review_cap: int = 10
    accept_rate_window_days: float = 7.0

    git_ops_mode: Literal["auto", "record_only", "off"] = "auto"

    # SL1-RR-1 — write-path durability mode.
    #
    # Selects how aggressively the event log is persisted to disk. The write
    # path (see state/sqlite.py append()) reads this to decide whether to
    # fsync the log before COMMIT.
    #
    #   relaxed (DEFAULT) — laptop: synchronous=NORMAL, buffered log, no
    #                       per-event fsync. Correctness does not depend on
    #                       fsync (ordering + log-authority counter + forward
    #                       catch-up guarantee replay determinism); worst case
    #                       on hard power-loss is the last few un-synced events
    #                       drop from log and projection together and the user
    #                       repeats the last action.
    #   strict            — CI/shared/server: synchronous=FULL + fsync(log)
    #                       before COMMIT. Opt-in; the only mode that fsyncs
    #                       per event.
    #
    # Defaults to "relaxed" so a config written before this key existed keeps
    # its prior (un-synced) behaviour without surprise.
    durability: Literal["relaxed", "strict"] = "relaxed"

    # v1.15.0 — host-project branch-naming convention.
    #
    # The CLI's `claim` command creates a git branch per task. By default
    # the branch is `agent/<task_id_lower>-<slug>` — the `agent/` prefix
    # advertises that an agent (not a human) worked the task. But many
    # host projects encode their CI / PR-template / CODEOWNERS automation
    # around a `feature/` or `fix/` prefix, and the `agent/` default
    # silently bypasses those rules.
    #
    # Set this in `.anvil/config.yaml` to match the host project:
    #
    #     branch_prefix: feature   # → feature/<task>-<slug>
    #     branch_prefix: fix       # → fix/<task>-<slug>
    #     branch_prefix: ""        # → <task>-<slug>  (no prefix)
    #     branch_prefix: agent     # default; preserves pre-v1.15.0 behaviour
    #
    # Nested prefixes (e.g. `feature/agent`) are allowed verbatim — git
    # accepts slashes inside branch names. Validation: any string with no
    # whitespace and no leading/trailing slash. An empty string is
    # explicit opt-out and produces an unprefixed `<task>-<slug>` branch.
    branch_prefix: str = "agent"

    # T007 — cross-PRD claim guard. When a caller scopes `anvil claim` with
    # --prd / $ANVIL_PRD but names a task owned by another PRD, warn by default
    # so "execute this PRD" does not silently drift into another partition.
    # Projects that want a hard boundary can set crossPrdGuard: refuse; callers
    # may still override an intentional cross-PRD claim with `--force`.
    cross_prd_guard: Literal["warn", "refuse"] = "warn"

    # v1.21.0 — complexity score → auto-expansion loop.
    #
    # After scoring, every task whose ``complexity`` is at/above
    # ``auto_expand_threshold`` is surfaced in an EXPANSION QUEUE section
    # (CLI ``score``) and in the ``expansion_queue`` field of the MCP
    # ``score_tasks`` response, each with the exact follow-up command
    # (``anvil expand TXXX --use-llm``). The same threshold replaces
    # the previously hardcoded ``complexity >= 4`` gate in
    # ``anvil expand``.
    #
    #   auto_expand: true            # default; emit the queue after scoring
    #   auto_expand: false           # opt out: scores are reported, no queue
    #   auto_expand_threshold: 4     # default; valid range 1-5 (score scale)
    #
    # Queueing is deterministic — the LLM-side expansion itself still only
    # happens when ``expand --use-llm`` (or the planner agent) runs.
    auto_expand: bool = True
    auto_expand_threshold: int = DEFAULT_AUTO_EXPAND_THRESHOLD

    # v1.22.0 — git-backed events (Phase A of the 2026-06-10 spec).
    #
    # Selects the event-log id/replay strategy:
    #
    #   local (DEFAULT) — sequence-numbered event ids (E000001, …), strict
    #                     sequential replay, events.jsonl stays machine-scoped
    #                     (gitignored). Pre-1.22.0 behaviour, byte-for-byte.
    #   git             — hash-chained event ids ("E-" + sha256(parent ‖
    #                     canonical_json(payload) ‖ actor ‖ ts)[:12]) with a
    #                     Lamport counter in the envelope; replay is
    #                     order-tolerant (dedupe by id, order by
    #                     (lamport, ts, id)) so events.jsonl can be committed
    #                     and merged across branches via `merge=union`.
    #
    # Do not flip this by hand on an existing project — `anvil
    # migrate-events --to git` rewrites the log preserving order, emits the
    # old→new id mapping, and writes .anvil/.gitattributes in one step.
    events_storage: Literal["local", "git"] = "local"

    sync_github_enabled: bool = False
    sync_github_conflict_strategy: Literal[
        "local_wins", "remote_wins", "prompt", "manual_merge"
    ] = "prompt"

    # T025/B25 — completion-evidence ENFORCEMENT mode.
    #
    # The evidence gate (``review.gates.evidence_complete``) checks submitted
    # Evidence against the task's ``Verification.required_evidence`` list. By
    # default the gate is ADVISORY: the CLI shows the verdict but
    # ``apply --approve`` transitions the task to done regardless of missing
    # items. The single most-felt pain in the benchmark + competitor analysis
    # is "agents lie about done", so this knob makes the gate ENFORCEABLE.
    #
    #   strict_evidence: false  # DEFAULT — advisory. Pre-T025 behaviour,
    #                           # byte-for-byte: gate is shown, apply still
    #                           # approves even with missing evidence.
    #   strict_evidence: true   # apply --approve REFUSES (exit 1 / JSON
    #                           # error envelope code "evidence_incomplete")
    #                           # when required evidence is missing. --reject
    #                           # is unaffected; a complete gate (or a task
    #                           # with no required_evidence) is a no-op.
    #
    # Precedence at the ``apply``/``submit`` call site:
    #   explicit --strict/--no-strict flag  >  this config field  >  default(False)
    #
    # Defaults to False so a config written before this key existed keeps its
    # prior advisory behaviour without surprise.
    strict_evidence: bool = False

    # T020 — fast-lane (right-size process by score) thresholds.
    #
    # A task whose complexity AND blast_radius scores are both at/below these
    # ceilings is "trivial": ``context.packets`` renders it a minimal,
    # single-step work packet with a trimmed required-evidence set (fewer
    # fields for the agent to satisfy) instead of the full update-protocol
    # prose. The task still records an immutable completion-evidence transition
    # exactly like any other — only the *packet shape* is right-sized, never the
    # evidence ledger. A task above either ceiling (e.g. a 1/5-complexity change
    # that touches a 5/5-blast schema/config surface) always gets the full
    # packet — the safe default.
    #
    #   fast_lane_complexity_max: 2     # DEFAULT; valid range 1-5
    #   fast_lane_blast_radius_max: 2   # DEFAULT; valid range 1-5
    #
    # Defaults mirror the renderer's built-in conservative ceilings, so a config
    # written before these keys existed keeps the exact prior routing.
    fast_lane_complexity_max: int = DEFAULT_FAST_LANE_COMPLEXITY_MAX
    fast_lane_blast_radius_max: int = DEFAULT_FAST_LANE_BLAST_RADIUS_MAX

    # retro-opps T001 — review-tier thresholds consumed by
    # ``planning.scoring.review_tier``. Same 1-5 score-scale contract and
    # validation as the fast-lane ceilings above; absent keys keep the
    # conservative defaults (max at >=4 risk/blast, light only at <=2
    # confirmed review_risk).
    #
    #   review_tier_max_min: 4          # DEFAULT; valid range 1-5
    #   review_tier_light_risk_max: 2   # DEFAULT; valid range 1-5
    review_tier_max_min: int = DEFAULT_REVIEW_TIER_MAX_MIN
    review_tier_light_risk_max: int = DEFAULT_REVIEW_TIER_LIGHT_RISK_MAX

    # Phase 9 T5 — multi-provider sync.
    #
    # ``sync_providers`` is the contents of the optional top-level
    # ``sync.providers`` YAML key. When ``None`` (key absent), every caller
    # that asks "which providers are configured?" SHOULD fall back to
    # ``sorted(anvil.sync.registry.PROVIDER_REGISTRY)`` — i.e. the
    # full set of registered providers, matching v1.8.0 behaviour. When the
    # operator pins an explicit list, ``ReconciliationEngine`` and the
    # generic ``sync provider`` dispatch scope to that allow-list — useful
    # for projects that have multiple providers registered (github_issues,
    # linear, monday, …) but only want some of them to count toward
    # ``missing_sync_mapping`` discrepancies.
    #
    # An empty list (``sync.providers: []``) is preserved as-is — that
    # explicitly opts out of every provider (e.g. for a frozen project
    # that should no longer surface sync drift). Callers MUST disambiguate
    # ``None`` (use the registry) from ``[]`` (use nothing).
    sync_providers: tuple[str, ...] | None = None

    # Paths (resolved at load time to absolute strings).
    db_path: str = field(default="")
    events_path: str = field(default="")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_config(path: str | Path) -> Config:
    """Parse config.yaml at *path* and return a Config instance.

    Single-file load: does NOT merge the global-config layer. Use
    :func:`load_merged_config` when you want global defaults
    (``~/.config/anvil/config.yaml``) merged under the project config.

    Raises
    ------
    FileNotFoundError
        If the file does not exist.
    ValueError
        If required fields (project_name, project_id) are absent or blank,
        or if an enum-typed field has an invalid value.
    yaml.YAMLError
        If the file is not valid YAML.
    """
    resolved = Path(path).expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(
            f"Config file not found: {resolved}. "
            "Run `anvil init` to create one."
        )

    data = _normalize_config_aliases(_read_yaml_mapping(resolved))
    _validate_required(data, resolved)
    return _build_config(data, resolved)


# T016/B17 — global-config layer.
#
# Resolution of the global config path honours XDG_CONFIG_HOME (the standard
# Linux/macOS config root) and falls back to ``~/.config``. A dedicated
# ANVIL_GLOBAL_CONFIG override exists so tests (and power users with a
# non-standard layout) can point at an explicit file without disturbing the
# whole XDG_CONFIG_HOME tree.
_GLOBAL_CONFIG_ENV: Final[str] = "ANVIL_GLOBAL_CONFIG"
_XDG_CONFIG_HOME_ENV: Final[str] = "XDG_CONFIG_HOME"
_GLOBAL_CONFIG_SUBPATH: Final[str] = "anvil/config.yaml"


def _home_dir() -> Path:
    """Home directory for global Anvil config, honoring isolated HOME on Windows."""
    import os

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


def _expand_home_path(path: str | Path) -> Path:
    """Expand a leading ``~`` using Anvil's HOME-aware home resolver."""
    import os

    raw = os.fspath(path)
    if raw == "~":
        return _home_dir()
    if raw.startswith("~/") or raw.startswith("~\\"):
        return _home_dir() / raw[2:]
    return Path(path).expanduser()


def global_config_path() -> Path:
    """Return the resolved path to the user's global config.yaml.

    Precedence:

    1. ``ANVIL_GLOBAL_CONFIG`` env var — an explicit file path. Used
       verbatim (after ``~`` expansion); takes priority so tests and unusual
       installs can pin a location.
    2. ``$XDG_CONFIG_HOME/anvil/config.yaml`` when ``XDG_CONFIG_HOME``
       is set (and non-empty).
    3. ``~/.config/anvil/config.yaml`` — the documented default.

    The file is NOT required to exist; callers (:func:`load_merged_config`)
    treat a missing global config as "no global defaults".
    """
    import os

    override = os.environ.get(_GLOBAL_CONFIG_ENV)
    if override is not None and override.strip() != "":
        return _expand_home_path(override).resolve()

    xdg = os.environ.get(_XDG_CONFIG_HOME_ENV)
    if xdg is not None and xdg.strip() != "":
        return (_expand_home_path(xdg) / _GLOBAL_CONFIG_SUBPATH).resolve()

    return (_home_dir() / ".config" / _GLOBAL_CONFIG_SUBPATH).resolve()


def load_merged_config(
    path: str | Path,
    *,
    global_path: str | Path | None = None,
) -> Config:
    """Load the project config with the global-config layer merged underneath.

    Precedence (lowest → highest): built-in dataclass default < global config
    (``~/.config/anvil/config.yaml``) < project config (*path*). A key
    present in the project config overrides the same key in the global config;
    a key present only in the global config supplies the value; a key present
    in neither falls through to the dataclass default.

    Required identity fields (``project_name`` / ``project_id``) are resolved
    against the MERGED mapping, so a global config MAY supply a default
    ``project_name`` that an individual project overrides — but the global
    config is never *required* to carry them, and a project that omits them
    still raises the same ``ValueError`` as :func:`load_config` when the global
    layer does not supply them either.

    Paths (``db_path`` / ``events_path``) always resolve relative to the
    PROJECT config's directory, never the global one — per-project state must
    live next to the project, not in ``~/.config``.

    Parameters
    ----------
    path:
        The project ``config.yaml`` (must exist).
    global_path:
        Explicit global-config path. ``None`` → :func:`global_config_path`.
        A non-existent global path is treated as "no global defaults"
        (the common case: most users never write one).

    Raises
    ------
    FileNotFoundError
        If the PROJECT config does not exist. A missing GLOBAL config is fine.
    ValueError
        If the merged mapping is missing required fields, or any field has an
        invalid value (same validation as :func:`load_config`).
    """
    resolved = Path(path).expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(
            f"Config file not found: {resolved}. "
            "Run `anvil init` to create one."
        )

    gpath = (
        _expand_home_path(global_path).resolve()
        if global_path is not None
        else global_config_path()
    )

    global_data: dict[str, object] = {}
    if gpath.exists():
        # The global layer is optional defaults only — it is NOT required to
        # carry project_name/project_id, and a broken global config raises the
        # same loud ValueError/YAMLError as a broken project config (the user
        # explicitly wrote it, so a silent skip would hide their typo).
        global_data = _normalize_config_aliases(_read_yaml_mapping(gpath))

    project_data = _normalize_config_aliases(_read_yaml_mapping(resolved))

    # Shallow merge: project keys win over global keys. config.yaml is a flat
    # mapping of scalars plus the single nested ``sync`` block; a project that
    # sets ``sync`` replaces the global ``sync`` wholesale (we do not deep-merge
    # provider lists — an explicit project ``sync.providers`` is an explicit
    # override of the global one, matching the documented project>global rule).
    merged: dict[str, object] = {**global_data, **project_data}

    _validate_required(merged, resolved)
    return _build_config(merged, resolved)


def _read_yaml_mapping(resolved: Path) -> dict[str, object]:
    """Parse *resolved* as YAML and return it, requiring a top-level mapping."""
    with resolved.open(encoding="utf-8") as fh:
        raw: object = yaml.safe_load(fh)

    # An empty file (``yaml.safe_load`` → None) is treated as an empty mapping
    # so an empty global config means "no global defaults" rather than an error.
    if raw is None:
        return {}

    if not isinstance(raw, dict):
        raise ValueError(
            f"Config file {resolved} must be a YAML mapping, got {type(raw).__name__!r}."
        )
    return raw


def _normalize_config_aliases(data: dict[str, object]) -> dict[str, object]:
    """Normalize supported compatibility aliases inside one config layer.

    ``load_merged_config`` merges raw dictionaries by key, so aliases must be
    normalized before the global<project merge. Otherwise a global primary key
    can accidentally beat a project alias key even though project config is the
    higher-precedence layer.
    """
    normalized = dict(data)
    if "cross_prd_guard" in normalized and "crossPrdGuard" not in normalized:
        normalized["crossPrdGuard"] = normalized["cross_prd_guard"]
    normalized.pop("cross_prd_guard", None)
    return normalized


def _build_config(data: dict[str, object], resolved: Path) -> Config:
    """Build a validated :class:`Config` from an already-merged mapping.

    *data* is the (possibly global-merged) raw mapping; *resolved* is the
    PROJECT config path, used both for error messages and to resolve relative
    ``db_path`` / ``events_path`` values against the project directory.
    """
    # Resolve paths relative to the project config file's directory.
    config_dir = resolved.parent
    db_path = _resolve_path(data.get("db_path", "state.db"), config_dir)
    events_path = _resolve_path(data.get("events_path", "events.jsonl"), config_dir)

    git_ops_mode = _validate_literal(
        data.get("git_ops_mode", "auto"),
        ("auto", "record_only", "off"),
        "git_ops_mode",
    )

    # SL1-RR-1 — durability mode. Absent key → "relaxed" (back-compat with
    # configs written before this knob existed). An invalid value raises the
    # same ValueError that every other literal-typed field raises.
    durability = _validate_literal(
        data.get("durability", "relaxed"),
        ("relaxed", "strict"),
        "durability",
    )

    # v1.15.0 — branch_prefix. Validate format: no whitespace, no leading
    # or trailing slash. An empty string is acceptable (explicit no-prefix
    # mode). Internal slashes are allowed (nested prefixes like
    # `feature/agent`). Invalid values raise a config-load error so the
    # user sees the problem at init time, not when claim runs.
    branch_prefix_raw = data.get("branch_prefix", "agent")
    if not isinstance(branch_prefix_raw, str):
        raise ValueError(
            f"branch_prefix must be a string, got {type(branch_prefix_raw).__name__} "
            f"({resolved})"
        )
    branch_prefix = branch_prefix_raw
    if branch_prefix and (
        branch_prefix.startswith("/")
        or branch_prefix.endswith("/")
        or any(c.isspace() for c in branch_prefix)
    ):
        raise ValueError(
            f"branch_prefix {branch_prefix!r} has invalid shape: "
            "leading/trailing slashes and whitespace are not allowed "
            f"({resolved}). Use e.g. 'feature' or 'fix' or 'feature/agent'."
        )

    cross_prd_guard = _validate_literal(
        data.get("crossPrdGuard", "warn"),
        ("warn", "refuse"),
        "crossPrdGuard",
    )

    # v1.21.0 — auto-expansion knobs. ``auto_expand`` follows the same loose
    # bool coercion as ``sync_github_enabled``; the threshold is validated
    # strictly (int, 1-5) so a typo'd ``auto_expand_threshold: 9`` surfaces
    # at load time rather than silently queueing nothing (no score is >5) or
    # everything (every score is >=1).
    auto_expand_raw = data.get("auto_expand", True)
    if not isinstance(auto_expand_raw, bool):
        raise ValueError(
            f"auto_expand must be a boolean, got "
            f"{type(auto_expand_raw).__name__} ({resolved})."
        )
    auto_expand = auto_expand_raw

    # ``llm_fallback`` uses the same strict bool validation as ``auto_expand``
    # (NOT the loose ``bool(data.get(...))`` coercion) so a quoted ``"false"``
    # or a typo surfaces at load time instead of silently flipping env
    # auto-detection on.
    llm_fallback_raw = data.get("llm_fallback", False)
    if llm_fallback_raw is None:
        # A blank YAML value (the default template ships `llm_fallback:` with
        # no value) reads as None — treat it as the default False, like an
        # absent key. A non-bool *value* (e.g. the string "false") still fails.
        llm_fallback_raw = False
    if not isinstance(llm_fallback_raw, bool):
        raise ValueError(
            f"llm_fallback must be a boolean, got "
            f"{type(llm_fallback_raw).__name__} ({resolved})."
        )
    llm_fallback = llm_fallback_raw

    auto_expand_threshold = _validate_auto_expand_threshold(
        data.get("auto_expand_threshold", DEFAULT_AUTO_EXPAND_THRESHOLD),
        resolved,
    )

    # T020 — fast-lane score ceilings. Absent keys → the renderer's built-in
    # defaults, so a pre-T020 config keeps its exact prior packet routing.
    fast_lane_complexity_max = _validate_score_ceiling(
        data.get("fast_lane_complexity_max", DEFAULT_FAST_LANE_COMPLEXITY_MAX),
        "fast_lane_complexity_max",
        resolved,
    )
    fast_lane_blast_radius_max = _validate_score_ceiling(
        data.get(
            "fast_lane_blast_radius_max", DEFAULT_FAST_LANE_BLAST_RADIUS_MAX
        ),
        "fast_lane_blast_radius_max",
        resolved,
    )

    # retro-opps T001 — review-tier thresholds. Same 1-5 validation contract
    # as the fast-lane ceilings; absent keys keep the conservative defaults.
    review_tier_max_min = _validate_score_ceiling(
        data.get("review_tier_max_min", DEFAULT_REVIEW_TIER_MAX_MIN),
        "review_tier_max_min",
        resolved,
    )
    review_tier_light_risk_max = _validate_score_ceiling(
        data.get(
            "review_tier_light_risk_max", DEFAULT_REVIEW_TIER_LIGHT_RISK_MAX
        ),
        "review_tier_light_risk_max",
        resolved,
    )

    # v1.22.0 — events storage mode. Absent key → "local" (every pre-existing
    # project keeps sequence ids and strict replay). An invalid value raises
    # at load time like every other literal-typed field: a typo'd mode that
    # silently fell back to "local" would append E{N} ids into a hash-chained
    # log, which order-tolerant replay would then sort incorrectly.
    events_storage = _validate_literal(
        data.get("events_storage", "local"),
        ("local", "git"),
        "events_storage",
    )

    sync_conflict_strategy = _validate_literal(
        data.get("sync_github_conflict_strategy", "prompt"),
        ("local_wins", "remote_wins", "prompt", "manual_merge"),
        "sync_github_conflict_strategy",
    )

    sync_providers = _parse_sync_providers(data.get("sync"), resolved)

    # v1.17.0 — LLM provider / tier validation. Enum-typed fields rejected
    # at load time so misconfigs surface during `init`, not during plan.
    llm_provider_raw = _str_or_none(data.get("llm_provider"))
    if llm_provider_raw is not None:
        llm_provider_value: (
            Literal["agent-sdk", "anthropic", "bedrock", "custom"] | None
        ) = _validate_literal(  # type: ignore[assignment]
            llm_provider_raw,
            ("agent-sdk", "anthropic", "bedrock", "custom"),
            "llm_provider",
        )
    else:
        llm_provider_value = None

    llm_tier_raw = _str_or_none(data.get("llm_tier"))
    if llm_tier_raw is not None:
        llm_tier_value: Literal["opus", "sonnet", "haiku"] | None = (
            _validate_literal(  # type: ignore[assignment]
                llm_tier_raw,
                ("opus", "sonnet", "haiku"),
                "llm_tier",
            )
        )
    else:
        llm_tier_value = None

    # S3 durable storage knobs. Mirror bedrock_region/bedrock_profile pattern.
    durable_store = _validate_literal(
        data.get("durable_store", "none"),
        ("none", "s3"),
        "durable_store",
    )
    s3_bucket = _str_or_none(data.get("s3_bucket"))
    if durable_store == "s3" and not s3_bucket:
        raise ValueError(
            f"Config file {resolved}: durable_store is 's3' but s3_bucket is "
            "absent or blank. Set s3_bucket to the target bucket name."
        )

    return Config(
        project_name=str(data["project_name"]),
        project_id=str(data["project_id"]),
        llm_provider=llm_provider_value,
        llm_fallback=llm_fallback,
        llm_model=_str_or_none(data.get("llm_model")),
        llm_tier=llm_tier_value,
        bedrock_region=_str_or_none(data.get("bedrock_region")),
        bedrock_profile=_str_or_none(data.get("bedrock_profile")),
        custom_base_url=_str_or_none(data.get("custom_base_url")),
        custom_api_key_env=_str_or_none(data.get("custom_api_key_env")),
        # float(...) — not int(...) — so a fractional ``default_lease_minutes``
        # like 0.5 (= 30 s) is preserved instead of rejected/truncated. A
        # malformed value still raises ValueError at load time.
        default_lease_minutes=float(str(data.get("default_lease_minutes", 240))),
        default_heartbeat_minutes=float(
            str(data.get("default_heartbeat_minutes", 5))
        ),
        max_claim_age_multiplier=float(
            str(data.get("max_claim_age_multiplier", 4))
        ),
        accept_rate_floor=float(str(data.get("accept_rate_floor", 0.80))),
        needs_review_cap=int(str(data.get("needs_review_cap", 10))),
        accept_rate_window_days=float(
            str(data.get("accept_rate_window_days", 7))
        ),
        git_ops_mode=git_ops_mode,  # type: ignore[arg-type]
        durability=durability,  # type: ignore[arg-type]
        branch_prefix=branch_prefix,
        cross_prd_guard=cross_prd_guard,  # type: ignore[arg-type]
        auto_expand=auto_expand,
        auto_expand_threshold=auto_expand_threshold,
        events_storage=events_storage,  # type: ignore[arg-type]
        sync_github_enabled=bool(data.get("sync_github_enabled", False)),
        sync_github_conflict_strategy=sync_conflict_strategy,  # type: ignore[arg-type]
        sync_providers=sync_providers,
        # T025/B25 — same loose bool coercion as sync_github_enabled. Absent
        # key → False (advisory), preserving pre-T025 behaviour for every
        # config written before this knob existed.
        strict_evidence=bool(data.get("strict_evidence", False)),
        # T020 — fast-lane ceilings. Absent keys fall back to the renderer's
        # built-in defaults (see _build_config above), preserving prior routing.
        fast_lane_complexity_max=fast_lane_complexity_max,
        fast_lane_blast_radius_max=fast_lane_blast_radius_max,
        review_tier_max_min=review_tier_max_min,
        review_tier_light_risk_max=review_tier_light_risk_max,
        db_path=db_path,
        events_path=events_path,
        # S3 durable storage — mirrors bedrock_region/bedrock_profile pattern.
        durable_store=durable_store,  # type: ignore[arg-type]
        s3_bucket=s3_bucket,
        s3_prefix=str(data.get("s3_prefix", "") or ""),
        s3_region=_str_or_none(data.get("s3_region")),
        s3_profile=_str_or_none(data.get("s3_profile")),
    )


def write_default_config(path: str | Path, *, project_name: str) -> None:
    """Write a starter config.yaml to *path*.

    Generates a fresh project_id (UUIDv4).  Does NOT overwrite an existing
    file — callers must check first.

    Raises
    ------
    FileExistsError
        If *path* already exists.
    """
    resolved = Path(path).expanduser().resolve()
    if resolved.exists():
        raise FileExistsError(
            f"Config file already exists: {resolved}. "
            "Delete it manually if you want to re-initialise."
        )
    resolved.parent.mkdir(parents=True, exist_ok=True)
    project_id = str(uuid.uuid4())
    content = _render_template(project_name=project_name, project_id=project_id)
    resolved.write_text(content, encoding="utf-8")


def config_template(*, project_name: str = "my-project") -> str:
    """Return the default config YAML as a string.

    Useful for the `anvil init` command to display what will be written,
    or for tests that want the canonical default shape without touching the disk.
    """
    return _render_template(
        project_name=project_name,
        project_id=str(uuid.uuid4()),
    )


def read_events_storage(path: str | Path) -> Literal["local", "git"]:
    """Return the ``events_storage`` mode declared by the config at *path*.

    Narrow, storage-mode-only read used by the backend factories
    (``cli._helpers._open_backend`` / ``mcp_server._open_backend``), which
    must pick the write/replay strategy *before* any command-level config
    concern applies:

    * Missing file → ``"local"`` — scratch projects without a config keep
      pre-1.22.0 behaviour.
    * Unparseable YAML / non-mapping shape → warn + ``"local"``. The repo's
      standing contract is that a broken config never blocks a CLI command
      (see ``tests/test_cli_sync.py::TestMalformedConfigFallsBackToRegistry``
      and the ``_load_config_optional`` soft-load pattern); a project whose
      YAML is damaged surfaces loud warnings from every config consumer, so
      the small mixed-log risk of guessing "local" loses to consistency here.
    * Parseable mapping with an INVALID ``events_storage`` value → raise.
      The user explicitly set this knob; a typo that silently fell back to
      "local" would append sequence ids into a hash-chained log, which
      order-tolerant replay would then sort incorrectly. Same load-time
      strictness as every other literal-typed field in :func:`load_config`.

    Deliberately does NOT call :func:`load_config`: full-config validation
    would make unrelated misconfigs (say, a bad ``llm_provider``) break every
    backend open — a regression for commands that never touch the LLM.
    """
    resolved = Path(path).expanduser().resolve()
    if not resolved.exists():
        return "local"
    try:
        with resolved.open(encoding="utf-8") as fh:
            raw: object = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError) as exc:
        logger.warning(
            "read_events_storage: cannot parse %s (%s: %s); "
            "assuming events_storage: local",
            resolved,
            type(exc).__name__,
            exc,
        )
        return "local"
    if not isinstance(raw, dict):
        logger.warning(
            "read_events_storage: %s is not a YAML mapping (got %s); "
            "assuming events_storage: local",
            resolved,
            type(raw).__name__,
        )
        return "local"
    value = _validate_literal(
        raw.get("events_storage", "local"),
        ("local", "git"),
        "events_storage",
    )
    return value  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _validate_required(data: dict[str, object], path: Path) -> None:
    """Raise ValueError if required top-level keys are missing or blank."""
    for key in ("project_name", "project_id"):
        val = data.get(key)
        if not val or not str(val).strip():
            raise ValueError(
                f"Config file {path} is missing required field {key!r}. "
                "Run `anvil init` to generate a valid config."
            )


def _validate_literal(
    value: object,
    allowed: tuple[str, ...],
    field_name: str,
) -> str:
    """Return *value* as str if it is in *allowed*, else raise ValueError."""
    s = str(value)
    if s not in allowed:
        raise ValueError(
            f"Invalid value {s!r} for config field {field_name!r}. "
            f"Allowed values: {allowed}."
        )
    return s


def _validate_auto_expand_threshold(value: object, config_path: Path) -> int:
    """Return *value* as an int in [1, 5], else raise ValueError.

    YAML integers arrive as ``int``; a quoted ``"4"`` arrives as ``str`` and
    is accepted for symmetry with ``default_lease_minutes`` (which coerces via
    ``int(str(...))``). Booleans are rejected explicitly — in Python
    ``bool`` is an ``int`` subclass, so ``auto_expand_threshold: true`` would
    otherwise silently become 1 (queue everything).
    """
    if isinstance(value, bool):
        raise ValueError(
            f"auto_expand_threshold must be an integer 1-5, got boolean "
            f"{value!r} ({config_path})."
        )
    try:
        threshold = int(str(value))
    except ValueError as exc:
        raise ValueError(
            f"auto_expand_threshold must be an integer 1-5, got "
            f"{value!r} ({config_path})."
        ) from exc
    if not 1 <= threshold <= 5:
        raise ValueError(
            f"auto_expand_threshold must be in the range 1-5 (the complexity "
            f"score scale), got {threshold} ({config_path})."
        )
    return threshold


def _validate_score_ceiling(
    value: object, field_name: str, config_path: Path
) -> int:
    """Return *value* as an int in [1, 5], else raise ValueError.

    Shared validator for the T020 fast-lane score ceilings
    (``fast_lane_complexity_max`` / ``fast_lane_blast_radius_max``). Same
    contract as :func:`_validate_auto_expand_threshold`: YAML ints pass through,
    a quoted ``"2"`` is accepted, and booleans are rejected explicitly (in
    Python ``bool`` is an ``int`` subclass, so ``fast_lane_complexity_max:
    true`` would otherwise silently become 1 — never fast-lane anything but the
    very smallest tasks — with no error).
    """
    if isinstance(value, bool):
        raise ValueError(
            f"{field_name} must be an integer 1-5, got boolean "
            f"{value!r} ({config_path})."
        )
    try:
        ceiling = int(str(value))
    except ValueError as exc:
        raise ValueError(
            f"{field_name} must be an integer 1-5, got {value!r} "
            f"({config_path})."
        ) from exc
    if not 1 <= ceiling <= 5:
        raise ValueError(
            f"{field_name} must be in the range 1-5 (the score scale), got "
            f"{ceiling} ({config_path})."
        )
    return ceiling


def _str_or_none(value: object) -> str | None:
    """Return None if value is None or empty string, else str(value)."""
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


def _resolve_path(value: object, base: Path) -> str:
    """Resolve *value* (str path) relative to *base* directory."""
    p = Path(str(value)).expanduser()
    if p.is_absolute():
        return str(p)
    return str((base / p).resolve())


def _parse_sync_providers(
    sync_block: object,
    config_path: Path,
) -> tuple[str, ...] | None:
    """Parse the optional top-level ``sync:`` block.

    Returns
    -------
    tuple[str, ...] | None
        * ``None`` — the ``sync`` key is absent, OR the ``sync`` block has
          no ``providers`` key. Callers SHOULD fall back to
          ``sorted(PROVIDER_REGISTRY)`` (v1.8.0 behaviour: every
          registered provider counts).
        * ``tuple[str, ...]`` — the operator pinned an explicit list of
          provider ids. May be empty (``sync.providers: []``) to opt out
          of every provider; callers MUST treat that as a no-op rather
          than falling back to the registry.

    Raises
    ------
    ValueError
        If ``sync`` is present but not a mapping, OR ``sync.providers`` is
        present but not a list of strings.
    """
    if sync_block is None:
        return None
    if not isinstance(sync_block, dict):
        raise ValueError(
            f"Config file {config_path}: top-level 'sync' key must be a "
            f"mapping, got {type(sync_block).__name__!r}."
        )
    providers = sync_block.get("providers")
    if providers is None:
        return None
    if not isinstance(providers, list):
        raise ValueError(
            f"Config file {config_path}: 'sync.providers' must be a list, "
            f"got {type(providers).__name__!r}."
        )
    out: list[str] = []
    for idx, item in enumerate(providers):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(
                f"Config file {config_path}: 'sync.providers[{idx}]' must "
                f"be a non-empty string, got {item!r}."
            )
        out.append(item.strip())
    return tuple(out)


def _render_template(*, project_name: str, project_id: str) -> str:
    """Render the default config YAML template."""
    return f"""\
# anvil configuration
# Generated by `anvil init`. Edit as needed.

# ---------------------------------------------------------------------------
# Project identity (required)
# ---------------------------------------------------------------------------
project_name: {project_name!r}
project_id: {project_id!r}

# ---------------------------------------------------------------------------
# Storage paths (relative to this file, or absolute)
# ---------------------------------------------------------------------------
db_path: state.db
events_path: events.jsonl

# ---------------------------------------------------------------------------
# LLM integration (optional — used by `anvil plan/score/expand --use-llm`)
#
# `llm_provider` picks ONE of: agent-sdk | anthropic | bedrock | custom.
# When blank, the default is `agent-sdk` — the Claude Agent SDK driving the
# bundled `claude` CLI over your logged-in subscription (no API key). This is
# the capacity-bound default; it needs the `claude` CLI on PATH at call time.
#
# `llm_fallback: true` (default false) restores env auto-detection before
# falling through to agent-sdk:
#   ANTHROPIC_API_KEY → anthropic    (direct API)
#   AWS_REGION + anthropic[bedrock] installed → bedrock
#   CUSTOM_LLM_BASE_URL → custom     (any OpenAI-compatible /v1 endpoint)
#   else → agent-sdk
#
# `llm_tier` (opus | sonnet | haiku) sets the default tier across the
# project; per-call overrides win. `llm_model` is an explicit model-id
# override that bypasses tier resolution entirely. For agent-sdk, leaving
# both blank uses the subscription's own default model.
#
# Tier-mapping defaults (refreshed 2026-05-26):
#   opus   → claude-opus-4-7        (us.anthropic.claude-opus-4-7   on Bedrock)
#   sonnet → claude-sonnet-4-6      (us.anthropic.claude-sonnet-4-6 on Bedrock)
#   haiku  → claude-haiku-4-5       (us.anthropic.claude-haiku-4-5  on Bedrock)
#
# See docs/llm.md for the full setup guide.
# ---------------------------------------------------------------------------
llm_provider:                       # agent-sdk | anthropic | bedrock | custom (blank = agent-sdk)
llm_fallback:                       # true = env auto-detect before agent-sdk (default false)
llm_tier:                           # opus|sonnet|haiku; blank=sonnet, agent-sdk=sub default
llm_model:                          # explicit model id (overrides tier)

# Bedrock-only knobs (ignored unless llm_provider resolves to "bedrock").
# Region falls back to AWS_REGION / AWS_DEFAULT_REGION env vars.
bedrock_region:                     # e.g. "us-east-1"
bedrock_profile:                    # named profile from ~/.aws/credentials

# Custom-endpoint knobs (ignored unless llm_provider resolves to "custom").
# `base_url` is REQUIRED for the custom path (either here or via env var
# CUSTOM_LLM_BASE_URL). `api_key_env` names the env var to read for the
# bearer token; defaults to CUSTOM_LLM_API_KEY then OPENAI_API_KEY.
custom_base_url:                    # e.g. "http://localhost:8000/v1"
custom_api_key_env:                 # e.g. "OPENROUTER_API_KEY"

# ---------------------------------------------------------------------------
# Claim / lease settings
# ---------------------------------------------------------------------------
# 240 min: real agent workflows routinely exceed 15 min per task; a shorter
# lease silently expires mid-work and the task reverts to ready.
default_lease_minutes: 240
default_heartbeat_minutes: 5
# Hard cap on a single claim's age = default_lease_minutes x this multiplier
# (B46). After it, renew() refuses even if heartbeating, so a wedged agent
# cannot hold a lease forever. Raise it for legitimately long-running tasks.
max_claim_age_multiplier: 4
# B49 — accept-rate governor. `anvil next` offers no new work when the human
# review queue is saturated (needs_review depth >= needs_review_cap) or the
# runner's recent accept-rate (over accept_rate_window_days) is below the floor.
accept_rate_floor: 0.80
needs_review_cap: 10
accept_rate_window_days: 7

# ---------------------------------------------------------------------------
# Git operations  (auto | record_only | off)
#   auto        — anvil creates branches and records commits
#   record_only — records what happened; does not drive git
#   off         — no git integration
# ---------------------------------------------------------------------------
git_ops_mode: auto

# ---------------------------------------------------------------------------
# Write-path durability  (relaxed | strict)
#   relaxed — DEFAULT. synchronous=NORMAL, buffered log, no per-event fsync.
#             Fast; correctness does not depend on fsync. Worst case on hard
#             power-loss: the last few un-synced events drop and you repeat
#             the last action. Right choice for a laptop.
#   strict  — synchronous=FULL + fsync(log) before COMMIT. The only mode that
#             fsyncs per event. Use on CI / shared / server storage.
# ---------------------------------------------------------------------------
durability: relaxed

# ---------------------------------------------------------------------------
# Event-log storage  (local | git)   — v1.22.0, git-backed events Phase A
#   local — DEFAULT. Sequence event ids (E000001 …), strict replay,
#           events.jsonl stays machine-scoped (gitignored).
#   git   — hash-chained event ids + order-tolerant replay; events.jsonl is
#           committed to the repo and merges across branches via merge=union.
#           Do NOT flip this by hand on an existing project — run
#           `anvil migrate-events --to git` (rewrites the log,
#           emits the id mapping, writes .gitattributes).
# ---------------------------------------------------------------------------
events_storage: local

# ---------------------------------------------------------------------------
# Branch naming convention (v1.15.0)
#
# Prefix applied to branches created by `anvil claim`. Defaults to
# `agent` (advertises that an agent worked the task). Override to match
# the host project's convention so PR templates, CODEOWNERS, branch
# protection rules, and CI hooks fire as expected.
#
#   branch_prefix: agent     # default: agent/<task>-<slug>
#   branch_prefix: feature   # feature/<task>-<slug>
#   branch_prefix: fix       # fix/<task>-<slug>
#   branch_prefix: ""        # no prefix: <task>-<slug>
#
# Nested prefixes (e.g. `feature/agent`) are also accepted verbatim.
# ---------------------------------------------------------------------------
branch_prefix: agent

# ---------------------------------------------------------------------------
# Cross-PRD claim guard (warn | refuse)
#
# When `anvil claim` is scoped with --prd or $ANVIL_PRD, but the requested task
# belongs to a different PRD partition:
#   warn   — DEFAULT. Warn, then proceed.
#   refuse — exit 1 unless the caller passes --force.
# ---------------------------------------------------------------------------
crossPrdGuard: warn

# ---------------------------------------------------------------------------
# Auto-expansion (v1.21.0)
#
# After `anvil score`, every task whose complexity score is at or
# above `auto_expand_threshold` is listed in an EXPANSION QUEUE section
# with the exact follow-up command (`anvil expand TXXX --use-llm`).
# The same threshold gates `anvil expand` itself. Queueing is
# deterministic; the LLM-side decomposition only runs via expand --use-llm.
#
#   auto_expand: true            # default; set false to silence the queue
#   auto_expand_threshold: 4     # 1-5 (complexity score scale)
# ---------------------------------------------------------------------------
auto_expand: true
auto_expand_threshold: 4

# ---------------------------------------------------------------------------
# Fast-lane work packets — right-size process by score (T020)
#
# A task whose complexity AND blast_radius scores are both at or below these
# ceilings is treated as "trivial": `anvil packet` renders it a minimal,
# single-step work packet with a trimmed required-evidence set (fewer fields
# for the agent to satisfy) instead of the full update-protocol prose. The task
# still records an immutable completion-evidence transition exactly like any
# other — only the packet shape is right-sized. A task above either ceiling
# (e.g. a tiny change that touches a schema/config/public-API surface) always
# gets the full packet — the safe default.
#
#   fast_lane_complexity_max: 2     # 1-5 (complexity score scale)
#   fast_lane_blast_radius_max: 2   # 1-5 (blast-radius score scale)
# ---------------------------------------------------------------------------
fast_lane_complexity_max: 2
fast_lane_blast_radius_max: 2

# ---------------------------------------------------------------------------
# Completion-evidence enforcement (T025/B25)
#
# The evidence gate checks submitted evidence against each task's
# `required_evidence` list. By default the gate is ADVISORY — `apply --approve`
# shows the verdict but still approves even when evidence is missing.
#
#   strict_evidence: false  # DEFAULT — advisory; apply approves regardless.
#   strict_evidence: true   # enforce — apply --approve REFUSES (exit 1) when
#                           # required evidence is missing. --reject is
#                           # unaffected; tasks with no required_evidence and a
#                           # complete gate proceed normally.
#
# Override per-invocation with `apply --strict` / `apply --no-strict`
# (flag > config > default).
# ---------------------------------------------------------------------------
strict_evidence: false

# ---------------------------------------------------------------------------
# GitHub sync (optional)
# ---------------------------------------------------------------------------
sync_github_enabled: false
sync_github_conflict_strategy: prompt  # local_wins | remote_wins | prompt | manual_merge

# ---------------------------------------------------------------------------
# S3 durable storage (optional)
# Run `anvil backup` to push events.jsonl; `anvil restore` to pull + replay.
# Requires boto3: pip install 'anvil-state[s3]'
# ---------------------------------------------------------------------------
# durable_store: s3
# s3_bucket: my-anvil-backups
# s3_prefix: my-project          # key prefix; recommended to avoid collisions
# s3_region:                     # defaults to AWS_REGION env
# s3_profile:                    # named AWS profile
"""
