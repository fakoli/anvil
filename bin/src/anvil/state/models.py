"""Pydantic v2 models for anvil — the single source of truth for all entity types.

All other modules (sqlite backend, MCP tools, work-packet renderer, review gates)
import from here. If the types change, everything downstream changes with them.

Design decisions:
- StrEnum for every status / kind / decision field: grep-able, serialisable to str.
- All datetimes are timezone-aware UTC; a model_validator enforces tzinfo presence.
- Score dimensions are nullable until explicitly scored; Field(ge=1, le=5) when set.
- Type aliases (TaskID, FeatureID, …) are plain str — no over-engineering, but they
  give search-grep ability and document intent at every call site.
- Most state models use ConfigDict(frozen=False, validate_assignment=True,
  extra='forbid') so mutable transitions remain assignment-validated. PRD is a
  frozen provenance value object and updates only through validated_copy().
"""

from __future__ import annotations

import base64
import binascii
import copy
import datetime
import enum
import hashlib
import json
import re
from collections.abc import Iterable
from typing import (  # noqa: UP035 — TypeAlias required for 3.11 compat
    Annotated,
    Any,
    Literal,
    Mapping,
    Self,
    SupportsIndex,
    TypeAlias,
)

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictBytes,
    StrictInt,
    StrictStr,
    field_serializer,
    field_validator,
    model_serializer,
    model_validator,
)

__all__ = [
    # Type aliases
    "TaskID",
    "FeatureID",
    "RequirementID",
    "ClaimID",
    "EvidenceID",
    "DecisionID",
    "ReviewID",
    "EventID",
    "PRDID",
    "BundleID",
    # Constants
    "DEFAULT_PRD_ID",
    "TERMINAL_BUNDLE_STATUSES",
    "MAX_CLAIM_COMMAND_BYTES",
    "MAX_CLAIM_COMMAND_OUTPUT_BYTES",
    "MAX_CLAIM_COMMAND_PROOF_ARTIFACT_BYTES",
    "MAX_CLAIM_COMMAND_PROOF_BATCH_ITEMS",
    "MAX_CLAIM_COMMAND_PROOF_BATCH_BYTES",
    # Enums
    "PRDStatus",
    "FeatureStatus",
    "TaskStatus",
    "TaskPriority",
    "TaskType",
    "ClaimType",
    "ClaimStatus",
    "BundleStatus",
    "DelegatedAgentStatus",
    "ReviewTargetKind",
    "ReviewDecision",
    "RejectionCategory",
    "RejectionReasonCode",
    "RejectionQualityFinding",
    "RejectionQualityFindingCode",
    "RejectionProcessPredicate",
    "ExternalSystem",
    "KNOWN_EXTERNAL_SYSTEMS",
    "SyncState",
    "ConflictResolutionStrategy",
    "ProofKind",
    # Models
    "Score",
    "Verification",
    "HookCommandAttribution",
    "CommandProof",
    "hook_command_semantic_digest",
    "hook_command_semantic_projection",
    "task_snapshot_revision",
    "ClaimCommandEvidenceCore",
    "ClaimCommandIssuer",
    "ClaimCommandProof",
    "claim_command_semantic_projection",
    "DiffProof",
    "LinkProof",
    "AssertionProof",
    "ProofArtifact",
    "ProofRequirement",
    "Project",
    "PRDAssumption",
    "PRD",
    "Requirement",
    "Feature",
    "Task",
    "ClaimExpectedPathBaseline",
    "ClaimAttestationContext",
    "ClaimProgressAttestation",
    "Claim",
    "BundleClaim",
    "Evidence",
    "BundleReviewPolicy",
    "BundleReviewVerdict",
    "BundleThroughputBudget",
    "DelegatedAgentObservation",
    "BundleCheckpoint",
    "ExecutionBundle",
    "EventRange",
    "AcceptanceProof",
    "Decision",
    "Review",
    "TaskRejectionProvenance",
    "supporting_evidence_digest",
    "EventDraft",
    "Event",
    "SyncMapping",
    "ConflictGroup",
]

# ---------------------------------------------------------------------------
# Type aliases — plain str newtypes for search-grep ability.
# ---------------------------------------------------------------------------

TaskID: TypeAlias = str
FeatureID: TypeAlias = str
RequirementID: TypeAlias = str
ClaimID: TypeAlias = str
EvidenceID: TypeAlias = str
DecisionID: TypeAlias = str
ReviewID: TypeAlias = str
EventID: TypeAlias = str  # monotonic E000001 (local) or hash-chained E-3f9a2c4d71be (git)
# PRD identity: 'default' for the implicit/migrated PRD, human-chosen
# (e.g. 'v0.2') for named PRDs.
PRDID: TypeAlias = str
BundleID: TypeAlias = str

# The single default PRD that owns all rows on a pre-multi-PRD (migrated) DB.
DEFAULT_PRD_ID = "default"

# v1.22.0 — git-backed events (Phase A). Hash-chained event ids are
# "E-" + sha256(parent_id ‖ canonical_json(payload) ‖ actor ‖ ts)[:12];
# see anvil.state.hashing for the generator. 12 lowercase hex chars,
# anchored, so a truncated/hand-mangled id fails validation instead of
# silently entering the chain.
_HASH_EVENT_ID_RE = re.compile(r"^E-[0-9a-f]{12}$")

# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class PRDStatus(enum.StrEnum):
    draft = "draft"
    reviewed = "reviewed"
    approved = "approved"
    rejected = "rejected"


class FeatureStatus(enum.StrEnum):
    proposed = "proposed"
    ready = "ready"
    in_progress = "in_progress"
    done = "done"


class TaskStatus(enum.StrEnum):
    proposed = "proposed"
    drafted = "drafted"
    reviewed = "reviewed"
    ready = "ready"
    claimed = "claimed"
    in_progress = "in_progress"
    blocked = "blocked"
    needs_review = "needs_review"
    accepted = "accepted"
    done = "done"
    rejected = "rejected"


# Statuses meaning "finished — no work left". ``rejected`` is deliberately NOT
# terminal: rejection auto-promotes back to ``drafted`` for rework (see
# ``_handle_task_applied`` in state/sqlite.py), so a task *resting* at
# ``rejected`` (legacy DB or crashed loop) is stuck open work, not a finished
# task. Single source of truth — `list --open`, sync reconciliation, and any
# future surface import this rather than hand-rolling their own set.
TERMINAL_TASK_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.accepted, TaskStatus.done}
)


