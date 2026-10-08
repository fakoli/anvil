"""Bounded, observational current-attempt reads. Never grants execution authority."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, BinaryIO, Literal, NoReturn

from pydantic import BaseModel, ConfigDict, ValidationError

from anvil.attempt_timing import project_attempt_timing, project_bundle_timing
from anvil.project_snapshot import (
    ProjectSnapshotError,
    _event_cursor,
    _local_entity_id,
    _strict_json,
    _verify_event_identity,
)
from anvil.read_contracts import PrdScopedRefV1, ReadErrorCode
from anvil.state.backend import SchemaMismatch, SchemaProbeFailed
from anvil.state.hashing import CanonicalJsonRefusal, canonical_json_bytes
from anvil.state.models import (
    BundleCheckpoint,
    Claim,
    Evidence,
    ExecutionBundle,
    HookCommandAttribution,
    Task,
    TaskStatus,
    task_snapshot_revision,
)
from anvil.state.payloads import BundleCreatedPayload, TaskAppliedPayload
from anvil.state.sqlite import query_only_transaction

SCHEMA_ID = "anvil.state.attempt-view.v1"
DIGEST_DOMAIN = b"anvil.state.attempt-view.v1\0"


class AttemptViewRefusal(BaseModel):
    """Execution-specific safe errors, without widening provider read contracts."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_id: Literal["anvil.state.attempt-view-error.v1"] = "anvil.state.attempt-view-error.v1"
    code: ReadErrorCode
    field: str
    actual: int | None = None
    limit: int | None = None
    message: Literal["The attempt view could not be read completely."] = (
        "The attempt view could not be read completely."
    )


class AttemptViewError(ProjectSnapshotError):
    """A safe refusal; shared frontier failures also use ProjectSnapshotError."""

    def __init__(self, error: AttemptViewRefusal) -> None:
        self.error = error
        RuntimeError.__init__(self, error.message)


def _refuse(
    code: ReadErrorCode, *, field: str, actual: int | None = None, limit: int | None = None
) -> NoReturn:
    raise AttemptViewError(AttemptViewRefusal(code=code, field=field, actual=actual, limit=limit))


@dataclass(frozen=True)
class AttemptViewLimits:
    """Hard ceilings; callers may lower any ceiling but cannot raise one."""

    max_event_log_bytes: int = 32 * 1024 * 1024
    max_event_records: int = 20_000
    max_event_bytes: int = 1024 * 1024
    max_cell_bytes: int = 256 * 1024
    max_response_bytes: int = 64 * 1024


def _limits(requested: AttemptViewLimits | Mapping[str, Any] | None) -> AttemptViewLimits:
    values = asdict(AttemptViewLimits())
    supplied = asdict(requested) if isinstance(requested, AttemptViewLimits) else requested
    if supplied is not None:
        if not isinstance(supplied, Mapping) or len(supplied) > len(values):
            _refuse(ReadErrorCode.invalid_request, field="limits")
        for key, value in supplied.items():
            if key not in values or type(value) is not int or not 0 < value <= values[key]:
                _refuse(ReadErrorCode.invalid_request, field="limits")
            values[key] = value
    return AttemptViewLimits(**values)


def _overflow(field: str, actual: int, limit: int) -> None:
    _refuse(ReadErrorCode.limit_exceeded, field=field, actual=actual, limit=limit)


class _BoundedLog:
    """Apply limits before the shared frontier reader allocates each record."""

    def __init__(self, source: BinaryIO, limits: AttemptViewLimits) -> None:
        self.source, self.limits = source, limits
        self.count = self.total = 0
        self.name = source.name
        size = os.fstat(source.fileno()).st_size
        if size > limits.max_event_log_bytes:
            _overflow("max_event_log_bytes", size, limits.max_event_log_bytes)

    def fileno(self) -> int:
        return self.source.fileno()

    def seek(self, offset: int) -> int:
        self.count = self.total = 0
        return self.source.seek(offset)

    def readline(self, size: int) -> bytes:
        # ponytail: capped whole-history scan; an engine-owned incremental frontier
        # is the upgrade path once 32 MiB / 20,000 records is insufficient.
        line = self.source.readline(min(size, self.limits.max_event_bytes + 2))
        if line:
            self.count += 1
            self.total += len(line)
            for field, actual in (
                ("max_event_records", self.count),
                ("max_event_log_bytes", self.total),
                ("max_event_bytes", len(line.removesuffix(b"\n"))),
            ):
                if actual > getattr(self.limits, field):
                    _overflow(field, actual, getattr(self.limits, field))
        return line


