"""Owned Run worker: verified returned bytes, real checkout proofs, native submit."""

from __future__ import annotations

import base64
import contextlib
import dataclasses
import inspect
import json
import os
import re
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
from anvil.config import ExecutionAdapter, execution_file_bytes, load_execution_adapter
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
    revalidate_continuation,
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


CAPTURE_CLEANUP_SECONDS = 2.0
CAPTURE_STATUS_MAXIMUM = 128
# Product-owned counterpart of the native benchmark subreaper semantics. It
# uses only stdlib and its own adopted children; installed code imports no tests
# or benchmarks. The status pipe is never inherited by the actual command.
_CAPTURE_SUPERVISOR = r"""
import ctypes
import os
import signal
import subprocess
import sys
import time

status_fd, expected_parent = int(sys.argv[1]), int(sys.argv[2])
os.set_inheritable(status_fd, False)
stopping = False
cleanup_requested = False
def stop(signum, frame):
    global stopping
    stopping = True
def request_cleanup(signum, frame):
    global cleanup_requested
    cleanup_requested = True
def notify(raw):
    try:
        os.write(status_fd, raw)
    except OSError:
        pass
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGUSR1, request_cleanup)
signal.signal(signal.SIGCHLD, signal.SIG_DFL)
libc = ctypes.CDLL(None, use_errno=True)
if libc.prctl(36, 1, 0, 0, 0) != 0 or libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
    raise SystemExit(126)
if os.getppid() != expected_parent:
    stopping = True
children_path = f'/proc/self/task/{os.getpid()}/children'
def owned_children():
    with open(children_path, encoding='ascii') as stream:
        return [int(value) for value in stream.read().split()]

child = None
exit_notified = False
complete = False
try:
    owned_children()  # Refuse unsupported custody before starting the command.
    if not stopping:
        child = subprocess.Popen(sys.argv[3:])
        notify(f'started:{child.pid}\n'.encode('ascii'))
        while child.returncode is None and not stopping:
            try:
                child.wait(timeout=.02)
            except subprocess.TimeoutExpired:
                pass
        if child.returncode is not None:
            notify(f'exited:{child.returncode}\n'.encode('ascii'))
            exit_notified = True
            for fd in (1, 2):
                try:
                    os.close(fd)
                except OSError:
                    pass
            # Inherited command pipes must reach the caller's deadline instead
            # of being silently closed by early descendant cleanup.
            while not stopping and not cleanup_requested:
                time.sleep(.01)
finally:
    limit = time.monotonic() + 1.5
    while time.monotonic() < limit:
        for pid in owned_children():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        no_children = False
        while True:
            try:
                waited, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                no_children = True
                break
            if waited == 0:
                break
            if child is not None and waited == child.pid:
                child.returncode = os.waitstatus_to_exitcode(status)
        # Killing/waiting an immediate child can adopt further detached
        # descendants. Require both ECHILD and a fresh empty kernel child list.
        if no_children and not owned_children():
            complete = True
            break
        time.sleep(.01)
    if child is not None and child.returncode is not None and not exit_notified:
        notify(f'exited:{child.returncode}\n'.encode('ascii'))
    if complete:
        notify(b'stopped\n')
    os.close(status_fd)
if not complete:
    raise SystemExit(126)
"""


def require_linux_capture() -> None:
    if sys.platform != "linux" or not Path("/proc/self/task").is_dir():
        raise ExecutionError(
            "unsupported_platform", "Owned Run requires Linux subreaper descendant custody"
        )
    if signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL:
        raise ExecutionError(
            "unsupported_wait_status", "Owned capture requires observable child wait status"
        )
    try:
        os.killpg(os.getpgrp(), 0)
    except OSError as error:
        raise ExecutionError(
            "unsupported_custody", "Cannot observe owned process groups for Linux capture"
        ) from error


