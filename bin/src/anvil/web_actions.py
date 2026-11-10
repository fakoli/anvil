"""Installed, isolated worker for human web actions; never execute reader argv as a writer."""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from typing import Any

from anvil.cli._helpers import _open_backend, _resolve_state_dir
from anvil.clock import SystemClock
from anvil.project_snapshot import (
    _local_entity_id,
    captured_snapshot_transaction,
    snapshot_from_transaction,
)
from anvil.state.backend import EventRejected
from anvil.state.models import Task, TaskStatus
from anvil.state.sqlite import SqliteBackend, query_only_transaction
from anvil.task_actions import available_task_actions, transition_task
from anvil.web import BundleListRequest, Repository, WebError

ACTION_LABELS = {
    "ready": ("Make ready", "ready"),
    "backlog": ("Return to backlog", "backlog"),
    "start": ("Start claimed work", "active"),
    "block": ("Mark blocked", "blocked"),
    "resume": ("Resume work", "active"),
    "approve": ("Approve and close", "closed"),
    "reject": ("Request changes", "backlog"),
    "close": ("Close without completion", "closed"),
}
RESOLUTIONS = [
    {"id": "duplicate", "label": "Duplicate"},
    {"id": "cancelled", "label": "Cancelled"},
    {"id": "wont_do", "label": "Won’t do"},
    {"id": "obsolete", "label": "Obsolete"},
    {"id": "cannot_reproduce", "label": "Cannot reproduce"},
]


@contextmanager
def _read_backend(
    repository: Repository,
    *, snapshot: bool = False,
) -> Iterator[tuple[SqliteBackend, sqlite3.Connection, Any]]:
    state_dir = _resolve_state_dir(Path(repository.root))
    transaction = (
        captured_snapshot_transaction(state_dir) if snapshot else
        query_only_transaction(state_dir / "state.db", state_dir / "events.jsonl")
    )
    with transaction as (
        connection,
        events_file,
    ):
        backend = SqliteBackend(
            db_path=str(state_dir / "state.db"),
            events_path=str(state_dir / "events.jsonl"),
            clock=SystemClock(),
            project_root=repository.root,
        )
        # Reuse typed query methods on the existing read-only transaction. No
        # initialize(), migrations, stale-claim reaping or second connection.
        backend._conn = connection
        try:
            project = backend.get_project()
            if project is None or project.id != repository.project_id:
                raise WebError("project_identity_changed", "Repository identity changed.")
            yield backend, connection, events_file
        finally:
            backend._conn = None


def _task_key(task: Task) -> str:
    return f"{task.prd_id}:{_local_entity_id(task.id, task.prd_id)}"


def _version(backend: SqliteBackend, task: Task) -> str:
    evidence = (
        backend.get_latest_evidence(task.id) if task.status == TaskStatus.needs_review else None
    )
    value = {"task": task.model_dump(mode="json"), "evidence": evidence.id if evidence else None}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _actions(backend: SqliteBackend, task: Task, actor: str) -> list[dict[str, Any]]:
    actions = available_task_actions(backend, task, actor)
    if task.status == TaskStatus.needs_review:
        # These remain proposals: the existing apply command enforces the
        # evidence, review-attempt, bundle and configured merge gates on apply.
        if backend.bundle_member_acceptance_blocker(task.id) is None:
            actions.append("approve")
        actions.append("reject")
    return [
        {
            "id": action,
            "label": ACTION_LABELS[action][0],
            "column": ACTION_LABELS[action][1],
            "requires_reason": action in {"block", "reject", "close"},
        }
        for action in actions
    ]


