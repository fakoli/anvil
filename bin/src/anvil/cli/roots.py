# ruff: noqa: E501
"""CLI-only owner-root enrollment and coordinated claim surface."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

import typer

from anvil.cli._helpers import (
    _lease_manager_kwargs,
    _load_config_optional,
    _open_backend,
    _require_state_dir,
    _resolve_project_dir,
    _resolve_state_dir,
    resolve_actor,
)
from anvil.cli._json import JSON_OPTION, emit_success, fail
from anvil.roots.registry import (
    RootSetError,
    RootSetRegistry,
    _require_id,
    _validate_commands,
    authorize_root_set_claim,
    live_repository_identity,
    request_digest,
)

roots_app = typer.Typer(help="Enroll owner repositories and coordinate immutable root-set claims.")
_COMMAND = "roots"


def _root_fail(command: str, error: RootSetError, json_output: bool) -> None:
    if json_output:
        fail(command, str(error), code=error.code)
    typer.echo(f"Error: {error}", err=True)
    raise typer.Exit(code=1)


def _load_request(request_file: Path) -> dict[str, Any]:
    try:
        descriptor = os.open(
            request_file,
            os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise RootSetError("root_set_request_collision", "root-set request file is unavailable.") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 65_536:
            raise RootSetError("root_set_request_collision", "root-set request must be a bounded regular file.")
        raw = bytearray()
        while len(raw) <= 65_536:
            chunk = os.read(descriptor, min(65_537 - len(raw), 65_536))
            if not chunk:
                break
            raw.extend(chunk)
    finally:
        os.close(descriptor)
    if len(raw) > 65_536:
        raise RootSetError("root_set_request_collision", "root-set request exceeds 65536 bytes.")
    try:
        value = json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RootSetError("root_set_request_collision", "root-set request must be valid UTF-8 JSON.") from exc
    if not isinstance(value, dict) or set(value) != {"schema", "request_id", "primary_root_id", "roots"} or value.get("schema") != "anvil.root-set-request/v1":
        raise RootSetError("root_set_request_collision", "root-set request has an unsupported schema.")
    try:
        _require_id(value["request_id"], "request id")
        _require_id(value["primary_root_id"], "primary root id")
    except RootSetError as exc:
        raise RootSetError("root_set_request_collision", "root-set request identity is invalid.") from exc
    roots = value["roots"]
    if not isinstance(roots, list) or not 1 <= len(roots) <= 16:
        raise RootSetError("root_set_request_collision", "root-set request must contain 1 through 16 roots.")
    result: list[dict[str, Any]] = []
    ids: set[str] = set()
    repos: set[str] = set()
    for item in roots:
        if not isinstance(item, dict) or set(item) != {"root_id", "repository_id", "path", "expected_files", "verification_commands"}:
            raise RootSetError("root_set_request_collision", "root-set root declaration is invalid.")
        root_id, repository_id = item.get("root_id"), item.get("repository_id")
        try:
            _require_id(root_id, "root id")
            _require_id(repository_id, "repository id")
        except RootSetError as exc:
            raise RootSetError("root_set_request_collision", "root-set root identities must be unique and bounded.") from exc
        if root_id in ids or repository_id in repos:
            raise RootSetError("root_set_request_collision", "root-set root identities must be unique and bounded.")
        if not isinstance(item["path"], str) or not Path(item["path"]).is_absolute() or any(ord(ch) < 32 for ch in item["path"]):
            raise RootSetError("root_set_request_collision", "root-set root path is invalid.")
        files = item["expected_files"]
        if not isinstance(files, list) or len(files) > 256 or any(not isinstance(path, str) or not path or path.startswith("/") or ".." in Path(path).parts or any(ord(ch) < 32 for ch in path) for path in files):
            raise RootSetError("root_set_request_collision", "root-set expected files are invalid.")
        result.append({"root_id": root_id, "repository_id": repository_id, "path": item["path"], "expected_files": files, "verification_commands": _validate_commands(item["verification_commands"])})
        ids.add(root_id)
        repos.add(repository_id)
    if value["primary_root_id"] not in ids:
        raise RootSetError("root_set_request_collision", "primary root must be a declared root.")
    return {"schema": value["schema"], "request_id": value["request_id"], "primary_root_id": value["primary_root_id"], "roots": result}


def _request_authority(request: dict[str, Any], backend, registry: dict[str, Any], project_root: Path) -> list[dict[str, Any]]:
    task = backend.get_task(request["task_id"])
    if task is None:
        raise RootSetError("root_set_authority_lost", "canonical task is unavailable.")
    result = []
    for root in request["roots"]:
        enrollment = registry["repositories"].get(root["repository_id"])
        if not isinstance(enrollment, dict):
            raise RootSetError("root_set_not_enrolled", "every root must be enrolled before it can be claimed.")
        policy = list(task.verification.commands) if root["root_id"] == request["primary_root_id"] else enrollment["verification_commands"]
        if root["verification_commands"] != policy:
            raise RootSetError("root_set_authority_lost", "request verification commands do not match owner policy.")
        result.append(root)
    primary = next(root for root in result if root["root_id"] == request["primary_root_id"])
    if Path(primary["path"]).resolve() != project_root.resolve():
        raise RootSetError("root_set_identity_mismatch", "primary root must be the canonical State checkout.")
    return result


def _prepared_fact_matches(fact: dict[str, Any], registry: dict[str, Any]) -> bool:
    """Verify one retained worktree against its enrolled Git identity."""
    enrollment = registry["repositories"].get(fact.get("repository_id"))
    if not isinstance(enrollment, dict):
        return False
    try:
        live = live_repository_identity(
            fact["claim_worktree"], declared_origin=enrollment["origin"]
        )
        if not any(
            live["common_dir"] == alias.get("common_dir")
            and fact["canonical_root"] == alias.get("path")
            for alias in enrollment.get("aliases", [])
            if isinstance(alias, dict)
        ):
            return False
        branch = subprocess.run(
            ["git", "-C", fact["claim_worktree"], "branch", "--show-current"],
            check=False, capture_output=True, text=True, timeout=5,
        )
        head = subprocess.run(
            ["git", "-C", fact["claim_worktree"], "rev-parse", "HEAD"],
            check=False, capture_output=True, text=True, timeout=5,
        )
        return (
            branch.returncode == 0
            and head.returncode == 0
            and branch.stdout.strip() == fact["branch"]
            and head.stdout.strip() == fact["baseline_sha"]
        )
    except (OSError, subprocess.TimeoutExpired, RootSetError, KeyError, TypeError):
        return False


def _bound_claim_matches(backend, reservation: dict[str, Any], registry: dict[str, Any], state_dir: Path):
    """Return the sole live claim only when its full journal binding matches."""
    if reservation.get("state_identity") != str(state_dir.resolve()):
        raise RootSetError("root_set_reconciliation_required", "root-set reservation belongs to another State identity.")
    claim_id = reservation.get("claim_id")
    claim = backend.get_claim(claim_id) if isinstance(claim_id, str) else None
    from anvil.clock import SystemClock
    if (
        claim is None or claim.root_set is None
        or claim.root_set.request_id != reservation.get("request_id")
        or claim.root_set.request_digest != reservation.get("digest")
        or claim.root_set.reservation_id != reservation.get("reservation_id")
        or claim.status.value != "active" or claim.lease_expires_at <= SystemClock().now()
    ):
        raise RootSetError("root_set_reconciliation_required", "root-set reservation needs canonical State reconciliation.")
    results = reservation.get("root_results")
    if not isinstance(results, list) or len(results) != len(reservation.get("roots", [])):
        raise RootSetError("root_set_reconciliation_required", "root-set Git targets were not durably recorded.")
    expected_pairs = {(root["root_id"], root["repository_id"]) for root in reservation["roots"]}
    actual_pairs = {(item.get("root_id"), item.get("repository_id")) for item in results if isinstance(item, dict)}
    if expected_pairs != actual_pairs or len(actual_pairs) != len(results):
        raise RootSetError("root_set_reconciliation_required", "root-set Git targets do not match the request.")
    if any(item.get("state") != "prepared" or not _prepared_fact_matches(item, registry) for item in results):
        raise RootSetError("root_set_reconciliation_required", "a root-set Git target is unavailable.")
    journal_facts = [{key: item[key] for key in ("root_id", "repository_id", "baseline_sha", "canonical_root", "claim_worktree", "branch", "verification_commands")} for item in results]
    expected_digest = request_digest({"primary_root_id": reservation.get("primary_root_id"), "roots": journal_facts})
    if (
        [fact.model_dump(mode="json") for fact in claim.root_set.root_facts] != journal_facts
        or claim.root_set.primary_root_id != reservation.get("primary_root_id")
        or claim.root_set.root_set_digest != expected_digest
    ):
        raise RootSetError("root_set_reconciliation_required", "canonical root facts do not match the owner journal.")
    return claim


@roots_app.command("enroll")
def enroll(
    repository_id: str = typer.Option(..., "--repository-id"),  # noqa: B008
    path: Path = typer.Option(..., "--path"),  # noqa: B008
    origin: str = typer.Option(..., "--origin"),  # noqa: B008
    verification_command: list[str] | None = typer.Option(None, "--verification-command"),  # noqa: B008
    json_output: bool = JSON_OPTION,
) -> None:
    """Enroll one exact live Git checkout under an owner repository identity."""
    try:
        # Do not activate owner-global exclusivity around a checkout that is
        # already carrying a legacy State lease.  The backend is the supported
        # reader; no State files are inspected directly.
        candidate_state = _resolve_state_dir(path)
        if candidate_state.exists():
            backend = _open_backend(candidate_state)
            try:
                if backend.list_active_claims():
                    raise RootSetError(
                        "root_set_preexisting_claim",
                        "cannot enroll a checkout with active legacy claims.",
                    )
            finally:
                backend.close()
        data = RootSetRegistry().enroll(repository_id=repository_id, path=str(path), origin=origin, verification_commands=verification_command)
    except RootSetError as exc:
        _root_fail("roots enroll", exc, json_output)
    if json_output:
        emit_success("roots enroll", {"enrollment": data})
    else:
        typer.echo(f"Enrolled repository '{data['repository_id']}'.")


@roots_app.command("claim")
def claim(
    task_id: str,
    request_file: Path = typer.Option(..., "--request-file"),  # noqa: B008
    actor: str | None = typer.Option(None, "--actor"),  # noqa: B008
    lease_minutes: float | None = typer.Option(None, "--lease"),  # noqa: B008
    json_output: bool = JSON_OPTION,
    cwd: Path | None = typer.Option(None, "--cwd", hidden=True),  # noqa: B008
) -> None:
    """Acquire one canonical task claim after reserving every enrolled root."""
    command = "roots claim"
    resolved_actor = resolve_actor(actor)
    state_dir = _resolve_state_dir(cwd)
    _require_state_dir(state_dir, command=command, json_output=json_output)
    project_root = _resolve_project_dir(cwd)
    try:
        request, _request_id, digest = _original_request_identity(task_id=task_id, request_file=request_file, actor=resolved_actor, state_dir=state_dir)
        request["task_id"] = task_id
        registry_owner = RootSetRegistry()
        backend = _open_backend(state_dir)
        try:
            with registry_owner.locked() as registry:
                roots = _request_authority(request, backend, registry, project_root)
                reservation = registry_owner.reserve(registry, request_id=request["request_id"], digest=digest, actor=resolved_actor, state_identity=str(state_dir.resolve()), roots=roots)
                reservation["primary_root_id"] = request["primary_root_id"]
                registry_owner.checkpoint(registry)
                if reservation["state"] == "bound":
                    claim_model = _bound_claim_matches(backend, reservation, registry, state_dir)
                    response = _claim_response(reservation, claim_model)
                else:
                    response = _provision_and_claim(backend, state_dir, project_root, task_id, resolved_actor, lease_minutes, request, roots, registry_owner, registry, reservation, digest)
        finally:
            backend.close()
    except RootSetError as exc:
        _root_fail(command, exc, json_output)
    if json_output:
        emit_success(command, response)
    else:
        typer.echo(f"Root-set claim '{response['claim_id']}' is {response['status']}.")


def _provision_and_claim(backend, state_dir: Path, project_root: Path, task_id: str, actor: str, lease_minutes: float | None, request: dict[str, Any], roots: list[dict[str, Any]], registry_owner: RootSetRegistry, registry: dict[str, Any], reservation: dict[str, Any], digest: str) -> dict[str, Any]:
    from anvil.claims.manager import ClaimError, ClaimManager
    from anvil.clock import SystemClock
    from anvil.git_ops import (
        ClaimGitMutationTracker,
        apply_claim_plan,
        claim_git_metadata,
        resolve_claim_plan,
    )
    from anvil.state.models import RootSetClaimBinding, RootSetRootFact

    task = backend.get_task(task_id)
    if task is None:
        raise RootSetError("root_set_authority_lost", "canonical task is unavailable.")
    plans = []
    trackers = []
    try:
        for root in roots:
            root_path = Path(root["path"])
            plan = resolve_claim_plan(
                task_id,
                task.title,
                cwd=root_path,
                worktree=True,
                target_path=root_path.parent / f"wt-{task_id.lower()}-{root['root_id']}",
            )
            metadata = claim_git_metadata(plan)
            if metadata is None:
                raise RootSetError("root_set_provision_failed", "every root requires an isolated Git target.")
            plans.append((root, plan, metadata))
        reservation["root_results"] = [
            {
                "root_id": root["root_id"],
                "repository_id": root["repository_id"],
                "baseline_sha": metadata.claim_start_sha,
                "canonical_root": metadata.canonical_root,
                "claim_worktree": metadata.worktree_path,
                "branch": metadata.branch,
                "verification_commands": root["verification_commands"],
                "state": "intended",
            }
            for root, _plan, metadata in plans
        ]
        registry_owner.checkpoint(registry)
        for index, (_root, plan, _metadata) in enumerate(plans):
            tracker = ClaimGitMutationTracker(plan)
            apply_claim_plan(plan, cwd=Path(plan.caller_path), tracker=tracker)
            trackers.append(tracker)
            reservation["root_results"][index]["state"] = "prepared"
            registry_owner.checkpoint(registry)
        primary, _primary_plan, primary_metadata = next(item for item in plans if item[0]["root_id"] == request["primary_root_id"])
        root_facts = tuple(
            RootSetRootFact(
                **{
                    key: item[key]
                    for key in (
                        "root_id", "repository_id", "baseline_sha", "canonical_root",
                        "claim_worktree", "branch", "verification_commands",
                    )
                }
            )
            for item in reservation["root_results"]
        )
        binding = RootSetClaimBinding(
            request_id=request["request_id"],
            primary_root_id=request["primary_root_id"],
            request_digest=digest,
            root_set_digest=request_digest(
                {"primary_root_id": request["primary_root_id"], "roots": [
                    fact.model_dump(mode="json") for fact in root_facts
                ]}
            ),
            reservation_id=reservation["reservation_id"],
            root_facts=root_facts,
        )
        cfg = _load_config_optional(state_dir)
        manager = ClaimManager(backend, SystemClock(), actor=actor, project_root=project_root, **_lease_manager_kwargs(cfg, lease_override=lease_minutes))

        def mark_state_append_attempted() -> None:
            # ClaimManager invokes this only after every typed eligibility
            # gate and immediately before materializing the log event.  A
            # known pre-log refusal therefore remains safely cancellable;
            # every uncertain log/SQLite outcome retains the global overhold.
            reservation["state_append_attempted"] = True
            registry_owner.checkpoint(registry)

        result = manager.claim(
            task_id,
            expected_files=primary["expected_files"],
            branch=primary_metadata.branch,
            worktree_path=primary_metadata.worktree_path,
            git_metadata=primary_metadata,
            root_set=binding,
            root_set_authorization=authorize_root_set_claim(binding, reservation),
            pre_log_check=mark_state_append_attempted,
        )
    except RootSetError:
        raise
    except Exception as exc:
        # Do not delete a prepared target after an uncertain canonical append.
        # The durable pending reservation blocks reuse until owner reconciliation
        # proves which side won; dirty targets are never removed automatically.
        if isinstance(exc, ClaimError):
            raise RootSetError("root_set_provision_failed", str(exc)) from exc
        raise RootSetError("root_set_provision_failed", "root-set targets could not be prepared safely.") from exc
    registry_owner.bind(registry, reservation, result.claim.id)
    registry_owner.checkpoint(registry)
    return _claim_response(reservation, result.claim)


def _claim_response(reservation: dict[str, Any], claim_model) -> dict[str, Any]:
    return {"schema": "anvil.root-set-claim/v1", "status": "ready", "request_id": reservation["request_id"], "request_digest": reservation["digest"], "reservation_id": reservation["reservation_id"], "claim_id": claim_model.id, "lease_expires_at": claim_model.lease_expires_at.isoformat(), "roots": reservation.get("root_results", [])}


def _load_reservation(request_id: str, actor: str, digest: str) -> dict[str, Any]:
    with RootSetRegistry().locked() as registry:
        reservation = registry["reservations"].get(request_id)
        if reservation is None or reservation.get("actor") != actor or reservation.get("digest") != digest:
            raise RootSetError("root_set_reconciliation_required", "root-set request identity is unavailable.")
        return dict(reservation)


def _original_request_identity(*, task_id: str, request_file: Path, actor: str, state_dir: Path) -> tuple[dict[str, Any], str, str]:
    """Derive a lost-response lookup identity without exposing digest rules."""
    request = _load_request(request_file)
    digest = request_digest({"task_id": task_id, "actor": actor, "state_identity": str(state_dir.resolve()), "request": {key: request[key] for key in ("schema", "request_id", "primary_root_id", "roots")}})
    return request, request["request_id"], digest


@roots_app.command("request-digest")
def request_digest_command(
    task_id: str,
    request_file: Path = typer.Option(..., "--request-file"),  # noqa: B008
    actor: str = typer.Option(..., "--actor"),  # noqa: B008
    json_output: bool = JSON_OPTION,
    cwd: Path | None = typer.Option(None, "--cwd", hidden=True),  # noqa: B008
) -> None:
    """Derive the immutable lookup digest for an original root-set request."""
    command = "roots request-digest"
    state_dir = _resolve_state_dir(cwd)
    _require_state_dir(state_dir, command=command, json_output=json_output)
    try:
        _request, request_id, digest = _original_request_identity(task_id=task_id, request_file=request_file, actor=resolve_actor(actor), state_dir=state_dir)
    except RootSetError as exc:
        _root_fail(command, exc, json_output)
    data = {"schema": "anvil.root-set-request-digest/v1", "request_id": request_id, "request_digest": digest}
    if json_output:
        emit_success(command, data)
    else:
        typer.echo(f"Root-set request '{request_id}' digest derived.")


@roots_app.command("status")
def status(
    request_id: str = typer.Option(..., "--request-id"),  # noqa: B008
    request_digest_value: str = typer.Option(..., "--request-digest"),  # noqa: B008
    actor: str | None = typer.Option(None, "--actor"),  # noqa: B008
    json_output: bool = JSON_OPTION,
    cwd: Path | None = typer.Option(None, "--cwd", hidden=True),  # noqa: B008
) -> None:
    """Read one request using its original actor and immutable digest."""
    try:
        reservation = _load_reservation(request_id, resolve_actor(actor), request_digest_value)
        if reservation.get("state_identity") != str(_resolve_state_dir(cwd).resolve()):
            raise RootSetError("root_set_reconciliation_required", "root-set request belongs to another State identity.")
    except RootSetError as exc:
        _root_fail("roots status", exc, json_output)
    data = {
        "request_id": reservation["request_id"],
        "request_digest": reservation["digest"],
        "reservation_id": reservation["reservation_id"],
        "state": reservation["state"],
        "claim_id": reservation["claim_id"],
        "root_results": reservation.get("root_results", []),
    }
    if json_output:
        emit_success("roots status", data)
    else:
        typer.echo(f"Root-set request '{request_id}': {reservation['state']}.")


@roots_app.command("reconcile")
def reconcile(
    request_id: str = typer.Option(..., "--request-id"),  # noqa: B008
    request_digest_value: str = typer.Option(..., "--request-digest"),  # noqa: B008
    actor: str | None = typer.Option(None, "--actor"),  # noqa: B008
    confirm_runner_stopped: bool = typer.Option(False, "--confirm-runner-stopped"),  # noqa: B008
    cancel_if_no_claim: bool = typer.Option(False, "--cancel-if-no-claim"),  # noqa: B008
    json_output: bool = JSON_OPTION,
    cwd: Path | None = typer.Option(None, "--cwd", hidden=True),  # noqa: B008
) -> None:
    """Bind a pending request only when canonical State proves the same claim."""
    command = "roots reconcile"
    state_dir = _resolve_state_dir(cwd)
    _require_state_dir(state_dir, command=command, json_output=json_output)
    resolved_actor = resolve_actor(actor)
    try:
        backend = _open_backend(state_dir)
        try:
            with RootSetRegistry().locked() as registry:
                reservation = registry["reservations"].get(request_id)
                if reservation is None or reservation.get("actor") != resolved_actor or reservation.get("digest") != request_digest_value:
                    raise RootSetError("root_set_reconciliation_required", "root-set request identity is unavailable.")
                if reservation.get("state_identity") != str(state_dir.resolve()):
                    raise RootSetError("root_set_reconciliation_required", "root-set reservation belongs to another State identity.")
                candidates = [
                    claim
                    for claim in backend.list_claims()
                    if claim.root_set is not None
                    and claim.root_set.reservation_id == reservation["reservation_id"]
                    and claim.root_set.request_id == request_id
                    and claim.root_set.request_digest == request_digest_value
                    and claim.claimed_by == resolved_actor
                ]
                if cancel_if_no_claim:
                    if (
                        reservation.get("state") != "pending"
                        or reservation.get("state_append_attempted") is not False
                        or candidates
                    ):
                        raise RootSetError("root_set_reconciliation_required", "owner journal cannot prove a pre-State cancellation.")
                    # Prepared targets are intentionally retained; this only
                    # releases an owner reservation whose durable phase proves
                    # no canonical append was attempted.
                    reservation["state"] = "released"
                    RootSetRegistry().checkpoint(registry)
                    data = {"request_id": request_id, "state": "released", "targets_retained": True}
                    return _emit_reconcile(command, data, json_output)
                if (
                    reservation.get("state") in {"pending", "bound", "release_pending"}
                    and len(candidates) == 1
                    and candidates[0].status.value != "active"
                ):
                    # A canonical terminal reduction can succeed while the
                    # owner journal is unavailable.  Reconstruct its
                    # overhold here; never free it without the explicit stop
                    # confirmation below.
                    reservation["state"] = "release_pending"
                    registry_owner = RootSetRegistry()
                    registry_owner.checkpoint(registry)
                    if not confirm_runner_stopped:
                        raise RootSetError("root_set_reconciliation_required", "runner stop confirmation is required before releasing the owner reservation.")
                    reservation["state"] = "released"
                    registry_owner.checkpoint(registry)
                    data = {"request_id": request_id, "state": "released", "claim_id": candidates[0].id}
                    return _emit_reconcile(command, data, json_output)
                if len(candidates) != 1:
                    raise RootSetError("root_set_reconciliation_required", "canonical State does not prove exactly one root-set claim.")
                from anvil.clock import SystemClock

                if (
                    candidates[0].status.value != "active"
                    or candidates[0].lease_expires_at <= SystemClock().now()
                ):
                    raise RootSetError("root_set_reconciliation_required", "canonical root-set claim is terminal or expired.")
                root_results = reservation.get("root_results")
                if not isinstance(root_results, list) or len(root_results) != len(reservation["roots"]):
                    raise RootSetError("root_set_reconciliation_required", "root-set Git targets were not durably recorded.")
                if any(
                    not isinstance(item, dict)
                    or not isinstance(item.get("claim_worktree"), str)
                    or not Path(item["claim_worktree"]).is_dir()
                    or item.get("state") != "prepared"
                    or not _prepared_fact_matches(item, registry)
                    for item in root_results
                ):
                    raise RootSetError("root_set_reconciliation_required", "a root-set Git target is unavailable.")
                expected_pairs = {(root["root_id"], root["repository_id"]) for root in reservation["roots"]}
                result_pairs = {(item.get("root_id"), item.get("repository_id")) for item in root_results}
                if expected_pairs != result_pairs or len(result_pairs) != len(root_results):
                    raise RootSetError("root_set_reconciliation_required", "root-set Git targets do not match the request.")
                primary_matches = [
                    item
                    for item in root_results
                    if isinstance(item, dict)
                    and item.get("claim_worktree") == candidates[0].worktree_path
                ]
                primary = primary_matches[0] if len(primary_matches) == 1 else None
                if (
                    primary is None
                    or not isinstance(primary.get("claim_worktree"), str)
                    or not Path(primary["claim_worktree"]).is_dir()
                    or candidates[0].worktree_path != primary["claim_worktree"]
                ):
                    raise RootSetError("root_set_reconciliation_required", "root-set Git targets do not match canonical State.")
                binding_facts = [fact.model_dump(mode="json") for fact in candidates[0].root_set.root_facts]
                journal_facts = [
                    {key: item[key] for key in (
                        "root_id", "repository_id", "baseline_sha", "canonical_root",
                        "claim_worktree", "branch", "verification_commands",
                    )}
                    for item in root_results
                ]
                expected_digest = request_digest({
                    "primary_root_id": candidates[0].root_set.primary_root_id,
                    "roots": journal_facts,
                })
                if (
                    binding_facts != journal_facts
                    or candidates[0].root_set.primary_root_id != reservation.get("primary_root_id")
                    or candidates[0].root_set.root_set_digest != expected_digest
                ):
                    raise RootSetError("root_set_reconciliation_required", "canonical root facts do not match the owner journal.")
                RootSetRegistry.bind(registry, reservation, candidates[0].id)
                RootSetRegistry().checkpoint(registry)
                data = _claim_response(reservation, candidates[0])
        finally:
            backend.close()
    except RootSetError as exc:
        _root_fail(command, exc, json_output)
    if json_output:
        emit_success(command, data)
    else:
        typer.echo(f"Root-set request '{request_id}' reconciled.")


def _emit_reconcile(command: str, data: dict[str, Any], json_output: bool) -> None:
    """Emit a terminal reconciliation result without touching retained targets."""
    if json_output:
        emit_success(command, data)
    else:
        typer.echo(f"Root-set request '{data['request_id']}' is {data['state']}.")
