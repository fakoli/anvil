"""Owned Run worker: verified returned bytes, real checkout proofs, native submit."""

from __future__ import annotations

import base64
import dataclasses
import json
import os
import selectors
import signal
import stat
import subprocess
import sys
import tarfile
import time
from pathlib import Path, PurePosixPath
from typing import Any

from anvil.claims.command_proof_artifact import (
    claim_command_cwd_identity,
    load_claim_command_proof,
    verify_claim_command_proof_batch,
)
from anvil.cli._helpers import _open_backend
from anvil.config import ExecutionAdapter, load_execution_adapter
from anvil.execution import (
    AdapterResult,
    ExecutionError,
    ExecutionRequest,
    adapter_module,
    assignment_for,
    clean_source,
    digest,
    git,
    json_bytes,
    live_claim,
    native_cli,
    now,
    read_receipt,
    relative_file,
    request_for,
    require_visible_index,
    save_execution_capabilities,
    sync_directory,
    timestamp,
    write_receipt,
)
from anvil.state.hashing import canonical_json_bytes
from anvil.state.models import ClaimCommandEvidenceCore


def process_group_absent(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return True
    return False


def capture(
    argv: list[str],
    cwd: Path,
    directory: Path,
    name: str,
    deadline: float,
    maximum: int,
    environment: dict[str, str] | None = None,
    *,
    separate_stdout: bool = False,
    require_success: bool = True,
) -> dict[str, Any]:
    """Bounded bytes and owned group custody; no shell or PID fallback."""
    if os.name != "posix":
        raise ExecutionError(
            "unsupported_platform", "Owned group execution currently requires POSIX"
        )
    started = timestamp()
    child = subprocess.Popen(
        argv,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE if separate_stdout else subprocess.STDOUT,
        env=environment,
        start_new_session=True,
    )
    output = bytearray()
    stdout = bytearray()
    failure = None
    selector = selectors.DefaultSelector()
    try:
        write_receipt(
            directory / (name + ".started.json"),
            {"argv": argv, "cwd": str(cwd), "pid": child.pid, "started_at": started},
        )
        selector.register(child.stdout, selectors.EVENT_READ)
        if separate_stdout:
            selector.register(child.stderr, selectors.EVENT_READ)
        while selector.get_map():
            if time.monotonic() >= deadline:
                failure = "command_timeout"
                break
            for key, _ in selector.select(min(0.1, max(0, deadline - time.monotonic()))):
                chunk = os.read(key.fileobj.fileno(), min(65536, maximum + 1 - len(output)))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output.extend(chunk)
                if separate_stdout and key.fileobj is child.stdout:
                    stdout.extend(chunk)
                if len(output) > maximum:
                    failure = "output_overflow"
                    break
            if failure:
                break
        if failure is None:
            child.wait(timeout=max(0.01, deadline - time.monotonic()))
    except BaseException:
        failure = "capture_interrupted"
        raise
    finally:
        selector.close()
        if not process_group_absent(child.pid):
            if child.poll() is None or failure:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=5)
            if not process_group_absent(child.pid):
                # A command which leaves descendants has not completed custody.
                os.killpg(child.pid, signal.SIGKILL)
                failure = failure or "descendants_remain"
        child.wait(timeout=5)
        child.stdout.close()
        if child.stderr is not None:
            child.stderr.close()
        stopped = process_group_absent(child.pid)
        with (directory / (name + ".log")).open("xb") as stream:
            os.chmod(directory / (name + ".log"), 0o600)
            stream.write(output)
            stream.flush()
            os.fsync(stream.fileno())
        if separate_stdout:
            with (directory / (name + ".stdout")).open("xb") as stream:
                os.chmod(directory / (name + ".stdout"), 0o600)
                stream.write(stdout)
                stream.flush()
                os.fsync(stream.fileno())
        result = {
            "argv": argv,
            "cwd": str(cwd),
            "pid": child.pid,
            "started_at": started,
            "ended_at": timestamp(),
            "exit_code": child.returncode,
            "output_sha256": digest(output),
            "output_bytes": len(output),
            "failure": failure,
            "group_absent": stopped,
        }
        write_receipt(directory / (name + ".result.json"), result)
    if failure or not stopped or (require_success and child.returncode):
        raise ExecutionError(
            failure or "command_failed", "Owned command failed; inspect retained output and custody"
        )
    return result


