"""Closed version-1 contracts for side-effect-free provider reads.

The models in this module are the public wire allowlist.  They intentionally
do not inherit from the mutable state models: adding an internal field must
never add it to a provider response.  Projection code must construct these
DTOs field by field and receives an atomic validation failure when ownership
or dependency invariants are not satisfied.

Every public model's Draft 2020-12 schema carries
``x-anvil-validation-contract-v1``.  That schema is a structural prefilter,
not the authorization boundary: providers must pass decoded documents through
``validate_public_wire_document`` for strict scalar/lexical checks and the
cross-field identity, provenance, ownership, hierarchy, and digest invariants
that standard JSON Schema cannot completely represent.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any, Literal, TypeAlias, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from anvil.state.hashing import (
    MAX_CANONICAL_JSON_RESPONSE_BYTES,
    CanonicalJsonRefusal,
    CanonicalJsonRefusalCode,
    canonical_json_bytes,
    canonical_node_budget_for_bytes,
    domain_separated_sha256,
)

PROJECT_SNAPSHOT_OPERATION_ID = "state.project.snapshot"
PROJECT_SNAPSHOT_OPERATION_VERSION: Literal[1] = 1
PROJECT_SNAPSHOT_SCHEMA_ID: Literal["anvil.state.project-snapshot.v1"] = (
    "anvil.state.project-snapshot.v1"
)
PROJECT_SNAPSHOT_DIGEST_DOMAIN = b"anvil.project-snapshot.v1\0"
WIRE_INT64_MAX = (2**63) - 1

PRD_CONTENT_OPERATION_ID = "state.prd.content"
PRD_CONTENT_OPERATION_VERSION: Literal[1] = 1
PRD_CONTENT_SCHEMA_ID = "anvil.state.prd-content.v1"
PROVIDER_READ_CONTRACT_FIXTURE_SHA256 = (
    "e1d20f10c98727ee88c68057d89ea1b1651d24d7eaa139ba4f16e33a000507a7"
)

_FULL_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_FULL_SHA256_RE = re.compile(_FULL_SHA256_PATTERN)
_PRD_ID_PATTERN_TEXT = r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$"
_FEATURE_ID_PATTERN_TEXT = r"^F[0-9]{3}(?:\.[0-9]+)*$"
_TASK_ID_PATTERN_TEXT = r"^T[0-9]{3}(?:\.[0-9]+)*$"
_PRD_ID_PATTERN = re.compile(_PRD_ID_PATTERN_TEXT)
_FEATURE_ID_PATTERN = re.compile(_FEATURE_ID_PATTERN_TEXT)
_TASK_ID_PATTERN = re.compile(_TASK_ID_PATTERN_TEXT)
_NO_LINE_TERMINATOR_SCHEMA: dict[str, Any] = {
    "not": {"pattern": r"[\r\n]"}
}
_OPTIONAL_NO_LINE_TERMINATOR_SCHEMA: dict[str, Any] = {
    "if": {"type": "string"},
    "then": _NO_LINE_TERMINATOR_SCHEMA,
}

PUBLIC_SCHEMA_CONTRACT_KEY_V1 = "x-anvil-validation-contract-v1"
_PUBLIC_SCHEMA_RUNTIME_INVARIANTS = [
    "strict decoded scalar types and JSON lexical distinctions",
    "cross-field identity and provenance",
    "cross-record ownership and hierarchy",
    "snapshot digest equality",
]

FullSha256: TypeAlias = str
TaskKey: TypeAlias = tuple[str, str]
PrdStatusV1: TypeAlias = Literal["draft", "reviewed", "approved", "rejected"]
FeatureStatusV1: TypeAlias = Literal["proposed", "ready", "in_progress", "done"]
TaskStatusV1: TypeAlias = Literal[
    "proposed",
    "drafted",
    "reviewed",
    "ready",
    "claimed",
    "in_progress",
    "blocked",
    "needs_review",
    "accepted",
    "done",
    "rejected",
]
TaskPriorityV1: TypeAlias = Literal["low", "medium", "high", "critical"]
ProviderOperationIdV1: TypeAlias = Literal[
    "state.project.snapshot",
    "state.prd.content",
]
ReadErrorFieldV1: TypeAlias = Literal[
    "request",
    "prd_id",
    "expected_digest",
    "sections",
    "state",
    "schema",
    "projection",
    "prds",
    "features",
    "tasks",
    "dependencies",
    "acceptance_criteria",
    "content",
    "snapshot",
]
ReadErrorMessageV1: TypeAlias = Literal[
    "The read request is invalid.",
    "The requested identifier is invalid.",
    "The project hierarchy is invalid.",
    "A referenced hierarchy target is unavailable.",
    "The hierarchy contains a duplicate edge.",
    "The task dependency graph contains a cycle.",
    "A provider read limit was exceeded.",
    "Project state is unavailable.",
    "The project schema is incompatible.",
    "The project projection is not converged.",
    "The requested PRD was not found.",
    "The requested PRD content is unavailable.",
    "The expected PRD digest is stale.",
    "The persisted PRD content is not valid UTF-8.",
    "The persisted PRD source binding is inconsistent.",
    "The requested PRD section selection is invalid.",
]

def _apply_public_schema_contract(schema: dict[str, Any]) -> None:
    schema[PUBLIC_SCHEMA_CONTRACT_KEY_V1] = {
        "standard_schema_role": "structural-prefilter",
        "authoritative_validator": (
            "anvil.read_contracts.validate_public_wire_document"
        ),
        "runtime_required_for": list(_PUBLIC_SCHEMA_RUNTIME_INVARIANTS),
    }


_WIRE_CONFIG = ConfigDict(
    extra="forbid",
    frozen=True,
    strict=True,
    revalidate_instances="always",
    json_schema_extra=_apply_public_schema_contract,
)


class ReadErrorCode(enum.StrEnum):
    """Stable version-1 refusal codes shared by both read operations."""

    invalid_request = "invalid_request"
    invalid_identifier = "invalid_identifier"
    invalid_hierarchy = "invalid_hierarchy"
    missing_target = "missing_target"
    duplicate_edge = "duplicate_edge"
    dependency_cycle = "dependency_cycle"
    limit_exceeded = "limit_exceeded"
    state_unavailable = "state_unavailable"
    schema_incompatible = "schema_incompatible"
    projection_not_converged = "projection_not_converged"
    prd_not_found = "prd_not_found"
    content_unavailable = "content_unavailable"
    stale_digest = "stale_digest"
    invalid_utf8 = "invalid_utf8"
    source_drift = "source_drift"
    invalid_section = "invalid_section"


class ProviderLimitNameV1(enum.StrEnum):
    """Closed names for every immutable or caller-lowerable v1 ceiling."""

    max_prds = "max_prds"
    max_features = "max_features"
    max_tasks = "max_tasks"
    max_dependency_edges = "max_dependency_edges"
    max_dependencies_per_task = "max_dependencies_per_task"
    max_acceptance_criteria_per_task = "max_acceptance_criteria_per_task"
    max_verification_summaries_per_task = "max_verification_summaries_per_task"
    max_verification_summary_label_bytes = "max_verification_summary_label_bytes"
    max_string_bytes = "max_string_bytes"
    max_snapshot_bytes = "max_snapshot_bytes"
    max_response_bytes = "max_response_bytes"
    max_prd_content_bytes = "max_prd_content_bytes"
    max_canonical_json_depth = "max_canonical_json_depth"
    max_diagnostic_bytes = "max_diagnostic_bytes"


_SAFE_ERROR_MESSAGES: dict[ReadErrorCode, str] = {
    ReadErrorCode.invalid_request: "The read request is invalid.",
    ReadErrorCode.invalid_identifier: "The requested identifier is invalid.",
    ReadErrorCode.invalid_hierarchy: "The project hierarchy is invalid.",
    ReadErrorCode.missing_target: "A referenced hierarchy target is unavailable.",
    ReadErrorCode.duplicate_edge: "The hierarchy contains a duplicate edge.",
    ReadErrorCode.dependency_cycle: "The task dependency graph contains a cycle.",
    ReadErrorCode.limit_exceeded: "A provider read limit was exceeded.",
    ReadErrorCode.state_unavailable: "Project state is unavailable.",
    ReadErrorCode.schema_incompatible: "The project schema is incompatible.",
    ReadErrorCode.projection_not_converged: "The project projection is not converged.",
    ReadErrorCode.prd_not_found: "The requested PRD was not found.",
    ReadErrorCode.content_unavailable: "The requested PRD content is unavailable.",
    ReadErrorCode.stale_digest: "The expected PRD digest is stale.",
    ReadErrorCode.invalid_utf8: "The persisted PRD content is not valid UTF-8.",
    ReadErrorCode.source_drift: "The persisted PRD source binding is inconsistent.",
    ReadErrorCode.invalid_section: "The requested PRD section selection is invalid.",
}


def _read_error_json_schema(schema: dict[str, Any]) -> None:
    """Describe the derived message input and its code-specific constraint."""
    required = schema.get("required")
    if isinstance(required, list):
        schema["required"] = [field for field in required if field != "message"]
    schema["allOf"] = [
        {
            "if": {
                "properties": {"code": {"const": code.value}},
                "required": ["code"],
            },
            "then": {"properties": {"message": {"const": message}}},
        }
        for code, message in _SAFE_ERROR_MESSAGES.items()
    ]
    _apply_public_schema_contract(schema)


class ReadErrorV1(BaseModel):
    """Closed machine-readable refusal body; never carries exception text."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
        json_schema_extra=_read_error_json_schema,
    )

    schema_id: Literal["anvil.state.read-error.v1"] = "anvil.state.read-error.v1"
    code: ReadErrorCode
    message: ReadErrorMessageV1
    field: ReadErrorFieldV1 | None = None
    actual: int | None = Field(default=None, ge=0, le=WIRE_INT64_MAX)
    limit: int | None = Field(default=None, ge=0, le=WIRE_INT64_MAX)

    @model_validator(mode="before")
    @classmethod
    def bind_safe_message(cls, value: Any) -> Any:
        """Inject or verify the one fixed rendering belonging to the code."""
        if not isinstance(value, Mapping):
            return value
        data = dict(value)
        raw_code = data.get("code")
        try:
            if isinstance(raw_code, ReadErrorCode):
                code = raw_code
            elif isinstance(raw_code, str):
                code = ReadErrorCode(raw_code)
            else:
                return data
        except (TypeError, ValueError):
            return data
        expected = _SAFE_ERROR_MESSAGES[code]
        if "message" in data and data["message"] != expected:
            raise ValueError("error message must match the closed rendering for its code")
        data["code"] = code
        data["message"] = expected
        return data


