"""Bounded native Run admission and durable, State-local execution receipts.

The journal is operational custody, not a second task reducer. Only native
claim and submit commands change task state. Uncertain admission never retries.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
import types
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from anvil.actors import canonicalize_new_actor
from anvil.config import ExecutionAdapter, execution_file_bytes, load_execution_adapter
from anvil.state.hashing import canonical_json_bytes
from anvil.state.models import Claim, Task


class ExecutionError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def now() -> datetime:
    return datetime.now(UTC)


def timestamp(value: datetime | None = None) -> str:
    return (value or now()).astimezone(UTC).isoformat().replace("+00:00", "Z")


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def json_bytes(value: Any) -> bytes:
    return canonical_json_bytes(
        value, max_bytes=2 * 1024 * 1024, max_nodes=100000, max_string_bytes=2 * 1024 * 1024
    )


def private_directory(path: Path) -> Path:
    path = path.absolute()
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ExecutionError("unsafe_path", "Execution directories cannot contain symbolic links")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = path.stat()
    if not stat.S_ISDIR(metadata.st_mode) or (
        os.name == "posix" and (metadata.st_uid != os.getuid() or metadata.st_mode & 0o077)
    ):
        raise ExecutionError("unsafe_path", "Execution directory must be private to its owner")
    return path


def sync_directory(path: Path) -> None:
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def pending_receipt_path(path: Path) -> Path:
    return path.with_name("." + path.name + ".pending")


def receipt_attempt_exists(path: Path) -> bool:
    return os.path.lexists(path) or os.path.lexists(pending_receipt_path(path))


def write_receipt(path: Path, value: Any) -> None:
    """Publish complete bytes without replacement; failed staging forbids retry."""
    private_directory(path.parent)
    if os.path.lexists(path):
        raise FileExistsError(str(path))
    pending = pending_receipt_path(path)
    descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        # Keep this exclusive attempt after any failure, including serialization.
        sync_directory(path.parent)
        stream.write(json_bytes(value))
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.link(pending, path)
    except FileExistsError:
        # Another completed publisher can win between the initial check and
        # staging. This known duplicate must not poison a reusable slot lock.
        pending.unlink()
        sync_directory(path.parent)
        raise
    sync_directory(path.parent)
    pending.unlink()
    sync_directory(path.parent)


def read_receipt(path: Path, maximum: int = 1024 * 1024) -> Any:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
        raise ExecutionError("invalid_receipt", "Expected a bounded regular execution receipt")
    return json.loads(path.read_bytes())


def identifier(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{32}", value):
        raise ExecutionError("invalid_run", "Invalid execution receipt identifier")
    return value


def relative_file(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or str(path) != value
        or ".." in path.parts
        or any(part.startswith(".") and part in {".git", ".anvil"} for part in path.parts)
        or re.search(r"[\\,:*?\[\]\x00-\x1f]", value)
    ):
        raise ExecutionError("unsupported_scope", "Run requires exact portable relative file paths")
    return value


@dataclasses.dataclass(frozen=True)
class RunOptions:
    provider: str | None = None
    model: str | None = None
    thinking: str | None = None

    def __post_init__(self) -> None:
        if (self.provider is None) != (self.model is None):
            raise ExecutionError(
                "unsupported_options", "Provider and model must be supplied together"
            )
        if self.provider is not None and (
            type(self.provider) is not str or len(self.provider) > 128
        ):
            raise ExecutionError(
                "unsupported_options", "Provider exceeds the portable contract bound"
            )
        if self.model is not None and (type(self.model) is not str or len(self.model) > 255):
            raise ExecutionError("unsupported_options", "Model exceeds the portable contract bound")
        for value in (self.provider, self.model):
            if value is not None and (
                not isinstance(value, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}", value)
                or "://" in value
            ):
                raise ExecutionError("unsupported_options", "Invalid provider or model identifier")
        if self.thinking is not None and type(self.thinking) is not str:
            raise ExecutionError("unsupported_options", "Thinking must be a supported string")
        if self.thinking not in {None, "off", "minimal", "low", "medium", "high", "xhigh", "max"}:
            raise ExecutionError(
                "unsupported_options", "Unsupported thinking; omit to use the default"
            )

    def data(self) -> dict[str, str]:
        return {key: value for key, value in dataclasses.asdict(self).items() if value is not None}


def strict_object(value: Any, fields: set[str]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != fields:
        raise ExecutionError("invalid_contract", "Missing or unknown execution contract fields")
    return value


def bounded_text(value: Any, maximum: int, pattern: str | None = None) -> str:
    if (
        type(value) is not str
        or not 1 <= len(value.encode("utf-8")) <= maximum
        or "\0" in value
        or (pattern is not None and re.fullmatch(pattern, value) is None)
    ):
        raise ExecutionError("invalid_contract", "Invalid bounded execution contract string")
    return value


def bounded_integer(value: Any, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ExecutionError("invalid_contract", "Invalid bounded execution contract integer")
    return value


def fingerprint(value: Any) -> str:
    return bounded_text(value, 64, r"[0-9a-f]{64}")


def bounded_items(value: Any, minimum: int, maximum: int) -> list[Any]:
    if type(value) is not list or not minimum <= len(value) <= maximum:
        raise ExecutionError("invalid_contract", "Invalid bounded execution contract list")
    return value


@dataclasses.dataclass(frozen=True)
class ExecutionRequest:
    """Concrete portable request v1; local custody stays in context.json."""

    schema: str
    dispatch_id: str
    request_sha256: str
    operation: str
    adapter: dict[str, Any]
    binding: dict[str, Any]
    source: dict[str, Any]
    packet_sha256: str
    assignment_sha256: str
    writable_paths: list[dict[str, Any]]
    verification: list[dict[str, Any]]
    budgets: dict[str, Any]
    requested_options: dict[str, str]
    continuation: dict[str, Any] | None

    @classmethod
    def from_data(cls, value: Any) -> ExecutionRequest:
        strict_object(value, {field.name for field in dataclasses.fields(cls)})
        try:
            result = cls(**value)
            result.validate()
        except (TypeError, KeyError, AttributeError, UnicodeError) as error:
            raise ExecutionError(
                "invalid_contract", "Invalid execution request field types"
            ) from error
        return result

    def validate(self) -> None:
        if self.schema == "anvil.execution-request.v1":
            if self.operation != "run" or self.continuation is not None:
                raise ExecutionError("unsupported_contract", "Request v1 is ephemeral Run only")
        elif self.schema == "anvil.execution-request.v2":
            if self.operation == "continue":
                validate_continuation(self.continuation)
            elif self.operation != "run" or self.continuation is not None:
                raise ExecutionError("unsupported_contract", "Invalid saved Run operation")
        else:
            raise ExecutionError("unsupported_contract", "Unsupported execution request version")
        identifier(self.dispatch_id)
        for value in (self.request_sha256, self.packet_sha256, self.assignment_sha256):
            fingerprint(value)
        adapter = strict_object(
            self.adapter,
            {"adapter_id", "contract_version", "registration_sha256", "profile_sha256"},
        )
        bounded_text(adapter["adapter_id"], 128)
        bounded_integer(adapter["contract_version"], 1, 2**63 - 1)
        fingerprint(adapter["registration_sha256"])
        fingerprint(adapter["profile_sha256"])
        binding = strict_object(
            self.binding,
            {
                "project_id",
                "task_id",
                "actor",
                "claim_id",
                "generation",
                "prd_id",
                "prd_revision",
                "task_revision",
                "repository_id",
                "claim_start_sha",
            },
        )
        for key in ("project_id", "task_id", "claim_id", "prd_id"):
            bounded_text(binding[key], 255)
        bounded_text(binding["actor"], 4096)
        for key in ("generation", "prd_revision"):
            bounded_integer(binding[key], 1, 2**63 - 1)
        for key in ("task_revision", "repository_id"):
            fingerprint(binding[key])
        bounded_text(binding["claim_start_sha"], 64, r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
        source = strict_object(
            self.source, {"commit", "claim_worktree", "cwd_relative", "cwd_identity"}
        )
        bounded_text(source["commit"], 64, r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
        if (
            not Path(bounded_text(source["claim_worktree"], 4096)).is_absolute()
            or source["cwd_relative"] != "."
        ):
            raise ExecutionError("invalid_contract", "Invalid isolated source directory binding")
        fingerprint(source["cwd_identity"])
        paths = []
        for item in bounded_items(self.writable_paths, 0, 128):
            strict_object(item, {"path", "before_sha256", "host_mode"})
            paths.append(relative_file(bounded_text(item["path"], 4096)))
            if item["before_sha256"] is not None:
                fingerprint(item["before_sha256"])
            bounded_integer(item["host_mode"], 0, 0o777)
        if len({path.casefold() for path in paths}) != len(paths):
            raise ExecutionError("invalid_contract", "Duplicate execution paths")
        commands = []
        for item in bounded_items(self.verification, 1, 16):
            strict_object(item, {"command", "timeout_seconds"})
            commands.append(bounded_text(item["command"], 16384))
            bounded_integer(item["timeout_seconds"], 1, 600)
        if len(set(commands)) != len(commands):
            raise ExecutionError("invalid_contract", "Duplicate execution commands")
        budgets = strict_object(
            self.budgets,
            {
                "wall_seconds",
                "adapter_wall_seconds",
                "max_tool_calls",
                "max_turns",
                "command_output_bytes",
            },
        )
        for key, maximum in (
            ("wall_seconds", 3600),
            ("adapter_wall_seconds", 1800),
            ("max_tool_calls", 500),
            ("max_turns", 200),
        ):
            bounded_integer(budgets[key], 1, maximum)
        if (
            budgets["command_output_bytes"] != 131072
            or type(budgets["command_output_bytes"]) is not int
        ):
            raise ExecutionError("invalid_contract", "Invalid native output budget")
        if budgets["wall_seconds"] < budgets["adapter_wall_seconds"] + 30:
            raise ExecutionError("invalid_contract", "Missing owned cleanup budget")
        if type(self.requested_options) is not dict:
            raise ExecutionError("invalid_contract", "Invalid requested options")
        if any(type(value) is not str for value in self.requested_options.values()):
            raise ExecutionError(
                "invalid_contract", "Requested option fields cannot be null or non-string"
            )
        try:
            RunOptions(**self.requested_options)
        except TypeError as error:
            raise ExecutionError("invalid_contract", "Unknown requested options") from error
        payload = dataclasses.asdict(self)
        payload.pop("request_sha256")
        if digest(json_bytes(payload)) != self.request_sha256:
            raise ExecutionError("request_changed", "Portable request digest differs")


@dataclasses.dataclass(frozen=True)
class AdapterResult:
    """Portable verified adapter return, separate from operational Run status."""

    schema: str
    dispatch_id: str
    request_sha256: str
    assignment_sha256: str
    adapter_run_id: str
    outcome: str
    acceptance: str
    effective_options: dict[str, str]
    capability_receipt_sha256: str
    source_commit: str
    changes: list[dict[str, Any]]
    artifacts: list[dict[str, Any]]
    sandbox_checks: list[dict[str, Any]]
    raw_receipt_sha256: str
    custody: dict[str, Any]
    saved_conversation: dict[str, Any] | None
    error_code: str | None

    @classmethod
    def from_data(cls, value: Any, request: ExecutionRequest) -> AdapterResult:
        strict_object(value, {field.name for field in dataclasses.fields(cls)})
        try:
            result = cls(**value)
            result.validate(request)
        except (TypeError, KeyError, AttributeError, UnicodeError) as error:
            raise ExecutionError(
                "invalid_contract", "Invalid adapter result field types"
            ) from error
        return result

    def validate(self, request: ExecutionRequest) -> None:
        expected_schema = (
            "anvil.adapter-result.v2" if request.schema == "anvil.execution-request.v2"
            else "anvil.adapter-result.v1"
        )
        if self.schema != expected_schema or self.acceptance != "not_granted":
            raise ExecutionError(
                "invalid_contract", "Invalid result version or acceptance boundary"
            )
        if expected_schema.endswith(".v1") and self.saved_conversation is not None:
            raise ExecutionError("invalid_contract", "Ephemeral result cannot acquire a session")
        if self.outcome not in {"returned", "failed", "reconciliation_required"}:
            raise ExecutionError("invalid_contract", "Invalid adapter outcome")
        for actual, expected in (
            (self.dispatch_id, request.dispatch_id),
            (self.request_sha256, request.request_sha256),
            (self.assignment_sha256, request.assignment_sha256),
            (self.source_commit, request.source["commit"]),
        ):
            if actual != expected:
                raise ExecutionError(
                    "result_mismatch", "Adapter result belongs to another request or source"
                )
        bounded_text(self.adapter_run_id, 128)
        for value in (self.capability_receipt_sha256, self.raw_receipt_sha256):
            fingerprint(value)
        strict_object(self.effective_options, {"provider", "model", "thinking"})
        for key, maximum in (("provider", 128), ("model", 255), ("thinking", 16)):
            bounded_text(self.effective_options[key], maximum)
        RunOptions(**self.effective_options)
        if any(
            self.effective_options[key] != value for key, value in request.requested_options.items()
        ):
            raise ExecutionError(
                "result_mismatch", "Effective options differ from the requested selection"
            )
        preimages = {item["path"]: item for item in request.writable_paths}
        names = []
        for change in bounded_items(self.changes, 0, 128):
            strict_object(change, {"path", "before_sha256", "after_sha256", "artifact_path"})
            path = relative_file(bounded_text(change["path"], 4096))
            names.append(path)
            if path not in preimages or change["before_sha256"] != preimages[path]["before_sha256"]:
                raise ExecutionError(
                    "result_mismatch", "Result path or preimage differs from the request"
                )
            if change["after_sha256"] is None:
                if change["artifact_path"] is not None:
                    raise ExecutionError(
                        "invalid_contract", "Deleted paths cannot carry returned bytes"
                    )
            else:
                fingerprint(change["after_sha256"])
                relative_file(bounded_text(change["artifact_path"], 4096))
        if len(set(names)) != len(names):
            raise ExecutionError("invalid_contract", "Duplicate returned paths")
        artifacts = {}
        total = 0
        for artifact in bounded_items(self.artifacts, 0, 512):
            strict_object(artifact, {"path", "sha256", "size_bytes"})
            path = relative_file(bounded_text(artifact["path"], 4096))
            fingerprint(artifact["sha256"])
            total += bounded_integer(artifact["size_bytes"], 0, 64 * 1024 * 1024)
            if path in artifacts:
                raise ExecutionError("invalid_contract", "Duplicate returned artifacts")
            artifacts[path] = artifact
        if total > 64 * 1024 * 1024:
            raise ExecutionError(
                "invalid_contract", "Returned artifacts exceed the aggregate bound"
            )
        for change in self.changes:
            if change["artifact_path"] is not None and (
                change["artifact_path"] not in artifacts
                or artifacts[change["artifact_path"]]["sha256"] != change["after_sha256"]
            ):
                raise ExecutionError(
                    "result_mismatch",
                    "Returned change is absent from the complete artifact manifest",
                )
        for check in bounded_items(self.sandbox_checks, 0, 32):
            strict_object(check, {"name", "argv", "exit_code", "output_sha256", "authority"})
            bounded_text(check["name"], 64)
            for argument in bounded_items(check["argv"], 1, 128):
                bounded_text(argument, 16000)
            if type(check["exit_code"]) is not int or check["authority"] != "sandbox_only":
                raise ExecutionError(
                    "invalid_contract", "Sandbox checks cannot confer native proof authority"
                )
            fingerprint(check["output_sha256"])
        custody = strict_object(
            self.custody,
            {
                "local_coordinator",
                "remote_owner_reference",
                "sandbox_id",
                "main_instance_id",
                "stop_status",
                "protected_unchanged",
                "receipt_sha256",
            },
        )
        for key, maximum in (
            ("local_coordinator", 512),
            ("remote_owner_reference", 512),
            ("sandbox_id", 128),
            ("main_instance_id", 256),
        ):
            bounded_text(custody[key], maximum)
        if (
            custody["stop_status"] not in {"stopped", "unknown"}
            or type(custody["protected_unchanged"]) is not bool
        ):
            raise ExecutionError("invalid_contract", "Invalid owner custody result")
        fingerprint(custody["receipt_sha256"])
        if custody["receipt_sha256"] != self.raw_receipt_sha256:
            raise ExecutionError(
                "result_mismatch", "Custody receipt differs from the adapter receipt"
            )
        if self.outcome == "returned" and (
            custody["stop_status"] != "stopped"
            or custody["protected_unchanged"] is not True
            or self.error_code is not None
        ):
            raise ExecutionError(
                "result_mismatch", "Successful return requires stopped protected custody"
            )
        if self.error_code is not None:
            bounded_text(self.error_code, 128)
        if expected_schema.endswith(".v2"):
            if self.saved_conversation is not None:
                validate_saved_conversation(self.saved_conversation, request, self.adapter_run_id)
                checkpoint = artifacts.get("results/conversation.json")
                if checkpoint is None or checkpoint["sha256"] != self.saved_conversation[
                    "checkpoint_sha256"
                ]:
                    raise ExecutionError("result_mismatch", "Saved checkpoint artifact differs")
            elif self.outcome == "returned":
                raise ExecutionError(
                    "result_mismatch", "Saved Run requires a verified conversation"
                )


def validate_continuation(value: Any) -> None:
    strict_object(value, {
        "prior_run_id", "prior_request_file_sha256", "prior_result_file_sha256",
        "prior_adapter_result_file_sha256", "prior_adapter_run_id", "prior_adapter_receipt_sha256",
        "conversation_id", "official_session_id", "checkpoint_sha256", "session_sha256",
    })
    for key in ("prior_run_id", "prior_adapter_run_id", "conversation_id"):
        identifier(value[key])
    bounded_text(value["official_session_id"], 128, r"[A-Za-z0-9_.-]{1,128}")
    for key in value:
        if key.endswith("sha256"):
            fingerprint(value[key])


def validate_saved_conversation(value: Any, request: ExecutionRequest, adapter_run_id: str) -> None:
    strict_object(value, {
        "conversation_id", "official_session_id", "checkpoint_sha256", "prior_adapter_run_id",
        "prior_checkpoint_sha256", "session_before", "session_after", "prior_prefix_sha256",
        "workspace_sha256", "continuable",
    })
    identifier(adapter_run_id)
    identifier(value["conversation_id"])
    bounded_text(value["official_session_id"], 128, r"[A-Za-z0-9_.-]{1,128}")
    for key in ("checkpoint_sha256", "prior_prefix_sha256", "workspace_sha256"):
        fingerprint(value[key])
    for session in (value["session_before"], value["session_after"]):
        if session is None:
            continue
        strict_object(session, {"bytes", "sha256", "header_sha256"})
        bounded_integer(session["bytes"], 1, 16 * 1024 * 1024)
        fingerprint(session["sha256"])
        fingerprint(session["header_sha256"])
    if value["continuable"] is not True or value["session_after"] is None:
        raise ExecutionError(
            "result_mismatch", "Saved return is not a verified terminal checkpoint"
        )
    prior = request.continuation
    if prior is None:
        if (value["session_before"] is not None or value["prior_adapter_run_id"] is not None
                or value["prior_checkpoint_sha256"] is not None
                or value["conversation_id"] != adapter_run_id
                or value["prior_prefix_sha256"] != digest(b"")):
            raise ExecutionError("result_mismatch", "Initial saved Run acquired foreign history")
    else:
        before, after = value["session_before"], value["session_after"]
        if (value["conversation_id"] != prior["conversation_id"]
                or value["official_session_id"] != prior["official_session_id"]
                or value["prior_adapter_run_id"] != prior["prior_adapter_run_id"]
                or value["prior_checkpoint_sha256"] != prior["checkpoint_sha256"]
                or before is None or before["sha256"] != prior["session_sha256"]
                or value["prior_prefix_sha256"] != before["sha256"]
                or before["header_sha256"] != after["header_sha256"]
                or after["bytes"] < before["bytes"]
                or adapter_run_id == prior["prior_adapter_run_id"]):
            raise ExecutionError("result_mismatch", "Continued conversation changed prior identity")


def assignment_for(context: dict[str, Any], packet: bytes) -> dict[str, Any]:
    # The native CLI emits its sidecar location before the JSON body. Retain
    # those exact bytes in packet.txt; project only the structured work below.
    header, separator, payload = packet.partition(b"\n\n")
    if not separator or not header.startswith(b"Wrote packet to "):
        raise ExecutionError("invalid_packet", "Expected the native JSON packet output")
    work = json.loads(payload)
    if not isinstance(work, dict) or not isinstance(work.get("task"), dict):
        raise ExecutionError("invalid_packet", "Expected the native structured work packet")
    # Claim authority stays in the full retained packet and outer request.
    # Only this bookkeeping changes when a ready task acquires its claim.
    work.pop("active_claim", None)
    work.pop("update_protocol", None)
    work["task"].pop("status", None)
    work["task"].pop("updated_at", None)
    return {
        "version": 2,
        "task_id": context["task_id"],
        "kind": "implementation",
        "instructions": (
            "Implement this native Anvil packet. "
            "Return scoped edits; never grant acceptance.\n" + json_bytes(work).decode()
            + ("\nNative request-changes provenance:\n"
               + json_bytes(context["continuation_material"]["review"]).decode()
               if context.get("continuation_material", {}).get("review") is not None else "")
        ),
        "writable_paths": context["writable_paths"],
        "checks": [],
        "repository": context["cwd"],
        "revision": context["source_sha"],
        "wall_seconds": context["wall_seconds"],
        "run_options": context["options"],
    }


def writable_preimages(cwd: Path, names: list[str]) -> list[dict[str, Any]]:
    from anvil.execution_runner import checked_path

    paths = []
    for name in names:
        path = checked_path(cwd, name)
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
        bounded_integer(mode, 0, 0o777)
        paths.append({
            "path": name,
            "before_sha256": digest(path.read_bytes()) if path.exists() else None,
            "host_mode": mode,
        })
    return paths


def claim_binding(project_id: str, claim: Claim) -> dict[str, Any]:
    immutable = claim.attestation_context
    if immutable is None:
        raise ExecutionError("claim_changed", "Claim has no immutable execution binding")
    return {
        "project_id": project_id, "task_id": claim.task_id,
        "actor": claim.claimed_by, "claim_id": claim.id, "generation": claim.generation,
        "prd_id": immutable.prd_id, "prd_revision": immutable.prd_revision,
        "task_revision": immutable.task_revision, "repository_id": immutable.repository_id,
        "claim_start_sha": immutable.claim_start_sha,
    }


def request_for(
    context: dict[str, Any], packet: bytes, assignment: dict[str, Any]
) -> ExecutionRequest:
    from anvil.claims.command_proof_artifact import claim_command_cwd_identity

    claim = Claim.model_validate(context["claim"])
    immutable = claim.attestation_context
    registration = ExecutionAdapter(**context["adapter"])
    root = Path(context["cwd"])
    paths = writable_preimages(root, context["writable_paths"])
    value = {
        "schema": ("anvil.execution-request.v2" if registration.registration[
            "capabilities"]["saved_session_continue"] else "anvil.execution-request.v1"),
        "dispatch_id": context["run_id"],
        "operation": context.get("operation", "run"),
        "adapter": {
            "adapter_id": registration.registration["adapter_id"],
            "contract_version": registration.registration["contract_version"],
            "registration_sha256": digest(json_bytes(registration.registration)),
            "profile_sha256": registration.hashes["profile"],
        },
        "binding": claim_binding(context["project_id"], claim),
        "source": {
            "commit": context["source_sha"],
            "claim_worktree": str(root),
            "cwd_relative": ".",
            "cwd_identity": claim_command_cwd_identity(root, immutable.repository_id, "."),
        },
        "packet_sha256": digest(packet),
        "assignment_sha256": digest(json_bytes(assignment)),
        "writable_paths": paths,
        "verification": [
            {"command": command, "timeout_seconds": 120} for command in context["commands"]
        ],
        "budgets": {
            "wall_seconds": context["wall_seconds"] + 180,
            "adapter_wall_seconds": context["wall_seconds"],
            "max_tool_calls": 40,
            "max_turns": 32,
            "command_output_bytes": 131072,
        },
        "requested_options": context["options"],
        "continuation": context.get("continuation"),
    }
    value["request_sha256"] = digest(json_bytes(value))
    return ExecutionRequest.from_data(value)


def adapter_module(registration: ExecutionAdapter, *, require_registered: bool = True) -> Any:
    """Import only code explicitly pinned by the owner, not a task-chosen plugin."""
    from anvil.config import validate_execution_adapter_inputs

    if require_registered and load_execution_adapter() != registration:
        raise ExecutionError("adapter_changed", "Execution registration changed")
    validate_execution_adapter_inputs(registration)
    raw = execution_file_bytes(Path(registration.script))
    if digest(raw) != registration.hashes["script"]:
        raise ExecutionError("adapter_changed", "Adapter code changed before import")
    module = types.ModuleType("anvil_registered_coordination")
    module.__file__ = registration.script
    exec(compile(raw, registration.script, "exec"), module.__dict__)
    module.validate_task(
        {
            "version": 2,
            "task_id": "capability:check",
            "kind": "implementation",
            "instructions": "Contract check only",
            "writable_paths": [],
            "checks": [],
            "run_options": {},
        }
    )
    module.profile_settings(Path(registration.profile))
    return module


def validate_adapter_contract(
    registration: ExecutionAdapter, cwd: Path, assignment: dict[str, Any] | None = None,
    *, candidate_path: Path | None = None,
) -> None:
    """Local validation in isolated Python, without project import fallback."""
    from anvil.execution_runner import capture

    if candidate_path is not None:
        candidate = ExecutionAdapter(**json.loads(execution_file_bytes(candidate_path)))
        if candidate != registration:
            raise ExecutionError(
                "adapter_changed", "Candidate wrapper differs from validation input"
            )
        directory = private_directory(candidate_path.parent / "probes" / uuid.uuid4().hex)
    else:
        directory = private_directory(journal_root(cwd) / "operations" / uuid.uuid4().hex)
    arguments = [sys.executable, "-I", "-m", "anvil.execution_runner", "--validate-adapter"]
    if candidate_path is not None:
        arguments.extend(["--candidate", str(candidate_path)])
    if assignment is not None:
        path = directory / "assignment.json"
        write_receipt(path, assignment)
        arguments.append(str(path))
    result = capture(
        arguments,
        cwd,
        directory,
        "contract",
        time.monotonic() + 30,
        1024 * 1024,
        separate_stdout=True,
        require_success=False,
    )
    if result["exit_code"] or (directory / "contract.stdout").read_bytes() != json_bytes(
        dataclasses.asdict(registration)
    ):
        raise ExecutionError(
            "adapter_unavailable",
            "Registered adapter refuses the concrete assignment or source"
            if assignment is not None else "Registered adapter lacks the required v2 contract",
        )


def native_cli(cwd: Path, *arguments: str, maximum: int = 2 * 1024 * 1024) -> bytes:
    from anvil.execution_runner import capture

    environment = os.environ.copy()
    environment.pop("ANVIL_ROOT", None)
    environment.pop("ANVIL_PRD", None)
    directory = private_directory(journal_root(cwd) / "operations" / uuid.uuid4().hex)
    result = capture(
        [sys.executable, "-I", "-m", "anvil.cli", *arguments, "--cwd", str(cwd)],
        cwd,
        directory,
        "native",
        time.monotonic() + 120,
        maximum,
        environment,
        separate_stdout=True,
        require_success=False,
    )
    if result["exit_code"]:
        raise ExecutionError(
            "native_refused",
            (directory / "native.log").read_bytes().decode(errors="replace")[-4000:],
        )
    return (directory / "native.stdout").read_bytes()


def git(cwd: Path, *arguments: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(cwd), *arguments], timeout=30)


def clean_source(cwd: Path, expected: str | None = None) -> str:
    require_visible_index(cwd)
    head = git(cwd, "rev-parse", "HEAD").decode().strip()
    if expected is not None and head != expected:
        raise ExecutionError("source_changed", "Source revision changed")
    if git(cwd, "status", "--porcelain", "--untracked-files=all").strip():
        raise ExecutionError("dirty_source", "Run requires a clean isolated source checkout")
    return head


def require_visible_index(cwd: Path) -> None:
    """Git status is authoritative only when tracked paths cannot hide changes."""
    entries = git(cwd, "ls-files", "-v", "-z").split(b"\0")
    if any(entry and (entry[:1].islower() or entry[:1] == b"S") for entry in entries):
        raise ExecutionError(
            "hidden_index_flags",
            "Run refuses hidden index flags: assume-unchanged or skip-worktree",
        )


def execution_scope(backend: Any, task_id: str) -> dict[str, Any]:
    task = backend.get_task(task_id)
    project = backend.get_project()
    if task is None or project is None:
        raise ExecutionError("task_not_found", "Selected project or task is unavailable")
    prd = backend.get_prd(task.prd_id)
    if prd is None or str(prd.status) != "approved":
        raise ExecutionError("prd_unapproved", "Run requires the current approved PRD")
    if any(
        task_id in bundle.task_ids and str(bundle.status) not in {"withdrawn", "superseded"}
        for bundle in backend.list_bundles()
    ):
        raise ExecutionError("unsupported_bundle", "Bundle members require their coordinator")
    dependencies = []
    for dependency_id in task.dependencies:
        dependency = backend.get_task(dependency_id)
        if dependency is None or str(dependency.status) != "done":
            raise ExecutionError("dependency_not_done", "Run requires every dependency to be done")
        dependencies.append(dependency.model_dump(mode="json"))
    content = task.model_dump(mode="json")
    content.pop("status", None)
    content.pop("updated_at", None)
    return {
        "project_id": project.id,
        "task": content,
        "prd_id": task.prd_id,
        "prd": prd.model_dump(mode="json"),
        "dependencies": dependencies,
    }


def scope_digest(backend: Any, task_id: str) -> str:
    return digest(json_bytes(execution_scope(backend, task_id)))


def require_execution_scope(backend: Any, task_id: str, expected: str) -> None:
    from anvil.claims.manager import ClaimError

    try:
        if scope_digest(backend, task_id) != expected:
            raise ExecutionError("scope_changed", "Execution scope changed after preview")
    except ExecutionError as error:
        raise ClaimError(str(error)) from error


def task_contract(task: Task) -> tuple[list[str], list[str]]:
    paths = [relative_file(path) for path in task.likely_files]
    commands = task.verification.commands
    if not paths or len(paths) > 128 or len(set(paths)) != len(paths):
        raise ExecutionError("unsupported_scope", "Run needs 1..128 unique exact writable files")
    if len({path.casefold() for path in paths}) != len(paths):
        raise ExecutionError("unsupported_scope", "Case-aliased writable paths are unsupported")
    if not 1 <= len(commands) <= 16 or len(set(commands)) != len(commands):
        raise ExecutionError(
            "unsupported_proofs", "Run needs 1..16 unique literal verification commands"
        )
    if any(not command or len(command.encode()) > 16384 or "\0" in command for command in commands):
        raise ExecutionError(
            "unsupported_proofs", "Verification command exceeds native proof bounds"
        )
    if task.verification.profile is not None:
        raise ExecutionError(
            "unsupported_profile", "Run does not yet execute verification profiles"
        )
    requirements = {
        proof.command
        for proof in task.verification.required_proofs
        if str(proof.kind) == "command" and 0 in proof.passing_exit_codes
    }
    if set(commands) != requirements:
        raise ExecutionError(
            "unsupported_proofs",
            "Every literal command needs its native zero-exit proof requirement",
        )
    # Native submit's historical single-value comma splitting is not proof-safe.
    if len(commands) == 1 and "," in commands[0] or len(paths) == 1 and "," in paths[0]:
        raise ExecutionError(
            "unsupported_proofs", "Single comma-containing native submit values are unsupported"
        )
    return paths, list(commands)


def live_claim(backend: Any, request: dict[str, Any], *, remaining: int = 30) -> Claim:
    claim = backend.get_claim(request["claim"]["id"])
    task = backend.get_task(request["task_id"])
    if (
        claim is None
        or task is None
        or str(task.status) not in {"claimed", "in_progress"}
        or str(claim.status) != "active"
        or claim.generation != request["claim"]["generation"]
        or claim.claimed_by != request["actor"]
        or claim.task_id != request["task_id"]
        or claim.root_set is not None
        or claim.bundle_claim_id is not None
        or claim.attestation_context is None
        or claim.attestation_context.model_dump(mode="json")
        != request["claim"]["attestation_context"]
        or claim.worktree_path != request["cwd"]
        or claim.lease_expires_at <= now() + timedelta(seconds=remaining)
    ):
        raise ExecutionError(
            "claim_changed", "Run requires its exact active isolated claim and sufficient lease"
        )
    if scope_digest(backend, claim.task_id) != request["scope_sha256"]:
        raise ExecutionError("scope_changed", "Execution scope changed")
    return claim


def journal_root(cwd: Path) -> Path:
    from anvil.cli._helpers import _require_state_dir, _resolve_state_dir

    state = _resolve_state_dir(cwd)
    _require_state_dir(state)
    return private_directory(state / "execution")


def require_isolated_state_layout() -> None:
    from anvil.cli._helpers import _is_local_layout

    if _is_local_layout():
        raise ExecutionError(
            "unsupported_local_isolated_mode",
            "Run requires shared workspace State for isolated claims; local State is unsupported",
        )


def require_planned_source(
    cwd: Path, task: Task, source: str, state: Path, *, branch: str | None = None
) -> None:
    from anvil.cli._helpers import _load_config_optional
    from anvil.git_ops import ClaimPlanError, resolve_claim_plan

    config = _load_config_optional(state)
    try:
        plan = resolve_claim_plan(
            task.id, task.title, cwd=cwd, worktree=True,
            branch=branch,
            branch_prefix=config.branch_prefix if config is not None else "agent",
            isolation_required=True,
        )
    except ClaimPlanError as error:
        raise ExecutionError("claim_plan_refused", str(error)) from error
    if plan.claim_start_sha != source:
        raise ExecutionError(
            "claim_source_mismatch",
            "Native claim source differs from preview source; use a fresh isolated owner claim",
        )


def continuation_material(
    backend: Any, context: dict[str, Any], prior_run_id: str
) -> tuple[dict[str, Any], dict[str, Any], Path]:
    """Join retained bytes to native history; the adapter owns session parsing."""
    from anvil.attempt_view import read_attempt_view
    from anvil.claims.progress_attestation import inspect_local_repository
    from anvil.execution_runner import (
        captures_stopped,
        portable_adapter_result,
        verified_changes,
    )

    cwd = Path(context["cwd"])
    root = journal_root(cwd)
    directory = root / "runs" / identifier(prior_run_id)
    names = ["admission.json", "context.json", "request.json", "adapter-result.json",
             "result.json", "launched.json", "started.json", "adapter-task.json",
             "integration.json", "dispatch.json", "packet.txt", "adapter/receipt.json",
             "adapter/results/conversation.json"]
    raw = {name: execution_file_bytes(directory / name) for name in names}
    values = {name: json.loads(value) for name, value in raw.items() if name != "packet.txt"}
    prior_context = values["context.json"]
    prior_request = ExecutionRequest.from_data(values["request.json"])
    if prior_request.schema != "anvil.execution-request.v2":
        raise ExecutionError(
            "ephemeral_run", "This Run has no saved conversation; start a saved Run"
        )
    prior_result = AdapterResult.from_data(values["adapter-result.json"], prior_request)
    outcome = values["result.json"]
    registration = ExecutionAdapter(**context["adapter"])
    if (not registration.registration["capabilities"]["saved_session_continue"]
            or prior_context["adapter"] != context["adapter"]):
        raise ExecutionError(
            "adapter_changed", "Continue requires the same saved adapter registration"
        )
    if (outcome.get("state") not in {"needs_review", "failed_stopped"}
            or outcome.get("local_children_stopped") is not True
            or outcome.get("acceptance") != "not_granted"
            or outcome.get("run_id") != prior_run_id
            or prior_request.dispatch_id != prior_run_id
            or prior_context.get("run_id") != prior_run_id):
        raise ExecutionError("unsettled_run", "Prior native delivery requires owner reconciliation")
    launched, started = values["launched.json"], values["started.json"]
    pid = launched.get("pid")
    if (type(pid) is not int or pid <= 1 or started.get("pid") != pid
            or launched.get("run_id") != prior_run_id or started.get("run_id") != prior_run_id
            or prior_result.custody["local_coordinator"] != f"pid:{pid}/pgid:{pid}"):
        raise ExecutionError("unsettled_run", "Prior coordinator identity is incomplete")
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        pass
    else:
        raise ExecutionError("run_busy", "Prior coordinator has not stopped")
    capture_names = ["adapter"] + [
        f"check-{index}" for index in range(len(prior_context["commands"]))
        if (directory / f"check-{index}.attempt.json").exists()
    ]
    if not captures_stopped(directory, capture_names):
        raise ExecutionError("unsettled_run", "Prior command custody is incomplete")
    if outcome["state"] == "needs_review" and len(capture_names) != 1 + len(
        prior_context["commands"]
    ):
        raise ExecutionError("unsettled_run", "Prior native checks are incomplete")
    if outcome["state"] == "failed_stopped" and (
        (directory / "submission.json").exists()
        or outcome.get("error_code") not in {"command_failed", "command_timeout", "output_overflow"}
    ):
        raise ExecutionError("unsettled_run", "Uncertain native failure cannot Continue")
    for index, change in enumerate(values["integration.json"]["changes"]):
        name = f"integrated-{index}.json"
        raw[name] = execution_file_bytes(directory / name)
        if json.loads(raw[name]) != change:
            raise ExecutionError("unsettled_run", "Prior integration did not settle")
    task = backend.get_task(context["task_id"])
    project = backend.get_project()
    original_claim = backend.get_claim(prior_request.binding["claim_id"])
    identity = inspect_local_repository(cwd, project_id=project.id)
    if (project.id != prior_request.binding["project_id"]
            or task.prd_id != prior_request.binding["prd_id"]
            or identity.repository_id != prior_request.binding["repository_id"]
            or journal_root(Path(prior_context["cwd"])) != root
            or original_claim is None or str(original_claim.status) != "released"
            or original_claim.generation != prior_request.binding["generation"]
            or original_claim.task_id != prior_request.binding["task_id"]):
        raise ExecutionError("continuation_lineage", "Continue requires the same native lineage")
    retained_claim = Claim.model_validate(prior_context["claim"])
    immutable = original_claim.attestation_context
    if (immutable is None or any(
        getattr(original_claim, key) != getattr(retained_claim, key)
        for key in ("id", "task_id", "claimed_by", "generation", "attestation_context",
                    "worktree_path", "branch")
    ) or prior_request.binding != claim_binding(project.id, original_claim)
        or prior_request.source["commit"] != prior_context["source_sha"]
        or prior_request.source["claim_worktree"] != prior_context["cwd"]
        or prior_context["cwd"] != original_claim.worktree_path
        or prior_request.packet_sha256 != digest(raw["packet.txt"])
        or prior_request.assignment_sha256 != digest(json_bytes(values["adapter-task.json"]))
        or values["dispatch.json"].get("run_id") != prior_run_id
        or values["dispatch.json"].get("request_file_sha256") != digest(raw["request.json"])
        or values["dispatch.json"].get("context_sha256") != digest(raw["context.json"])):
        raise ExecutionError(
            "continuation_lineage", "Prior dispatch differs from its immutable claim"
        )
    git(cwd, "merge-base", "--is-ancestor", prior_request.source["commit"], context["source_sha"])
    current_claim = context.get("claim")
    if current_claim and (current_claim["id"] == original_claim.id or (
        task.id == original_claim.task_id
        and current_claim["generation"] <= original_claim.generation
    )):
        raise ExecutionError("claim_changed", "Continue requires a fresh native claim generation")
    attempt = None
    if task.id == original_claim.task_id:
        from anvil.project_snapshot import ProjectSnapshotError

        try:
            attempt = read_attempt_view(root.parent, original_claim.task_id)
        except ProjectSnapshotError as error:
            raise ExecutionError(
                "incomplete_history", "Prior native attempt is incomplete"
            ) from error
        if attempt["history"]["overflow"] is not False:
            raise ExecutionError("incomplete_history", "Prior native attempt is incomplete")
        candidates = [*attempt["history"]["evidence"]]
        if attempt["current"]["evidence"] is not None:
            candidates.append(attempt["current"]["evidence"])
        evidence = [item for item in candidates if item["claim_id"] == original_claim.id]
    else:
        # An explicit follow-up can retain a closed original task. Use the
        # existing single-task latest reader; an older delivery never substitutes
        # for a superseding native attempt, and no project-wide proof scan occurs.
        latest = backend.get_latest_evidence(original_claim.task_id)
        if latest is not None and latest.claim_id != original_claim.id:
            raise ExecutionError(
                "continuation_lineage", "A newer native delivery superseded this Run"
            )
        evidence = [latest.model_dump(mode="json")] if latest is not None else []
    if (outcome["state"] == "needs_review" and len(evidence) != 1
            or outcome["state"] == "failed_stopped" and evidence):
        raise ExecutionError("unsettled_run", "Native evidence differs from prior delivery")
    if evidence:
        for name in ("submission.json", "submitted.json"):
            raw[name] = execution_file_bytes(directory / name)
        submitted = json.loads(raw["submitted.json"])
        data = submitted.get("data", {})
        if (submitted.get("ok") is not True or submitted.get("command") != "submit"
                or data.get("evidence_id") != evidence[0]["id"]
                or data.get("claim_id") != original_claim.id
                or data.get("submitted_by") != original_claim.claimed_by
                or data.get("commands_run") != prior_context["commands"]
                or evidence[0]["commands_run"] != prior_context["commands"]
                or evidence[0]["task_id"] != original_claim.task_id
                or evidence[0]["submitted_by"] != original_claim.claimed_by):
            raise ExecutionError(
                "unsettled_run", "Prior submitted receipt differs from native evidence"
            )
    review = None
    if task.id == original_claim.task_id and evidence:
        reviews = [*attempt["current"]["reviews"], *attempt["history"]["reviews"]]
        rejected = [item for item in reviews if item.get("binding_status") == "exact"
                    and item.get("evidence_id") == evidence[0]["id"]
                    and item.get("claim_id") == original_claim.id
                    and item.get("generation") == original_claim.generation
                    and item.get("decision") == "rejected"]
        if not rejected:
            raise ExecutionError(
                "rework_not_requested", "Request changes on the exact prior evidence first"
            )
        review = max(rejected, key=lambda item: item["causal_order"])
    adapter = adapter_module(registration)
    output = directory / "adapter"
    changes, _ = verified_changes(
        adapter, output, values["adapter-task.json"], registration, prior_request)
    if values["integration.json"]["changes"] != changes:
        raise ExecutionError("unsettled_run", "Prior integration differs from verified edits")
    expected = dataclasses.asdict(portable_adapter_result(
        adapter, output, registration, prior_request, changes))
    expected["custody"]["local_coordinator"] = prior_result.custody["local_coordinator"]
    if expected != dataclasses.asdict(prior_result):
        raise ExecutionError(
            "result_mismatch", "Prior portable result differs from verified artifacts"
        )
    checkpoint = adapter.continuation_checkpoint(
        output, adapter.profile_settings(Path(registration.profile)))
    if checkpoint != values["adapter/results/conversation.json"]:
        raise ExecutionError("result_mismatch", "Prior saved checkpoint is no longer terminal")
    saved = prior_result.saved_conversation
    continuation = {
        "prior_run_id": prior_run_id,
        "prior_request_file_sha256": digest(raw["request.json"]),
        "prior_result_file_sha256": digest(raw["result.json"]),
        "prior_adapter_result_file_sha256": digest(raw["adapter-result.json"]),
        "prior_adapter_run_id": prior_result.adapter_run_id,
        "prior_adapter_receipt_sha256": digest(raw["adapter/receipt.json"]),
        "conversation_id": saved["conversation_id"],
        "official_session_id": saved["official_session_id"],
        "checkpoint_sha256": saved["checkpoint_sha256"],
        "session_sha256": saved["session_after"]["sha256"],
    }
    validate_continuation(continuation)
    material = {"files": {name: digest(value) for name, value in raw.items()}, "review": review,
                "evidence_id": evidence[0]["id"] if evidence else None,
                "prior_claim_id": original_claim.id, "prior_generation": original_claim.generation,
                "prior_task_id": original_claim.task_id, "prior_branch": original_claim.branch}
    return continuation, material, output


def revalidate_continuation(backend: Any, context: dict[str, Any]) -> Path | None:
    prior = context.get("continuation")
    if prior is None:
        if context.get("operation") == "continue":
            raise ExecutionError("invalid_continuation", "Continue is missing its saved lineage")
        return None
    current, material, output = continuation_material(backend, context, prior["prior_run_id"])
    if current != prior or material != context.get("continuation_material"):
        raise ExecutionError(
            "continuation_changed", "Prior native or conversation material changed"
        )
    return output


def preflight_assignment(context: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Validate actual work and export locally before reserving any execution."""
    cwd = Path(context["cwd"])
    preimages = writable_preimages(cwd, context["writable_paths"])
    packet = native_cli(cwd, "packet", context["task_id"], "--format", "json")
    assignment = assignment_for(context, packet)
    validate_adapter_contract(ExecutionAdapter(**context["adapter"]), cwd, assignment)
    clean_source(cwd, context["source_sha"])
    return assignment, preimages