def checked_path(root: Path, name: str) -> Path:
    path = root / relative_file(name)
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ExecutionError("unsafe_path", "Returned file path contains a symbolic link")
    if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
        raise ExecutionError("unsafe_path", "Returned file path is not an unaliased regular file")
    current = root
    for component in PurePosixPath(name).parts:
        if current.is_dir() and any(
            entry.name.casefold() == component.casefold() and entry.name != component
            for entry in current.iterdir()
        ):
            raise ExecutionError("unsafe_path", "Returned path aliases another path's case")
        current = current / component
    return path


def retained_adapter_task(output: Path, receipt: dict[str, Any]) -> dict[str, Any]:
    archive_path = output / "inputs.tar"
    if archive_path.is_symlink() or archive_path.stat().st_size > 96 * 1024 * 1024:
        raise ExecutionError("invalid_result", "Invalid retained adapter input archive")
    if digest(archive_path.read_bytes()) != receipt.get("input_sha256"):
        raise ExecutionError("result_mismatch", "Adapter input bytes changed")
    with tarfile.open(archive_path) as archive:
        members = [member for member in archive.getmembers() if member.name == "task.json"]
        if len(members) != 1 or not members[0].isfile() or members[0].size > 1024 * 1024:
            raise ExecutionError("invalid_result", "Invalid adapter native task")
        native_task = json.load(archive.extractfile(members[0]))
    return native_task


def verified_changes(
    adapter: Any, output: Path, derived: dict[str, Any], registration: ExecutionAdapter
) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    receipt = read_receipt(output / "receipt.json")
    for field in (
        "delivery_valid",
        "cleanup_complete",
        "protected_unchanged",
        "artifacts_verified",
    ):
        if receipt.get(field) is not True:
            raise ExecutionError(
                "adapter_failed", "Adapter did not prove delivery and stopped protected custody"
            )
    if (
        receipt.get("version") != 2
        or receipt.get("task_id") != derived["task_id"]
        or receipt.get("acceptance") != "not_granted"
        or receipt.get("outcome") != "returned"
    ):
        raise ExecutionError("result_mismatch", "Adapter result identity differs from the request")
    source_files, source = adapter.frozen_source(derived)
    if receipt.get("source") != source:
        raise ExecutionError(
            "source_changed", "Returned source manifest differs from the frozen checkout"
        )
    native_task = retained_adapter_task(output, receipt)
    for field in (
        "version",
        "task_id",
        "kind",
        "instructions",
        "writable_paths",
        "checks",
        "run_options",
    ):
        if native_task.get(field) != derived[field]:
            raise ExecutionError(
                "result_mismatch", "Adapter task changed the requested scope or options"
            )
    profile = adapter.profile_settings(Path(registration.profile))
    expected_controls = {
        "operation": "run",
        "max_tool_calls": 40,
        "max_turns": 32,
        "allowed_tools": sorted(adapter.TOOLS),
        "runtime_defaults": {
            "model": profile["model"],
            "thinking": profile.get("thinking", "high"),
        },
    }
    if any(native_task.get(key) != value for key, value in expected_controls.items()):
        raise ExecutionError("result_mismatch", "Adapter changed the budget, tools or defaults")
    if native_task.get("run_id") != receipt.get("run_id") or native_task.get("source") != source:
        raise ExecutionError("result_mismatch", "Adapter run or source identity mismatch")
    worker = adapter.verify_return(output / "results", native_task, source_files, receipt["run_id"])
    if receipt.get("worker") != worker:
        raise ExecutionError(
            "result_mismatch", "Host receipt differs from verified worker evidence"
        )
    changes = read_receipt(output / "results" / "changes.json")
    if not isinstance(changes, list) or len(changes) > len(derived["writable_paths"]):
        raise ExecutionError("invalid_changes", "Invalid returned change list")
    names = []
    for change in changes:
        if not isinstance(change, dict) or set(change) != {"path", "before_sha256", "after_sha256"}:
            raise ExecutionError("invalid_changes", "Unexpected returned change fields")
        name = relative_file(change["path"])
        names.append(name)
        if name not in derived["writable_paths"]:
            raise ExecutionError("invalid_changes", "Returned path is outside the native scope")
        before = source_files.get(name)
        if change["before_sha256"] != (digest(before) if before is not None else None):
            raise ExecutionError("invalid_changes", "Returned preimage differs from frozen source")
        if change["after_sha256"] is not None:
            path = checked_path(output / "results" / "changed-files", name)
            if digest(path.read_bytes()) != change["after_sha256"]:
                raise ExecutionError("invalid_changes", "Returned bytes differ from manifest")
    if len(set(names)) != len(names):
        raise ExecutionError("invalid_changes", "Duplicate returned paths")
    return changes, source_files