class ProviderReadLimitsV1(BaseModel):
    """Immutable provider ceilings (or a validated lower requested set)."""

    model_config = _WIRE_CONFIG

    max_prds: int = Field(default=128, ge=1, le=128)
    max_features: int = Field(default=4096, ge=1, le=4096)
    max_tasks: int = Field(default=50_000, ge=1, le=50_000)
    max_dependency_edges: int = Field(default=200_000, ge=0, le=200_000)
    max_dependencies_per_task: int = Field(default=512, ge=0, le=512)
    max_acceptance_criteria_per_task: int = Field(default=256, ge=0, le=256)
    max_verification_summaries_per_task: int = Field(default=256, ge=0, le=256)
    max_verification_summary_label_bytes: int = Field(default=4096, ge=1, le=4096)
    max_string_bytes: int = Field(default=65_536, ge=1, le=65_536)
    max_snapshot_bytes: int = Field(default=16_777_216, ge=1, le=16_777_216)
    max_response_bytes: int = Field(
        default=MAX_CANONICAL_JSON_RESPONSE_BYTES,
        ge=1,
        le=MAX_CANONICAL_JSON_RESPONSE_BYTES,
    )
    max_prd_content_bytes: int = Field(default=2_097_152, ge=1, le=2_097_152)
    max_canonical_json_depth: int = Field(default=128, ge=1, le=128)
    max_diagnostic_bytes: int = Field(default=4096, ge=1, le=4096)