def revalidate_admission(backend: Any, context: dict[str, Any], state: Path) -> None:
    """Recheck material after native packet refresh, before reserving a slot."""
    task_id = context["task_id"]
    if scope_digest(backend, task_id) != context["scope_sha256"]:
        raise ExecutionError("scope_changed", "Task scope changed after preview")
    claims = [
        item.model_dump(mode="json")
        for item in backend.list_active_claims()
        if item.task_id == task_id
    ]
    if claims != ([context["claim"]] if context["claim"] else []):
        raise ExecutionError("claim_changed", "Claim changed after preview")
    task = backend.get_task(task_id)
    if claims:
        live_claim(backend, context, remaining=context["wall_seconds"] + 180)
    elif str(task.status) != "ready":
        raise ExecutionError("task_not_ready", "Task is no longer ready")
    else:
        require_planned_source(Path(context["cwd"]), task, context["source_sha"], state,
                               branch=context.get("claim_branch"))
    revalidate_continuation(backend, context)


def read_execution_preview(root: Path, preview_id: str) -> dict[str, Any]:
    """Validate retained routing fields without granting freshness or admission."""
    try:
        identifier(bounded_text(preview_id, 32))
        preview = read_receipt(root / "previews" / (preview_id + ".json"))
        if (type(preview) is not dict or type(preview["version"]) is not int
                or preview["version"] != 1):
            raise ValueError("Invalid preview version")
        if identifier(bounded_text(preview["preview_id"], 32)) != preview_id:
            raise ValueError("Preview identity differs")
        for name, maximum in (
            ("task_id", 255), ("project_id", 255), ("actor", 4096), ("cwd", 4096)
        ):
            bounded_text(preview[name], maximum)
        if not Path(preview["cwd"]).is_absolute():
            raise ValueError("Preview checkout is not absolute")
        retained_claim = preview["claim"]
        if retained_claim is not None:
            claim = Claim.model_validate(retained_claim)
            if (claim.task_id != preview["task_id"] or claim.claimed_by != preview["actor"]
                    or str(Path(bounded_text(claim.worktree_path, 4096)).resolve())
                    != preview["cwd"]):
                raise ValueError("Claim differs from the retained route")
        operation = bounded_text(preview.get("operation", "run"), 8)
        if operation not in {"run", "continue"}:
            raise ValueError("Unsupported preview route")
        continuation = preview.get("continuation")
        if operation == "continue":
            validate_continuation(continuation)
        elif continuation is not None:
            raise ValueError("Run acquired continuation authority")
    except (KeyError, TypeError, ValueError):
        raise ExecutionError(
            "preview_mismatch", "Invalid preview object or routing fields"
        ) from None
    return preview