def inspect_repository(repository: Repository, actor: str) -> dict[str, Any]:
    from anvil.cli.describe import API_VERSION
    from anvil.state.schema import get_schema_version

    with _read_backend(repository, snapshot=True) as (backend, connection, events_file):
        snapshot = snapshot_from_transaction(connection, events_file).model_dump(mode="json")
        snapshot.update(
            operation_id="state.project.snapshot",
            operation_version=1,
            output_schema_id="anvil.state.project-snapshot.v1",
            api_version=API_VERSION,
            schema_version=get_schema_version(),
            digest_algorithm="sha256",
            truncated=False,
        )
        tasks = backend.list_tasks()
        actions = {_task_key(task): _actions(backend, task, actor) for task in tasks}
        versions = {
            _task_key(task): _version(backend, task) for task in tasks if actions[_task_key(task)]
        }
        closures = {
            _task_key(task): task.closure.model_dump(mode="json")
            for task in tasks
            if task.closure is not None
        }
        return {
            "snapshot": snapshot,
            "workflow": {
                "enabled": True,
                "reason": None,
                "actions": actions,
                "versions": versions,
                "closures": closures,
                "review_blockers": {
                    _task_key(task): blocker for task in tasks
                    if task.status == TaskStatus.needs_review
                    and (blocker := backend.bundle_member_acceptance_blocker(task.id))
                },
            },
        }


def preview_action(repository: Repository, actor: str, request: dict[str, Any]) -> dict[str, Any]:
    with _read_backend(repository) as (backend, _, __):
        task_id = request["task_id"]
        prd_id = request["prd_id"]
        stored_id = task_id if prd_id == "default" else f"{prd_id}:{task_id}"
        task = backend.get_task(stored_id)
        if task is None or task.prd_id != prd_id:
            raise WebError("task_not_found", "Task does not belong to the selected campaign.")
        if _version(backend, task) != request["task_version"]:
            raise WebError("stale_preview", "This task changed. Refresh the board and try again.")
        action = request["action"]
        if action not in {item["id"] for item in _actions(backend, task, actor)}:
            raise WebError(
                "transition_refused", "This move is unavailable in the task’s current state."
            )
        evidence = backend.get_latest_evidence(task.id)
        details = [
            f"{task.prd_id} / {task_id}: {task.title}",
            f"Current status: {task.status.value}",
            f"Recorded actor: {actor}",
        ]
        if action == "approve":
            if evidence is None:
                raise WebError("evidence_missing", "Submit completion evidence before approval.")
            from anvil.review.gates import evidence_complete

            complete, missing = evidence_complete(task, evidence)
            if not complete:
                raise WebError(
                    "evidence_incomplete", "Required evidence missing: " + ", ".join(missing)
                )
            details += [
                f"Evidence: {evidence.id}",
                f"Submitted by: {evidence.submitted_by}",
                f"Commit: {evidence.commit_sha or 'not recorded'}",
                "Commands: " + "; ".join(evidence.commands_run),
                "Required evidence is present. Engine checks run again on confirmation.",
                "Approval records final acceptance. Confirm the required independent reviews "
                "and your project validation are complete.",
            ]
            if evidence.known_limitations:
                details.append("Known limitations: " + evidence.known_limitations)
        if action == "close":
            details.append(
                "This closes the task without claiming successful completion. "
                "Dependent tasks remain blocked until their dependencies are resolved."
            )
        return {
            "title": ACTION_LABELS[action][0],
            "details": [item[:8192] for item in details],
            "requires_reason": action in {"block", "reject", "close"},
            "resolutions": RESOLUTIONS if action == "close" else [],
            "requires_confirmation": True,
            "binding": {
                "kind": "task_transition",
                "task_id": task.id,
                "prd_id": task.prd_id,
                "action": action,
                "expected_status": task.status.value,
                "expected_updated_at": task.updated_at.isoformat(),
                "expected_evidence_id": evidence.id if evidence else None,
                "task_version": request["task_version"],
            },
        }


