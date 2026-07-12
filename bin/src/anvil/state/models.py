"""Pydantic v2 models for anvil — the single source of truth for all entity types.

All other modules (sqlite backend, MCP tools, work-packet renderer, review gates)
import from here. If the types change, everything downstream changes with them.

Design decisions:
- StrEnum for every status / kind / decision field: grep-able, serialisable to str.
- All datetimes are timezone-aware UTC; a model_validator enforces tzinfo presence.
- Score dimensions are nullable until explicitly scored; Field(ge=1, le=5) when set.
- Type aliases (TaskID, FeatureID, …) are plain str — no over-engineering, but they
  give search-grep ability and document intent at every call site.
- ConfigDict(frozen=False, validate_assignment=True, extra='forbid') on every model:
  mutable for state transitions, but assignment-validated so transitions cannot
  smuggle bad values.
"""

from __future__ import annotations

import datetime
import enum
import json
import re
from typing import (  # noqa: UP035 — TypeAlias required for 3.11 compat
    Annotated,
    Any,
    Literal,
    TypeAlias,
)

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
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
    "ExternalSystem",
    "KNOWN_EXTERNAL_SYSTEMS",
    "SyncState",
    "ConflictResolutionStrategy",
    "ProofKind",
    # Models
    "Score",
    "Verification",
    "CommandProof",
    "DiffProof",
    "LinkProof",
    "AssertionProof",
    "ProofArtifact",
    "ProofRequirement",
    "Project",
    "PRD",
    "Requirement",
    "Feature",
    "Task",
    "Claim",
    "BundleClaim",
    "Evidence",
    "BundleReviewPolicy",
    "BundleThroughputBudget",
    "DelegatedAgentObservation",
    "BundleCheckpoint",
    "ExecutionBundle",
    "EventRange",
    "AcceptanceProof",
    "Decision",
    "Review",
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

    @field_validator("captured_at", mode="after")
    @classmethod
    def _validate_utc(cls, v: datetime.datetime) -> datetime.datetime:
        return _require_utc(v, "captured_at")


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
    CommandProof | DiffProof | LinkProof | AssertionProof,
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


class PRD(BaseModel):
    """Product Requirements Document — the gate that controls task claimability."""

    model_config = _MODEL_CONFIG

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
    revision: int = Field(default=1, ge=1, exclude=True)
    status: PRDStatus = PRDStatus.draft
    summary: str = ""
    goals: list[str] = Field(default_factory=list)
    non_goals: list[str] = Field(default_factory=list)
    requirements: list[RequirementID] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    last_reviewed_at: datetime.datetime | None = None
    last_reviewed_by: str | None = None
    created_at: datetime.datetime | None = Field(default=None, exclude=True)
    updated_at: datetime.datetime | None = Field(default=None, exclude=True)

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

    max_reviews: int = Field(default=1, ge=1)
    max_rereviews: int = Field(default=1, ge=0)
    independent_reviewer_required: bool = True
    required_angles: list[str] = Field(default_factory=list)


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
    command_results: list[CommandProof] = Field(default_factory=list)
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
    created_at: datetime.datetime

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
