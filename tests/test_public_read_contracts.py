"""Qualification vectors for the closed provider-read v1 contracts."""

from __future__ import annotations

import json
from collections import UserDict
from types import MappingProxyType

import pytest
from pydantic import ValidationError

from anvil.read_contracts import (
    PROVIDER_LIMITS_V1,
    EventCursorV1,
    FeatureRecordV1,
    FeatureScopedRefV1,
    PrdRecordV1,
    PrdScopedRefV1,
    ProjectRecordV1,
    ProjectSnapshotDataV1,
    ProjectSnapshotPayloadV1,
    ProviderReadLimitsV1,
    ReadErrorCode,
    ReadErrorV1,
    TaskRecordV1,
    TaskScopedRefV1,
    VerificationCountsV1,
    lowered_limits,
    snapshot_canonical_bytes,
    snapshot_digest,
    validate_snapshot_limits,
)
from anvil.state.hashing import (
    MAX_CANONICAL_JSON_DEPTH,
    MAX_CANONICAL_JSON_NODES,
    MAX_CANONICAL_JSON_STRING_CHARS,
    CanonicalJsonRefusal,
    CanonicalJsonRefusalCode,
    canonical_json_bytes,
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
        acceptance_criteria=("It is bounded.",),
        verification_counts=VerificationCountsV1(
            commands=2,
            manual_steps=0,
            required_evidence=1,
            typed_proofs=2,
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


def test_models_forbid_unknown_wire_fields_at_every_level() -> None:
    data = _snapshot().model_dump(mode="json")
    data["unexpected"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        _wire_validate(data)

    data = _snapshot().model_dump(mode="json")
    data["tasks"][0]["raw_command"] = "printenv SECRET"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        _wire_validate(data)


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


def test_exact_snapshot_allowlist_contains_counts_not_operational_content() -> None:
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
        "verification_counts",
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
    assert snapshot.tasks[0].verification_counts.commands == 2


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


@pytest.mark.parametrize("value", [1.0, float("nan"), float("inf")])
def test_canonical_json_rejects_every_float(value: float) -> None:
    with pytest.raises(CanonicalJsonRefusal) as raised:
        canonical_json_bytes({"value": value})
    assert raised.value.code is CanonicalJsonRefusalCode.float_forbidden


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

    with pytest.raises(CanonicalJsonRefusal) as byte_refusal:
        canonical_json_bytes("x" * (MAX_CANONICAL_JSON_STRING_CHARS + 1))
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
        "edf47a596cceceb757b524fb073cb267c24b55ed2defcdbe6284a3d959c42c0a"
    )
    assert len(snapshot_digest(snapshot)) == 64
    assert snapshot_digest(snapshot.model_dump(mode="json")) == snapshot_digest(snapshot)


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


def test_lowered_entity_and_string_limits_refuse_atomically() -> None:
    snapshot = _snapshot()
    with pytest.raises(ValueError, match="task count"):
        validate_snapshot_limits(snapshot, lowered_limits({"max_tasks": 1}))
    with pytest.raises(ValueError, match="string byte size"):
        validate_snapshot_limits(snapshot, lowered_limits({"max_string_bytes": 2}))
    with pytest.raises(ValueError, match="snapshot byte size"):
        validate_snapshot_limits(snapshot, lowered_limits({"max_snapshot_bytes": 10}))


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
    assert ReadErrorV1.model_validate_json(error.model_dump_json()) == error
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ReadErrorV1.model_validate({**error.model_dump(), "path": "C:/secret/state.db"})


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
