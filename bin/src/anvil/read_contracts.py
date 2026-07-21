"""Closed version-1 contracts for side-effect-free provider reads.

The models in this module are the public wire allowlist.  They intentionally
do not inherit from the mutable state models: adding an internal field must
never add it to a provider response.  Projection code must construct these
DTOs field by field and receives an atomic validation failure when ownership
or dependency invariants are not satisfied.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator

from anvil.state.hashing import (
    MAX_CANONICAL_JSON_RESPONSE_BYTES,
    CanonicalJsonRefusal,
    CanonicalJsonRefusalCode,
    canonical_json_bytes,
    canonical_node_budget_for_bytes,
    domain_separated_sha256,
)

PROJECT_SNAPSHOT_OPERATION_ID = "state.project.snapshot"
PROJECT_SNAPSHOT_OPERATION_VERSION = 1
PROJECT_SNAPSHOT_SCHEMA_ID = "anvil.state.project-snapshot.v1"
PROJECT_SNAPSHOT_DIGEST_DOMAIN = b"anvil.project-snapshot.v1\0"
WIRE_INT64_MAX = (2**63) - 1
MIN_PROJECT_SNAPSHOT_RESPONSE_BYTES = len(
    b'{"applied_limits":{},"event_cursor":{},"payload":{},"snapshot_digest":""}'
)

PRD_CONTENT_OPERATION_ID = "state.prd.content"
PRD_CONTENT_OPERATION_VERSION = 1
PRD_CONTENT_SCHEMA_ID = "anvil.state.prd-content.v1"

_FULL_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_PRD_ID_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$")
_FEATURE_ID_PATTERN = re.compile(r"^F[0-9]{3}(?:\.[0-9]+)*$")
_TASK_ID_PATTERN = re.compile(r"^T[0-9]{3}(?:\.[0-9]+)*$")

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

_WIRE_CONFIG = ConfigDict(extra="forbid", frozen=True, strict=True)


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


class ReadErrorV1(BaseModel):
    """Closed machine-readable refusal body; never carries exception text."""

    model_config = _WIRE_CONFIG

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
            code = raw_code if isinstance(raw_code, ReadErrorCode) else ReadErrorCode(raw_code)
        except (TypeError, ValueError):
            return data
        expected = _SAFE_ERROR_MESSAGES[code]
        supplied = data.get("message")
        if supplied is not None and supplied != expected:
            raise ValueError("error message must match the closed rendering for its code")
        data["message"] = expected
        return data


class ProviderReadLimitsV1(BaseModel):
    """Immutable provider ceilings (or a validated lower requested set)."""

    model_config = _WIRE_CONFIG

    max_prds: int = Field(default=128, ge=1, le=128)
    max_features: int = Field(default=4096, ge=1, le=4096)
    max_tasks: int = Field(default=50_000, ge=1, le=50_000)
    max_dependencies_per_task: int = Field(default=512, ge=0, le=512)
    max_acceptance_criteria_per_task: int = Field(default=256, ge=0, le=256)
    max_string_bytes: int = Field(default=65_536, ge=1, le=65_536)
    max_snapshot_bytes: int = Field(default=16_777_216, ge=1, le=16_777_216)
    max_response_bytes: int = Field(
        default=MAX_CANONICAL_JSON_RESPONSE_BYTES,
        ge=1,
        le=MAX_CANONICAL_JSON_RESPONSE_BYTES,
    )
    max_prd_content_bytes: int = Field(default=2_097_152, ge=1, le=2_097_152)


PROVIDER_LIMITS_V1 = ProviderReadLimitsV1()


class PrdScopedRefV1(BaseModel):
    model_config = _WIRE_CONFIG

    prd_id: str

    @model_validator(mode="after")
    def validate_prd_id(self) -> PrdScopedRefV1:
        _require_prd_id(self.prd_id)
        return self


class FeatureScopedRefV1(BaseModel):
    model_config = _WIRE_CONFIG

    prd_id: str
    feature_id: str

    @model_validator(mode="after")
    def validate_ids(self) -> FeatureScopedRefV1:
        _require_prd_id(self.prd_id)
        _require_local_id(self.feature_id, pattern=_FEATURE_ID_PATTERN, kind="feature")
        return self


class TaskScopedRefV1(BaseModel):
    model_config = _WIRE_CONFIG

    prd_id: str
    task_id: str

    @model_validator(mode="after")
    def validate_ids(self) -> TaskScopedRefV1:
        _require_prd_id(self.prd_id)
        _require_local_id(self.task_id, pattern=_TASK_ID_PATTERN, kind="task")
        return self


class ProjectRecordV1(BaseModel):
    model_config = _WIRE_CONFIG

    project_id: str = Field(min_length=1, max_length=256)
    name: str = Field(min_length=1, max_length=4096)


class PrdRecordV1(BaseModel):
    model_config = _WIRE_CONFIG

    ref: PrdScopedRefV1
    local_id: str
    title: str = Field(min_length=1, max_length=4096)
    revision: int = Field(ge=1, le=WIRE_INT64_MAX)
    status: PrdStatusV1
    target_version: str | None = Field(default=None, max_length=256)
    target_tag: str | None = Field(default=None, max_length=256)
    source_sha256: str | None = Field(default=None, pattern=_FULL_SHA256_PATTERN)
    source_size_bytes: int | None = Field(default=None, ge=0, le=WIRE_INT64_MAX)
    source_encoding: Literal["utf-8"] | None = None
    provenance_state: Literal["available", "legacy_unbound"]
    content_available: bool

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
    local_id: str
    prd_ref: PrdScopedRefV1
    title: str = Field(min_length=1, max_length=4096)
    status: FeatureStatusV1

    @model_validator(mode="after")
    def validate_identity(self) -> FeatureRecordV1:
        _require_local_id(self.local_id, pattern=_FEATURE_ID_PATTERN, kind="feature")
        if self.ref.feature_id != self.local_id:
            raise ValueError("feature local_id must equal ref.feature_id")
        if self.ref.prd_id != self.prd_ref.prd_id:
            raise ValueError("feature ref and owning PRD must have the same PRD scope")
        return self


class VerificationCountsV1(BaseModel):
    model_config = _WIRE_CONFIG

    commands: int = Field(ge=0, le=WIRE_INT64_MAX)
    manual_steps: int = Field(ge=0, le=WIRE_INT64_MAX)
    required_evidence: int = Field(ge=0, le=WIRE_INT64_MAX)
    typed_proofs: int = Field(ge=0, le=WIRE_INT64_MAX)


class TaskRecordV1(BaseModel):
    model_config = _WIRE_CONFIG

    ref: TaskScopedRefV1
    local_id: str
    prd_ref: PrdScopedRefV1
    feature_ref: FeatureScopedRefV1
    parent_ref: TaskScopedRefV1 | None = None
    title: str = Field(min_length=1, max_length=4096)
    status: TaskStatusV1
    priority: TaskPriorityV1
    dependency_refs: tuple[TaskScopedRefV1, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    verification_counts: VerificationCountsV1

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
    def preflight_raw_limits(cls, value: Any, info: ValidationInfo) -> Any:
        """Refuse structurally impossible wire input before nested DTO parsing."""
        if isinstance(value, Mapping):
            _validate_raw_snapshot_limits(value, PROVIDER_LIMITS_V1)
            if info.mode == "json":
                # A before-validator receives decoded JSON as Python lists.
                # Preserve strict DTO semantics while retaining JSON-array input.
                value = dict(value)
                for name in ("prds", "features"):
                    collection = value.get(name)
                    if isinstance(collection, list):
                        value[name] = tuple(collection)
                tasks = value.get("tasks")
                if isinstance(tasks, list):
                    converted_tasks: list[Any] = []
                    for task in tasks:
                        if isinstance(task, Mapping):
                            task = dict(task)
                            for name in (
                                "dependency_refs",
                                "acceptance_criteria",
                            ):
                                collection = task.get(name)
                                if isinstance(collection, list):
                                    task[name] = tuple(collection)
                        converted_tasks.append(task)
                    value["tasks"] = tuple(converted_tasks)
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
    event_frontier_sha256: str = Field(pattern=_FULL_SHA256_PATTERN)


class ProjectSnapshotDataV1(BaseModel):
    """Operation data including cursor and limits outside digest material."""

    model_config = _WIRE_CONFIG

    payload: ProjectSnapshotPayloadV1
    event_cursor: EventCursorV1
    applied_limits: ProviderReadLimitsV1
    snapshot_digest: str = Field(pattern=_FULL_SHA256_PATTERN)

    @model_validator(mode="before")
    @classmethod
    def preflight_response_ceiling(cls, value: Any) -> Any:
        """Reject an impossible envelope ceiling before touching its payload."""
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
            return value
        if (
            isinstance(ceiling, int)
            and not isinstance(ceiling, bool)
            and ceiling < MIN_PROJECT_SNAPSHOT_RESPONSE_BYTES
        ):
            raise ValueError(
                "response byte ceiling cannot fit invariant response fields"
            )
        return value

    @model_validator(mode="after")
    def validate_digest(self) -> ProjectSnapshotDataV1:
        validate_snapshot_limits(self.payload, self.applied_limits)
        if self.snapshot_digest != snapshot_digest(self.payload):
            raise ValueError("snapshot_digest does not match the allowlisted payload")
        _validate_response_serialized_limit(self, self.applied_limits)
        return self


def snapshot_digest(
    payload: ProjectSnapshotPayloadV1 | Mapping[str, Any],
) -> FullSha256:
    """Return the v1 digest of only schema/version and allowlisted hierarchy."""
    if isinstance(payload, Mapping) and not isinstance(
        payload, ProjectSnapshotPayloadV1
    ):
        _validate_raw_snapshot_limits(payload, PROVIDER_LIMITS_V1)
    validated = (
        payload
        if isinstance(payload, ProjectSnapshotPayloadV1)
        else ProjectSnapshotPayloadV1.model_validate_json(
            canonical_json_bytes(
                payload,
                max_nodes=canonical_node_budget_for_bytes(
                    PROVIDER_LIMITS_V1.max_snapshot_bytes
                ),
                max_bytes=PROVIDER_LIMITS_V1.max_snapshot_bytes,
                max_string_bytes=PROVIDER_LIMITS_V1.max_string_bytes,
            )
        )
    )
    digest_document = validated.model_dump(mode="json")
    return domain_separated_sha256(
        PROJECT_SNAPSHOT_DIGEST_DOMAIN,
        digest_document,
        max_nodes=canonical_node_budget_for_bytes(
            PROVIDER_LIMITS_V1.max_snapshot_bytes
        ),
        max_bytes=PROVIDER_LIMITS_V1.max_snapshot_bytes,
        max_string_bytes=PROVIDER_LIMITS_V1.max_string_bytes,
    )


def snapshot_canonical_bytes(payload: ProjectSnapshotPayloadV1) -> bytes:
    """Expose exact digest preimage JSON for cross-runtime qualification vectors."""
    return canonical_json_bytes(
        payload.model_dump(mode="json"),
        max_nodes=canonical_node_budget_for_bytes(
            PROVIDER_LIMITS_V1.max_snapshot_bytes
        ),
        max_bytes=PROVIDER_LIMITS_V1.max_snapshot_bytes,
        max_string_bytes=PROVIDER_LIMITS_V1.max_string_bytes,
    )


def snapshot_response_canonical_bytes(response: ProjectSnapshotDataV1) -> bytes:
    """Return canonical bytes for the complete bounded operation data."""
    limits = response.applied_limits
    return canonical_json_bytes(
        response.model_dump(mode="json"),
        max_nodes=canonical_node_budget_for_bytes(limits.max_response_bytes),
        max_bytes=limits.max_response_bytes,
        max_string_bytes=min(
            limits.max_string_bytes,
            limits.max_response_bytes,
        ),
    )


def lowered_limits(requested: Mapping[str, Any]) -> ProviderReadLimitsV1:
    """Validate caller limits, refusing unknown, raised, or non-integer values."""
    merged = PROVIDER_LIMITS_V1.model_dump()
    unknown = set(requested) - set(merged)
    if unknown:
        raise ValueError(f"unknown provider limit: {sorted(unknown)[0]}")
    for name, value in requested.items():
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"provider limit {name} must be an integer")
        if value > merged[name]:
            raise ValueError(f"provider limit {name} may only be lowered")
        merged[name] = value
    return ProviderReadLimitsV1.model_validate(merged)


def validate_snapshot_limits(
    snapshot: ProjectSnapshotPayloadV1,
    limits: ProviderReadLimitsV1,
) -> None:
    """Refuse a hierarchy exceeding provider or caller-lowered ceilings."""
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

    _validate_snapshot_aggregate_lower_bound(snapshot, limits)
    document = snapshot.model_dump(mode="json")
    for value in _walk_strings(document):
        if len(value) > limits.max_string_bytes:
            raise ValueError("string byte size exceeds provider limit")
        try:
            size = len(value.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise CanonicalJsonRefusal(
                CanonicalJsonRefusalCode.invalid_unicode,
                path="$",
            ) from exc
        if size > limits.max_string_bytes:
            raise ValueError("string byte size exceeds provider limit")


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
    collections = (
        ("prds", limits.max_prds, "PRD count exceeds provider limit"),
        ("features", limits.max_features, "feature count exceeds provider limit"),
        ("tasks", limits.max_tasks, "task count exceeds provider limit"),
    )
    for name, limit, message in collections:
        value = payload.get(name)
        if _is_json_sequence(value) and len(value) > limit:
            raise ValueError(message)

    tasks = payload.get("tasks")
    if not _is_json_sequence(tasks):
        return

    def raw_task_shapes() -> Iterator[tuple[bool, int, int]]:
        for task in tasks:
            if isinstance(task, TaskRecordV1):
                parent_present = task.parent_ref is not None
                dependencies = task.dependency_refs
                criteria = task.acceptance_criteria
            elif isinstance(task, Mapping):
                parent_present = task.get("parent_ref") is not None
                dependencies = task.get("dependency_refs")
                criteria = task.get("acceptance_criteria")
            else:
                continue
            dependency_count = (
                len(dependencies) if _is_json_sequence(dependencies) else 0
            )
            criterion_count = len(criteria) if _is_json_sequence(criteria) else 0
            if dependency_count > limits.max_dependencies_per_task:
                raise ValueError("task dependency count exceeds provider limit")
            if criterion_count > limits.max_acceptance_criteria_per_task:
                raise ValueError(
                    "task acceptance-criteria count exceeds provider limit"
                )
            yield parent_present, dependency_count, criterion_count

    prds = payload.get("prds")
    features = payload.get("features")
    _validate_snapshot_aggregate_counts(
        prd_count=len(prds) if _is_json_sequence(prds) else 0,
        feature_count=len(features) if _is_json_sequence(features) else 0,
        task_shapes=raw_task_shapes(),
        limits=limits,
    )


def _validate_snapshot_aggregate_counts(
    *,
    prd_count: int,
    feature_count: int,
    task_shapes: Iterator[tuple[bool, int, int]],
    limits: ProviderReadLimitsV1,
) -> None:
    """Apply a conservative lower bound using only collection lengths."""
    node_limit = canonical_node_budget_for_bytes(limits.max_snapshot_bytes)
    minimum_nodes = 5  # root, project, and the three entity arrays
    minimum_bytes = minimum_nodes * 2

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
    for parent_present, dependency_count, criterion_count in task_shapes:
        # Task, three mandatory refs, verification counts, and two list containers.
        add(nodes=7, bytes_=14)
        if parent_present:
            add(nodes=3, bytes_=6)
        if dependency_count:
            add(nodes=dependency_count * 3, bytes_=dependency_count * 6)
        if criterion_count:
            add(nodes=criterion_count, bytes_=criterion_count * 2)


def _is_json_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray, memoryview),
    )


def _validate_snapshot_serialized_limit(
    snapshot: ProjectSnapshotPayloadV1,
    limits: ProviderReadLimitsV1,
) -> None:
    """Apply the final canonical-byte gate after graph invariants are proven."""
    try:
        canonical_json_bytes(
            snapshot.model_dump(mode="json"),
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
    for record in snapshot.features:
        if record.prd_ref.prd_id not in prds:
            raise ValueError("feature owning PRD target is missing")

    tasks = {_task_key(record.ref): record for record in snapshot.tasks}
    if len(tasks) != len(snapshot.tasks):
        raise ValueError("duplicate task scoped reference")
    for record in snapshot.tasks:
        key = _task_key(record.ref)
        if record.prd_ref.prd_id not in prds:
            raise ValueError("task owning PRD target is missing")
        if _feature_key(record.feature_ref) not in features:
            raise ValueError("task feature target is missing")
        if record.parent_ref is not None:
            parent_key = _task_key(record.parent_ref)
            if parent_key == key:
                raise ValueError("task cannot parent itself")
            if parent_key not in tasks:
                raise ValueError("task parent target is missing")
        dependency_keys = [_task_key(ref) for ref in record.dependency_refs]
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
    "MIN_PROJECT_SNAPSHOT_RESPONSE_BYTES",
    "PROVIDER_LIMITS_V1",
    "PrdRecordV1",
    "PrdScopedRefV1",
    "ProjectRecordV1",
    "ProjectSnapshotDataV1",
    "ProjectSnapshotPayloadV1",
    "ProviderReadLimitsV1",
    "ReadErrorCode",
    "ReadErrorV1",
    "TaskRecordV1",
    "TaskScopedRefV1",
    "VerificationCountsV1",
    "lowered_limits",
    "snapshot_canonical_bytes",
    "snapshot_digest",
    "snapshot_response_canonical_bytes",
    "validate_snapshot_limits",
    "WIRE_INT64_MAX",
]