def require_fresh_preview(preview: dict[str, Any]) -> None:
    try:
        created = datetime.fromisoformat(bounded_text(preview["created_at"], 64))
        expires = datetime.fromisoformat(bounded_text(preview["expires_at"], 64))
        if created.tzinfo is None or expires.tzinfo is None:
            raise ValueError("Preview times require time zones")
    except (KeyError, TypeError, ValueError):
        raise ExecutionError("preview_mismatch", "Invalid preview validity window") from None
    if not created <= now() < expires:
        raise ExecutionError("preview_expired", "Preview expired or the clock moved backwards")


def require_unconsumed_preview(root: Path, preview_id: str) -> None:
    consumed = root / "consumed" / (identifier(preview_id) + ".json")
    if receipt_attempt_exists(consumed):
        existing = read_receipt(consumed)
        raise ExecutionError(
            "preview_consumed", "Preview already dispatched as " + existing["run_id"]
        )


def preview_run(
    cwd: Path, task_id: str, actor: str, options: RunOptions, wall_seconds: int = 600
) -> dict[str, Any]:
    return _preview_execution(cwd, task_id, actor, options, wall_seconds)


def preview_continue(
    cwd: Path, prior_run_id: str, actor: str, options: RunOptions,
    wall_seconds: int = 600, task_id: str | None = None,
) -> dict[str, Any]:
    if not load_execution_adapter().registration["capabilities"]["saved_session_continue"]:
        raise ExecutionError(
            "continuation_unsupported", "Registered adapter does not support Continue"
        )
    prior = read_receipt(journal_root(cwd) / "runs" / identifier(prior_run_id) / "request.json")
    request = ExecutionRequest.from_data(prior)
    if request.schema != "anvil.execution-request.v2":
        raise ExecutionError(
            "ephemeral_run", "This Run has no saved conversation; start a saved Run"
        )
    return _preview_execution(
        cwd, task_id or request.binding["task_id"], actor, options, wall_seconds,
        prior_run_id=prior_run_id)