def integrate(
    cwd: Path,
    output: Path,
    changes: list[dict[str, Any]],
    source_files: dict[str, bytes],
    directory: Path,
) -> None:
    """Validate all preimages first; preserve partial application without rollback."""
    modes = {}
    for change in changes:
        name = change["path"]
        target = checked_path(cwd, name)
        before = target.read_bytes() if target.exists() else None
        if before != source_files.get(name):
            raise ExecutionError("preimage_changed", "Local source changed before integration")
        modes[name] = stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o644
    write_receipt(directory / "integration.json", {"changes": changes, "modes": modes})
    for index, change in enumerate(changes):
        name = change["path"]
        target = checked_path(cwd, name)
        if (target.read_bytes() if target.exists() else None) != source_files.get(name):
            raise ExecutionError(
                "partial_integration", "Preimage changed during integration; inspect retained facts"
            )
        if change["after_sha256"] is None:
            target.unlink()
        else:
            returned = checked_path(output / "results" / "changed-files", name).read_bytes()
            if digest(returned) != change["after_sha256"]:
                raise ExecutionError(
                    "partial_integration", "Returned bytes changed during integration"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".anvil-run-" + directory.name)
            with temporary.open("xb") as stream:
                os.chmod(temporary, modes[name])
                stream.write(returned)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        sync_directory(target.parent)
        write_receipt(directory / (f"integrated-{index}.json"), change)


