"""Atomic, side-effect-free projection for the provider snapshot contract."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, BinaryIO, NoReturn

from pydantic import ValidationError

from anvil.read_contracts import (
    PROJECT_SNAPSHOT_OPERATION_ID,
    PROVIDER_LIMITS_V1,
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
    snapshot_digest,
)
from anvil.state.backend import SchemaMismatch, SchemaProbeFailed
from anvil.state.hashing import CanonicalJsonRefusal, canonical_json_bytes
from anvil.state.models import Event, Verification
from anvil.state.sqlite import query_only_transaction

EVENT_FRONTIER_DOMAIN_V1 = b"anvil.project-event-frontier.v1\0"
_MAX_EVENT_RECORD_BYTES = PROVIDER_LIMITS_V1.max_snapshot_bytes


class ProjectSnapshotError(RuntimeError):
    """A closed, value-safe refusal from :func:`read_project_snapshot`."""

    def __init__(self, error: ReadErrorV1 | ProviderLimitRefusalV1) -> None:
        self.error = error
        if isinstance(error, ReadErrorV1):
            message = error.message
        else:
            message = "A provider read limit was exceeded."
        super().__init__(message)


def read_project_snapshot(
    state_dir: str | os.PathLike[str],
    *,
    limits: ProviderReadLimitsV1 | Mapping[str, Any] | None = None,
) -> ProjectSnapshotDataV1:
    """Return one converged allowlisted hierarchy or refuse without mutation."""
    applied_limits = _validated_limits(limits)
    root = Path(state_dir)
    try:
        with query_only_transaction(
            root / "state.db",
            root / "events.jsonl",
        ) as (conn, events_fh):
            event_cursor, event_identity = _event_cursor(conn, events_fh)
            payload = _project_payload(conn, applied_limits)
            payload_digest = snapshot_digest(payload)
            _enforce_serialized_limits(
                payload,
                event_cursor,
                payload_digest,
                applied_limits,
            )
            response = ProjectSnapshotDataV1(
                payload=payload,
                event_cursor=event_cursor,
                applied_limits=applied_limits,
                snapshot_digest=payload_digest,
            )
            _verify_event_identity(events_fh, event_identity)
            return response
    except ProjectSnapshotError:
        raise
    except FileNotFoundError:
        _refuse(ReadErrorCode.state_unavailable, field="state")
    except SchemaMismatch:
        _refuse(ReadErrorCode.schema_incompatible, field="schema")
    except SchemaProbeFailed:
        _refuse(ReadErrorCode.projection_not_converged, field="projection")
    except (sqlite3.Error, OSError):
        _refuse(ReadErrorCode.state_unavailable, field="state")
    except (CanonicalJsonRefusal, ValidationError, TypeError, ValueError):
        _refuse(ReadErrorCode.invalid_hierarchy, field="state")


def _validated_limits(
    requested: ProviderReadLimitsV1 | Mapping[str, Any] | None,
) -> ProviderReadLimitsV1:
    try:
        if requested is None:
            return PROVIDER_LIMITS_V1
        if isinstance(requested, ProviderReadLimitsV1):
            return ProviderReadLimitsV1.model_validate(requested)
        return lowered_limits(requested)
    except (TypeError, ValueError, ValidationError):
        _refuse(ReadErrorCode.invalid_request, field="request")


def _project_payload(
    conn: sqlite3.Connection,
    limits: ProviderReadLimitsV1,
) -> ProjectSnapshotPayloadV1:
    projects_count = _table_count(conn, "projects")
    if projects_count == 0:
        _refuse(ReadErrorCode.state_unavailable, field="state")
    if projects_count != 1:
        _refuse(ReadErrorCode.invalid_hierarchy, field="state")
    _gate_count(conn, "prds", limits.max_prds, ProviderLimitNameV1.max_prds)
    _gate_count(
        conn,
        "features",
        limits.max_features,
        ProviderLimitNameV1.max_features,
    )
    _gate_count(conn, "tasks", limits.max_tasks, ProviderLimitNameV1.max_tasks)

    project_row = conn.execute("SELECT id, name FROM projects").fetchone()
    if project_row is None:
        _refuse(ReadErrorCode.state_unavailable, field="state")
    project_id = _required_string(project_row["id"])
    project = ProjectRecordV1(
        project_id=project_id,
        name=_required_string(project_row["name"]),
    )

    prds: list[PrdRecordV1] = []
    prd_ids: set[str] = set()
    default_count = 0
    for row in conn.execute(
        "SELECT id, project_id, title, revision, status, target_version, "
        "target_tag, is_default, source_bytes, source_sha256, "
        "source_size_bytes, source_encoding, source_revision, "
        "provenance_state, content_available FROM prds ORDER BY id"
    ):
        prd_id = _required_string(row["id"])
        if _required_string(row["project_id"]) != project_id:
            _refuse(ReadErrorCode.invalid_hierarchy, field="prds")
        is_default = _strict_db_bool(row["is_default"])
        default_count += int(is_default)
        if is_default != (prd_id == "default"):
            _refuse(ReadErrorCode.invalid_hierarchy, field="prds")
        if prd_id in prd_ids:
            _refuse(ReadErrorCode.invalid_hierarchy, field="prds")
        prd_ids.add(prd_id)
        source = _validated_source_binding(row)
        prds.append(
            PrdRecordV1(
                ref=PrdScopedRefV1(prd_id=prd_id),
                local_id=prd_id,
                title=_required_string(row["title"], allow_empty=True),
                revision=_required_int(row["revision"], minimum=1),
                status=_required_string(row["status"]),
                target_version=_optional_string(row["target_version"]),
                target_tag=_optional_string(row["target_tag"]),
                source_sha256=source[0],
                source_size_bytes=source[1],
                source_encoding=source[2],
                provenance_state=_required_string(row["provenance_state"]),
                content_available=_strict_db_bool(row["content_available"]),
            )
        )
    if prds and default_count > 1:
        _refuse(ReadErrorCode.invalid_hierarchy, field="prds")

    feature_rows = conn.execute(
        "SELECT id, prd_id, title, status FROM features ORDER BY id"
    ).fetchall()
    feature_refs: dict[str, FeatureScopedRefV1] = {}
    features: list[FeatureRecordV1] = []
    for row in feature_rows:
        stored_id = _required_string(row["id"])
        prd_id = _required_string(row["prd_id"])
        if prd_id not in prd_ids:
            _refuse(ReadErrorCode.missing_target, field="features")
        local_id = _local_entity_id(stored_id, prd_id)
        ref = FeatureScopedRefV1(prd_id=prd_id, feature_id=local_id)
        if stored_id in feature_refs:
            _refuse(ReadErrorCode.invalid_hierarchy, field="features")
        feature_refs[stored_id] = ref
        features.append(
            FeatureRecordV1(
                ref=ref,
                local_id=local_id,
                prd_ref=PrdScopedRefV1(prd_id=prd_id),
                title=_required_string(row["title"]),
                status=_required_string(row["status"]),
            )
        )

    task_rows = conn.execute(
        "SELECT id, feature_id, prd_id, title, status, priority, "
        "dependencies, acceptance_criteria, verification, parent_task_id "
        "FROM tasks ORDER BY id"
    ).fetchall()
    task_refs: dict[str, TaskScopedRefV1] = {}
    for row in task_rows:
        stored_id = _required_string(row["id"])
        prd_id = _required_string(row["prd_id"])
        if prd_id not in prd_ids:
            _refuse(ReadErrorCode.missing_target, field="tasks")
        task_refs[stored_id] = TaskScopedRefV1(
            prd_id=prd_id,
            task_id=_local_entity_id(stored_id, prd_id),
        )
    if len(task_refs) != len(task_rows):
        _refuse(ReadErrorCode.invalid_hierarchy, field="tasks")

    tasks: list[TaskRecordV1] = []
    dependency_edges = 0
    for row in task_rows:
        stored_id = _required_string(row["id"])
        ref = task_refs[stored_id]
        feature_id = _required_string(row["feature_id"])
        feature_ref = feature_refs.get(feature_id)
        if feature_ref is None:
            _refuse(ReadErrorCode.missing_target, field="features")
        if feature_ref.prd_id != ref.prd_id:
            _refuse(ReadErrorCode.invalid_hierarchy, field="features")
        dependency_ids = _json_string_list(row["dependencies"])
        if len(dependency_ids) > limits.max_dependencies_per_task:
            _limit(
                ProviderLimitNameV1.max_dependencies_per_task,
                len(dependency_ids),
                limits.max_dependencies_per_task,
            )
        if len(set(dependency_ids)) != len(dependency_ids):
            _refuse(ReadErrorCode.duplicate_edge, field="dependencies")
        dependency_edges += len(dependency_ids)
        if dependency_edges > limits.max_dependency_edges:
            _limit(
                ProviderLimitNameV1.max_dependency_edges,
                dependency_edges,
                limits.max_dependency_edges,
            )
        dependencies: list[TaskScopedRefV1] = []
        for dependency_id in dependency_ids:
            target = task_refs.get(dependency_id)
            if target is None:
                _refuse(ReadErrorCode.missing_target, field="dependencies")
            if target == ref:
                _refuse(ReadErrorCode.duplicate_edge, field="dependencies")
            dependencies.append(target)

        acceptance = _json_string_list(row["acceptance_criteria"])
        if len(acceptance) > limits.max_acceptance_criteria_per_task:
            _limit(
                ProviderLimitNameV1.max_acceptance_criteria_per_task,
                len(acceptance),
                limits.max_acceptance_criteria_per_task,
            )
        verification = _verification(row["verification"])
        summaries = _verification_summaries(verification)
        if len(summaries) > limits.max_verification_summaries_per_task:
            _limit(
                ProviderLimitNameV1.max_verification_summaries_per_task,
                len(summaries),
                limits.max_verification_summaries_per_task,
            )
        parent_id = _optional_string(row["parent_task_id"])
        parent_ref = None
        if parent_id is not None:
            parent_ref = task_refs.get(parent_id)
            if parent_ref is None:
                _refuse(ReadErrorCode.missing_target, field="tasks")
            if parent_ref.prd_id != ref.prd_id:
                _refuse(ReadErrorCode.invalid_hierarchy, field="tasks")
        tasks.append(
            TaskRecordV1(
                ref=ref,
                local_id=ref.task_id,
                prd_ref=PrdScopedRefV1(prd_id=ref.prd_id),
                feature_ref=feature_ref,
                parent_ref=parent_ref,
                title=_required_string(row["title"]),
                status=_required_string(row["status"]),
                priority=_required_string(row["priority"]),
                dependency_refs=tuple(dependencies),
                acceptance_criteria=tuple(acceptance),
                verification_summaries=summaries,
            )
        )

    try:
        payload = ProjectSnapshotPayloadV1(
            project=project,
            prds=tuple(sorted(prds, key=lambda item: item.ref.prd_id)),
            features=tuple(
                sorted(features, key=lambda item: (item.ref.prd_id, item.local_id))
            ),
            tasks=tuple(
                sorted(tasks, key=lambda item: (item.ref.prd_id, item.local_id))
            ),
        )
    except ValidationError as exc:
        _map_hierarchy_validation(exc)
    return payload


def _event_cursor(
    conn: sqlite3.Connection,
    events_fh: BinaryIO,
) -> tuple[EventCursorV1, tuple[int, int, int, int]]:
    start_stat = os.fstat(events_fh.fileno())
    events_fh.seek(0)
    by_id: dict[str, tuple[Event, bytes]] = {}
    local_last = -1
    mode: str | None = None
    while True:
        line = events_fh.readline(_MAX_EVENT_RECORD_BYTES + 2)
        if not line:
            break
        if len(line) > _MAX_EVENT_RECORD_BYTES + 1:
            _refuse(ReadErrorCode.projection_not_converged, field="projection")
        if not line.endswith(b"\n") or line.startswith(b"\xef\xbb\xbf"):
            _refuse(ReadErrorCode.projection_not_converged, field="projection")
        raw = line[:-1]
        try:
            document = _strict_json(raw)
            event = Event.model_validate(document)
            record = canonical_json_bytes(
                event.model_dump(mode="json"),
                max_bytes=_MAX_EVENT_RECORD_BYTES,
                max_string_bytes=_MAX_EVENT_RECORD_BYTES,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError):
            _refuse(ReadErrorCode.projection_not_converged, field="projection")
        is_git = event.id.startswith("E-")
        current_mode = "git" if is_git else "local"
        if mode is None:
            mode = current_mode
        elif mode != current_mode:
            _refuse(ReadErrorCode.projection_not_converged, field="projection")
        prior = by_id.get(event.id)
        if prior is not None:
            if mode != "git" or prior[1] != record:
                _refuse(ReadErrorCode.projection_not_converged, field="projection")
            continue
        if mode == "local":
            if event.parent_event_id is not None or event.lamport is not None:
                _refuse(ReadErrorCode.projection_not_converged, field="projection")
            sequence = int(event.id[1:])
            if sequence <= local_last:
                _refuse(ReadErrorCode.projection_not_converged, field="projection")
            local_last = sequence
        elif event.lamport is None:
            _refuse(ReadErrorCode.projection_not_converged, field="projection")
        by_id[event.id] = (event, record)

    identity = (start_stat.st_dev, start_stat.st_ino, start_stat.st_size, start_stat.st_mtime_ns)
    _verify_event_identity(events_fh, identity)

    rows = conn.execute(
        "SELECT id, timestamp, actor, action, target_kind, target_id, "
        "payload_json FROM events ORDER BY id"
    ).fetchall()
    if len(rows) != len(by_id):
        _refuse(ReadErrorCode.projection_not_converged, field="projection")
    for row in rows:
        event_entry = by_id.get(_required_string(row["id"]))
        if event_entry is None:
            _refuse(ReadErrorCode.projection_not_converged, field="projection")
        event_json = event_entry[0].model_dump(mode="json")
        try:
            payload_json = _strict_json(
                _required_string(row["payload_json"], allow_empty=True).encode("utf-8")
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            _refuse(ReadErrorCode.projection_not_converged, field="projection")
        if (
            row["timestamp"] != event_json["timestamp"]
            or row["actor"] != event_json["actor"]
            or row["action"] != event_json["action"]
            or row["target_kind"] != event_json["target_kind"]
            or row["target_id"] != event_json["target_id"]
            or payload_json != event_json["payload_json"]
        ):
            _refuse(ReadErrorCode.projection_not_converged, field="projection")

    if mode == "git":
        _validate_git_material(conn, by_id)

    hasher = hashlib.sha256(EVENT_FRONTIER_DOMAIN_V1)
    records = sorted(record for _, record in by_id.values())
    for record in records:
        hasher.update(len(record).to_bytes(8, "big"))
        hasher.update(record)
    return (
        EventCursorV1(
            event_count=len(records),
            event_frontier_sha256=hasher.hexdigest(),
        ),
        identity,
    )


def _verify_event_identity(
    events_fh: BinaryIO,
    identity: tuple[int, int, int, int],
) -> None:
    end_stat = os.fstat(events_fh.fileno())
    try:
        path_stat = os.stat(events_fh.name)
    except OSError:
        _refuse(ReadErrorCode.projection_not_converged, field="projection")
    if identity != (
        end_stat.st_dev,
        end_stat.st_ino,
        end_stat.st_size,
        end_stat.st_mtime_ns,
    ) or identity != (
        path_stat.st_dev,
        path_stat.st_ino,
        path_stat.st_size,
        path_stat.st_mtime_ns,
    ):
        _refuse(ReadErrorCode.projection_not_converged, field="projection")


def _validate_git_material(
    conn: sqlite3.Connection,
    by_id: dict[str, tuple[Event, bytes]],
) -> None:
    state = conn.execute(
        "SELECT initialized FROM git_event_material_state WHERE singleton = 1"
    ).fetchone()
    if state is None or state[0] != 1:
        _refuse(ReadErrorCode.projection_not_converged, field="projection")
    rows = conn.execute(
        "SELECT event_id, fingerprint FROM git_event_material"
    ).fetchall()
    if len(rows) != len(by_id):
        _refuse(ReadErrorCode.projection_not_converged, field="projection")
    fingerprints = {row["event_id"]: row["fingerprint"] for row in rows}
    for event_id, (event, _) in by_id.items():
        material = json.dumps(
            event.model_dump(mode="json", exclude={"id"}),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if fingerprints.get(event_id) != hashlib.sha256(material).hexdigest():
            _refuse(ReadErrorCode.projection_not_converged, field="projection")


def _validated_source_binding(
    row: sqlite3.Row,
) -> tuple[str | None, int | None, str | None]:
    state = _required_string(row["provenance_state"])
    available = _strict_db_bool(row["content_available"])
    fields = (
        row["source_bytes"],
        row["source_sha256"],
        row["source_size_bytes"],
        row["source_encoding"],
        row["source_revision"],
    )
    if state == "legacy_unbound":
        if available or any(value is not None for value in fields):
            _refuse(ReadErrorCode.source_drift, field="prds")
        return None, None, None
    if state != "available" or not available or any(value is None for value in fields):
        _refuse(ReadErrorCode.source_drift, field="prds")
    source_bytes = bytes(row["source_bytes"])
    try:
        source_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        _refuse(ReadErrorCode.invalid_utf8, field="prds")
    source_size = _required_int(row["source_size_bytes"], minimum=0)
    source_revision = _required_int(row["source_revision"], minimum=1)
    revision = _required_int(row["revision"], minimum=1)
    source_sha = _required_string(row["source_sha256"])
    encoding = _required_string(row["source_encoding"])
    if (
        encoding != "utf-8"
        or len(source_bytes) != source_size
        or hashlib.sha256(source_bytes).hexdigest() != source_sha
        or source_revision != revision
    ):
        _refuse(ReadErrorCode.source_drift, field="prds")
    return source_sha, source_size, encoding


def _verification(raw: Any) -> Verification:
    try:
        if not isinstance(raw, str):
            raise TypeError
        return Verification.model_validate(_strict_json(raw.encode("utf-8")))
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError):
        _refuse(ReadErrorCode.invalid_hierarchy, field="tasks")


def _verification_summaries(
    verification: Verification,
) -> tuple[VerificationSummaryV1, ...]:
    groups = (
        (VerificationKindV1.command, "Automated checks", len(verification.commands)),
        (VerificationKindV1.manual_step, "Manual checks", len(verification.manual_steps)),
        (
            VerificationKindV1.required_evidence,
            "Required evidence",
            len(verification.required_evidence),
        ),
        (
            VerificationKindV1.typed_proof,
            "Typed proofs",
            len(verification.required_proofs) + len(verification.artifact_assertions),
        ),
    )
    return tuple(
        VerificationSummaryV1(kind=kind, label=label, count=count)
        for kind, label, count in groups
        if count
    )


def _local_entity_id(stored_id: str, prd_id: str) -> str:
    if prd_id == "default":
        if ":" in stored_id:
            _refuse(ReadErrorCode.invalid_hierarchy, field="tasks")
        return stored_id
    prefix = f"{prd_id}:"
    if not stored_id.startswith(prefix):
        _refuse(ReadErrorCode.invalid_hierarchy, field="tasks")
    local_id = stored_id[len(prefix) :]
    if ":" in local_id:
        _refuse(ReadErrorCode.invalid_hierarchy, field="tasks")
    return local_id


def _json_string_list(raw: Any) -> list[str]:
    if not isinstance(raw, str):
        _refuse(ReadErrorCode.invalid_hierarchy, field="tasks")
    try:
        value = _strict_json(raw.encode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        _refuse(ReadErrorCode.invalid_hierarchy, field="tasks")
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        _refuse(ReadErrorCode.invalid_hierarchy, field="tasks")
    return value


def _strict_json(raw: bytes) -> Any:
    text = raw.decode("utf-8", errors="strict")

    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    return json.loads(
        text,
        object_pairs_hook=object_pairs,
        parse_constant=lambda _value: (_ for _ in ()).throw(
            ValueError("non-finite JSON number")
        ),
    )


def _map_hierarchy_validation(exc: ValidationError) -> NoReturn:
    messages = " ".join(str(item.get("msg", "")) for item in exc.errors())
    if "duplicate" in messages or "itself" in messages:
        _refuse(ReadErrorCode.duplicate_edge, field="dependencies")
    if "cycle" in messages:
        _refuse(ReadErrorCode.dependency_cycle, field="dependencies")
    if "missing" in messages:
        _refuse(ReadErrorCode.missing_target, field="tasks")
    _refuse(ReadErrorCode.invalid_hierarchy, field="state")


def _table_count(conn: sqlite3.Connection, table: str) -> int:
    # Table names are closed constants supplied only by this module.
    row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()  # noqa: S608
    return int(row[0]) if row else 0


def _enforce_serialized_limits(
    payload: ProjectSnapshotPayloadV1,
    cursor: EventCursorV1,
    payload_digest: str,
    limits: ProviderReadLimitsV1,
) -> None:
    payload_document = payload.model_dump(mode="json")
    max_label_bytes = max(
        (
            len(summary.label.encode("utf-8"))
            for task in payload.tasks
            for summary in task.verification_summaries
        ),
        default=0,
    )
    if max_label_bytes > limits.max_verification_summary_label_bytes:
        _limit(
            ProviderLimitNameV1.max_verification_summary_label_bytes,
            max_label_bytes,
            limits.max_verification_summary_label_bytes,
        )
    payload_bytes = canonical_json_bytes(
        payload_document,
        max_depth=PROVIDER_LIMITS_V1.max_canonical_json_depth,
        max_nodes=PROVIDER_LIMITS_V1.max_snapshot_bytes,
        max_bytes=PROVIDER_LIMITS_V1.max_snapshot_bytes,
        max_string_bytes=PROVIDER_LIMITS_V1.max_string_bytes,
    )
    if len(payload_bytes) > limits.max_snapshot_bytes:
        _limit(
            ProviderLimitNameV1.max_snapshot_bytes,
            len(payload_bytes),
            limits.max_snapshot_bytes,
        )
    response_document = {
        "payload": payload_document,
        "event_cursor": cursor.model_dump(mode="json"),
        "applied_limits": limits.model_dump(mode="json"),
        "snapshot_digest": payload_digest,
    }
    max_string_bytes = max(
        (len(value.encode("utf-8")) for value in _walk_strings(response_document)),
        default=0,
    )
    if max_string_bytes > limits.max_string_bytes:
        _limit(
            ProviderLimitNameV1.max_string_bytes,
            max_string_bytes,
            limits.max_string_bytes,
        )
    depth = _json_depth(response_document)
    if depth > limits.max_canonical_json_depth:
        _limit(
            ProviderLimitNameV1.max_canonical_json_depth,
            depth,
            limits.max_canonical_json_depth,
        )
    response_bytes = canonical_json_bytes(
        response_document,
        max_depth=PROVIDER_LIMITS_V1.max_canonical_json_depth,
        max_nodes=PROVIDER_LIMITS_V1.max_response_bytes,
        max_bytes=PROVIDER_LIMITS_V1.max_response_bytes,
        max_string_bytes=PROVIDER_LIMITS_V1.max_string_bytes,
    )
    if len(response_bytes) > limits.max_response_bytes:
        _limit(
            ProviderLimitNameV1.max_response_bytes,
            len(response_bytes),
            limits.max_response_bytes,
        )


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


def _json_depth(value: Any) -> int:
    if isinstance(value, Mapping):
        return 1 + max((_json_depth(item) for item in value.values()), default=0)
    if isinstance(value, (list, tuple)):
        return 1 + max((_json_depth(item) for item in value), default=0)
    return 0


def _gate_count(
    conn: sqlite3.Connection,
    table: str,
    limit: int,
    limit_name: ProviderLimitNameV1,
) -> None:
    actual = _table_count(conn, table)
    if actual > limit:
        _limit(limit_name, actual, limit)


def _required_string(value: Any, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ValueError("invalid persisted string")
    return value


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    return _required_string(value, allow_empty=True)


def _required_int(value: Any, *, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError("invalid persisted integer")
    return value


def _strict_db_bool(value: Any) -> bool:
    if type(value) is not int or value not in (0, 1):
        raise ValueError("invalid persisted boolean")
    return bool(value)


def _limit(name: ProviderLimitNameV1, actual: int, limit: int) -> NoReturn:
    raise ProjectSnapshotError(
        ProviderLimitRefusalV1(
            operation_id=PROJECT_SNAPSHOT_OPERATION_ID,
            limit_name=name,
            actual=actual,
            limit=limit,
        )
    )


def _refuse(
    code: ReadErrorCode,
    *,
    field: str,
    actual: int | None = None,
    limit: int | None = None,
) -> NoReturn:
    raise ProjectSnapshotError(
        ReadErrorV1(
            code=code,
            field=field,
            actual=actual,
            limit=limit,
        )
    ) from None


__all__ = [
    "EVENT_FRONTIER_DOMAIN_V1",
    "ProjectSnapshotError",
    "read_project_snapshot",
]
