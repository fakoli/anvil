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
from anvil.web import (
    _DISPATCH_OPERATIONS,
    BundleListRequest,
    ExecutionCapabilities,
    ExecutionOptions,
    ExecutionPreview,
    ExecutionStatus,
    Repository,
    WebError,
    _native_writer,
)

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
        claims = {claim.task_id: claim for claim in backend.list_active_claims()}
        execution = {
            _task_key(task): {"run": (
                task.status == TaskStatus.ready
                or task.status in {TaskStatus.claimed, TaskStatus.in_progress}
                and task.id in claims and claims[task.id].claimed_by == actor
                and bool(claims[task.id].worktree_path)
            )} for task in tasks
        }
        versions = {
            _task_key(task): _version(backend, task) for task in tasks
            if actions[_task_key(task)] or execution[_task_key(task)]["run"]
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
                "execution": execution,
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


_EXECUTION_MESSAGES = {
    "adapter_busy": "The configured execution owner is busy or needs reconciliation.",
    "adapter_changed": "The registered adapter changed. Preview again after reviewing it.",
    "adapter_unavailable": "The configured adapter is unavailable for this task.",
    "adapter_unregistered": "Configure an execution adapter with the native CLI first.",
    "task_not_ready": "Make the task ready or use its current owned claim.",
    "claim_changed": "The current claim does not match this operator or checkout.",
    "insufficient_lease": "Renew the native claim before previewing this execution.",
    "dirty_source": "The selected checkout has uncommitted changes.",
    "source_changed": "The source changed. Inspect it and create a new preview.",
    "scope_changed": "The approved task scope changed. Create a new preview.",
    "prd_unapproved": "The current task scope needs approval before execution.",
    "dependency_not_done": "Required dependencies have not completed.",
    "unsupported_bundle": "This task belongs to a coordinator-owned batch.",
    "unsupported_options": "The selected model or thinking option is unavailable.",
    "invalid_capabilities": "Verified model information is unavailable. Refresh it explicitly.",
    "continuation_unsupported": "The registered adapter does not support saved Continue.",
    "ephemeral_run": "This run has no saved conversation to continue.",
    "unsettled_run": "The previous run must settle before Continue can be previewed.",
    "continuation_lineage": "The saved run does not match the current task history or source.",
    "preview_expired": "The native preview expired. No execution was retried.",
    "preview_consumed": "This preview was already consumed. Inspect its existing execution.",
    "preview_mismatch": "The retained preview does not match this execution route.",
    "invalid_receipt": "Execution evidence is unavailable or incomplete; the outcome is unknown.",
}


def _execution_identity(repository: Repository) -> None:
    if not _native_writer(repository):
        raise WebError("read_only_repository", "Execution needs a compatible native writer.")
    with _read_backend(repository):
        pass


def _execution_checkout(
    repository: Repository, actor: str, request: dict[str, Any],
) -> tuple[Path, str]:
    with _read_backend(repository) as (backend, _, __):
        task_id = request["task_id"]
        if request["prd_id"] != "default":
            task_id = request["prd_id"] + ":" + task_id
        task = backend.get_task(task_id)
        if task is None or task.prd_id != request["prd_id"]:
            raise WebError("task_not_found", "Task does not belong to the selected campaign.")
        if _version(backend, task) != request["task_version"]:
            raise WebError("stale_preview", "This task changed. Refresh before previewing.")
        claims = [claim for claim in backend.list_active_claims() if claim.task_id == task_id]
        checkout = Path(repository.root)
        if claims:
            if len(claims) != 1 or claims[0].claimed_by != actor or not claims[0].worktree_path:
                raise WebError("claim_changed", _EXECUTION_MESSAGES["claim_changed"])
            checkout = Path(claims[0].worktree_path).resolve()
    # The claim is the only source of a non-enrollment checkout. Confirm its
    # State belongs to this enrollment before native source/claim admission.
    if checkout != Path(repository.root).resolve():
        with _read_backend(repository.model_copy(update={"root": str(checkout)})):
            pass
    return checkout, task_id


def _verified_options(cwd: Path, options: dict[str, Any]) -> dict[str, str]:
    from anvil.execution import RunOptions, read_execution_capabilities

    selected = RunOptions(**options).data()
    if not selected:
        return selected
    capabilities = read_execution_capabilities(cwd)
    if capabilities["status"] != "fresh":
        raise WebError("invalid_capabilities", _EXECUTION_MESSAGES["invalid_capabilities"])
    if (not selected.get("provider")
            or {"provider": selected["provider"], "id": selected["model"]}
            not in capabilities["models"]):
        raise WebError("unsupported_options", _EXECUTION_MESSAGES["unsupported_options"])
    if "thinking" in selected:
        thinking = capabilities.get("thinking", {})
        if (thinking.get("provider") != selected["provider"]
                or thinking.get("model") != selected["model"]
                or selected["thinking"] not in thinking.get("levels", [])):
            raise WebError("unsupported_options", _EXECUTION_MESSAGES["unsupported_options"])
    return selected


def _execution_status(
    repository: Repository, actor: str, binding: dict[str, Any],
) -> dict[str, Any]:
    from anvil.execution import inspect_execution_preview, inspect_run

    operation = binding["operation"]
    status = {
        "operation": operation, "state": "unknown", "dispatch_state": "unknown",
        "continuation": {"state": "unavailable", "reason": "No verified saved return."},
    }
    run_id = binding.get("run_id")
    if binding.get("native_preview_id"):
        observed = inspect_execution_preview(
            Path(binding["cwd"]), binding["native_preview_id"], actor,
        )
        if observed["operation"] != operation or observed["task_id"] != binding["task_id"]:
            raise WebError("preview_mismatch", _EXECUTION_MESSAGES["preview_mismatch"])
        status.update(dispatch_state=observed["state"], task_id=binding["task_id"])
        run_id = observed["run_id"]
        if observed["state"] == "unconsumed":
            status["state"] = "unconsumed"
    elif operation == "metadata" and not binding.get("confirmation_attempted"):
        status.update(state="unconsumed", dispatch_state="unconsumed")
    if run_id is not None:
        result = inspect_run(Path(binding["cwd"]), run_id)
        if result.get("prior_run_id") is not None:
            status["operation"] = "continue"
        # Known native CLI IDs share the enrolled project's status authority;
        # actor attribution is not a separate authentication mechanism.
        with _read_backend(repository) as (backend, _, __):
            if operation != "metadata" and backend.get_task(result["task_id"]) is None:
                raise WebError("task_not_found", "The run is not a task in this project.")
        allowed_states = {
            "dispatched", "running_or_interrupted", "reconciliation_required",
            "needs_review", "failed_stopped", "capabilities_ready",
        }
        status.update(
            run_id=run_id, dispatch_state="consumed",
            state=result["state"] if result["state"] in allowed_states else "unknown",
            task_id=None if operation == "metadata" else result["task_id"],
        )
        if type(result.get("local_children_stopped")) is bool:
            status["local_children_stopped"] = result["local_children_stopped"]
        continuation = result.get("continuation", {})
        if continuation.get("state") == "requires_preview":
            status["continuation"] = {
                "state": "requires_preview", "prior_run_id": run_id,
                "reason": "A fresh eligible task, source and claim must pass native preview.",
            }
        elif continuation.get("state") == "unknown":
            status["continuation"] = {"state": "unknown", "reason": "Saved return is incomplete."}
    return ExecutionStatus.model_validate(status).model_dump(exclude_none=True)


def execution_action(
    repository: Repository, actor: str, operation: str, request: dict[str, Any] | None,
) -> dict[str, Any]:
    from anvil.execution import (
        ExecutionError,
        RunOptions,
        confirm_continue,
        confirm_run,
        preview_continue,
        preview_run,
        read_execution_capabilities,
        refresh_execution_capabilities,
    )

    _execution_identity(repository)
    request = request or {}
    try:
        if operation == "execution_capabilities":
            native = read_execution_capabilities(Path(repository.root))
            selected = {key: native[key] for key in (
                "status", "models", "thinking", "effective", "observed_at",
            ) if key in native}
            return ExecutionCapabilities.model_validate(selected).model_dump(exclude_none=True)
        if operation == "execution_status":
            return _execution_status(repository, actor, request["binding"])
        if operation == "execution_inspect":
            binding = {"operation": "run", "run_id": request["run_id"], "cwd": repository.root}
            return {**_execution_status(repository, actor, binding), "binding": binding}
        if operation == "metadata_preview":
            selected = RunOptions(**request).data()
            capabilities = read_execution_capabilities(Path(repository.root))
            return {
                **ExecutionPreview(
                    operation="metadata", title="Refresh model information",
                    details=["Read verified model capabilities without sending a task prompt.",
                             "Selected model: "
                             + (" / ".join(selected.values()) or "inherited configuration")],
                    options=ExecutionOptions(**selected),
                ).model_dump(exclude_none=True),
                "binding": {
                    "kind": "execution_metadata", "operation": "metadata", "cwd": repository.root,
                    "options": selected, "registration_sha256": capabilities["registration_sha256"],
                },
            }
        if operation == "execution_preview":
            cwd, task_id = _execution_checkout(repository, actor, request)
            options = _verified_options(cwd, request.get("options", {}))
            prior_run_id = None
            if request["operation"] == "continue":
                prior = _execution_status(repository, actor, request["prior_binding"])
                if prior["continuation"]["state"] != "requires_preview":
                    raise WebError("unsettled_run", _EXECUTION_MESSAGES["unsettled_run"])
                prior_run_id = prior["run_id"]
                native = preview_continue(cwd, prior_run_id, actor, RunOptions(**options),
                                          request["wall_seconds"], task_id=task_id)
            else:
                native = preview_run(
                    cwd, task_id, actor, RunOptions(**options), request["wall_seconds"],
                )
            return {
                **ExecutionPreview(
                    operation=request["operation"],
                    title="Continue saved run" if prior_run_id else "Run task",
                    details=[f"Task: {task_id}", f"Operator: {actor}",
                             *([f"Prior run: {prior_run_id}"] if prior_run_id else []),
                             f"Source: {native['source_sha']}",
                             f"Budget: {native['wall_seconds']} seconds",
                             "Model options: "
                             + (json.dumps(options) if options else "inherited configuration"),
                             "Successful proof submission awaits review; "
                             "no acceptance is granted."],
                    options=ExecutionOptions(**options),
                ).model_dump(exclude_none=True),
                "binding": {
                    "kind": "execution_" + request["operation"], "operation": request["operation"],
                    "native_preview_id": native["preview_id"], "cwd": str(cwd), "task_id": task_id,
                    "prior_run_id": prior_run_id, "options": options,
                },
            }
        binding = request["binding"]
        cwd = Path(binding["cwd"])
        if operation == "metadata_confirm":
            native = refresh_execution_capabilities(
                cwd, **binding["options"],
                expected_registration_sha256=binding["registration_sha256"],
            )
        elif operation == "execution_confirm":
            _verified_options(cwd, binding["options"])
            if binding["operation"] == "continue":
                native = confirm_continue(
                    cwd, binding["prior_run_id"], actor, binding["native_preview_id"],
                )
            else:
                native = confirm_run(cwd, binding["task_id"], actor, binding["native_preview_id"])
        else:
            raise WebError("invalid_action", "Unknown execution operation.")
        return ExecutionStatus(
            operation=binding["operation"], state="dispatched", dispatch_state="consumed",
            run_id=native["run_id"], task_id=binding.get("task_id"),
            continuation={"state": "unavailable", "reason": "Execution has not returned."},
        ).model_dump(exclude_none=True)
    except (ExecutionError, EventRejected) as error:
        code = getattr(error, "code", "execution_refused")
        code = code if code in _EXECUTION_MESSAGES else "execution_refused"
        raise WebError(code, _EXECUTION_MESSAGES.get(
            code, "Native execution refused this request. Inspect it with the CLI.",
        )) from None


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
        elif operation.startswith("execution_") or operation in {
            "metadata_preview", "metadata_confirm",
        }:
            result = execution_action(repository, actor, operation, request.get("request"))
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
                        "code": (
                            "outcome_unknown"
                            if operation in _DISPATCH_OPERATIONS else "action_failed"
                        ),
                        "message": "The result could not be confirmed. Refresh and inspect "
                        "the task before trying another change.",
                    },
                }
            )
        )


if __name__ == "__main__":
    main()
