"""FastMCP (stdio) server — agent-facing tools for anvil.

Each tool opens a fresh SqliteBackend against the project's .anvil/state.db.
State resolves per call from the cwd arg (workflow tools), else ANVIL_ROOT,
else Path.cwd(); the no-cwd tools are pinned to the server's launch directory.

Stale-claim reaping runs at the top of every mutating tool and on
get_project_summary; read-only listers skip it for latency. Claim tools use the
same transactional Git plan as the CLI when the resolved project is a Git
repository; non-Git projects continue through the state-only claim path.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema

from anvil.build_identity import get_build_identity
from anvil.cli._actor_output import (
    actor_identity_data,
    actor_mismatch_data,
    bundle_continuation_data,
    continuation_data,
)
from anvil.state.models import (
    RejectionQualityFinding,
    RejectionQualityFindingCode,
    RejectionReasonCode,
    TaskRejectionProvenance,
)
from anvil.state.rollup import BundleRollupEntry

if TYPE_CHECKING:
    from anvil.claims.command_proof_artifact import LoadedClaimCommandProof
    from anvil.cli._helpers import IngestedPrdSource
    from anvil.state.models import ClaimCommandProof

# ---------------------------------------------------------------------------
# FastMCP instance
# ---------------------------------------------------------------------------

mcp: FastMCP = FastMCP("anvil", version=get_build_identity().display_version)

_MAX_MCP_SCHEMA_ERROR_BYTES = 4_096

# ---------------------------------------------------------------------------
# Planning vs execution surface split (audit item L2)
# ---------------------------------------------------------------------------
#
# The 36 tools fall into two groups:
#
#   EXECUTION (24) — the turn-to-turn loop an agent runs while doing work:
#       get_next_task, claim_task, release_task, renew_claim, submit_progress,
#       submit_completion_evidence, update_task_status, get_task,
#       get_project_status, get_project_summary, list_tasks, check_conflicts,
#       generate_work_packet, get_dependency_graph
#       plus 10 coordinator-bundle execution/read tools
#
#   PLANNING (12) — one-shot bootstrap/plan/review operations run rarely (often
#       once per project), tagged ``planning`` below:
#       init_project, parse_prd, assess_prd, review_prd, plan_tasks, score_tasks,
#       review_tasks, apply_review_decision, edit_dependencies, find_decisions,
#       describe_surface, create_bundle
#
# Every planning tool carries the ``planning`` tag. The live stdio server hides
# the planning surface BY DEFAULT (``apply_surface_gate`` at startup) so a steady-
# state execution client never pays the ~1.2k-token planning schema cost on every
# turn. Setting ``ANVIL_MCP_PLANNING`` (truthy) keeps all 36 tools on the wire —
# use it for the planning phase, or run a second server entry with the flag set.
#
# IMPORTANT: the gate is applied ONLY when the live server starts (see
# ``apply_surface_gate``), never at import time. So ``from anvil.mcp_server import
# mcp`` still sees all 36 registered tools, and every introspection surface that
# reports "what the engine can do" — ``describe_surface``, ``anvil describe``,
# ``mcp_tool_names()``, the ``--help`` tool list, the Docker catalog smoke test —
# is unchanged. Only the per-turn wire surface of the *default* execution server
# shrinks. No tool is removed; all 36 remain reachable.

PLANNING_TAG = "planning"

# FastMCP/Pydantic must not render raw hostile values before this tool's own
# bounded validation runs. ``Any`` makes runtime input unconstrained, while
# WithJsonSchema preserves the public list[list[string]] | null contract.
_DEPENDENCY_EDGES_INPUT_SCHEMA: dict[str, Any] = {
    "anyOf": [
        {
            "items": {"items": {"type": "string"}, "type": "array"},
            "type": "array",
        },
        {"type": "null"},
    ]
}
DependencyEdgesInput = Annotated[
    Any,
    WithJsonSchema(_DEPENDENCY_EDGES_INPUT_SCHEMA),
]

# Env flag that opts a live server back into the full 36-tool surface.
_PLANNING_ENV = "ANVIL_MCP_PLANNING"


def _planning_surface_enabled(env: dict[str, str] | None = None) -> bool:
    """Return True when the planning surface should be exposed on the wire.

    Resolves from the ``ANVIL_MCP_PLANNING`` env var. Truthy values
    (``1``/``true``/``yes``/``on``, case-insensitive) enable the full 36-tool
    surface; anything else (incl. unset) yields the lean execution-only default.
    """
    import os

    source = os.environ if env is None else env
    raw = source.get(_PLANNING_ENV, "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def apply_surface_gate(
    server: FastMCP = mcp, env: dict[str, str] | None = None
) -> bool:
    """Hide the planning tool surface on *server* unless the env flag opts in.

    Called once at live-server startup (``main``) and by the context audit so the
    measured/served surface matches. Returns True when the planning surface is
    exposed (no gate applied), False when it was hidden.

    Idempotent and reversible: re-enables the planning tags first, then disables
    them when the flag is off, so calling it twice (or after a prior enable)
    converges to the same state.
    """
    if _planning_surface_enabled(env):
        # Full surface: ensure planning tools are visible (covers a prior gate).
        server.enable(tags={PLANNING_TAG})
        return True
    server.disable(tags={PLANNING_TAG})
    return False

# ---------------------------------------------------------------------------
# Return-type Pydantic models (what each tool returns)
# ---------------------------------------------------------------------------


class TaskCountsByStatus(BaseModel):
    """Task counts broken down by status for the project summary."""

    model_config = ConfigDict(extra="forbid")

    proposed: int = 0
    drafted: int = 0
    reviewed: int = 0
    ready: int = 0
    claimed: int = 0
    in_progress: int = 0
    blocked: int = 0
    needs_review: int = 0
    accepted: int = 0
    done: int = 0
    rejected: int = 0


class PrdStatusEntry(BaseModel):
    """One per-PRD slice of project state (T020).

    Additive: ``get_project_status`` / ``get_project_summary`` grow a ``prds``
    list of these alongside the existing flat project-total fields. On a single-
    PRD DB there is exactly one entry whose numbers equal those flat totals.
    """

    model_config = ConfigDict(extra="forbid")

    prd_id: str
    status: str
    task_counts: TaskCountsByStatus
    total_tasks: int
    ready_task_count: int
    active_claim_count: int


def _prd_status_entries(
    prds: Any, tasks: Any, active_claims: Any
) -> list[PrdStatusEntry]:
    """Adapt the pure :func:`compute_prd_rollup` output to ``PrdStatusEntry``.

    Keeps the per-PRD aggregation logic in one place (anvil.state.rollup) so the
    CLI ``anvil status`` and these MCP tools never drift.
    """
    from anvil.state.rollup import compute_prd_rollup

    entries: list[PrdStatusEntry] = []
    for r in compute_prd_rollup(prds, tasks, active_claims):
        counts = TaskCountsByStatus()
        for status_val, n in r.task_counts.items():
            if hasattr(counts, status_val):
                setattr(counts, status_val, n)
        entries.append(
            PrdStatusEntry(
                prd_id=r.prd_id,
                status=r.status,
                task_counts=counts,
                total_tasks=r.total_tasks,
                ready_task_count=r.ready_task_count,
                active_claim_count=r.active_claim_count,
            )
        )
    return entries


def _bundle_status_entries(
    backend: Any, tasks: Any, active_claims: Any
) -> list[BundleRollupEntry]:
    """Return the same bundle rollup used by CLI ``status --json``."""
    from anvil.clock import SystemClock
    from anvil.state.rollup import compute_bundle_rollup

    bundles = backend.list_bundles()
    bundle_ids = {bundle.id for bundle in bundles}
    bundle_claims = [
        claim for claim in backend.list_bundle_claims() if claim.bundle_id in bundle_ids
    ]
    reviews = [
        review
        for bundle in bundles
        for review in backend.list_bundle_reviews(bundle.id)
    ]
    return compute_bundle_rollup(
        bundles,
        tasks,
        bundle_claims,
        reviews,
        active_claims,
        now=SystemClock().now(),
    )


class ProjectSummary(BaseModel):
    """Summary of project state returned by get_project_summary."""

    model_config = ConfigDict(extra="forbid")

    project_id: str
    project_name: str
    project_description: str
    prd_status: str | None
    task_counts: TaskCountsByStatus
    active_claim_count: int
    blocked_task_count: int
    ready_task_count: int
    # T020: additive per-PRD rollup. Flat fields above remain the PROJECT TOTAL.
    prds: list[PrdStatusEntry] = Field(default_factory=list)
    bundles: list[BundleRollupEntry] = Field(default_factory=list)


class ClaimResponse(BaseModel):
    """Claim details returned by claim_task."""

    model_config = ConfigDict(extra="forbid")

    id: str
    task_id: str
    claimed_by: str
    lease_expires_at: str
    branch: str | None
    worktree_path: str | None
    git_metadata: dict[str, Any] | None = None
    expected_files: list[str]
    # Advisory notes (e.g. the worktree_isolation shared-checkout warning);
    # additive with a default so existing readers are unaffected.
    warnings: list[str] = []
    actor_identity: dict[str, Any] = Field(default_factory=dict)
    continuation: dict[str, Any] = Field(default_factory=dict)
    generation: int = 1
    attestation_context: dict[str, Any] | None = None


class ReleaseResponse(BaseModel):
    """Result of release_task."""

    model_config = ConfigDict(extra="forbid")

    released: bool
    claim_id: str
    actor_identity: dict[str, Any] = Field(default_factory=dict)


class RenewProgressReceipt(BaseModel):
    """The progress fact that authorized, or declined, one renewal."""

    model_config = ConfigDict(extra="forbid")

    source: Literal["attestation", "file_changed", "legacy_unmeasurable", "none"]
    digest: str | None = None
    generation: int | None = None
    trust_mode: Literal[
        "claim_owner_self_attested", "configured_issuer_verified"
    ] | None = None


class RenewResponse(BaseModel):
    """Result of renew_claim."""

    model_config = ConfigDict(extra="forbid")

    lease_expires_at: str
    # B46 part 2: False when the renew was a no-op (no progress since the last
    # heartbeat), so the lease was NOT extended and ``lease_expires_at`` is the
    # unchanged, possibly-imminent expiry — the client should not treat it as a
    # fresh lease.
    renewed: bool = True
    actor_identity: dict[str, Any] = Field(default_factory=dict)
    progress: RenewProgressReceipt | None = None


class WorkPacketResponse(BaseModel):
    """Result of generate_work_packet."""

    model_config = ConfigDict(extra="forbid")

    format: str
    content: Any  # str for markdown, dict for json


class ProgressAttestationReceipt(BaseModel):
    """Stable accepted-attestation receipt returned to MCP clients."""

    model_config = ConfigDict(extra="forbid")

    digest: str
    generation: int
    trust_mode: Literal[
        "claim_owner_self_attested", "configured_issuer_verified"
    ]
    kind: Literal["commit", "file"]
    issuer_id: str | None = None


class ProgressResponse(BaseModel):
    """Result of submit_progress."""

    model_config = ConfigDict(extra="forbid")

    recorded: bool
    actor_identity: dict[str, Any] = Field(default_factory=dict)
    event_action: str = "progress.noted"
    attestation: ProgressAttestationReceipt | None = None


class BundleReviewPolicyRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_reviews: int
    max_rereviews: int
    independent_reviewer_required: bool
    required_angles: list[str]


class BundleThroughputBudgetRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_tasks: int
    max_serial_stages: int


class BundleCheckpointRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    commit_sha: str | None = None
    pr_url: str | None = None
    recorded_at: str
    recorded_by: str


class DelegatedAgentRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    handle: str | None = None
    runtime: str | None = None
    task_ids: list[str] = Field(default_factory=list)
    status: str
    observed_at: str
    detail: str | None = None


class BundleRecord(BaseModel):
    """Compact explicit wire schema for an execution bundle."""

    model_config = ConfigDict(extra="forbid")

    id: str
    creation_event_id: str
    prd_id: str
    task_ids: list[str]
    coordinator: str
    status: str
    review_disposition_event_id: str | None = None
    superseded_by: str | None = None
    last_result_at: str | None = None
    branch: str | None = None
    worktree_path: str | None = None
    review_policy: BundleReviewPolicyRecord
    throughput_budget: BundleThroughputBudgetRecord
    delegated_agents: list[DelegatedAgentRecord] = Field(default_factory=list)
    checkpoint: BundleCheckpointRecord | None = None
    created_at: str
    updated_at: str


class BundleClaimRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    bundle_id: str
    claimed_by: str
    status: str
    branch: str | None = None
    worktree_path: str | None = None
    git_metadata: dict[str, Any] | None = None
    session_id: str | None = None
    expected_files: list[str]
    member_claim_ids: dict[str, str]
    created_at: str
    lease_expires_at: str
    last_heartbeat_at: str
    released_at: str | None = None
    release_reason: str | None = None


class BundleReviewVerdictRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    bundle_id: str
    creation_event_id: str
    disposition_event_id: str
    review_round: int
    angle: str
    reviewed_by: str
    decision: str
    notes: str | None = None
    created_at: str


class BundleDetailResponse(BaseModel):
    """Typed bundle read response shared by bundle MCP operations."""

    model_config = ConfigDict(extra="forbid")

    bundle: BundleRecord
    claim: BundleClaimRecord | None = None
    reviews: list[BundleReviewVerdictRecord] = Field(default_factory=list)


class BundleListResponse(BaseModel):
    """Stable list envelope for bundle discovery."""

    model_config = ConfigDict(extra="forbid")

    bundles: list[BundleRecord] = Field(default_factory=list)


class BundleClaimResponse(BaseModel):
    """Coordinator claim plus canonical projected bundle."""

    model_config = ConfigDict(extra="forbid")

    bundle: BundleRecord
    claim: BundleClaimRecord
    warnings: list[str] = Field(default_factory=list)
    actor_identity: dict[str, Any] = Field(default_factory=dict)
    continuation: dict[str, Any] = Field(default_factory=dict)


class BundleReviewGateResponse(BaseModel):
    """Typed bounded-review gate result."""

    model_config = ConfigDict(extra="forbid")

    passed: bool
    review_round: int
    reviews_used: int
    rereviews_used: int
    missing_angles: list[str] = Field(default_factory=list)
    missing_reviewers: int = 0
    blocking_findings: list[str] = Field(default_factory=list)
    invalid_reviewers: list[str] = Field(default_factory=list)
    replan_required: bool = False


class BundleReviewResponse(BaseModel):
    """Canonical bundle plus deterministic bounded-review gate."""

    model_config = ConfigDict(extra="forbid")

    bundle: BundleRecord
    gate: BundleReviewGateResponse


class BundleCheckpointResponse(BaseModel):
    """Canonical checkpoint and bundle projection."""

    model_config = ConfigDict(extra="forbid")

    bundle: BundleRecord
    checkpoint: BundleCheckpointRecord


class BundleProgressResponse(BaseModel):
    """Progress plus optional completion/readiness transition."""

    model_config = ConfigDict(extra="forbid")

    bundle: BundleRecord
    recorded: bool = True
    can_mark_implemented: bool | None = None
    unproven_members: dict[str, list[str]] = Field(default_factory=dict)


class NextReadyTask(BaseModel):
    """Compact descriptor of the next claimable task, surfaced in finish/submit
    responses so the caller can chain into the next piece of work. ``null``
    when no task is claimable."""

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    priority: str


class GovernorProjectionResponse(BaseModel):
    """Complete, deterministic accept-rate governor calculation."""

    model_config = ConfigDict(extra="forbid")

    as_of: str
    window_days: float
    window_start: str
    numerator: int
    denominator: int
    rate: float | None
    floor: float
    configured_floor: float
    needs_review_depth: int
    needs_review_cap: int
    guidance: str
    withheld_reason: str | None = None
    offer_throttled: bool = False


class GetNextTaskResponse(BaseModel):
    """Next task plus truthful governor state, including empty/withheld queues."""

    model_config = ConfigDict(extra="forbid")

    task: dict[str, Any] | None
    governor: GovernorProjectionResponse
    actor_identity: dict[str, Any] = Field(default_factory=dict)


class EvidenceResponse(BaseModel):
    """Result of submit_completion_evidence."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    task_status: str
    # T014: name the next claimable task (deps/claims/conflict-group/file-overlap
    # aware) so the agent can chain work; null when none is available.
    next_ready: NextReadyTask | None = None
    actor_identity: dict[str, Any] = Field(default_factory=dict)
    claim_bound_command_proofs: list[dict[str, Any]] = Field(default_factory=list)
    hook_command_proofs: list[dict[str, Any]] = Field(default_factory=list)
    missing_claim_bound_proofs: list[str] = Field(default_factory=list)
    missing_legacy_evidence: list[str] = Field(default_factory=list)


class ConflictEntry(BaseModel):
    """A single conflict entry from check_conflicts."""

    model_config = ConfigDict(extra="forbid")

    file: str
    claim_id: str
    claimed_by: str
    task_id: str


class ConflictCheckResponse(BaseModel):
    """Result of check_conflicts."""

    model_config = ConfigDict(extra="forbid")

    conflicts: list[ConflictEntry]


class DependencyNode(BaseModel):
    """A node in the dependency graph."""

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    status: str
    priority: str
    feature_id: str


