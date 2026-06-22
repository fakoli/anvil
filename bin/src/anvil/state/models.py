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

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
    # Constants
    "DEFAULT_PRD_ID",
    # Enums
    "PRDStatus",
    "FeatureStatus",
    "TaskStatus",
    "TaskPriority",
    "TaskType",
    "ClaimType",
    "ClaimStatus",
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
    "Evidence",
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
    parent_task_id: TaskID | None = None
    created_at: datetime.datetime
    updated_at: datetime.datetime

    @field_validator("created_at", "updated_at", mode="after")
    @classmethod
    def _validate_utc(cls, v: datetime.datetime) -> datetime.datetime:
        return _require_utc(v, "created_at / updated_at")


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
        FK into ``tasks``.
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

    task_id: TaskID
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


class ConflictGroup(BaseModel):
    """A named set of tasks whose expected_files overlap.

    Claiming one task in the group while another is active is allowed but warned.
    """

    model_config = _MODEL_CONFIG

    id: str
    name: str
    task_ids: list[TaskID] = Field(default_factory=list)
    reason: str