def execute_action(repository: Repository, actor: str, request: dict[str, Any]) -> dict[str, Any]:
    binding = request["binding"]
    if binding.get("kind") == "bundle_claim":
        return execute_bundle_claim(repository, actor, binding)
    # Only the selected task is compared: independent tasks can advance while a
    # human reviews this preview. The reducer repeats CAS under its writer lock.
    with _read_backend(repository) as (backend, _, __):
        task = backend.get_task(binding["task_id"])
        if (
            task is None
            or task.prd_id != binding["prd_id"]
            or _version(backend, task) != binding["task_version"]
        ):
            raise WebError("stale_preview", "This task or its evidence changed. Preview it again.")
        if binding["action"] not in {item["id"] for item in _actions(backend, task, actor)}:
            raise WebError("transition_refused", "This move is no longer available.")
    action = binding["action"]
    reason = request.get("reason")
    if action in {"block", "reject", "close"} and not (reason or "").strip():
        raise WebError("reason_required", "Enter a reason for this change.")
    if action in {"approve", "reject"}:
        from anvil.cli import app

        if binding["expected_evidence_id"] is None:
            raise WebError(
                "evidence_missing", "No submitted evidence is available for this review."
            )
        arguments = [
            "apply",
            binding["task_id"],
            "--" + action,
            "--strict",
            "--reviewer",
            actor,
            "--expected-evidence-id",
            binding["expected_evidence_id"],
            "--expected-task-updated-at",
            binding["expected_updated_at"],
            "--json",
            "--cwd",
            repository.root,
        ]
        if reason:
            arguments += ["--reason", reason]
        output = io.StringIO()
        with redirect_stdout(output):
            app(args=arguments, standalone_mode=False)
        response = json.loads(output.getvalue())
        if response.get("ok") is not True:
            error = response.get("error", {})
            raise WebError(
                error.get("code", "review_refused"), error.get("message", "Review refused.")
            )
    else:
        duplicate_of = request.get("duplicate_of")
        if duplicate_of:
            if ":" not in duplicate_of and binding["prd_id"] != "default":
                duplicate_of = f"{binding['prd_id']}:{duplicate_of}"
            elif duplicate_of.startswith("default:"):
                duplicate_of = duplicate_of.removeprefix("default:")
        state_dir = _resolve_state_dir(Path(repository.root))
        backend = _open_backend(state_dir, project_root=Path(repository.root))
        try:
            transition_task(
                backend,
                binding["task_id"],
                action,
                actor,
                expected_status=binding["expected_status"],
                expected_updated_at=binding["expected_updated_at"],
                reason=reason,
                resolution=request.get("resolution"),
                duplicate_of=duplicate_of,
            )
        finally:
            backend.close()
    return {"ok": True}


