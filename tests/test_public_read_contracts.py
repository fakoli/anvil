"""Qualification vectors for the closed provider-read v1 contracts."""

from __future__ import annotations

import hashlib
import json
from collections import UserDict
from importlib import resources
from types import MappingProxyType

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

import anvil.read_contracts as read_contracts_module
from anvil.read_contracts import (
    MAX_PROVIDER_LIMIT_REQUEST_FIELDS,
    MIN_PROJECT_SNAPSHOT_RESPONSE_BYTES,
    PROVIDER_LIMITS_V1,
    PROVIDER_READ_CONTRACT_FIXTURE_SHA256,
    PUBLIC_SCHEMA_CONTRACT_KEY_V1,
    WIRE_INT64_MAX,
    EventCursorV1,
    FeatureRecordV1,
    FeatureScopedRefV1,
    PrdRecordV1,
    PrdScopedRefV1,
    ProjectRecordV1,
    ProjectSnapshotDataV1,
    ProjectSnapshotPayloadV1,
    ProviderLimitNameV1,
    ProviderLimitRefusalV1,
    ProviderReadLimitsV1,
    ReadErrorCode,
    ReadErrorV1,
    TaskRecordV1,
    TaskScopedRefV1,
    VerificationKindV1,
    VerificationSummaryV1,
    lowered_limits,
    snapshot_canonical_bytes,
    snapshot_digest,
    snapshot_response_canonical_bytes,
    validate_public_wire_document,
    validate_snapshot_limits,
)
from anvil.state.hashing import (
    MAX_CANONICAL_JSON_DEPTH,
    MAX_CANONICAL_JSON_INTEGER,
    MAX_CANONICAL_JSON_NODES,
    MIN_CANONICAL_JSON_INTEGER,
    CanonicalJsonRefusal,
    CanonicalJsonRefusalCode,
    canonical_json_bytes,
    canonical_node_budget_for_bytes,
    domain_separated_sha256,
)

_A_SHA = "a" * 64
_B_SHA = "b" * 64


def _prd(
    prd_id: str,
    *,
    available: bool = True,
    title: str | None = None,
) -> PrdRecordV1:
    return PrdRecordV1(
        ref=PrdScopedRefV1(prd_id=prd_id),
        local_id=prd_id,
        title=title or f"PRD {prd_id}",
        revision=1,
        status="approved",
        target_version="1.0.0",
        target_tag="v1.0.0",
        source_sha256=_A_SHA if available else None,
        source_size_bytes=123 if available else None,
        source_encoding="utf-8" if available else None,
        provenance_state="available" if available else "legacy_unbound",
        content_available=available,
    )


def _feature(prd_id: str) -> FeatureRecordV1:
    return FeatureRecordV1(
        ref=FeatureScopedRefV1(prd_id=prd_id, feature_id="F001"),
        local_id="F001",
        prd_ref=PrdScopedRefV1(prd_id=prd_id),
        title=f"Feature {prd_id}",
        status="ready",
    )


def _task(
    prd_id: str,
    task_id: str = "T001",
    *,
    dependencies: tuple[TaskScopedRefV1, ...] = (),
    parent: TaskScopedRefV1 | None = None,
    title: str | None = None,
    acceptance: tuple[str, ...] = ("It is bounded.",),
) -> TaskRecordV1:
    return TaskRecordV1(
        ref=TaskScopedRefV1(prd_id=prd_id, task_id=task_id),
        local_id=task_id,
        prd_ref=PrdScopedRefV1(prd_id=prd_id),
        feature_ref=FeatureScopedRefV1(prd_id=prd_id, feature_id="F001"),
        parent_ref=parent,
        title=title or f"Task {prd_id}:{task_id}",
        status="ready",
        priority="high",
        dependency_refs=dependencies,
        acceptance_criteria=acceptance,
        verification_summaries=(
            VerificationSummaryV1(
                kind=VerificationKindV1.command,
                label="Automated checks",
                count=2,
            ),
            VerificationSummaryV1(
                kind=VerificationKindV1.required_evidence,
                label="Required evidence",
                count=1,
            ),
            VerificationSummaryV1(
                kind=VerificationKindV1.typed_proof,
                label="Typed proofs",
                count=2,
            ),
        ),
    )


def _snapshot(*, project_name: str = "Anvil") -> ProjectSnapshotPayloadV1:
    return ProjectSnapshotPayloadV1(
        project=ProjectRecordV1(project_id="anvil", name=project_name),
        prds=(_prd("default"), _prd("release-1", available=False)),
        features=(_feature("default"), _feature("release-1")),
        tasks=(
            _task("default"),
            _task(
                "release-1",
                dependencies=(TaskScopedRefV1(prd_id="default", task_id="T001"),),
            ),
        ),
    )


def _wire_validate(data: dict[str, object]) -> ProjectSnapshotPayloadV1:
    return ProjectSnapshotPayloadV1.model_validate_json(
        json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    )