def _rows(
    conn: sqlite3.Connection,
    table: str,
    columns: str,
    where: str,
    args: tuple[Any, ...],
    limits: AttemptViewLimits,
    *,
    order: str = "id",
    preflight_only: bool = False,
    budget: list[int] | None = None,
) -> list[dict[str, Any]]:
    """Inspect byte lengths in SQLite before transferring any projected cell."""
    names = [name.split(" AS ")[0] for name in columns.split(", ")]
    count = conn.execute(f"SELECT count(*) FROM {table} WHERE {where}", args).fetchone()[0]
    if count > limits.max_event_records:
        _overflow("max_event_records", count, limits.max_event_records)
    checks = ", ".join(f"max(length(CAST({name} AS BLOB)))" for name in names)
    sizes = conn.execute(f"SELECT {checks} FROM {table} WHERE {where}", args).fetchone()
    # Event payloads obey the event ceiling; smaller projected fields obey the
    # cell ceiling. Otherwise a valid unrelated proof event can block this read.
    cell_field = "max_event_bytes" if table == "events" else "max_cell_bytes"
    cell_limit = getattr(limits, cell_field)
    for size in sizes:
        if size is not None and size > cell_limit:
            _overflow(cell_field, size, cell_limit)
    # Bound aggregate allocation as well as every individual cell.
    size_expr = " + ".join(f"coalesce(length(CAST({name} AS BLOB)), 0)" for name in names)
    total = conn.execute(
        f"SELECT coalesce(sum({size_expr}), 0) FROM {table} WHERE {where}", args
    ).fetchone()[0]
    if total > limits.max_event_log_bytes:
        _overflow("max_event_log_bytes", total, limits.max_event_log_bytes)
    if budget is not None:
        budget[0] += count
        budget[1] += total
        if budget[0] > limits.max_event_records:
            _overflow("max_event_records", budget[0], limits.max_event_records)
        if budget[1] > limits.max_event_log_bytes:
            _overflow("max_event_log_bytes", budget[1], limits.max_event_log_bytes)
    if preflight_only:
        return []
    return [
        dict(row)
        for row in conn.execute(
            f"SELECT {columns} FROM {table} WHERE {where} ORDER BY {order}", args
        )
    ]


def _json_columns(row: dict[str, Any], names: str) -> dict[str, Any]:
    result = dict(row)
    for name in names.split():
        raw = result[name]
        if raw is not None:
            if type(raw) is not str:
                _refuse(ReadErrorCode.invalid_hierarchy, field="projection")
            result[name] = _strict_json(raw.encode("utf-8"))
    return result