class ProviderLimitRefusalV1(BaseModel):
    """Exact value-safe metadata for a v1 provider-limit refusal."""

    model_config = _WIRE_CONFIG

    code: Literal[ReadErrorCode.limit_exceeded] = ReadErrorCode.limit_exceeded
    operation_id: ProviderOperationIdV1
    operation_version: Literal[1] = 1
    limit_name: ProviderLimitNameV1
    actual: int = Field(ge=0, le=WIRE_INT64_MAX)
    limit: int = Field(ge=0, le=WIRE_INT64_MAX)

    @model_validator(mode="before")
    @classmethod
    def accept_closed_json_enums(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        data = dict(value)
        if type(data.get("code")) is str:
            data["code"] = ReadErrorCode(data["code"])
        if type(data.get("limit_name")) is str:
            data["limit_name"] = ProviderLimitNameV1(data["limit_name"])
        return data


PROVIDER_LIMITS_V1 = ProviderReadLimitsV1()
MAX_PROVIDER_LIMIT_REQUEST_FIELDS = len(ProviderReadLimitsV1.model_fields)
_PROVIDER_LIMIT_FIELDS = (
    "max_prds",
    "max_features",
    "max_tasks",
    "max_dependency_edges",
    "max_dependencies_per_task",
    "max_acceptance_criteria_per_task",
    "max_verification_summaries_per_task",
    "max_verification_summary_label_bytes",
    "max_string_bytes",
    "max_snapshot_bytes",
    "max_response_bytes",
    "max_prd_content_bytes",
    "max_canonical_json_depth",
    "max_diagnostic_bytes",
)
_PROVIDER_LIMIT_FIELD_SET = frozenset(_PROVIDER_LIMIT_FIELDS)
_MAX_PROVIDER_LIMIT_FIELD_NAME_CHARS = 64


def _derive_minimum_project_snapshot_response_bytes() -> int:
    """Return the exact size of the smallest schema-valid response shape.

    The payload uses the shortest valid project strings and empty hierarchy
    arrays.  Its lowered limits are the smallest values that still admit that
    payload and the two mandatory 64-byte hexadecimal digests.  The response
    ceiling is serialized inside the response, so converge it to the fixed
    point where the declared ceiling equals the canonical response size.

    Computing this once from the public wire shape keeps the cheap preflight
    bound sound if a mandatory field name or fixed literal changes.  It is not
    computed from, and never traverses, caller-controlled response data.
    """
    payload: dict[str, Any] = {
        "schema_id": PROJECT_SNAPSHOT_SCHEMA_ID,
        "operation_version": PROJECT_SNAPSHOT_OPERATION_VERSION,
        "project": {"project_id": "x", "name": "x"},
        "prds": [],
        "features": [],
        "tasks": [],
    }
    payload_bytes = canonical_json_bytes(payload)
    payload_digest = domain_separated_sha256(
        PROJECT_SNAPSHOT_DIGEST_DOMAIN,
        payload,
    )
    ceiling = 1
    while True:
        limits = ProviderReadLimitsV1(
            max_prds=1,
            max_features=1,
            max_tasks=1,
            max_dependencies_per_task=0,
            max_acceptance_criteria_per_task=0,
            max_string_bytes=64,
            max_snapshot_bytes=len(payload_bytes),
            max_response_bytes=ceiling,
            max_prd_content_bytes=1,
        )
        document = {
            "payload": payload,
            "event_cursor": {
                "event_count": 0,
                "event_frontier_sha256": "0" * 64,
            },
            "applied_limits": limits.model_dump(mode="json"),
            "snapshot_digest": payload_digest,
        }
        exact_size = len(canonical_json_bytes(document))
        if exact_size == ceiling:
            return exact_size
        ceiling = exact_size


MIN_PROJECT_SNAPSHOT_RESPONSE_BYTES = (
    _derive_minimum_project_snapshot_response_bytes()
)


class PrdScopedRefV1(BaseModel):
    model_config = _WIRE_CONFIG

    prd_id: str = Field(
        pattern=_PRD_ID_PATTERN_TEXT,
        json_schema_extra=_NO_LINE_TERMINATOR_SCHEMA,
    )

    @model_validator(mode="after")
    def validate_prd_id(self) -> PrdScopedRefV1:
        _require_prd_id(self.prd_id)
        return self


class FeatureScopedRefV1(BaseModel):
    model_config = _WIRE_CONFIG

    prd_id: str = Field(
        pattern=_PRD_ID_PATTERN_TEXT,
        json_schema_extra=_NO_LINE_TERMINATOR_SCHEMA,
    )
    feature_id: str = Field(
        pattern=_FEATURE_ID_PATTERN_TEXT,
        json_schema_extra=_NO_LINE_TERMINATOR_SCHEMA,
    )

    @model_validator(mode="after")
    def validate_ids(self) -> FeatureScopedRefV1:
        _require_prd_id(self.prd_id)
        _require_local_id(self.feature_id, pattern=_FEATURE_ID_PATTERN, kind="feature")
        return self


class TaskScopedRefV1(BaseModel):
    model_config = _WIRE_CONFIG

    prd_id: str = Field(
        pattern=_PRD_ID_PATTERN_TEXT,
        json_schema_extra=_NO_LINE_TERMINATOR_SCHEMA,
    )
    task_id: str = Field(
        pattern=_TASK_ID_PATTERN_TEXT,
        json_schema_extra=_NO_LINE_TERMINATOR_SCHEMA,
    )

    @model_validator(mode="after")
    def validate_ids(self) -> TaskScopedRefV1:
        _require_prd_id(self.prd_id)
        _require_local_id(self.task_id, pattern=_TASK_ID_PATTERN, kind="task")
        return self


class ProjectRecordV1(BaseModel):
    model_config = _WIRE_CONFIG

    project_id: str = Field(min_length=1)
    name: str = Field(min_length=1)

    @field_validator("project_id", "name", mode="before")
    @classmethod
    def validate_bounded_strings(cls, value: Any) -> Any:
        return _require_utf8_bytes(value, limit=65_536, path="$.project")


class PrdRecordV1(BaseModel):
    model_config = _WIRE_CONFIG

    ref: PrdScopedRefV1
    local_id: str = Field(
        pattern=_PRD_ID_PATTERN_TEXT,
        json_schema_extra=_NO_LINE_TERMINATOR_SCHEMA,
    )
    title: str = Field(min_length=1)
    revision: int = Field(ge=1, le=WIRE_INT64_MAX)
    status: PrdStatusV1
    target_version: str | None = None
    target_tag: str | None = None
    source_sha256: str | None = Field(
        default=None,
        pattern=_FULL_SHA256_PATTERN,
        json_schema_extra=_OPTIONAL_NO_LINE_TERMINATOR_SCHEMA,
    )
    source_size_bytes: int | None = Field(default=None, ge=0, le=WIRE_INT64_MAX)
    source_encoding: Literal["utf-8"] | None = None
    provenance_state: Literal["available", "legacy_unbound"]
    content_available: bool

    @field_validator("source_sha256", mode="before")
    @classmethod
    def validate_source_sha256(cls, value: Any) -> Any:
        if type(value) is str and _FULL_SHA256_RE.fullmatch(value) is None:
            raise ValueError("value must be a full lowercase SHA-256 digest")
        return value

    @field_validator("title", "target_version", "target_tag", mode="before")
    @classmethod
    def validate_bounded_strings(cls, value: Any) -> Any:
        return _require_utf8_bytes(value, limit=65_536, path="$.prd")

    @model_validator(mode="after")
    def validate_identity_and_provenance(self) -> PrdRecordV1:
        _require_prd_id(self.local_id)
        if self.local_id != self.ref.prd_id:
            raise ValueError("PRD local_id must equal ref.prd_id")
        provenance = (self.source_sha256, self.source_size_bytes, self.source_encoding)
        if self.provenance_state == "available":
            if not self.content_available or any(value is None for value in provenance):
                raise ValueError(
                    "available provenance requires digest, size, encoding, and content"
                )
        elif self.content_available or any(value is not None for value in provenance):
            raise ValueError("legacy-unbound provenance cannot fabricate source metadata")
        return self


class FeatureRecordV1(BaseModel):
    model_config = _WIRE_CONFIG

    ref: FeatureScopedRefV1
    local_id: str = Field(
        pattern=_FEATURE_ID_PATTERN_TEXT,
        json_schema_extra=_NO_LINE_TERMINATOR_SCHEMA,
    )
    prd_ref: PrdScopedRefV1
    title: str = Field(min_length=1)
    status: FeatureStatusV1

    @field_validator("title", mode="before")
    @classmethod
    def validate_bounded_title(cls, value: Any) -> Any:
        return _require_utf8_bytes(value, limit=65_536, path="$.feature.title")

    @model_validator(mode="after")
    def validate_identity(self) -> FeatureRecordV1:
        _require_local_id(self.local_id, pattern=_FEATURE_ID_PATTERN, kind="feature")
        if self.ref.feature_id != self.local_id:
            raise ValueError("feature local_id must equal ref.feature_id")
        if self.ref.prd_id != self.prd_ref.prd_id:
            raise ValueError("feature ref and owning PRD must have the same PRD scope")
        return self


class VerificationKindV1(enum.StrEnum):
    """Closed, non-executable categories represented in task summaries."""

    command = "command"
    manual_step = "manual_step"
    required_evidence = "required_evidence"
    typed_proof = "typed_proof"


class VerificationSummaryV1(BaseModel):
    model_config = _WIRE_CONFIG

    kind: VerificationKindV1
    label: str = Field(min_length=1)
    count: int = Field(ge=1, le=WIRE_INT64_MAX)

    @model_validator(mode="before")
    @classmethod
    def accept_closed_json_kind(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        data = dict(value)
        if type(data.get("kind")) is str:
            data["kind"] = VerificationKindV1(data["kind"])
        return data

    @field_validator("label", mode="before")
    @classmethod
    def validate_bounded_label(cls, value: Any) -> Any:
        return _require_utf8_bytes(value, limit=4096, path="$.verification.label")


class TaskRecordV1(BaseModel):
    model_config = _WIRE_CONFIG

    ref: TaskScopedRefV1
    local_id: str = Field(
        pattern=_TASK_ID_PATTERN_TEXT,
        json_schema_extra=_NO_LINE_TERMINATOR_SCHEMA,
    )
    prd_ref: PrdScopedRefV1
    feature_ref: FeatureScopedRefV1
    parent_ref: TaskScopedRefV1 | None = None
    title: str = Field(min_length=1)
    status: TaskStatusV1
    priority: TaskPriorityV1
    dependency_refs: tuple[TaskScopedRefV1, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    verification_summaries: tuple[VerificationSummaryV1, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def accept_decoded_json_arrays(cls, value: Any) -> Any:
        """Accept JSON lists without relaxing strict scalar validation."""
        if not isinstance(value, Mapping):
            return value
        data = dict(value)
        data["title"] = _require_utf8_bytes(
            data.get("title"),
            limit=65_536,
            path="$.title",
        )
        for field in (
            "dependency_refs",
            "acceptance_criteria",
            "verification_summaries",
        ):
            collection = _plain_wire_sequence(data.get(field), field=field)
            if field == "acceptance_criteria" and collection is not None:
                collection = tuple(
                    _require_utf8_bytes(
                        item,
                        limit=65_536,
                        path=f"$.acceptance_criteria[{index}]",
                    )
                    for index, item in enumerate(collection)
                )
                data[field] = collection
            if type(collection) is list:
                data[field] = tuple(collection)
        return data

    @model_validator(mode="after")
    def validate_identity(self) -> TaskRecordV1:
        _require_local_id(self.local_id, pattern=_TASK_ID_PATTERN, kind="task")
        if self.ref.task_id != self.local_id:
            raise ValueError("task local_id must equal ref.task_id")
        if self.ref.prd_id != self.prd_ref.prd_id:
            raise ValueError("task ref and owning PRD must have the same PRD scope")
        if self.feature_ref.prd_id != self.prd_ref.prd_id:
            raise ValueError("task feature ownership cannot cross PRD scope")
        if self.parent_ref is not None and self.parent_ref.prd_id != self.prd_ref.prd_id:
            raise ValueError("task parent ownership cannot cross PRD scope")
        return self


class ProjectSnapshotPayloadV1(BaseModel):
    """Digest-bearing allowlisted hierarchy, excluding operational envelope data."""

    model_config = _WIRE_CONFIG

    schema_id: Literal["anvil.state.project-snapshot.v1"] = PROJECT_SNAPSHOT_SCHEMA_ID
    operation_version: Literal[1] = PROJECT_SNAPSHOT_OPERATION_VERSION
    project: ProjectRecordV1
    prds: tuple[PrdRecordV1, ...]
    features: tuple[FeatureRecordV1, ...]
    tasks: tuple[TaskRecordV1, ...]

    @model_validator(mode="before")
    @classmethod
    def preflight_raw_limits(cls, value: Any) -> Any:
        """Refuse structurally impossible wire input before nested DTO parsing."""
        if isinstance(value, Mapping):
            _preflight_snapshot_value(value, PROVIDER_LIMITS_V1)
            # Both ``model_validate_json`` and an already-decoded JSON
            # document use lists for wire arrays.  Convert only exact lists at
            # the tuple-shaped public fields; strict nested/scalar validation
            # remains in force for every other Python value.
            value = dict(value)
            for name in ("prds", "features", "tasks"):
                collection = value.get(name)
                if type(collection) is list:
                    value[name] = tuple(collection)
        return value

    @model_validator(mode="after")
    def validate_hierarchy(self) -> ProjectSnapshotPayloadV1:
        _validate_snapshot_shape_limits(self, PROVIDER_LIMITS_V1)
        _validate_hierarchy(self)
        _validate_snapshot_serialized_limit(self, PROVIDER_LIMITS_V1)
        return self


class EventCursorV1(BaseModel):
    model_config = _WIRE_CONFIG

    event_count: int = Field(ge=0, le=WIRE_INT64_MAX)
    event_frontier_sha256: str = Field(
        pattern=_FULL_SHA256_PATTERN,
        json_schema_extra=_NO_LINE_TERMINATOR_SCHEMA,
    )

    @field_validator("event_frontier_sha256", mode="before")
    @classmethod
    def validate_event_frontier_sha256(cls, value: Any) -> Any:
        if type(value) is str and _FULL_SHA256_RE.fullmatch(value) is None:
            raise ValueError("value must be a full lowercase SHA-256 digest")
        return value


class ProjectSnapshotDataV1(BaseModel):
    """Operation data including cursor and limits outside digest material."""

    model_config = _WIRE_CONFIG

    payload: ProjectSnapshotPayloadV1
    event_cursor: EventCursorV1
    applied_limits: ProviderReadLimitsV1
    snapshot_digest: str = Field(
        pattern=_FULL_SHA256_PATTERN,
        json_schema_extra=_NO_LINE_TERMINATOR_SCHEMA,
    )

    @field_validator("snapshot_digest", mode="before")
    @classmethod
    def validate_snapshot_digest(cls, value: Any) -> Any:
        if type(value) is str and _FULL_SHA256_RE.fullmatch(value) is None:
            raise ValueError("value must be a full lowercase SHA-256 digest")
        return value

    @model_validator(mode="before")
    @classmethod
    def preflight_response_ceiling(cls, value: Any) -> Any:
        """Apply caller ceilings before nested DTO or hierarchy validation."""
        if not isinstance(value, Mapping):
            return value
        raw_limits = value.get("applied_limits")
        if isinstance(raw_limits, ProviderReadLimitsV1):
            ceiling = raw_limits.max_response_bytes
        elif isinstance(raw_limits, Mapping):
            ceiling = raw_limits.get(
                "max_response_bytes",
                PROVIDER_LIMITS_V1.max_response_bytes,
            )
        else:
            raise ValueError("applied_limits must be a mapping")
        if type(ceiling) is not int:
            raise ValueError("response byte ceiling must be an integer")
        if not 1 <= ceiling <= PROVIDER_LIMITS_V1.max_response_bytes:
            raise ValueError("response byte ceiling is outside provider bounds")
        if ceiling < MIN_PROJECT_SNAPSHOT_RESPONSE_BYTES:
            raise ValueError(
                "response byte ceiling cannot fit invariant response fields"
            )
        validated_limits = ProviderReadLimitsV1.model_validate(raw_limits)
        _preflight_snapshot_value(value.get("payload"), validated_limits)
        _preflight_response_value(value, validated_limits)
        return value

    @model_validator(mode="after")
    def validate_digest(self) -> ProjectSnapshotDataV1:
        _validate_snapshot_limits_validated(self.payload, self.applied_limits)
        # Nested DTOs were revalidated while constructing this response, so
        # avoid a second full hierarchy parse solely to recompute its digest.
        if self.snapshot_digest != _validated_snapshot_digest(self.payload):
            raise ValueError("snapshot_digest does not match the allowlisted payload")
        _validate_response_serialized_limit(self, self.applied_limits)
        return self


_PublicReadModelT = TypeVar("_PublicReadModelT", bound=BaseModel)
_PUBLIC_READ_MODEL_TYPES: tuple[type[BaseModel], ...] = (
    EventCursorV1,
    FeatureRecordV1,
    FeatureScopedRefV1,
    PrdRecordV1,
    PrdScopedRefV1,
    ProjectRecordV1,
    ProjectSnapshotDataV1,
    ProjectSnapshotPayloadV1,
    ProviderReadLimitsV1,
    ReadErrorV1,
    TaskRecordV1,
    TaskScopedRefV1,
    VerificationSummaryV1,
    ProviderLimitRefusalV1,
)


def validate_public_wire_document(
    model: type[_PublicReadModelT],
    document: Mapping[str, Any],
) -> _PublicReadModelT:
    """Apply the authoritative runtime checks after an optional schema prefilter."""
    if model not in _PUBLIC_READ_MODEL_TYPES:
        raise TypeError("model is not a public read-contract DTO")
    return model.model_validate(document)


def snapshot_digest(
    payload: ProjectSnapshotPayloadV1 | Mapping[str, Any],
) -> FullSha256:
    """Return the v1 digest of only schema/version and allowlisted hierarchy."""
    if isinstance(payload, ProjectSnapshotPayloadV1):
        # ``model_copy(update=...)`` deliberately skips validation.  Apply the
        # cheap container-integrity bound, then ask Pydantic to recursively
        # revalidate the instance exactly once.  Mapping inputs still pass
        # through canonical JSON so arbitrary safe Mapping implementations are
        # materialized without changing the digest contract.
        _preflight_typed_snapshot(payload)
        validated = ProjectSnapshotPayloadV1.model_validate(payload)
    else:
        _validate_raw_snapshot_limits(payload, PROVIDER_LIMITS_V1)
        validated = ProjectSnapshotPayloadV1.model_validate_json(
            canonical_json_bytes(
                payload,
                max_depth=PROVIDER_LIMITS_V1.max_canonical_json_depth,
                max_nodes=canonical_node_budget_for_bytes(
                    PROVIDER_LIMITS_V1.max_snapshot_bytes
                ),
                max_bytes=PROVIDER_LIMITS_V1.max_snapshot_bytes,
                max_string_bytes=PROVIDER_LIMITS_V1.max_string_bytes,
            )
        )
    return _validated_snapshot_digest(validated)


def _validated_snapshot_digest(payload: ProjectSnapshotPayloadV1) -> FullSha256:
    """Hash a payload already validated at its immediate public boundary."""
    digest_document = payload.model_dump(mode="json")
    return domain_separated_sha256(
        PROJECT_SNAPSHOT_DIGEST_DOMAIN,
        digest_document,
        max_depth=PROVIDER_LIMITS_V1.max_canonical_json_depth,
        max_nodes=canonical_node_budget_for_bytes(
            PROVIDER_LIMITS_V1.max_snapshot_bytes
        ),
        max_bytes=PROVIDER_LIMITS_V1.max_snapshot_bytes,
        max_string_bytes=PROVIDER_LIMITS_V1.max_string_bytes,
    )


def snapshot_canonical_bytes(payload: ProjectSnapshotPayloadV1) -> bytes:
    """Expose exact digest preimage JSON for cross-runtime qualification vectors."""
    _preflight_typed_snapshot(payload)
    validated = ProjectSnapshotPayloadV1.model_validate(payload)
    return canonical_json_bytes(
        validated.model_dump(mode="json"),
        max_depth=PROVIDER_LIMITS_V1.max_canonical_json_depth,
        max_nodes=canonical_node_budget_for_bytes(
            PROVIDER_LIMITS_V1.max_snapshot_bytes
        ),
        max_bytes=PROVIDER_LIMITS_V1.max_snapshot_bytes,
        max_string_bytes=PROVIDER_LIMITS_V1.max_string_bytes,
    )


def snapshot_response_canonical_bytes(response: ProjectSnapshotDataV1) -> bytes:
    """Return canonical bytes for the complete bounded operation data."""
    validated = ProjectSnapshotDataV1.model_validate(response)
    limits = validated.applied_limits
    return canonical_json_bytes(
        validated.model_dump(mode="json"),
        max_depth=limits.max_canonical_json_depth,
        max_nodes=canonical_node_budget_for_bytes(limits.max_response_bytes),
        max_bytes=limits.max_response_bytes,
        max_string_bytes=min(
            limits.max_string_bytes,
            limits.max_response_bytes,
        ),
    )


def lowered_limits(requested: Mapping[str, Any]) -> ProviderReadLimitsV1:
    """Validate a small plain request without reflecting caller-controlled keys."""
    if type(requested) is not dict:
        raise TypeError("provider limit request must be a plain object")
    if len(requested) > MAX_PROVIDER_LIMIT_REQUEST_FIELDS:
        raise ValueError("provider limit request has too many fields")
    for name in requested:
        if (
            type(name) is not str
            or len(name) > _MAX_PROVIDER_LIMIT_FIELD_NAME_CHARS
            or name not in _PROVIDER_LIMIT_FIELD_SET
        ):
            raise ValueError("unknown provider limit field")
    merged = PROVIDER_LIMITS_V1.model_dump()
    for name in _PROVIDER_LIMIT_FIELDS:
        if name not in requested:
            continue
        value = requested[name]
        if type(value) is not int:
            raise TypeError("provider limit value must be an integer")
        if value > merged[name]:
            raise ValueError("provider limit may only be lowered")
        merged[name] = value
    return ProviderReadLimitsV1.model_validate(merged)


def validate_snapshot_limits(
    snapshot: ProjectSnapshotPayloadV1,
    limits: ProviderReadLimitsV1,
) -> None:
    """Refuse a hierarchy exceeding provider or caller-lowered ceilings."""
    validated_limits = ProviderReadLimitsV1.model_validate(limits)
    _preflight_snapshot_value(snapshot, validated_limits)
    validated_snapshot = ProjectSnapshotPayloadV1.model_validate(snapshot)
    _validate_snapshot_limits_validated(validated_snapshot, validated_limits)


def _validate_snapshot_limits_validated(
    snapshot: ProjectSnapshotPayloadV1,
    limits: ProviderReadLimitsV1,
) -> None:
    """Apply caller limits to DTOs revalidated at their public boundary."""
    _validate_snapshot_shape_limits(snapshot, limits)
    _validate_snapshot_serialized_limit(snapshot, limits)


def _validate_snapshot_shape_limits(
    snapshot: ProjectSnapshotPayloadV1,
    limits: ProviderReadLimitsV1,
) -> None:
    """Apply cheap count/string gates before any hierarchy graph construction."""
    if len(snapshot.prds) > limits.max_prds:
        raise ValueError("PRD count exceeds provider limit")
    if len(snapshot.features) > limits.max_features:
        raise ValueError("feature count exceeds provider limit")
    if len(snapshot.tasks) > limits.max_tasks:
        raise ValueError("task count exceeds provider limit")
    for task in snapshot.tasks:
        if len(task.dependency_refs) > limits.max_dependencies_per_task:
            raise ValueError("task dependency count exceeds provider limit")
        if len(task.acceptance_criteria) > limits.max_acceptance_criteria_per_task:
            raise ValueError("task acceptance-criteria count exceeds provider limit")
        if (
            len(task.verification_summaries)
            > limits.max_verification_summaries_per_task
        ):
            raise ValueError("task verification-summary count exceeds provider limit")
        for summary in task.verification_summaries:
            _require_utf8_bytes(
                summary.label,
                limit=limits.max_verification_summary_label_bytes,
                path="$.tasks.verification_summaries.label",
            )

    _validate_snapshot_aggregate_lower_bound(snapshot, limits)
    document = snapshot.model_dump(mode="json")
    for value in _walk_strings(document):
        _require_utf8_bytes(value, limit=limits.max_string_bytes, path="$")


def _validate_snapshot_aggregate_lower_bound(
    snapshot: ProjectSnapshotPayloadV1,
    limits: ProviderReadLimitsV1,
) -> None:
    """Reject structurally impossible payloads without copying nested values.

    The accounting deliberately underestimates canonical JSON: each container
    contributes only its braces/brackets, each string only its quotes, and
    field names/separators/scalar contents are omitted.  Crossing either bound
    therefore proves the real payload cannot fit, while an admitted payload
    proceeds to exact validation after hierarchy checks.
    """
    _validate_snapshot_aggregate_counts(
        prd_count=len(snapshot.prds),
        feature_count=len(snapshot.features),
        task_shapes=(
            (
                task.parent_ref is not None,
                len(task.dependency_refs),
                len(task.acceptance_criteria),
                len(task.verification_summaries),
            )
            for task in snapshot.tasks
        ),
        limits=limits,
    )


def _validate_raw_snapshot_limits(
    payload: Mapping[str, Any],
    limits: ProviderReadLimitsV1,
) -> None:
    """Reject impossible raw collections before nested DTO materialization."""
    operation_version = payload.get(
        "operation_version",
        PROJECT_SNAPSHOT_OPERATION_VERSION,
    )
    if (
        type(operation_version) is not int
        or operation_version != PROJECT_SNAPSHOT_OPERATION_VERSION
    ):
        raise ValueError("operation_version must be the integer 1")

    collections = (
        ("prds", limits.max_prds, "PRD count exceeds provider limit"),
        ("features", limits.max_features, "feature count exceeds provider limit"),
        ("tasks", limits.max_tasks, "task count exceeds provider limit"),
    )
    for name, limit, message in collections:
        value = _plain_wire_sequence(payload.get(name), field=name)
        if value is not None and len(value) > limit:
            raise ValueError(message)

    tasks = _plain_wire_sequence(payload.get("tasks"), field="tasks")
    if tasks is None:
        return

    def raw_task_shapes() -> Iterator[tuple[bool, int, int, int]]:
        for task in tasks:
            dependencies: Any
            criteria: Any
            summaries: Any
            if isinstance(task, TaskRecordV1):
                parent_present = task.parent_ref is not None
                dependencies = task.dependency_refs
                criteria = task.acceptance_criteria
                summaries = task.verification_summaries
            elif isinstance(task, Mapping):
                parent_present = task.get("parent_ref") is not None
                dependencies = task.get("dependency_refs")
                criteria = task.get("acceptance_criteria")
                summaries = task.get("verification_summaries")
            else:
                continue
            dependency_values = _plain_wire_sequence(
                dependencies,
                field="task dependency_refs",
            )
            criterion_values = _plain_wire_sequence(
                criteria,
                field="task acceptance_criteria",
            )
            summary_values = _plain_wire_sequence(
                summaries,
                field="task verification_summaries",
            )
            dependency_count = (
                len(dependency_values) if dependency_values is not None else 0
            )
            criterion_count = (
                len(criterion_values) if criterion_values is not None else 0
            )
            summary_count = len(summary_values) if summary_values is not None else 0
            if dependency_count > limits.max_dependencies_per_task:
                raise ValueError("task dependency count exceeds provider limit")
            if criterion_count > limits.max_acceptance_criteria_per_task:
                raise ValueError(
                    "task acceptance-criteria count exceeds provider limit"
                )
            if summary_count > limits.max_verification_summaries_per_task:
                raise ValueError("task verification-summary count exceeds provider limit")
            if summary_values is not None:
                for summary in summary_values:
                    if isinstance(summary, VerificationSummaryV1):
                        label: Any = summary.label
                    elif isinstance(summary, Mapping):
                        label = summary.get("label")
                    else:
                        continue
                    _require_utf8_bytes(
                        label,
                        limit=limits.max_verification_summary_label_bytes,
                        path="$.tasks.verification_summaries.label",
                    )
            yield parent_present, dependency_count, criterion_count, summary_count

    prds = _plain_wire_sequence(payload.get("prds"), field="prds")
    features = _plain_wire_sequence(payload.get("features"), field="features")
    _validate_snapshot_aggregate_counts(
        prd_count=len(prds) if prds is not None else 0,
        feature_count=len(features) if features is not None else 0,
        task_shapes=raw_task_shapes(),
        limits=limits,
    )


def _preflight_snapshot_value(
    payload: Any,
    limits: ProviderReadLimitsV1,
) -> None:
    """Apply lowered bounds before Pydantic can build or graph the hierarchy."""
    if isinstance(payload, ProjectSnapshotPayloadV1):
        _preflight_typed_snapshot(payload)
        _validate_snapshot_shape_limits(payload, limits)
        _validate_snapshot_serialized_limit(payload, limits)
        return
    if not isinstance(payload, Mapping):
        return
    _validate_raw_snapshot_limits(payload, limits)
    document = _snapshot_preflight_document(payload)
    for value in _walk_strings(document):
        _require_utf8_bytes(value, limit=limits.max_string_bytes, path="$")
    try:
        canonical_json_bytes(
            document,
            max_depth=limits.max_canonical_json_depth,
            max_nodes=canonical_node_budget_for_bytes(limits.max_snapshot_bytes),
            max_bytes=limits.max_snapshot_bytes,
            max_string_bytes=min(limits.max_string_bytes, limits.max_snapshot_bytes),
        )
    except CanonicalJsonRefusal as exc:
        if exc.code is CanonicalJsonRefusalCode.byte_limit_exceeded:
            raise ValueError("snapshot byte size exceeds provider limit") from exc
        raise


def _snapshot_preflight_document(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Materialize the exact default-complete valid wire shape without validation."""
    document = dict(payload)
    document.setdefault("schema_id", PROJECT_SNAPSHOT_SCHEMA_ID)
    document.setdefault("operation_version", PROJECT_SNAPSHOT_OPERATION_VERSION)
    project = document.get("project")
    if isinstance(project, BaseModel):
        document["project"] = project.model_dump(mode="json")
    elif isinstance(project, Mapping):
        document["project"] = dict(project)

    prds = _plain_wire_sequence(document.get("prds"), field="prds")
    if prds is not None:
        normalized_prds: list[Any] = []
        for raw_prd in prds:
            if isinstance(raw_prd, BaseModel):
                normalized_prds.append(raw_prd.model_dump(mode="json"))
                continue
            if not isinstance(raw_prd, Mapping):
                normalized_prds.append(raw_prd)
                continue
            prd = dict(raw_prd)
            for name in (
                "target_version",
                "target_tag",
                "source_sha256",
                "source_size_bytes",
                "source_encoding",
            ):
                prd.setdefault(name, None)
            normalized_prds.append(prd)
        document["prds"] = normalized_prds

    features = _plain_wire_sequence(document.get("features"), field="features")
    if features is not None:
        document["features"] = [
            feature.model_dump(mode="json")
            if isinstance(feature, BaseModel)
            else dict(feature)
            if isinstance(feature, Mapping)
            else feature
            for feature in features
        ]

    tasks = _plain_wire_sequence(document.get("tasks"), field="tasks")
    if tasks is not None:
        normalized_tasks: list[Any] = []
        for raw_task in tasks:
            if isinstance(raw_task, BaseModel):
                normalized_tasks.append(raw_task.model_dump(mode="json"))
                continue
            if not isinstance(raw_task, Mapping):
                normalized_tasks.append(raw_task)
                continue
            task = dict(raw_task)
            task.setdefault("parent_ref", None)
            task.setdefault("dependency_refs", [])
            task.setdefault("acceptance_criteria", [])
            task.setdefault("verification_summaries", [])
            normalized_tasks.append(task)
        document["tasks"] = normalized_tasks
    return document


def _preflight_response_value(
    response: Mapping[str, Any],
    limits: ProviderReadLimitsV1,
) -> None:
    """Bound the default-complete response before payload hierarchy validation."""
    document = dict(response)
    payload = document.get("payload")
    if isinstance(payload, ProjectSnapshotPayloadV1):
        document["payload"] = payload.model_dump(mode="json")
    elif isinstance(payload, Mapping):
        document["payload"] = _snapshot_preflight_document(payload)
    cursor = document.get("event_cursor")
    if isinstance(cursor, BaseModel):
        document["event_cursor"] = cursor.model_dump(mode="json")
    document["applied_limits"] = limits.model_dump(mode="json")
    try:
        canonical_json_bytes(
            document,
            max_depth=limits.max_canonical_json_depth,
            max_nodes=canonical_node_budget_for_bytes(limits.max_response_bytes),
            max_bytes=limits.max_response_bytes,
            max_string_bytes=min(limits.max_string_bytes, limits.max_response_bytes),
        )
    except CanonicalJsonRefusal as exc:
        if exc.code is CanonicalJsonRefusalCode.byte_limit_exceeded:
            raise ValueError("response byte size exceeds provider limit") from exc
        raise


def _validate_snapshot_aggregate_counts(
    *,
    prd_count: int,
    feature_count: int,
    task_shapes: Iterator[tuple[bool, int, int, int]],
    limits: ProviderReadLimitsV1,
) -> None:
    """Apply a conservative lower bound using only collection lengths."""
    node_limit = canonical_node_budget_for_bytes(limits.max_snapshot_bytes)
    minimum_nodes = 5  # root, project, and the three entity arrays
    minimum_bytes = minimum_nodes * 2
    dependency_edges = 0

    def add(*, nodes: int, bytes_: int) -> None:
        nonlocal minimum_nodes, minimum_bytes
        minimum_nodes += nodes
        minimum_bytes += bytes_
        if minimum_nodes > node_limit:
            raise ValueError("snapshot minimum node count exceeds provider limit")
        if minimum_bytes > limits.max_snapshot_bytes:
            raise ValueError("snapshot minimum byte size exceeds provider limit")

    # Record plus its mandatory nested scoped-reference containers.
    add(nodes=prd_count * 2, bytes_=prd_count * 4)
    add(nodes=feature_count * 3, bytes_=feature_count * 6)
    for (
        parent_present,
        dependency_count,
        criterion_count,
        summary_count,
    ) in task_shapes:
        dependency_edges += dependency_count
        if dependency_edges > limits.max_dependency_edges:
            raise ValueError("aggregate dependency-edge count exceeds provider limit")
        # Task, three mandatory refs, and three list containers.
        add(nodes=7, bytes_=14)
        if parent_present:
            add(nodes=3, bytes_=6)
        if dependency_count:
            add(nodes=dependency_count * 3, bytes_=dependency_count * 6)
        if criterion_count:
            add(nodes=criterion_count, bytes_=criterion_count * 2)
        if summary_count:
            # Every summary contributes its record plus three scalar leaves.
            add(nodes=summary_count * 4, bytes_=summary_count * 8)


def _plain_wire_sequence(
    value: Any,
    *,
    field: str,
) -> list[Any] | tuple[Any, ...] | None:
    """Return a finite built-in wire array without invoking subclass hooks."""
    if type(value) is list or type(value) is tuple:
        return value
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray, memoryview),
    ):
        raise ValueError(f"{field} must use a plain list or tuple")
    return None


def _require_valid_unicode(value: Any, *, path: str) -> None:
    if not isinstance(value, str):
        return
    try:
        str.encode(str.__str__(value), "utf-8")
    except UnicodeEncodeError as exc:
        raise CanonicalJsonRefusal(
            CanonicalJsonRefusalCode.invalid_unicode,
            path=path,
        ) from exc


def _require_utf8_bytes(value: Any, *, limit: int, path: str) -> Any:
    """Apply a byte ceiling without an undocumented character-count ceiling."""
    if not isinstance(value, str):
        return value
    plain_value = str.__str__(value)
    _require_valid_unicode(plain_value, path=path)
    if bytes.__len__(str.encode(plain_value, "utf-8")) > limit:
        raise ValueError("string byte size exceeds provider limit")
    return plain_value


def _preflight_typed_snapshot(payload: ProjectSnapshotPayloadV1) -> None:
    """Reject model-copy corruption before invoking container hooks or dumps."""
    typed_collections = (
        ("prds", payload.prds, PrdRecordV1),
        ("features", payload.features, FeatureRecordV1),
        ("tasks", payload.tasks, TaskRecordV1),
    )
    for field, collection, record_type in typed_collections:
        if type(collection) is not tuple:
            raise ValueError(f"typed snapshot {field} must remain a tuple")
        if any(not isinstance(record, record_type) for record in collection):
            raise ValueError(f"typed snapshot {field} contains an invalid record")
    for task in payload.tasks:
        if type(task.dependency_refs) is not tuple:
            raise ValueError("typed task dependency_refs must remain a tuple")
        if type(task.acceptance_criteria) is not tuple:
            raise ValueError("typed task acceptance_criteria must remain a tuple")
        if type(task.verification_summaries) is not tuple:
            raise ValueError("typed task verification_summaries must remain a tuple")
        if any(
            not isinstance(summary, VerificationSummaryV1)
            for summary in task.verification_summaries
        ):
            raise ValueError("typed task verification_summaries contains an invalid record")


def _validate_snapshot_serialized_limit(
    snapshot: ProjectSnapshotPayloadV1,
    limits: ProviderReadLimitsV1,
) -> None:
    """Apply the final canonical-byte gate after graph invariants are proven."""
    try:
        canonical_json_bytes(
            snapshot.model_dump(mode="json"),
            max_depth=limits.max_canonical_json_depth,
            max_nodes=canonical_node_budget_for_bytes(limits.max_snapshot_bytes),
            max_bytes=limits.max_snapshot_bytes,
            max_string_bytes=min(
                limits.max_string_bytes,
                limits.max_snapshot_bytes,
            ),
        )
    except CanonicalJsonRefusal as exc:
        if exc.code is CanonicalJsonRefusalCode.byte_limit_exceeded:
            raise ValueError("snapshot byte size exceeds provider limit") from exc
        raise


def _validate_response_serialized_limit(
    response: ProjectSnapshotDataV1,
    limits: ProviderReadLimitsV1,
) -> None:
    """Bound the complete operation data, not only its digest payload."""
    try:
        canonical_json_bytes(
            response.model_dump(mode="json"),
            max_depth=limits.max_canonical_json_depth,
            max_nodes=canonical_node_budget_for_bytes(limits.max_response_bytes),
            max_bytes=limits.max_response_bytes,
            max_string_bytes=min(
                limits.max_string_bytes,
                limits.max_response_bytes,
            ),
        )
    except CanonicalJsonRefusal as exc:
        if exc.code is CanonicalJsonRefusalCode.byte_limit_exceeded:
            raise ValueError("response byte size exceeds provider limit") from exc
        raise


def _validate_hierarchy(snapshot: ProjectSnapshotPayloadV1) -> None:
    prds = {record.ref.prd_id: record for record in snapshot.prds}
    if len(prds) != len(snapshot.prds):
        raise ValueError("duplicate PRD scoped reference")

    features = {_feature_key(record.ref): record for record in snapshot.features}
    if len(features) != len(snapshot.features):
        raise ValueError("duplicate feature scoped reference")
    for feature_record in snapshot.features:
        if feature_record.prd_ref.prd_id not in prds:
            raise ValueError("feature owning PRD target is missing")

    tasks = {_task_key(record.ref): record for record in snapshot.tasks}
    if len(tasks) != len(snapshot.tasks):
        raise ValueError("duplicate task scoped reference")
    for task_record in snapshot.tasks:
        key = _task_key(task_record.ref)
        if task_record.prd_ref.prd_id not in prds:
            raise ValueError("task owning PRD target is missing")
        if _feature_key(task_record.feature_ref) not in features:
            raise ValueError("task feature target is missing")
        if task_record.parent_ref is not None:
            parent_key = _task_key(task_record.parent_ref)
            if parent_key == key:
                raise ValueError("task cannot parent itself")
            if parent_key not in tasks:
                raise ValueError("task parent target is missing")
        dependency_keys = [_task_key(ref) for ref in task_record.dependency_refs]
        if len(set(dependency_keys)) != len(dependency_keys):
            raise ValueError("duplicate task dependency edge")
        if key in dependency_keys:
            raise ValueError("task cannot depend on itself")
        if any(target not in tasks for target in dependency_keys):
            raise ValueError("task dependency target is missing")

    _reject_cycles(
        tasks,
        lambda task: tuple(_task_key(ref) for ref in task.dependency_refs),
        label="dependency",
    )
    _reject_cycles(
        tasks,
        lambda task: (() if task.parent_ref is None else (_task_key(task.parent_ref),)),
        label="parent",
    )


def _reject_cycles(
    tasks: dict[TaskKey, TaskRecordV1],
    targets: Callable[[TaskRecordV1], tuple[TaskKey, ...]],
    *,
    label: str,
) -> None:
    """Use Kahn's algorithm so a valid provider-sized graph cannot overflow."""
    incoming = {key: 0 for key in tasks}
    dependants: dict[tuple[str, str], list[tuple[str, str]]] = {
        key: [] for key in tasks
    }
    for source, task in tasks.items():
        for target in targets(task):
            incoming[source] += 1
            dependants[target].append(source)
    ready = [key for key, count in incoming.items() if count == 0]
    visited = 0
    while ready:
        target = ready.pop()
        visited += 1
        for source in dependants[target]:
            incoming[source] -= 1
            if incoming[source] == 0:
                ready.append(source)
    if visited != len(tasks):
        raise ValueError(f"task {label} graph contains a cycle")


def _walk_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield key
            yield from _walk_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_strings(item)


def _feature_key(ref: FeatureScopedRefV1) -> tuple[str, str]:
    return (ref.prd_id, ref.feature_id)


def _task_key(ref: TaskScopedRefV1) -> tuple[str, str]:
    return (ref.prd_id, ref.task_id)


def _require_prd_id(value: str) -> None:
    if not _PRD_ID_PATTERN.fullmatch(value) or value in {".", ".."}:
        raise ValueError("malformed PRD identifier")


def _require_local_id(value: str, *, pattern: re.Pattern[str], kind: str) -> None:
    if not pattern.fullmatch(value):
        raise ValueError(f"malformed {kind} local identifier")


__all__ = [
    "EventCursorV1",
    "FeatureRecordV1",
    "FeatureScopedRefV1",
    "PRD_CONTENT_OPERATION_ID",
    "PRD_CONTENT_OPERATION_VERSION",
    "PRD_CONTENT_SCHEMA_ID",
    "PROJECT_SNAPSHOT_DIGEST_DOMAIN",
    "PROJECT_SNAPSHOT_OPERATION_ID",
    "PROJECT_SNAPSHOT_OPERATION_VERSION",
    "PROJECT_SNAPSHOT_SCHEMA_ID",
    "PROVIDER_READ_CONTRACT_FIXTURE_SHA256",
    "MIN_PROJECT_SNAPSHOT_RESPONSE_BYTES",
    "MAX_PROVIDER_LIMIT_REQUEST_FIELDS",
    "PUBLIC_SCHEMA_CONTRACT_KEY_V1",
    "PROVIDER_LIMITS_V1",
    "PrdRecordV1",
    "PrdScopedRefV1",
    "ProjectRecordV1",
    "ProjectSnapshotDataV1",
    "ProjectSnapshotPayloadV1",
    "ProviderReadLimitsV1",
    "ProviderLimitNameV1",
    "ProviderLimitRefusalV1",
    "ReadErrorCode",
    "ReadErrorV1",
    "TaskRecordV1",
    "TaskScopedRefV1",
    "VerificationKindV1",
    "VerificationSummaryV1",
    "lowered_limits",
    "snapshot_canonical_bytes",
    "snapshot_digest",
    "snapshot_response_canonical_bytes",
    "validate_snapshot_limits",
    "validate_public_wire_document",
    "WIRE_INT64_MAX",
]