class TaskPriority(enum.StrEnum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class TaskType(enum.StrEnum):
    """What kind of change a task represents.

    The default ``feature`` preserves backward compatibility — every task
    created before this enum existed deserialises as ``feature`` (the column
    default and the model default both point at it), so the loop behaves
    exactly as it did before for greenfield feature work.

    The non-feature kinds let a brownfield / maintenance PRD describe work that
    is not net-new capability:

    - ``bugfix``  — repair incorrect behaviour in existing code.
    - ``refactor`` — restructure without changing observable behaviour.
    - ``modify``  — change existing behaviour (tweak, extend, re-tune).

    The kind flows through plan → score → claim → work-packet → evidence. It is
    advisory: a small ``modify`` is allowed to ride the lightweight work-packet
    variant (see :func:`anvil.context.packets.is_lightweight`), while a
    high-blast-radius ``refactor`` still gets the full packet.
    """

    feature = "feature"
    bugfix = "bugfix"
    refactor = "refactor"
    modify = "modify"


class ClaimType(enum.StrEnum):
    task = "task"
    feature = "feature"
    file_scope = "file_scope"
    exploratory = "exploratory"


class ClaimStatus(enum.StrEnum):
    active = "active"
    released = "released"
    stale = "stale"
    force_released = "force_released"


class BundleStatus(enum.StrEnum):
    """Coordinator-level delivery state; member Task state remains authoritative."""

    planned = "planned"
    active = "active"
    implemented_unreviewed = "implemented_unreviewed"
    reviewed_unintegrated = "reviewed_unintegrated"
    integrated = "integrated"
    merged = "merged"
    replan_required = "replan_required"
    completed = "completed"
    superseded = "superseded"


TERMINAL_BUNDLE_STATUSES: frozenset[BundleStatus] = frozenset(
    {BundleStatus.merged, BundleStatus.completed, BundleStatus.superseded}
)


class DelegatedAgentStatus(enum.StrEnum):
    """Observed harness handle state; informational and never a lifecycle gate."""

    active = "active"
    completed = "completed"
    stale = "stale"
    closed = "closed"
    missing = "missing"


class ReviewTargetKind(enum.StrEnum):
    prd = "prd"
    task = "task"
    feature = "feature"


class ReviewDecision(enum.StrEnum):
    approve = "approve"
    reject = "reject"
    needs_changes = "needs_changes"


class RejectionCategory(enum.StrEnum):
    """Engine-derived accounting class for a rejected task review."""

    quality = "quality"
    evidence_resubmission = "evidence_resubmission"
    process = "process"


class RejectionReasonCode(enum.StrEnum):
    """Bounded reviewer assertion that the engine independently verifies."""

    unspecified_quality = "unspecified_quality"
    quality_findings = "quality_findings"
    evidence_incomplete = "evidence_incomplete"
    claim_stale = "claim_stale"
    claim_force_released = "claim_force_released"
    bundle_review_required = "bundle_review_required"


class RejectionQualityFindingCode(enum.StrEnum):
    """Typed quality dimensions; explanatory prose remains in review notes."""

    correctness = "correctness"
    security = "security"
    tests = "tests"
    scope = "scope"
    maintainability = "maintainability"
    documentation = "documentation"
    performance = "performance"
    other = "other"


class RejectionProcessPredicate(enum.StrEnum):
    """Persisted engine facts eligible for a non-quality process rejection."""

    claim_status_stale = "claim_status_stale"
    claim_status_force_released = "claim_status_force_released"
    bundle_member_claim = "bundle_member_claim"


class ExternalSystem(enum.StrEnum):
    """Canonical names for first-party sync providers shipped with
    anvil.

    Kept as a reference enum (so ``ExternalSystem.github_issues`` still
    evaluates to ``"github_issues"`` for code that wants the constant),
    but ``SyncMapping.external_system`` is typed as ``str`` so that
    contributor-registered providers (e.g. ``"monday"``, ``"linear"``,
    ``"my_custom_tracker"``) can persist mappings without first having
    to patch this enum.

    See also :data:`KNOWN_EXTERNAL_SYSTEMS` for the tuple form used by
    docs / introspection.
    """

    github_issues = "github_issues"


# Tuple form of the canonical first-party provider ids. Used for docs
# and introspection; the SyncMapping DB column accepts any string so
# contributor providers are not gated on inclusion here.
KNOWN_EXTERNAL_SYSTEMS: tuple[str, ...] = tuple(s.value for s in ExternalSystem)


class SyncState(enum.StrEnum):
    in_sync = "in_sync"
    local_ahead = "local_ahead"
    remote_ahead = "remote_ahead"
    conflict = "conflict"
    external_deleted = "external_deleted"
    remote_unknown = "remote_unknown"


class ConflictResolutionStrategy(enum.StrEnum):
    local_wins = "local_wins"
    remote_wins = "remote_wins"
    prompt = "prompt"
    manual_merge = "manual_merge"


# ---------------------------------------------------------------------------
# Shared config for all models
# ---------------------------------------------------------------------------

_MODEL_CONFIG = ConfigDict(
    frozen=False,
    validate_assignment=True,
    extra="forbid",
)


def _require_utc(dt: datetime.datetime, field_name: str) -> datetime.datetime:
    """Raise ValueError if dt is naive (no tzinfo)."""
    if dt.tzinfo is None:
        raise ValueError(
            f"{field_name} must be timezone-aware (UTC); "
            f"got naive datetime {dt!r}. "
            "Use datetime.datetime.now(datetime.timezone.utc) or "
            "datetime.datetime(..., tzinfo=datetime.timezone.utc)."
        )
    return dt


# ---------------------------------------------------------------------------
# Embedded value objects
# ---------------------------------------------------------------------------


class Score(BaseModel):
    """Six-dimension scoring for a Task. All dimensions are 1-5 or None until scored."""

    model_config = _MODEL_CONFIG

    complexity: int | None = Field(default=None, ge=1, le=5)
    parallelizability: int | None = Field(default=None, ge=1, le=5)
    context_load: int | None = Field(default=None, ge=1, le=5)
    blast_radius: int | None = Field(default=None, ge=1, le=5)
    review_risk: int | None = Field(default=None, ge=1, le=5)
    agent_suitability: int | None = Field(default=None, ge=1, le=5)
    explanation: str | None = None
    # B45 — risk-axis eligibility (safe-by-construction). False means the
    # blast_radius / review_risk score is a heuristic (filename regex / base)
    # only, NOT human-or-LLM-confirmed. A ceilinged `anvil next --max-blast /
    # --max-review-risk` treats an unconfirmed (or unscored) task as
    # frontier-only — ineligible even if the number is within the ceiling — so
    # the filter fails safe, never routing weakly-scored risk to a local runner.
    # Defaults False; a confirmation source (a trusted risk label) is a follow-up.
    blast_radius_confirmed: bool = False
    review_risk_confirmed: bool = False


# ---------------------------------------------------------------------------
# Typed proof model (SL-3 / B48 acceptance 2) — additive, non-breaking.
#
# A proof is a TYPED record of a command result, diff, link, or an explicit
# honour-system assertion. ``CommandProof`` is the load-bearing one: it carries
# a real ``exit_code``, so a requirement can demand "command X exited 0" and a
# free-text claim written into a description/output field cannot satisfy it —
# that specific hole is closed.
#
# TRUST BOUNDARY (read before relying on this for unattended work): a
# CommandProof is only as trustworthy as whatever WROTE it. It originates from
# the per-claim evidence buffer that the PostToolUse capture hook appends to;
# ``output_sha256`` is recorded but the engine does NOT re-run the command or
# re-hash its output, so the proof is *tamper-evident in transit*, NOT
# *independently re-executed*. In a harness where the gated agent can write the
# evidence buffer, a determined agent can fabricate a passing CommandProof.
# Hardening (re-verify output / out-of-tree append-only buffer / trusted writer)
# is tracked in docs/tech-debt-backlog.md. See docs/specs/2026-06-19-sl3-proofartifact.md.
# ---------------------------------------------------------------------------


class ProofKind(enum.StrEnum):
    """Discriminator for the ``ProofArtifact`` union. str-serialisable (house rule)."""

    command = "command"
    diff = "diff"
    link = "link"
    assertion = "assertion"


_HOOK_COMMAND_PROOF_SEMANTIC_DOMAIN = b"anvil.hook-command-proof.v1\0"
_TASK_SNAPSHOT_REVISION_DOMAIN = b"anvil.progress-task.v1\0"


class HookCommandAttribution(BaseModel):
    """Immutable claim identity captured with one legacy hook command proof.

    This keeps the hook trust boundary explicit: the hook writer is still the
    source of the observation, but actor/claim ownership can no longer be lost
    when the buffer record is imported into durable evidence.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    project_id: StrictStr = Field(min_length=1, max_length=255)
    claim_id: StrictStr = Field(min_length=1, max_length=255)
    generation: StrictInt = Field(ge=1)
    claimed_by: StrictStr = Field(min_length=1, max_length=4096)
    task_id: StrictStr = Field(min_length=1, max_length=255)
    task_revision: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    prd_id: StrictStr = Field(min_length=1, max_length=255)
    prd_revision: StrictInt = Field(ge=1)
    repository_id: StrictStr | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    claim_start_sha: StrictStr | None = Field(
        default=None, pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"
    )

    @model_validator(mode="after")
    def _validate_repository_binding_pair(self) -> HookCommandAttribution:
        if (self.repository_id is None) != (self.claim_start_sha is None):
            raise ValueError(
                "hook command repository identity and claim start SHA must be "
                "supplied together"
            )
        return self


def task_snapshot_revision(task_snapshot: Any) -> str:
    """Return the deterministic semantic revision used by claim proof bindings."""
    from anvil.state.hashing import (
        canonical_node_budget_for_bytes,
        domain_separated_sha256,
    )

    material = (
        task_snapshot.model_dump(mode="json")
        if isinstance(task_snapshot, BaseModel)
        else dict(task_snapshot)
        if isinstance(task_snapshot, Mapping)
        else task_snapshot
    )
    max_bytes = MAX_CLAIM_COMMAND_PROOF_ARTIFACT_BYTES
    return domain_separated_sha256(
        _TASK_SNAPSHOT_REVISION_DOMAIN,
        material,
        max_bytes=max_bytes,
        max_nodes=canonical_node_budget_for_bytes(max_bytes),
        max_string_bytes=max_bytes,
    )


def hook_command_semantic_projection(
    *,
    attribution: HookCommandAttribution,
    command: str,
    exit_code: int,
    output_sha256: str,
    captured_at: datetime.datetime,
) -> dict[str, Any]:
    """Return the canonical identity of one hook-observed command result."""
    captured_at = _require_utc(captured_at, "captured_at")
    return {
        "schema_version": 1,
        "attribution": attribution.model_dump(mode="json"),
        "command": command,
        "exit_code": exit_code,
        "output_sha256": output_sha256,
        "captured_at": captured_at.astimezone(datetime.UTC)
        .isoformat()
        .replace("+00:00", "Z"),
    }


def hook_command_semantic_digest(
    *,
    attribution: HookCommandAttribution,
    command: str,
    exit_code: int,
    output_sha256: str,
    captured_at: datetime.datetime,
) -> str:
    """Hash a hook proof together with its exact durable claim attribution."""
    from anvil.state.hashing import (
        canonical_node_budget_for_bytes,
        domain_separated_sha256,
    )

    max_bytes = MAX_CLAIM_COMMAND_PROOF_ARTIFACT_BYTES
    return domain_separated_sha256(
        _HOOK_COMMAND_PROOF_SEMANTIC_DOMAIN,
        hook_command_semantic_projection(
            attribution=attribution,
            command=command,
            exit_code=exit_code,
            output_sha256=output_sha256,
            captured_at=captured_at,
        ),
        max_bytes=max_bytes,
        max_nodes=canonical_node_budget_for_bytes(max_bytes),
        max_string_bytes=max_bytes,
    )


class CommandProof(BaseModel):
    """A typed command result: command, real exit code, and an output hash.

    Captured by the PostToolUse hook and reconciled at submit. Authenticity
    depends on a trusted hook writer (output_sha256 is recorded, not
    re-verified) — see the TRUST BOUNDARY note above.
    """

    model_config = _MODEL_CONFIG

    kind: Literal[ProofKind.command] = ProofKind.command
    command: str
    exit_code: int
    output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    captured_at: datetime.datetime
    # Both fields are absent only on pre-T008.1 historical events. New live
    # submissions require them at the authoritative append boundary.
    attribution: HookCommandAttribution | None = None
    semantic_digest: StrictStr | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )

    @field_validator("captured_at", mode="after")
    @classmethod
    def _validate_utc(cls, v: datetime.datetime) -> datetime.datetime:
        return _require_utc(v, "captured_at")

    @model_validator(mode="after")
    def _validate_attributed_material(self) -> CommandProof:
        if (self.attribution is None) != (self.semantic_digest is None):
            raise ValueError(
                "hook command attribution and semantic digest must be supplied together"
            )
        if self.attribution is not None:
            expected = hook_command_semantic_digest(
                attribution=self.attribution,
                command=self.command,
                exit_code=self.exit_code,
                output_sha256=self.output_sha256,
                captured_at=self.captured_at,
            )
            if self.semantic_digest != expected:
                raise ValueError("hook command semantic digest does not match its material")
        return self

    @model_serializer(mode="wrap")
    def _preserve_historical_shape(self, handler: Any) -> dict[str, Any]:
        data = handler(self)
        if self.attribution is None:
            data.pop("attribution", None)
            data.pop("semantic_digest", None)
        return data


MAX_CLAIM_COMMAND_BYTES = 16_384
MAX_CLAIM_COMMAND_OUTPUT_BYTES = 131_072
MAX_CLAIM_COMMAND_PROOF_ARTIFACT_BYTES = 262_144
MAX_CLAIM_COMMAND_PROOF_BATCH_ITEMS = 16
MAX_CLAIM_COMMAND_PROOF_BATCH_BYTES = 1_048_576


class ClaimCommandEvidenceCore(BaseModel):
    """Stable claim-bound identity of one externally observed command run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1]
    project_id: StrictStr = Field(min_length=1, max_length=255)
    claim_id: StrictStr = Field(min_length=1, max_length=255)
    generation: StrictInt = Field(ge=1)
    claimed_by: StrictStr = Field(min_length=1, max_length=4096)
    task_id: StrictStr = Field(min_length=1, max_length=255)
    task_revision: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    prd_id: StrictStr = Field(min_length=1, max_length=255)
    prd_revision: StrictInt = Field(ge=1)
    repository_id: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    claim_start_sha: StrictStr = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    cwd_relative: StrictStr = Field(min_length=1, max_length=4096)
    cwd_identity: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    command_base64: StrictStr = Field(min_length=4)
    started_at: StrictStr
    ended_at: StrictStr
    exit_code: StrictInt
    output_base64: StrictStr
    output_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("schema_version", mode="before")
    @classmethod
    def _strict_schema_version(cls, value: Any) -> Any:
        if type(value) is not int:
            raise ValueError("schema_version must be an integer")
        return value

    @field_validator("started_at", "ended_at")
    @classmethod
    def _validate_command_time(cls, value: str) -> str:
        try:
            parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("command proof timestamps must be ISO 8601") from None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("command proof timestamps must be timezone-aware")
        canonical = parsed.astimezone(datetime.UTC).isoformat().replace("+00:00", "Z")
        if value != canonical:
            raise ValueError("command proof timestamps must use canonical UTC spelling")
        return value

    @model_validator(mode="after")
    def _validate_bounded_bytes(self) -> ClaimCommandEvidenceCore:
        started_at = datetime.datetime.fromisoformat(
            self.started_at.replace("Z", "+00:00")
        )
        ended_at = datetime.datetime.fromisoformat(self.ended_at.replace("Z", "+00:00"))
        if started_at > ended_at:
            raise ValueError("command proof start must not follow its end")
        command = _decode_canonical_base64(
            self.command_base64,
            field_name="command_base64",
            max_bytes=MAX_CLAIM_COMMAND_BYTES,
            allow_empty=False,
        )
        try:
            command.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise ValueError("command_base64 must encode valid UTF-8") from None
        output = _decode_canonical_base64(
            self.output_base64,
            field_name="output_base64",
            max_bytes=MAX_CLAIM_COMMAND_OUTPUT_BYTES,
            allow_empty=True,
        )
        if hashlib.sha256(output).hexdigest() != self.output_sha256:
            raise ValueError("output_sha256 must match decoded output bytes")
        if self.exit_code != 0:
            raise ValueError("claim command proof requires exit_code 0")
        return self


