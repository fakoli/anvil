"""Read-only, bounded dispatch advice over existing bundle authorizations.

This is not a scheduler or evidence verifier. A coordinator remains responsible
for starting each delegate once and submitting its claim-bound evidence.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from anvil.planning.inference import BundlePlanningError, _canonical_project_path
from anvil.state.backend import Backend
from anvil.state.models import PRD, BundleStatus, ClaimStatus, ExecutionBundle, Task, TaskStatus


class BundleExecutionPlanError(ValueError):
    """The requested bounded projection cannot be produced."""


def _prd_lineage(prd: PRD | None) -> dict[str, Any] | None:
    if prd is None:
        return None
    return {
        name: getattr(prd, name)
        for name in (
            "id", "status", "revision", "source_revision", "source_sha256",
            "material_sha256", "content_event_id", "review_event_id",
            "lifecycle_revision", "lifecycle_source_sha256", "lifecycle_material_sha256",
            "lifecycle_content_event_id", "provenance_state", "content_available",
        )
    }


def _scope(
    backend: Backend, bundle_id: str,
) -> tuple[str, ExecutionBundle, PRD | None, list[Task | None]]:
    project = backend.get_project()
    if project is None:
        raise BundleExecutionPlanError("Project identity is unavailable.")
    bundle = backend.get_bundle(bundle_id)
    if bundle is None:
        raise BundleExecutionPlanError(f"Bundle '{bundle_id}' not found.")
    return (
        project.id, bundle, backend.get_prd(bundle.prd_id),
        [backend.get_task(task_id) for task_id in bundle.task_ids],
    )


def _scope_digest(
    project_id: str, bundle: ExecutionBundle, prd: PRD | None, tasks: list[Task | None],
) -> str:
    # Bind selected material, not the global event cursor or unrelated work.
    material = {
        "contract": "anvil.bundle-execution-scope.v1",
        "project_id": project_id,
        "bundle": bundle.model_dump(mode="json", exclude={
            "delegated_agents", "checkpoint", "last_result_at", "updated_at",
        }),
        "prd": _prd_lineage(prd),
        "members": [task.model_dump(mode="json") if task else None for task in tasks],
    }
    return hashlib.sha256(json.dumps(
        material, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False,
    ).encode("utf-8")).hexdigest()


def bundle_execution_scope_digest(backend: Backend, bundle_id: str) -> str:
    """Digest exact bundle/PRD/member scope in the caller's current snapshot."""
    return _scope_digest(*_scope(backend, bundle_id))