def _preview_execution(
    cwd: Path, task_id: str, actor: str, options: RunOptions, wall_seconds: int,
    *, prior_run_id: str | None = None,
) -> dict[str, Any]:
    from anvil.cli._helpers import _open_backend, _require_state_dir, _resolve_state_dir
    from anvil.execution_runner import require_linux_capture

    require_linux_capture()
    if type(wall_seconds) is not int or not 1 <= wall_seconds <= 1800:
        raise ExecutionError("invalid_budget", "Run budget must be 1..1800 seconds")
    require_isolated_state_layout()
    registration = load_execution_adapter()
    state = _resolve_state_dir(cwd)
    _require_state_dir(state)
    backend = _open_backend(state, project_root=cwd)
    try:
        scope = execution_scope(backend, task_id)
        task = backend.get_task(task_id)
        paths, commands = task_contract(task)
        claims = [claim for claim in backend.list_active_claims() if claim.task_id == task_id]
        if claims:
            if len(claims) != 1:
                raise ExecutionError("claim_changed", "Ambiguous active ownership")
            claim = claims[0]
            if (
                str(task.status) not in {"claimed", "in_progress"}
                or claim.claimed_by != actor
                or not claim.worktree_path
                or Path(claim.worktree_path).resolve() != cwd.resolve()
                or claim.root_set is not None
                or claim.bundle_claim_id is not None
                or claim.attestation_context is None
            ):
                raise ExecutionError(
                    "claim_changed", "Use the exact owner's isolated ordinary claim checkout"
                )
            if claim.lease_expires_at <= now() + timedelta(seconds=wall_seconds + 180):
                raise ExecutionError(
                    "insufficient_lease", "Renew the native claim before previewing this budget"
                )
        elif str(task.status) != "ready":
            raise ExecutionError(
                "task_not_ready", "Run requires a ready task or its active isolated claim"
            )
        else:
            # Creation uses the native canonical identity. Historical claim
            # lookup above deliberately preserves the exact supplied spelling.
            actor = canonicalize_new_actor(actor)
        source = clean_source(cwd)
        created = now()
        value = {
            "version": 1,
            "operation": "continue" if prior_run_id is not None else "run",
            "preview_id": uuid.uuid4().hex,
            "task_id": task_id,
            "project_id": scope["project_id"],
            "actor": actor,
            "cwd": str(cwd.resolve()),
            "scope_sha256": digest(json_bytes(scope)),
            "source_sha": source,
            "claim": claims[0].model_dump(mode="json") if claims else None,
            "writable_paths": paths,
            "commands": commands,
            "options": options.data(),
            "wall_seconds": wall_seconds,
            "adapter": dataclasses.asdict(registration),
            "created_at": timestamp(created),
            "expires_at": timestamp(created + timedelta(seconds=300)),
        }
        if prior_run_id is not None:
            continuation, material, _ = continuation_material(backend, value, prior_run_id)
            value.update(continuation=continuation, continuation_material=material)
            if not claims and task_id == material["prior_task_id"]:
                if not material["prior_branch"]:
                    raise ExecutionError(
                        "continuation_lineage", "Prior claim has no retained branch"
                    )
                value["claim_branch"] = material["prior_branch"]
        if not claims:
            require_planned_source(cwd, task, source, state, branch=value.get("claim_branch"))
        assignment, preimages = preflight_assignment(value)
        revalidate_admission(backend, value, state)
        value.update(assignment=assignment, preimages=preimages)
        root = journal_root(cwd)
        write_receipt(root / "previews" / (value["preview_id"] + ".json"), value)
        return value
    finally:
        backend.close()