def claim_command_semantic_projection(
    core: ClaimCommandEvidenceCore,
) -> dict[str, Any]:
    """Return the stable execution identity, excluding cwd display spelling.

    ``cwd_identity`` is verifier-proven and remains in the projection, so two
    canonical spellings that resolve to the same directory cannot mint distinct
    semantic evidence. The full core, including ``cwd_relative``, remains the
    configured issuer's signature preimage.
    """
    projection = core.model_dump(mode="json")
    projection.pop("cwd_relative")
    return projection


class ClaimCommandIssuer(BaseModel):
    """Durable detached Ed25519 issuer material for a command proof."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    algorithm: Literal["ed25519"]
    signer_id: StrictStr = Field(pattern=r"^[0-9a-f]{16}$")
    public_key: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    signature: StrictStr = Field(pattern=r"^[0-9a-f]{128}$")


class ClaimCommandProof(BaseModel):
    """First-class claim-bound command proof embedded in completion evidence."""

    model_config = _MODEL_CONFIG

    kind: Literal["claim_command"] = "claim_command"
    command: StrictStr = Field(min_length=1, max_length=MAX_CLAIM_COMMAND_BYTES)
    exit_code: StrictInt
    output_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    captured_at: datetime.datetime
    semantic_digest: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    trust_mode: Literal[
        "claim_owner_self_attested", "configured_issuer_verified"
    ]
    issuer_id: StrictStr | None = Field(default=None, pattern=r"^[0-9a-f]{16}$")
    evidence_core: ClaimCommandEvidenceCore
    issuer: ClaimCommandIssuer | None = None

    @field_validator("captured_at", mode="after")
    @classmethod
    def _validate_captured_at(cls, value: datetime.datetime) -> datetime.datetime:
        return _require_utc(value, "captured_at")

    @model_validator(mode="after")
    def _validate_flattened_command_proof(self) -> ClaimCommandProof:
        command = _decode_canonical_base64(
            self.evidence_core.command_base64,
            field_name="command_base64",
            max_bytes=MAX_CLAIM_COMMAND_BYTES,
            allow_empty=False,
        ).decode("utf-8", errors="strict")
        ended_at = datetime.datetime.fromisoformat(
            self.evidence_core.ended_at.replace("Z", "+00:00")
        )
        if (
            self.command != command
            or self.exit_code != self.evidence_core.exit_code
            or self.output_sha256 != self.evidence_core.output_sha256
            or self.captured_at != ended_at
        ):
            raise ValueError("flattened command proof fields must match evidence_core")
        if self.trust_mode == "configured_issuer_verified":
            if self.issuer is None or self.issuer_id is None:
                raise ValueError("issuer-verified command proof requires issuer material")
            if self.issuer.signer_id != self.issuer_id:
                raise ValueError("command proof issuer identity mismatch")
        elif self.issuer is not None or self.issuer_id is not None:
            raise ValueError("self-attested command proof cannot declare issuer material")
        return self


def _decode_canonical_base64(
    value: str, *, field_name: str, max_bytes: int, allow_empty: bool
) -> bytes:
    if not value:
        if allow_empty:
            return b""
        raise ValueError(f"{field_name} must not be empty")
    if len(value) > ((max_bytes + 2) // 3) * 4 or len(value) % 4:
        raise ValueError(f"{field_name} is outside its byte limit")
    try:
        encoded = value.encode("ascii", errors="strict")
        decoded = base64.b64decode(encoded, validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError):
        raise ValueError(f"{field_name} must be canonical base64") from None
    if len(decoded) > max_bytes or base64.b64encode(decoded) != encoded:
        raise ValueError(f"{field_name} must be canonical base64")
    return decoded


class DiffProof(BaseModel):
    """A unified diff captured by the hooks (a later drift check keys on this)."""

    model_config = _MODEL_CONFIG

    kind: Literal[ProofKind.diff] = ProofKind.diff
    files_changed: list[str] = Field(default_factory=list)
    diff_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    insertions: int = Field(default=0, ge=0)
    deletions: int = Field(default=0, ge=0)


class LinkProof(BaseModel):
    """An external artifact reference (PR, CI run, screenshot URL)."""

    model_config = _MODEL_CONFIG

    kind: Literal[ProofKind.link] = ProofKind.link
    url: str
    label: str | None = None


class AssertionProof(BaseModel):
    """A human/agent attestation — the ONLY honour-system proof, typed as such
    so the gate can refuse to let it satisfy a ``CommandProof`` requirement."""

    model_config = _MODEL_CONFIG

    kind: Literal[ProofKind.assertion] = ProofKind.assertion
    statement: str
    attested_by: str


# A serialized proof always carries its ``kind``, so the SQLite JSON column and
# the events.jsonl payload round-trip through ``TypeAdapter(list[ProofArtifact])``
# deterministically. ``ProofArtifact`` is a discriminated union, not a BaseModel.
ProofArtifact = Annotated[
    CommandProof | ClaimCommandProof | DiffProof | LinkProof | AssertionProof,
    Field(discriminator="kind"),
]


class ClaimKind(enum.StrEnum):
    """What a TaskClaim asserts (issue #153). str-serialisable (house rule)."""

    measurement = "measurement"
    data_integrity = "data_integrity"
    behavioral_validation = "behavioral_validation"
    review_verdict = "review_verdict"
    generic = "generic"


class TaskClaim(BaseModel):
    """A named claim a task must PROVE before acceptance (evidence contracts).

    Named ``TaskClaim`` (not ``Claim``) because ``Claim`` is the lease model.
    The claim is the bridge between human intent ("candidate benchmark
    completed") and machine-checkable evidence: ProofRequirements and
    ArtifactAssertions bind to a claim id, and the gate reports a verdict
    per claim.
    """

    model_config = _MODEL_CONFIG

    id: str
    subject: str = ""
    kind: ClaimKind = ClaimKind.generic


class EvidenceCategory(enum.StrEnum):
    """What role submitted evidence is allowed to play (issue #153).

    ``completion`` can satisfy a claim; ``diagnostic``/``advisory`` are
    useful context that must NEVER satisfy a completion claim (the voice
    incident: failed candidate rows were excellent diagnostics and zero
    proof of the benchmark claim); ``blocked`` explains why the claim could
    not be proven; ``promotion_quality`` marks evidence strong enough for
    trust/routing decisions.
    """

    completion = "completion"
    diagnostic = "diagnostic"
    blocked = "blocked"
    advisory = "advisory"
    promotion_quality = "promotion_quality"


class PredicateOp(enum.StrEnum):
    """Operators of the small, domain-agnostic artifact predicate language."""

    exists = "exists"
    not_null = "not_null"
    equals = "equals"
    not_equals = "not_equals"
    contains = "contains"
    not_contains = "not_contains"
    gt = "gt"
    gte = "gte"
    lt = "lt"
    lte = "lte"
    len_eq = "len_eq"
    len_gte = "len_gte"


class Predicate(BaseModel):
    """One machine-checkable assertion over a JSON artifact value.

    ``path`` is a dotted path with an optional single-level ``[*]`` wildcard
    (e.g. ``stage_timings_ms.llm_ms``, ``errors[*].stage``). ``value`` is the
    JSON scalar the operator compares against (unused for ``exists`` /
    ``not_null``).
    """

    model_config = _MODEL_CONFIG

    path: str
    op: PredicateOp
    value: Any | None = None


class ArtifactAssertion(BaseModel):
    """Typed content assertions over a produced artifact, bound to a claim.

    The generic answer to "a command exiting 0 only proves the command
    exited 0": the artifact must EXIST and its content must satisfy every
    predicate. Phase predicates express staged work ("the candidate run must
    reach the llm stage"): ``stage_order`` declares the pipeline order,
    ``stage_path`` names where failure stages are recorded in the artifact,
    and ``must_reach`` / ``must_not_fail_before`` gate on them.
    """

    model_config = _MODEL_CONFIG

    artifact: str  # path relative to the project root
    format: Literal["json"] = "json"
    claim: str | None = None  # TaskClaim id this assertion proves
    assertions: list[Predicate] = Field(default_factory=list)
    stage_order: list[str] = Field(default_factory=list)
    stage_path: str | None = None
    must_reach: str | None = None
    must_not_fail_before: str | None = None


class ProofRequirement(BaseModel):
    """One typed thing a Task demands before it can be accepted."""

    model_config = _MODEL_CONFIG

    kind: ProofKind
    # command requirements pin the exact command and the passing exit set:
    command: str | None = None
    passing_exit_codes: list[int] = Field(default_factory=lambda: [0])
    # link requirements may pin a required URL substring (optional):
    link_contains: str | None = None
    label: str  # human description for packets / errors
    # Evidence contracts (issue #153): the TaskClaim id this requirement
    # proves. None keeps today's task-level semantics (implicit claim).
    claim: str | None = None

    @model_validator(mode="after")
    def _command_requirements_pin_a_command(self) -> ProofRequirement:
        # A kind=command requirement with command=None can never be satisfied
        # (CommandProof.command is always a str), so reject it at construction
        # rather than letting the gate fail it silently.
        if self.kind is ProofKind.command and self.command is None:
            raise ValueError("command-kind ProofRequirement requires `command`")
        return self


class Verification(BaseModel):
    """Verification instructions embedded on a Task."""

    model_config = _MODEL_CONFIG

    commands: list[str] = Field(default_factory=list)
    manual_steps: list[str] = Field(default_factory=list)
    required_evidence: list[str] = Field(default_factory=list)
    # SL-3 / B48: typed requirements — a free-text claim in a description field
    # can't satisfy a command requirement (authenticity still rests on a trusted
    # hook writer; see the TRUST BOUNDARY note above). Additive — the legacy
    # free-text ``required_evidence`` path stays for back-compat; the gate
    # evaluates both. New planners populate ``required_proofs``.
    required_proofs: list[ProofRequirement] = Field(default_factory=list)
    # Evidence contracts (issue #153): content assertions over produced
    # artifacts, optionally bound to task claims. Additive — [] means the
    # gate behaves exactly as before this feature.
    artifact_assertions: list[ArtifactAssertion] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def _omit_empty_assertions(self, handler: Any) -> dict[str, Any]:
        # Same omit-when-empty discipline as Task.claims: unused contracts
        # keep the pre-v9 byte shape.
        data = handler(self)
        if not data.get("artifact_assertions"):
            data.pop("artifact_assertions", None)
        return data


# ---------------------------------------------------------------------------
# Top-level entities
# ---------------------------------------------------------------------------


class Project(BaseModel):
    """Root entity that owns all other entities in the database."""

    model_config = _MODEL_CONFIG

    id: str
    name: str
    description: str
    created_at: datetime.datetime
    updated_at: datetime.datetime

    @field_validator("created_at", "updated_at", mode="after")
    @classmethod
    def _validate_utc(cls, v: datetime.datetime) -> datetime.datetime:
        return _require_utc(v, "created_at / updated_at")


MAX_PRD_ASSUMPTIONS = 100
MAX_PRD_ASSUMPTION_ID_LENGTH = 32
MAX_PRD_ASSUMPTION_STATEMENT_LENGTH = 500
MAX_PRD_ASSUMPTION_RATIONALE_LENGTH = 1_000
MAX_PRD_ASSUMPTION_REQUIREMENTS = 100


class _FrozenList(tuple[Any, ...]):
    """Tuple-backed sequence with list-compatible equality and wire shape."""

    def __new__(cls, values: Iterable[Any] = ()) -> Self:
        return super().__new__(cls, values)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, list):
            return tuple.__eq__(self, tuple(other))
        return tuple.__eq__(self, other)

    def __ne__(self, other: object) -> bool:
        if isinstance(other, list):
            return tuple.__ne__(self, tuple(other))
        return tuple.__ne__(self, other)

    __hash__ = tuple.__hash__

    def __iadd__(self, value: Iterable[Any]) -> Self:  # type: ignore[misc]
        raise TypeError("frozen list cannot be mutated")

    def __imul__(self, value: SupportsIndex) -> Self:
        raise TypeError("frozen list cannot be mutated")

    @staticmethod
    def _immutable(*_args: object, **_kwargs: object) -> None:
        raise TypeError("frozen list cannot be mutated")

    append = _immutable
    clear = _immutable
    extend = _immutable
    insert = _immutable
    pop = _immutable
    remove = _immutable
    reverse = _immutable
    sort = _immutable