def check_integrated_source(
    cwd: Path, source_sha: str, changes: list[dict[str, Any]], modes: dict[str, int]
) -> None:
    require_visible_index(cwd)
    if git(cwd, "rev-parse", "HEAD").decode().strip() != source_sha:
        raise ExecutionError("source_changed", "Checkout revision changed during execution")
    actual = git(cwd, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    names = []
    for entry in actual.split(b"\0"):
        if not entry:
            continue
        if entry[:2] not in {b" M", b" D", b"??"}:
            raise ExecutionError("source_changed", "Unexpected index or file metadata changes")
        names.append(entry[3:].decode("utf-8"))
    if set(names) != {change["path"] for change in changes}:
        raise ExecutionError(
            "source_changed", "Checkout contains changes outside returned manifest"
        )
    for change in changes:
        path = checked_path(cwd, change["path"])
        actual_digest = digest(path.read_bytes()) if path.exists() else None
        if actual_digest != change["after_sha256"]:
            raise ExecutionError("source_changed", "Integrated bytes changed during verification")
        if path.exists() and stat.S_IMODE(path.stat().st_mode) != modes[change["path"]]:
            raise ExecutionError(
                "source_changed", "Integrated file mode changed during verification"
            )


def portable_adapter_result(
    adapter: Any,
    output: Path,
    registration: ExecutionAdapter,
    request: ExecutionRequest,
    changes: list[dict[str, Any]],
) -> AdapterResult:
    """Join one verified concrete adapter return to the portable host request."""
    receipt = read_receipt(output / "receipt.json")
    configuration = receipt["worker"]["configuration"]
    profile = adapter.profile_settings(Path(registration.profile))
    before, running, after = (
        receipt[name] for name in ("sandbox_before", "sandbox_running", "sandbox_after")
    )
    if (
        any(value.get("id") != profile["sandbox_id"] for value in (before, running, after))
        or after.get("phase") != "Stopped"
        or after.get("main_process_instance_id") != running.get("main_process_instance_id")
        or receipt.get("cleanup_complete") is not True
        or receipt.get("protected_unchanged") is not True
    ):
        raise ExecutionError(
            "result_mismatch", "Returned owner identities do not prove exact stopped custody"
        )
    actual = adapter.snapshot(output / "results")
    raw_digest = digest((output / "receipt.json").read_bytes())
    value = {
        "schema": "anvil.adapter-result.v1",
        "dispatch_id": request.dispatch_id,
        "request_sha256": request.request_sha256,
        "assignment_sha256": request.assignment_sha256,
        "adapter_run_id": receipt["run_id"],
        "outcome": "returned",
        "acceptance": "not_granted",
        "effective_options": configuration["effective"],
        "capability_receipt_sha256": digest(actual["rpc-configuration.json"]),
        "source_commit": receipt["source"]["commit"],
        "changes": [
            {
                **change,
                "artifact_path": "results/changed-files/" + change["path"]
                if change["after_sha256"] is not None
                else None,
            }
            for change in changes
        ],
        "artifacts": [
            {"path": "results/" + name, "sha256": digest(raw), "size_bytes": len(raw)}
            for name, raw in sorted(actual.items())
        ],
        "sandbox_checks": [
            {
                "name": check["name"],
                "argv": check["argv"],
                "exit_code": check["exit_code"],
                "output_sha256": check["log_sha256"],
                "authority": "sandbox_only",
            }
            for check in receipt["worker"]["checks"]
        ],
        "raw_receipt_sha256": raw_digest,
        "custody": {
            "local_coordinator": f"pid:{os.getpid()}/pgid:{os.getpgrp()}",
            "remote_owner_reference": "openshell:" + profile["sandbox_name"],
            "sandbox_id": after["id"],
            "main_instance_id": after["main_process_instance_id"],
            "stop_status": "stopped",
            "protected_unchanged": True,
            "receipt_sha256": raw_digest,
        },
        "saved_conversation": None,
        "error_code": None,
    }
    if configuration.get("session_file") is not None:
        raise ExecutionError(
            "result_mismatch", "Initial Run cannot return an unapproved saved conversation"
        )
    return AdapterResult.from_data(value, request)


def verify_portable_return(
    result: AdapterResult,
    request: ExecutionRequest,
    adapter: Any,
    output: Path,
    registration: ExecutionAdapter,
    changes: list[dict[str, Any]],
) -> None:
    if load_execution_adapter() != registration:
        raise ExecutionError("adapter_changed", "Registration changed after the verified return")
    result.validate(request)
    expected = portable_adapter_result(adapter, output, registration, request, changes)
    if dataclasses.asdict(result) != dataclasses.asdict(expected):
        raise ExecutionError(
            "result_mismatch", "Portable result differs from actual artifacts or custody"
        )


def execute_capability_refresh(directory: Path, request: dict[str, Any]) -> dict[str, Any]:
    outcome = {
        "version": 1,
        "run_id": request["run_id"],
        "state": "reconciliation_required",
        "acceptance": "not_granted",
        "local_children_stopped": False,
    }
    try:
        registration = ExecutionAdapter(**request["adapter"])
        adapter = adapter_module(registration)
        output = directory / "adapter"
        capture(
            [
                registration.interpreter,
                "-I",
                registration.script,
                "capabilities",
                "--profile",
                registration.profile,
                "--output",
                str(output),
            ],
            directory,
            directory,
            "adapter",
            time.monotonic() + 150,
            4 * 1024 * 1024,
        )
        if load_execution_adapter() != registration:
            raise ExecutionError("adapter_changed", "Registration changed during metadata refresh")
        receipt = read_receipt(output / "receipt.json")
        for field in (
            "capabilities_valid",
            "cleanup_complete",
            "protected_unchanged",
            "artifacts_verified",
        ):
            if receipt.get(field) is not True:
                raise ExecutionError(
                    "adapter_failed", "Metadata did not prove stopped protected custody"
                )
        native_task = retained_adapter_task(output, receipt)
        profile = adapter.profile_settings(Path(registration.profile))
        expected = {
            "version": 2,
            "task_id": "capabilities",
            "kind": "validation",
            "operation": "capabilities",
            "writable_paths": [],
            "checks": [],
            "run_options": {},
            "source": {"kind": "empty-workspace", "files": {}, "modes": {}},
            "runtime_defaults": {
                "model": profile["model"],
                "thinking": profile.get("thinking", "high"),
            },
        }
        if any(native_task.get(key) != value for key, value in expected.items()):
            raise ExecutionError("result_mismatch", "Metadata task changed its no-prompt scope")
        if (
            receipt.get("acceptance") != "not_granted"
            or receipt.get("outcome") != "capabilities"
            or native_task.get("run_id") != receipt.get("run_id")
        ):
            raise ExecutionError("result_mismatch", "Metadata outcome or identity mismatch")
        verified = adapter.verify_return(output / "results", native_task, {}, receipt["run_id"])
        if (
            verified != receipt.get("worker")
            or verified["configuration"]["prompt_sent"] is not False
        ):
            raise ExecutionError(
                "result_mismatch", "Metadata evidence differs or includes a prompt"
            )
        save_execution_capabilities(
            directory.parents[1],
            verified["configuration"],
            registration,
            digest((output / "receipt.json").read_bytes()),
        )
        outcome.update(
            state="capabilities_ready",
            local_children_stopped=True,
            adapter_run_id=receipt["run_id"],
        )
    except Exception as error:
        outcome.update(
            error_code=getattr(error, "code", "metadata_failed"), error=str(error)[:4000]
        )
    outcome["ended_at"] = timestamp()
    write_receipt(directory / "result.json", outcome)
    return outcome


def execute_run(directory: Path) -> dict[str, Any]:
    value = read_receipt(directory / "request.json")
    dispatch = read_receipt(directory / "dispatch.json")
    metadata = value.get("operation") == "capabilities"
    if metadata:
        request = value
        if dispatch != {
            "run_id": request["run_id"],
            "at": dispatch.get("at"),
            "request_sha256": digest(json_bytes(request)),
        }:
            raise ExecutionError("request_changed", "Metadata dispatch request differs")
        portable = None
    else:
        portable = ExecutionRequest.from_data(value)
        request = read_receipt(directory / "context.json")
        expected = {
            "run_id": portable.dispatch_id,
            "at": dispatch.get("at"),
            "request_sha256": portable.request_sha256,
            "request_file_sha256": digest(json_bytes(value)),
            "context_sha256": digest(json_bytes(request)),
        }
        if dispatch != expected:
            raise ExecutionError("request_changed", "Portable request or local custody changed")
    write_receipt(
        directory / "started.json",
        {"pid": os.getpid(), "run_id": request["run_id"], "at": timestamp()},
    )
    if request.get("operation") == "capabilities":
        return execute_capability_refresh(directory, request)
    cwd = Path(request["cwd"])
    backend = _open_backend(directory.parents[2], project_root=cwd)
    outcome = {
        "version": 1,
        "run_id": request["run_id"],
        "state": "reconciliation_required",
        "acceptance": "not_granted",
        "local_children_stopped": False,
    }
    remote_verified = False
    integration_complete = False
    try:
        registration = ExecutionAdapter(**request["adapter"])
        adapter = adapter_module(registration)
        live_claim(backend, request, remaining=request["wall_seconds"] + 180)
        clean_source(cwd, request["source_sha"])
        packet = (directory / "packet.txt").read_bytes()
        if digest(packet) != request["packet_sha256"]:
            raise ExecutionError("packet_changed", "Native packet changed before execution")
        derived = read_receipt(directory / "adapter-task.json")
        if derived != assignment_for(request, packet):
            raise ExecutionError(
                "request_changed", "Derived assignment differs from native context"
            )
        expected_request = request_for(request, packet, derived)
        if dataclasses.asdict(portable) != dataclasses.asdict(expected_request):
            raise ExecutionError(
                "request_changed", "Current native source, preimages or assignment differ"
            )
        adapter.validate_task(derived)
        output = directory / "adapter"
        deadline = time.monotonic() + request["wall_seconds"] + 120
        if load_execution_adapter() != registration:
            raise ExecutionError("adapter_changed", "Registration changed before dispatch")
        capture(
            [
                registration.interpreter,
                "-I",
                registration.script,
                "run",
                str(directory / "adapter-task.json"),
                "--profile",
                registration.profile,
                "--output",
                str(output),
            ],
            cwd,
            directory,
            "adapter",
            deadline,
            4 * 1024 * 1024,
        )
        live_claim(backend, request)
        clean_source(cwd, request["source_sha"])
        if load_execution_adapter() != registration:
            raise ExecutionError("adapter_changed", "Registration changed during execution")
        changes, source_files = verified_changes(adapter, output, derived, registration)
        returned = portable_adapter_result(adapter, output, registration, portable, changes)
        write_receipt(directory / "adapter-result.json", dataclasses.asdict(returned))
        verify_portable_return(returned, portable, adapter, output, registration, changes)
        remote_verified = True
        receipt = read_receipt(output / "receipt.json")
        outcome["adapter_run_id"] = receipt["run_id"]
        if isinstance(receipt["worker"].get("configuration"), dict):
            save_execution_capabilities(
                directory.parents[1],
                receipt["worker"]["configuration"],
                registration,
                digest((output / "receipt.json").read_bytes()),
            )
        if not changes:
            raise ExecutionError("no_changes", "Implementation returned no scoped source changes")
        live_claim(backend, request)
        clean_source(cwd, request["source_sha"])
        for preimage in portable.writable_paths:
            path = checked_path(cwd, preimage["path"])
            actual_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
            if actual_mode != preimage["host_mode"]:
                raise ExecutionError("source_changed", "Host file mode changed before integration")
        integrate(cwd, output, changes, source_files, directory)
        integration_complete = True
        modes = read_receipt(directory / "integration.json")["modes"]
        proofs = []
        proof_paths = []
        for index, command in enumerate(request["commands"]):
            claim = live_claim(backend, request)
            verify_portable_return(
                AdapterResult.from_data(read_receipt(directory / "adapter-result.json"), portable),
                portable,
                adapter,
                output,
                registration,
                changes,
            )
            check_integrated_source(cwd, request["source_sha"], changes, modes)
            environment = os.environ.copy()
            environment.update(
                ANVIL_ROOT=str(cwd),
                ANVIL_ACTOR=claim.claimed_by,
                ANVIL_CLAIM_ID=claim.id,
                ANVIL_PRD=claim.attestation_context.prd_id,
            )
            result = capture(
                ["/bin/sh", "-c", command],
                cwd,
                directory,
                f"check-{index}",
                min(deadline, time.monotonic() + portable.verification[index]["timeout_seconds"]),
                portable.budgets["command_output_bytes"],
                environment,
            )
            raw = (directory / f"check-{index}.log").read_bytes()
            context = claim.attestation_context
            core = ClaimCommandEvidenceCore(
                schema_version=1,
                project_id=request["project_id"],
                claim_id=claim.id,
                generation=claim.generation,
                claimed_by=claim.claimed_by,
                task_id=claim.task_id,
                task_revision=context.task_revision,
                prd_id=context.prd_id,
                prd_revision=context.prd_revision,
                repository_id=context.repository_id,
                claim_start_sha=context.claim_start_sha,
                cwd_relative=".",
                cwd_identity=claim_command_cwd_identity(cwd, context.repository_id, "."),
                command_base64=base64.b64encode(command.encode()).decode(),
                started_at=result["started_at"],
                ended_at=result["ended_at"],
                exit_code=result["exit_code"],
                output_base64=base64.b64encode(raw).decode(),
                output_sha256=digest(raw),
            )
            artifact = canonical_json_bytes(
                {
                    "envelope_id": request["run_id"] + f"-{index}",
                    "payload": core.model_dump(mode="json"),
                    "issuer": None,
                },
                max_bytes=262144,
                max_string_bytes=200000,
            )
            proofs.append(load_claim_command_proof(artifact))
            path = directory / f"proof-{index}.json"
            with path.open("xb") as stream:
                os.chmod(path, 0o600)
                stream.write(artifact)
                stream.flush()
                os.fsync(stream.fileno())
            proof_paths.append(path)
        claim = live_claim(backend, request)
        verify_portable_return(
            AdapterResult.from_data(read_receipt(directory / "adapter-result.json"), portable),
            portable,
            adapter,
            output,
            registration,
            changes,
        )
        check_integrated_source(cwd, request["source_sha"], changes, modes)
        verify_claim_command_proof_batch(
            proofs,
            claim=claim,
            task=backend.get_task(claim.task_id),
            project_id=request["project_id"],
            project_root=cwd,
            actor=request["actor"],
            declared_commands=request["commands"],
            now=now(),
        )
        arguments = ["submit", request["task_id"], "--actor", request["actor"], "--json"]
        for command in request["commands"]:
            arguments.extend(["--commands", command])
        for change in changes:
            arguments.extend(["--files-changed", change["path"]])
        for path in proof_paths:
            arguments.extend(["--command-proof-file", str(path)])
        write_receipt(directory / "submission.json", {"argv": arguments, "at": timestamp()})
        submitted = native_cli(cwd, *arguments)
        write_receipt(directory / "submitted.json", json.loads(submitted))
        outcome.update(
            state="needs_review",
            local_children_stopped=True,
            changed_files=[change["path"] for change in changes],
        )
    except Exception as error:
        outcome.update(
            error_code=getattr(error, "code", "execution_failed"), error=str(error)[:4000]
        )
        # Only known stopped checks may admit a fresh reviewed request. Partial
        # integration, uncertain submit and unknown remote custody remain held.
        capture_results = list(directory.glob("*.result.json"))
        all_stopped = bool(capture_results) and all(
            read_receipt(path).get("group_absent") is True for path in capture_results
        )
        if (
            remote_verified
            and integration_complete
            and all_stopped
            and not (directory / "submission.json").exists()
        ):
            outcome.update(state="failed_stopped", local_children_stopped=True)
        else:
            outcome["state"] = "reconciliation_required"
    finally:
        backend.close()
        outcome["ended_at"] = timestamp()
        write_receipt(directory / "result.json", outcome)
    return outcome


def main() -> int:
    if sys.argv[1:] == ["--validate-adapter"]:
        registration = load_execution_adapter()
        adapter_module(registration)
        sys.stdout.buffer.write(json_bytes(dataclasses.asdict(registration)))
        return 0
    result = execute_run(Path(sys.argv[1]))
    return 0 if result["state"] in {"needs_review", "capabilities_ready"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