def _one(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if len(rows) != 1:
        _refuse(ReadErrorCode.missing_target, field="identity")
    return rows[0]


def _pick(value: dict[str, Any], names: str) -> dict[str, Any]:
    return {key: value[key] for key in names.split() if key in value}


def _task_ref(prd_id: str, task_id: str) -> dict[str, str]:
    # Native task numbers have minimum width three; provider v1 is narrower.
    reference = PrdScopedRefV1(prd_id=prd_id).model_dump(mode="json")
    if type(task_id) is not str or re.fullmatch(r"T[0-9]{3,}(?:\.[0-9]+)*", task_id) is None:
        _refuse(ReadErrorCode.invalid_identifier, field="identity")
    return {**reference, "task_id": task_id}


def read_attempt_view(
    state_dir: str | os.PathLike[str],
    task_id: str,
    *,
    prd_id: str | None = None,
    limits: AttemptViewLimits | Mapping[str, Any] | None = None,
    observation_at: str | None = None,
    bundle: bool = False,
) -> dict[str, Any]:
    return _read_frontier(
        state_dir,
        task_id,
        prd_id=prd_id,
        limits=limits,
        observation_at=observation_at,
        bundle=bundle,
    )


def read_evidence_preflight(
    state_dir: str | os.PathLike[str],
    task_id: str,
    *,
    prd_id: str | None = None,
    limits: AttemptViewLimits | Mapping[str, Any] | None = None,
    observation_at: str | None = None,
) -> dict[str, Any]:
    """Inspect the complete buffer under one read frontier; never authorize submit."""
    return _read_frontier(
        state_dir,
        task_id,
        prd_id=prd_id,
        limits=limits,
        observation_at=observation_at,
        preflight=True,
    )


def _read_frontier(
    state_dir,
    task_id,
    *,
    prd_id=None,
    limits=None,
    observation_at=None,
    bundle=False,
    preflight=False,
) -> dict[str, Any]:
    """Return complete current facts and labeled history, or a closed refusal.

    Errors use ProjectSnapshotError / ReadErrorV1. There is no truncation,
    implicit repair, owner-state read, lease renewal, or sidecar generation.
    """
    applied = _limits(limits)
    try:
        if type(bundle) is not bool:
            _refuse(ReadErrorCode.invalid_request, field="bundle")
        if type(task_id) is not str or (prd_id is not None and type(prd_id) is not str):
            _refuse(ReadErrorCode.invalid_identifier, field="identity")
        if ":" in task_id:
            prefix, local = task_id.split(":", 1)
            if prd_id is not None and prd_id != prefix:
                _refuse(ReadErrorCode.missing_target, field="identity")
            prd_id = prefix
        else:
            local = task_id
        scope = prd_id or "default"
        if bundle:
            if not task_id.strip() or len(task_id) > 255 or any(ord(c) < 32 for c in task_id):
                _refuse(ReadErrorCode.invalid_identifier, field="identity")
            stored = task_id
        else:
            _task_ref(scope, local)
            stored = local if scope == "default" else f"{scope}:{local}"
        root = Path(state_dir)
        with query_only_transaction(root / "state.db", root / "events.jsonl") as (conn, fh):
            bounded = _BoundedLog(fh, applied)
            # The shared checker reads DB event material too; preflight it first.
            _rows(
                conn,
                "events",
                "id, timestamp, actor, action, target_kind, target_id, payload_json",
                "1",
                (),
                applied,
                preflight_only=True,
            )
            cursor, identity = _event_cursor(conn, bounded)
            if bundle:
                result = _compose_bundle(conn, stored, prd_id, applied, observation_at)
            elif preflight:
                result = _compose(conn, stored, scope, applied, observation_at, preflight_root=root)
            else:
                result = _compose(conn, stored, scope, applied, observation_at)
            result.update(
                schema_id="anvil.state.evidence-preflight.v1" if preflight else SCHEMA_ID,
                operation_version=1,
                event_cursor=cursor.model_dump(mode="json"),
                applied_limits=asdict(applied),
            )
            encoded = _encode(result, applied)
            domain = b"anvil.state.evidence-preflight.v1\0" if preflight else DIGEST_DOMAIN
            result["view_digest"] = "sha256:" + hashlib.sha256(domain + encoded).hexdigest()
            _encode(result, applied)
            _verify_event_identity(fh, identity)
            return result
    except ProjectSnapshotError:
        raise
    except FileNotFoundError:
        _refuse(ReadErrorCode.state_unavailable, field="state")
    except SchemaProbeFailed:
        _refuse(ReadErrorCode.projection_not_converged, field="projection")
    except SchemaMismatch:
        _refuse(ReadErrorCode.schema_incompatible, field="schema")
    except (sqlite3.Error, OSError):
        _refuse(ReadErrorCode.state_unavailable, field="state")
    except (CanonicalJsonRefusal, ValidationError, TypeError, ValueError, KeyError, RecursionError):
        _refuse(ReadErrorCode.invalid_hierarchy, field="projection")


def _encode(value: dict[str, Any], limits: AttemptViewLimits) -> bytes:
    try:
        return canonical_json_bytes(
            value, max_bytes=limits.max_response_bytes, max_string_bytes=limits.max_response_bytes
        )
    except CanonicalJsonRefusal:
        _overflow("max_response_bytes", limits.max_response_bytes + 1, limits.max_response_bytes)


def _delivery(bundle: dict[str, Any]) -> dict[str, Any]:
    return {
        "bundle_id": bundle["id"],
        "recorded_status": bundle["status"],
        "checkpoint": bundle.get("checkpoint"),
        "deployed": "unknown",
        "checkpoint_is_delivery_proof": False,
    }


def _compose_bundle(conn, bundle_id, scope, limits, observation_at):
    budget, response_budget = [0, 0], [0]
    row = _one(
        _rows(
            conn,
            "execution_bundles",
            "id, creation_event_id, prd_id, coordinator, status, review_disposition_event_id, "
            "superseded_by, last_result_at, branch, review_policy, throughput_budget, "
            "checkpoint, created_at, updated_at",
            "id = ?",
            (bundle_id,),
            limits,
            budget=budget,
        )
    )
    if scope is not None and row["prd_id"] != scope:
        _refuse(ReadErrorCode.missing_target, field="identity")
    scope = row["prd_id"]
    membership = _rows(
        conn,
        "execution_bundle_members",
        "task_id, position",
        "bundle_id = ?",
        (bundle_id,),
        limits,
        order="position",
        budget=budget,
    )
    if [m["position"] for m in membership] != list(range(len(membership))):
        _refuse(ReadErrorCode.projection_not_converged, field="membership")
    row["task_ids"] = [m["task_id"] for m in membership]
    bundle = ExecutionBundle.model_validate_json(
        canonical_json_bytes(_json_columns(row, "review_policy throughput_budget checkpoint")),
        strict=True,
    )
    creation = _one(
        _rows(
            conn,
            "events",
            "id, action, target_kind, target_id, payload_json",
            "id = ?",
            (bundle.creation_event_id,),
            limits,
            budget=budget,
        )
    )
    source = BundleCreatedPayload.model_validate(
        _json_columns(creation, "payload_json")["payload_json"]
    )
    if (
        creation["action"] != "bundle.created"
        or creation["target_kind"] != "bundle"
        or creation["target_id"] != bundle_id
        or source.id != bundle_id
        or source.prd_id != scope
        or source.task_ids != bundle.task_ids
        or source.coordinator != bundle.coordinator
    ):
        _refuse(ReadErrorCode.projection_not_converged, field="membership")
    safe = _pick(
        bundle.model_dump(mode="json"),
        "id creation_event_id prd_id task_ids coordinator status review_disposition_event_id "
        "superseded_by last_result_at branch review_policy throughput_budget checkpoint "
        "created_at updated_at",
    )
    safe["reviews"] = _rows(conn, "bundle_review_verdicts",
        "id, creation_event_id, disposition_event_id, review_round, angle, reviewed_by, "
        "decision, created_at", "bundle_id = ?", (bundle_id,), limits,
        order="rowid", budget=budget)
    if any(review["creation_event_id"] != bundle.creation_event_id for review in safe["reviews"]):
        _refuse(ReadErrorCode.projection_not_converged, field="reviews")
    response_budget[0] += len(_encode(safe, limits))
    members = [
        _compose(
            conn,
            task_id,
            scope,
            limits,
            observation_at,
            budget=budget,
            response_budget=response_budget,
        )
        for task_id in bundle.task_ids
    ]
    project_ids = {member["identity"]["project_id"] for member in members}
    if len(project_ids) != 1:
        _refuse(ReadErrorCode.invalid_hierarchy, field="identity")
    return {
        "identity": {
            "project_id": members[0]["identity"]["project_id"],
            "prd_id": scope,
            "bundle_id": bundle_id,
        },
        "bundle": safe,
        "members": members,
        "timing": project_bundle_timing(members),
        "source_delivery": _delivery(safe),
        "custody": {"external_owner_state": "unknown", "runner_stop": "unknown"},
        "mutation_authority": False,
    }


def _compose(
    conn: sqlite3.Connection,
    stored: str,
    scope: str,
    limits: AttemptViewLimits,
    observation_at: str | None = None,
    *,
    budget: list[int] | None = None,
    response_budget: list[int] | None = None,
    preflight_root: Path | None = None,
) -> dict[str, Any]:
    budget = [0, 0] if budget is None else budget
    response_budget = [0] if response_budget is None else response_budget

    def reserve_response(value: dict[str, Any]) -> None:
        response_budget[0] += len(_encode(value, limits))
        if response_budget[0] > limits.max_response_bytes:
            _overflow("max_response_bytes", response_budget[0], limits.max_response_bytes)

    def rows(
        table: str, columns: str, where: str = "1", args: tuple = (), *, order: str = "id"
    ) -> list[dict[str, Any]]:
        return _rows(conn, table, columns, where, args, limits, order=order, budget=budget)

    project = _one(rows("projects", "id, name"))
    prd = _one(rows("prds", "id, project_id, revision, status", "id = ?", (scope,)))
    if prd["project_id"] != project["id"]:
        _refuse(ReadErrorCode.invalid_hierarchy, field="identity")
    task_row = _one(
        rows(
            "tasks",
            "id, feature_id, prd_id, title, description, status, priority, task_type, "
            "dependencies, conflict_groups, scores, acceptance_criteria, implementation_notes, "
            "verification, claims, likely_files, parent_task_id, created_at, updated_at",
            "id = ? AND prd_id = ?",
            (stored, scope),
        )
    )
    task_data = _json_columns(
        task_row,
        "dependencies conflict_groups scores acceptance_criteria "
        "implementation_notes verification claims likely_files",
    )
    # Match the native row reader, including JSON arrays in frozen profile bindings.
    typed_task = Task.model_validate(task_data)
    task = typed_task.model_dump(mode="json")
    reserve_response(task)
    feature = _one(
        rows(
            "features",
            "id, prd_id, requirements",
            "id = ? AND prd_id = ?",
            (task["feature_id"], scope),
        )
    )
    requirement_ids = _json_columns(feature, "requirements")["requirements"]
    if type(requirement_ids) is not list or any(type(item) is not str for item in requirement_ids):
        _refuse(ReadErrorCode.invalid_hierarchy, field="requirements")
    requirements = []
    for req_id in dict.fromkeys(requirement_ids):
        requirement = _one(
            rows(
                "requirements",
                "id, prd_id, text, prd_section, revision_introduced, revision_superseded",
                "id = ? AND prd_id = ?",
                (req_id, scope),
            )
        )
        reserve_response(requirement)
        requirements.append(requirement)
    dependencies = []
    for dep_id in task["dependencies"]:
        dep = _one(rows("tasks", "id, prd_id, status", "id = ?", (dep_id,)))
        TaskStatus(dep["status"])
        dependency = {
            "ref": _task_ref(dep["prd_id"], _local_entity_id(dep["id"], dep["prd_id"])),
            "stored_task_id": dep["id"],
            "status": dep["status"],
        }
        reserve_response(dependency)
        dependencies.append(dependency)

    claims, timing_claims = [], []
    for raw in rows(
        "claims",
        "id, task_id, claimed_by, claim_type, status, generation, root_set, "
        "bundle_claim_id, attestation_context, created_at, lease_expires_at, last_heartbeat_at, "
        "released_at, release_reason, git_metadata, branch, worktree_path",
        "task_id = ?",
        (stored,),
        order="generation",
    ):
        claim = Claim.model_validate_json(
            canonical_json_bytes(_json_columns(raw, "root_set attestation_context git_metadata")),
            strict=True,
        ).model_dump(mode="json")
        safe = _pick(
            claim,
            "id task_id claimed_by claim_type status generation bundle_claim_id "
            "created_at lease_expires_at last_heartbeat_at released_at release_reason",
        )
        context = claim.get("attestation_context")
        safe["proof_attribution"] = HookCommandAttribution(
            project_id=project["id"],
            claim_id=claim["id"],
            generation=claim["generation"],
            claimed_by=claim["claimed_by"],
            task_id=claim["task_id"],
            task_revision=context["task_revision"]
            if context
            else task_snapshot_revision(typed_task),
            prd_id=context["prd_id"] if context else scope,
            prd_revision=context["prd_revision"] if context else prd["revision"],
            repository_id=context["repository_id"] if context else None,
            claim_start_sha=context["claim_start_sha"] if context else None,
        ).model_dump(mode="json")
        if claim.get("git_metadata"):
            safe["git_metadata"] = _pick(
                claim["git_metadata"],
                "schema_version mode selected_default_base_ref selected_default_base_sha "
                "claim_start_ref claim_start_sha branch",
            )
        root = claim.get("root_set")
        if root:
            safe["root_set"] = _pick(
                root,
                "schema_version request_id primary_root_id request_digest "
                "root_set_digest reservation_id",
            )
            safe["root_set"]["root_facts"] = [
                _pick(fact, "root_id repository_id baseline_sha") for fact in root["root_facts"]
            ]
        reserve_response(safe)
        claims.append(safe)
        timing_claims.append({**claim, "proof_attribution": safe["proof_attribution"]})
    by_claim = {claim["id"]: claim for claim in claims}
    active = [claim for claim in claims if claim["status"] == "active"]
    if len(active) > 1:
        _refuse(ReadErrorCode.invalid_hierarchy, field="claims")

    if preflight_root is not None:
        from anvil.claims.evidence_import import (
            CommandBufferInspection,
            CommandProofImportOverflow,
            inspect_command_buffer,
            project_evidence_preflight,
        )

        current = active[0] if active else (claims[-1] if claims else None)
        parent = None
        if current and current.get("bundle_claim_id"):
            parent = _one(
                rows(
                    "bundle_claims",
                    "id, bundle_id, claimed_by, status, released_at, lease_expires_at, "
                    "member_claim_ids",
                    "id = ?",
                    (current["bundle_claim_id"],),
                )
            )
            parent = _json_columns(parent, "member_claim_ids")
            bundle_row = _one(
                rows(
                    "execution_bundles", "id, coordinator, status", "id = ?", (parent["bundle_id"],)
                )
            )
            parent["bundle_status"] = bundle_row["status"]
            parent["coordinator"] = bundle_row["coordinator"]
            if parent["member_claim_ids"].get(stored) != current["id"]:
                _refuse(ReadErrorCode.projection_not_converged, field="claims")
        try:
            inspection = (
                inspect_command_buffer(preflight_root, current["id"])
                if current
                else CommandBufferInspection("ineligible", None, 0, 0, (), ())
            )
            return project_evidence_preflight(
                identity={
                    "project_id": project["id"],
                    "prd_id": scope,
                    "task_id": _local_entity_id(stored, scope),
                    "stored_task_id": stored,
                },
                task=typed_task,
                claim=current,
                inspection=inspection,
                observation_at=observation_at,
                bundle_claim=parent, prd_revision=prd["revision"],
            )
        except CommandProofImportOverflow:
            _refuse(ReadErrorCode.invalid_hierarchy, field="buffer")

    events, timing_events = [], []
    event_rows = rows(
        "events",
        "rowid AS causal_order, id, timestamp, actor, action, target_kind, target_id, payload_json",
        "(target_kind = 'task' AND target_id = ?) OR "
        "(target_kind = 'claim' AND target_id IN (SELECT id FROM claims WHERE task_id = ?)) OR "
        "(target_kind = 'bundle' AND action IN "
        "('bundle.claimed', 'bundle.claim_renewed', 'bundle.claim_released', 'bundle.claim_stale') "
        "AND target_id IN (SELECT bundle_id FROM bundle_claims WHERE id IN "
        "(SELECT bundle_claim_id FROM claims WHERE task_id = ?)))",
        (stored, stored, stored),
        order="rowid",
    )
    evidence_events = {}
    review_events = {}
    review_materials = {}
    for row in event_rows:
        payload = _json_columns(row, "payload_json").pop("payload_json")
        if type(payload) is not dict:
            _refuse(ReadErrorCode.invalid_hierarchy, field="events")
        timing_events.append({**row, "payload": payload})
        event = _pick(row, "id causal_order timestamp actor action target_kind target_id")
        event["payload"] = _pick(
            payload,
            "task_id claim_id evidence_id review_attempt_id generation "
            "claimed_by submitted_by reviewer decision status released_at "
            "created_at noted_at phase rejection",
        )
        # Never return arbitrary event payloads (source bytes, root paths, logs).
        reserve_response(event)
        events.append(event)
        if row["action"] == "evidence.submitted":
            evidence_id = payload.get("evidence_id")
            if type(evidence_id) is not str or evidence_id in evidence_events:
                _refuse(ReadErrorCode.invalid_hierarchy, field="evidence")
            evidence_events[evidence_id] = event
        if row["action"] == "task.applied":
            # This is the native review projection identity, not a timestamp match.
            review_events[f"RV-{row['id']}"] = event
            review_materials[f"RV-{row['id']}"] = TaskAppliedPayload.model_validate(payload)

    evidence = []
    for raw in rows(
        "evidence",
        "id, task_id, claim_id, commands_run, pr_url, commit_sha, known_limitations, "
        "proofs, category, submitted_at, submitted_by",
        "task_id = ?",
        (stored,),
    ):
        item = Evidence.model_validate_json(
            canonical_json_bytes(_json_columns(raw, "commands_run proofs")),
            strict=True,
        ).model_dump(mode="json")
        event = evidence_events.get(item["id"])
        claim = by_claim.get(item["claim_id"])
        if (
            event is None
            or claim is None
            or any(
                event["payload"].get(key) != item[key]
                for key in ("claim_id", "task_id", "submitted_by")
            )
        ):
            _refuse(ReadErrorCode.projection_not_converged, field="evidence")
        safe = _pick(
            item,
            "id task_id claim_id commands_run pr_url commit_sha known_limitations "
            "category submitted_at submitted_by",
        )
        safe.update(
            event_id=event["id"],
            causal_order=event["causal_order"],
            generation=claim["generation"],
            proofs=[_proof(p) for p in item["proofs"]],
        )
        reserve_response(safe)
        evidence.append(safe)
    if len(evidence_events) != len(evidence):
        _refuse(ReadErrorCode.projection_not_converged, field="evidence")
    evidence.sort(key=lambda item: item["causal_order"])
    by_evidence = {item["id"]: item for item in evidence}

    reviews = []
    for row in rows(
        "reviews",
        "id, target_kind, target_id, reviewed_by, decision, notes, rejection_category, "
        "rejection_reason_code, claim_id, review_attempt_id, supporting_evidence_digest, "
        "quality_findings, matched_process_predicate, counts_toward_accept_rate, created_at",
        "target_kind = 'task' AND target_id = ?",
        (stored,),
    ):
        review = _json_columns(row, "quality_findings")
        event = review_events.get(review["id"])
        if (
            event is None
            or type(review["counts_toward_accept_rate"]) is not int
            or review["counts_toward_accept_rate"] not in (0, 1)
        ):
            _refuse(ReadErrorCode.projection_not_converged, field="reviews")
        material = review_materials[review["id"]]
        expected_attempt = (
            material.rejection.review_attempt_id
            if material.rejection
            else material.review_attempt_id
        )
        if (
            review["review_attempt_id"] != expected_attempt
            or review["decision"] != material.decision
            or review["reviewed_by"] != material.reviewer
            or review["notes"] != material.notes
        ):
            _refuse(ReadErrorCode.projection_not_converged, field="reviews")
        if material.rejection:
            expected = material.rejection.model_dump(mode="json")
            mapping = {"rejection_category": "category", "rejection_reason_code": "reason_code"}
            for key in (
                "rejection_category",
                "rejection_reason_code",
                "claim_id",
                "supporting_evidence_digest",
                "quality_findings",
                "matched_process_predicate",
                "counts_toward_accept_rate",
            ):
                if review[key] != expected[mapping.get(key, key)]:
                    _refuse(ReadErrorCode.projection_not_converged, field="reviews")
        attempt = review["review_attempt_id"]
        if attempt is not None and attempt not in by_evidence:
            _refuse(ReadErrorCode.projection_not_converged, field="reviews")
        review.update(
            event_id=event["id"],
            causal_order=event["causal_order"],
            binding_status="exact" if attempt is not None else "legacy_unknown",
        )
        bound = by_evidence.get(attempt)
        if bound:
            review.update(
                evidence_id=bound["id"],
                evidence_event_id=bound["event_id"],
                claim_id=bound["claim_id"],
                generation=bound["generation"],
            )
        if material.invalidation:
            accepted_event = review_events.get(f"RV-{material.invalidation.accepted_event_id}")
            accepted_material = review_materials.get(
                f"RV-{material.invalidation.accepted_event_id}"
            )
            if (
                accepted_event is None
                or accepted_material.decision != "accepted"
                or accepted_material.review_attempt_id != attempt
                or accepted_event["causal_order"] >= event["causal_order"]
            ):
                _refuse(ReadErrorCode.projection_not_converged, field="invalidation")
            review.pop("notes", None)
            review["kind"] = "acceptance_invalidation"
            review["counts_toward_quality_rejection"] = False
            review["invalidation"] = _pick(
                material.invalidation.model_dump(mode="json"),
                "accepted_event_id binding_digest decision_id evidence_gap_sha256 "
                "evidence_gap_reviewed_by",
            )
            review["invalidation"]["event_id"] = event["id"]
        reserve_response(review)
        reviews.append(review)
    if len(reviews) != len(review_events):
        _refuse(ReadErrorCode.projection_not_converged, field="reviews")
    reviews.sort(key=lambda item: item["causal_order"])
    latest = evidence[-1] if evidence else None
    current_claim = active[0] if active else (claims[-1] if claims else None)
    # New ownership does not silently inherit an earlier generation's evidence.
    current_evidence = (
        latest if latest and current_claim and latest["claim_id"] == current_claim["id"] else None
    )
    current_reviews = [
        review
        for review in reviews
        if current_evidence and review["review_attempt_id"] == current_evidence["id"]
    ]
    bundles = []
    for membership in rows(
        "execution_bundle_members",
        "bundle_id, task_id, position",
        "task_id = ?",
        (stored,),
        order="bundle_id",
    ):
        bundle = _one(
            rows(
                "execution_bundles",
                "id, prd_id, creation_event_id, coordinator, status, "
                "review_disposition_event_id, superseded_by, last_result_at, checkpoint",
                "id = ?",
                (membership["bundle_id"],),
            )
        )
        bundle = _json_columns(bundle, "checkpoint")
        if bundle["checkpoint"] is not None:
            bundle["checkpoint"] = BundleCheckpoint.model_validate(bundle["checkpoint"]).model_dump(
                mode="json"
            )
        bundle["claims"] = rows(
            "bundle_claims",
            "id, bundle_id, claimed_by, status, created_at, "
            "lease_expires_at, last_heartbeat_at, released_at, release_reason",
            "bundle_id = ?",
            (bundle["id"],),
        )
        reserve_response(bundle)
        bundles.append(bundle)
    accepted = [review for review in reviews if review["decision"] == "accepted"]
    timing = project_attempt_timing(
        project_id=project["id"],
        claims=timing_claims,
        events=timing_events,
        evidence=evidence,
        reviews=reviews,
        observation_at=observation_at,
    )
    reserve_response(timing)
    return {
        "identity": {
            "project_id": project["id"],
            "prd_id": scope,
            "task_id": _local_entity_id(stored, scope),
            "stored_task_id": stored,
        },
        "prd": prd,
        "task": _pick(
            task,
            "id feature_id title description status priority task_type acceptance_criteria "
            "verification claims conflict_groups parent_task_id",
        ),
        "requirements": requirements,
        "dependencies": dependencies,
        "current": {
            "claim": current_claim,
            "evidence": current_evidence,
            "reviews": current_reviews,
        },
        "latest_evidence_id": latest["id"] if latest else None,
        "history": {
            "claims": [c for c in claims if c != current_claim],
            "evidence": [e for e in evidence if e != current_evidence],
            "reviews": [r for r in reviews if r not in current_reviews],
            "counts": {"claims": len(claims), "evidence": len(evidence), "reviews": len(reviews)},
            "overflow": False,
        },
        "acceptance": {
            "task_status": task["status"],
            "accepted": task["status"] in {"accepted", "done"},
            "latest_accepted_event_id": accepted[-1]["event_id"] if accepted else None,
            "invalidation": next(
                (r["invalidation"] for r in reversed(reviews) if "invalidation" in r), None
            ),
        },
        "custody": {
            "bundles": bundles,
            "external_owner_state": "unknown",
            "runner_stop": "unknown",
        },
        "source_delivery": [_delivery(b) for b in bundles] if bundles else "unknown",
        "timing": timing,
        "mutation_authority": False,
        "events": events,
    }


def _proof(proof: dict[str, Any]) -> dict[str, Any]:
    safe = _pick(
        proof,
        "kind command exit_code output_sha256 captured_at semantic_digest trust_mode "
        "issuer_id attribution diff_sha256 insertions deletions url label statement attested_by",
    )
    if "evidence_core" in proof:
        safe["evidence_core"] = _pick(
            proof["evidence_core"],
            "schema_version project_id claim_id generation claimed_by task_id task_revision "
            "prd_id prd_revision repository_id claim_start_sha started_at ended_at exit_code "
            "output_sha256",
        )
    safe["duration_status"] = "measured" if "evidence_core" in proof else "unknown"
    return safe