class PRDAssumption(BaseModel):
    """A bounded, reviewable premise used while planning a PRD.

    Assumptions deliberately live on the PRD rather than on a task: they are
    product context, not implementation evidence.  An empty
    ``requirement_ids`` list denotes a global assumption; a populated list
    scopes it to the requirements it affects.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        revalidate_instances="always",
    )

    id: str = Field(max_length=MAX_PRD_ASSUMPTION_ID_LENGTH)
    statement: str = Field(max_length=MAX_PRD_ASSUMPTION_STATEMENT_LENGTH)
    rationale: str = Field(max_length=MAX_PRD_ASSUMPTION_RATIONALE_LENGTH)
    requirement_ids: list[RequirementID] = Field(
        default_factory=list,
        max_length=MAX_PRD_ASSUMPTION_REQUIREMENTS,
    )

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        """Canonicalize and validate the stable identifier in every input path."""
        normalized = value.strip().upper()
        if not re.fullmatch(r"A[0-9]{3,31}", normalized):
            raise ValueError("PRD assumption id must use the stable A### format")
        return normalized

    @field_validator("statement", "rationale")
    @classmethod
    def _validate_required_text(cls, value: str) -> str:
        """Reject blank canonical records even when they bypass Markdown parsing."""
        normalized = value.strip()
        if not normalized:
            raise ValueError("PRD assumption statement and rationale must not be blank")
        return normalized

    @model_validator(mode="after")
    def _freeze_requirement_ids(self) -> PRDAssumption:
        object.__setattr__(self, "requirement_ids", _FrozenList(self.requirement_ids))
        return self

    @field_serializer("requirement_ids")
    def _serialize_requirement_ids(self, value: list[RequirementID]) -> list[str]:
        return list(value)

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        if update:
            raise TypeError(
                "PRDAssumption.model_copy(update=...) is disabled; revalidate instead"
            )
        return super().model_copy(deep=deep)

    def copy(
        self,
        *,
        include: Any = None,
        exclude: Any = None,
        update: dict[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        if include is not None or exclude is not None or update:
            raise TypeError("PRDAssumption.copy filters/updates are disabled; revalidate instead")
        return super().copy(deep=deep)


class PRD(BaseModel):
    """Product Requirements Document — the gate that controls task claimability."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        hide_input_in_errors=True,
    )

    # Identity / release fields (v0.3 multi-PRD, Phase 0). All default so reading
    # a v6 prds row that predates these columns still constructs. ``exclude=True``
    # keeps Phase 0 purely additive with NO behavior change: these fields are
    # constructible and readable in memory, but are omitted from ``model_dump()``
    # so the existing v6 event payloads / snapshot blobs stay byte-identical and
    # the ``extra="forbid"`` payload models in payloads.py do not reject them.
    # Wiring them into the schema / payloads / sqlite is a later task (T002+).
    id: PRDID = Field(default=DEFAULT_PRD_ID, exclude=True)
    title: str = Field(default="", exclude=True)
    target_version: str | None = Field(default=None, exclude=True)
    target_tag: str | None = Field(default=None, exclude=True)
    is_default: bool = Field(default=False, exclude=True)
    # Event-sourced revision counter (v0.3 multi-PRD, Phase 6 wiring). First
    # parse is revision 1; each re-parse bumps it via a ``prd.revised`` event.
    # Defaults to 1 so a PRD constructed without it (and any v6 prds row that
    # predates the column) reads as the first revision. ``exclude=True`` keeps
    # Phase 0 additive — omitted from ``model_dump()`` so existing event
    # payloads / snapshot blobs stay byte-identical until Phase 6 wires it in.
    revision: StrictInt = Field(default=1, ge=1, exclude=True)
    # Exact source provenance is projection state, not part of generic PRD
    # serialization. Dedicated content/read contracts access these attributes
    # explicitly; ``model_dump()`` must never leak raw source bytes.
    source_bytes: StrictBytes | None = Field(default=None, exclude=True, repr=False)
    source_sha256: StrictStr | None = Field(default=None, exclude=True)
    source_size_bytes: StrictInt | None = Field(
        default=None,
        ge=0,
        le=2_097_152,
        exclude=True,
    )
    source_encoding: Literal["utf-8"] | None = Field(default=None, exclude=True)
    source_revision: StrictInt | None = Field(default=None, ge=1, exclude=True)
    provenance_state: Literal["available", "legacy_unbound"] = Field(
        default="legacy_unbound",
        exclude=True,
    )
    content_available: StrictBool = Field(default=False, exclude=True)
    status: PRDStatus = PRDStatus.draft
    summary: str = ""
    goals: list[str] = Field(default_factory=list)
    non_goals: list[str] = Field(default_factory=list)
    requirements: list[RequirementID] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    assumptions: list[PRDAssumption] = Field(
        default_factory=list,
        max_length=MAX_PRD_ASSUMPTIONS,
    )
    last_reviewed_at: datetime.datetime | None = None
    last_reviewed_by: str | None = None
    created_at: datetime.datetime | None = Field(default=None, exclude=True)
    updated_at: datetime.datetime | None = Field(default=None, exclude=True)

    def __iter__(self):
        """Preserve legacy mapping shape while redacting source provenance.

        Pydantic's default iterator exposes fields marked ``exclude=True`` to
        ``dict(model)``. Existing identity/release fields historically relied
        on that behavior, so filter only the newly sensitive provenance fields
        rather than changing the established mapping contract wholesale.
        """
        sensitive_fields = {
            "source_bytes",
            "source_sha256",
            "source_size_bytes",
            "source_encoding",
            "source_revision",
            "provenance_state",
            "content_available",
        }
        yield from (
            (name, value)
            for name, value in super().__iter__()
            if name not in sensitive_fields
        )

    def validated_copy(self, **updates: object) -> PRD:
        """Return an immutable PRD update with every invariant revalidated."""
        values = {
            name: copy.deepcopy(getattr(self, name))
            for name in type(self).model_fields
        }
        values.update(updates)
        return type(self).model_validate(values)

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        """Copy safely; provenance-bearing updates must pass full validation."""
        if update:
            raise TypeError("PRD.model_copy(update=...) is disabled; use validated_copy")
        return super().model_copy(deep=deep)

    def copy(
        self,
        *,
        include: Any = None,
        exclude: Any = None,
        update: dict[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        if include is not None or exclude is not None or update:
            raise TypeError("PRD.copy filters/updates are disabled; use validated_copy")
        return super().copy(deep=deep)

    @model_validator(mode="after")
    def _freeze_nested_state(self) -> PRD:
        for field_name in (
            "goals",
            "non_goals",
            "requirements",
            "acceptance_criteria",
            "risks",
            "open_questions",
            "assumptions",
        ):
            object.__setattr__(self, field_name, _FrozenList(getattr(self, field_name)))
        return self

    @field_serializer(
        "goals",
        "non_goals",
        "requirements",
        "acceptance_criteria",
        "risks",
        "open_questions",
        "assumptions",
    )
    def _serialize_frozen_lists(self, value: list[Any]) -> list[Any]:
        return list(value)

    @model_validator(mode="after")
    def _validate_source_provenance(self) -> PRD:
        source_fields = (
            self.source_bytes,
            self.source_sha256,
            self.source_size_bytes,
            self.source_encoding,
            self.source_revision,
        )
        if self.provenance_state == "legacy_unbound":
            if self.content_available or any(value is not None for value in source_fields):
                raise ValueError(
                    "legacy-unbound provenance cannot fabricate source metadata"
                )
            return self

        if not self.content_available or any(
            value is None for value in source_fields
        ):
            raise ValueError(
                "available provenance requires exact source bytes, digest, size, "
                "encoding, revision, and content availability"
            )
        if self.source_bytes is None:
            raise ValueError("available provenance requires exact source bytes")
        # Inspect the raw buffer before copying it so hostile subclasses cannot
        # bypass the ceiling and oversized inputs do not amplify memory use.
        source_view = memoryview(self.source_bytes)
        if source_view.nbytes > 2_097_152:
            raise ValueError("available source exceeds the Version 1 byte ceiling")
        source_bytes = source_view.tobytes()
        object.__setattr__(self, "source_bytes", source_bytes)
        try:
            source_bytes.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise ValueError("available source must be valid UTF-8") from None
        if self.source_size_bytes != len(source_bytes):
            raise ValueError("source byte size does not match exact source bytes")
        if self.source_sha256 != hashlib.sha256(source_bytes).hexdigest():
            raise ValueError("source digest does not match exact source bytes")
        if self.source_revision != self.revision:
            raise ValueError("source provenance must bind the projected PRD revision")
        return self

    @field_validator("last_reviewed_at", mode="after")
    @classmethod
    def _validate_last_reviewed_utc(
        cls, v: datetime.datetime | None
    ) -> datetime.datetime | None:
        if v is not None:
            return _require_utc(v, "last_reviewed_at")
        return v

    @field_validator("created_at", "updated_at", mode="after")
    @classmethod
    def _validate_created_updated_utc(
        cls, v: datetime.datetime | None
    ) -> datetime.datetime | None:
        if v is not None:
            return _require_utc(v, "created_at / updated_at")
        return v


class Requirement(BaseModel):
    """A single atomic requirement derived from a section of the PRD."""

    model_config = _MODEL_CONFIG

    id: RequirementID
    prd_id: PRDID = Field(default=DEFAULT_PRD_ID, exclude=True)
    prd_section: str
    text: str
    source_paragraph: str | None = None
    derived: bool = False
    # Revision lineage (v0.3 multi-PRD, Phase 6 wiring). ``revision_introduced``
    # is the PRD revision a requirement first appeared in; ``revision_superseded``
    # is the revision that removed/replaced it (None = still live). Defaults make
    # a Requirement constructed without them read as "introduced at revision 1,
    # never superseded" — matching the nullable INTEGER columns the v7 migration
    # adds. ``exclude=True`` keeps Phase 0 additive: omitted from ``model_dump()``
    # so existing event payloads / snapshot blobs stay byte-identical.
    revision_introduced: int = Field(default=1, ge=1, exclude=True)
    revision_superseded: int | None = Field(default=None, exclude=True)


class Feature(BaseModel):
    """A logical grouping of tasks that delivers a user-observable capability."""

    model_config = _MODEL_CONFIG

    id: FeatureID
    prd_id: PRDID = Field(default=DEFAULT_PRD_ID, exclude=True)
    title: str
    description: str
    status: FeatureStatus = FeatureStatus.proposed
    requirements: list[RequirementID] = Field(default_factory=list)
    tasks: list[TaskID] = Field(default_factory=list)


class Task(BaseModel):
    """The primary unit of work — claimable, scoreable, evidence-backed."""

    model_config = _MODEL_CONFIG

    id: TaskID
    feature_id: FeatureID
    prd_id: PRDID = Field(default=DEFAULT_PRD_ID, exclude=True)
    title: str
    description: str
    status: TaskStatus = TaskStatus.proposed
    priority: TaskPriority = TaskPriority.medium
    # task_type defaults to ``feature`` so every pre-existing task (and any
    # caller that omits it) keeps its original meaning — full backward
    # compatibility. See :class:`TaskType`.
    task_type: TaskType = TaskType.feature
    dependencies: list[TaskID] = Field(default_factory=list)
    conflict_groups: list[str] = Field(default_factory=list)
    scores: Score = Field(default_factory=Score)
    acceptance_criteria: list[str] = Field(default_factory=list)
    implementation_notes: list[str] = Field(default_factory=list)
    verification: Verification = Field(default_factory=Verification)
    likely_files: list[str] = Field(default_factory=list)
    # Evidence contracts (issue #153): named claims this task must prove.
    # [] keeps today's behavior exactly (no claims, task-level gate only).
    claims: list[TaskClaim] = Field(default_factory=list)

    @field_validator("claims", mode="before")
    @classmethod
    def _none_claims_is_empty(cls, v: object) -> object:
        # TaskCreatedPayload defaults claims to None (optional key so pre-v9
        # JSONL replays unchanged); the handler forwards its model_dump here,
        # so None must mean "no claims", same as an absent key.
        return [] if v is None else v

    @model_serializer(mode="wrap")
    def _omit_empty_claims(self, handler: Any) -> dict[str, Any]:
        # Omit-when-empty (T010 discipline): a task with no claims serializes
        # byte-identically to pre-v9, so task.created events and API dumps
        # only change shape when the feature is genuinely used.
        data = handler(self)
        if not data.get("claims"):
            data.pop("claims", None)
        return data
    parent_task_id: TaskID | None = None
    created_at: datetime.datetime
    updated_at: datetime.datetime

    @field_validator("created_at", "updated_at", mode="after")
    @classmethod
    def _validate_utc(cls, v: datetime.datetime) -> datetime.datetime:
        return _require_utc(v, "created_at / updated_at")


class BundleReviewPolicy(BaseModel):
    """Bounded independent-review policy stored with an execution bundle."""

    model_config = _MODEL_CONFIG

    # The pre-T003 draft serialized 1/[] for these two fields. Preserve those
    # defaults for replay equivalence; the gate applies a non-configurable
    # minimum of three distinct reviewers/angles and treats max_reviews as a
    # bounded per-round cap (also floored at three for legacy rows).
    max_reviews: int = Field(default=1, ge=1, le=20)
    max_rereviews: int = Field(default=1, ge=0)
    independent_reviewer_required: bool = True
    required_angles: list[str] = Field(default_factory=list)

    @field_validator("required_angles")
    @classmethod
    def _validate_review_angles(cls, value: list[str]) -> list[str]:
        normalized = [angle.strip().lower() for angle in value]
        if any(not angle for angle in normalized):
            raise ValueError("bundle review angles must not be blank")
        if len(normalized) != len(set(normalized)):
            raise ValueError("bundle review angles must be unique")
        return normalized

    @model_validator(mode="after")
    def _reviews_cover_angles(self) -> BundleReviewPolicy:
        if self.max_reviews < len(self.required_angles):
            raise ValueError("max_reviews must cover every required review angle")
        return self


class BundleReviewVerdict(BaseModel):
    """One independently authored adversarial verdict for a bundle review round."""

    model_config = _MODEL_CONFIG

    id: ReviewID
    bundle_id: BundleID
    creation_event_id: EventID
    disposition_event_id: EventID
    review_round: int = Field(ge=1)
    angle: str
    reviewed_by: str
    decision: ReviewDecision
    notes: str | None = None
    created_at: datetime.datetime

    @field_validator("angle", "reviewed_by")
    @classmethod
    def _validate_review_identity(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("bundle review angle and reviewer must not be blank")
        return normalized

    @field_validator("created_at", mode="after")
    @classmethod
    def _validate_review_time(cls, value: datetime.datetime) -> datetime.datetime:
        return _require_utc(value, "created_at")


class BundleThroughputBudget(BaseModel):
    """Planning limits captured at bundle creation for an auditable decision."""

    model_config = _MODEL_CONFIG

    # 500 keeps every membership query below SQLite's conservative variable
    # ceiling even after status parameters are added. It is an escape hatch
    # above the normal threshold, not permission for an unbounded SQL request.
    max_tasks: int = Field(default=12, ge=1, le=500)
    max_serial_stages: int = Field(default=6, ge=1, le=500)


class DelegatedAgentObservation(BaseModel):
    """One optional harness-handle observation; never controls bundle state."""

    model_config = _MODEL_CONFIG

    id: str
    handle: str | None = None
    runtime: str | None = None
    task_ids: list[TaskID] = Field(default_factory=list)
    status: DelegatedAgentStatus
    observed_at: datetime.datetime
    detail: str | None = None

    @field_validator("id")
    @classmethod
    def _validate_id(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("delegated agent observation id must not be empty")
        return v

    @field_validator("task_ids")
    @classmethod
    def _validate_task_ids(cls, v: list[TaskID]) -> list[TaskID]:
        if len(v) != len(set(v)):
            raise ValueError("delegated agent observation task_ids must be unique")
        return v

    @field_validator("observed_at", mode="after")
    @classmethod
    def _validate_observed_at(cls, v: datetime.datetime) -> datetime.datetime:
        return _require_utc(v, "observed_at")


class BundleCheckpoint(BaseModel):
    """Optional delivery reference; metadata only, never task evidence."""

    model_config = _MODEL_CONFIG

    commit_sha: str | None = None
    pr_url: str | None = None
    recorded_at: datetime.datetime
    recorded_by: str

    @field_validator("recorded_at", mode="after")
    @classmethod
    def _validate_recorded_at(cls, v: datetime.datetime) -> datetime.datetime:
        return _require_utc(v, "recorded_at")

    @model_validator(mode="after")
    def _requires_reference(self) -> BundleCheckpoint:
        if not self.commit_sha and not self.pr_url:
            raise ValueError("bundle checkpoint requires commit_sha or pr_url")
        return self


class ExecutionBundle(BaseModel):
    """Coordinator-owned execution unit over ordered, independently-audited tasks."""

    model_config = _MODEL_CONFIG

    id: BundleID
    creation_event_id: EventID
    prd_id: PRDID
    task_ids: list[TaskID]
    coordinator: str
    status: BundleStatus = BundleStatus.planned
    review_disposition_event_id: EventID | None = None
    superseded_by: BundleID | None = None
    last_result_at: datetime.datetime | None = None
    branch: str | None = None
    worktree_path: str | None = None
    review_policy: BundleReviewPolicy = Field(default_factory=BundleReviewPolicy)
    throughput_budget: BundleThroughputBudget = Field(
        default_factory=BundleThroughputBudget
    )
    delegated_agents: list[DelegatedAgentObservation] = Field(default_factory=list)
    checkpoint: BundleCheckpoint | None = None
    created_at: datetime.datetime
    updated_at: datetime.datetime

    @field_validator("id", "coordinator")
    @classmethod
    def _validate_required_identity(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("execution bundle id and coordinator must not be empty")
        return v

    @field_validator("task_ids")
    @classmethod
    def _validate_task_ids(cls, v: list[TaskID]) -> list[TaskID]:
        if not v:
            raise ValueError("execution bundle requires at least one task")
        if len(v) != len(set(v)):
            raise ValueError("execution bundle task_ids must be unique")
        return v

    @field_validator("delegated_agents")
    @classmethod
    def _validate_observation_ids(
        cls, v: list[DelegatedAgentObservation]
    ) -> list[DelegatedAgentObservation]:
        ids = [observation.id for observation in v]
        if len(ids) != len(set(ids)):
            raise ValueError("delegated agent observation ids must be unique")
        return v

    @field_validator("created_at", "updated_at", mode="after")
    @classmethod
    def _validate_bundle_utc(cls, v: datetime.datetime) -> datetime.datetime:
        return _require_utc(v, "created_at / updated_at")

    @field_validator("last_result_at", mode="after")
    @classmethod
    def _validate_last_result_at(
        cls, value: datetime.datetime | None
    ) -> datetime.datetime | None:
        return _require_utc(value, "last_result_at") if value is not None else None

    @model_validator(mode="after")
    def _validate_members_fit_budget(self) -> ExecutionBundle:
        if self.updated_at < self.created_at:
            raise ValueError("execution bundle updated_at must not precede created_at")
        if len(self.task_ids) > self.throughput_budget.max_tasks:
            raise ValueError(
                f"execution bundle has {len(self.task_ids)} tasks but its "
                f"throughput budget permits {self.throughput_budget.max_tasks}"
            )
        members = set(self.task_ids)
        outside = sorted(
            {
                task_id
                for observation in self.delegated_agents
                for task_id in observation.task_ids
                if task_id not in members
            }
        )
        if outside:
            raise ValueError(
                f"delegated agent observations reference non-member tasks: {outside}"
            )
        return self

    @model_serializer(mode="wrap")
    def _omit_empty_review_disposition(self, handler: Any) -> dict[str, Any]:
        data = handler(self)
        if data.get("review_disposition_event_id") is None:
            data.pop("review_disposition_event_id", None)
        if data.get("superseded_by") is None:
            data.pop("superseded_by", None)
        if data.get("last_result_at") is None:
            data.pop("last_result_at", None)
        return data


class ClaimExpectedPathBaseline(BaseModel):
    """One canonical repo-relative claim path and its claim-start blob digest.

    ``baseline_sha256`` is nullable because a task may legitimately be expected
    to create a new file.  The immutable value object is captured before the
    claim event is appended; replay never consults the working tree.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: StrictStr = Field(min_length=1, max_length=4096)
    baseline_sha256: StrictStr | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )


class ClaimAttestationContext(BaseModel):
    """Immutable Git/revision binding for external progress attestations."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository_id: StrictStr = Field(min_length=1, max_length=512)
    claim_start_sha: StrictStr = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    prd_id: StrictStr = Field(min_length=1, max_length=255)
    prd_revision: StrictInt = Field(ge=1)
    # Opaque canonical task-content revision supplied by the claim producer.
    # Keeping this as a string allows a versioned semantic digest without
    # coupling durable state to one task serializer.
    task_revision: StrictStr = Field(min_length=1, max_length=255)
    expected_paths: list[ClaimExpectedPathBaseline] = Field(default_factory=list)

    @field_validator("expected_paths")
    @classmethod
    def _validate_unique_expected_paths(
        cls, value: list[ClaimExpectedPathBaseline]
    ) -> list[ClaimExpectedPathBaseline]:
        paths = [entry.path for entry in value]
        if len(paths) != len(set(paths)):
            raise ValueError("attestation expected paths must be unique")
        return value


class ClaimProgressAttestation(BaseModel):
    """Authoritative projection of one claim-bound progress attestation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    semantic_digest: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    accepted_event_id: EventID
    envelope_id: StrictStr | None = Field(default=None, max_length=255)
    claim_id: ClaimID
    task_id: TaskID
    claimed_by: StrictStr = Field(min_length=1)
    generation: StrictInt = Field(ge=1)
    repository_id: StrictStr = Field(min_length=1, max_length=512)
    claim_start_sha: StrictStr = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    prd_id: PRDID
    prd_revision: StrictInt = Field(ge=1)
    task_revision: StrictStr = Field(min_length=1, max_length=255)
    kind: Literal["commit", "file"]
    commit_sha: StrictStr | None = Field(
        default=None, pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"
    )
    changed_paths: list[StrictStr] = Field(default_factory=list)
    path: StrictStr | None = Field(default=None, min_length=1, max_length=4096)
    file_sha256: StrictStr | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    attested_at: datetime.datetime
    recorded_at: datetime.datetime
    trust_mode: Literal[
        "claim_owner_self_attested", "configured_issuer_verified"
    ]
    issuer_id: StrictStr | None = Field(default=None, max_length=255)
    consumed_by_event_id: EventID | None = None
    consumed_at: datetime.datetime | None = None
    invalidated_by_event_id: EventID | None = None
    collision_detected: StrictBool = False

    @model_validator(mode="after")
    def _validate_attestation_kind(self) -> ClaimProgressAttestation:
        if self.kind == "commit":
            if self.commit_sha is None or not self.changed_paths:
                raise ValueError("commit attestation requires commit_sha and changed_paths")
            if self.path is not None or self.file_sha256 is not None:
                raise ValueError("commit attestation cannot carry file-only fields")
        else:
            if self.commit_sha is not None or self.changed_paths:
                raise ValueError("file attestation cannot carry commit-only fields")
            if self.path is None or self.file_sha256 is None:
                raise ValueError("file attestation requires path and file_sha256")
        return self

    @field_validator("attested_at", "recorded_at", "consumed_at", mode="after")
    @classmethod
    def _validate_attestation_utc(
        cls, value: datetime.datetime | None
    ) -> datetime.datetime | None:
        if value is None:
            return None
        return _require_utc(value, "attestation timestamp")


class Claim(BaseModel):
    """An exclusive lease that an agent holds on a Task while working on it."""

    model_config = _MODEL_CONFIG

    id: ClaimID
    task_id: TaskID
    claimed_by: str
    claim_type: ClaimType = ClaimType.task
    status: ClaimStatus = ClaimStatus.active
    branch: str | None = None
    worktree_path: str | None = None
    expected_files: list[str] = Field(default_factory=list)
    # Monotonic per-task lifecycle generation.  v17 migration deterministically
    # assigns generations to legacy rows; only claims with an immutable context
    # may accept external progress attestations.
    generation: StrictInt = Field(default=1, ge=1)
    attestation_context: ClaimAttestationContext | None = None
    # Internal authorization created atomically under one public bundle claim.
    # None preserves the legacy standalone-task claim shape.
    bundle_claim_id: str | None = None
    # The claiming loop's session discriminator (ANVIL_SESSION_ID /
    # CLAUDE_CODE_SESSION_ID), recorded INDEPENDENTLY of the actor string so
    # two loops sharing a pinned ANVIL_ACTOR are still distinguishable — the
    # basis of the same-actor/different-session fail-fast. None for claims
    # made with no session env (and for all pre-v10 claims).
    session_id: str | None = None
    created_at: datetime.datetime
    lease_expires_at: datetime.datetime
    last_heartbeat_at: datetime.datetime
    released_at: datetime.datetime | None = None
    release_reason: str | None = None

    @field_validator(
        "created_at",
        "lease_expires_at",
        "last_heartbeat_at",
        mode="after",
    )
    @classmethod
    def _validate_utc_required(
        cls, v: datetime.datetime
    ) -> datetime.datetime:
        return _require_utc(v, "created_at / lease_expires_at / last_heartbeat_at")

    @model_serializer(mode="wrap")
    def _omit_empty_bundle_claim(self, handler: Any) -> dict[str, Any]:
        data = handler(self)
        if data.get("bundle_claim_id") is None:
            data.pop("bundle_claim_id", None)
        return data


class BundleClaim(BaseModel):
    """One public coordinator lease over an execution bundle.

    ``member_claim_ids`` are internal task authorizations used only to preserve
    the existing task-scoped evidence and disposition contract.
    """

    model_config = _MODEL_CONFIG

    id: ClaimID
    bundle_id: BundleID
    claimed_by: str
    status: ClaimStatus = ClaimStatus.active
    branch: str | None = None
    worktree_path: str | None = None
    session_id: str | None = None
    expected_files: list[str] = Field(default_factory=list)
    member_claim_ids: dict[TaskID, ClaimID]
    created_at: datetime.datetime
    lease_expires_at: datetime.datetime
    last_heartbeat_at: datetime.datetime
    released_at: datetime.datetime | None = None
    release_reason: str | None = None

    @field_validator("created_at", "lease_expires_at", "last_heartbeat_at")
    @classmethod
    def _validate_required_utc(cls, v: datetime.datetime) -> datetime.datetime:
        return _require_utc(v, "bundle claim timestamps")

    @model_validator(mode="after")
    def _validate_member_claims(self) -> BundleClaim:
        if not self.member_claim_ids:
            raise ValueError("bundle claim requires member claim authorizations")
        if len(set(self.member_claim_ids.values())) != len(self.member_claim_ids):
            raise ValueError("bundle member claim ids must be unique")
        return self

    @field_validator("released_at", mode="after")
    @classmethod
    def _validate_released_utc(
        cls, v: datetime.datetime | None
    ) -> datetime.datetime | None:
        if v is not None:
            return _require_utc(v, "released_at")
        return v


class Evidence(BaseModel):
    """Completion evidence submitted by an agent after finishing a Task."""

    model_config = _MODEL_CONFIG

    id: EvidenceID
    task_id: TaskID
    claim_id: ClaimID
    commands_run: list[str] = Field(default_factory=list)
    output_excerpt: str | None = None
    files_changed: list[str] = Field(default_factory=list)
    pr_url: str | None = None
    commit_sha: str | None = None
    screenshots: list[str] = Field(default_factory=list)
    known_limitations: str | None = None
    # SL-3 / B48: typed proofs the gate reads (additive). The legacy string
    # fields above stay as descriptive metadata; the gate no longer needs them
    # once a task declares ``required_proofs``.
    proofs: list[ProofArtifact] = Field(default_factory=list)
    # Evidence contracts (issue #153): what role this evidence may play.
    # diagnostic/advisory evidence can never satisfy a completion claim.
    category: EvidenceCategory = EvidenceCategory.completion
    submitted_at: datetime.datetime
    submitted_by: str

    @field_validator("submitted_at", mode="after")
    @classmethod
    def _validate_utc(cls, v: datetime.datetime) -> datetime.datetime:
        return _require_utc(v, "submitted_at")


class RejectionQualityFinding(BaseModel):
    """One bounded typed quality finding attached to a rejected attempt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: RejectionQualityFindingCode


class TaskRejectionProvenance(BaseModel):
    """Immutable engine-derived provenance for one rejected review attempt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    category: RejectionCategory
    reason_code: RejectionReasonCode
    claim_id: StrictStr = Field(min_length=1, max_length=255)
    review_attempt_id: StrictStr = Field(min_length=1, max_length=255)
    supporting_evidence_digest: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    quality_findings: tuple[RejectionQualityFinding, ...] = Field(
        default_factory=tuple,
        max_length=32,
    )
    matched_process_predicate: RejectionProcessPredicate | None = None
    counts_toward_accept_rate: StrictBool

    @model_validator(mode="after")
    def _validate_derived_shape(self) -> TaskRejectionProvenance:
        finding_codes = [finding.code for finding in self.quality_findings]
        if len(finding_codes) != len(set(finding_codes)):
            raise ValueError("rejection quality finding codes must be unique")
        if self.category is RejectionCategory.quality:
            if self.matched_process_predicate is not None:
                raise ValueError("quality rejection cannot carry a process predicate")
            if not self.counts_toward_accept_rate:
                raise ValueError("quality rejection must count toward accept rate")
        elif self.category is RejectionCategory.evidence_resubmission:
            if self.reason_code is not RejectionReasonCode.evidence_incomplete:
                raise ValueError(
                    "evidence-resubmission rejection requires evidence_incomplete"
                )
            if self.quality_findings or self.matched_process_predicate is not None:
                raise ValueError(
                    "evidence-resubmission rejection cannot carry findings or a "
                    "process predicate"
                )
            if self.counts_toward_accept_rate:
                raise ValueError(
                    "evidence-resubmission rejection cannot count toward accept rate"
                )
        else:
            if self.quality_findings:
                raise ValueError("process rejection cannot carry quality findings")
            if self.matched_process_predicate is None:
                raise ValueError("process rejection requires a matched predicate")
            if self.counts_toward_accept_rate:
                raise ValueError("process rejection cannot count toward accept rate")
        return self


def supporting_evidence_digest(evidence: Evidence) -> str:
    """Return the stable digest binding a review to persisted evidence bytes."""
    from anvil.state.hashing import domain_separated_sha256

    return domain_separated_sha256(
        b"anvil.task-review-evidence.v1\0",
        evidence.model_dump(mode="json"),
    )


class EventRange(BaseModel):
    """The inclusive event-id span an ``AcceptanceProof`` attests to."""

    model_config = _MODEL_CONFIG

    start: EventID  # first event recorded for the task
    end: EventID  # the task.applied (acceptance) event


class AcceptanceProof(BaseModel):
    """A portable, signed receipt emitted when a task is accepted (B48 part 2).

    Binds the task + claim/lease + actor + the observed ``CommandProof``s + the
    event-log range, with a detached Ed25519 signature so it verifies off-host
    with only the public key (plus a trust list). This is the acceptance
    *envelope* that WRAPS the per-evidence ``ProofArtifact`` union — a distinct
    concept, hence a distinct name.

    The signature covers :meth:`signed_bytes` — every field EXCEPT the signature
    envelope (``signer_id`` / ``public_key`` / ``signature``) — so a verifier
    reconstructs identical bytes from the loaded proof and checks them against
    the embedded public key.
    """

    model_config = _MODEL_CONFIG

    format_version: int = 1
    # project_id binds the proof to its originating project so a signed proof
    # for a common task id (e.g. "T001") in one repo cannot be replayed as a
    # proof for the same id in another. Part of the signed payload.
    project_id: str
    task_id: TaskID
    claim_id: ClaimID
    actor: str
    command_results: list[CommandProof | ClaimCommandProof] = Field(
        default_factory=list
    )
    event_range: EventRange
    created_at: datetime.datetime
    # --- signature envelope (NOT covered by the signature) ---
    algorithm: str = "ed25519"
    signer_id: str
    public_key: str  # hex-encoded raw Ed25519 public key
    # Filled in by signing.sign_proof after construction; "" means unsigned
    # (verification rejects an empty signature).
    signature: str = ""  # hex-encoded detached signature over signed_bytes()

    @field_validator("created_at", mode="after")
    @classmethod
    def _validate_utc(cls, v: datetime.datetime) -> datetime.datetime:
        return _require_utc(v, "created_at")

    def signed_payload(self) -> dict[str, Any]:
        """The canonical core the detached signature covers.

        Built from ``model_dump(mode="json")`` minus the signature envelope, so
        signer and verifier serialize identically regardless of who holds the
        private key.
        """
        payload = self.model_dump(mode="json")
        for envelope_field in ("signer_id", "public_key", "signature"):
            payload.pop(envelope_field, None)
        return payload

    def signed_bytes(self) -> bytes:
        """Deterministic bytes to sign / verify: canonical JSON of the core."""
        return json.dumps(
            self.signed_payload(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")


class Decision(BaseModel):
    """An architectural or design decision recorded for audit and context."""

    model_config = _MODEL_CONFIG

    id: DecisionID
    title: str
    context: str
    decision: str
    consequences: str
    created_at: datetime.datetime
    related_tasks: list[TaskID] = Field(default_factory=list)
    related_features: list[FeatureID] = Field(default_factory=list)

    @field_validator("created_at", mode="after")
    @classmethod
    def _validate_utc(cls, v: datetime.datetime) -> datetime.datetime:
        return _require_utc(v, "created_at")


class Review(BaseModel):
    """A human or agent review verdict on a PRD, Task, or Feature."""

    model_config = _MODEL_CONFIG

    id: ReviewID
    target_kind: ReviewTargetKind
    target_id: str
    reviewed_by: str
    decision: ReviewDecision
    notes: str | None = None
    rejection_category: RejectionCategory | None = None
    counts_toward_accept_rate: StrictBool = True
    rejection: TaskRejectionProvenance | None = None
    created_at: datetime.datetime

    @model_validator(mode="after")
    def _validate_rejection_provenance(self) -> Review:
        if self.rejection is not None and self.target_kind is not ReviewTargetKind.task:
            raise ValueError("rejection provenance is task-review-only")
        if self.rejection is not None and self.decision is not ReviewDecision.needs_changes:
            raise ValueError("rejection provenance requires a rejected task review")
        if self.rejection is not None:
            if self.rejection_category is not self.rejection.category:
                raise ValueError("review rejection category/provenance mismatch")
            if (
                self.counts_toward_accept_rate
                is not self.rejection.counts_toward_accept_rate
            ):
                raise ValueError("review rejection accounting/provenance mismatch")
        return self

    @field_validator("created_at", mode="after")
    @classmethod
    def _validate_utc(cls, v: datetime.datetime) -> datetime.datetime:
        return _require_utc(v, "created_at")


class EventDraft(BaseModel):
    """An intended mutation whose event id has not yet been assigned.

    A draft carries every field of an :class:`Event` *except* ``id``. It is the
    input to the backend write path (``append(draft) -> Event``): the backend
    validates the draft, assigns the next monotonic id from the log, and
    materializes it into an :class:`Event`. The type system therefore prevents
    handing an unassigned draft to replay, or a materialized ``Event`` to
    ``append``.

    Field set (the materialized ``Event`` adds only ``id`` on top of these):
    - ``timestamp`` — UTC-aware; the moment the mutation was requested.
    - ``actor`` — who requested it.
    - ``action`` — the action name (e.g. ``"task.applied"``).
    - ``target_kind`` / ``target_id`` — what the mutation is about.
    - ``payload_json`` — the action-specific payload.
    """

    model_config = _MODEL_CONFIG

    timestamp: datetime.datetime
    actor: str
    action: str
    target_kind: str
    target_id: str
    payload_json: dict[str, Any] = Field(default_factory=dict)

    @field_validator("timestamp", mode="after")
    @classmethod
    def _validate_utc(cls, v: datetime.datetime) -> datetime.datetime:
        return _require_utc(v, "timestamp")


class Event(EventDraft):
    """An immutable append-only log entry — a draft assigned an id and applied.

    The event log is the audit trail; replaying it from scratch must reconstruct
    canonical SQLite state exactly. Events are never updated or deleted. An
    ``Event`` is an :class:`EventDraft` plus the ``id`` assigned by the backend
    at log-append time — monotonic ``E000001`` in local mode, hash-chained
    ``E-3f9a2c4d71be`` in git mode (v1.22.0, git-backed events Phase A).
    """

    id: EventID  # E000001 (local) or E-<12 hex> (git)

    # v1.22.0 — git-mode envelope fields. Populated only when the project
    # runs with ``events_storage: git``: ``parent_event_id`` is the id of the
    # previous event as seen by the writer (the log becomes a hash chain;
    # None marks the chain root), and ``lamport`` is the writer's max-seen
    # logical clock + 1, used by order-tolerant replay to sort merged logs
    # deterministically via (lamport, ts, id). Local mode leaves both None
    # and the write path omits them from the serialized JSONL line, so
    # pre-1.22.0 logs stay byte-identical.
    parent_event_id: EventID | None = None
    lamport: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _validate_event_id_format(self) -> Event:
        # SL1-RR-1 (write-path rework): the PENDING_EVENT_ID sentinel is retired.
        # The ``append(EventDraft)`` path assigns ids inside the flock critical
        # section, so every Event id must be in one of the two canonical
        # formats: monotonic ``E000001`` (local mode, from the log-authority
        # counter) or hash-chained ``E-<12 hex>`` (git mode, from
        # state/hashing.hash_event_id).
        is_monotonic = self.id.startswith("E") and self.id[1:].isdigit()
        is_hash = _HASH_EVENT_ID_RE.fullmatch(self.id) is not None
        if not (is_monotonic or is_hash):
            raise ValueError(
                "Event.id must be in monotonic format 'E000001' or "
                f"hash-chained format 'E-3f9a2c4d71be'; got {self.id!r}"
            )
        return self


class SyncMapping(BaseModel):
    """Tracks a Task's relationship to an issue in an external system.

    Fields
    ------
    task_id:
        FK into ``tasks`` for a ``entity_kind='task'`` mapping. ``None`` for a
        ``entity_kind='prd'`` mapping — a milestone/release-level mapping is
        owned by a PRD, not a single task, so it carries ``prd_id`` and a null
        ``task_id`` instead (enforced by the model_validator).
    prd_id:
        Owning PRD partition (v0.3 multi-PRD). For a task-kind mapping this is
        the task's owning PRD (stamped by the sync push path, T027); for a
        prd-kind (milestone) mapping it is the PRD the milestone tracks and is
        REQUIRED. ``exclude=True`` keeps this additive: the field round-trips as
        an in-memory attribute but stays out of ``model_dump()`` so existing
        event payloads / snapshot blobs stay byte-identical (it is persisted via
        the explicit :class:`anvil.state.payloads.SyncMappingUpsertedPayload`
        field + the sync_mappings ``prd_id`` column, not via the model dump).
    entity_kind:
        ``'task'`` (the default — a per-task issue mapping) or ``'prd'`` (a
        milestone/release-level mapping owned by a PRD). ``exclude=True`` for the
        same byte-identity reason as ``prd_id``.
    external_system:
        Provider id string (snake_case: ``github_issues``,
        ``"monday"``, ``"linear"``, etc.). Matches the key under which
        the provider is registered in
        :data:`anvil.sync.registry.PROVIDER_REGISTRY`. Not gated
        on the :class:`ExternalSystem` enum — contributor providers can
        register any string id and persist mappings under it.
    external_id:
        Provider-native record id (stringified for uniformity across
        providers).
    external_url:
        Optional human-facing URL to the remote record. Stored on the
        mapping so the CLI can render a link without a re-fetch.
    last_synced_at:
        UTC timestamp of the last successful round-trip.
    sync_state:
        Per-mapping conflict / health label (in_sync / local_ahead / ...).
    conflict_resolution_strategy:
        Per-mapping strategy (local_wins / remote_wins / prompt /
        manual_merge). Falls back to project-level config at the CLI
        layer if not set explicitly.
    provider_metadata:
        Opaque provider-specific extension dict. GitHub puts
        ``{"labels": [...], "assignees": [...]}`` here; Jira puts
        ``{"watchers": [...], "reporter": ...}``; etc. The
        reconciliation engine never inspects this — only the originating
        provider knows its shape.
    """

    model_config = _MODEL_CONFIG

    # task_id is nullable: a prd-kind (milestone) mapping carries prd_id and a
    # NULL task_id instead (see the model_validator below).
    task_id: TaskID | None = None
    # Multi-PRD partition (v0.3). Both fields default + exclude=True so a pre-
    # change ``sync_mapping.upserted`` event (which never carried them) and any
    # legacy sync_mappings row reconstruct cleanly, and the snapshot / event
    # payload byte-shape is unchanged. Persistence flows through the explicit
    # ``SyncMappingUpsertedPayload`` fields + the dedicated DB columns, not the
    # model dump — exactly the pattern the v7 PRD identity columns use.
    prd_id: PRDID | None = Field(default=None, exclude=True)
    entity_kind: Literal["task", "prd"] = Field(default="task", exclude=True)
    # ``external_system`` is ``str`` (not the ``ExternalSystem`` enum) so
    # that contributor-registered providers (e.g. ``"monday"``,
    # ``"linear"``, ``"my_custom_tracker"``) can persist mappings without
    # first having to patch the canonical-first-party enum. The DB column
    # is TEXT and the abstraction layer (registry / Protocol) only ever
    # carries the string ``provider_id``. See ``KNOWN_EXTERNAL_SYSTEMS``
    # for the docs-only tuple of first-party ids.
    external_system: str
    external_id: str
    external_url: str | None = None
    last_synced_at: datetime.datetime
    sync_state: SyncState = SyncState.in_sync
    conflict_resolution_strategy: ConflictResolutionStrategy = (
        ConflictResolutionStrategy.prompt
    )
    provider_metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("last_synced_at", mode="after")
    @classmethod
    def _validate_utc(cls, v: datetime.datetime) -> datetime.datetime:
        return _require_utc(v, "last_synced_at")

    @model_validator(mode="after")
    def _validate_entity_kind_invariants(self) -> SyncMapping:
        """Keep the (entity_kind, task_id, prd_id) trio internally consistent.

        Overloading ``task_id`` on prd-kind rows is what we are guarding against:
        a milestone (``entity_kind='prd'``) mapping is owned by a PRD, so it must
        carry a ``prd_id`` and a NULL ``task_id`` — otherwise
        ``get_sync_mapping`` / ``list_sync_mappings`` would surface it as if it
        were a task mapping. A task-kind row is the mirror image: it must carry a
        ``task_id`` (the FK into ``tasks``).
        """
        if self.entity_kind == "prd":
            if self.prd_id is None:
                raise ValueError(
                    "entity_kind='prd' SyncMapping requires a prd_id"
                )
            if self.task_id is not None:
                raise ValueError(
                    "entity_kind='prd' SyncMapping must have a null task_id "
                    "(a milestone mapping is owned by a PRD, not a task)"
                )
        else:  # entity_kind == 'task'
            if self.task_id is None:
                raise ValueError(
                    "entity_kind='task' SyncMapping requires a task_id"
                )
        return self


class ConflictGroup(BaseModel):
    """A named set of tasks whose expected_files overlap.

    Claiming one task in the group while another is active is allowed but warned.
    """

    model_config = _MODEL_CONFIG

    id: str
    name: str
    task_ids: list[TaskID] = Field(default_factory=list)
    reason: str