def capture_status(raw: bytes) -> tuple[int | None, int | None, bool]:
    match = re.fullmatch(
        rb"(?:started:([1-9][0-9]{0,19})\n)?(?:exited:(-?[0-9]{1,10})\n)?(stopped\n)?",
        raw,
    )
    if len(raw) > CAPTURE_STATUS_MAXIMUM or match is None:
        raise ExecutionError("containment_unknown", "Invalid owned capture status")
    pid, exit_code, stopped = match.groups()
    if exit_code is not None and (pid is None or not -64 <= int(exit_code) <= 255):
        raise ExecutionError("containment_unknown", "Invalid owned command completion")
    return int(pid) if pid else None, int(exit_code) if exit_code else None, stopped is not None


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
    """Bounded command bytes and Linux descendant custody, including setsid."""
    require_linux_capture()
    started = timestamp()
    attempt = {
        "argv": argv, "cwd": str(cwd), "started_at": started,
        "supervisor_sha256": digest(_CAPTURE_SUPERVISOR.encode()),
        "capture_protocol": "linux-descendant-capture.v1",
    }
    write_receipt(directory / (name + ".attempt.json"), attempt)
    output = bytearray()
    stdout = bytearray()
    status = bytearray()
    failure = None
    supervisor = None
    cleanup_requested = False
    forced_stop = False
    status_reader, status_writer = os.pipe()
    os.set_blocking(status_reader, False)
    selector = selectors.DefaultSelector()
    try:
        supervisor = subprocess.Popen(
            [sys.executable, "-I", "-c", _CAPTURE_SUPERVISOR,
             str(status_writer), str(os.getpid()), *argv],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE if separate_stdout else subprocess.STDOUT,
            env=environment,
            pass_fds=(status_writer,),
            start_new_session=True,
        )
        os.close(status_writer)
        status_writer = None
        write_receipt(
            directory / (name + ".started.json"),
            {**attempt, "pid": None, "supervisor_pid": supervisor.pid},
        )
        selector.register(supervisor.stdout, selectors.EVENT_READ, "stdout")
        if separate_stdout:
            selector.register(supervisor.stderr, selectors.EVENT_READ, "stderr")
        selector.register(status_reader, selectors.EVENT_READ, "status")
        while selector.get_map():
            if time.monotonic() >= deadline:
                failure = "command_timeout"
                break
            for key, _ in selector.select(min(0.1, max(0, deadline - time.monotonic()))):
                target = status if key.data == "status" else output
                limit = CAPTURE_STATUS_MAXIMUM if key.data == "status" else maximum
                chunk = os.read(key.fd, min(65536, limit + 1 - len(target)))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                target.extend(chunk)
                if separate_stdout and key.data == "stdout":
                    stdout.extend(chunk)
                if len(target) > limit:
                    failure = "containment_unknown" if key.data == "status" else "output_overflow"
                    break
            if failure:
                break
            output_open = any(key.data != "status" for key in selector.get_map().values())
            if not output_open and b"\nexited:" in status and status.endswith(b"\n"):
                capture_status(status)
                if not cleanup_requested:
                    with contextlib.suppress(ProcessLookupError):
                        supervisor.send_signal(signal.SIGUSR1)
                    cleanup_requested = True
                    deadline = time.monotonic() + CAPTURE_CLEANUP_SECONDS
        if failure is None:
            supervisor.wait(timeout=max(0.01, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        failure = "command_timeout"
    except BaseException:
        failure = "capture_interrupted"
        raise
    finally:
        selector.close()
        if status_writer is not None:
            os.close(status_writer)
        if supervisor is not None:
            if supervisor.poll() is None:
                with contextlib.suppress(OSError):
                    supervisor.terminate()
                try:
                    supervisor.wait(timeout=CAPTURE_CLEANUP_SECONDS)
                except subprocess.TimeoutExpired:
                    forced_stop = True
                    with contextlib.suppress(OSError):
                        os.killpg(supervisor.pid, signal.SIGKILL)
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        supervisor.wait(timeout=5)
            while len(status) <= CAPTURE_STATUS_MAXIMUM:
                try:
                    chunk = os.read(status_reader, CAPTURE_STATUS_MAXIMUM + 1 - len(status))
                except BlockingIOError:
                    break
                if not chunk:
                    break
                status.extend(chunk)
            supervisor.stdout.close()
            if supervisor.stderr is not None:
                supervisor.stderr.close()
        os.close(status_reader)
        command_pid = exit_code = None
        contained = False
        try:
            command_pid, exit_code, contained = capture_status(status)
        except ExecutionError:
            failure = failure or "containment_unknown"
        reaped = supervisor is not None and supervisor.returncode is not None
        try:
            stopped = supervisor is not None and process_group_absent(supervisor.pid)
        except OSError:
            stopped = False
        contained = bool(
            contained and reaped and stopped and supervisor.returncode == 0
            and command_pid is not None and exit_code is not None and not forced_stop
        )
        if not contained:
            failure = failure or "containment_unknown"
        elif command_pid is None or exit_code is None:
            failure = failure or "command_start_failed"
        elif not cleanup_requested:
            failure = failure or "capture_interrupted"
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
            **attempt,
            "pid": command_pid,
            "command_reaped": exit_code is not None,
            "supervisor_pid": supervisor.pid if supervisor is not None else None,
            "supervisor_exit_code": supervisor.returncode if supervisor is not None else None,
            "supervisor_reaped": reaped,
            "containment_complete": contained,
            "ended_at": timestamp(),
            "exit_code": exit_code,
            "output_sha256": digest(output),
            "output_bytes": len(output),
            "failure": failure,
            "group_absent": stopped,
        }
        write_receipt(directory / (name + ".result.json"), result)
    if failure or not contained or (require_success and exit_code):
        raise ExecutionError(
            failure or "command_failed", "Owned command failed; inspect retained output and custody"
        )
    return result


def captures_stopped(directory: Path, expected: list[str]) -> bool:
    """Missing current custody cannot be replaced by earlier successful captures."""
    if not expected or len(expected) != len(set(expected)) or {
        path.name.removesuffix(".started.json") for path in directory.glob("*.started.json")
    } != set(expected):
        return False
    fields = {"argv", "cwd", "started_at", "supervisor_sha256", "capture_protocol"}
    try:
        for name in expected:
            attempt = read_receipt(directory / (name + ".attempt.json"))
            started = read_receipt(directory / (name + ".started.json"))
            result = read_receipt(directory / (name + ".result.json"))
            if any(type(value) is not dict or not fields <= value.keys()
                   for value in (attempt, started, result)):
                return False
            if any(attempt[key] != started[key] or attempt[key] != result[key] for key in fields):
                return False
            if (attempt["capture_protocol"] != "linux-descendant-capture.v1"
                    or attempt["supervisor_sha256"] != digest(_CAPTURE_SUPERVISOR.encode())
                    or type(result.get("pid")) is not int or result["pid"] <= 1
                    or type(result.get("supervisor_pid")) is not int
                    or result["supervisor_pid"] <= 1
                    or result["supervisor_pid"] != started.get("supervisor_pid")
                    or result["supervisor_pid"] == result["pid"]
                    or type(result.get("exit_code")) is not int
                    or not -64 <= result["exit_code"] <= 255
                    or type(result.get("supervisor_exit_code")) is not int
                    or result["supervisor_exit_code"] != 0
                    or any(result.get(key) is not True for key in (
                        "command_reaped", "supervisor_reaped", "group_absent",
                        "containment_complete"))
                    or not process_group_absent(result["supervisor_pid"])):
                return False
    except (OSError, ValueError, TypeError):
        return False
    return True


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
    adapter: Any,
    output: Path,
    derived: dict[str, Any],
    registration: ExecutionAdapter,
    request: ExecutionRequest,
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
        receipt.get("version") != (3 if request.schema == "anvil.execution-request.v2" else 2)
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
        "version": 3 if request.schema == "anvil.execution-request.v2" else 2,
        "operation": request.operation,
        "max_tool_calls": request.budgets["max_tool_calls"],
        "max_turns": request.budgets["max_turns"],
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
    if request.schema == "anvil.execution-request.v2":
        adapter.validate_native_task(native_task)
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


def verified_saved_conversation(
    adapter: Any, output: Path, request: ExecutionRequest, receipt: dict[str, Any]
) -> dict[str, Any]:
    """Project the pinned adapter's verified checkpoint, never parse a transcript."""
    native_task = retained_adapter_task(output, receipt)
    if native_task.get("version") != 3 or native_task.get("operation") != request.operation:
        raise ExecutionError("result_mismatch", "Saved operation differs from the request")
    checkpoint = adapter.verify_conversation(output / "results", native_task, receipt["worker"])
    conversation = receipt["worker"]["conversation"]
    checkpoint_sha256 = digest((output / "results" / "conversation.json").read_bytes())
    if checkpoint_sha256 != conversation["checkpoint_sha256"]:
        raise ExecutionError("result_mismatch", "Retained checkpoint bytes differ from receipt")
    prior = request.continuation
    if prior is not None and (
        checkpoint["conversation_id"] != prior["conversation_id"]
        or checkpoint["prior_run_id"] != prior["prior_adapter_run_id"]
        or checkpoint["prior_checkpoint_sha256"] != prior["checkpoint_sha256"]
        or checkpoint["session"]["id"] != prior["official_session_id"]
        or conversation["session_before"]["sha256"] != prior["session_sha256"]
    ):
        raise ExecutionError("result_mismatch", "Saved return differs from prior dispatch lineage")

    def session_identity(value: dict[str, Any] | None) -> dict[str, Any] | None:
        return (
            {key: value[key] for key in ("bytes", "sha256", "header_sha256")}
            if value is not None else None
        )

    return {
        "conversation_id": checkpoint["conversation_id"],
        "official_session_id": checkpoint["session"]["id"],
        "checkpoint_sha256": checkpoint_sha256,
        "prior_adapter_run_id": checkpoint["prior_run_id"],
        "prior_checkpoint_sha256": checkpoint["prior_checkpoint_sha256"],
        "session_before": session_identity(conversation["session_before"]),
        "session_after": session_identity(conversation["session_after"]),
        "prior_prefix_sha256": conversation["prior_prefix_sha256"],
        "workspace_sha256": digest(json_bytes(checkpoint["workspace"])),
        "continuable": conversation["continuable"],
    }


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


def verified_stopped_owner(receipt: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    """Join returned lifecycle facts to the exact registered disposable owner."""
    states = [receipt.get(name) for name in ("sandbox_before", "sandbox_running", "sandbox_after")]
    if any(type(value) is not dict for value in states):
        raise ExecutionError("result_mismatch", "Missing returned owner identities")
    before, running, after = states
    main_instance = running.get("main_process_instance_id")
    if (
        any(value.get("id") != profile["sandbox_id"] for value in states)
        or before.get("phase") != "Stopped"
        or running.get("phase") != "Ready"
        or after.get("phase") != "Stopped"
        or type(main_instance) is not str
        or not 1 <= len(main_instance) <= 256
        or after.get("main_process_instance_id") != main_instance
        or receipt.get("cleanup_complete") is not True
        or receipt.get("protected_unchanged") is not True
    ):
        raise ExecutionError(
            "result_mismatch", "Returned owner identities do not prove exact stopped custody"
        )
    return after


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
    after = verified_stopped_owner(receipt, profile)
    actual = adapter.snapshot(output / "results")
    raw_digest = digest((output / "receipt.json").read_bytes())
    value = {
        "schema": (
            "anvil.adapter-result.v2"
            if request.schema == "anvil.execution-request.v2" else "anvil.adapter-result.v1"
        ),
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
        "saved_conversation": (
            verified_saved_conversation(adapter, output, request, receipt)
            if request.schema == "anvil.execution-request.v2" else None
        ),
        "error_code": None,
    }
    if (request.schema == "anvil.execution-request.v1"
            and configuration.get("session_file") is not None):
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
        selected_model = request.get("selected_model")
        arguments = [
            registration.interpreter,
            "-I",
            registration.script,
            "capabilities",
            "--profile",
            registration.profile,
            "--output",
            str(output),
        ]
        if selected_model is not None:
            arguments.extend([
                "--provider", selected_model["provider"], "--model", selected_model["model"],
            ])
        capture(
            arguments,
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
        verified_stopped_owner(receipt, profile)
        expected = {
            "version": 3 if selected_model is not None else 2,
            "task_id": "capabilities",
            "kind": "validation",
            "operation": "capabilities_selected" if selected_model is not None else "capabilities",
            "writable_paths": [],
            "checks": [],
            "run_options": {},
            "source": {"kind": "empty-workspace", "files": {}, "modes": {}},
            "runtime_defaults": {
                "model": profile["model"],
                "thinking": profile.get("thinking", "high"),
            },
        }
        if selected_model is not None:
            expected["selected_model"] = selected_model
            adapter.validate_native_task(native_task)
        if any(native_task.get(key) != value for key, value in expected.items()):
            raise ExecutionError("result_mismatch", "Metadata task changed its no-prompt scope")
        if (
            receipt.get("acceptance") != "not_granted"
            or receipt.get("outcome") != "capabilities"
            or receipt.get("version") != expected["version"]
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
    failed_check: tuple[str, str] | None = None
    attempted_captures: list[str] = []
    try:
        registration = ExecutionAdapter(**request["adapter"])
        adapter = adapter_module(registration)
        live_claim(backend, request, remaining=request["wall_seconds"] + 180)
        clean_source(cwd, request["source_sha"])
        previous_output = revalidate_continuation(backend, request)
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
        arguments = [
            registration.interpreter,
            "-I",
            registration.script,
            portable.operation,
            str(directory / "adapter-task.json"),
            "--profile",
            registration.profile,
            "--output",
            str(output),
        ]
        if portable.schema == "anvil.execution-request.v2":
            if portable.operation == "continue":
                if previous_output is None:
                    raise ExecutionError("invalid_continuation", "Missing verified prior output")
                arguments.extend(["--from", str(previous_output)])
            else:
                arguments.append("--save-session")
        attempted_captures.append("adapter")
        capture(
            arguments,
            cwd,
            directory,
            "adapter",
            deadline,
            4 * 1024 * 1024,
        )
        live_claim(backend, request)
        clean_source(cwd, request["source_sha"])
        revalidate_continuation(backend, request)
        if load_execution_adapter() != registration:
            raise ExecutionError("adapter_changed", "Registration changed during execution")
        changes, source_files = verified_changes(adapter, output, derived, registration, portable)
        returned = portable_adapter_result(adapter, output, registration, portable, changes)
        write_receipt(directory / "adapter-result.json", dataclasses.asdict(returned))
        verify_portable_return(returned, portable, adapter, output, registration, changes)
        remote_verified = True
        receipt = read_receipt(output / "receipt.json")
        outcome["adapter_run_id"] = receipt["run_id"]
        # Selected options, protocol and stopped custody were verified above.
        # Optional chooser metadata cannot strand that completed task delivery.
        try:
            save_execution_capabilities(
                directory.parents[1],
                receipt["worker"]["configuration"],
                registration,
                digest((output / "receipt.json").read_bytes()),
            )
        except (ExecutionError, OSError, ValueError):
            outcome["capability_cache"] = "unavailable"
        else:
            outcome["capability_cache"] = "updated"
        if not changes:
            raise ExecutionError("no_changes", "Implementation returned no scoped source changes")
        live_claim(backend, request)
        clean_source(cwd, request["source_sha"])
        revalidate_continuation(backend, request)
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
            revalidate_continuation(backend, request)
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
            attempted_captures.append(f"check-{index}")
            try:
                result = capture(
                    ["/bin/sh", "-c", command],
                    cwd,
                    directory,
                    f"check-{index}",
                    min(
                        deadline, time.monotonic() + portable.verification[index]["timeout_seconds"]
                    ),
                    portable.budgets["command_output_bytes"],
                    environment,
                )
            except ExecutionError as error:
                if error.code in {"command_failed", "command_timeout", "output_overflow"}:
                    failed_check = (f"check-{index}", error.code)
                raise
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
        revalidate_continuation(backend, request)
        verify_portable_return(
            AdapterResult.from_data(read_receipt(directory / "adapter-result.json"), portable),
            portable,
            adapter,
            output,
            registration,
            changes,
        )
        check_integrated_source(cwd, request["source_sha"], changes, modes)
        if not captures_stopped(directory, attempted_captures):
            raise ExecutionError("containment_unknown", "Capture custody changed before submission")
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
        all_stopped = captures_stopped(directory, attempted_captures)
        if (
            remote_verified
            and integration_complete
            and failed_check is not None
            and all_stopped
            and not (directory / "submission.json").exists()
        ):
            try:
                name, failure_code = failed_check
                captured = read_receipt(directory / f"{name}.result.json")
                if (
                    failure_code == "command_failed"
                    and (captured["failure"] is not None or captured["exit_code"] == 0)
                    or failure_code != "command_failed" and captured["failure"] != failure_code
                ):
                    raise ExecutionError("containment_unknown", "Failed check receipt differs")
                live_claim(backend, request)
                revalidate_continuation(backend, request)
                verify_portable_return(
                    AdapterResult.from_data(
                        read_receipt(directory / "adapter-result.json"), portable
                    ),
                    portable, adapter, output, registration, changes,
                )
                check_integrated_source(cwd, request["source_sha"], changes, modes)
            except Exception as revalidation_error:
                # A stopped process is not a settled delivery when source or authority drifted.
                outcome["reconciliation_error_code"] = getattr(
                    revalidation_error, "code", "revalidation_failed"
                )
            else:
                outcome.update(state="failed_stopped", local_children_stopped=True)
    finally:
        backend.close()
        outcome["ended_at"] = timestamp()
        write_receipt(directory / "result.json", outcome)
    return outcome


def validate_saved_adapter_contract(adapter: Any, parent: Path) -> None:
    """Probe the declared saved contract locally; never start an owner or a prompt."""
    for name, arguments, keywords in (
        ("run_task", (None, None, None, None),
         {"save_session": True, "previous_directory": None, "selected_model": None}),
        ("continuation_checkpoint", (None, None), {}),
        ("verify_conversation", (None, None, None), {}),
        ("saved_descriptor", (None,), {}),
        ("validate_native_task", (None,), {}),
        ("exclusive_owner", (None, None), {}),
    ):
        function = getattr(adapter, name, None)
        if not callable(function):
            raise ExecutionError("adapter_unavailable", "Missing saved adapter contract operation")
        try:
            inspect.signature(function).bind(*arguments, **keywords)
        except (TypeError, ValueError) as error:
            raise ExecutionError(
                "adapter_unavailable", "Saved adapter signature differs"
            ) from error
    run_id = "a" * 32
    root = parent.absolute() / "contract-probe"
    conversation = root / "conversations" / run_id
    saved = {
        "version": 3,
        "run_id": run_id,
        "task_id": "contract:probe",
        "kind": "implementation",
        "instructions": "Validate a contract without execution.",
        "workspace": str(conversation / "workspace"),
        "output_directory": str(root / run_id / "results"),
        "writable_paths": [],
        "allowed_tools": [],
        "max_tool_calls": 1,
        "max_turns": 1,
        "source": {"kind": "git", "commit": "1" * 40, "files": {}, "modes": {}},
        "checks": [],
        "operation": "run",
        "run_options": {},
        "runtime_defaults": {"model": "synthetic/model", "thinking": "off"},
        "session": {
            "conversation_id": run_id,
            "directory": str(conversation),
            "file": str(conversation / "session.jsonl"),
            "prior": None,
            "prior_sha256": None,
        },
    }
    selected = {
        **{key: value for key, value in saved.items() if key != "session"},
        "operation": "capabilities_selected",
        "selected_model": {"provider": "synthetic", "model": "model"},
    }
    adapter.validate_native_task(saved)
    adapter.saved_descriptor(saved)
    adapter.validate_native_task(selected)
    prior = {"conversation_id": run_id, "run_id": "b" * 32}
    prior_raw = (json.dumps(prior, sort_keys=True, separators=(",", ":")) + "\n").encode()
    continued = {
        **saved,
        "operation": "continue",
        "session": {**saved["session"], "prior": prior, "prior_sha256": digest(prior_raw)},
    }
    adapter.validate_native_task(continued)
    adapter.saved_descriptor(continued)
    # Positive validation alone must not mistake a permissive old stub for v3 support.
    for invalid in (
        {**saved, "session": None},
        {**saved, "session": {**saved["session"], "conversation_id": "b" * 32}},
        {**continued, "session": {**continued["session"], "prior_sha256": "0" * 64}},
        {**selected, "selected_model": {"provider": "synthetic"}},
        {**selected, "run_options": {"thinking": "low"}},
    ):
        try:
            adapter.validate_native_task(invalid)
        except (TypeError, ValueError, KeyError):
            continue
        raise ExecutionError("adapter_unavailable", "Adapter accepts an invalid saved contract")


OWNER_READ_TIMEOUT_SECONDS = 30


def validated_owner_state(value: Any) -> dict[str, Any]:
    """Bound the private read-only owner projection, never the full owner payload."""
    names = {"id", "name", "phase", "main_process_instance_id"}
    if (type(value) is not dict or set(value) != names | {"restart_count"}
            or any(type(value[name]) is not str
                   or not 1 <= len(value[name].encode("utf-8")) <= 1024 for name in names)
            or type(value["restart_count"]) is not int
            or not 0 <= value["restart_count"] <= 2**63 - 1
            or len(json_bytes(value)) > 4096):
        raise ExecutionError("owner_unavailable", "Invalid stopped owner readback")
    return value


def verify_candidate_owner(registration: ExecutionAdapter, directory: Path) -> dict[str, Any]:
    """Run the pinned owner's verifier with a bounded, read-only command transport."""
    adapter = adapter_module(registration, require_registered=False)
    profile = adapter.profile_settings(Path(registration.profile))
    owner = adapter.OpenShellOwner(profile)
    directory.mkdir(mode=0o700)
    execution_file_bytes(directory / "owner-get.attempt.json", 0, missing_ok=True)

    def bounded_owner_command(*arguments: str, timeout: int = 60) -> subprocess.CompletedProcess:
        if arguments != ("get", profile["sandbox_name"], "-o", "json") or timeout != 30:
            raise ExecutionError("owner_unavailable", "Owner verifier requested another operation")
        argv = [*owner.argv, "sandbox", *arguments]
        result = capture(
            argv, directory, directory, "owner-get",
            time.monotonic() + OWNER_READ_TIMEOUT_SECONDS, 65536, separate_stdout=True,
        )
        stdout = execution_file_bytes(directory / "owner-get.stdout", 65536).decode("utf-8")
        return subprocess.CompletedProcess(argv, result["exit_code"], stdout, "")

    # Preserve the pinned verify/get identity semantics, replacing only its
    # unbounded subprocess.run(capture_output=True) transport on this instance.
    owner.command = bounded_owner_command
    return validated_owner_state(adapter.selected_state(owner.verify()))


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "--verify-owner":
        if (len(sys.argv) != 6 or sys.argv[2] != "--candidate"
                or sys.argv[4] != "--output"):
            raise ExecutionError("invalid_contract", "Invalid owner verification arguments")
        registration = ExecutionAdapter(**json.loads(
            execution_file_bytes(Path(sys.argv[3]), 1024 * 1024)
        ))
        state = verify_candidate_owner(registration, Path(sys.argv[5]))
        sys.stdout.buffer.write(json_bytes(state))
        return 0
    if len(sys.argv) >= 2 and sys.argv[1] == "--validate-adapter":
        candidate = len(sys.argv) == 4 and sys.argv[2] == "--candidate"
        if not candidate and len(sys.argv) not in {2, 3}:
            raise ExecutionError("invalid_contract", "Invalid adapter validation arguments")
        if candidate:
            path = Path(sys.argv[3])
            registration = ExecutionAdapter(**json.loads(execution_file_bytes(path, 1024 * 1024)))
        else:
            registration = load_execution_adapter()
        adapter = adapter_module(registration, require_registered=not candidate)
        if registration.registration["capabilities"]["saved_session_continue"]:
            validate_saved_adapter_contract(adapter, Path(registration.script).parent)
        if not candidate and len(sys.argv) == 3:
            assignment = read_receipt(Path(sys.argv[2]))
            adapter.validate_task(assignment)
            adapter.frozen_source(assignment)
        sys.stdout.buffer.write(json_bytes(dataclasses.asdict(registration)))
        return 0
    result = execute_run(Path(sys.argv[1]))
    return 0 if result["state"] in {"needs_review", "capabilities_ready"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