class DependencyEdge(BaseModel):
    """A directed edge in the dependency graph (from → to)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_task: str = Field(alias="from")
    to_task: str = Field(alias="to")


class DependencyGraphResponse(BaseModel):
    """Result of get_dependency_graph."""

    model_config = ConfigDict(extra="forbid")

    nodes: list[DependencyNode]
    edges: list[DependencyEdge]
    ready_to_claim: list[str]


class StatusUpdateResponse(BaseModel):
    """Result of update_task_status."""

    model_config = ConfigDict(extra="forbid")

    from_status: str
    to_status: str


class EditDependenciesResponse(BaseModel):
    """Result of edit_dependencies.

    ``changed`` lists every task whose dependency set was actually mutated;
    ``added`` / ``removed`` are the ``[source, target]`` edges (source depends
    on target) that took effect — no-op edges are excluded from both.
    """

    model_config = ConfigDict(extra="forbid")

    prd_id: str
    changed: list[str]
    added: list[list[str]]
    removed: list[list[str]]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_STATE_DIR_NAME = ".anvil"

# Allowed transitions for update_task_status per spec:
# "Limited to drafted↔ready and blocked toggle"
_ALLOWED_STATUS_TRANSITIONS: dict[str, set[str]] = {
    "drafted": {"ready"},
    "ready": {"drafted"},
    "in_progress": {"blocked"},
    "blocked": {"in_progress"},
    # spec also allows toggling blocked for claimed tasks
    "claimed": {"blocked"},
}


def _require_actor(actor: str) -> str:
    """Validate and NFC-canonicalize a newly introduced audit identity."""
    from anvil.actors import ActorIdentityError, canonicalize_new_actor

    try:
        return canonicalize_new_actor(actor)
    except ActorIdentityError as exc:
        raise ToolError(str(exc)) from exc


def _exact_lifecycle_actor(actor: str) -> str:
    """Return a persisted lifecycle owner exactly, including legacy-invalid IDs.

    Renew/release/progress/submit compare this byte-for-byte with the active
    claim before appending anything.  Validation belongs only to creation: a
    historical empty, whitespace, non-NFC, or control-bearing owner must remain
    addressable without being silently rewritten or orphaned.
    """
    return actor


def _actor_mismatch_tool_error(*, owner: str, actual: str, action: str) -> ToolError:
    """Return an exact but JSON-escaped MCP ownership refusal."""
    detail = {
        "code": "actor_mismatch",
        "message": f"Only the claim owner may {action}.",
        **actor_mismatch_data(owner=owner, actual=actual),
    }
    return ToolError(json.dumps(detail, ensure_ascii=True, separators=(",", ":")))


def _resolve_state_dir(cwd: str | None = None) -> Path:
    """Return the absolute path to .anvil/ for the given cwd.

    Each MCP tool call resolves state relative to cwd at call time so agents
    can invoke from any project directory. The optional ``cwd`` argument lets
    workflow tools (init_project, parse_prd, etc.) point at a different
    project root without restarting the MCP server.

    Resolution precedence (T005/B07) — identical to the CLI
    (``cli/_helpers._resolve_state_dir``), so a host configures one project
    root and both surfaces agree:

        explicit ``cwd`` arg  >  ANVIL_ROOT env  >  Path.cwd()

    ``ANVIL_ROOT`` points at the project root (the dir containing
    ``.anvil/``) and is consulted only when no explicit ``cwd`` is
    given. If it is set but does not contain a ``.anvil/`` directory we
    raise ``ToolError`` — never a silent fall back to cwd, which would mask the
    misconfiguration for an MCP host that has no meaningful cwd.
    """
    # Delegate to the centralized CLI resolver so the env-override precedence
    # lives in exactly one place. Translate its ClickException into a ToolError
    # so MCP clients receive a structured error instead of a CLI-shaped one.
    from anvil.cli._helpers import StateRootError
    from anvil.cli._helpers import _resolve_state_dir as _cli_resolve

    try:
        return _cli_resolve(Path(cwd) if cwd else None)
    except StateRootError as exc:
        raise ToolError(exc.message) from exc


def _resolve_prd_id(backend: Any, prd_id: str | None = None) -> str:
    """Resolve which PRD partition an MCP tool targets (T018).

    Delegates to the shared CLI resolver (:func:`cli._helpers.resolve_prd_id`)
    so the MCP ``prd_id`` argument and the CLI ``--prd`` flag pick the IDENTICAL
    PRD for identical DB + env inputs. Precedence is therefore the same:

        explicit ``prd_id``  >  $ANVIL_PRD  >  single PRD | default | error

    Translates the CLI ambiguity ``ClickException`` into a ``ToolError`` so MCP
    clients receive a structured error instead of a CLI-shaped one (mirrors how
    :func:`_resolve_state_dir` translates ``StateRootError``).
    """
    from anvil.cli._helpers import PrdAmbiguityError, resolve_prd_id

    try:
        return resolve_prd_id(backend, prd_id)
    except PrdAmbiguityError as exc:
        raise ToolError(exc.message) from exc


def _open_backend(state_dir: Path):  # type: ignore[return]
    """Open a fresh SqliteBackend for the given state_dir.

    Raises ToolError if the project is uninitialized or its schema is
    incompatible. Initialization failures close the constructed backend here;
    successful callers must close the returned backend in a try/finally.
    """
    from anvil.cli._helpers import (
        BoundedSchemaProbe,
        schema_diagnostic_from_exception,
    )
    from anvil.clock import SystemClock
    from anvil.config import read_events_storage
    from anvil.state.backend import SchemaMismatch
    from anvil.state.sqlite import SqliteBackend

    if not state_dir.exists():
        raise ToolError(
            f"anvil not initialized in {state_dir.parent}. "
            "Run `anvil init` in your project root first.",
        )
    db_path = str(state_dir / "state.db")
    events_path = str(state_dir / "events.jsonl")
    schema_probe = BoundedSchemaProbe()
    backend = None
    primary_failure = False

    def close_backend_without_masking() -> None:
        nonlocal backend
        closing = backend
        if closing is None:
            return
        # A cancellation can interrupt close before it releases the SQLite
        # connection. Retain ownership and retry once; cleanup failures still
        # must not replace the schema/probe failure that caused cleanup.
        for _ in range(2):
            try:
                closing.close()
                break
            except BaseException:
                continue
        backend = None

    def close_probe_after_interruption() -> BaseException | None:
        first_failure: BaseException | None = None
        # The first close can itself be interrupted before it reaches the
        # worker. A second bounded attempt preserves the original failure while
        # ensuring the worker and pipe handles still get a cleanup opportunity.
        for _ in range(2):
            try:
                schema_probe.close()
                return first_failure
            except BaseException as exc:
                if first_failure is None:
                    first_failure = exc
        return first_failure

    try:
        # Match the CLI boundary: compatibility wins over fallible config, and
        # the same cumulative worker budget covers preflight plus initialize.
        SqliteBackend.validate_schema_compatibility(schema_probe(db_path))
        backend = SqliteBackend(
            db_path=db_path,
            events_path=events_path,
            clock=SystemClock(),
            # v1.22.0: the storage mode decides the event-id format and the
            # replay strategy, so it must be resolved BEFORE the backend opens.
            events_storage=read_events_storage(state_dir / "config.yaml"),
            schema_probe_fn=schema_probe,
        )
        backend.initialize()
    except BaseException as exc:
        primary_failure = True
        close_backend_without_masking()
        if isinstance(exc, SchemaMismatch):
            diagnostic = schema_diagnostic_from_exception(exc)
            message = json.dumps(
                {"error": diagnostic.as_dict()},
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            if len(message.encode("utf-8")) > _MAX_MCP_SCHEMA_ERROR_BYTES:
                message = (
                    '{"error":{"code":"schema_mismatch",'
                    '"guidance":"Upgrade Anvil and restart the MCP server; '
                    'do not delete state."}}'
                )
            raise ToolError(message) from None
        raise
    finally:
        probe_failure = close_probe_after_interruption()
        if probe_failure is not None:
            close_backend_without_masking()
            if not primary_failure:
                raise probe_failure
    return backend


def _reap_stale(backend: Any) -> None:
    """Run the stale-claim detector; failures are best-effort (never block)."""
    try:
        from anvil.claims.stale import detect_and_release_stale
        from anvil.clock import SystemClock

        detect_and_release_stale(backend, SystemClock())
    except Exception:  # noqa: BLE001
        pass


def _find_active_claim_for_task(backend: Any, task_id: str) -> Any | None:
    """Return the active Claim for task_id, or None if none found."""
    for claim in backend.list_active_claims():
        if claim.task_id == task_id:
            return claim
    return None


def _compute_next_ready(backend: Any, actor: str | None = None) -> dict[str, Any] | None:
    """Return a thin descriptor of the next claimable task, or None.

    Shared by the finish/submit surfaces (T014) so the response can name the
    next ready task immediately after a task transitions out of the active set.
    Reuses ``ClaimManager.next_ready_excluding_active_files`` so the suggestion
    respects dependencies, active claims, conflict groups AND file-conflict
    exclusions (a task whose files overlap an active claim is never named).

    The descriptor is intentionally compact — {id, title, priority} — so the
    field stays cheap on a hot path and stable across CLI/MCP. Returns None
    when no task is claimable.
    """
    from anvil.claims.manager import ClaimManager
    from anvil.clock import SystemClock

    manager = ClaimManager(
        backend,
        SystemClock(),
        actor=actor or "agent",
    )
    task = manager.next_ready_excluding_active_files()
    if task is None:
        return None
    return {
        "id": task.id,
        "title": task.title,
        "priority": task.priority.value,
    }


def _resolve_strict_evidence(strict: bool | None, state_dir: Path) -> bool:
    """Resolve the effective strict-evidence mode for an MCP tool call.

    Mirrors ``cli/packet_apply._resolve_strict_evidence`` so the MCP accept
    path enforces the same completion-evidence gate as ``anvil apply``.
    The MCP path is the surface agents actually use (they complete work via
    MCP, not the CLI), so leaving it ungated lets an agent mark a task done
    with missing required evidence — exactly what strict mode exists to stop.

    Precedence (same as the CLI):

        explicit ``strict`` param  >  $ANVIL_STRICT_EVIDENCE  >  config  >  False

    The ``ANVIL_STRICT_EVIDENCE`` env lets an autonomous loop / fleet enforce
    strict mode across every unattended accept without per-project config
    (B48 acceptance 1).

    Args:
        strict: Tri-state override. ``True``/``False`` are explicit; ``None``
            defers to the project config (then the default).
        state_dir: ``.anvil/`` directory whose ``config.yaml`` carries
            ``strict_evidence``.

    Returns:
        True if strict enforcement is in effect, else False (advisory default).

    Fail-closed on intent (should_fix): if ``config.yaml`` *exists* but fails
    to load, we do NOT silently treat strict as off — we emit a warning to
    stderr so a broken config that was meant to enable enforcement does not
    quietly disable it. (We still fall back to ``False`` to avoid hard-failing
    every accept on a malformed config, matching the soft-load contract used
    everywhere else; the warning is the signal.)
    """
    if strict is not None:
        return strict

    from anvil.cli.packet_apply import (
        _strict_evidence_env,
        _warn_if_env_overrides_strict_config,
    )

    env = _strict_evidence_env()
    if env is not None:
        _warn_if_env_overrides_strict_config(env, state_dir)
        return env

    config_path = state_dir / "config.yaml"
    if not config_path.exists():
        return False

    import yaml

    try:
        from anvil.config import load_config

        return load_config(config_path).strict_evidence
    except (FileNotFoundError, OSError, ValueError, yaml.YAMLError) as exc:
        print(
            f"Warning: config.yaml load failed "
            f"({type(exc).__name__}: {exc}); strict-evidence enforcement could "
            "not be resolved from config and is treated as OFF for this call. "
            "Fix config.yaml to restore strict mode.",
            file=sys.stderr,
        )
        return False


def _load_merged_config_optional(state_dir: Path):  # type: ignore[no-untyped-def]
    """Soft-load the project config with the GLOBAL layer merged underneath.

    retro-opps T003 (review MUST-FIX): the CLI derives review tiers from
    ``_load_config_optional`` → ``load_merged_config`` (global merged under
    project), so the MCP tier surfaces must use the same merged loader —
    ``load_config`` alone ignores a tier key set only in
    ``~/.config/anvil/config.yaml`` and silently derives a DIFFERENT tier
    than ``anvil next``/``show`` for the same task. Returns ``None`` when
    there is no config.yaml or it fails to parse (derive with defaults).
    """
    config_path = state_dir / "config.yaml"
    if not config_path.exists():
        return None

    import yaml

    try:
        from anvil.config import load_merged_config

        return load_merged_config(config_path)
    except (FileNotFoundError, OSError, ValueError, yaml.YAMLError) as exc:
        print(
            f"Warning: config.yaml load failed "
            f"({type(exc).__name__}: {exc}); review tier derived from "
            "built-in defaults for this call.",
            file=sys.stderr,
        )
        return None


def _load_fast_lane_config(state_dir: Path):  # type: ignore[no-untyped-def]
    """Soft-load the project config for T020 fast-lane packet routing.

    Returns a ``Config`` (carrying ``fast_lane_complexity_max`` /
    ``fast_lane_blast_radius_max``) or ``None`` when there is no config.yaml or
    it fails to parse — in which case ``generate_work_packet`` falls back to
    ``render_packet`` with the renderer's built-in default ceilings. A broken
    config never blocks packet generation.
    """
    config_path = state_dir / "config.yaml"
    if not config_path.exists():
        return None

    import yaml

    try:
        from anvil.config import load_config

        return load_config(config_path)
    except (FileNotFoundError, OSError, ValueError, yaml.YAMLError) as exc:
        print(
            f"Warning: config.yaml load failed "
            f"({type(exc).__name__}: {exc}); fast-lane packet thresholds could "
            "not be resolved from config; using built-in defaults for this call.",
            file=sys.stderr,
        )
        return None


# ---------------------------------------------------------------------------
# Tool 1: get_project_summary
# ---------------------------------------------------------------------------


@mcp.tool
def get_project_summary() -> ProjectSummary:
    """Summarize project state: info, task counts by status, active claims,
    blocked count, ready count. Reaps stale claims first."""
    state_dir = _resolve_state_dir()
    backend = _open_backend(state_dir)
    try:
        _reap_stale(backend)

        project = backend.get_project()
        if project is None:
            raise ToolError(
                "Project not found — run `anvil init` to initialize.",
            )

        # T021 audit (get_prd no-arg): default-only-correct. The flat
        # ``prd_status`` field is the legacy single-PRD summary — it reads the
        # default PRD's status. Multi-PRD callers read the additive per-PRD
        # ``prds`` rollup below (built from list_prds()), which scopes each
        # entry's status to its own partition; the flat field stays the default.
        prd = backend.get_prd()
        prds = backend.list_prds()
        all_tasks = backend.list_tasks()
        active_claims = backend.list_active_claims()

        counts = TaskCountsByStatus()
        blocked_count = 0
        ready_count = 0
        for task in all_tasks:
            status_val = task.status.value
            if hasattr(counts, status_val):
                setattr(counts, status_val, getattr(counts, status_val) + 1)
            if status_val == "blocked":
                blocked_count += 1
            if status_val == "ready":
                ready_count += 1

        return ProjectSummary(
            project_id=project.id,
            project_name=project.name,
            project_description=project.description,
            prd_status=prd.status.value if prd is not None else None,
            task_counts=counts,
            active_claim_count=len(active_claims),
            blocked_task_count=blocked_count,
            ready_task_count=ready_count,
            # T020: per-PRD rollup; flat fields above stay the project total.
            prds=_prd_status_entries(prds, all_tasks, active_claims),
            bundles=_bundle_status_entries(backend, all_tasks, active_claims),
        )
    finally:
        backend.close()


# ---------------------------------------------------------------------------
# Tool 2: list_tasks
# ---------------------------------------------------------------------------


@mcp.tool
def list_tasks(
    status: str | None = None,
    feature_id: str | None = None,
    claimed_by: str | None = None,
    task_type: str | None = None,
    cwd: str | None = None,
) -> list[dict[str, Any]]:
    """List tasks, optionally filtered by status, feature_id, task_type
    (feature/bugfix/refactor/modify), and/or claimed_by actor.

    Args:
        claimed_by: Filter to tasks with an active claim held by this actor.
        cwd: Project root. Defaults to ``Path.cwd()``.
    """
    state_dir = _resolve_state_dir(cwd)
    backend = _open_backend(state_dir)
    try:
        tasks = backend.list_tasks(
            status=status, feature_id=feature_id, task_type=task_type
        )

        if claimed_by is not None:
            # Cross-reference active claims to filter by actor.
            active_claims = backend.list_active_claims()
            claimed_task_ids = {
                c.task_id for c in active_claims if c.claimed_by == claimed_by
            }
            tasks = [t for t in tasks if t.id in claimed_task_ids]

        return [json.loads(t.model_dump_json()) for t in tasks]
    finally:
        backend.close()


# ---------------------------------------------------------------------------
# Tool 3: get_task
# ---------------------------------------------------------------------------


@mcp.tool
def get_task(task_id: str) -> dict[str, Any]:
    """Return the full Task with the given ID (ToolError if not found).

    The response carries a derived ``review_tier`` (light/standard/max,
    retro-opps T003) computed at read time from the project config —
    identical to the CLI ``show``/``next`` value for the same task."""
    state_dir = _resolve_state_dir()
    backend = _open_backend(state_dir)
    try:
        task = backend.get_task(task_id)
        if task is None:
            raise ToolError(
                f"Task '{task_id}' not found.",
            )
        from anvil.planning.scoring import review_tier

        data = json.loads(task.model_dump_json())
        data["review_tier"] = review_tier(
            task, config=_load_merged_config_optional(state_dir)
        )
        return data
    finally:
        backend.close()


# ---------------------------------------------------------------------------
# Tool 4: get_next_task
# ---------------------------------------------------------------------------


@mcp.tool
def get_next_task(
    actor: str | None = None,
    prd_id: str | None = None,
    max_blast: int | None = None,
    max_review_risk: int | None = None,
) -> GetNextTaskResponse:
    """Return the single highest-priority ready task that has no overlapping
    active claim, plus the complete offer-governor calculation. ``task`` is
    null when no task is claimable; ``governor.withheld_reason`` distinguishes
    throttling from an empty queue.

    Ordering: critical > high > medium > low; then complexity asc, creation
    time asc, and id asc.

    ``max_blast`` / ``max_review_risk`` (B45/#56) are optional risk-axis ceilings:
    when set, a task is only offered if that dimension is CONFIRMED and within
    the ceiling — so a weak/local runner can declare a ceiling and never be
    handed high-risk work. This uses the SAME
    :func:`anvil.claims.manager.within_risk_ceiling` helper as the CLI
    ``ClaimManager.next_claimable``, so the two seams cannot diverge.

    ``prd_id`` (T019) scopes the CANDIDATE pool to one PRD partition while the
    exclusion sets (active claims, done-deps, active conflict groups) still span
    ALL PRDs — cross-PRD coordination, same contract as ``next --prd`` / the CLI
    ``ClaimManager.next_claimable(prd_id=...)``. ``None`` keeps the all-PRDs
    behaviour. ``actor`` selects the accept-rate history used by the governor.
    """
    state_dir = _resolve_state_dir()
    backend = _open_backend(state_dir)
    try:
        _reap_stale(backend)

        # T019: resolve which PRD to scope candidates to (explicit > $ANVIL_PRD;
        # None when neither names one -> all PRDs, byte-identical to pre-T019).
        # Collapse the default sentinel ('prd') so prd_id='prd' matches tasks
        # stored with prd_id='default' rather than narrowing to an empty pool.
        from anvil.claims.manager import ClaimManager
        from anvil.claims.metrics import AcceptRateMetrics
        from anvil.cli._helpers import canonical_prd_id, resolve_actor
        from anvil.clock import SystemClock

        resolved_actor = resolve_actor(actor)
        clock = SystemClock()
        cfg = _load_merged_config_optional(state_dir)
        metrics = AcceptRateMetrics(
            backend,
            clock,
            window_days=cfg.accept_rate_window_days if cfg is not None else 7.0,
            floor=cfg.accept_rate_floor if cfg is not None else 0.80,
            needs_review_cap=cfg.needs_review_cap if cfg is not None else 10,
            as_of=clock.now(),
        )

        scoped_prd_id = (
            canonical_prd_id(_resolve_prd_id(backend, prd_id)) if prd_id else None
        )

        manager = ClaimManager(backend, clock, actor=resolved_actor)
        diagnosis = manager.diagnose_next_offer(
            max_blast=max_blast,
            max_review_risk=max_review_risk,
            metrics=metrics,
            prd_id=scoped_prd_id,
        )
        best = diagnosis.task
        if best is None:
            projection = metrics.projection(
                resolved_actor, task_id=diagnosis.governor_task_id
            )
            projection.update(
                withheld_reason=diagnosis.withheld_reason,
                offer_throttled=diagnosis.withheld_reason
                in {
                    "review_queue_saturated",
                    "actor_below_floor",
                    "task_accept_rate_floor",
                },
            )
            return GetNextTaskResponse(
                task=None,
                governor=GovernorProjectionResponse.model_validate(projection),
                actor_identity=actor_identity_data(resolved_actor),
            )

        # retro-opps T003 — derived review tier, same computation as the CLI
        # `next` (identical value for the same task + config).
        from anvil.planning.scoring import review_tier

        data = json.loads(best.model_dump_json())
        data["review_tier"] = review_tier(
            best, config=_load_merged_config_optional(state_dir)
        )
        # retro-opps T009 — advisory collision visibility, same
        # ClaimManager.check_conflicts seam as the CLI `next` (read-only).
        conflict_warnings: list[dict[str, Any]] = []
        if best.likely_files:
            conflict_warnings = [
                {
                    "claim_id": w.other_claim_id,
                    "actor": w.other_actor,
                    "files": list(w.overlapping_files),
                }
                for w in manager.check_conflicts(
                    best.id, list(best.likely_files)
                )
            ]
        data["conflict_warnings"] = conflict_warnings
        projection = metrics.projection(resolved_actor, task_id=best.id)
        projection.update(withheld_reason=None, offer_throttled=False)
        return GetNextTaskResponse(
            task=data,
            governor=GovernorProjectionResponse.model_validate(projection),
            actor_identity=actor_identity_data(resolved_actor),
        )
    finally:
        backend.close()


# ---------------------------------------------------------------------------
# Tool 5: claim_task
# ---------------------------------------------------------------------------


@mcp.tool
def claim_task(
    task_id: str,
    claimed_by: str,
    expected_files: list[str] | None = None,
    lease_duration_seconds: int = 900,
    shared_tree: bool = False,
    cwd: str | None = None,
) -> ClaimResponse:
    """Acquire an exclusive lease on task_id for claimed_by.

    Reaps stale claims first; refuses (ToolError) unless the task's OWNING PRD
    is reviewed/approved (enforced by ClaimManager's per-PRD gate, T011/T012).
    lease_duration_seconds defaults to 900 (15 min).

    Honors the worktree_isolation policy (config.yaml): under ``require`` this
    tool REFUSES unless shared_tree=true — the MCP server cannot create git
    worktrees, so an isolated claim must go through ``anvil claim`` (CLI);
    shared_tree=true acknowledges a deliberately shared-checkout claim
    (read-only/docs work). Under ``advisory`` a shared-checkout collision
    warning is returned in the response.
    """
    # ClaimManager owns new-identity canonicalization. Passing the exact input
    # preserves actor_input for SQLite's locked normalized-collision check.
    state_dir = _resolve_state_dir(cwd)
    backend = _open_backend(state_dir)
    try:
        from anvil.claims.manager import ClaimError, ClaimManager
        from anvil.clock import SystemClock
        from anvil.git_ops import (
            ClaimGitMutationTracker,
            ClaimPlanError,
            apply_claim_plan,
            claim_git_metadata,
            compensate_claim_plan_tracker,
            finalize_claim_plan_tracker,
            resolve_claim_plan,
            revalidate_claim_plan,
        )

        _reap_stale(backend)

        # worktree_isolation parity with the CLI (review finding: the policy
        # lived only in cli/claim.py, so MCP claims silently bypassed it).
        from anvil.cli._helpers import _load_config_optional, _resolve_project_dir

        cfg = _load_config_optional(state_dir)
        isolation = cfg.worktree_isolation if cfg is not None else "advisory"
        isolation_warnings: list[str] = []
        if not shared_tree:
            if isolation == "require":
                raise ToolError(
                    "worktree_isolation: require — this MCP tool cannot "
                    "create git worktrees. Claim via the CLI (`anvil claim "
                    f"{task_id} --worktree`), or pass shared_tree=true to "
                    "deliberately claim into the shared checkout "
                    "(read-only/docs work)."
                )
            if isolation == "advisory":
                shared_active = [
                    c for c in backend.list_active_claims()
                    if not c.worktree_path
                ]
                if shared_active:
                    others = ", ".join(
                        f"{c.task_id} ({c.claimed_by})"
                        for c in shared_active[:4]
                    )
                    isolation_warnings.append(
                        f"{len(shared_active)} other active claim(s) share "
                        f"this checkout ({others}) — concurrent edits can "
                        "collide; prefer `anvil claim --worktree` (CLI)."
                    )

        # The PRD gate is enforced inside ClaimManager.claim() via
        # get_prd_for_task (T011/T012): the task's OWNING PRD must be reviewed or
        # approved. Its ClaimError is translated to ToolError below, so the MCP
        # and CLI paths apply the IDENTICAL per-PRD gate. (A duplicated inline
        # pre-check on the global get_prd() lived here pre-T012; it resolved the
        # default PRD and so disagreed with the per-PRD gate under multi-PRD.)
        lease_minutes = max(1, lease_duration_seconds // 60)
        project_dir = _resolve_project_dir(Path(cwd) if cwd else None)
        manager = ClaimManager(
            backend,
            SystemClock(),
            actor=claimed_by,
            default_lease_minutes=lease_minutes,
            project_root=project_dir,
        )

        files = expected_files or []
        task = backend.get_task(task_id)
        if task is None:
            raise ToolError(f"Task '{task_id}' not found.")

        try:
            plan = resolve_claim_plan(
                task_id,
                task.title,
                cwd=project_dir,
                branch_prefix=cfg.branch_prefix if cfg is not None else "agent",
                shared_tree=shared_tree,
                ignored_worktree_paths=(state_dir,),
            )
            metadata = claim_git_metadata(plan)
            mutation_tracker = ClaimGitMutationTracker(plan)
            with backend.claim_operation_lock():
                revalidate_claim_plan(plan, cwd=project_dir)
                result = manager.claim(
                    task_id,
                    expected_files=files,
                    branch=metadata.branch if metadata is not None else None,
                    worktree_path=(
                        metadata.worktree_path if metadata is not None else None
                    ),
                    git_metadata=metadata,
                    operation_locked=True,
                )
                try:
                    apply_claim_plan(plan, cwd=project_dir, tracker=mutation_tracker)
                except BaseException:
                    try:
                        manager.release(
                            result.claim.id,
                            reason="transactional Git claim failed",
                        )
                    finally:
                        compensate_claim_plan_tracker(
                            mutation_tracker, cwd=project_dir
                        )
                    raise
                finalize_claim_plan_tracker(
                    mutation_tracker, cwd=project_dir
                )
        except ClaimPlanError as exc:
            raise ToolError(f"{exc.code}: {exc}") from exc
        except ClaimError as exc:
            raise ToolError(str(exc)) from exc

        claim = result.claim
        if claim.attestation_context is None:
            isolation_warnings.append(
                "Progress attestation unavailable: this project is not an "
                "accessible Git repository; legacy file-change renewal remains available."
            )
        return ClaimResponse(
            id=claim.id,
            task_id=claim.task_id,
            claimed_by=claim.claimed_by,
            lease_expires_at=claim.lease_expires_at.isoformat(),
            branch=claim.branch,
            worktree_path=claim.worktree_path,
            git_metadata=(
                claim.git_metadata.model_dump(mode="json")
                if claim.git_metadata is not None
                else None
            ),
            expected_files=claim.expected_files,
            warnings=isolation_warnings,
            actor_identity=actor_identity_data(claim.claimed_by),
            continuation=continuation_data(
                task_id,
                claim.id,
                claim.claimed_by,
                attestation_context=(
                    claim.attestation_context.model_dump(mode="json")
                    if claim.attestation_context is not None
                    else None
                ),
                generation=claim.generation,
            ),
            generation=claim.generation,
            attestation_context=(
                claim.attestation_context.model_dump(mode="json")
                if claim.attestation_context is not None
                else None
            ),
        )
    finally:
        backend.close()


# ---------------------------------------------------------------------------
# Tool 6: release_task
# ---------------------------------------------------------------------------


@mcp.tool
def release_task(
    task_id: str,
    actor: str,
    reason: str | None = None,
    target_kind: Literal["task", "bundle"] = "task",
    cwd: str | None = None,
) -> ReleaseResponse:
    """Release a task claim, or an explicit target_kind=bundle coordinator claim."""
    actor = _exact_lifecycle_actor(actor)
    state_dir = _resolve_state_dir(cwd)
    backend = _open_backend(state_dir)
    try:
        from anvil.claims.manager import ClaimError, ClaimManager
        from anvil.clock import SystemClock

        _reap_stale(backend)

        if target_kind == "bundle":
            from anvil.bundles.manager import BundleError

            claim = backend.get_bundle_claim(task_id)
            if claim is None or claim.status.value != "active":
                raise ToolError(f"No active bundle claim found for '{task_id}'.")
            try:
                _bundle_manager(backend, state_dir, actor, cwd=cwd).release(
                    task_id, reason=reason
                )
            except BundleError as exc:
                raise ToolError(f"bundle_error: {exc}") from exc
            return ReleaseResponse(
                released=True,
                claim_id=claim.id,
                actor_identity=actor_identity_data(actor),
            )

        active_claim = _find_active_claim_for_task(backend, task_id)
        if active_claim is None:
            raise ToolError(
                f"No active claim found for task '{task_id}'. "
                "The task may already be released or was never claimed.",
            )

        if active_claim.claimed_by != actor:
            raise _actor_mismatch_tool_error(
                owner=active_claim.claimed_by, actual=actor, action="release the claim"
            )

        manager = ClaimManager(
            backend,
            SystemClock(),
            actor=actor,
        )

        try:
            manager.release(active_claim.id, reason=reason)
        except ClaimError as exc:
            raise ToolError(str(exc)) from exc

        return ReleaseResponse(
            released=True,
            claim_id=active_claim.id,
            actor_identity=actor_identity_data(actor),
        )
    finally:
        backend.close()


# ---------------------------------------------------------------------------
# Tool 7: renew_claim
# ---------------------------------------------------------------------------


@mcp.tool
def renew_claim(
    task_id: str,
    actor: str,
    extend_seconds: int = 900,
    target_kind: Literal["task", "bundle"] = "task",
    cwd: str | None = None,
) -> RenewResponse:
    """Renew a task claim, or an explicit target_kind=bundle coordinator claim."""
    actor = _exact_lifecycle_actor(actor)
    state_dir = _resolve_state_dir(cwd)
    backend = _open_backend(state_dir)
    try:
        from anvil.claims.manager import ClaimError, ClaimManager
        from anvil.clock import SystemClock

        _reap_stale(backend)

        if target_kind == "bundle":
            from anvil.bundles.manager import BundleError

            claim = backend.get_bundle_claim(task_id)
            if claim is None or claim.status.value != "active":
                raise ToolError(f"No active bundle claim found for '{task_id}'.")
            try:
                from anvil.clock import SystemClock

                remaining_seconds = max(
                    0.0,
                    (claim.lease_expires_at - SystemClock().now()).total_seconds(),
                )
                updated = _bundle_manager(
                    backend,
                    state_dir,
                    actor,
                    lease_minutes=max(
                        1.0, (remaining_seconds + extend_seconds) / 60.0
                    ),
                    cwd=cwd,
                ).renew(task_id)
            except BundleError as exc:
                raise ToolError(f"bundle_error: {exc}") from exc
            return RenewResponse(
                lease_expires_at=updated.lease_expires_at.isoformat(),
                renewed=updated.lease_expires_at != claim.lease_expires_at,
                actor_identity=actor_identity_data(actor),
            )

        active_claim = _find_active_claim_for_task(backend, task_id)
        if active_claim is None:
            raise ToolError(
                f"No active claim found for task '{task_id}'. "
                "The task may have been released or its lease may have expired.",
            )

        if active_claim.claimed_by != actor:
            raise _actor_mismatch_tool_error(
                owner=active_claim.claimed_by, actual=actor, action="renew the claim"
            )

        lease_minutes = max(1, extend_seconds // 60)
        manager = ClaimManager(
            backend,
            SystemClock(),
            actor=actor,
            default_lease_minutes=lease_minutes,
        )

        try:
            renewal = manager.renew_with_result(active_claim.id)
        except ClaimError as exc:
            raise ToolError(str(exc)) from exc

        # B46 part 2 — a no-progress renew is a no-op (lease unchanged). Surface
        # whether the lease actually advanced so an MCP client can tell a real
        # renewal from a declined one instead of trusting a stale expiry.
        return RenewResponse(
            lease_expires_at=renewal.claim.lease_expires_at.isoformat(),
            renewed=renewal.renewed,
            actor_identity=actor_identity_data(actor),
            progress=RenewProgressReceipt(
                source=renewal.progress_source,
                digest=renewal.attestation_digest,
                generation=renewal.attestation_generation,
                trust_mode=renewal.attestation_trust_mode,
            ),
        )
    finally:
        backend.close()


# ---------------------------------------------------------------------------
# Tool 8: generate_work_packet
# ---------------------------------------------------------------------------


@mcp.tool
def generate_work_packet(
    task_id: str,
    format: Literal["markdown", "json"] = "markdown",
) -> WorkPacketResponse:
    """Render the work packet for task_id (task brief, dependencies, prior
    findings) as markdown or JSON."""
    state_dir = _resolve_state_dir()
    backend = _open_backend(state_dir)
    try:
        from anvil.context.packets import (
            fast_lane_packet,
            relevant_assumptions,
            render_packet,
        )
        from anvil.state.models import Task

        task = backend.get_task(task_id)
        if task is None:
            raise ToolError(f"Task '{task_id}' not found.")

        feature = backend.get_feature(task.feature_id)
        task_assumptions = relevant_assumptions(
            backend.get_prd_for_task(task), feature
        )

        dependencies_completed: list[Task] = []
        dependencies_open: list[Task] = []
        for dep_id in task.dependencies:
            dep = backend.get_task(dep_id)
            if dep is None:
                continue
            if dep.status.value == "done":
                dependencies_completed.append(dep)
            else:
                dependencies_open.append(dep)

        active_claim = _find_active_claim_for_task(backend, task_id)

        # T017 — surface prior deferred / failed-review findings whose files
        # overlap this task's files (the active claim's expected_files when
        # claimed, else the planner's likely_files hint).
        from anvil.review.gates import deferred_findings_for_files

        overlap_files = (
            active_claim.expected_files
            if active_claim is not None and active_claim.expected_files
            else task.likely_files
        )
        deferred = deferred_findings_for_files(
            backend.list_reviews(),
            backend.list_tasks(),
            backend.list_evidence(),
            overlap_files,
        )

        # T020 — route the fast-lane from the project's config thresholds when a
        # config can be loaded; fall back to the renderer's built-in defaults
        # otherwise. A broken config never blocks packet generation.
        cfg = _load_fast_lane_config(state_dir)
        if cfg is not None:
            packet = fast_lane_packet(
                task,
                cfg,
                feature=feature,
                dependencies_completed=dependencies_completed,
                dependencies_open=dependencies_open,
                related_decisions=None,
                active_claim=active_claim,
                assumptions=task_assumptions,
                deferred_findings=deferred,
            )
        else:
            packet = render_packet(
                task,
                feature=feature,
                dependencies_completed=dependencies_completed,
                dependencies_open=dependencies_open,
                related_decisions=None,
                active_claim=active_claim,
                assumptions=task_assumptions,
                deferred_findings=deferred,
            )

        if format == "json":
            return WorkPacketResponse(format="json", content=packet.json_data)
        return WorkPacketResponse(format="markdown", content=packet.markdown)
    finally:
        backend.close()


# ---------------------------------------------------------------------------
# Tool 9: submit_progress
# ---------------------------------------------------------------------------


@mcp.tool
def submit_progress(
    task_id: str,
    actor: str,
    notes: str | None = None,
    phase: str | None = None,
    detail: str | None = None,
    attestation_base64: str | None = None,
    cwd: str | None = None,
) -> ProgressResponse:
    """Record a progress note for task_id as a 'progress.noted' audit event.
    Does NOT change task status. Reaps stale claims first.

    ``phase`` (retro-opps T010) is an optional structured label ("build",
    "tests", "review-fixes", ...) for the heartbeat bus — status surfaces
    read the latest phase back so operators can see where a long run is
    without asking. ``detail`` is free-text elaboration for the phase."""
    actor = _exact_lifecycle_actor(actor)
    state_dir = _resolve_state_dir(cwd)
    backend = _open_backend(state_dir)
    try:
        from anvil.clock import SystemClock
        from anvil.state.models import EventDraft

        _reap_stale(backend)

        task = backend.get_task(task_id)
        if task is None:
            raise ToolError(f"Task '{task_id}' not found.")

        active_claim = _find_active_claim_for_task(backend, task_id)
        if active_claim is not None and active_claim.claimed_by != actor:
            raise _actor_mismatch_tool_error(
                owner=active_claim.claimed_by,
                actual=actor,
                action="record progress for the claim",
            )
        if active_claim is None:
            actor = _require_actor(actor)

        if attestation_base64 is not None:
            from anvil import signing
            from anvil.claims.manager import ClaimError, ClaimManager
            from anvil.claims.progress_attestation import (
                ProgressAttestationError,
                load_progress_attestation_base64,
            )
            from anvil.cli._helpers import _resolve_project_dir
            from anvil.cli.proof import _default_trust_path

            try:
                loaded = load_progress_attestation_base64(
                    attestation_base64,
                    trusted_issuers=signing.load_trust_list(_default_trust_path()),
                )
                if loaded.payload.task_id != task_id:
                    raise ProgressAttestationError(
                        "task_mismatch",
                        "attestation task does not match the progress command",
                    )
                persisted = ClaimManager(
                    backend,
                    SystemClock(),
                    actor=actor,
                    project_root=_resolve_project_dir(Path(cwd) if cwd else None),
                ).accept_progress_attestation(loaded)
            except (ClaimError, ProgressAttestationError) as exc:
                raise ToolError(
                    f"progress_attestation_error[{getattr(exc, 'code', 'rejected')}]: {exc}"
                ) from exc
            return ProgressResponse(
                recorded=True,
                actor_identity=actor_identity_data(actor),
                event_action="progress.attested",
                attestation=ProgressAttestationReceipt(
                    digest=persisted.semantic_digest,
                    generation=persisted.generation,
                    trust_mode=persisted.trust_mode,
                    kind=persisted.kind,
                    issuer_id=persisted.issuer_id,
                ),
            )

        if notes is None:
            raise ToolError("notes is required when attestation_base64 is not provided")

        clock = SystemClock()
        now = clock.now()

        draft = EventDraft(
            timestamp=now,
            actor=actor,
            action="progress.noted",
            target_kind="task",
            target_id=task_id,
            payload_json={
                "task_id": task_id,
                "actor": actor,
                "notes": notes,
                "noted_at": now.isoformat(),
                # T010 — omit the keys when unused: a no-phase row stays
                # byte-identical to the pre-T010 shape, so an OLDER anvil
                # sharing the same HOME-workspace state dir (installed
                # plugin vs dev checkout) only hard-fails on rows that
                # genuinely exercise the feature, not on every progress
                # note (extra='forbid' on its payload model).
                **({"phase": phase} if phase is not None else {}),
                **({"detail": detail} if detail is not None else {}),
            },
        )
        backend.append(draft)
        return ProgressResponse(
            recorded=True, actor_identity=actor_identity_data(actor)
        )
    finally:
        backend.close()


# ---------------------------------------------------------------------------
# Tool 10: submit_completion_evidence
# ---------------------------------------------------------------------------


@mcp.tool
def submit_completion_evidence(
    task_id: str,
    actor: str,
    commands_run: list[str],
    files_changed: list[str],
    output_excerpt: str | None = None,
    pr_url: str | None = None,
    commit_sha: str | None = None,
    category: str | None = None,
    command_proof_artifacts_base64: list[str] | None = None,
    cwd: str | None = None,
) -> EvidenceResponse:
    """Submit completion evidence for task_id (requires an active claim held by
    actor). Auto-releases the claim and moves the task to needs_review; names
    the next claimable task. Reaps stale claims first.

    ``category`` (evidence-contracts T006) is the evidence role: completion
    (default), diagnostic, blocked, advisory, or promotion_quality —
    diagnostic/advisory evidence can never satisfy a completion claim."""
    if category is not None:
        from anvil.state.models import EvidenceCategory

        valid = [c.value for c in EvidenceCategory]
        if category not in valid:
            raise ToolError(
                f"invalid_category: {category!r} is not a valid evidence "
                f"category; valid values: {', '.join(valid)}."
            )
    actor = _exact_lifecycle_actor(actor)
    state_dir = _resolve_state_dir(cwd)
    backend = _open_backend(state_dir)
    try:
        from anvil.cli.packet_apply import _read_command_proofs
        from anvil.clock import SystemClock
        from anvil.state.backend import EventRejected
        from anvil.state.models import EventDraft

        _reap_stale(backend)

        task = backend.get_task(task_id)
        if task is None:
            raise ToolError(f"Task '{task_id}' not found.")

        active_claim = _find_active_claim_for_task(backend, task_id)
        if active_claim is None:
            raise ToolError(
                f"No active claim found for task '{task_id}'. "
                "Claim the task first before submitting evidence.",
            )

        # Enforce actor ownership — only the claim owner may submit evidence.
        # Without this guard any MCP caller can force-complete another agent's
        # claim by passing a different actor name (caught by critic-PR#45-P1).
        if active_claim.claimed_by != actor:
            raise _actor_mismatch_tool_error(
                owner=active_claim.claimed_by,
                actual=actor,
                action="submit completion evidence",
            )

        clock = SystemClock()

        claim_bound_proofs: tuple[ClaimCommandProof, ...] = ()
        loaded_claim_proofs: list[LoadedClaimCommandProof] = []
        proof_project = None
        proof_project_root = None
        if command_proof_artifacts_base64:
            from anvil import signing
            from anvil.claims.command_proof_artifact import (
                ClaimCommandProofError,
                load_claim_command_proof_base64,
                verify_claim_command_proof_batch,
            )
            from anvil.cli._helpers import _resolve_project_dir
            from anvil.cli.proof import _default_trust_path
            from anvil.state.models import (
                MAX_CLAIM_COMMAND_PROOF_BATCH_BYTES,
                MAX_CLAIM_COMMAND_PROOF_BATCH_ITEMS,
            )

            try:
                # Bound the public list before decoding any element. The
                # conservative encoded cap permits base64 padding for every
                # allowed item; exact decoded aggregate size is rechecked by
                # verify_claim_command_proof_batch and the state handler.
                artifact_count = len(command_proof_artifacts_base64)
                if artifact_count > MAX_CLAIM_COMMAND_PROOF_BATCH_ITEMS:
                    raise ClaimCommandProofError(
                        "batch_size",
                        "command proof batch is outside limits",
                    )
                max_encoded_batch = (
                    (MAX_CLAIM_COMMAND_PROOF_BATCH_BYTES + 2 * artifact_count + 2)
                    // 3
                ) * 4
                if (
                    sum(len(artifact) for artifact in command_proof_artifacts_base64)
                    > max_encoded_batch
                ):
                    raise ClaimCommandProofError(
                        "batch_too_large",
                        "command proof batch exceeds its byte limit",
                    )
                trusted = signing.load_trust_list(_default_trust_path())
                loaded_claim_proofs = [
                    load_claim_command_proof_base64(
                        artifact, trusted_issuers=trusted
                    )
                    for artifact in command_proof_artifacts_base64
                ]
                proof_project = backend.get_project()
                proof_project_root = _resolve_project_dir(
                    Path(cwd) if cwd else None
                )
                if proof_project is None:
                    raise ClaimCommandProofError(
                        "context_missing",
                        "command proof requires an initialized project",
                    )
            except ClaimCommandProofError as exc:
                raise ToolError(
                    f"command_proof_error[{exc.code}]: {exc}"
                ) from exc

        evidence_id = "EV" + uuid.uuid4().hex[:8].upper()

        # SL-3 / B48: reconcile the per-claim evidence buffer (real exit codes
        # the PostToolUse hook observed) into typed CommandProofs, so an
        # MCP-driven submit carries the same observed proofs as the CLI path.
        command_proofs = _read_command_proofs(state_dir, active_claim.id)

        # Refresh after every artifact/buffer read, then revalidate the exact
        # lease window immediately before drafting. SQLite repeats this check
        # against its authoritative clock while holding the append lock.
        now = clock.now()
        if command_proof_artifacts_base64:
            if proof_project is None or proof_project_root is None:
                raise ToolError("command_proof_error[context_missing]: project context missing")
            try:
                claim_bound_proofs = verify_claim_command_proof_batch(
                    loaded_claim_proofs,
                    claim=active_claim,
                    task=task,
                    project_id=proof_project.id,
                    project_root=proof_project_root,
                    actor=actor,
                    declared_commands=commands_run,
                    now=now,
                )
            except ClaimCommandProofError as exc:
                raise ToolError(
                    f"command_proof_error[{exc.code}]: {exc}"
                ) from exc

        draft = EventDraft(
            timestamp=now,
            actor=actor,
            action="evidence.submitted",
            target_kind="task",
            target_id=task_id,
            payload_json={
                "task_id": task_id,
                "claim_id": active_claim.id,
                "submitted_by": actor,
                "evidence_id": evidence_id,
                # T006 — omit-when-default keeps the pre-v9 byte shape.
                **(
                    {"category": category}
                    if category and category != "completion"
                    else {}
                ),
                "commands_run": commands_run,
                "files_changed": files_changed,
                "output_excerpt": output_excerpt,
                "pr_url": pr_url,
                "commit_sha": commit_sha,
                "screenshots": [],
                "known_limitations": None,
                "proofs": [
                    p.model_dump(mode="json")
                    for p in [*command_proofs, *claim_bound_proofs]
                ],
            },
        )

        try:
            backend.append(draft)
        except EventRejected as exc:
            raise ToolError(str(exc)) from exc

        fresh_task = backend.get_task(task_id)
        task_status = fresh_task.status.value if fresh_task is not None else "needs_review"
        missing_claim_bound_proofs: list[str] = []
        missing_legacy_evidence: list[str] = []
        evidence_obj = backend.get_latest_evidence(task_id)
        if fresh_task is not None and evidence_obj is not None:
            from anvil.review.gates import evidence_missing_details

            missing_legacy_evidence, missing_claim_bound_proofs = (
                evidence_missing_details(fresh_task, evidence_obj)
            )

        # T014: name the next claimable task now that this one has left the
        # active set. The submitting actor's own (now-released) claim is
        # excluded from file-conflict checks, so a follow-on task touching the
        # same files this agent just finished is still eligible.
        next_ready_raw = _compute_next_ready(backend, actor)
        next_ready = (
            NextReadyTask(**next_ready_raw) if next_ready_raw is not None else None
        )

        hook_command_proof_receipts: list[dict[str, object]] = []
        for proof in command_proofs:
            attribution = proof.attribution
            if attribution is None or proof.semantic_digest is None:
                raise ToolError(
                    "hook_command_proof_error[attribution_missing]: "
                    "claim-bound hook proof attribution is missing"
                )
            hook_command_proof_receipts.append(
                {
                    "command": proof.command,
                    "exit_code": proof.exit_code,
                    "output_sha256": proof.output_sha256,
                    "captured_at": proof.captured_at.isoformat(),
                    "source": "hook_claim_bound",
                    "claim_id": attribution.claim_id,
                    "generation": attribution.generation,
                    "semantic_digest": proof.semantic_digest,
                    "actor": attribution.claimed_by,
                }
            )

        return EvidenceResponse(
            evidence_id=evidence_id,
            task_status=task_status,
            next_ready=next_ready,
            actor_identity=actor_identity_data(actor),
            claim_bound_command_proofs=[
                {
                    "digest": proof.semantic_digest,
                    "generation": proof.evidence_core.generation,
                    "trust_mode": proof.trust_mode,
                    "issuer_id": proof.issuer_id,
                    "command": proof.command,
                    "output_sha256": proof.output_sha256,
                }
                for proof in claim_bound_proofs
            ],
            hook_command_proofs=hook_command_proof_receipts,
            missing_claim_bound_proofs=missing_claim_bound_proofs,
            missing_legacy_evidence=missing_legacy_evidence,
        )
    finally:
        backend.close()


# ---------------------------------------------------------------------------
# Tool 11: check_conflicts
# ---------------------------------------------------------------------------


@mcp.tool
def check_conflicts(
    task_id: str,
    proposed_files: list[str],
) -> ConflictCheckResponse:
    """Check proposed_files against active claims (excluding task_id's own),
    returning one conflict entry per overlapping file per claim. Empty list
    means no conflicts."""
    state_dir = _resolve_state_dir()
    backend = _open_backend(state_dir)
    try:
        proposed_set = set(proposed_files)
        active_claims = backend.list_active_claims()

        conflicts: list[ConflictEntry] = []
        for claim in active_claims:
            # Skip this task's own claim.
            if claim.task_id == task_id:
                continue
            overlap = proposed_set & set(claim.expected_files)
            for file in sorted(overlap):
                conflicts.append(
                    ConflictEntry(
                        file=file,
                        claim_id=claim.id,
                        claimed_by=claim.claimed_by,
                        task_id=claim.task_id,
                    )
                )

        return ConflictCheckResponse(conflicts=conflicts)
    finally:
        backend.close()


# ---------------------------------------------------------------------------
# Tool 12: get_dependency_graph
# ---------------------------------------------------------------------------


@mcp.tool
def get_dependency_graph(
    scope: Literal["all", "feature", "task"] = "all",
    target_id: str | None = None,
) -> DependencyGraphResponse:
    """Return the task dependency graph (nodes, edges, ready_to_claim).

    scope='all' is the whole project; 'feature' is one feature's tasks; 'task'
    is the target plus its transitive deps (target_id required for the latter
    two). ready_to_claim = ready tasks with all deps done and no active claim.
    """
    state_dir = _resolve_state_dir()
    backend = _open_backend(state_dir)
    try:
        all_tasks = backend.list_tasks()
        task_map = {t.id: t for t in all_tasks}
        active_claims = backend.list_active_claims()
        claimed_task_ids = {c.task_id for c in active_claims}
        done_task_ids = {t.id for t in all_tasks if t.status.value == "done"}

        # Determine which tasks are in scope.
        if scope == "all":
            scoped_tasks = all_tasks
        elif scope == "feature":
            if target_id is None:
                raise ToolError(
                    "target_id is required when scope='feature'."
                )
            scoped_tasks = [t for t in all_tasks if t.feature_id == target_id]
        elif scope == "task":
            if target_id is None:
                raise ToolError(
                    "target_id is required when scope='task'."
                )
            # Collect the target task plus all its transitive dependencies.
            visited: set[str] = set()
            queue = [target_id]
            while queue:
                tid = queue.pop()
                if tid in visited:
                    continue
                visited.add(tid)
                t = task_map.get(tid)
                if t is None:
                    continue
                for dep_id in t.dependencies:
                    if dep_id not in visited:
                        queue.append(dep_id)
            scoped_tasks = [task_map[tid] for tid in visited if tid in task_map]
        else:
            scoped_tasks = all_tasks

        scoped_ids = {t.id for t in scoped_tasks}

        nodes = [
            DependencyNode(
                id=t.id,
                title=t.title,
                status=t.status.value,
                priority=t.priority.value,
                feature_id=t.feature_id,
            )
            for t in scoped_tasks
        ]

        # Edges: dependency relationships within scope.
        edges = []
        for t in scoped_tasks:
            for dep_id in t.dependencies:
                if dep_id in scoped_ids:
                    edges.append(
                        DependencyEdge(
                            **{"from": dep_id, "to": t.id}
                        )
                    )

        # ready_to_claim: ready tasks with all deps done and no active claim.
        ready_to_claim = []
        for t in scoped_tasks:
            if t.status.value != "ready":
                continue
            if t.id in claimed_task_ids:
                continue
            if any(dep_id not in done_task_ids for dep_id in t.dependencies):
                continue
            ready_to_claim.append(t.id)

        return DependencyGraphResponse(
            nodes=nodes,
            edges=edges,
            ready_to_claim=sorted(ready_to_claim),
        )
    finally:
        backend.close()


# ---------------------------------------------------------------------------
# Tool 12b: edit_dependencies — batch dependency-edit primitive (T022/F007)
# ---------------------------------------------------------------------------


@mcp.tool(tags={PLANNING_TAG})
def edit_dependencies(
    actor: str,
    add: DependencyEdgesInput = None,
    remove: DependencyEdgesInput = None,
    prd_id: str | None = None,
    cwd: str | None = None,
) -> EditDependenciesResponse:
    """Apply dependency edits after whole-batch validation.

    ``add`` / ``remove`` are ``[source, target]`` pairs meaning *source depends
    on target*. The whole batch is validated up front: any unknown task,
    self-dependency, or cycle rejects the ENTIRE batch (ToolError) before any
    mutation. ``prd_id`` explicitly selects the owning PRD (or resolves the
    single/default PRD when omitted). Changed tasks are persisted by one atomic
    dependency-batch event; a no-op request emits no event. Inputs are manually
    shape-checked before state access and a request may contain at most 10,000
    edges. ``cwd`` selects the project root.
    """
    from anvil.clock import SystemClock
    from anvil.planning._plan_helpers import (
        DEPENDENCY_BATCH_LIMIT_MESSAGE,
        DEPENDENCY_EDGE_LIST_FORMAT_MESSAGE,
        DEPENDENCY_EVENT_REJECTED_MESSAGE,
        DEPENDENCY_PAIR_FORMAT_MESSAGE,
        MAX_DEPENDENCY_EDGES_PER_BATCH,
        BatchDepError,
        DepEdge,
        emit_batch_dep_events,
        plan_batch_dep_edits,
        validate_dep_source_owners,
    )
    from anvil.state.backend import EventRejected

    def _outer_list(value: Any) -> list[Any]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ToolError(DEPENDENCY_EDGE_LIST_FORMAT_MESSAGE)
        return value

    add_pairs = _outer_list(add)
    remove_pairs = _outer_list(remove)
    if not add_pairs and not remove_pairs:
        raise ToolError(
            "no edges supplied; pass at least one add or remove pair."
        )
    if len(add_pairs) + len(remove_pairs) > MAX_DEPENDENCY_EDGES_PER_BATCH:
        raise ToolError(DEPENDENCY_BATCH_LIMIT_MESSAGE)

    def _to_edges(pairs: list[Any], op: str) -> list[DepEdge]:
        out: list[DepEdge] = []
        for pair in pairs:
            if (
                not isinstance(pair, list)
                or len(pair) != 2
                or not isinstance(pair[0], str)
                or not isinstance(pair[1], str)
            ):
                raise ToolError(DEPENDENCY_PAIR_FORMAT_MESSAGE)
            out.append(DepEdge(op=op, source=pair[0], target=pair[1]))
        return out

    edges = _to_edges(add_pairs, "add") + _to_edges(remove_pairs, "remove")
    actor = _require_actor(actor)

    from anvil.cli._helpers import canonical_prd_id

    state_dir = _resolve_state_dir(cwd)
    backend = _open_backend(state_dir)
    try:
        clock = SystemClock()
        selected_prd_id = canonical_prd_id(_resolve_prd_id(backend, prd_id))
        if backend.get_prd(selected_prd_id) is None:
            raise ToolError(
                "selected PRD was not found in state. Call parse_prd first."
            )
        all_tasks = backend.list_tasks()
        tasks_by_id = {t.id: t for t in all_tasks}

        # Validate the WHOLE batch before emitting anything — a raised
        # BatchDepError here means zero events were appended (no partial apply).
        try:
            validate_dep_source_owners(
                tasks_by_id,
                edges,
                prd_id=selected_prd_id,
            )
            batch_plan = plan_batch_dep_edits(all_tasks, edges)
        except BatchDepError as exc:
            raise ToolError(exc.message) from None

        try:
            changed = emit_batch_dep_events(
                backend,
                tasks_by_id,
                batch_plan,
                prd_id=selected_prd_id,
                actor=actor,
                clock=clock,
            )
        except EventRejected:
            raise ToolError(DEPENDENCY_EVENT_REJECTED_MESSAGE) from None
        return EditDependenciesResponse(
            prd_id=selected_prd_id,
            changed=changed,
            added=[list(e) for e in batch_plan.added],
            removed=[list(e) for e in batch_plan.removed],
        )
    finally:
        backend.close()


# ---------------------------------------------------------------------------
# Tool 13: update_task_status
# ---------------------------------------------------------------------------


@mcp.tool
def update_task_status(
    task_id: str,
    to_status: Literal["drafted", "ready", "blocked", "in_progress"],
    actor: str,
    reason: str | None = None,
) -> StatusUpdateResponse:
    """Transition task_id to a new status. Only these moves are allowed
    (any other raises ToolError): drafted↔ready, in_progress/claimed→blocked,
    blocked→in_progress. Reaps stale claims first."""
    actor = _require_actor(actor)
    state_dir = _resolve_state_dir()
    backend = _open_backend(state_dir)
    try:
        from anvil.clock import SystemClock
        from anvil.state.backend import EventRejected
        from anvil.state.models import EventDraft

        _reap_stale(backend)

        task = backend.get_task(task_id)
        if task is None:
            raise ToolError(f"Task '{task_id}' not found.")

        from_status = task.status.value
        allowed_targets = _ALLOWED_STATUS_TRANSITIONS.get(from_status, set())

        if to_status not in allowed_targets:
            raise ToolError(
                f"Cannot transition task '{task_id}' from '{from_status}' to '{to_status}'. "
                f"Allowed targets from '{from_status}': {sorted(allowed_targets) or 'none'}. "
                "This tool supports only: drafted↔ready and blocked toggle.",
            )

        clock = SystemClock()
        now = clock.now()

        draft = EventDraft(
            timestamp=now,
            actor=actor,
            action="task.status_changed",
            target_kind="task",
            target_id=task_id,
            payload_json={
                "task_id": task_id,
                "from": from_status,
                "to": to_status,
                "reason": reason,
            },
        )

        try:
            backend.append(draft)
        except EventRejected as exc:
            raise ToolError(str(exc)) from exc

        return StatusUpdateResponse(from_status=from_status, to_status=to_status)
    finally:
        backend.close()


# ===========================================================================
# Workflow tools (init / PRD / plan / review / apply)
# ===========================================================================
#
# These complete the PRD → plan → review → approve → claim → apply lifecycle
# for non-Claude-Code MCP clients. Each mirrors the corresponding CLI handler
# via shared modules (no logic duplication), touches no git, and accepts an
# optional ``cwd`` to target a project root other than the server's launch dir.

_PRD_FILENAME = "prd.md"


# ---------------------------------------------------------------------------
# Tool 14: init_project
# ---------------------------------------------------------------------------


class InitProjectResponse(BaseModel):
    """Result of init_project."""

    model_config = ConfigDict(extra="forbid")

    project_id: str
    project_name: str
    state_dir: str
    created: bool
    # T019: the default PRD partition a freshly-scaffolded project owns. A new
    # project has no parsed PRD yet, so this is always the reserved default id —
    # the partition `parse_prd` / `plan_tasks` write into when no prd_id is named.
    # REQUIRED (no field default): init_project must set it explicitly, so a
    # regression that drops the assignment fails construction rather than being
    # masked by a field default that silently supplies 'default'.
    prd_id: str


@mcp.tool(tags={PLANNING_TAG})
def init_project(
    name: str | None = None,
    cwd: str | None = None,
) -> InitProjectResponse:
    """Scaffold a fresh .anvil/ state directory in the target project root.

    Creates the canonical layout (config.yaml, state.db, events.jsonl,
    packets/), seeds the project row, and emits project.created +
    state.initialized. Non-destructive: raises ToolError if .anvil/ already
    exists (use ``anvil init --force`` from the CLI to reinit) or inside the
    plugin root.

    Args:
        name: Project name. Defaults to the cwd basename.
        cwd:  Project root. Defaults to Path.cwd().
    """
    from anvil.cli._helpers import (
        _is_local_layout,
        _is_plugin_root,
        _resolve_base_dir,
        _slug,
    )
    from anvil.clock import SystemClock
    from anvil.config import write_default_config
    from anvil.state.models import EventDraft
    from anvil.state.sqlite import SqliteBackend

    # MUST-FIX 1: resolve the project root the SAME way reads do
    # (explicit cwd > ANVIL_ROOT > Path.cwd()), so init_project and
    # every read tool (get_project_status, etc.) agree on the project dir.
    base = _resolve_base_dir(Path(cwd) if cwd else None)

    # Plugin-root guard only under the legacy local layout (state would land
    # in-repo). In workspace layout init writes to ~/.anvil/... so this is moot
    # (B44: the guard checked the resolved HOME base, never a plugin root).
    if _is_local_layout() and _is_plugin_root(base):
        raise ToolError(
            f"Refusing to initialize anvil in {base}: this is the "
            "plugin root, not a project directory. Pass cwd= a project path.",
        )

    state_dir = base / _STATE_DIR_NAME
    if state_dir.exists():
        raise ToolError(
            f"{state_dir} already exists. Use the `anvil init --force` "
            "CLI command to reinitialize (MCP init_project is non-destructive).",
        )

    project_name = name if name else base.name
    project_id = _slug(project_name)

    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "packets").mkdir(exist_ok=True)
        (state_dir / "events.jsonl").touch()
        write_default_config(state_dir / "config.yaml", project_name=project_name)
    except (OSError, FileExistsError) as exc:
        raise ToolError(f"Failed to scaffold {state_dir}: {exc}") from exc

    backend = SqliteBackend(
        db_path=str(state_dir / "state.db"),
        events_path=str(state_dir / "events.jsonl"),
        clock=SystemClock(),
    )
    try:
        # initialize() must be inside try so a failure during schema
        # bootstrap still triggers backend.close() in the finally block.
        backend.initialize()
        now = SystemClock().now()
        backend.append(EventDraft(
            timestamp=now,
            actor="anvil-mcp",
            action="project.created",
            target_kind="project",
            target_id=project_id,
            payload_json={
                "id": project_id,
                "name": project_name,
                "description": "",
                "created_at": now.isoformat(),
                "updated_at": now.isoformat(),
            },
        ))
        backend.append(EventDraft(
            timestamp=now,
            actor="anvil-mcp",
            action="state.initialized",
            target_kind="project",
            target_id=project_id,
            payload_json={},
        ))
    finally:
        backend.close()

    from anvil.state.models import DEFAULT_PRD_ID

    return InitProjectResponse(
        project_id=project_id,
        project_name=project_name,
        state_dir=str(state_dir),
        created=True,
        prd_id=DEFAULT_PRD_ID,
    )


# ---------------------------------------------------------------------------
# Tool 15: get_project_status
# ---------------------------------------------------------------------------


class ProjectStatusResponse(BaseModel):
    """Result of get_project_status — a structured equivalent of
    ``anvil status``."""

    model_config = ConfigDict(extra="forbid")

    initialized: bool
    project_id: str | None
    project_name: str | None
    state_dir: str
    prd_status: str | None
    task_counts: TaskCountsByStatus
    total_tasks: int
    ready_queue_depth: int
    active_claim_count: int
    # T020: additive per-PRD rollup. Flat fields above remain the PROJECT TOTAL.
    prds: list[PrdStatusEntry] = Field(default_factory=list)
    bundles: list[BundleRollupEntry] = Field(default_factory=list)


@mcp.tool
def get_project_status(cwd: str | None = None) -> ProjectStatusResponse:
    """Return PRD status, task counts by state, active-claim count, and ready-
    queue depth. The canonical "am I bootstrapped?" probe: returns
    initialized=False with empty counts (no exception) when .anvil/ is absent.

    Args:
        cwd: Project root. Defaults to Path.cwd().
    """
    state_dir = _resolve_state_dir(cwd)
    empty_counts = TaskCountsByStatus()

    if not state_dir.exists():
        return ProjectStatusResponse(
            initialized=False,
            project_id=None,
            project_name=None,
            state_dir=str(state_dir),
            prd_status=None,
            task_counts=empty_counts,
            total_tasks=0,
            ready_queue_depth=0,
            active_claim_count=0,
        )

    backend = _open_backend(state_dir)
    try:
        project = backend.get_project()
        # T021 audit (get_prd no-arg): default-only-correct. ``prd_status`` is the
        # flat legacy field (the default PRD's status); per-PRD status lives in the
        # additive ``prds`` rollup below. Mirrors get_project_summary.
        prd = backend.get_prd()
        prds = backend.list_prds()
        all_tasks = backend.list_tasks()
        active_claims = backend.list_active_claims()

        counts = TaskCountsByStatus()
        ready_depth = 0
        for task in all_tasks:
            status_val = task.status.value
            if hasattr(counts, status_val):
                setattr(counts, status_val, getattr(counts, status_val) + 1)
            if status_val == "ready":
                ready_depth += 1

        return ProjectStatusResponse(
            initialized=True,
            project_id=project.id if project is not None else None,
            project_name=project.name if project is not None else None,
            state_dir=str(state_dir),
            prd_status=prd.status.value if prd is not None else None,
            task_counts=counts,
            total_tasks=len(all_tasks),
            ready_queue_depth=ready_depth,
            active_claim_count=len(active_claims),
            # T020: per-PRD rollup; flat fields above stay the project total.
            prds=_prd_status_entries(prds, all_tasks, active_claims),
            bundles=_bundle_status_entries(backend, all_tasks, active_claims),
        )
    finally:
        backend.close()


# ---------------------------------------------------------------------------
# Tool 16: parse_prd
# ---------------------------------------------------------------------------


class ParseErrorEntry(BaseModel):
    """One ParseError from the PRD parser."""

    model_config = ConfigDict(extra="forbid")

    section: str
    line: int
    message: str


class ParsePrdResponse(BaseModel):
    """Result of parse_prd."""

    model_config = ConfigDict(extra="forbid")

    prd_status: str
    requirement_count: int
    feature_count: int
    task_count: int
    errors: list[ParseErrorEntry]
    error_count: int = 0
    errors_shown: int = 0
    errors_omitted: int = 0
    errors_truncated: bool = False
    error_messages_truncated: int = 0
    prd_path: str


class BehavioralFindingResponse(BaseModel):
    """One deterministic, advisory behavioural-readiness finding."""

    model_config = ConfigDict(extra="forbid")

    id: str
    category: str
    severity: str
    location: str
    message: str
    challenge_question: str


class AssessPrdResponse(BaseModel):
    """Read-only PRD behavioural-readiness assessment."""

    model_config = ConfigDict(extra="forbid")

    prd_source: str
    findings: list[BehavioralFindingResponse] = Field(default_factory=list)
    count: int
    advisory: bool = True


def _ingest_planning_prd_source(
    *,
    state_dir: Path,
    prd_id: str | None,
    file: str | None,
    cwd: str | None,
) -> tuple[str, str, IngestedPrdSource]:
    """Return validated partition, stable identity, and bounded markdown.

    Both MCP planning readers use the same opened-handle primitive as the CLI.
    Operational failures are path-safe and occur before any backend is opened.
    """
    from anvil.cli._helpers import (
        PrdSourceIngestError,
        canonical_prd_id,
        ingest_prd_source,
        ingest_prd_source_for_id,
        validate_prd_id,
    )

    source_identity: str | None = None
    try:
        parse_prd_id = validate_prd_id(prd_id if prd_id is not None else "prd")
        if file is not None:
            source_identity = "custom"
            source_path = Path(file)
            if not source_path.is_absolute():
                base = Path(cwd).resolve() if cwd else Path.cwd().resolve()
                source_path = base / source_path
            source = ingest_prd_source(source_path)
        else:
            source_identity = canonical_prd_id(parse_prd_id)
            source = ingest_prd_source_for_id(state_dir, parse_prd_id)
    except PrdSourceIngestError as exc:
        suffix = f": {source_identity}" if source_identity is not None else ""
        detail = f"{exc.message.rstrip('.')}."
        raise ToolError(f"Cannot ingest PRD source{suffix}. {detail}") from None
    assert source_identity is not None
    return parse_prd_id, source_identity, source


@mcp.tool(tags={PLANNING_TAG})
def assess_prd(
    file: str | None = None,
    prd_id: str | None = None,
    cwd: str | None = None,
) -> AssessPrdResponse:
    """Assess a PRD for behaviour-first readiness without mutating state.

    The output is advisory and deterministic: it reports explainable gaps and
    a focused challenge question, but never blocks parse, review, approval,
    planning, claiming, or autonomous execution.
    """
    from anvil.planning.behavioral_readiness import assess_behavioral_readiness
    from anvil.planning.diagnostics import format_parse_error_summary
    from anvil.planning.template import parse_prd as _parse_prd_impl

    state_dir = _resolve_state_dir(cwd)
    if not state_dir.exists():
        raise ToolError(
            f"anvil not initialized in {state_dir.parent}. "
            "Call init_project first.",
        )
    parse_prd_id, source_identity, source = _ingest_planning_prd_source(
        state_dir=state_dir,
        prd_id=prd_id,
        file=file,
        cwd=cwd,
    )
    markdown = source.markdown

    result = _parse_prd_impl(markdown, prd_id=parse_prd_id)
    if result.errors:
        details = format_parse_error_summary(result.errors)
        raise ToolError(
            f"PRD parse failed with {len(result.errors)} error(s): {details}"
        )
    findings = assess_behavioral_readiness(result)
    return AssessPrdResponse(
        prd_source=source_identity,
        findings=[
            BehavioralFindingResponse(
                id=finding.id,
                category=finding.category,
                severity=finding.severity,
                location=finding.location,
                message=finding.message,
                challenge_question=finding.challenge_question,
            )
            for finding in findings
        ],
        count=len(findings),
    )


@mcp.tool(tags={PLANNING_TAG})
def parse_prd(
    file: str | None = None,
    prd_id: str | None = None,
    cwd: str | None = None,
) -> ParsePrdResponse:
    """Parse the PRD markdown into requirements/features/tasks and emit
    prd.parsed; returns counts. Parse errors are returned in the response (not
    raised) so the caller can fix and retry; ToolError is raised only for
    operational failures (missing/unreadable file, project not initialized).

    Args:
        file: PRD path (absolute or cwd-relative). Defaults to the selected
            PRD's portable managed source.
        prd_id: PRD partition to parse (multi-PRD, T019). Mirrors the CLI
            ``--prd`` flag: a non-default id reads its portable collection
            source and stamps the partition into the prd.parsed event so only that PRD's
            rows are (re)written. ``None`` / 'default' / 'prd' keep the bare
            ``.anvil/prd.md`` source + default partition. Ignored for the source
            path when ``file`` is given (but still honoured for the partition).
        cwd:  Project root. Defaults to Path.cwd().
    """
    from anvil.cli._helpers import _DEFAULT_PRD_IDS
    from anvil.clock import SystemClock
    from anvil.planning.diagnostics import parse_diagnostic_report
    from anvil.planning.prd_persistence import (
        PrdRevisionError,
        build_prd_persistence_plan,
    )
    from anvil.planning.template import parse_prd as _parse_prd_impl

    state_dir = _resolve_state_dir(cwd)
    if not state_dir.exists():
        raise ToolError(
            f"anvil not initialized in {state_dir.parent}. "
            "Call init_project first.",
        )

    # T019: the parse-time prd_id controls id shape AND the partition the event
    # writes into — mirrors cli/prd.py. None collapses to the 'prd' sentinel
    # (the default PRD); an explicit non-default id scopes the parse.
    parse_prd_id, source_identity, source = _ingest_planning_prd_source(
        state_dir=state_dir,
        prd_id=prd_id,
        file=file,
        cwd=cwd,
    )
    markdown = source.markdown

    result = _parse_prd_impl(markdown, prd_id=parse_prd_id)

    # Surface errors in the response without short-circuiting the event.
    # When errors exist we skip emission (mirrors the CLI which exits 1
    # before applying); otherwise we emit prd.parsed exactly like the CLI.
    diagnostics = parse_diagnostic_report(result.errors)
    errors_out = [
        ParseErrorEntry(section=e.section, line=e.line, message=e.message)
        for e in diagnostics.entries
    ]

    if result.errors:
        return ParsePrdResponse(
            prd_status=result.prd.status.value,
            requirement_count=len(result.requirements),
            feature_count=len(result.features),
            task_count=len(result.tasks),
            errors=errors_out,
            error_count=diagnostics.total_count,
            errors_shown=diagnostics.shown_count,
            errors_omitted=diagnostics.omitted_count,
            errors_truncated=diagnostics.errors_truncated,
            error_messages_truncated=diagnostics.messages_truncated,
            prd_path=source_identity,
        )

    backend = _open_backend(state_dir)
    try:
        clock = SystemClock()
        project = backend.get_project()
        project_id = project.id if project is not None else "project"
        stored_prd_id = result.prd.id
        is_default_prd = parse_prd_id in _DEFAULT_PRD_IDS
        effective_status = result.prd.status.value
        try:
            persistence = build_prd_persistence_plan(
                backend,
                result,
                source,
                project_id=project_id,
                is_default=is_default_prd,
                actor="anvil-mcp",
                clock=clock,
            )
        except PrdRevisionError as exc:
            raise ToolError(str(exc)) from None

        from anvil.state.backend import EventRejected

        try:
            if persistence.draft is not None:
                backend.append(persistence.draft)
        except EventRejected as exc:
            raise ToolError(f"PRD parse rejected: {exc}") from None

        effective_status = persistence.status
        if persistence.draft is not None:
            persisted_prd = backend.get_prd(stored_prd_id)
            if persisted_prd is not None:
                effective_status = persisted_prd.status.value
    finally:
        backend.close()

    return ParsePrdResponse(
        prd_status=effective_status,
        requirement_count=len(result.requirements),
        feature_count=len(result.features),
        task_count=len(result.tasks),
        errors=errors_out,
        prd_path=source_identity,
    )


# ---------------------------------------------------------------------------
# Tool 17: review_prd
# ---------------------------------------------------------------------------


class ReviewPrdResponse(BaseModel):
    """Result of review_prd."""

    model_config = ConfigDict(extra="forbid")

    from_status: str
    to_status: str
    reviewer: str


@mcp.tool(tags={PLANNING_TAG})
def review_prd(
    approve: bool = False,
    reviewer: str = "human",
    notes: str | None = None,
    prd_id: str | None = None,
    cwd: str | None = None,
) -> ReviewPrdResponse:
    """Advance the PRD review state: draft → reviewed (default), or reviewed →
    approved when approve=True. Emits prd.reviewed or prd.approved.

    Args:
        approve:  True moves reviewed → approved; False moves draft → reviewed.
        reviewer: Identity recorded in the event payload.
        notes:    Optional reviewer notes (recorded on prd.reviewed only).
        prd_id:   PRD partition to review (multi-PRD, T019). Mirrors the CLI
            ``prd review --prd``: resolves which PRD's status to check via
            ``get_prd`` and stamps that id into the emitted event so the handler
            mutates only that PRD's row. ``None`` resolves the single/default
            PRD, byte-identical to pre-T019 on a single-PRD project.
        cwd:      Project root. Defaults to Path.cwd().
    """
    from anvil.cli._helpers import _DEFAULT_PRD_IDS, canonical_prd_id
    from anvil.clock import SystemClock
    from anvil.state.backend import EventRejected
    from anvil.state.models import EventDraft

    state_dir = _resolve_state_dir(cwd)
    if not state_dir.exists():
        raise ToolError(
            f"anvil not initialized in {state_dir.parent}. "
            "Call init_project first.",
        )

    backend = _open_backend(state_dir)
    try:
        # T019: resolve which PRD this review targets (explicit > $ANVIL_PRD >
        # single/default), then read THAT PRD's status. Collapse the default
        # sentinel ('prd') to the stored id ('default') so prd_id='prd' finds
        # the default PRD row instead of looking up a nonexistent id='prd'.
        resolved_prd_id = canonical_prd_id(_resolve_prd_id(backend, prd_id))
        prd = backend.get_prd(resolved_prd_id)
        if prd is None:
            raise ToolError(
                "No PRD found in state. Run parse_prd first.",
            )
        from_status = prd.status.value
        project = backend.get_project()
        project_id = project.id if project is not None else "project"

        # Stamp prd_id into the payload only for a named (non-default) PRD so the
        # default-PRD event stays byte-identical to the pre-multi-PRD payload.
        def _scope(payload: dict[str, Any]) -> dict[str, Any]:
            if prd.id not in _DEFAULT_PRD_IDS:
                payload["prd_id"] = prd.id
            return payload

        if approve:
            if from_status != "reviewed":
                raise ToolError(
                    f"PRD must be in 'reviewed' status to approve, "
                    f"got '{from_status}'. Call review_prd without "
                    "approve=True first.",
                )
            action = "prd.approved"
            to_status = "approved"
            payload: dict[str, Any] = _scope(
                {
                    "project_id": project_id,
                    "expected_revision": prd.revision,
                    "expected_status": prd.status.value,
                    "approver": reviewer,
                }
            )
        else:
            if from_status != "draft":
                raise ToolError(
                    f"PRD must be in 'draft' status to review, "
                    f"got '{from_status}'. Pass approve=True to move "
                    "reviewed → approved.",
                )
            action = "prd.reviewed"
            to_status = "reviewed"
            payload = _scope(
                {
                    "project_id": project_id,
                    "expected_revision": prd.revision,
                    "expected_status": prd.status.value,
                    "reviewer": reviewer,
                    "notes": notes,
                }
            )

        clock = SystemClock()
        now = clock.now()
        try:
            backend.append(EventDraft(
                timestamp=now,
                actor=reviewer,
                action=action,
                target_kind="prd",
                target_id=project_id,
                payload_json=payload,
            ))
        except EventRejected as exc:
            raise ToolError(str(exc)) from exc

        return ReviewPrdResponse(
            from_status=from_status,
            to_status=to_status,
            reviewer=reviewer,
        )
    finally:
        backend.close()


# ---------------------------------------------------------------------------
# Tool 18: plan_tasks
# ---------------------------------------------------------------------------


class PlanTasksResponse(BaseModel):
    """Result of plan_tasks."""

    model_config = ConfigDict(extra="forbid")

    feature_count: int
    task_count: int
    conflict_group_count: int
    warnings: list[ParseErrorEntry]
    warning_count: int = 0
    warnings_shown: int = 0
    warnings_omitted: int = 0
    warnings_truncated: bool = False
    warning_messages_truncated: int = 0
    # LLM backstop signalling. ``llm_generated`` is True when this call drafted
    # a ``## Tasks`` section via the LLM and appended it to prd.md;
    # ``llm_provider`` is the resolved provider slug (else None).
    llm_generated: bool = False
    llm_provider: str | None = None
    # Orphan-prune signalling: task/feature IDs that were in state.db but absent
    # from the new PRD parse and deleted this call. Empty when none were pruned.
    pruned_task_ids: list[str] = []
    pruned_feature_ids: list[str] = []


@mcp.tool(tags={PLANNING_TAG})
def plan_tasks(
    cwd: str | None = None,
    use_llm: bool = True,
    prune_force: bool = False,
    prd_id: str | None = None,
) -> PlanTasksResponse:
    """Run the planner over the current PRD: generate features and tasks, infer
    dependencies and conflict groups, then promote proposed tasks to drafted.

    When the PRD has features but no ``## Tasks`` section, the LLM planner
    drafts tasks, appends them to prd.md, and re-parses (set use_llm=False to
    opt out and keep the deterministic parse). The provider defaults to the
    Claude subscription via the Agent SDK; pin anthropic/bedrock/custom in
    .anvil/config.yaml, or set llm_fallback: true for env auto-detect. See
    docs/llm.md.

    PRD parse errors surface as warnings; LLM failures raise ToolError rather
    than returning a silent zero-count.

    Args:
        cwd: Project root. Defaults to Path.cwd().
        use_llm: When True (default), draft tasks via LLM if the PRD has
            features but 0 tasks.
        prune_force: When True, delete orphan tasks that advanced past
            ``ready`` (default False raises ToolError so claim/evidence
            history is not lost silently).
        prd_id: PRD partition to plan (multi-PRD, T019). Mirrors the CLI
            ``plan --prd``: a non-default id reads its portable collection source,
            scopes orphan-prune to that partition, and stamps the partition into
            the one atomic ``planning.batch_applied`` event. ``None`` / 'default' /
            'prd' keep the bare ``.anvil/prd.md`` source + default partition.
    """
    from anvil.cli._helpers import (
        PrdSourceIngestError,
        _resolve_project_dir,
        display_path,
        ingest_prd_source_for_id,
        replace_prd_source_for_id,
        selected_prd_source_path,
    )
    from anvil.clock import SystemClock
    from anvil.planning.inference import BundlePlanningError, infer_all
    from anvil.planning.llm import LLMProviderError
    from anvil.planning.llm_planner import (
        PlannerProviderUnavailable,
        TaskGenerationError,
        generate_tasks_markdown,
    )
    from anvil.planning.template import parse_prd as _parse_prd_impl
    from anvil.state.backend import EventRejected
    from anvil.state.models import EventDraft

    state_dir = _resolve_state_dir(cwd)
    project_root = _resolve_project_dir(Path(cwd) if cwd else None)
    if not state_dir.exists():
        raise ToolError(
            f"anvil not initialized in {state_dir.parent}. "
            "Call init_project first.",
        )

    # T019: the parse-time prd_id controls id shape AND the source path +
    # partition plan scopes to (mirrors cli/plan.py). None collapses to the
    # 'prd' sentinel (the default PRD).
    parse_prd_id = prd_id if prd_id is not None else "prd"

    try:
        prd_path = selected_prd_source_path(state_dir, parse_prd_id)
    except PrdSourceIngestError as exc:
        raise ToolError(f"Cannot resolve PRD source: {exc.message}") from exc
    prd_display = display_path(prd_path)
    try:
        source = ingest_prd_source_for_id(state_dir, parse_prd_id)
    except PrdSourceIngestError as exc:
        if exc.code == "source_not_found":
            raise ToolError(
                f"PRD file not found at {prd_display}. "
                "Author your PRD and call parse_prd first."
            ) from exc
        raise ToolError(f"Cannot read {prd_display}: {exc.message}") from exc
    markdown = source.markdown

    # v1.17.0 — load config so the LLM-planner backstop honors the
    # project's llm_provider / llm_tier / bedrock / custom-endpoint knobs.
    # Soft-load: a missing or malformed config falls back to env-only
    # resolution rather than blocking the tool.
    #
    # Mirrors cli/plan.py's _load_config_optional pattern: narrow handler
    # for expected error types first, then a labeled last-resort guard for
    # everything else (yaml.YAMLError and friends). That split lets ops
    # distinguish "your YAML is broken" from "the config module itself
    # blew up" in the debug log. (mcp-critic SHOULD FIX, PR #65)
    config = None
    config_path = state_dir / "config.yaml"
    if config_path.exists():
        try:
            from anvil.config import load_config as _load_config

            config = _load_config(config_path)
        except (FileNotFoundError, OSError, ValueError) as exc:
            print(
                f"plan_tasks: config.yaml load failed "
                f"({type(exc).__name__}: {exc}); falling back to env-only "
                "LLM resolution.",
                file=sys.stderr,
            )
        except Exception as exc:  # noqa: BLE001 — last-resort guard, never re-raise
            # yaml.YAMLError and any other unexpected error: warn and
            # fall back. Distinct prefix so the debug log distinguishes
            # this from the narrow-handler path above.
            print(
                f"plan_tasks: unexpected config.yaml load error "
                f"({type(exc).__name__}: {exc}); falling back to env-only "
                "LLM resolution.",
                file=sys.stderr,
            )

    result = _parse_prd_impl(markdown, prd_id=parse_prd_id)
    from anvil.planning.diagnostics import parse_diagnostic_report

    warning_report = parse_diagnostic_report(result.errors)
    warnings = [
        ParseErrorEntry(section=e.section, line=e.line, message=e.message)
        for e in warning_report.entries
    ]

    # ------------------------------------------------------------------
    # LLM task-generation backstop (v1.15+)
    #
    # When the PRD has features+requirements but no `## Tasks` section the
    # deterministic parser yields 0 tasks. Previously plan_tasks returned
    # task_count=0 silently and downstream tools were left without tasks
    # to operate on. Now we call the LLM planner, append generated tasks
    # to prd.md, and re-parse before any events are emitted.
    # ------------------------------------------------------------------
    llm_generated = False
    llm_provider: str | None = None
    if (
        use_llm
        and len(result.tasks) == 0
        and len(result.features) > 0
    ):
        try:
            gen_result = generate_tasks_markdown(
                prd=result.prd,
                features=result.features,
                requirements=result.requirements,
                config=config,
            )
        except PlannerProviderUnavailable as exc:
            raise ToolError(str(exc)) from exc
        except LLMProviderError as exc:
            # The default agent-sdk provider always resolves but can fail at
            # generate() time (missing `claude` CLI / SDK, bad model, transport
            # error) — an LLMProviderError, not the resolve-time
            # PlannerProviderUnavailable. Surface it as a clean ToolError so the
            # client gets the actionable message instead of an unhandled
            # exception. (The message names the fix: install/login to Claude
            # Code or pin a provider.)
            raise ToolError(f"LLM call failed: {exc}") from exc
        except TaskGenerationError as exc:
            # mcp-critic SHOULD FIX from PR #63: TaskGenerationError's
            # message can include up to 500 chars of raw LLM output (see
            # llm_planner._validate_and_normalize). Re-raising it through
            # ToolError leaks that to the MCP client. The full exception
            # is logged for ops, but the client sees a safe summary.
            print(
                f"LLM task generation failed for plan_tasks: {exc}",
                file=sys.stderr,
            )
            raise ToolError(
                "LLM task generation failed: the response did not contain "
                "any '### TXXX:' blocks. Check the LLM provider's output "
                "in stderr for the full response; fix prd.md or re-tune "
                "the prompt and re-run plan_tasks."
            ) from exc

        # Idempotency guard: only append `## Tasks` when not already
        # present, so re-running plan_tasks after a previous append is a
        # no-op on the file.
        try:
            current_source = ingest_prd_source_for_id(state_dir, parse_prd_id)
        except PrdSourceIngestError as exc:
            raise ToolError(
                f"Cannot re-read {prd_display}: {exc.message}"
            ) from exc

        from anvil.planning._plan_helpers import has_tasks_section
        current_markdown = current_source.markdown
        source = current_source
        if not has_tasks_section(current_markdown):
            new_markdown = (
                current_markdown.rstrip() + "\n\n" + gen_result.markdown + "\n"
            )
            try:
                updated_source = replace_prd_source_for_id(
                    state_dir,
                    parse_prd_id,
                    expected_sha256=current_source.source_sha256,
                    markdown=new_markdown,
                )
            except PrdSourceIngestError as exc:
                raise ToolError(
                    f"Cannot write generated tasks to {prd_display}: {exc.message}"
                ) from exc
            markdown = updated_source.markdown
            source = updated_source
        else:
            markdown = current_markdown
        result = _parse_prd_impl(markdown, prd_id=parse_prd_id)
        llm_generated = True
        llm_provider = gen_result.provider_used

    backend = _open_backend(state_dir)
    try:
        # T019: the partition this plan run owns. ``result.prd.id`` is the MODEL
        # prd_id ('default' for the default PRD, else the named id) already
        # collapsed from the 'prd' parse sentinel. Orphan-prune scopes to this
        # partition so tasks in OTHER PRDs are never pruned just because they
        # are absent from this PRD's prd.md. Mirrors cli/plan.py.
        scope_prd_id = result.prd.id

        # Guard: `parse_prd` must have run first so the backend has the PRD row
        # THIS run targets. Without this check, an out-of-order call would emit
        # a graph batch into a backend with no matching PRD row, leaving
        # downstream tools (review_prd, apply_review_decision) to fail with
        # "No PRD found" after the state was already mutated. Fail loudly here.
        #
        # Probe the target partition (``scope_prd_id``), NOT the bare default:
        # a multi-PRD project with only named PRDs (no is_default row) can call
        # plan_tasks(prd_id='v0.2') legitimately, and bare get_prd() would wrongly
        # raise even though v0.2 is a real parsed partition.
        stored_prd = backend.get_prd(scope_prd_id)
        if stored_prd is None:
            raise ToolError(
                f"No PRD found in state for '{scope_prd_id}'. Call parse_prd "
                "before plan_tasks so the PRD row exists before the atomic "
                "planning graph is emitted."
            )

        # Validate and infer the complete parsed task set before any event is
        # appended. Native path identity failures must surface as ToolError
        # and leave both the projection and append-only log byte-identical.
        try:
            inference_result = infer_all(
                result.tasks,
                project_root=project_root,
            )
        except BundlePlanningError as exc:
            raise ToolError(f"Planning inference refused: {exc}") from None

        clock = SystemClock()

        def _with_prd_id(payload: dict[str, Any], model_prd_id: str) -> dict[str, Any]:
            # prd_id is Field(exclude=True) on Feature/Task, so model_dump drops
            # it. Stamp it back so the SQL handler writes the row into THIS PRD's
            # partition instead of defaulting to 'default'.
            payload["prd_id"] = model_prd_id
            return payload

        # --------------------------------------------------------------
        # Orphan-prune (v1.15.0). Shares planning._plan_helpers with the
        # CLI — see that module's docstring for the multi-critic review
        # finding that drove the extraction (previously this logic was
        # duplicated, the safe-status set was triplicated, and the CLI
        # was missing the TransactionAborted catch that the MCP had).
        # --------------------------------------------------------------
        from anvil.planning._plan_helpers import (
            build_prd_revision_draft,
            build_prune_event_drafts,
            classify_orphans,
            emit_planning_batch,
        )

        classification = classify_orphans(
            backend.list_tasks(prd_id=scope_prd_id),
            {t.id for t in result.tasks},
            backend.list_features(prd_id=scope_prd_id),
            {f.id for f in result.features},
        )

        if classification.unsafe_task_orphans and not prune_force:
            blocked = ", ".join(
                f"{t.id}({t.status.value})"
                for t in classification.unsafe_task_orphans
            )
            raise ToolError(
                f"{len(classification.unsafe_task_orphans)} orphan task(s) "
                "removed from prd.md have advanced past `ready` status; "
                "deleting silently would lose claim/evidence history. "
                f"Blocked: {blocked}. Release the claims (or complete the "
                "work) and re-call plan_tasks, OR re-call with "
                "prune_force=True to delete despite the status (audit "
                "history is preserved either way)."
            )

        try:
            prd_revision_draft = build_prd_revision_draft(
                backend,
                result,
                source,
                actor="anvil-mcp",
                clock=clock,
            )
        except ValueError:
            raise ToolError(
                "Planning source could not be bound to the persisted PRD."
            ) from None

        operations, prune_result = build_prune_event_drafts(
            classification,
            actor="anvil-mcp",
            clock=clock,
            prune_force=prune_force,
        )
        if prd_revision_draft is not None:
            operations.insert(0, prd_revision_draft)

        pruned_task_ids = prune_result.pruned_task_ids
        pruned_feature_ids = prune_result.pruned_feature_ids

        # Build one atomic canonical graph transition.
        for feature in result.features:
            now = clock.now()
            operations.append(EventDraft(
                    timestamp=now,
                    actor="anvil-mcp",
                    action="feature.created",
                    target_kind="feature",
                    target_id=feature.id,
                    payload_json=_with_prd_id(
                        feature.model_dump(mode="json"), feature.prd_id
                    ),
                ))

        # Persist only the validated canonical inference result. Previously the
        # MCP path first appended every raw parsed task and then upserted the
        # inferred copy. That exposed a transient/raw graph in the event log and
        # could leave it behind when a later inference append was refused.
        for inferred_task in inference_result.tasks:
            now = clock.now()
            operations.append(EventDraft(
                    timestamp=now,
                    actor="anvil-mcp",
                    action="task.created",
                    target_kind="task",
                    target_id=inferred_task.id,
                    payload_json=_with_prd_id(
                        inferred_task.model_dump(mode="json"), inferred_task.prd_id
                    ),
                ))

            current = backend.get_task(inferred_task.id)
            if current is None or current.status.value == "proposed":
                now = clock.now()
                operations.append(EventDraft(
                        timestamp=now,
                        actor="anvil-mcp",
                        action="task.status_changed",
                        target_kind="task",
                        target_id=inferred_task.id,
                        payload_json={
                            "task_id": inferred_task.id,
                            "from": "proposed",
                            "to": "drafted",
                            "reason": "plan_tasks: initial draft after inference",
                        },
                    ))

        # CL-4 — persist the inferred ConflictGroups so the conflict_groups
        # table round-trips them (parity with `anvil plan`). The task rows
        # already carry the group IDs; these events populate the dedicated
        # table with the full group records.
        for cg in inference_result.conflict_groups:
            now = clock.now()
            operations.append(EventDraft(
                    timestamp=now,
                    actor="anvil-mcp",
                    action="conflict_group.upserted",
                    target_kind="conflict_group",
                    target_id=cg.id,
                    payload_json=cg.model_dump(mode="json"),
                ))

        try:
            emit_planning_batch(
                backend,
                operations,
                actor="anvil-mcp",
                clock=clock,
                prd_id=scope_prd_id,
                expected_prd_revision=stored_prd.revision,
                expected_prd_source_sha256=stored_prd.source_sha256,
            )
        except EventRejected as exc:
            raise ToolError(str(exc)) from exc

        return PlanTasksResponse(
            feature_count=len(result.features),
            task_count=len(result.tasks),
            conflict_group_count=len(inference_result.conflict_groups),
            warnings=warnings,
            warning_count=warning_report.total_count,
            warnings_shown=warning_report.shown_count,
            warnings_omitted=warning_report.omitted_count,
            warnings_truncated=warning_report.errors_truncated,
            warning_messages_truncated=warning_report.messages_truncated,
            llm_generated=llm_generated,
            llm_provider=llm_provider,
            pruned_task_ids=pruned_task_ids,
            pruned_feature_ids=pruned_feature_ids,
        )
    finally:
        backend.close()


# `_has_tasks_section` and `_TASKS_HEADING_RE` previously lived here as a
# twin of cli/plan.py. As of v1.15.0 post-review they live in
# planning/_plan_helpers.py — see that module's docstring for the
# multi-critic finding that drove the extraction.


# ---------------------------------------------------------------------------
# Tool 19: score_tasks
# ---------------------------------------------------------------------------


class TaskScoreEntry(BaseModel):
    """One per-task score in the score_tasks response."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    complexity: int
    parallelizability: int
    context_load: int
    blast_radius: int
    review_risk: int
    agent_suitability: int


class ExpansionQueueEntry(BaseModel):
    """One task queued for sub-task expansion (complexity >= threshold),
    carrying the task identity, its complexity, a suggested split size, and the
    exact CLI follow-up command. Expansion itself runs via the planner agent /
    ``expand --use-llm``, never here."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    title: str
    complexity: int
    suggested_subtasks: int
    expand_command: str


class ScoreTasksResponse(BaseModel):
    """Result of score_tasks."""

    model_config = ConfigDict(extra="forbid")

    prd_id: str | None
    all_prds: bool
    scored: list[TaskScoreEntry]
    skipped_already_scored: int
    # ``expansion_queue`` lists every task at/above ``auto_expand_threshold``
    # when ``auto_expand`` is on; empty when disabled.
    auto_expand: bool
    auto_expand_threshold: int
    expansion_queue: list[ExpansionQueueEntry]


@mcp.tool(tags={PLANNING_TAG})
def score_tasks(
    task_id: str | None = None,
    prd_id: str | None = None,
    all_prds: bool = False,
    cwd: str | None = None,
) -> ScoreTasksResponse:
    """Run the rule-based (non-LLM) scoring engine on one task or all unscored
    tasks across six dimensions; emits task.scored per task.

    Pass task_id to always re-score that one task; pass None to score only
    tasks whose scores are incomplete (the rest count toward
    skipped_already_scored). By default exactly one PRD is resolved from
    prd_id, ANVIL_PRD, or the default/single partition. Set all_prds=true for
    an explicit project-wide pass. The response also carries a deterministic
    expansion_queue of high-complexity tasks; the LLM-side expansion runs via
    the planner agent, never here.

    Args:
        task_id: Specific task to score (always re-scored). None scores all
                 unscored tasks.
        prd_id:  PRD partition to score. Mutually exclusive with all_prds.
        all_prds: Explicitly score every PRD partition; ignores ANVIL_PRD.
        cwd:     Project root. Defaults to Path.cwd().
    """
    from anvil.cli._helpers import _scores_complete, canonical_prd_id
    from anvil.clock import SystemClock
    from anvil.config import DEFAULT_AUTO_EXPAND_THRESHOLD
    from anvil.planning.scoring import (
        IncompleteScoreError,
        build_recursive_expansion_queue,
        require_complete_score,
        score_task,
    )
    from anvil.state.backend import EventRejected
    from anvil.state.models import EventDraft

    state_dir = _resolve_state_dir(cwd)
    if not state_dir.exists():
        raise ToolError(
            f"anvil not initialized in {state_dir.parent}. "
            "Call init_project first.",
        )

    # v1.21.0 — soft-load config for the auto-expansion knobs. Mirrors the
    # plan_tasks pattern above: a missing or malformed config never blocks
    # the tool; we fall back to the defaults (auto_expand on, threshold 4).
    auto_expand = True
    auto_expand_threshold = DEFAULT_AUTO_EXPAND_THRESHOLD
    config_path = state_dir / "config.yaml"
    if config_path.exists():
        try:
            from anvil.config import load_config as _load_config

            _config = _load_config(config_path)
            auto_expand = _config.auto_expand
            auto_expand_threshold = _config.auto_expand_threshold
        except (FileNotFoundError, OSError, ValueError) as exc:
            print(
                f"score_tasks: config.yaml load failed "
                f"({type(exc).__name__}: {exc}); falling back to default "
                "auto-expansion settings.",
                file=sys.stderr,
            )
        except Exception as exc:  # noqa: BLE001 — last-resort guard, never re-raise
            # yaml.YAMLError and any other unexpected error: warn and fall
            # back. Distinct prefix so the debug log distinguishes this
            # from the narrow-handler path above.
            print(
                f"score_tasks: unexpected config.yaml load error "
                f"({type(exc).__name__}: {exc}); falling back to default "
                "auto-expansion settings.",
                file=sys.stderr,
            )

    backend = _open_backend(state_dir)
    try:
        if all_prds and prd_id is not None:
            raise ToolError("prd_id and all_prds are mutually exclusive")
        scoped_prd_id = None
        if not all_prds:
            scoped_prd_id = canonical_prd_id(_resolve_prd_id(backend, prd_id))
            if backend.get_prd(scoped_prd_id) is None:
                raise ToolError(
                    "selected PRD was not found in state. Run parse_prd first."
                )

        if task_id is not None:
            task = backend.get_task(task_id)
            if task is None:
                raise ToolError(f"Task '{task_id}' not found.")
            if scoped_prd_id is not None and task.prd_id != scoped_prd_id:
                raise ToolError(
                    f"Task '{task_id}' belongs to PRD '{task.prd_id}', "
                    f"not '{scoped_prd_id}'."
                )
            tasks_to_score = [task]
            skipped = 0
        else:
            all_tasks = backend.list_tasks(prd_id=scoped_prd_id)
            tasks_to_score = [t for t in all_tasks if not _scores_complete(t)]
            skipped = len(all_tasks) - len(tasks_to_score)

        clock = SystemClock()
        scored: list[TaskScoreEntry] = []
        validated_scores = []
        for task in tasks_to_score:
            try:
                computed = require_complete_score(score_task(task))
            except IncompleteScoreError as exc:
                raise ToolError(f"score_incomplete: {exc}") from exc
            validated_scores.append((task, computed))

        # Refuse an incomplete batch before the first event append.
        for task, computed in validated_scores:
            now = clock.now()
            payload: dict[str, Any] = {
                "task_id": task.id,
                "scores": {
                    "complexity": computed.complexity,
                    "parallelizability": computed.parallelizability,
                    "context_load": computed.context_load,
                    "blast_radius": computed.blast_radius,
                    "review_risk": computed.review_risk,
                    "agent_suitability": computed.agent_suitability,
                },
                "explanation": computed.explanation,
            }
            try:
                backend.append(EventDraft(
                    timestamp=now,
                    actor="anvil-mcp",
                    action="task.scored",
                    target_kind="task",
                    target_id=task.id,
                    payload_json=payload,
                ))
            except EventRejected as exc:
                raise ToolError(str(exc)) from exc

            scored.append(TaskScoreEntry(
                task_id=task.id,
                complexity=computed.complexity,
                parallelizability=computed.parallelizability,
                context_load=computed.context_load,
                blast_radius=computed.blast_radius,
                review_risk=computed.review_risk,
                agent_suitability=computed.agent_suitability,
            ))

        # v1.21.0 — re-fetch AFTER the task.scored events landed so the
        # queue covers every task at/above threshold (including ones scored
        # in earlier runs), not just this call's batch.
        expansion_queue: list[ExpansionQueueEntry] = []
        if auto_expand:
            expansion_queue = [
                ExpansionQueueEntry(
                    task_id=candidate.task_id,
                    title=candidate.title,
                    complexity=candidate.complexity,
                    suggested_subtasks=candidate.suggested_subtasks,
                    expand_command=(
                        f"anvil expand {candidate.task_id} --use-llm"
                    ),
                )
                for candidate in build_recursive_expansion_queue(
                    backend.list_tasks(prd_id=scoped_prd_id),
                    threshold=auto_expand_threshold,
                )
            ]

        return ScoreTasksResponse(
            prd_id=scoped_prd_id,
            all_prds=all_prds,
            scored=scored,
            skipped_already_scored=skipped,
            auto_expand=auto_expand,
            auto_expand_threshold=auto_expand_threshold,
            expansion_queue=expansion_queue,
        )
    finally:
        backend.close()


# ---------------------------------------------------------------------------
# Tool 20: review_tasks
# ---------------------------------------------------------------------------


class BlockedTaskEntry(BaseModel):
    """One task that failed a review gate."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    reason: str


class ReviewTasksResponse(BaseModel):
    """Result of review_tasks."""

    model_config = ConfigDict(extra="forbid")

    promoted_to_reviewed: list[str]
    promoted_to_ready: list[str]
    blocked: list[BlockedTaskEntry]
    prd_id: str | None
    all_prds: bool


@mcp.tool(tags={PLANNING_TAG})
def review_tasks(
    cwd: str | None = None,
    prd_id: str | None = None,
    all_prds: bool = False,
) -> ReviewTasksResponse:
    """Promote tasks through drafted → reviewed → ready, applying the review
    gates. Returns the promoted task IDs per stage plus any tasks a gate
    blocked (with reasons).

    Args:
        cwd: Project root. Defaults to Path.cwd().
        prd_id: PRD partition. Precedence is explicit value, ``ANVIL_PRD``,
            then single/default resolution. Mutually exclusive with all_prds.
        all_prds: Explicitly review every PRD partition. When true, an ambient
            ``ANVIL_PRD`` is ignored.
    """
    from anvil.clock import SystemClock
    from anvil.state.backend import EventRejected
    from anvil.state.models import EventDraft
    from anvil.state.transitions import (
        TransitionError,
        task_drafted_to_reviewed,
        task_reviewed_to_ready,
    )

    state_dir = _resolve_state_dir(cwd)
    if not state_dir.exists():
        raise ToolError(
            f"anvil not initialized in {state_dir.parent}. "
            "Call init_project first.",
        )

    backend = _open_backend(state_dir)
    try:
        clock = SystemClock()
        if all_prds and prd_id is not None:
            raise ToolError("prd_id and all_prds are mutually exclusive.")
        selected_prd_id = None
        if not all_prds:
            from anvil.cli._helpers import canonical_prd_id

            selected_prd_id = canonical_prd_id(_resolve_prd_id(backend, prd_id))
            if backend.get_prd(selected_prd_id) is None:
                raise ToolError("selected PRD was not found in state. Run parse_prd first.")
        all_tasks = backend.list_tasks(prd_id=selected_prd_id)

        drafted = [t for t in all_tasks if t.status.value == "drafted"]
        already_reviewed_ids = {
            t.id for t in all_tasks if t.status.value == "reviewed"
        }

        promoted_to_reviewed: list[str] = []
        promoted_to_ready: list[str] = []
        blocked: list[BlockedTaskEntry] = []

        # drafted → reviewed
        for task in drafted:
            now = clock.now()
            try:
                task_drafted_to_reviewed(task, now)
            except TransitionError as exc:
                blocked.append(BlockedTaskEntry(task_id=task.id, reason=exc.message))
                continue
            try:
                backend.append(EventDraft(
                    timestamp=now,
                    actor="anvil-mcp",
                    action="task.status_changed",
                    target_kind="task",
                    target_id=task.id,
                    payload_json={
                        "task_id": task.id,
                        "from": "drafted",
                        "to": "reviewed",
                        "reason": "review_tasks: gate passed",
                    },
                ))
            except EventRejected as exc:
                raise ToolError(str(exc)) from exc
            promoted_to_reviewed.append(task.id)

        # reviewed → ready (covers tasks promoted just above plus pre-existing reviewed)
        candidates = backend.list_tasks(prd_id=selected_prd_id)
        promoted_set = set(promoted_to_reviewed)
        for task in candidates:
            if task.status.value != "reviewed":
                continue
            if task.id not in promoted_set and task.id not in already_reviewed_ids:
                continue
            now = clock.now()
            try:
                task_reviewed_to_ready(task, now)
            except TransitionError as exc:
                blocked.append(BlockedTaskEntry(task_id=task.id, reason=exc.message))
                continue
            try:
                backend.append(EventDraft(
                    timestamp=now,
                    actor="anvil-mcp",
                    action="task.status_changed",
                    target_kind="task",
                    target_id=task.id,
                    payload_json={
                        "task_id": task.id,
                        "from": "reviewed",
                        "to": "ready",
                        "reason": "review_tasks: promoted to ready",
                    },
                ))
            except EventRejected as exc:
                raise ToolError(str(exc)) from exc
            try:
                from anvil.cli.plan import confirm_task_risk_scores

                confirm_task_risk_scores(backend, task, now, "anvil-mcp")
            except EventRejected as exc:
                raise ToolError(str(exc)) from exc
            promoted_to_ready.append(task.id)

        return ReviewTasksResponse(
            promoted_to_reviewed=promoted_to_reviewed,
            promoted_to_ready=promoted_to_ready,
            blocked=blocked,
            prd_id=selected_prd_id,
            all_prds=all_prds,
        )
    finally:
        backend.close()


# ---------------------------------------------------------------------------
# Tool 21: apply_review_decision
# ---------------------------------------------------------------------------


class ApplyRejectionMetricsResponse(BaseModel):
    """Immediate governor projection after a rejected review attempt."""

    model_config = ConfigDict(extra="forbid")

    counts_toward_accept_rate: bool
    work_actor: str | None = None
    as_of: str
    window_days: float
    window_start: str
    numerator: int
    denominator: int
    rate: float | None = None
    floor: float
    configured_floor: float
    needs_review_depth: int
    needs_review_cap: int
    guidance: str
    rejection_count: int
    required_floor: float


class ApplyReviewResponse(BaseModel):
    """Result of apply_review_decision."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    decision: str  # "accepted" or "rejected"
    from_status: str
    to_status: str
    reviewer: str
    # The next claimable task after this disposition (an approval may unblock
    # dependents); null when none is available.
    next_ready: NextReadyTask | None = None
    rejection: TaskRejectionProvenance | None = None
    rejection_metrics: ApplyRejectionMetricsResponse | None = None


@mcp.tool(tags={PLANNING_TAG})
def apply_review_decision(
    task_id: str,
    approve: bool,
    reviewer: str = "human",
    reason: str | None = None,
    reason_code: RejectionReasonCode | None = None,
    quality_findings: list[RejectionQualityFindingCode] | None = None,
    strict: bool | None = None,
    cwd: str | None = None,
) -> ApplyReviewResponse:
    """Apply a human review decision on a needs_review task: approve (→ accepted
    → done) or reject (→ rejected/drafted for rework). Emits task.applied; the
    backend auto-promotes through accepted → done on approval.

    Under strict evidence mode an approval REFUSES (ToolError code
    ``evidence_incomplete``, listing the missing items) before any event is
    appended, leaving the task in needs_review; rejections are never gated.
    Strict resolves as: explicit ``strict`` param > config ``strict_evidence`` >
    False (advisory).

    Args:
        task_id:  Task awaiting review (must be in needs_review status).
        approve:  True accepts the work; False rejects it.
        reviewer: Identity recorded in the event payload.
        reason:   Required when approve=False; recorded as review notes.
        reason_code: Bounded rejection assertion; the backend derives category.
        quality_findings: Typed quality dimensions; duplicates are refused.
        strict:   Evidence-gate override (approve only). None defers to config.
        cwd:      Project root. Defaults to Path.cwd().
    """
    from anvil.clock import SystemClock
    from anvil.state.backend import EventRejected
    from anvil.state.models import EventDraft

    state_dir = _resolve_state_dir(cwd)
    if not state_dir.exists():
        raise ToolError(
            f"anvil not initialized in {state_dir.parent}. "
            "Call init_project first.",
        )

    if not approve and not reason:
        raise ToolError(
            "Rejection requires reason= (non-empty). "
            "Pass approve=True to accept, or provide a rejection reason.",
        )
    if approve and (reason_code is not None or quality_findings):
        raise ToolError(
            "Rejection provenance inputs are only valid when approve=False."
        )
    if quality_findings and len(quality_findings) != len(set(quality_findings)):
        raise ToolError("quality_findings must not contain duplicate codes")

    backend = _open_backend(state_dir)
    try:
        task = backend.get_task(task_id)
        if task is None:
            raise ToolError(f"Task '{task_id}' not found.")

        from_status = task.status.value
        if from_status != "needs_review":
            raise ToolError(
                f"Task '{task_id}' has status '{from_status}', "
                "expected 'needs_review'. Submit completion evidence first.",
            )

        # T025/B25 — completion-evidence ENFORCEMENT on the MCP accept path.
        # Only approvals are gated (rejecting a task with missing evidence is
        # the right move). When strict is in effect and the gate is INCOMPLETE,
        # refuse BEFORE appending the task.applied event so the task stays in
        # needs_review. A complete gate, or a task with no required_evidence,
        # is a no-op. DEFAULT (strict None, no config) preserves the historical
        # advisory behaviour — accept proceeds regardless.
        if approve and _resolve_strict_evidence(strict, state_dir):
            from anvil.review.gates import evidence_complete

            evidence_obj = backend.get_latest_evidence(task_id)
            if evidence_obj is not None:
                gate_passed, gate_missing = evidence_complete(task, evidence_obj)
            elif (
                task.verification.required_evidence
                or task.verification.required_proofs
            ):
                # No evidence at all when something is required is a failure —
                # check BOTH the legacy string list and the typed proofs.
                gate_passed, gate_missing = (
                    False,
                    list(task.verification.required_evidence)
                    + [r.label for r in task.verification.required_proofs],
                )
            else:
                gate_passed, gate_missing = True, []

            if not gate_passed:
                # Standard MCP error surface: raise ToolError. The message
                # carries the stable code ``evidence_incomplete`` plus the
                # missing items so callers can branch on it the same way the
                # CLI's JSON ``error.code`` does.
                raise ToolError(
                    f"evidence_incomplete: strict evidence gate refused "
                    f"approval of task '{task_id}'; required evidence is "
                    f"missing ({', '.join(gate_missing)}). Task remains in "
                    "needs_review. Submit the missing evidence and retry, or "
                    "pass strict=False to override for this call.",
                )

        # evidence-contracts:T005 — AUTO-STRICT contract gate on the MCP
        # accept path, identical to the CLI: a task declaring claims or
        # artifact assertions is held to them independent of strict_evidence.
        # Refuse BEFORE task.applied; rejections are never gated.
        latest_evidence = backend.get_latest_evidence(task_id)
        affirmative_category = bool(
            latest_evidence is not None
            and str(latest_evidence.category) != "completion"
        )
        if approve and (
            task.claims
            or task.verification.artifact_assertions
            or affirmative_category
        ):
            from pathlib import Path as _Path

            from anvil.review.gates import evaluate_claims

            try:
                contract_verdict = evaluate_claims(
                    task,
                    latest_evidence,
                    project_root=_Path(cwd).resolve() if cwd else _Path.cwd(),
                )
            except Exception:  # noqa: BLE001 — never brick apply on a gate bug
                # Review finding: the fail-open must be LOUD on the machine
                # surface too — a silently skipped gate is the incident class.
                print(
                    "anvil-mcp: claim gate could not run (internal error); "
                    "contract enforcement skipped for this call.",
                    file=sys.stderr,
                )
                contract_verdict = None
            if (
                contract_verdict is not None
                and contract_verdict.enforceable_unproven
            ):
                unproven = ", ".join(
                    f"{cv.claim or '(task)'} [{cv.verdict}]: "
                    + "; ".join(cv.failures + cv.missing + cv.proof_missing)
                    for cv in contract_verdict.enforceable_unproven
                )
                raise ToolError(
                    f"claim_unproven: claim gate refused approval of task "
                    f"'{task_id}'; unproven claim(s): {unproven}. Task "
                    "remains in needs_review.",
                )

        decision = "accepted" if approve else "rejected"
        clock = SystemClock()
        now = clock.now()
        rejection_provenance: TaskRejectionProvenance | None = None
        if not approve:
            selected_reason = reason_code or (
                RejectionReasonCode.quality_findings
                if quality_findings
                else RejectionReasonCode.unspecified_quality
            )
            typed_findings = [
                RejectionQualityFinding(code=code)
                for code in (quality_findings or [])
            ]
            try:
                rejection_provenance = backend.derive_task_rejection_provenance(
                    task_id,
                    reason_code=selected_reason,
                    quality_findings=typed_findings,
                )
            except EventRejected as exc:
                raise ToolError(f"rejection_provenance_invalid: {exc}") from None
        payload: dict[str, Any] = {
            "schema_version": 1,
            "task_id": task_id,
            "reviewer": reviewer,
            "decision": decision,
            "notes": reason,
        }
        if approve:
            current_attempt = backend.get_latest_evidence(task_id)
            if current_attempt is not None:
                payload["review_attempt_id"] = current_attempt.id
        if rejection_provenance is not None:
            payload["rejection"] = rejection_provenance.model_dump(mode="json")

        try:
            applied_event = backend.append(EventDraft(
                timestamp=now,
                actor=reviewer,
                action="task.applied",
                target_kind="task",
                target_id=task_id,
                payload_json=payload,
            ))
        except EventRejected as exc:
            raise ToolError(str(exc)) from exc

        # B48 part 2: on acceptance, emit a portable signed AcceptanceProof
        # (best-effort, file-only — mirrors the CLI apply path).
        if approve and applied_event is not None:
            from anvil.cli.packet_apply import emit_acceptance_proof

            emit_acceptance_proof(state_dir, backend, task_id, applied_event)

        # Read fresh status after the backend's auto-promotion (accepted → done
        # on approval, needs_review → drafted on rejection, etc.).
        fresh = backend.get_task(task_id)
        to_status = fresh.status.value if fresh is not None else decision

        # T014: name the next claimable task after this disposition. Use the
        # reviewer as the actor (a human reviewer holds no active claims, so
        # all foreign locks are honoured).
        next_ready_raw = _compute_next_ready(backend, reviewer)
        next_ready = (
            NextReadyTask(**next_ready_raw) if next_ready_raw is not None else None
        )
        rejection_metrics = None
        if rejection_provenance is not None:
            from anvil.cli.packet_apply import _rejection_metrics_block

            rejection_metrics = ApplyRejectionMetricsResponse.model_validate(
                _rejection_metrics_block(
                    backend,
                    state_dir=state_dir,
                    task_id=task_id,
                    provenance=rejection_provenance,
                    clock=clock,
                    as_of=now,
                )
            )

        return ApplyReviewResponse(
            task_id=task_id,
            decision=decision,
            from_status=from_status,
            to_status=to_status,
            reviewer=reviewer,
            next_ready=next_ready,
            rejection=rejection_provenance,
            rejection_metrics=rejection_metrics,
        )
    finally:
        backend.close()


# ===========================================================================
# Decision resolution
# ===========================================================================
#
# One read-only tool that surfaces unresolved decisions in the PRD so the
# `resolve-decisions` skill (markdown) can drive Q&A. Detection logic lives
# in anvil.planning.decisions and is shared with the CLI.


# ---------------------------------------------------------------------------
# Tool 22: find_decisions
# ---------------------------------------------------------------------------


class UnresolvedDecisionEntry(BaseModel):
    """One unresolved-decision record, flat for over-the-wire transport."""

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: str  # "needs_decision" | "open_question" | "missing_field"
    location: str
    text: str
    context_paragraph: str
    suggested_resolution_field: str


class FindDecisionsResponse(BaseModel):
    """Result of find_decisions."""

    model_config = ConfigDict(extra="forbid")

    prd_id: str
    prd_source: str
    decisions: list[UnresolvedDecisionEntry]
    counts_by_kind: dict[str, int]
    total: int


@mcp.tool(tags={PLANNING_TAG})
def find_decisions(
    cwd: str | None = None,
    prd_id: str | None = None,
) -> FindDecisionsResponse:
    """Scan the PRD for items needing a human decision (read-only; emits no
    events). Walks three sources: inline ``[NEEDS DECISION]`` markers,
    ``## Open Questions`` items, and tasks with empty acceptance_criteria or
    verification.commands. Drives the `resolve-decisions` skill.

    Returns the decisions (needs_decision, then open_question, then
    missing_field), counts by kind, and the total. Raises ToolError when
    .anvil/ or prd.md is missing.

    Args:
        cwd: Project root. Defaults to ``Path.cwd()``.
        prd_id: PRD partition. Precedence is explicit value, ``ANVIL_PRD``,
            then the single/default partition.
    """
    import os

    from anvil.cli._helpers import (
        PrdSourceIngestError,
        canonical_prd_id,
        ingest_prd_source_for_id,
        validate_prd_id,
    )
    from anvil.planning.decisions import find_unresolved_decisions
    from anvil.planning.template import parse_prd as _parse_prd_impl

    state_dir = _resolve_state_dir(cwd)
    if not state_dir.exists():
        raise ToolError(
            f"anvil not initialized in {state_dir.parent}. "
            "Call init_project first.",
        )

    backend = _open_backend(state_dir)
    try:
        has_prds = bool(backend.list_prds())
        if has_prds:
            effective_prd_id = canonical_prd_id(_resolve_prd_id(backend, prd_id))
        else:
            effective_prd_id = canonical_prd_id(
                validate_prd_id(prd_id or os.environ.get("ANVIL_PRD") or "default")
            )
        if has_prds and backend.get_prd(effective_prd_id) is None:
            raise ToolError(f"PRD partition {effective_prd_id!r} does not exist")
        try:
            source = ingest_prd_source_for_id(state_dir, effective_prd_id)
        except PrdSourceIngestError as exc:
            if exc.code == "source_not_found":
                raise ToolError(
                    f"PRD file not found for partition {effective_prd_id!r}"
                ) from exc
            raise ToolError(f"{exc.code}: {exc.message}") from exc
        backend_tasks = backend.list_tasks(prd_id=effective_prd_id)
        tasks_or_none = backend_tasks if backend_tasks else None
    finally:
        backend.close()

    result = _parse_prd_impl(source.markdown, prd_id=effective_prd_id)
    # Match the CLI's behavior: if the parse failed, surface the errors
    # rather than silently returning a deceptive 0-open_questions count
    # (the PRD model exists but with empty sections). The needs_decision
    # detector works against raw markdown and would still find inline
    # markers, but the user almost certainly wants the parse failure
    # surfaced first so they can fix the structural problem before
    # interpreting the decision list.
    if result.errors:
        from anvil.planning.diagnostics import format_parse_error_summary

        error_summary = format_parse_error_summary(result.errors)
        raise ToolError(
            f"PRD parse failed with {len(result.errors)} error(s); "
            f"fix prd.md and call parse_prd before find_decisions. {error_summary}"
        )

    decisions = find_unresolved_decisions(
        source.markdown,
        prd=result.prd,
        tasks=tasks_or_none,
    )

    entries = [
        UnresolvedDecisionEntry(
            id=d.id,
            kind=d.kind.value,
            location=d.location,
            text=d.text,
            context_paragraph=d.context_paragraph,
            suggested_resolution_field=d.suggested_resolution_field,
        )
        for d in decisions
    ]

    counts: dict[str, int] = {
        "needs_decision": 0,
        "open_question": 0,
        "missing_field": 0,
    }
    for d in decisions:
        counts[d.kind.value] = counts.get(d.kind.value, 0) + 1

    return FindDecisionsResponse(
        prd_id=effective_prd_id,
        prd_source=effective_prd_id,
        decisions=entries,
        counts_by_kind=counts,
        total=len(entries),
    )


# ---------------------------------------------------------------------------
# Bundle execution and planning contract (issue #171)
# ---------------------------------------------------------------------------


def _review_gate_response(gate: Any) -> BundleReviewGateResponse:
    return BundleReviewGateResponse.model_validate(gate.__dict__)


def _bundle_record(bundle: Any) -> BundleRecord:
    return BundleRecord.model_validate(bundle.model_dump(mode="json"))


def _bundle_claim_record(claim: Any) -> BundleClaimRecord:
    return BundleClaimRecord.model_validate(claim.model_dump(mode="json"))


def _bundle_review_record(review: Any) -> BundleReviewVerdictRecord:
    return BundleReviewVerdictRecord.model_validate(review.model_dump(mode="json"))


def _bundle_checkpoint_record(checkpoint: Any) -> BundleCheckpointRecord:
    return BundleCheckpointRecord.model_validate(checkpoint.model_dump(mode="json"))


def _bundle_manager(
    backend: Any,
    state_dir: Path,
    actor: str,
    lease_minutes: float = 240,
    cwd: str | None = None,
    *,
    new_claim: bool = False,
):
    from anvil.bundles.manager import BundleManager
    from anvil.cli._helpers import _resolve_project_root
    from anvil.clock import SystemClock

    return BundleManager(
        backend,
        SystemClock(),
        actor=(
            _require_actor(actor)
            if new_claim
            else _exact_lifecycle_actor(actor)
        ),
        project_root=_resolve_project_root(Path(cwd) if cwd else None),
        lease_minutes=lease_minutes,
    )


@mcp.tool(tags={PLANNING_TAG})
def create_bundle(
    bundle_id: str,
    prd_id: str,
    task_ids: list[str],
    coordinator: str,
    actor: str,
    max_tasks: int = 12,
    max_serial_stages: int = 6,
    max_reviews: int = 3,
    max_rereviews: int = 1,
    required_angles: list[str] | None = None,
    cwd: str | None = None,
) -> BundleDetailResponse:
    """Create one planned bundle. Planning-gated; ordered task_ids are preserved."""
    from anvil.bundles.catalog import BundleCatalog, BundleCatalogError
    from anvil.clock import SystemClock
    from anvil.state.models import BundleReviewPolicy, BundleThroughputBudget

    state_dir = _resolve_state_dir(cwd)
    backend = _open_backend(state_dir)
    try:
        try:
            bundle = BundleCatalog(
                backend, SystemClock(), actor=_require_actor(actor)
            ).create(
                bundle_id,
                prd_id=prd_id,
                task_ids=task_ids,
                # Preserve the exact caller spelling so BundleCatalog can pass
                # it through to SQLite's locked NFC-collision check. The
                # catalog canonicalizes the persisted coordinator itself.
                coordinator=coordinator,
                review_policy=BundleReviewPolicy(
                    max_reviews=max_reviews,
                    max_rereviews=max_rereviews,
                    required_angles=required_angles or [],
                ),
                throughput_budget=BundleThroughputBudget(
                    max_tasks=max_tasks,
                    max_serial_stages=max_serial_stages,
                ),
            )
        except (BundleCatalogError, ValueError) as exc:
            raise ToolError(f"bundle_error: {exc}") from exc
        return BundleDetailResponse(bundle=_bundle_record(bundle))
    finally:
        backend.close()


@mcp.tool
def list_bundles(
    prd_id: str | None = None, cwd: str | None = None
) -> BundleListResponse:
    """List execution bundles in stable ID order."""
    backend = _open_backend(_resolve_state_dir(cwd))
    try:
        return BundleListResponse(
            bundles=[
                _bundle_record(bundle)
                for bundle in backend.list_bundles(prd_id=prd_id)
            ]
        )
    finally:
        backend.close()


@mcp.tool
def get_bundle(bundle_id: str, cwd: str | None = None) -> BundleDetailResponse:
    """Read one bundle with its coordinator claim and review history."""
    from anvil.bundles.catalog import BundleCatalog, BundleCatalogError
    from anvil.clock import SystemClock

    backend = _open_backend(_resolve_state_dir(cwd))
    try:
        try:
            bundle = BundleCatalog(backend, SystemClock(), actor="reader").get(bundle_id)
        except BundleCatalogError as exc:
            raise ToolError(f"bundle_error: {exc}") from exc
        claim = backend.get_bundle_claim(bundle_id)
        return BundleDetailResponse(
            bundle=_bundle_record(bundle),
            claim=_bundle_claim_record(claim) if claim else None,
            reviews=[
                _bundle_review_record(review)
                for review in backend.list_bundle_reviews(bundle_id)
            ],
        )
    finally:
        backend.close()


@mcp.tool
def claim_bundle(
    bundle_id: str,
    actor: str,
    lease_minutes: float = 240,
    shared_tree: bool = False,
    cwd: str | None = None,
) -> BundleClaimResponse:
    """Atomically claim a bundle and create internal member authorizations."""
    from anvil.bundles.manager import BundleError
    from anvil.cli._helpers import _resolve_project_dir
    from anvil.git_ops import (
        ClaimGitMutationTracker,
        ClaimPlanError,
        apply_claim_plan,
        claim_git_metadata,
        compensate_claim_plan_tracker,
        finalize_claim_plan_tracker,
        resolve_claim_plan,
        revalidate_claim_plan,
    )

    state_dir = _resolve_state_dir(cwd)
    backend = _open_backend(state_dir)
    try:
        _reap_stale(backend)
        from anvil.cli._helpers import _load_config_optional

        cfg = _load_config_optional(state_dir)
        isolation = cfg.worktree_isolation if cfg is not None else "advisory"
        warnings: list[str] = []
        if not shared_tree and isolation == "require":
            raise ToolError(
                "bundle_error: worktree_isolation: require; claim through "
                "`anvil claim --bundle --worktree`, or pass shared_tree=true."
            )
        if not shared_tree and isolation == "advisory":
            shared = [
                claim
                for claim in backend.list_active_claims()
                if not claim.worktree_path
            ]
            if shared:
                warnings.append(
                    f"{len(shared)} active task claim(s) share this checkout; "
                    "prefer the CLI worktree claim path."
                )
        project_dir = _resolve_project_dir(Path(cwd) if cwd else None)
        manager = _bundle_manager(
            backend,
            state_dir,
            actor,
            lease_minutes=lease_minutes,
            cwd=cwd,
            new_claim=True,
        )
        try:
            plan = resolve_claim_plan(
                bundle_id,
                f"Bundle {bundle_id}",
                cwd=project_dir,
                branch_prefix=cfg.branch_prefix if cfg is not None else "agent",
                shared_tree=shared_tree,
                ignored_worktree_paths=(state_dir,),
            )
            metadata = claim_git_metadata(plan)
            mutation_tracker = ClaimGitMutationTracker(plan)
            with backend.claim_operation_lock():
                revalidate_claim_plan(plan, cwd=project_dir)
                result = manager.claim(
                    bundle_id,
                    branch=metadata.branch if metadata is not None else None,
                    worktree_path=(
                        metadata.worktree_path if metadata is not None else None
                    ),
                    git_metadata=metadata,
                )
                try:
                    apply_claim_plan(plan, cwd=project_dir, tracker=mutation_tracker)
                except BaseException:
                    try:
                        manager.release(
                            bundle_id,
                            reason="transactional Git claim failed",
                        )
                    finally:
                        compensate_claim_plan_tracker(
                            mutation_tracker, cwd=project_dir
                        )
                    raise
                finalize_claim_plan_tracker(
                    mutation_tracker, cwd=project_dir
                )
        except ClaimPlanError as exc:
            raise ToolError(f"bundle_error: {exc.code}: {exc}") from exc
        except (BundleError, ValueError) as exc:
            raise ToolError(f"bundle_error: {exc}") from exc
        return BundleClaimResponse(
            bundle=_bundle_record(result.bundle),
            claim=_bundle_claim_record(result.claim),
            warnings=warnings,
            actor_identity=actor_identity_data(result.claim.claimed_by),
            continuation=bundle_continuation_data(
                bundle_id, result.claim.claimed_by
            ),
        )
    finally:
        backend.close()


@mcp.tool
def generate_bundle_packet(
    bundle_id: str,
    actor: str,
    format: Literal["markdown", "json"] = "markdown",
    cwd: str | None = None,
) -> WorkPacketResponse:
    """Render the coordinator packet for an execution bundle."""
    from anvil.bundles.manager import BundleError

    state_dir = _resolve_state_dir(cwd)
    backend = _open_backend(state_dir)
    try:
        try:
            packet = _bundle_manager(backend, state_dir, actor, cwd=cwd).packet(
                bundle_id
            )
        except BundleError as exc:
            raise ToolError(f"bundle_error: {exc}") from exc
        return WorkPacketResponse(
            format=format,
            content=packet.markdown if format == "markdown" else packet.json_data,
        )
    finally:
        backend.close()


@mcp.tool
def submit_bundle_progress(
    bundle_id: str,
    actor: str,
    phase: str,
    detail: str | None = None,
    member_task_ids: list[str] | None = None,
    complete: bool = False,
    cwd: str | None = None,
) -> BundleProgressResponse:
    """Record progress; complete=true opens review after evidence is proven."""
    from anvil.bundles.manager import BundleError

    state_dir = _resolve_state_dir(cwd)
    backend = _open_backend(state_dir)
    try:
        try:
            manager = _bundle_manager(backend, state_dir, actor, cwd=cwd)
            if complete:
                # Completion is a retry-safe gate, not a progress mutation.
                # Do not append progress before proving readiness: a failed
                # completion must leave history unchanged, and an identical
                # retry after success must remain idempotent like CLI complete.
                readiness = manager.mark_implemented(bundle_id)
            else:
                manager.note_progress(
                    bundle_id,
                    phase=phase,
                    detail=detail,
                    member_task_ids=member_task_ids,
                )
                readiness = None
        except BundleError as exc:
            raise ToolError(f"bundle_error: {exc}") from exc
        if readiness is not None and not readiness.can_mark_implemented:
            raise ToolError(
                "bundle_not_ready: "
                + json.dumps(readiness.unproven_members, sort_keys=True)
            )
        bundle = backend.get_bundle(bundle_id)
        assert bundle is not None
        return BundleProgressResponse(
            bundle=_bundle_record(bundle),
            can_mark_implemented=(
                readiness.can_mark_implemented if readiness is not None else None
            ),
            unproven_members=(
                readiness.unproven_members if readiness is not None else {}
            ),
        )
    finally:
        backend.close()


@mcp.tool
def record_bundle_review(
    bundle_id: str,
    actor: str,
    review_round: int,
    angle: str,
    decision: Literal["approve", "reject", "needs_changes"],
    notes: str | None = None,
    cwd: str | None = None,
) -> BundleReviewResponse:
    """Record one independent review verdict and return the current gate."""
    from anvil.bundles.review import BundleReviewError, BundleReviewManager
    from anvil.clock import SystemClock
    from anvil.state.models import ReviewDecision

    backend = _open_backend(_resolve_state_dir(cwd))
    try:
        try:
            gate = BundleReviewManager(
                backend, SystemClock(), actor=_require_actor(actor)
            ).record(
                bundle_id,
                review_round=review_round,
                angle=angle,
                decision=ReviewDecision(decision),
                notes=notes,
            )
            bundle = backend.get_bundle(bundle_id)
        except (BundleReviewError, ValueError) as exc:
            raise ToolError(f"bundle_error: {exc}") from exc
        assert bundle is not None
        return BundleReviewResponse(
            bundle=_bundle_record(bundle), gate=_review_gate_response(gate)
        )
    finally:
        backend.close()


@mcp.tool
def finalize_bundle_review(
    bundle_id: str, actor: str, cwd: str | None = None
) -> BundleReviewResponse:
    """Apply a completed bounded review gate as the coordinator."""
    from anvil.bundles.review import BundleReviewError, BundleReviewManager
    from anvil.clock import SystemClock

    backend = _open_backend(_resolve_state_dir(cwd))
    try:
        try:
            gate = BundleReviewManager(
                backend, SystemClock(), actor=_exact_lifecycle_actor(actor)
            ).finalize(bundle_id)
            bundle = backend.get_bundle(bundle_id)
        except BundleReviewError as exc:
            raise ToolError(f"bundle_error: {exc}") from exc
        assert bundle is not None
        return BundleReviewResponse(
            bundle=_bundle_record(bundle), gate=_review_gate_response(gate)
        )
    finally:
        backend.close()


@mcp.tool
def checkpoint_bundle(
    bundle_id: str,
    actor: str,
    commit_sha: str | None = None,
    pr_url: str | None = None,
    cwd: str | None = None,
) -> BundleCheckpointResponse:
    """Record canonical commit or PR delivery metadata."""
    from anvil.bundles.delivery import BundleDeliveryError, BundleDeliveryManager
    from anvil.clock import SystemClock

    backend = _open_backend(_resolve_state_dir(cwd))
    try:
        try:
            checkpoint = BundleDeliveryManager(
                backend, SystemClock(), actor=_exact_lifecycle_actor(actor)
            ).checkpoint(bundle_id, commit_sha=commit_sha, pr_url=pr_url)
            bundle = backend.get_bundle(bundle_id)
        except (BundleDeliveryError, ValueError) as exc:
            raise ToolError(f"bundle_error: {exc}") from exc
        assert bundle is not None
        return BundleCheckpointResponse(
            bundle=_bundle_record(bundle),
            checkpoint=_bundle_checkpoint_record(checkpoint),
        )
    finally:
        backend.close()


@mcp.tool
def reconcile_bundle(
    bundle_id: str,
    actor: str,
    commit_sha: str | None = None,
    pr_url: str | None = None,
    merged: bool = False,
    cwd: str | None = None,
) -> BundleDetailResponse:
    """Idempotently reconcile checkpoint and integration delivery state."""
    from anvil.bundles.delivery import BundleDeliveryError, BundleDeliveryManager
    from anvil.clock import SystemClock

    backend = _open_backend(_resolve_state_dir(cwd))
    try:
        try:
            BundleDeliveryManager(
                backend, SystemClock(), actor=_exact_lifecycle_actor(actor)
            ).reconcile(
                bundle_id, commit_sha=commit_sha, pr_url=pr_url, merged=merged
            )
            bundle = backend.get_bundle(bundle_id)
        except (BundleDeliveryError, ValueError) as exc:
            raise ToolError(f"bundle_error: {exc}") from exc
        assert bundle is not None
        return BundleDetailResponse(bundle=_bundle_record(bundle))
    finally:
        backend.close()


@mcp.tool
def supersede_bundle(
    bundle_id: str,
    replacement_bundle_id: str,
    actor: str,
    cwd: str | None = None,
) -> BundleDetailResponse:
    """Supersede a bundle with a named replacement while retaining history."""
    from anvil.bundles.delivery import BundleDeliveryError, BundleDeliveryManager
    from anvil.clock import SystemClock

    backend = _open_backend(_resolve_state_dir(cwd))
    try:
        try:
            BundleDeliveryManager(
                backend, SystemClock(), actor=_exact_lifecycle_actor(actor)
            ).supersede(bundle_id, replacement_bundle_id=replacement_bundle_id)
            bundle = backend.get_bundle(bundle_id)
        except (BundleDeliveryError, ValueError) as exc:
            raise ToolError(f"bundle_error: {exc}") from exc
        assert bundle is not None
        return BundleDetailResponse(bundle=_bundle_record(bundle))
    finally:
        backend.close()


# ---------------------------------------------------------------------------
# describe_surface (self-describing command surface — T012)
# ---------------------------------------------------------------------------


@mcp.tool(tags={PLANNING_TAG})
def describe_surface() -> dict[str, Any]:
    """Return a machine-readable manifest of the anvil command surface: the CLI
    subcommands and MCP tool names this engine exposes, plus engine version,
    schema version, and a stable ``api_version`` to pin against. Introspected
    live, needs no project. Lets an MCP-only host discover the surface."""
    # Imported lazily and reused so the CLI and MCP surfaces report the IDENTICAL
    # manifest (single source of truth — no second hand-maintained list).
    from anvil.cli.describe import build_manifest

    return build_manifest()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

# A one-line usage string shared by ``--help`` and the unknown-flag error path.
_USAGE = "usage: python -m anvil.mcp_server [--help] [--version]"


def _help_text() -> str:
    """Render the ``--help`` page.

    Deliberately self-contained (no project/backend access) so it works inside
    a bare Docker image where no ``.anvil/`` exists yet. The tool list is
    introspected live from the registered FastMCP surface so it can never drift
    from reality. ``ANVIL_ROOT`` is documented here because the container
    image resolves project state through it (a bind-mounted host directory).
    """
    from anvil.build_identity import get_build_identity
    from anvil.cli.describe import mcp_tool_names

    tools = mcp_tool_names()
    identity = get_build_identity()
    lines = [
        f"anvil-mcp {identity.display_version} — FastMCP (stdio) server",
        "",
        _USAGE,
        "",
        "Run with no arguments to start the stdio MCP server (the default; this",
        "is what an MCP client launches). --help and --version print and exit 0",
        "without opening a backend, so they are safe as a container smoke test.",
        "",
        "Options:",
        "  -h, --help      Show this help and exit.",
        "  -v, --version   Print the engine version and exit.",
        "",
        "Environment:",
        "  ANVIL_ROOT  Project root holding .anvil/ (defaults to the",
        "                     current working directory). In Docker, bind-mount the",
        "                     host project here, e.g. -v \"$PWD:/project\" -e",
        "                     ANVIL_ROOT=/project.",
        "  ANVIL_MCP_PLANNING  When truthy (1/true/yes/on), the live server",
        "                     exposes the full 36-tool surface. By DEFAULT the 12",
        "                     one-shot planning tools (init_project, parse_prd, assess_prd,",
        "                     review_prd, plan_tasks, score_tasks, review_tasks,",
        "                     apply_review_decision, edit_dependencies,",
        "                     find_decisions, describe_surface, create_bundle) are hidden from the",
        "                     per-turn wire surface to cut always-on context; the",
        "                     24 execution tools remain. All 36 are always",
        "                     registered (this list reflects the full surface).",
        "",
        f"Registered tools ({len(tools)}):",
    ]
    lines += [f"  {name}" for name in tools]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for the MCP server.

    With no recognised flags this starts the blocking stdio server (the default
    an MCP client invokes) and never returns. ``--help``/``--version`` short-
    circuit before ``mcp.run()`` so a container smoke test
    (``docker run --rm anvil-mcp --help``) prints and exits cleanly
    instead of hanging on stdio. Backward-compatible: the no-arg path is
    unchanged.
    """
    args = sys.argv[1:] if argv is None else argv

    if any(a in ("-h", "--help") for a in args):
        print(_help_text())
        return 0

    if any(a in ("-v", "--version") for a in args):
        from anvil.build_identity import get_build_identity

        print(get_build_identity().display_version)
        return 0

    # Reject unknown flags rather than silently ignoring them and starting the
    # server — a typo'd flag should fail fast, not block on stdio forever.
    unknown = [a for a in args if a.startswith("-")]
    if unknown:
        print(f"anvil-mcp: unrecognized arguments: {' '.join(unknown)}", file=sys.stderr)
        print(_USAGE, file=sys.stderr)
        return 2

    # L2: hide the one-shot planning tool surface on the live wire UNLESS the
    # operator opts back in via ANVIL_MCP_PLANNING. This shrinks the always-on
    # per-turn cost for the common execution client without removing any tool —
    # all 36 stay registered (introspection/--help/describe unchanged) and the
    # planning 12 return the moment the flag is set. Applied here, not at import,
    # so only the live server's wire surface is affected.
    if not apply_surface_gate(mcp):
        print(
            "anvil-mcp: planning tools hidden (execution surface only). "
            f"Set {_PLANNING_ENV}=1 to expose the full 36-tool surface for "
            "planning.",
            file=sys.stderr,
        )

    mcp.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