def build_bundle_execution_plan(
    backend: Backend, bundle_id: str, *, max_workers: int = 8, now: datetime | None = None,
) -> dict[str, Any]:
    """Return deterministic dispatch advice without mutating or verifying proof.

    The caller owns snapshot consistency. Active in-progress members consume
    capacity and reserve declared paths/groups. Pending review does not consume
    a worker, but dependencies still require the native ``done`` disposition.
    """
    if (
        isinstance(max_workers, bool) or not isinstance(max_workers, int)
        or not 1 <= max_workers <= 500
    ):
        raise BundleExecutionPlanError("max_workers must be an integer from 1 to 500.")
    current_time = now or datetime.now(UTC)
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        raise BundleExecutionPlanError("now must be timezone-aware.")
    project_id, bundle, prd, tasks = _scope(backend, bundle_id)
    coordinator_claim = backend.get_bundle_claim(bundle_id)
    blockers: list[str] = []
    member_ids = set(bundle.task_ids)
    independent = not any(
        dependency in member_ids
        for task in tasks if task is not None for dependency in task.dependencies
    )
    if not independent:
        blockers.append("internal_dependencies_require_sequential_bundle_flow")
    if bundle.status is not BundleStatus.active:
        blockers.append(f"bundle_status:{bundle.status.value}")
    if prd is None or prd.status.value != "approved":
        blockers.append("prd_not_approved")
    if coordinator_claim is None:
        blockers.append("coordinator_claim_missing")
    elif coordinator_claim.status is not ClaimStatus.active:
        blockers.append(f"coordinator_claim_status:{coordinator_claim.status.value}")
    elif coordinator_claim.lease_expires_at <= current_time:
        blockers.append("coordinator_lease_expired")
    elif coordinator_claim.claimed_by != bundle.coordinator:
        blockers.append("coordinator_identity_mismatch")

    task_lookup = {task.id: task for task in tasks if task is not None}
    members: list[dict[str, Any]] = []
    scopes: dict[str, tuple[set[str], set[str]]] = {}
    for task_id, task in zip(bundle.task_ids, tasks, strict=True):
        member_blockers: list[str] = []
        claim_id = coordinator_claim.member_claim_ids.get(task_id) if coordinator_claim else None
        claim = backend.get_claim(claim_id) if claim_id else None
        if task is None:
            member_blockers.append("task_missing")
        else:
            try:
                # Conservative case folding prevents cross-platform aliases
                # from entering the same wave. No filesystem probes here.
                reserved_paths = [*task.likely_files, *(claim.expected_files if claim else [])]
                paths = {_canonical_project_path(path).casefold() for path in reserved_paths}
                scopes[task_id] = (paths, set(task.conflict_groups))
            except BundlePlanningError:
                member_blockers.append("invalid_file_scope")
            if task.status not in {TaskStatus.claimed, TaskStatus.in_progress}:
                member_blockers.append(f"task_status:{task.status.value}")
            for dependency_id in task.dependencies:
                if dependency_id not in task_lookup:
                    dependency = backend.get_task(dependency_id)
                    if dependency is not None:
                        task_lookup[dependency_id] = dependency
                dependency = task_lookup.get(dependency_id)
                if dependency is None or dependency.status is not TaskStatus.done:
                    member_blockers.append(f"dependency_not_done:{dependency_id}")
        if claim is None:
            member_blockers.append("member_claim_missing")
        elif (
            coordinator_claim is None or claim.bundle_claim_id != coordinator_claim.id
            or claim.task_id != task_id or claim.claimed_by != bundle.coordinator
        ):
            member_blockers.append("member_claim_identity_mismatch")
        elif claim.status is not ClaimStatus.active:
            member_blockers.append(f"member_claim_status:{claim.status.value}")
        elif claim.lease_expires_at <= current_time:
            member_blockers.append("member_lease_expired")
        members.append({
            "task_id": task_id,
            "title": task.title if task else None,
            "status": task.status.value if task else None,
            "claim_id": claim_id,
            "claim_generation": claim.generation if claim else None,
            "claim_status": claim.status.value if claim else None,
            "lease_expires_at": claim.lease_expires_at.isoformat() if claim else None,
            "blockers": member_blockers,
        })

    running = [member for member in members if member["status"] == "in_progress"]
    reserved_files: set[str] = set()
    reserved_groups: set[str] = set()
    for member in running:
        paths, groups = scopes.get(member["task_id"], (set(), set()))
        reserved_files.update(paths)
        reserved_groups.update(groups)
    # Unknown scope on an existing worker cannot safely admit another worker.
    if any(member["blockers"] for member in running):
        blockers.append("running_member_requires_attention")
    wave: list[str] = []
    for member in members:
        if member["status"] != "claimed" or member["blockers"]:
            continue
        paths, groups = scopes[member["task_id"]]
        if paths & reserved_files or groups & reserved_groups:
            member["blockers"].append("conflicts_with_running_or_selected_member")
        elif len(wave) + len(running) >= max_workers:
            member["blockers"].append("worker_limit")
        elif not blockers:
            wave.append(member["task_id"])
            reserved_files.update(paths)
            reserved_groups.update(groups)

    attention_statuses = {"blocked", "rejected", "drafted", "closed"}
    return {
        "contract_version": 1,
        "project_id": project_id,
        "bundle_id": bundle.id,
        "creation_event_id": bundle.creation_event_id,
        "scope_digest": _scope_digest(project_id, bundle, prd, tasks),
        "fanout_eligible": independent,
        "status": bundle.status.value,
        "prd": _prd_lineage(prd),
        "coordinator": {
            "actor": bundle.coordinator,
            "claim_id": coordinator_claim.id if coordinator_claim else None,
            "claim_status": coordinator_claim.status.value if coordinator_claim else None,
            "expected_scope_digest": (
                coordinator_claim.expected_scope_digest if coordinator_claim else None
            ),
            "lease_expires_at": (
                coordinator_claim.lease_expires_at.isoformat() if coordinator_claim else None
            ),
        },
        "max_workers": max_workers,
        "members": members,
        "blockers": blockers,
        "wave": wave,
        "counts": {
            "total": len(members), "runnable": len(wave), "running": len(running),
            "pending_review": sum(member["status"] == "needs_review" for member in members),
            "completed": sum(member["status"] in {"accepted", "done"} for member in members),
            "attention_required": sum(
                member["status"] in attention_statuses or (
                    member["status"] in {"claimed", "in_progress"}
                    and bool(member["blockers"])
                    and member["blockers"] != ["worker_limit"]
                ) for member in members
            ),
        },
        "advisory": True,
        "proof_verification": "deferred_to_native_submit_and_review",
    }
