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
import types
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from anvil.cli._helpers import _open_backend, _require_state_dir, _resolve_state_dir
from anvil.config import ExecutionAdapter, execution_file_bytes, load_execution_adapter
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
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


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


def write_receipt(path: Path, value: Any) -> None:
    """Exclusive immutable receipt; a failed write remains evidence, not retry permission."""
    private_directory(path.parent)
    with path.open("xb") as stream:
        os.chmod(path, 0o600)
        stream.write(json_bytes(value))
        stream.flush()
        os.fsync(stream.fileno())
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
        for value in (self.provider, self.model):
            if value is not None and (
                not isinstance(value, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}", value)
                or "://" in value
            ):
                raise ExecutionError("unsupported_options", "Invalid provider or model identifier")
        if self.thinking not in {None, "off", "minimal", "low", "medium", "high", "xhigh", "max"}:
            raise ExecutionError(
                "unsupported_options", "Unsupported thinking; omit to use the default"
            )

    def data(self) -> dict[str, str]:
        return {key: value for key, value in dataclasses.asdict(self).items() if value is not None}


def adapter_module(registration: ExecutionAdapter) -> Any:
    """Import only code explicitly pinned by the owner, not a task-chosen plugin."""
    if load_execution_adapter() != registration:
        raise ExecutionError("adapter_changed", "Execution registration changed")
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


def validate_adapter_contract(registration: ExecutionAdapter) -> None:
    """Local validation in isolated Python, without project import fallback."""
    result = subprocess.run(
        [sys.executable, "-I", "-m", "anvil.execution_runner", "--validate-adapter"],
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode or result.stdout != json_bytes(dataclasses.asdict(registration)):
        raise ExecutionError(
            "adapter_unavailable", "Registered adapter lacks the required v2 contract"
        )


def native_cli(cwd: Path, *arguments: str, maximum: int = 2 * 1024 * 1024) -> bytes:
    environment = os.environ.copy()
    environment.pop("ANVIL_ROOT", None)
    environment.pop("ANVIL_PRD", None)
    result = subprocess.run(
        [sys.executable, "-I", "-m", "anvil.cli", *arguments, "--cwd", str(cwd)],
        cwd=cwd,
        env=environment,
        capture_output=True,
        timeout=120,
        check=False,
    )
    if len(result.stdout) + len(result.stderr) > maximum:
        raise ExecutionError("native_output_overflow", "Native command output exceeded its bound")
    if result.returncode:
        raise ExecutionError(
            "native_refused",
            result.stderr.decode(errors="replace")[-4000:]
            or result.stdout.decode(errors="replace")[-4000:],
        )
    return result.stdout


def git(cwd: Path, *arguments: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(cwd), *arguments], timeout=30)


def clean_source(cwd: Path, expected: str | None = None) -> str:
    head = git(cwd, "rev-parse", "HEAD").decode().strip()
    if expected is not None and head != expected:
        raise ExecutionError("source_changed", "Source revision changed")
    if git(cwd, "status", "--porcelain", "--untracked-files=all").strip():
        raise ExecutionError("dirty_source", "Run requires a clean isolated source checkout")
    return head


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
    state = _resolve_state_dir(cwd)
    _require_state_dir(state)
    return private_directory(state / "execution")


def preview_run(
    cwd: Path, task_id: str, actor: str, options: RunOptions, wall_seconds: int = 600
) -> dict[str, Any]:
    if os.name != "posix":
        raise ExecutionError(
            "unsupported_platform", "Owned Run currently requires POSIX; no claim was created"
        )
    if type(wall_seconds) is not int or not 1 <= wall_seconds <= 1800:
        raise ExecutionError("invalid_budget", "Run budget must be 1..1800 seconds")
    registration = load_execution_adapter()
    validate_adapter_contract(registration)  # no model/runtime call
    root = journal_root(cwd)
    backend = _open_backend(root.parent, project_root=cwd)
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
        created = now()
        value = {
            "version": 1,
            "preview_id": uuid.uuid4().hex,
            "task_id": task_id,
            "project_id": scope["project_id"],
            "actor": actor,
            "cwd": str(cwd.resolve()),
            "scope_sha256": digest(json_bytes(scope)),
            "source_sha": clean_source(cwd),
            "claim": claims[0].model_dump(mode="json") if claims else None,
            "writable_paths": paths,
            "commands": commands,
            "options": options.data(),
            "wall_seconds": wall_seconds,
            "adapter": dataclasses.asdict(registration),
            "created_at": timestamp(created),
            "expires_at": timestamp(created + timedelta(seconds=300)),
        }
        write_receipt(root / "previews" / (value["preview_id"] + ".json"), value)
        return value
    finally:
        backend.close()