def inspect_bundles(
    repository: Repository, actor: str, request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from anvil.bundles.execution import build_bundle_execution_plan

    selection = BundleListRequest.model_validate(request or {})
    # Immutable IDs keep earlier status changes from shifting later pages.
    # Each page is fresh advice; refresh from the start to see earlier additions.
    with _read_backend(repository, snapshot=True) as (backend, connection, _):
        conditions = [
            "status IN ('planned', 'active', 'implemented_unreviewed', "
            "'reviewed_unintegrated', 'integrated', 'replan_required')",
        ]
        parameters = []
        if selection.prd_id is not None:
            if backend.get_prd(selection.prd_id) is None:
                raise WebError("invalid_batch_query", "Campaign is not in this repository.")
            conditions.append("prd_id = ?")
            parameters.append(selection.prd_id)
        if selection.after_bundle_id is not None:
            conditions.append("id > ?")
            parameters.append(selection.after_bundle_id)
        rows = connection.execute(
            "SELECT id FROM execution_bundles WHERE " + " AND ".join(conditions)
            + " ORDER BY id LIMIT 51", parameters,
        ).fetchall()
        return {
            "plans": [build_bundle_execution_plan(backend, row[0]) for row in rows[:50]],
            "truncated": len(rows) > 50,
            "actor": actor,
            "repository_id": repository.id,
            "project_id": repository.project_id,
            "prd_id": selection.prd_id,
            "pagination": {
                "after_bundle_id": selection.after_bundle_id,
                "next_after_bundle_id": rows[49][0] if len(rows) > 50 else None,
                "page_size": 50,
            },
        }


def preview_bundle_claim(
    repository: Repository, actor: str, request: dict[str, Any],
) -> dict[str, Any]:
    from anvil.bundles.execution import build_bundle_execution_plan

    with _read_backend(repository) as (backend, _, __):
        bundle = backend.get_bundle(request["bundle_id"])
        if bundle is None or bundle.status.value != "planned":
            raise WebError("bundle_unavailable", "Only a planned batch can be claimed.")
        if bundle.coordinator != actor:
            raise WebError("coordinator_mismatch", "This session is not the batch coordinator.")
        plan = build_bundle_execution_plan(backend, bundle.id)
        return {
            "title": "Claim execution batch",
            "details": [
                f"Batch: {bundle.id} · Campaign: {bundle.prd_id}",
                f"Coordinator: {actor} · Members: {len(bundle.task_ids)}",
                *[f"{member['task_id']}: {member['title']} ({member['status']})"
                  for member in plan["members"]],
                "Claims the exact member scope under one coordinator lease. "
                "Native source, dependency, conflict and isolation checks run on confirmation.",
                "Workers still submit individual evidence. This does not start workers "
                "or approve completion; pending review does not pause independent work.",
            ],
            "requires_reason": False,
            "resolutions": [],
            "requires_confirmation": True,
            "binding": {
                "kind": "bundle_claim", "bundle_id": bundle.id,
                "expected_scope_digest": plan["scope_digest"],
            },
        }


def execute_bundle_claim(
    repository: Repository, actor: str, binding: dict[str, Any],
) -> dict[str, Any]:
    from anvil.cli import app

    with _read_backend(repository):
        pass  # Recheck enrollment identity; scope CAS binds it again atomically.
    output = io.StringIO()
    # The existing CLI owns root locks, source/profile validation and claim
    # creation. Never carry a separate read lock across this call.
    with redirect_stdout(output):
        app(args=[
            "bundle", "claim", binding["bundle_id"], "--actor", actor,
            "--expected-scope-digest", binding["expected_scope_digest"],
            "--json", "--cwd", repository.root,
        ], standalone_mode=False)
    response = json.loads(output.getvalue())
    if response.get("ok") is not True:
        error = response.get("error", {})
        raise WebError(error.get("code", "claim_refused"), error.get("message", "Claim refused."))
    return {"ok": True, "bundle_id": binding["bundle_id"]}


def main() -> None:
    operation = ""
    try:
        operation, encoded = sys.argv[1:]
        request = json.loads(encoded)
        repository = Repository.model_validate(request["repository"])
        actor = request["actor"]
        if not isinstance(actor, str) or not actor.strip() or len(actor) > 256:
            raise WebError("invalid_actor", "A configured operator identity is required.")
        if operation == "snapshot":
            result = inspect_repository(repository, actor)
        elif operation == "preview":
            result = preview_action(repository, actor, request["request"])
        elif operation == "bundles":
            result = inspect_bundles(repository, actor, request.get("request"))
        elif operation == "bundle_preview":
            result = preview_bundle_claim(repository, actor, request["request"])
        elif operation == "execute":
            result = execute_action(repository, actor, request["request"])
        else:
            raise WebError("invalid_action", "Unknown action operation.")
        print(json.dumps({"ok": True, "data": result}))
    except (WebError, EventRejected) as error:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": getattr(error, "code", "transition_refused"),
                        "message": str(error)[:8192],
                    },
                }
            )
        )
    except Exception:
        # Don't leak local paths/configuration/tracebacks into the browser.
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "outcome_unknown" if operation == "execute" else "action_failed",
                        "message": "The result could not be confirmed. Refresh and inspect "
                        "the task before trying another change.",
                    },
                }
            )
        )


if __name__ == "__main__":
    main()