def _count_json_nodes(value: object) -> int:
    if isinstance(value, dict):
        return 1 + sum(_count_json_nodes(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return 1 + sum(_count_json_nodes(item) for item in value)
    return 1


def _response_document(
    payload: ProjectSnapshotPayloadV1,
    cursor: EventCursorV1,
    limits: ProviderReadLimitsV1,
) -> dict[str, object]:
    return {
        "payload": payload.model_dump(mode="json"),
        "event_cursor": cursor.model_dump(mode="json"),
        "applied_limits": limits.model_dump(mode="json"),
        "snapshot_digest": snapshot_digest(payload),
    }


def test_models_forbid_unknown_wire_fields_at_every_level() -> None:
    data = _snapshot().model_dump(mode="json")
    data["unexpected"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        _wire_validate(data)

    data = _snapshot().model_dump(mode="json")
    data["tasks"][0]["raw_command"] = "printenv SECRET"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        _wire_validate(data)


@pytest.mark.parametrize("operation_version", [True, 1.0, "1", 2])
def test_operation_version_requires_the_exact_integer_literal(
    operation_version: object,
) -> None:
    data = _snapshot().model_dump(mode="json")
    data["operation_version"] = operation_version
    with pytest.raises(ValidationError, match="must be the integer 1"):
        ProjectSnapshotPayloadV1.model_validate(data)
    with pytest.raises(ValidationError, match="must be the integer 1"):
        ProjectSnapshotPayloadV1.model_validate_json(
            json.dumps(data, separators=(",", ":"))
        )


@pytest.mark.parametrize(
    ("model", "kwargs"),
    [
        (PrdScopedRefV1, {"prd_id": "../escape"}),
        (PrdScopedRefV1, {"prd_id": "C:drive"}),
        (PrdScopedRefV1, {"prd_id": "bad/name"}),
        (FeatureScopedRefV1, {"prd_id": "default", "feature_id": "T001"}),
        (TaskScopedRefV1, {"prd_id": "default", "task_id": "T1"}),
    ],
)
def test_malformed_scoped_ids_fail_validation(
    model: type[PrdScopedRefV1 | FeatureScopedRefV1 | TaskScopedRefV1],
    kwargs: dict[str, str],
) -> None:
    with pytest.raises(ValidationError):
        model(**kwargs)


def test_default_and_named_local_ids_have_distinct_scoped_references() -> None:
    snapshot = _snapshot()
    refs = {task.ref for task in snapshot.tasks}
    assert len(refs) == 2
    assert {ref.task_id for ref in refs} == {"T001"}
    assert {ref.prd_id for ref in refs} == {"default", "release-1"}


def test_current_and_legacy_provenance_are_explicit_and_consistent() -> None:
    current, legacy = _snapshot().prds
    assert current.model_dump()["source_sha256"] == _A_SHA
    assert current.source_size_bytes == 123
    assert current.source_encoding == "utf-8"
    assert current.content_available is True
    assert legacy.provenance_state == "legacy_unbound"
    assert legacy.content_available is False
    assert legacy.source_sha256 is None
    assert legacy.source_size_bytes is None
    assert legacy.source_encoding is None

    invalid_legacy = _prd("legacy", available=False).model_dump()
    invalid_legacy["source_sha256"] = _A_SHA
    with pytest.raises(ValidationError, match="cannot fabricate"):
        PrdRecordV1.model_validate(invalid_legacy)


def test_all_public_integer_payload_fields_accept_signed_int64_maximum() -> None:
    prd_data = _prd("default").model_dump()
    prd_data.update(
        revision=WIRE_INT64_MAX,
        source_size_bytes=WIRE_INT64_MAX,
    )
    assert PrdRecordV1.model_validate(prd_data).revision == WIRE_INT64_MAX

    summary = VerificationSummaryV1(
        kind=VerificationKindV1.command,
        label="Checks",
        count=WIRE_INT64_MAX,
    )
    assert summary.count == WIRE_INT64_MAX

    cursor = EventCursorV1(
        event_count=WIRE_INT64_MAX,
        event_frontier_sha256=_A_SHA,
    )
    assert cursor.event_count == WIRE_INT64_MAX

    error = ReadErrorV1(
        code=ReadErrorCode.limit_exceeded,
        field="tasks",
        actual=WIRE_INT64_MAX,
        limit=WIRE_INT64_MAX,
    )
    assert error.actual == WIRE_INT64_MAX


def test_public_integer_payload_fields_keep_their_semantic_lower_bounds() -> None:
    assert EventCursorV1(
        event_count=0,
        event_frontier_sha256=_A_SHA,
    ).event_count == 0
    with pytest.raises(ValidationError):
        EventCursorV1(event_count=-1, event_frontier_sha256=_A_SHA)

    prd_data = _prd("default").model_dump()
    assert PrdRecordV1.model_validate({**prd_data, "revision": 1}).revision == 1
    with pytest.raises(ValidationError):
        PrdRecordV1.model_validate({**prd_data, "revision": 0})


@pytest.mark.parametrize(
    ("model", "field", "base"),
    [
        (PrdRecordV1, "revision", _prd("default").model_dump()),
        (PrdRecordV1, "source_size_bytes", _prd("default").model_dump()),
        (
            VerificationSummaryV1,
            "count",
            {
                "kind": VerificationKindV1.command,
                "label": "Checks",
                "count": 1,
            },
        ),
        (
            EventCursorV1,
            "event_count",
            {"event_count": 0, "event_frontier_sha256": _A_SHA},
        ),
        (
            ReadErrorV1,
            "actual",
            {"code": ReadErrorCode.limit_exceeded, "field": "tasks"},
        ),
        (
            ReadErrorV1,
            "limit",
            {"code": ReadErrorCode.limit_exceeded, "field": "tasks"},
        ),
    ],
)
def test_all_public_integer_payload_fields_reject_above_signed_int64(
    model: type[object],
    field: str,
    base: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate({**base, field: WIRE_INT64_MAX + 1})  # type: ignore[attr-defined]


def test_every_leaf_dto_revalidates_model_copy_corruption() -> None:
    valid_error = ReadErrorV1(
        code=ReadErrorCode.limit_exceeded,
        field="tasks",
        actual=1,
        limit=1,
    )
    cases: tuple[tuple[type[object], object], ...] = (
        (
            PrdScopedRefV1,
            PrdScopedRefV1(prd_id="default").model_copy(
                update={"prd_id": "../escape"}
            ),
        ),
        (
            FeatureScopedRefV1,
            FeatureScopedRefV1(prd_id="default", feature_id="F001").model_copy(
                update={"feature_id": "T001"}
            ),
        ),
        (
            TaskScopedRefV1,
            TaskScopedRefV1(prd_id="default", task_id="T001").model_copy(
                update={"task_id": "T1"}
            ),
        ),
        (
            ProjectRecordV1,
            ProjectRecordV1(project_id="anvil", name="Anvil").model_copy(
                update={"name": ""}
            ),
        ),
        (PrdRecordV1, _prd("default").model_copy(update={"revision": 0})),
        (
            FeatureRecordV1,
            _feature("default").model_copy(update={"title": ""}),
        ),
        (
            VerificationSummaryV1,
            VerificationSummaryV1(
                kind=VerificationKindV1.command,
                label="Checks",
                count=1,
            ).model_copy(update={"count": 0}),
        ),
        (TaskRecordV1, _task("default").model_copy(update={"title": ""})),
        (
            EventCursorV1,
            EventCursorV1(
                event_count=1,
                event_frontier_sha256=_A_SHA,
            ).model_copy(update={"event_count": -1}),
        ),
        (
            ProviderReadLimitsV1,
            PROVIDER_LIMITS_V1.model_copy(
                update={"max_tasks": PROVIDER_LIMITS_V1.max_tasks + 1}
            ),
        ),
        (ReadErrorV1, valid_error.model_copy(update={"actual": -1})),
    )
    for model, corrupted in cases:
        with pytest.raises(ValidationError):
            model.model_validate(corrupted)  # type: ignore[attr-defined]
        document = corrupted.model_dump(mode="json")  # type: ignore[attr-defined]
        schema = model.model_json_schema()  # type: ignore[attr-defined]
        assert not Draft202012Validator(schema).is_valid(document)


def test_nested_dtos_revalidate_model_copy_corruption_at_parent_boundaries() -> None:
    payload = _snapshot()
    corrupt_project = payload.project.model_copy(update={"name": ""})
    with pytest.raises(ValidationError, match="project.name"):
        ProjectSnapshotPayloadV1(
            project=corrupt_project,
            prds=payload.prds,
            features=payload.features,
            tasks=payload.tasks,
        )

    task = _task("default")
    corrupt_summary = task.verification_summaries[0].model_copy(update={"count": 0})
    with pytest.raises(ValidationError, match="verification_summaries.0.count"):
        TaskRecordV1(
            **{
                **task.model_dump(),
                "verification_summaries": (corrupt_summary,),
            }
        )


@pytest.mark.parametrize(
    "update",
    [
        {"event_count": -1},
        {"event_frontier_sha256": "z" * 64},
    ],
)
def test_snapshot_response_revalidates_corrupt_cursor(
    update: dict[str, object],
) -> None:
    payload = _snapshot()
    cursor = EventCursorV1(
        event_count=1,
        event_frontier_sha256=_A_SHA,
    ).model_copy(update=update)
    with pytest.raises(ValidationError, match="event_cursor"):
        ProjectSnapshotDataV1(
            payload=payload,
            event_cursor=cursor,
            applied_limits=PROVIDER_LIMITS_V1,
            snapshot_digest=snapshot_digest(payload),
        )
    document = cursor.model_dump(mode="json")
    assert not Draft202012Validator(EventCursorV1.model_json_schema()).is_valid(
        document
    )


@pytest.mark.parametrize("field", list(PROVIDER_LIMITS_V1.model_dump()))
def test_snapshot_response_revalidates_every_raised_provider_limit(field: str) -> None:
    payload = _snapshot()
    corrupt_limits = PROVIDER_LIMITS_V1.model_copy(
        update={field: getattr(PROVIDER_LIMITS_V1, field) + 1}
    )
    with pytest.raises(ValidationError):
        ProviderReadLimitsV1.model_validate(corrupt_limits)
    with pytest.raises(ValidationError):
        ProjectSnapshotDataV1(
            payload=payload,
            event_cursor=EventCursorV1(
                event_count=1,
                event_frontier_sha256=_A_SHA,
            ),
            applied_limits=corrupt_limits,
            snapshot_digest=snapshot_digest(payload),
        )
    document = corrupt_limits.model_dump(mode="json")
    schema = ProviderReadLimitsV1.model_json_schema()
    assert not Draft202012Validator(schema).is_valid(document)


def test_valid_cross_prd_dependency_is_explicit_and_serializable() -> None:
    dependency = _snapshot().tasks[1].dependency_refs[0]
    assert dependency.model_dump(mode="json") == {
        "prd_id": "default",
        "task_id": "T001",
    }


def test_missing_duplicate_self_and_cyclic_dependency_edges_refuse() -> None:
    missing = _snapshot().model_dump(mode="json")
    missing["tasks"][1]["dependency_refs"] = [
        {"prd_id": "default", "task_id": "T999"}
    ]
    with pytest.raises(ValidationError, match="dependency target is missing"):
        _wire_validate(missing)

    duplicate = _snapshot().model_dump(mode="json")
    duplicate["tasks"][1]["dependency_refs"] *= 2
    with pytest.raises(ValidationError, match="duplicate task dependency edge"):
        _wire_validate(duplicate)

    self_loop = _snapshot().model_dump(mode="json")
    self_loop["tasks"][0]["dependency_refs"] = [
        {"prd_id": "default", "task_id": "T001"}
    ]
    with pytest.raises(ValidationError, match="depend on itself"):
        _wire_validate(self_loop)

    cycle = _snapshot().model_dump(mode="json")
    cycle["tasks"][0]["dependency_refs"] = [
        {"prd_id": "release-1", "task_id": "T001"}
    ]
    with pytest.raises(ValidationError, match="dependency graph contains a cycle"):
        _wire_validate(cycle)


def test_cross_prd_or_unresolved_parent_and_feature_links_refuse() -> None:
    cross_parent = _snapshot().model_dump(mode="json")
    cross_parent["tasks"][1]["parent_ref"] = {
        "prd_id": "default",
        "task_id": "T001",
    }
    with pytest.raises(ValidationError, match="parent ownership cannot cross"):
        _wire_validate(cross_parent)

    missing_parent = _snapshot().model_dump(mode="json")
    missing_parent["tasks"][1]["parent_ref"] = {
        "prd_id": "release-1",
        "task_id": "T999",
    }
    with pytest.raises(ValidationError, match="parent target is missing"):
        _wire_validate(missing_parent)

    cross_feature = _snapshot().model_dump(mode="json")
    cross_feature["tasks"][1]["feature_ref"]["prd_id"] = "default"
    with pytest.raises(ValidationError, match="feature ownership cannot cross"):
        _wire_validate(cross_feature)


def test_parent_cycles_refuse() -> None:
    data = _snapshot().model_dump(mode="json")
    data["tasks"].append(
        _task(
            "default",
            "T002",
            parent=TaskScopedRefV1(prd_id="default", task_id="T001"),
        ).model_dump(mode="json")
    )
    data["tasks"][0]["parent_ref"] = {"prd_id": "default", "task_id": "T002"}
    with pytest.raises(ValidationError, match="parent graph contains a cycle"):
        _wire_validate(data)


def test_exact_snapshot_allowlist_contains_summaries_not_operational_content() -> None:
    snapshot = _snapshot()
    assert set(snapshot.model_dump()) == {
        "schema_id",
        "operation_version",
        "project",
        "prds",
        "features",
        "tasks",
    }
    assert set(snapshot.tasks[0].model_dump()) == {
        "ref",
        "local_id",
        "prd_ref",
        "feature_ref",
        "parent_ref",
        "title",
        "status",
        "priority",
        "dependency_refs",
        "acceptance_criteria",
        "verification_summaries",
    }
    serialized = snapshot_canonical_bytes(snapshot)
    for excluded in (
        b"events.jsonl",
        b"state.db",
        b"worktree_path",
        b"claim",
        b"evidence_output",
        b"raw_command",
        b"markdown",
        b"source_bytes",
        b"printenv SECRET",
    ):
        assert excluded not in serialized
    assert snapshot.tasks[0].verification_summaries[0] == VerificationSummaryV1(
        kind=VerificationKindV1.command,
        label="Automated checks",
        count=2,
    )


def test_canonical_json_has_a_portable_exact_utf8_vector() -> None:
    left = {"z": [1, True, None], "a": "café\r\n"}
    right = {"a": "café\r\n", "z": [1, True, None]}
    expected = b'{"a":"caf\xc3\xa9\\r\\n","z":[1,true,null]}'
    assert canonical_json_bytes(left) == expected
    assert canonical_json_bytes(right) == expected
    assert not expected.startswith(b"\xef\xbb\xbf")
    assert not expected.endswith((b"\n", b"\r"))


def test_canonical_json_materializes_mapping_and_sequence_implementations() -> None:
    value = MappingProxyType(
        {
            "z": range(3),
            "a": UserDict(
                {
                    "nested": MappingProxyType({"values": range(2)}),
                }
            ),
        }
    )
    assert canonical_json_bytes(value) == (
        b'{"a":{"nested":{"values":[0,1]}},"z":[0,1,2]}'
    )
    assert canonical_json_bytes(value) == canonical_json_bytes(
        {"a": {"nested": {"values": [0, 1]}}, "z": [0, 1, 2]}
    )


def test_canonical_json_refuses_duplicate_keys_after_base_string_normalization() -> None:
    class IdentityKey(str):
        def __hash__(self) -> int:
            return id(self)

        def __eq__(self, other: object) -> bool:
            return self is other

    clean: dict[str, object] = {"tasks": []}
    hostile = dict(clean)
    hostile[IdentityKey("tasks")] = object()

    with pytest.raises(CanonicalJsonRefusal) as refusal:
        canonical_json_bytes(hostile)
    assert (
        refusal.value.code
        is CanonicalJsonRefusalCode.duplicate_key_after_normalization
    )
    assert refusal.value.path == "$.key[1]"


@pytest.mark.parametrize("value", [1.0, float("nan"), float("inf")])
def test_canonical_json_rejects_every_float(value: float) -> None:
    with pytest.raises(CanonicalJsonRefusal) as raised:
        canonical_json_bytes({"value": value})
    assert raised.value.code is CanonicalJsonRefusalCode.float_forbidden


def test_canonical_json_uses_the_complete_signed_int64_interval() -> None:
    assert canonical_json_bytes(MIN_CANONICAL_JSON_INTEGER) == str(
        MIN_CANONICAL_JSON_INTEGER
    ).encode()
    assert canonical_json_bytes(MAX_CANONICAL_JSON_INTEGER) == str(
        MAX_CANONICAL_JSON_INTEGER
    ).encode()

    for refused in (
        MIN_CANONICAL_JSON_INTEGER - 1,
        MAX_CANONICAL_JSON_INTEGER + 1,
    ):
        with pytest.raises(CanonicalJsonRefusal) as refusal:
            canonical_json_bytes(refused)
        assert refusal.value.code is CanonicalJsonRefusalCode.integer_out_of_range


@pytest.mark.parametrize(
    "value",
    [
        "\ud800",
        ["safe", "\udfff"],
        {"value": "\ud800"},
        {"\ud800": "value"},
        UserDict({"nested": MappingProxyType({"value": "\udfff"})}),
    ],
)
def test_canonical_json_refuses_invalid_unicode_at_every_scalar_shape(
    value: object,
) -> None:
    with pytest.raises(CanonicalJsonRefusal) as refusal:
        canonical_json_bytes(value)
    assert refusal.value.code is CanonicalJsonRefusalCode.invalid_unicode


def test_snapshot_string_limit_reports_invalid_unicode_without_raw_codec_error() -> None:
    invalid_task = _task("default").model_copy(update={"title": "\ud800"})
    with pytest.raises(ValidationError, match="invalid_unicode") as refusal:
        ProjectSnapshotPayloadV1(
            project=ProjectRecordV1(project_id="anvil", name="Anvil"),
            prds=(_prd("default"),),
            features=(_feature("default"),),
            tasks=(invalid_task,),
        )
    assert "UnicodeEncodeError" not in str(refusal.value)


def test_canonical_json_refuses_sequence_and_mapping_cycles_safely() -> None:
    cyclic_list: list[object] = []
    cyclic_list.append(cyclic_list)
    with pytest.raises(CanonicalJsonRefusal) as list_refusal:
        canonical_json_bytes(cyclic_list)
    assert list_refusal.value.code is CanonicalJsonRefusalCode.cyclic_value

    backing: dict[str, object] = {}
    cyclic_proxy = MappingProxyType(backing)
    backing["self"] = cyclic_proxy
    with pytest.raises(CanonicalJsonRefusal) as mapping_refusal:
        canonical_json_bytes(cyclic_proxy)
    assert mapping_refusal.value.code is CanonicalJsonRefusalCode.cyclic_value


def test_canonical_json_refuses_excessive_depth_and_nodes_with_typed_codes() -> None:
    too_deep: object = None
    for _ in range(MAX_CANONICAL_JSON_DEPTH + 1):
        too_deep = [too_deep]
    with pytest.raises(CanonicalJsonRefusal) as depth_refusal:
        canonical_json_bytes(too_deep)
    assert depth_refusal.value.code is CanonicalJsonRefusalCode.depth_exceeded

    with pytest.raises(CanonicalJsonRefusal) as node_refusal:
        canonical_json_bytes(range(MAX_CANONICAL_JSON_NODES + 1))
    assert node_refusal.value.code is CanonicalJsonRefusalCode.node_limit_exceeded

    assert canonical_json_bytes(
        "x" * 50,
        max_bytes=60,
        max_string_bytes=60,
    ) == b'"' + (b"x" * 50) + b'"'
    with pytest.raises(CanonicalJsonRefusal) as byte_refusal:
        canonical_json_bytes(
            '"' * 30,
            max_bytes=60,
            max_string_bytes=60,
        )
    assert byte_refusal.value.code is CanonicalJsonRefusalCode.byte_limit_exceeded


def test_unicode_and_newline_bytes_are_not_normalized() -> None:
    assert canonical_json_bytes({"v": "line\n"}) != canonical_json_bytes(
        {"v": "line\r\n"}
    )
    assert canonical_json_bytes({"v": "é"}) != canonical_json_bytes(
        {"v": "e\u0301"}
    )
    assert snapshot_digest(_snapshot(project_name="line\n")) != snapshot_digest(
        _snapshot(project_name="line\r\n")
    )
    assert snapshot_digest(_snapshot(project_name="é")) != snapshot_digest(
        _snapshot(project_name="e\u0301")
    )


def test_snapshot_digest_has_a_domain_separated_full_sha256_vector() -> None:
    snapshot = _snapshot()
    assert snapshot_digest(snapshot) == (
        "ef29b5224da13784c2c3fded74fc3d5a42a36a1fca083a0d69a68fb391cf95a8"
    )
    assert len(snapshot_digest(snapshot)) == 64
    assert snapshot_digest(snapshot.model_dump(mode="json")) == snapshot_digest(snapshot)


def test_snapshot_digest_revalidates_typed_model_copy_instances() -> None:
    snapshot = _snapshot()
    orphan = snapshot.tasks[0].model_copy(
        update={
            "feature_ref": FeatureScopedRefV1(
                prd_id="default",
                feature_id="F999",
            )
        }
    )
    invalid = snapshot.model_copy(
        update={"tasks": (orphan, snapshot.tasks[1])},
    )
    with pytest.raises(ValidationError, match="feature target is missing"):
        snapshot_digest(invalid)
    with pytest.raises(ValidationError, match="feature target is missing"):
        snapshot_digest(invalid.model_dump(mode="json"))


def test_snapshot_digest_refuses_typed_hostile_arrays_without_consuming() -> None:
    phases: list[str] = []

    class HostileList(list[object]):
        def __len__(self) -> int:
            phases.append("length")
            raise AssertionError("hostile typed length must not run")

        def __iter__(self):  # type: ignore[no-untyped-def]
            phases.append("iterator")
            while True:
                yield object()

    invalid = _snapshot().model_copy(update={"tasks": HostileList()})
    with pytest.raises(ValueError, match="typed snapshot tasks must remain a tuple"):
        snapshot_digest(invalid)
    assert phases == []


def test_snapshot_digest_retains_ordinary_mapping_and_tuple_inputs() -> None:
    snapshot = _snapshot()
    document = snapshot.model_dump(mode="json")
    for field in ("prds", "features", "tasks"):
        document[field] = tuple(document[field])
    wrapped = UserDict(document)
    assert snapshot_digest(wrapped) == snapshot_digest(snapshot)


def test_snapshot_digest_refuses_duplicate_keys_after_base_string_normalization() -> None:
    class IdentityKey(str):
        def __hash__(self) -> int:
            return id(self)

        def __eq__(self, other: object) -> bool:
            return self is other

    snapshot = _snapshot()
    hostile = snapshot.model_dump(mode="json")
    hostile[IdentityKey("tasks")] = []

    with pytest.raises(CanonicalJsonRefusal) as refusal:
        snapshot_digest(hostile)
    assert (
        refusal.value.code
        is CanonicalJsonRefusalCode.duplicate_key_after_normalization
    )
    assert refusal.value.path == "$.key[6]"


def test_cursor_and_lowered_limits_are_excluded_from_snapshot_digest() -> None:
    payload = _snapshot()
    digest = snapshot_digest(payload)
    first = ProjectSnapshotDataV1(
        payload=payload,
        event_cursor=EventCursorV1(event_count=1, event_frontier_sha256=_A_SHA),
        applied_limits=PROVIDER_LIMITS_V1,
        snapshot_digest=digest,
    )
    second = ProjectSnapshotDataV1(
        payload=payload,
        event_cursor=EventCursorV1(event_count=99, event_frontier_sha256=_B_SHA),
        applied_limits=lowered_limits({"max_tasks": 10}),
        snapshot_digest=digest,
    )
    assert first.snapshot_digest == second.snapshot_digest

    with pytest.raises(ValidationError, match="does not match"):
        ProjectSnapshotDataV1(
            payload=payload,
            event_cursor=first.event_cursor,
            applied_limits=first.applied_limits,
            snapshot_digest=_B_SHA,
        )


def test_payload_and_complete_response_have_separate_serialized_ceilings() -> None:
    assert PROVIDER_LIMITS_V1.max_response_bytes > (
        PROVIDER_LIMITS_V1.max_snapshot_bytes
    )
    serialized_limits = PROVIDER_LIMITS_V1.model_dump(mode="json")
    assert serialized_limits["max_snapshot_bytes"] == 16_777_216
    assert serialized_limits["max_response_bytes"] == 16_842_752

    payload = _snapshot()
    response = ProjectSnapshotDataV1(
        payload=payload,
        event_cursor=EventCursorV1(
            event_count=1,
            event_frontier_sha256=_A_SHA,
        ),
        applied_limits=PROVIDER_LIMITS_V1,
        snapshot_digest=snapshot_digest(payload),
    )
    assert len(snapshot_response_canonical_bytes(response)) > len(
        snapshot_canonical_bytes(payload)
    )


def test_complete_response_ceiling_has_exact_lowered_boundary() -> None:
    payload = _snapshot()
    cursor = EventCursorV1(event_count=1, event_frontier_sha256=_A_SHA)
    ceiling = PROVIDER_LIMITS_V1.max_response_bytes

    # The applied ceiling is itself serialized, so converge on the stable
    # exact size for this fixture before exercising N and N-1.
    for _ in range(4):
        candidate_limits = lowered_limits({"max_response_bytes": ceiling})
        document = _response_document(payload, cursor, candidate_limits)
        exact_size = len(
            canonical_json_bytes(
                document,
                max_nodes=canonical_node_budget_for_bytes(
                    PROVIDER_LIMITS_V1.max_response_bytes
                ),
                max_bytes=PROVIDER_LIMITS_V1.max_response_bytes,
                max_string_bytes=PROVIDER_LIMITS_V1.max_string_bytes,
            )
        )
        if exact_size == ceiling:
            break
        ceiling = exact_size
    assert exact_size == ceiling

    exact_limits = lowered_limits({"max_response_bytes": ceiling})
    exact = ProjectSnapshotDataV1(
        payload=payload,
        event_cursor=cursor,
        applied_limits=exact_limits,
        snapshot_digest=snapshot_digest(payload),
    )
    assert len(snapshot_response_canonical_bytes(exact)) == ceiling

    too_small = lowered_limits({"max_response_bytes": ceiling - 1})
    with pytest.raises(ValidationError, match="response byte size exceeds"):
        ProjectSnapshotDataV1(
            payload=payload,
            event_cursor=cursor,
            applied_limits=too_small,
            snapshot_digest=snapshot_digest(payload),
        )


def test_decoded_json_payload_and_response_round_trip_under_strict_models() -> None:
    payload = _snapshot()
    payload_document = payload.model_dump(mode="json")
    assert isinstance(payload_document["prds"], list)
    assert isinstance(payload_document["features"], list)
    assert isinstance(payload_document["tasks"], list)
    assert ProjectSnapshotPayloadV1.model_validate(payload_document) == payload

    response = ProjectSnapshotDataV1(
        payload=payload,
        event_cursor=EventCursorV1(
            event_count=1,
            event_frontier_sha256=_A_SHA,
        ),
        applied_limits=PROVIDER_LIMITS_V1,
        snapshot_digest=snapshot_digest(payload),
    )
    response_document = response.model_dump(mode="json")
    assert isinstance(response_document["payload"]["tasks"], list)
    assert ProjectSnapshotDataV1.model_validate(response_document) == response

    invalid_scalar = payload.model_dump(mode="json")
    invalid_scalar["tasks"][0]["verification_summaries"][0]["count"] = "2"
    with pytest.raises(ValidationError, match="valid integer"):
        ProjectSnapshotPayloadV1.model_validate(invalid_scalar)


def test_decoded_json_leaf_dtos_round_trip_without_scalar_coercion() -> None:
    task = _task(
        "default",
        dependencies=(TaskScopedRefV1(prd_id="default", task_id="T002"),),
        acceptance=("criterion",),
    )
    task_document = task.model_dump(mode="json")
    assert isinstance(task_document["dependency_refs"], list)
    assert isinstance(task_document["acceptance_criteria"], list)
    assert TaskRecordV1.model_validate(task_document) == task
    assert TaskRecordV1.model_validate_json(task.model_dump_json()) == task

    task_document["verification_summaries"][0]["count"] = "2"
    with pytest.raises(ValidationError, match="valid integer"):
        TaskRecordV1.model_validate(task_document)


def test_impossible_response_ceiling_precedes_payload_and_digest_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    phases: list[str] = []

    class ExplodingPayload(UserDict[str, object]):
        def __iter__(self):  # type: ignore[no-untyped-def]
            phases.append("payload")
            raise AssertionError("payload validation must not begin")

        def __getitem__(self, key: str) -> object:
            phases.append("payload")
            raise AssertionError("payload validation must not begin")

    def fail_expensive(*args: object, **kwargs: object) -> object:
        phases.append("expensive")
        raise AssertionError("response validation work must not begin")

    monkeypatch.setattr(read_contracts_module, "validate_snapshot_limits", fail_expensive)
    monkeypatch.setattr(read_contracts_module, "snapshot_digest", fail_expensive)
    monkeypatch.setattr(
        read_contracts_module,
        "_validate_response_serialized_limit",
        fail_expensive,
    )
    below_minimum = PROVIDER_LIMITS_V1.model_dump(mode="json")
    below_minimum["max_response_bytes"] = (
        MIN_PROJECT_SNAPSHOT_RESPONSE_BYTES - 1
    )

    with pytest.raises(
        ValidationError,
        match="response byte ceiling cannot fit invariant response fields",
    ):
        ProjectSnapshotDataV1.model_validate(
            {
                "payload": ExplodingPayload(),
                "event_cursor": {
                    "event_count": 0,
                    "event_frontier_sha256": _A_SHA,
                },
                "applied_limits": below_minimum,
                "snapshot_digest": _A_SHA,
            }
        )
    assert phases == []

    at_minimum = PROVIDER_LIMITS_V1.model_dump(mode="json")
    at_minimum["max_response_bytes"] = MIN_PROJECT_SNAPSHOT_RESPONSE_BYTES
    with pytest.raises(
        ValidationError,
        match="Assertion failed, payload validation must not begin",
    ):
        ProjectSnapshotDataV1.model_validate(
            {
                "payload": ExplodingPayload(),
                "event_cursor": {
                    "event_count": 0,
                    "event_frontier_sha256": _A_SHA,
                },
                "applied_limits": at_minimum,
                "snapshot_digest": _A_SHA,
            }
        )
    assert phases == ["payload"]


@pytest.mark.parametrize(
    ("ceiling", "message"),
    [
        ("593", "response byte ceiling must be an integer"),
        (False, "response byte ceiling must be an integer"),
        (None, "response byte ceiling must be an integer"),
        (593.0, "response byte ceiling must be an integer"),
        (-1, "response byte ceiling is outside provider bounds"),
        (0, "response byte ceiling is outside provider bounds"),
        (
            PROVIDER_LIMITS_V1.max_response_bytes + 1,
            "response byte ceiling is outside provider bounds",
        ),
        (2**80, "response byte ceiling is outside provider bounds"),
    ],
)
def test_malformed_response_ceiling_refuses_before_hostile_payload(
    ceiling: object,
    message: str,
) -> None:
    phases: list[str] = []

    class ExplodingPayload(UserDict[str, object]):
        def __iter__(self):  # type: ignore[no-untyped-def]
            phases.append("payload")
            raise AssertionError("payload validation must not begin")

        def __getitem__(self, key: str) -> object:
            phases.append("payload")
            raise AssertionError("payload validation must not begin")

    raw_limits = PROVIDER_LIMITS_V1.model_dump(mode="json")
    raw_limits["max_response_bytes"] = ceiling
    with pytest.raises(ValidationError, match=message):
        ProjectSnapshotDataV1.model_validate(
            {
                "payload": ExplodingPayload(),
                "event_cursor": {
                    "event_count": 0,
                    "event_frontier_sha256": _A_SHA,
                },
                "applied_limits": raw_limits,
                "snapshot_digest": _A_SHA,
            }
        )
    assert phases == []


@pytest.mark.parametrize("raw_limits", [None, False, "limits", []])
def test_malformed_applied_limits_refuse_before_hostile_payload(
    raw_limits: object,
) -> None:
    phases: list[str] = []

    class ExplodingPayload(UserDict[str, object]):
        def __iter__(self):  # type: ignore[no-untyped-def]
            phases.append("payload")
            raise AssertionError("payload validation must not begin")

        def __getitem__(self, key: str) -> object:
            phases.append("payload")
            raise AssertionError("payload validation must not begin")

    with pytest.raises(ValidationError, match="applied_limits must be a mapping"):
        ProjectSnapshotDataV1.model_validate(
            {
                "payload": ExplodingPayload(),
                "event_cursor": {
                    "event_count": 0,
                    "event_frontier_sha256": _A_SHA,
                },
                "applied_limits": raw_limits,
                "snapshot_digest": _A_SHA,
            }
        )
    assert phases == []


def test_integer_subclass_response_ceiling_refuses_without_comparison() -> None:
    phases: list[str] = []

    class HostileInteger(int):
        def __lt__(self, other: object) -> bool:
            phases.append("compare")
            raise AssertionError("integer subclass comparison must not run")

        def __le__(self, other: object) -> bool:
            phases.append("compare")
            raise AssertionError("integer subclass comparison must not run")

    raw_limits = PROVIDER_LIMITS_V1.model_dump(mode="json")
    raw_limits["max_response_bytes"] = HostileInteger(593)
    with pytest.raises(ValidationError, match="ceiling must be an integer"):
        ProjectSnapshotDataV1.model_validate(
            {
                "payload": {},
                "event_cursor": {},
                "applied_limits": raw_limits,
                "snapshot_digest": _A_SHA,
            }
        )
    assert phases == []


def test_minimum_response_bound_is_a_real_exact_public_response() -> None:
    assert MIN_PROJECT_SNAPSHOT_RESPONSE_BYTES == 768
    payload = ProjectSnapshotPayloadV1(
        project=ProjectRecordV1(project_id="x", name="x"),
        prds=(),
        features=(),
        tasks=(),
    )
    limits = ProviderReadLimitsV1(
        max_prds=1,
        max_features=1,
        max_tasks=1,
        max_dependencies_per_task=0,
        max_acceptance_criteria_per_task=0,
        max_string_bytes=64,
        max_snapshot_bytes=len(snapshot_canonical_bytes(payload)),
        max_response_bytes=MIN_PROJECT_SNAPSHOT_RESPONSE_BYTES,
        max_prd_content_bytes=1,
    )
    response = ProjectSnapshotDataV1(
        payload=payload,
        event_cursor=EventCursorV1(
            event_count=0,
            event_frontier_sha256="0" * 64,
        ),
        applied_limits=limits,
        snapshot_digest=snapshot_digest(payload),
    )
    assert len(snapshot_response_canonical_bytes(response)) == (
        MIN_PROJECT_SNAPSHOT_RESPONSE_BYTES
    )


def test_hash_domains_must_be_ascii_and_nul_terminated() -> None:
    assert len(domain_separated_sha256(b"contract.v1\0", {"a": 1})) == 64
    with pytest.raises(ValueError, match="terminating NUL"):
        domain_separated_sha256(b"contract.v1", {"a": 1})
    with pytest.raises(ValueError, match="non-empty"):
        domain_separated_sha256(b"\0", {"a": 1})
    with pytest.raises(ValueError, match="exactly one terminating NUL"):
        domain_separated_sha256(b"contract\0v1\0", {"a": 1})
    with pytest.raises(ValueError, match="ASCII"):
        domain_separated_sha256("é\0".encode(), {"a": 1})


def test_provider_limits_are_frozen_serializable_and_may_only_be_lowered() -> None:
    assert ProviderReadLimitsV1.model_validate_json(
        PROVIDER_LIMITS_V1.model_dump_json()
    ) == PROVIDER_LIMITS_V1
    with pytest.raises(ValidationError, match="frozen"):
        PROVIDER_LIMITS_V1.max_tasks = 1  # type: ignore[misc]

    requested = lowered_limits({"max_tasks": 10, "max_snapshot_bytes": 4096})
    assert requested.max_tasks == 10
    assert requested.max_snapshot_bytes == 4096
    with pytest.raises(ValueError, match="may only be lowered"):
        lowered_limits({"max_tasks": PROVIDER_LIMITS_V1.max_tasks + 1})
    with pytest.raises(ValueError, match="unknown provider limit"):
        lowered_limits({"max_secrets": 1})
    with pytest.raises(TypeError, match="must be an integer"):
        lowered_limits({"max_tasks": True})


def test_exact_provider_limits_and_packaged_contract_fixture_are_digest_pinned() -> None:
    expected_limits = {
        "max_prds": 128,
        "max_features": 4096,
        "max_tasks": 50_000,
        "max_dependency_edges": 200_000,
        "max_dependencies_per_task": 512,
        "max_acceptance_criteria_per_task": 256,
        "max_verification_summaries_per_task": 256,
        "max_verification_summary_label_bytes": 4096,
        "max_string_bytes": 65_536,
        "max_snapshot_bytes": 16_777_216,
        "max_response_bytes": 16_842_752,
        "max_prd_content_bytes": 2_097_152,
        "max_canonical_json_depth": 128,
        "max_diagnostic_bytes": 4096,
    }
    assert PROVIDER_LIMITS_V1.model_dump(mode="json") == expected_limits
    assert {name.value for name in ProviderLimitNameV1} == set(expected_limits)

    fixture_bytes = (
        resources.files("anvil")
        .joinpath("_data/provider-read-contract-v1.json")
        .read_bytes()
    )
    document = json.loads(fixture_bytes)
    assert (
        hashlib.sha256(canonical_json_bytes(document)).hexdigest()
        == PROVIDER_READ_CONTRACT_FIXTURE_SHA256
    )
    assert document["schema_id"] == "anvil.provider-read-contract.v1"
    assert document["operation_version"] == 1
    assert document["provider_limits"] == expected_limits
    assert document["limit_refusal"]["required_fields"] == [
        "code",
        "operation_id",
        "operation_version",
        "limit_name",
        "actual",
        "limit",
    ]
    assert set(document["limit_refusal"]["limit_names"]) == set(expected_limits)


def test_limit_refusal_metadata_is_exact_closed_and_value_safe() -> None:
    refusal = ProviderLimitRefusalV1(
        operation_id="state.project.snapshot",
        limit_name=ProviderLimitNameV1.max_dependency_edges,
        actual=200_001,
        limit=200_000,
    )
    assert refusal.model_dump(mode="json") == {
        "code": "limit_exceeded",
        "operation_id": "state.project.snapshot",
        "operation_version": 1,
        "limit_name": "max_dependency_edges",
        "actual": 200_001,
        "limit": 200_000,
    }
    assert len(canonical_json_bytes(refusal.model_dump(mode="json"))) < (
        PROVIDER_LIMITS_V1.max_diagnostic_bytes
    )
    assert ProviderLimitRefusalV1.model_validate_json(refusal.model_dump_json()) == refusal
    for forbidden in ("message", "path", "author_text", "schema_id"):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            ProviderLimitRefusalV1.model_validate(
                {**refusal.model_dump(), forbidden: "secret"}
            )
    with pytest.raises(ValidationError):
        ProviderLimitRefusalV1.model_validate(
            {**refusal.model_dump(), "limit_name": "max_secrets"}
        )


def test_verification_summary_count_and_utf8_label_boundaries_are_exact() -> None:
    exact_label = "é" * 2048
    exact = VerificationSummaryV1(
        kind=VerificationKindV1.command,
        label=exact_label,
        count=1,
    )
    task = _task("default").model_copy(
        update={"verification_summaries": (exact,) * 256}
    )
    snapshot = ProjectSnapshotPayloadV1(
        project=ProjectRecordV1(project_id="anvil", name="Anvil"),
        prds=(_prd("default"),),
        features=(_feature("default"),),
        tasks=(task,),
    )
    assert len(snapshot.tasks[0].verification_summaries) == 256

    with pytest.raises(ValidationError, match="verification-summary count"):
        ProjectSnapshotPayloadV1(
            project=snapshot.project,
            prds=snapshot.prds,
            features=snapshot.features,
            tasks=(
                task.model_copy(
                    update={"verification_summaries": (exact,) * 257}
                ),
            ),
        )
    with pytest.raises(ValidationError, match="string byte size"):
        VerificationSummaryV1(
            kind=VerificationKindV1.command,
            label=exact_label + "a",
            count=1,
        )

    phases: list[str] = []

    class ExplodingSummary(UserDict[str, object]):
        def get(self, key: str, default: object = None) -> object:
            phases.append(key)
            raise AssertionError("nested verification summary must not materialize")

    raw = _snapshot().model_dump(mode="json")
    raw["tasks"][0]["verification_summaries"] = [ExplodingSummary()] * 257
    with pytest.raises(ValidationError, match="verification-summary count"):
        ProjectSnapshotPayloadV1.model_validate(raw)
    assert phases == []


def test_other_strings_use_utf8_bytes_without_an_undocumented_character_cap() -> None:
    exact = "é" * 32_768
    assert ProjectRecordV1(project_id="anvil", name=exact).name == exact
    with pytest.raises(ValidationError, match="string byte size"):
        ProjectRecordV1(project_id="anvil", name=exact + "a")


def test_hostile_str_subclasses_cannot_bypass_public_or_canonical_byte_gates() -> None:
    phases: list[str] = []

    class HostileStr(str):
        def encode(self, *args: object, **kwargs: object) -> bytes:
            phases.append("encode")
            return b"x"

        def __len__(self) -> int:
            phases.append("len")
            return 0

        def __str__(self) -> str:
            phases.append("str")
            return "masked"

        def __lt__(self, other: object) -> bool:
            phases.append("lt")
            return False

    oversized_other = HostileStr("é" * 32_769)
    with pytest.raises(ValidationError, match="string byte size"):
        ProjectRecordV1(project_id="anvil", name=oversized_other)
    with pytest.raises(ValidationError, match="string byte size"):
        _task("default", acceptance=(oversized_other,))

    oversized_label = HostileStr("é" * 2049)
    with pytest.raises(ValidationError, match="string byte size"):
        VerificationSummaryV1(
            kind=VerificationKindV1.command,
            label=oversized_label,
            count=1,
        )

    with pytest.raises(CanonicalJsonRefusal) as scalar:
        canonical_json_bytes({"value": oversized_other}, max_string_bytes=16)
    assert scalar.value.code is CanonicalJsonRefusalCode.byte_limit_exceeded
    with pytest.raises(CanonicalJsonRefusal) as key:
        canonical_json_bytes({HostileStr("k" * 17): 1}, max_string_bytes=16)
    assert key.value.code is CanonicalJsonRefusalCode.byte_limit_exceeded
    invalid_unicode = HostileStr("\udfff")
    with pytest.raises(ValidationError, match="invalid_unicode"):
        ProjectRecordV1(project_id="anvil", name=invalid_unicode)
    with pytest.raises(CanonicalJsonRefusal) as invalid:
        canonical_json_bytes({"value": invalid_unicode})
    assert invalid.value.code is CanonicalJsonRefusalCode.invalid_unicode

    admitted = ProjectRecordV1(project_id="anvil", name=HostileStr("safe"))
    assert type(admitted.name) is str
    assert admitted.name == "safe"
    assert phases == []


def test_lowered_limits_preflight_every_expensive_snapshot_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _snapshot()
    phases: list[str] = []

    def fail_hierarchy(*args: object, **kwargs: object) -> None:
        phases.append("hierarchy")
        raise AssertionError("hierarchy graph work must not begin")

    monkeypatch.setattr(read_contracts_module, "_validate_hierarchy", fail_hierarchy)
    for requested, match in (
        ({"max_tasks": 1}, "task count"),
        ({"max_dependency_edges": 0}, "aggregate dependency-edge count"),
        ({"max_string_bytes": 2}, "string byte size"),
        ({"max_snapshot_bytes": 10}, r"snapshot (minimum )?byte size"),
    ):
        with pytest.raises((ValueError, ValidationError), match=match):
            validate_snapshot_limits(snapshot, lowered_limits(requested))
        assert phases == []


def test_response_applied_limits_preflight_payload_and_response_before_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _snapshot()
    cursor = EventCursorV1(event_count=1, event_frontier_sha256=_A_SHA)
    phases: list[str] = []

    def fail_hierarchy(*args: object, **kwargs: object) -> None:
        phases.append("hierarchy")
        raise AssertionError("hierarchy graph work must not begin")

    task_limited = lowered_limits({"max_tasks": 1})
    task_document = _response_document(payload, cursor, task_limited)
    response_limited = lowered_limits(
        {"max_response_bytes": MIN_PROJECT_SNAPSHOT_RESPONSE_BYTES + 1}
    )
    response_document = _response_document(payload, cursor, response_limited)
    monkeypatch.setattr(read_contracts_module, "_validate_hierarchy", fail_hierarchy)

    with pytest.raises(ValidationError, match="task count"):
        ProjectSnapshotDataV1.model_validate(task_document)
    assert phases == []
    with pytest.raises(ValidationError, match="response byte size"):
        ProjectSnapshotDataV1.model_validate(response_document)
    assert phases == []


def test_lowered_depth_and_aggregate_edge_ceilings_refuse_before_graph_work() -> None:
    snapshot = _snapshot()
    validate_snapshot_limits(snapshot, lowered_limits({"max_dependency_edges": 1}))
    with pytest.raises(ValueError, match="aggregate dependency-edge count"):
        validate_snapshot_limits(snapshot, lowered_limits({"max_dependency_edges": 0}))
    with pytest.raises(CanonicalJsonRefusal) as depth:
        canonical_json_bytes({"a": {"b": 1}}, max_depth=1)
    assert depth.value.code is CanonicalJsonRefusalCode.depth_exceeded
    with pytest.raises(CanonicalJsonRefusal) as lowered_depth:
        validate_snapshot_limits(
            snapshot,
            lowered_limits({"max_canonical_json_depth": 1}),
        )
    assert lowered_depth.value.code is CanonicalJsonRefusalCode.depth_exceeded
    with pytest.raises(ValueError, match="depth ceiling"):
        canonical_json_bytes({}, max_depth=129)

    raw = snapshot.model_dump(mode="json")
    raw_task = raw["tasks"][0]
    raw_task["dependency_refs"] = [raw_task["ref"]] * 512
    raw["tasks"] = [raw_task] * 391  # 200,192 edges: over the exact 200,000 cap.
    with pytest.raises(ValidationError, match="aggregate dependency-edge count"):
        ProjectSnapshotPayloadV1.model_validate(raw)


def test_lowered_limits_bounds_untrusted_request_shape_and_error_text() -> None:
    huge_key = "x" * 100_000
    with pytest.raises(ValueError) as huge:
        lowered_limits({huge_key: 1})
    assert str(huge.value) == "unknown provider limit field"
    assert huge_key not in str(huge.value)

    many = {
        f"unknown_{index}": 1
        for index in range(MAX_PROVIDER_LIMIT_REQUEST_FIELDS + 1)
    }
    with pytest.raises(ValueError) as excessive:
        lowered_limits(many)
    assert str(excessive.value) == "provider limit request has too many fields"

    with pytest.raises(ValueError) as mixed:
        lowered_limits({1: 1})  # type: ignore[dict-item]
    assert str(mixed.value) == "unknown provider limit field"


def test_lowered_limits_rejects_hostile_mapping_before_calling_hooks() -> None:
    phases: list[str] = []

    class HostileDict(dict[str, int]):
        def __len__(self) -> int:
            phases.append("length")
            raise AssertionError("hostile length must not run")

        def __iter__(self):  # type: ignore[no-untyped-def]
            phases.append("iterator")
            raise AssertionError("hostile iterator must not run")

        def __getitem__(self, key: str) -> int:
            phases.append("getitem")
            raise AssertionError("hostile lookup must not run")

        def items(self):  # type: ignore[no-untyped-def]
            phases.append("items")
            raise AssertionError("hostile items must not run")

    with pytest.raises(TypeError, match="must be a plain object"):
        lowered_limits(HostileDict(max_tasks=1))
    assert phases == []


def test_lowered_entity_and_string_limits_refuse_atomically() -> None:
    snapshot = _snapshot()
    with pytest.raises(ValueError, match="task count"):
        validate_snapshot_limits(snapshot, lowered_limits({"max_tasks": 1}))
    with pytest.raises(ValueError, match="string byte size"):
        validate_snapshot_limits(snapshot, lowered_limits({"max_string_bytes": 2}))
    with pytest.raises(ValueError, match=r"snapshot (minimum )?byte size"):
        validate_snapshot_limits(snapshot, lowered_limits({"max_snapshot_bytes": 10}))


def test_public_count_gates_precede_hierarchy_graph_work() -> None:
    repeated = tuple(
        TaskScopedRefV1(prd_id="default", task_id="T001")
        for _ in range(PROVIDER_LIMITS_V1.max_dependencies_per_task + 1)
    )
    with pytest.raises(ValidationError, match="dependency count exceeds") as edges:
        ProjectSnapshotPayloadV1(
            project=ProjectRecordV1(project_id="anvil", name="Anvil"),
            prds=(_prd("default"),),
            features=(_feature("default"),),
            tasks=(
                _task("default"),
                _task("default", "T002", dependencies=repeated),
            ),
        )
    assert "duplicate task dependency edge" not in str(edges.value)

    with pytest.raises(ValidationError, match="PRD count exceeds") as entities:
        ProjectSnapshotPayloadV1(
            project=ProjectRecordV1(project_id="anvil", name="Anvil"),
            prds=tuple(
                _prd("default") for _ in range(PROVIDER_LIMITS_V1.max_prds + 1)
            ),
            features=(_feature("default"),),
            tasks=(_task("default"),),
        )
    assert "duplicate PRD scoped reference" not in str(entities.value)

    raw = _snapshot().model_dump(mode="json")
    raw["prds"] = [raw["prds"][0]] * (PROVIDER_LIMITS_V1.max_prds + 1)
    with pytest.raises(ValueError, match="PRD count exceeds") as raw_entities:
        snapshot_digest(raw)
    assert "node_limit_exceeded" not in str(raw_entities.value)


def test_raw_preflight_refuses_list_subclasses_without_consuming_them() -> None:
    phases: list[str] = []

    class FalseLengthList(list[object]):
        def __len__(self) -> int:
            phases.append("false-length")
            return 0

        def __iter__(self):  # type: ignore[no-untyped-def]
            phases.append("false-iterator")
            raise AssertionError("subclass iterator must not run")

    class InfiniteList(list[object]):
        def __len__(self) -> int:
            phases.append("infinite-length")
            return 0

        def __iter__(self):  # type: ignore[no-untyped-def]
            phases.append("infinite-iterator")
            while True:
                yield object()

    raw = _snapshot().model_dump(mode="json")
    raw["prds"] = FalseLengthList(
        [raw["prds"][0]] * (PROVIDER_LIMITS_V1.max_prds + 1)
    )
    with pytest.raises(ValidationError, match="prds must use a plain list or tuple"):
        ProjectSnapshotPayloadV1.model_validate(raw)
    assert phases == []

    raw = _snapshot().model_dump(mode="json")
    raw["tasks"] = InfiniteList()
    with pytest.raises(ValidationError, match="tasks must use a plain list or tuple"):
        ProjectSnapshotPayloadV1.model_validate(raw)
    assert phases == []

    raw = _snapshot().model_dump(mode="json")
    raw["tasks"][0]["dependency_refs"] = FalseLengthList(
        [raw["tasks"][0]["ref"]]
        * (PROVIDER_LIMITS_V1.max_dependencies_per_task + 1)
    )
    with pytest.raises(
        ValidationError,
        match="task dependency_refs must use a plain list or tuple",
    ):
        ProjectSnapshotPayloadV1.model_validate(raw)
    assert phases == []


def test_aggregate_lower_bound_precedes_dump_sets_and_cycle_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    phases: list[str] = []

    class ExplodingDependency(UserDict[str, object]):
        def __iter__(self):  # type: ignore[no-untyped-def]
            phases.append("nested-dto")
            raise AssertionError("nested dependency validation must not begin")

        def __getitem__(self, key: str) -> object:
            phases.append("nested-dto")
            raise AssertionError("nested dependency validation must not begin")

    raw = _snapshot().model_dump(mode="json")
    raw_task = raw["tasks"][0]
    raw_task["dependency_refs"] = [ExplodingDependency()] * (
        PROVIDER_LIMITS_V1.max_dependencies_per_task
    )
    raw_task["acceptance_criteria"] = []
    raw["tasks"] = [raw_task] * PROVIDER_LIMITS_V1.max_tasks

    def fail_dump(*args: object, **kwargs: object) -> object:
        phases.append("model_dump")
        raise AssertionError("model_dump must not run for an impossible aggregate")

    def fail_hierarchy(*args: object, **kwargs: object) -> None:
        phases.append("hierarchy")
        raise AssertionError("hierarchy sets must not be built")

    def fail_canonical(*args: object, **kwargs: object) -> bytes:
        phases.append("canonical")
        raise AssertionError("canonical materialization must not begin")

    monkeypatch.setattr(ProjectSnapshotPayloadV1, "model_dump", fail_dump)
    monkeypatch.setattr(read_contracts_module, "_validate_hierarchy", fail_hierarchy)
    monkeypatch.setattr(read_contracts_module, "canonical_json_bytes", fail_canonical)

    with pytest.raises(ValidationError, match="aggregate dependency-edge count"):
        ProjectSnapshotPayloadV1.model_validate(raw)
    assert phases == []


def test_public_string_gate_precedes_hierarchy_graph_work() -> None:
    oversized = "x" * (PROVIDER_LIMITS_V1.max_string_bytes + 1)
    invalid_feature_task = _task("default", "T002").model_copy(
        update={
            "acceptance_criteria": (oversized,),
            "feature_ref": FeatureScopedRefV1(
                prd_id="default",
                feature_id="F999",
            )
        }
    )
    with pytest.raises(ValidationError, match="string byte size exceeds") as refusal:
        ProjectSnapshotPayloadV1(
            project=ProjectRecordV1(project_id="anvil", name="Anvil"),
            prds=(_prd("default"),),
            features=(_feature("default"),),
            tasks=(_task("default"), invalid_feature_task),
        )
    assert "feature target is missing" not in str(refusal.value)


def test_provider_node_budget_cannot_reject_a_byte_admitted_json_value() -> None:
    byte_ceiling = PROVIDER_LIMITS_V1.max_snapshot_bytes
    assert canonical_node_budget_for_bytes(byte_ceiling) == byte_ceiling

    corpus: tuple[object, ...] = (
        None,
        0,
        "",
        [],
        {},
        [0, 0, 0],
        {"a": [0, {"b": ""}]},
    )
    for value in corpus:
        assert _count_json_nodes(value) <= len(canonical_json_bytes(value))


def test_valid_five_thousand_task_snapshot_exceeds_generic_node_default() -> None:
    tasks = tuple(
        _task("default", f"T001.{index}", title="t", acceptance=())
        for index in range(1, 5001)
    )
    snapshot = ProjectSnapshotPayloadV1(
        project=ProjectRecordV1(project_id="anvil", name="Anvil"),
        prds=(_prd("default"),),
        features=(_feature("default"),),
        tasks=tasks,
    )
    encoded = snapshot_canonical_bytes(snapshot)
    assert 1_500_000 < len(encoded) < 3_000_000
    assert _count_json_nodes(snapshot.model_dump(mode="json")) > (
        MAX_CANONICAL_JSON_NODES
    )
    assert len(tasks) == 5000


@pytest.mark.parametrize(
    "limits",
    [
        {"max_tasks": 1},
        {"max_string_bytes": 2},
        {"max_snapshot_bytes": 10},
    ],
)
def test_snapshot_data_refuses_payload_that_exceeds_applied_limits(
    limits: dict[str, int],
) -> None:
    payload = _snapshot()
    with pytest.raises(ValidationError, match="exceeds provider limit"):
        ProjectSnapshotDataV1(
            payload=payload,
            event_cursor=EventCursorV1(
                event_count=1,
                event_frontier_sha256=_A_SHA,
            ),
            applied_limits=lowered_limits(limits),
            snapshot_digest=snapshot_digest(payload),
        )


def test_read_error_codes_and_body_are_closed_and_serializable() -> None:
    error = ReadErrorV1(
        code=ReadErrorCode.limit_exceeded,
        field="tasks",
        actual=11,
        limit=10,
    )
    assert error.model_dump(mode="json") == {
        "schema_id": "anvil.state.read-error.v1",
        "code": "limit_exceeded",
        "field": "tasks",
        "actual": 11,
        "limit": 10,
        "message": "A provider read limit was exceeded.",
    }
    assert ReadErrorV1.model_validate(error.model_dump()) == error
    assert ReadErrorV1.model_validate(error.model_dump(mode="json")) == error
    assert ReadErrorV1.model_validate_json(error.model_dump_json()) == error
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ReadErrorV1.model_validate({**error.model_dump(), "path": "C:/secret/state.db"})


def test_every_public_wire_model_declares_and_satisfies_the_schema_contract() -> None:
    payload = _snapshot()
    response = ProjectSnapshotDataV1(
        payload=payload,
        event_cursor=EventCursorV1(
            event_count=1,
            event_frontier_sha256=_A_SHA,
        ),
        applied_limits=PROVIDER_LIMITS_V1,
        snapshot_digest=snapshot_digest(payload),
    )
    instances = (
        PrdScopedRefV1(prd_id="default"),
        FeatureScopedRefV1(prd_id="default", feature_id="F001"),
        TaskScopedRefV1(prd_id="default", task_id="T001"),
        ProjectRecordV1(project_id="anvil", name="Anvil"),
        _prd("default"),
        _feature("default"),
        VerificationSummaryV1(
            kind=VerificationKindV1.command,
            label="Checks",
            count=1,
        ),
        _task("default"),
        payload,
        response.event_cursor,
        response,
        PROVIDER_LIMITS_V1,
        ProviderLimitRefusalV1(
            operation_id="state.project.snapshot",
            limit_name=ProviderLimitNameV1.max_tasks,
            actual=11,
            limit=10,
        ),
        ReadErrorV1(code=ReadErrorCode.state_unavailable, field="state"),
    )
    for instance in instances:
        model = type(instance)
        schema = model.model_json_schema()
        contract = schema[PUBLIC_SCHEMA_CONTRACT_KEY_V1]
        assert contract == {
            "standard_schema_role": "structural-prefilter",
            "authoritative_validator": (
                "anvil.read_contracts.validate_public_wire_document"
            ),
            "runtime_required_for": [
                "strict decoded scalar types and JSON lexical distinctions",
                "cross-field identity and provenance",
                "cross-record ownership and hierarchy",
                "snapshot digest equality",
            ],
        }
        document = instance.model_dump(mode="json")
        assert Draft202012Validator(schema).is_valid(document)
        assert validate_public_wire_document(model, document) == instance


def test_schema_prefilter_overaccepts_documents_rejected_by_runtime_contract() -> None:
    cursor_float = EventCursorV1(
        event_count=1,
        event_frontier_sha256=_A_SHA,
    ).model_dump(mode="json")
    cursor_float["event_count"] = 1.0

    payload_float = _snapshot().model_dump(mode="json")
    payload_float["operation_version"] = 1.0

    mismatched_prd = _prd("default").model_dump(mode="json")
    mismatched_prd["local_id"] = "release-1"

    false_available_provenance = _prd("default").model_dump(mode="json")
    false_available_provenance["content_available"] = False

    cross_owned_task = _task("default").model_dump(mode="json")
    cross_owned_task["feature_ref"] = {
        "prd_id": "release-1",
        "feature_id": "F001",
    }

    missing_dependency = _snapshot().model_dump(mode="json")
    missing_dependency["tasks"][0]["dependency_refs"] = [
        {"prd_id": "default", "task_id": "T999"}
    ]

    payload = _snapshot()
    wrong_digest = ProjectSnapshotDataV1(
        payload=payload,
        event_cursor=EventCursorV1(
            event_count=1,
            event_frontier_sha256=_A_SHA,
        ),
        applied_limits=PROVIDER_LIMITS_V1,
        snapshot_digest=snapshot_digest(payload),
    ).model_dump(mode="json")
    wrong_digest["snapshot_digest"] = _B_SHA

    cases = (
        (EventCursorV1, cursor_float),
        (ProjectSnapshotPayloadV1, payload_float),
        (PrdRecordV1, mismatched_prd),
        (PrdRecordV1, false_available_provenance),
        (TaskRecordV1, cross_owned_task),
        (ProjectSnapshotPayloadV1, missing_dependency),
        (ProjectSnapshotDataV1, wrong_digest),
    )
    for model, document in cases:
        assert Draft202012Validator(model.model_json_schema()).is_valid(document)
        with pytest.raises(ValidationError):
            validate_public_wire_document(model, document)


@pytest.mark.parametrize("invalid_digest", [_A_SHA + "\n", _A_SHA + "\r", "A" * 64])
def test_full_sha256_schema_and_runtime_exclude_non_exact_values(
    invalid_digest: str,
) -> None:
    payload = _snapshot()
    response = ProjectSnapshotDataV1(
        payload=payload,
        event_cursor=EventCursorV1(
            event_count=1,
            event_frontier_sha256=_A_SHA,
        ),
        applied_limits=PROVIDER_LIMITS_V1,
        snapshot_digest=snapshot_digest(payload),
    )
    cases = (
        (PrdRecordV1, _prd("default").model_dump(mode="json"), "source_sha256"),
        (
            EventCursorV1,
            response.event_cursor.model_dump(mode="json"),
            "event_frontier_sha256",
        ),
        (
            ProjectSnapshotDataV1,
            response.model_dump(mode="json"),
            "snapshot_digest",
        ),
    )
    for model, valid_document, field in cases:
        document = {**valid_document, field: invalid_digest}
        assert not Draft202012Validator(model.model_json_schema()).is_valid(document)
        with pytest.raises(ValidationError, match="full lowercase SHA-256 digest"):
            validate_public_wire_document(model, document)


@pytest.mark.parametrize(
    ("model", "document", "expected"),
    [
        (PrdScopedRefV1, {"prd_id": "release-1"}, True),
        (PrdScopedRefV1, {"prd_id": "../escape"}, False),
        (PrdScopedRefV1, {"prd_id": "release-1\n"}, False),
        (PrdScopedRefV1, {"prd_id": "release-1\r"}, False),
        (
            FeatureScopedRefV1,
            {"prd_id": "default", "feature_id": "F001.2"},
            True,
        ),
        (
            FeatureScopedRefV1,
            {"prd_id": "default", "feature_id": "T001"},
            False,
        ),
        (
            FeatureScopedRefV1,
            {"prd_id": "default", "feature_id": "F001\n"},
            False,
        ),
        (
            TaskScopedRefV1,
            {"prd_id": "default", "task_id": "T001.2"},
            True,
        ),
        (
            TaskScopedRefV1,
            {"prd_id": "default", "task_id": "T1"},
            False,
        ),
        (
            TaskScopedRefV1,
            {"prd_id": "default", "task_id": "T001\r"},
            False,
        ),
    ],
)
def test_scoped_id_json_schema_matches_runtime(
    model: type[PrdScopedRefV1 | FeatureScopedRefV1 | TaskScopedRefV1],
    document: dict[str, object],
    expected: bool,
) -> None:
    schema_accepts = Draft202012Validator(model.model_json_schema()).is_valid(document)
    try:
        model.model_validate(document)
    except ValidationError:
        runtime_accepts = False
    else:
        runtime_accepts = True
    assert schema_accepts is expected
    assert runtime_accepts is expected


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        ({"code": "state_unavailable", "field": "state"}, True),
        (
            {
                "code": "state_unavailable",
                "field": "state",
                "message": "Project state is unavailable.",
            },
            True,
        ),
        (
            {
                "code": "state_unavailable",
                "field": "state",
                "message": "A provider read limit was exceeded.",
            },
            False,
        ),
        ({"code": "state_unavailable", "field": "state", "message": None}, False),
        ({"code": "unknown", "field": "state"}, False),
    ],
)
def test_read_error_json_schema_matches_runtime(
    document: dict[str, object],
    expected: bool,
) -> None:
    schema_accepts = Draft202012Validator(
        ReadErrorV1.model_json_schema()
    ).is_valid(document)
    try:
        ReadErrorV1.model_validate(document)
    except ValidationError:
        runtime_accepts = False
    else:
        runtime_accepts = True
    assert schema_accepts is expected
    assert runtime_accepts is expected


@pytest.mark.parametrize(
    "untrusted",
    [
        {"message": "C:/secret/state.db"},
        {"exception": "sqlite failed at C:/secret/state.db"},
        {"details": "TOKEN=do-not-leak"},
    ],
)
def test_read_error_rejects_untrusted_rendering_and_exception_text(
    untrusted: dict[str, str],
) -> None:
    with pytest.raises(ValidationError):
        ReadErrorV1.model_validate(
            {
                "code": ReadErrorCode.state_unavailable,
                "field": "state",
                **untrusted,
            }
        )


def test_read_error_field_is_closed_and_rendering_contains_no_input_text() -> None:
    sentinel = "C:/secret/state.db?token=do-not-leak"
    with pytest.raises(ValidationError):
        ReadErrorV1(code=ReadErrorCode.state_unavailable, field=sentinel)

    rendered = ReadErrorV1(
        code=ReadErrorCode.state_unavailable,
        field="state",
    ).model_dump_json()
    assert sentinel not in rendered
    assert "secret" not in rendered
    assert "state.db" not in rendered