def confirm_run(cwd: Path, task_id: str, actor: str, preview_id: str) -> dict[str, Any]:
    return _confirm_execution(cwd, task_id, actor, preview_id)


def confirm_continue(
    cwd: Path, prior_run_id: str, actor: str, preview_id: str
) -> dict[str, Any]:
    identifier(prior_run_id)
    preview = read_execution_preview(journal_root(cwd), preview_id)
    continuation = preview.get("continuation")
    if not isinstance(continuation, dict) or continuation.get("prior_run_id") != prior_run_id:
        raise ExecutionError("preview_mismatch", "Preview belongs to another prior Run")
    return _confirm_execution(
        cwd, preview["task_id"], actor, preview_id, operation="continue")


def _confirm_execution(
    cwd: Path, task_id: str, actor: str, preview_id: str, *, operation: str = "run"
) -> dict[str, Any]:
    from anvil.cli._helpers import _open_backend
    from anvil.execution_runner import require_linux_capture

    require_linux_capture()
    require_isolated_state_layout()
    root = journal_root(cwd)
    preview = read_execution_preview(root, preview_id)
    if preview["claim"] is None:
        actor = canonicalize_new_actor(actor)
    if (
        preview["task_id"] != task_id
        or preview.get("operation", "run") != operation
        or preview["actor"] != actor
        or preview["cwd"] != str(cwd.resolve())
    ):
        raise ExecutionError(
            "preview_mismatch", "Preview belongs to another task, actor or checkout"
        )
    require_fresh_preview(preview)
    require_unconsumed_preview(root, preview_id)
    if dataclasses.asdict(load_execution_adapter()) != preview["adapter"]:
        raise ExecutionError("adapter_changed", "Adapter registration changed after preview")
    clean_source(cwd, preview["source_sha"])
    backend = _open_backend(root.parent, project_root=cwd)
    try:
        revalidate_admission(backend, preview, root.parent)
        assignment, preimages = preflight_assignment(preview)
        revalidate_admission(backend, preview, root.parent)
    finally:
        backend.close()
    if assignment != preview.get("assignment") or preimages != preview.get("preimages"):
        raise ExecutionError(
            "assignment_changed", "Execution work or preimages changed; preview again"
        )
    require_fresh_preview(preview)
    run_id = uuid.uuid4().hex
    slot = root / "slots" / (digest(task_id.encode()) + ".json")
    reserve_slot(
        root,
        slot,
        {"run_id": run_id, "task_id": task_id, "preview_id": preview_id},
        consume_preview=True,
    )
    run_directory = private_directory(root / "runs" / run_id)
    # From here every failure retains the slot. No caller retries a possible launch.
    write_receipt(run_directory / "admission.json", preview)
    if preview["claim"] is None:
        output = native_cli(
            cwd,
            "claim",
            task_id,
            "--actor",
            actor,
            "--worktree",
            *(["--branch", preview["claim_branch"]] if preview.get("claim_branch") else []),
            "--execution-scope",
            preview["scope_sha256"],
            "--execution-source",
            preview["source_sha"],
            "--json",
        )
        claim = json.loads(output)["data"]["claim"]
        cwd = Path(claim["worktree_path"])
    else:
        claim = preview["claim"]
    clean_source(cwd, preview["source_sha"])
    packet = native_cli(cwd, "packet", task_id, "--format", "json")
    with (run_directory / "packet.txt").open("xb") as stream:
        os.chmod(run_directory / "packet.txt", 0o600)
        stream.write(packet)
        stream.flush()
        os.fsync(stream.fileno())
    request = {
        **preview,
        "run_id": run_id,
        "cwd": str(cwd),
        "claim": claim,
        "packet_sha256": digest(packet),
    }
    backend = _open_backend(root.parent, project_root=cwd)
    try:
        live_claim(backend, request, remaining=preview["wall_seconds"] + 180)
        revalidate_continuation(backend, request)
        clean_source(cwd, preview["source_sha"])
    finally:
        backend.close()
    assignment = assignment_for(request, packet)
    if assignment != {**preview["assignment"], "repository": str(cwd)}:
        raise ExecutionError(
            "assignment_changed", "Claimed work differs from the admitted assignment"
        )
    portable = request_for(request, packet, assignment)
    write_receipt(run_directory / "context.json", request)
    write_receipt(run_directory / "adapter-task.json", assignment)
    write_receipt(run_directory / "request.json", dataclasses.asdict(portable))
    write_receipt(
        run_directory / "dispatch.json",
        {
            "run_id": run_id,
            "at": timestamp(),
            "request_sha256": portable.request_sha256,
            "request_file_sha256": digest(json_bytes(dataclasses.asdict(portable))),
            "context_sha256": digest(json_bytes(request)),
        },
    )
    with (run_directory / "coordinator.log").open("xb") as log:
        os.chmod(run_directory / "coordinator.log", 0o600)
        worker = subprocess.Popen(
            [sys.executable, "-I", "-m", "anvil.execution_runner", str(run_directory)],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    write_receipt(
        run_directory / "launched.json", {"pid": worker.pid, "run_id": run_id, "at": timestamp()}
    )
    return {"run_id": run_id, "state": "dispatched", "cwd": str(cwd), "claim_id": claim["id"]}


def reserve_slot(
    root: Path, slot: Path, value: dict[str, str], *, consume_preview: bool = False
) -> None:
    """Short per-task admission only; no Git, model or native mutation under this lock."""
    lock = slot.with_suffix(".lock")
    consumed = root / "consumed" / (identifier(value["preview_id"]) + ".json")
    try:
        write_receipt(lock, value)
    except FileExistsError as error:
        raise ExecutionError(
            "admission_busy", "Task admission is active or needs reconciliation"
        ) from error
    try:
        if consume_preview:
            require_unconsumed_preview(root, value["preview_id"])
        if receipt_attempt_exists(slot):
            existing = read_receipt(slot)
            previous = root / "runs" / identifier(existing["run_id"])
            finished = (
                read_receipt(previous / "result.json")
                if (previous / "result.json").exists()
                else {}
            )
            launched = (
                read_receipt(previous / "launched.json")
                if (previous / "launched.json").exists()
                else {}
            )
            stopped = False
            if type(launched.get("pid")) is int and launched["pid"] > 1:
                try:
                    os.killpg(launched["pid"], 0)
                except ProcessLookupError:
                    stopped = True
            if (
                finished.get("state")
                not in {"needs_review", "capabilities_ready", "failed_stopped"}
                or finished.get("local_children_stopped") is not True
                or not stopped
            ):
                raise ExecutionError(
                    "run_active", "Existing execution requires inspection: " + existing["run_id"]
                )
            if consume_preview:
                write_receipt(consumed, {"run_id": value["run_id"]})
            write_receipt(root / "retired" / (existing["run_id"] + ".json"), existing)
            slot.unlink()
            sync_directory(slot.parent)
        elif consume_preview:
            write_receipt(consumed, {"run_id": value["run_id"]})
        write_receipt(slot, value)
    finally:
        lock.unlink()
        sync_directory(lock.parent)


def inspect_run(cwd: Path, run_id: str) -> dict[str, Any]:
    directory = journal_root(cwd) / "runs" / identifier(run_id)
    admission = read_receipt(directory / "admission.json")
    result = {
        "run_id": run_id,
        "task_id": admission["task_id"],
        "state": "reconciliation_required",
        "acceptance": "not_granted",
    }
    if (directory / "result.json").exists():
        result.update(read_receipt(directory / "result.json"))
    elif (directory / "started.json").exists():
        result["state"] = "running_or_interrupted"
    result["continuation"] = {"state": "unavailable", "reason": "No verified saved return"}
    if (directory / "adapter-result.json").exists():
        try:
            request = ExecutionRequest.from_data(read_receipt(directory / "request.json"))
            returned = AdapterResult.from_data(
                read_receipt(directory / "adapter-result.json"), request)
            saved = returned.saved_conversation
            if (saved is not None and result["state"] in {"needs_review", "failed_stopped"}
                    and result.get("local_children_stopped") is True):
                result["continuation"] = {
                    "state": "requires_preview", "prior_run_id": run_id,
                    "conversation_id": saved["conversation_id"],
                    "official_session_id": saved["official_session_id"],
                    "reason": "Fresh native authority, source and checkpoint checks required",
                }
            if request.continuation is not None:
                result["prior_run_id"] = request.continuation["prior_run_id"]
        except (ExecutionError, OSError, ValueError, TypeError, KeyError):
            result["continuation"] = {"state": "unknown", "reason": "Incomplete saved return"}
    return result


def inspect_execution_preview(cwd: Path, preview_id: str, actor: str) -> dict[str, Any]:
    """Read one exact route after a lost response; inspection never dispatches."""
    from anvil.cli._helpers import _open_backend

    root = journal_root(cwd)
    preview = read_execution_preview(root, preview_id)
    if preview.get("claim") is None:
        actor = canonicalize_new_actor(actor)
    if preview.get("actor") != actor or preview.get("cwd") != str(cwd.resolve()):
        raise ExecutionError("preview_mismatch", "Preview belongs to another actor or checkout")
    backend = _open_backend(root.parent, project_root=cwd)
    try:
        if backend.get_project().id != preview.get("project_id"):
            raise ExecutionError("preview_mismatch", "Preview belongs to another project")
    finally:
        backend.close()
    operation = preview.get("operation", "run")
    prior = preview.get("continuation")
    result = {"preview_id": preview_id, "state": "unconsumed", "run_id": None,
              "operation": operation, "task_id": preview["task_id"],
              "prior_run_id": prior["prior_run_id"] if prior else None}
    consumed_path = root / "consumed" / (preview_id + ".json")
    if receipt_attempt_exists(consumed_path):
        result["state"] = "unknown"
        try:
            consumed = read_receipt(consumed_path)
            run_id = identifier(consumed["run_id"])
            directory = root / "runs" / run_id
            admission = read_receipt(directory / "admission.json")
            dispatch = strict_object(read_receipt(directory / "dispatch.json"), {
                "run_id", "at", "request_sha256", "request_file_sha256", "context_sha256",
            })
            request_raw = execution_file_bytes(directory / "request.json")
            context_raw = execution_file_bytes(directory / "context.json")
            request = ExecutionRequest.from_data(json.loads(request_raw))
            context = json.loads(context_raw)
            claim = Claim.model_validate(context["claim"])
            packet_sha256 = digest(execution_file_bytes(directory / "packet.txt"))
            if (set(consumed) != {"run_id"} or admission != preview
                    or dispatch.get("run_id") != run_id
                    or digest(request_raw) != dispatch.get("request_file_sha256")
                    or digest(context_raw) != dispatch.get("context_sha256")
                    or dispatch.get("request_sha256") != request.request_sha256
                    or context != {**preview, "run_id": run_id, "cwd": claim.worktree_path,
                                   "claim": context["claim"], "packet_sha256": packet_sha256}
                    or preview["claim"] is not None and context["claim"] != preview["claim"]
                    or request.dispatch_id != run_id or request.operation != operation
                    or request.continuation != prior or request.packet_sha256 != packet_sha256
                    or request.binding != claim_binding(preview["project_id"], claim)
                    or claim.task_id != preview["task_id"] or claim.claimed_by != preview["actor"]
                    or request.source["claim_worktree"] != context["cwd"]
                    or request.source["commit"] != preview["source_sha"]
                    or request.requested_options != preview["options"]):
                raise ExecutionError("preview_mismatch", "Consumed preview route is incomplete")
            result.update(state="consumed", run_id=run_id)
        except (ExecutionError, OSError, ValueError, TypeError, KeyError):
            pass
    else:
        slot = root / "slots" / (digest(preview["task_id"].encode()) + ".json")
        if receipt_attempt_exists(slot):
            try:
                reserved = strict_object(read_receipt(slot), {"run_id", "task_id", "preview_id"})
                identifier(reserved["run_id"])
                reserved_preview_id = identifier(reserved["preview_id"])
                if reserved["task_id"] != preview["task_id"]:
                    raise ExecutionError("preview_mismatch", "Slot belongs to another task")
                if reserved_preview_id == preview_id:
                    result["state"] = "unknown"
            except (ExecutionError, OSError, ValueError, TypeError, KeyError):
                result["state"] = "unknown"
    return result


def save_execution_capabilities(
    root: Path, configuration: dict[str, Any], registration: ExecutionAdapter, receipt_sha256: str
) -> None:
    """Project only verified RPC facts; never expose sessions, endpoints or argv."""
    models = configuration["available_models"]
    effective = configuration["effective"]
    levels = configuration["supported_thinking_levels"]
    if not isinstance(models, list) or not 1 <= len(models) <= 1000:
        raise ExecutionError("invalid_capabilities", "Invalid verified model catalog")
    sanitized = []
    for model in models:
        try:
            selection = RunOptions(provider=model["provider"], model=model["id"])
        except ExecutionError:
            # Pi's catalog permits identifiers beyond this portable contract.
            # Unselected, unrepresentable choices are never offered by Anvil.
            continue
        sanitized.append({"provider": selection.provider, "id": selection.model})
    selection = RunOptions(**{key: effective[key] for key in ("provider", "model", "thinking")})
    if (
        not isinstance(levels, list)
        or not levels
        or len(set(levels)) != len(levels)
        or any(
            level not in {"off", "minimal", "low", "medium", "high", "xhigh", "max"}
            for level in levels
        )
    ):
        raise ExecutionError("invalid_capabilities", "Invalid verified thinking levels")
    value = {
        "version": 1,
        "observed_at": timestamp(),
        "registration_sha256": digest(json_bytes(dataclasses.asdict(registration))),
        "adapter_sha256": registration.hashes["script"],
        "profile_sha256": registration.hashes["profile"],
        "receipt_sha256": receipt_sha256,
        "models": sanitized,
        "omitted_models": len(models) - len(sanitized),
        "effective": selection.data(),
        "thinking": {"provider": selection.provider, "model": selection.model, "levels": levels},
    }
    directory = private_directory(root / "capabilities")
    retained = directory / (uuid.uuid4().hex + ".json")
    write_receipt(retained, value)
    temporary = directory / (uuid.uuid4().hex + ".pointer")
    write_receipt(temporary, {"receipt": retained.name, "sha256": digest(json_bytes(value))})
    os.replace(temporary, directory / "current.json")
    sync_directory(directory)


def read_execution_capabilities(cwd: Path) -> dict[str, Any]:
    registration = load_execution_adapter()
    registration_sha256 = digest(json_bytes(dataclasses.asdict(registration)))
    unknown = {
        "version": 1,
        "status": "unknown",
        "registration_sha256": registration_sha256,
        "adapter_sha256": registration.hashes["script"],
        "profile_sha256": registration.hashes["profile"],
        "models": [],
    }
    directory = journal_root(cwd) / "capabilities"
    if not (directory / "current.json").exists():
        return unknown
    pointer = read_receipt(directory / "current.json")
    name = identifier(pointer["receipt"].removesuffix(".json")) + ".json"
    value = read_receipt(directory / name)
    if digest(json_bytes(value)) != pointer["sha256"]:
        raise ExecutionError("invalid_capabilities", "Capability receipt changed")
    if value["registration_sha256"] != registration_sha256:
        return unknown
    age = (now() - datetime.fromisoformat(value["observed_at"])).total_seconds()
    if not 0 <= age < 300:
        # Stale choices must not be offered as current selected-model capabilities.
        return {**unknown, "status": "stale", "observed_at": value["observed_at"]}
    return {**value, "status": "fresh"}


def refresh_execution_capabilities(
    cwd: Path, provider: str | None = None, model: str | None = None,
    expected_registration_sha256: str | None = None,
) -> dict[str, Any]:
    """An explicit bounded metadata action; Default Run never calls this."""
    from anvil.execution_runner import require_linux_capture

    require_linux_capture()
    registration = load_execution_adapter()
    if expected_registration_sha256 is not None:
        fingerprint(expected_registration_sha256)
        if digest(json_bytes(dataclasses.asdict(registration))) != expected_registration_sha256:
            raise ExecutionError("adapter_changed", "Metadata registration changed after preview")
    selected = RunOptions(provider=provider, model=model).data()
    if selected and not registration.registration["capabilities"]["saved_session_continue"]:
        raise ExecutionError(
            "unsupported_metadata", "Upgrade registration for selected-model metadata"
        )
    validate_adapter_contract(registration, cwd)
    root = journal_root(cwd)
    # A local unresolved Run must be reconciled, never adopted for metadata.
    for slot in (root / "slots").glob("*.json"):
        previous = read_receipt(slot)
        directory = root / "runs" / identifier(previous["run_id"])
        result = (
            read_receipt(directory / "result.json") if (directory / "result.json").exists() else {}
        )
        launched = (
            read_receipt(directory / "launched.json")
            if (directory / "launched.json").exists()
            else {}
        )
        stopped = False
        if type(launched.get("pid")) is int and launched["pid"] > 1:
            try:
                os.killpg(launched["pid"], 0)
            except ProcessLookupError:
                stopped = True
        if (
            result.get("state") not in {"needs_review", "capabilities_ready", "failed_stopped"}
            or not stopped
        ):
            raise ExecutionError(
                "adapter_busy", "An existing execution needs to stop or be reconciled"
            )
    run_id = uuid.uuid4().hex
    task_id = "capabilities:" + digest(json_bytes(dataclasses.asdict(registration)))
    directory = private_directory(root / "runs" / run_id)
    reserve_slot(
        root,
        root / "slots" / (digest(task_id.encode()) + ".json"),
        {"run_id": run_id, "task_id": task_id, "preview_id": run_id},
    )
    request = {
        "version": 1,
        "run_id": run_id,
        "task_id": task_id,
        "operation": "capabilities",
        "adapter": dataclasses.asdict(registration),
        "selected_model": selected or None,
    }
    write_receipt(directory / "admission.json", request)
    write_receipt(directory / "request.json", request)
    write_receipt(
        directory / "dispatch.json",
        {"run_id": run_id, "at": timestamp(), "request_sha256": digest(json_bytes(request))},
    )
    with (directory / "coordinator.log").open("xb") as log:
        os.chmod(directory / "coordinator.log", 0o600)
        child = subprocess.Popen(
            [sys.executable, "-I", "-m", "anvil.execution_runner", str(directory)],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    write_receipt(
        directory / "launched.json", {"pid": child.pid, "run_id": run_id, "at": timestamp()}
    )
    return {"run_id": run_id, "state": "dispatched"}