def confirm_run(cwd: Path, task_id: str, actor: str, preview_id: str) -> dict[str, Any]:
    if os.name != "posix":
        raise ExecutionError("unsupported_platform", "Owned Run currently requires POSIX")
    root = journal_root(cwd)
    preview = read_receipt(root / "previews" / (identifier(preview_id) + ".json"))
    if (
        preview["task_id"] != task_id
        or preview["actor"] != actor
        or preview["cwd"] != str(cwd.resolve())
    ):
        raise ExecutionError(
            "preview_mismatch", "Preview belongs to another task, actor or checkout"
        )
    if (
        not datetime.fromisoformat(preview["created_at"])
        <= now()
        < datetime.fromisoformat(preview["expires_at"])
    ):
        raise ExecutionError("preview_expired", "Preview expired or the clock moved backwards")
    if dataclasses.asdict(load_execution_adapter()) != preview["adapter"]:
        raise ExecutionError("adapter_changed", "Adapter registration changed after preview")
    clean_source(cwd, preview["source_sha"])
    backend = _open_backend(root.parent, project_root=cwd)
    try:
        if scope_digest(backend, task_id) != preview["scope_sha256"]:
            raise ExecutionError("scope_changed", "Task scope changed after preview")
        current_claims = [
            item.model_dump(mode="json")
            for item in backend.list_active_claims()
            if item.task_id == task_id
        ]
        if current_claims != ([preview["claim"]] if preview["claim"] else []):
            raise ExecutionError("claim_changed", "Claim changed after preview")
    finally:
        backend.close()
    run_id = uuid.uuid4().hex
    run_directory = private_directory(root / "runs" / run_id)
    slot = root / "slots" / (digest(task_id.encode()) + ".json")
    reserve_slot(root, slot, {"run_id": run_id, "task_id": task_id, "preview_id": preview_id})
    # From here every failure retains the slot. No caller retries a possible launch.
    write_receipt(run_directory / "admission.json", preview)
    write_receipt(root / "consumed" / (preview_id + ".json"), {"run_id": run_id})
    if preview["claim"] is None:
        output = native_cli(
            cwd,
            "claim",
            task_id,
            "--actor",
            actor,
            "--worktree",
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
        clean_source(cwd, preview["source_sha"])
    finally:
        backend.close()
    write_receipt(run_directory / "request.json", request)
    write_receipt(
        run_directory / "dispatch.json",
        {"run_id": run_id, "at": timestamp(), "request_sha256": digest(json_bytes(request))},
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


def reserve_slot(root: Path, slot: Path, value: dict[str, str]) -> None:
    """Short per-task admission only; no Git, model or native mutation under this lock."""
    lock = slot.with_suffix(".lock")
    try:
        write_receipt(lock, value)
    except FileExistsError as error:
        raise ExecutionError(
            "admission_busy", "Task admission is active or needs reconciliation"
        ) from error
    try:
        if slot.exists():
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
                finished.get("state") not in {"needs_review", "capabilities_ready"}
                or finished.get("local_children_stopped") is not True
                or not stopped
            ):
                raise ExecutionError(
                    "run_active", "Existing execution requires inspection: " + existing["run_id"]
                )
            write_receipt(root / "retired" / (existing["run_id"] + ".json"), existing)
            slot.unlink()
            sync_directory(slot.parent)
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
        selection = RunOptions(provider=model["provider"], model=model["id"])
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


def refresh_execution_capabilities(cwd: Path) -> dict[str, Any]:
    """An explicit bounded metadata action; Default Run never calls this."""
    if os.name != "posix":
        raise ExecutionError(
            "unsupported_platform", "Capability refresh requires owned POSIX execution"
        )
    registration = load_execution_adapter()
    validate_adapter_contract(registration)
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
        if result.get("state") not in {"needs_review", "capabilities_ready"} or not stopped:
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
