"""Owner-enrolled, read-only web projection of canonical Anvil snapshots.

The initial Kanban view derives from user-owned fakoli/layerspeak PRs 20 and 21.
Anvil's MIT license applies; original attribution is retained.
Enrollment is private presentation configuration, never a second task database.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import Future
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from anvil.read_contracts import (
    EventCursorV1,
    ProjectSnapshotDataV1,
    ProjectSnapshotPayloadV1,
    snapshot_digest,
)

MAX_CONFIG_BYTES = 65_536
MAX_OUTPUT_BYTES = 17 * 1024 * 1024
READER_TIMEOUT_SECONDS = 20
MAX_SCRATCH_FILE_BYTES = 64 * 1024 * 1024
MAX_ACTION_BYTES = 32_768
PREVIEW_LIFETIME_SECONDS = 300
_READER_SLOTS = threading.BoundedSemaphore(4)
_WRITER_SLOTS = threading.BoundedSemaphore(2)


class WebError(ValueError):
    """A bounded, user-readable configuration or reader refusal."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def default_config_path() -> Path:
    return Path.home() / ".anvil" / "web" / "config.json"


def validate_https_origins(origins: list[str]) -> list[str]:
    for origin in origins:
        parsed = urlsplit(origin)
        hostname = parsed.hostname or ""
        if (
            origin != f"https://{parsed.netloc}"
            or parsed.scheme != "https"
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or not all(
                character.isascii() and (character.isalnum() or character in ".-")
                for character in hostname
            )
            or parsed.netloc != hostname + (f":{parsed.port}" if parsed.port else "")
        ):
            raise ValueError(
                "Allowed origins must be exact HTTPS DNS origins without paths or credentials"
            )
    return origins


class Repository(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, validate_assignment=True)
    id: str = Field(pattern=r"^[0-9a-f]{24}$")
    label: str = Field(min_length=1, max_length=256)
    root: str = Field(min_length=1, max_length=4096)
    project_id: str = Field(min_length=1, max_length=128)
    reader_argv: list[str] = Field(min_length=1, max_length=32)
    reader_label: str = Field(min_length=1, max_length=512)
    expected_schema_version: int = Field(ge=1)

    @field_validator("reader_argv")
    @classmethod
    def validate_reader(cls, argv: list[str]) -> list[str]:
        if not Path(argv[0]).is_absolute() or any(
            not item or "\0" in item or len(item) > 4096 for item in argv
        ):
            raise ValueError(
                "Reader argv requires an absolute executable and bounded nonempty arguments"
            )
        return argv

    @field_validator("root")
    @classmethod
    def validate_root(cls, root: str) -> str:
        if not Path(root).is_absolute() or "\0" in root:
            raise ValueError("Repository root must be an absolute path")
        return root


class WebConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_id: Literal["anvil.web.config.v1"] = "anvil.web.config.v1"
    repositories: list[Repository] = Field(default_factory=list, max_length=16)
    allowed_https_origins: list[str] = Field(default_factory=list, max_length=4)
    port: int = Field(default=8780, ge=1, le=65535)

    @field_validator("allowed_https_origins")
    @classmethod
    def validate_origins(cls, origins: list[str]) -> list[str]:
        return validate_https_origins(origins)

    @field_validator("repositories")
    @classmethod
    def validate_repositories(cls, repositories: list[Repository]) -> list[Repository]:
        if len({repository.id for repository in repositories}) != len(repositories):
            raise ValueError("Repository identifiers must be unique")
        return repositories


def load_config(path: Path) -> WebConfig:
    if path.is_symlink():
        raise WebError("invalid_config", "Web config must not be a symlink")
    if not path.exists():
        return WebConfig()
    try:
        if not stat.S_ISREG(path.stat().st_mode) or (
            os.name == "posix" and path.stat().st_mode & 0o022
        ):
            raise WebError(
                "invalid_config",
                "Web config must be a regular file without group or public write access",
            )
        with path.open("rb") as handle:
            data = handle.read(MAX_CONFIG_BYTES + 1)
        if len(data) > MAX_CONFIG_BYTES:
            raise WebError("config_limit", "Web config exceeds its 64 KiB limit")
        return WebConfig.model_validate_json(data)
    except (OSError, ValidationError, ValueError) as error:
        if isinstance(error, WebError):
            raise
        raise WebError("invalid_config", "Web configuration is invalid or unavailable") from error


def save_config(path: Path, config: WebConfig) -> None:
    payload = config.model_dump_json(indent=2).encode()
    if len(payload) > MAX_CONFIG_BYTES:
        raise WebError("config_limit", "Web config exceeds its 64 KiB limit")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise WebError("invalid_config", "Refusing to replace a symlinked web config")
    descriptor, temporary = tempfile.mkstemp(prefix=".web-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def machine_identity() -> dict[str, str]:
    hostname = socket.gethostname()
    return {"id": hostname, "label": hostname, "kind": "local"}


def catalog(config: WebConfig) -> dict[str, Any]:
    return {
        "schema_id": "anvil.web.catalog.v1",
        "machine": machine_identity(),
        "repositories": [repository.model_dump() for repository in config.repositories],
    }


def _kill_reader(process: subprocess.Popen[bytes]) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass


def _reader_output(repository: Repository, *, mutation: bool = False) -> bytes:
    slots = _WRITER_SLOTS if mutation else _READER_SLOTS
    if not slots.acquire(blocking=False):
        raise WebError("busy", "All readers are busy. Try again shortly.")
    try:
        return _bounded_reader_output(repository, mutation=mutation)
    finally:
        slots.release()


def _bounded_reader_output(repository: Repository, *, mutation: bool = False) -> bytes:
    environment = os.environ.copy()
    for key in list(environment):
        if key in {"ANVIL_ROOT", "ANVIL_PRD", "ANVIL_STATE_LAYOUT", "PYTHONPATH"} or key.startswith(
            "GIT_"
        ):
            environment.pop(key, None)
    # Separate exec worker applies the POSIX scratch-file ceiling without the
    # unsafe preexec_fn-in-a-thread pattern. Windows retains output/time limits.
    with tempfile.TemporaryDirectory(prefix="anvil-web-reader-") as scratch:
        if not mutation:
            environment.update({"TMPDIR": scratch, "TEMP": scratch, "TMP": scratch})
        try:
            process = subprocess.Popen(
                (
                    [
                        sys.executable,
                        "-I",
                        "-m",
                        "anvil.web",
                        "--writer" if mutation else "--reader",
                        json.dumps(repository.reader_argv),
                    ]
                    if os.name == "posix"
                    else repository.reader_argv
                ),
                cwd=repository.root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=os.name == "posix",
                shell=False,
            )
        except OSError as error:
            raise WebError(
                "reader_unavailable", "Cannot start the configured snapshot reader"
            ) from error
        output = bytearray()

        def read_stdout() -> None:
            assert process.stdout is not None
            while len(output) <= MAX_OUTPUT_BYTES:
                chunk = process.stdout.read(min(65_536, MAX_OUTPUT_BYTES + 1 - len(output)))
                if not chunk:
                    return
                output.extend(chunk)
            _kill_reader(process)

        reader = threading.Thread(target=read_stdout, daemon=True)
        reader.start()
        try:
            process.wait(timeout=READER_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as error:
            _kill_reader(process)
            process.wait(timeout=2)
            reader.join(timeout=1)
            raise WebError(
                "reader_timeout", "Configured snapshot reader exceeded 20 seconds"
            ) from error
        finally:
            if process.poll() is None:
                _kill_reader(process)
        reader.join(timeout=1)
        if reader.is_alive():
            _kill_reader(process)
            raise WebError("reader_timeout", "Snapshot reader output did not close")
        if len(output) > MAX_OUTPUT_BYTES:
            raise WebError(
                "reader_output_limit", "Snapshot reader exceeded its 17 MiB output limit"
            )
        if process.returncode != 0:
            try:
                response = json.loads(output)
                reader_error = response.get("error", {}) if isinstance(response, dict) else {}
                code = (
                    reader_error.get("code", "reader_failed")
                    if isinstance(reader_error, dict)
                    else "reader_failed"
                )
            except (ValueError, TypeError, RecursionError):
                code = "reader_failed"
            raise WebError(
                str(code)[:128],
                "Configured reader exited unsuccessfully; inspect its schema and state",
            )
        return bytes(output)


def read_snapshot(repository: Repository) -> dict[str, Any]:
    try:
        response = json.loads(_reader_output(repository))
    except (ValueError, TypeError, RecursionError) as error:
        if isinstance(error, WebError):
            raise
        raise WebError("invalid_snapshot", "Configured reader returned malformed JSON") from error
    return _validate_snapshot(repository, response)


def _validate_snapshot(repository: Repository, response: object) -> dict[str, Any]:
    try:
        if not isinstance(response, dict) or response.get("ok") is not True:
            reader_error = response.get("error", {}) if isinstance(response, dict) else {}
            code = (
                reader_error.get("code", "reader_failed")
                if isinstance(reader_error, dict)
                else "reader_failed"
            )
            raise WebError(
                str(code)[:128],
                "Configured Anvil reader refused this project; inspect its schema and state",
            )
        snapshot = response["data"]
        if (
            not isinstance(snapshot, dict)
            or snapshot.get("truncated") is not False
            or snapshot.get("output_schema_id") != "anvil.state.project-snapshot.v1"
            or snapshot.get("operation_id") != "state.project.snapshot"
            or snapshot.get("digest_algorithm") != "sha256"
            or type(snapshot.get("schema_version")) is not int
            or not 1 <= snapshot["schema_version"] <= 2**63 - 1
            or type(snapshot.get("operation_version")) is not int
            or snapshot.get("operation_version") != 1
            or not isinstance(snapshot.get("api_version"), str)
            or re.fullmatch(r"[1-9][0-9]{0,8}", snapshot["api_version"]) is None
        ):
            raise WebError(
                "invalid_snapshot", "Reader returned an incomplete or unsupported snapshot"
            )
        payload = ProjectSnapshotPayloadV1.model_validate(snapshot["payload"])
        EventCursorV1.model_validate(snapshot["event_cursor"])
        if snapshot.get("snapshot_digest") != snapshot_digest(payload):
            raise WebError("snapshot_digest_mismatch", "Snapshot payload digest does not match")
        if payload.project.project_id != repository.project_id:
            raise WebError(
                "project_identity_changed",
                "Configured reader no longer identifies the enrolled project",
            )
        if snapshot.get("schema_version") != repository.expected_schema_version:
            raise WebError(
                "reader_schema_changed",
                "Observed reader schema differs from enrolled schema; reenroll deliberately",
            )
        canonical = ProjectSnapshotDataV1.model_validate(
            {
                key: snapshot.get(key)
                for key in ("payload", "event_cursor", "applied_limits", "snapshot_digest")
            }
        )
        # Publish the validated canonical model, including contract defaults.
        # Readers may legally omit optional arrays that the UI iterates.
        headers = {
            key: snapshot[key]
            for key in (
                "operation_id",
                "operation_version",
                "output_schema_id",
                "api_version",
                "schema_version",
                "digest_algorithm",
                "truncated",
            )
        }
        return {**headers, **canonical.model_dump(mode="json")}
    except WebError:
        raise
    except (ValueError, KeyError, TypeError, ValidationError) as error:
        raise WebError(
            "invalid_snapshot", "Configured reader returned malformed snapshot data"
        ) from error


# ponytail: enrollment has one writer; add a config lock if concurrent enrollment is needed.
def enroll_repository(
    path: Path,
    root: Path,
    label: str | None = None,
    reader_argv: list[str] | None = None,
    reader_label: str | None = None,
) -> Repository:
    config = load_config(path)
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise WebError("repository_unavailable", "Enrolled repository directory must exist")
    argv = (
        [sys.executable, "-I", "-m", "anvil.cli", "project", "snapshot", "--json"]
        if reader_argv is None
        else reader_argv
    )
    # First bounded read discovers identity without initializing or migrating.
    provisional = Repository(
        id=hashlib.sha256(str(root).encode()).hexdigest()[:24],
        label=label or root.name,
        root=str(root),
        project_id="pending",
        reader_argv=argv,
        reader_label=reader_label or "Configured Anvil CLI",
        expected_schema_version=1,
    )
    try:
        response = json.loads(_reader_output(provisional))
        snapshot = response["data"]
        provisional.project_id = snapshot["payload"]["project"]["project_id"]
        provisional.expected_schema_version = snapshot["schema_version"]
    except WebError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise WebError(
            "enrollment_refused",
            "Reader could not identify an existing Anvil project; no state was created",
        ) from error
    _validate_snapshot(provisional, response)
    config.repositories = [item for item in config.repositories if item.id != provisional.id] + [
        provisional
    ]
    # Revalidate assignment bounds before publishing private enrollment.
    save_config(path, WebConfig.model_validate(config.model_dump()))
    return provisional


class ActionPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    task_id: str = Field(pattern=r"^T[0-9]{3}(?:\.[0-9]+)*$", max_length=128)
    prd_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", max_length=128)
    action: Literal["ready", "backlog", "start", "block", "resume", "approve", "reject", "close"]
    task_version: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ActionConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    preview_id: str = Field(min_length=32, max_length=128)
    confirmed: Literal[True]
    reason: str | None = Field(default=None, max_length=4096)
    resolution: (
        Literal["duplicate", "cancelled", "wont_do", "obsolete", "cannot_reproduce"] | None
    ) = None
    duplicate_of: str | None = Field(default=None, max_length=256)

    @field_validator("confirmed", mode="before")
    @classmethod
    def require_explicit_confirmation(cls, value: Any) -> Any:
        if value is not True:
            raise ValueError("An explicit confirmation is required")
        return value


class BundlePreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    bundle_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$", max_length=128)


class BundleListRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    after_bundle_id: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$", max_length=128,
    )
    prd_id: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", max_length=128,
    )


class ExecutionOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    provider: str | None = Field(default=None, min_length=1, max_length=128)
    model: str | None = Field(default=None, min_length=1, max_length=255)
    thinking: str | None = Field(default=None, min_length=1, max_length=32)

    @model_validator(mode="after")
    def require_model_pair(self) -> ExecutionOptions:
        if (self.provider is None) != (self.model is None):
            raise ValueError("Provider and model are an exact pair")
        return self


class ExecutionPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    task_id: str = Field(pattern=r"^T[0-9]{3}(?:\.[0-9]+)*$", max_length=128)
    prd_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", max_length=128)
    task_version: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation: Literal["run", "continue"]
    prior_inspection_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{43}$")
    options: ExecutionOptions = Field(default_factory=ExecutionOptions)
    wall_seconds: int = Field(default=600, ge=1, le=1800)

    @model_validator(mode="after")
    def require_prior_run(self) -> ExecutionPreviewRequest:
        if (self.operation == "continue") != (self.prior_inspection_id is not None):
            raise ValueError("Only Continue requires a known prior run")
        return self


class ExecutionConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    preview_id: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")
    confirmed: Literal[True]

    @field_validator("confirmed", mode="before")
    @classmethod
    def require_true(cls, value: Any) -> Any:
        if value is not True:
            raise ValueError("Explicit confirmation is required")
        return value


class MetadataPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    provider: str | None = Field(default=None, min_length=1, max_length=128)
    model: str | None = Field(default=None, min_length=1, max_length=255)

    @model_validator(mode="after")
    def require_model_pair(self) -> MetadataPreviewRequest:
        ExecutionOptions(provider=self.provider, model=self.model)
        return self


class ExecutionInspectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    run_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class ExecutionStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    inspection_id: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")


class ExecutionContinuation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    state: Literal["unavailable", "requires_preview", "unknown"] = "unavailable"
    prior_run_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    reason: str = Field(max_length=256)


class ExecutionStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    operation: Literal["run", "continue", "metadata"]
    state: Literal[
        "unconsumed", "unknown", "dispatched", "running_or_interrupted",
        "reconciliation_required", "needs_review", "failed_stopped", "capabilities_ready",
    ]
    dispatch_state: Literal["unconsumed", "consumed", "unknown"] = "unknown"
    run_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    task_id: str | None = Field(default=None, max_length=255)
    acceptance: Literal["not_granted"] = "not_granted"
    local_children_stopped: bool | None = None
    continuation: ExecutionContinuation


class ExecutionPreview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    operation: Literal["run", "continue", "metadata"]
    title: str = Field(max_length=128)
    details: list[Annotated[str, Field(max_length=1024)]] = Field(max_length=16)
    requires_confirmation: Literal[True] = True
    options: ExecutionOptions = Field(default_factory=ExecutionOptions)


class ExecutionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    provider: str = Field(min_length=1, max_length=128)
    id: str = Field(min_length=1, max_length=255)


class ExecutionThinking(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    provider: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=255)
    levels: list[Annotated[str, Field(min_length=1, max_length=32)]] = Field(max_length=32)


class ExecutionCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    status: Literal["fresh", "stale", "unknown", "unavailable"]
    models: list[ExecutionModel] = Field(default_factory=list, max_length=1000)
    thinking: ExecutionThinking | None = None
    effective: ExecutionOptions | None = None
    observed_at: str | None = Field(default=None, max_length=64)


_EXECUTION_MUTATIONS = {"execution_preview", "execution_confirm", "metadata_confirm"}
_DISPATCH_OPERATIONS = {"execute", "execution_confirm", "metadata_confirm"}


def _native_writer(repository: Repository) -> bool:
    """A custom read command never implicitly grants execution authority."""
    from anvil.state.schema import get_schema_version

    return (
        repository.reader_argv
        == [sys.executable, "-I", "-m", "anvil.cli", "project", "snapshot", "--json"]
        and repository.expected_schema_version == get_schema_version()
    )


def _action_worker(
    repository: Repository, actor: str, operation: str, request: Any = None
) -> dict[str, Any]:
    arguments = [
        sys.executable,
        "-I",
        "-m",
        "anvil.web_actions",
        operation,
        json.dumps(
            {
                "repository": repository.model_dump(),
                "actor": actor,
                "request": request,
            }
        ),
    ]
    worker = repository.model_copy(update={"reader_argv": arguments})
    try:
        response = json.loads(_reader_output(
            worker, mutation=operation == "execute" or operation in _EXECUTION_MUTATIONS,
        ))
        if not isinstance(response, dict):
            raise ValueError("Invalid worker envelope")
        if response.get("ok") is False:
            failure = response.get("error")
            if not isinstance(failure, dict) or not all(
                isinstance(failure.get(key), str) and failure[key]
                for key in ("code", "message")
            ):
                raise ValueError("Invalid worker error")
        elif response.get("ok") is not True or not isinstance(response.get("data"), dict):
            raise ValueError("Invalid worker result")
    except WebError as error:
        if operation in _DISPATCH_OPERATIONS and error.code != "busy":
            raise WebError(
                "outcome_unknown",
                "The result could not be confirmed. Refresh the board "
                "and inspect the task before trying another change.",
            ) from error
        raise
    except (ValueError, TypeError) as error:
        raise WebError(
            "outcome_unknown" if operation in _DISPATCH_OPERATIONS else "invalid_response",
            "The result could not be confirmed. Refresh and inspect the task before retrying.",
        ) from error
    if response["ok"] is False:
        raise WebError(response["error"]["code"], response["error"]["message"])
    data = response["data"]
    assert isinstance(data, dict)  # validated above; retain the typed return contract
    return data


class WebHTTPServer(ThreadingHTTPServer):
    """Eight request threads; four isolated readers and two reserved writer slots."""

    daemon_threads = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.request_slots = threading.BoundedSemaphore(8)
        super().__init__(*args, **kwargs)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self.request_slots.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n"
                    b"Retry-After: 1\r\nConnection: close\r\n\r\n"
                )
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.request_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.request_slots.release()

    def get_request(self) -> tuple[Any, Any]:
        connection, address = super().get_request()
        connection.settimeout(5)
        return connection, address


def make_server(
    config_path: Path,
    port: int | None = None,
    *,
    allow_writes: bool = False,
    actor: str | None = None,
) -> WebHTTPServer:
    config = load_config(config_path)
    if allow_writes and (not actor or not actor.strip() or len(actor) > 256):
        raise WebError(
            "invalid_actor", "--allow-writes requires a nonempty --actor (at most 256 characters)."
        )
    if allow_writes:
        from anvil.actors import ActorIdentityError, canonicalize_new_actor

        try:
            actor = canonicalize_new_actor(actor or "")
        except ActorIdentityError as error:
            raise WebError("invalid_actor", str(error)) from error
    page = files("anvil").joinpath("_data/web/index.html").read_bytes()
    csrf_token = secrets.token_urlsafe(32)
    previews: dict[str, tuple[float, Repository, dict[str, Any]]] = {}
    # ponytail: 128 retained handles per server; persistent handles need a native API.
    inspections: dict[str, tuple[Repository, dict[str, Any]]] = {}
    preview_lock = threading.Lock()
    reads: dict[str, Future[dict[str, Any]]] = {}
    read_lock = threading.Lock()
    read_request_slots = threading.BoundedSemaphore(4)

    def retain_inspection(repository: Repository, binding: dict[str, Any]) -> str:
        inspection_id = secrets.token_urlsafe(32)
        with preview_lock:
            if len(inspections) >= 128:
                inspections.pop(next(iter(inspections)))
            inspections[inspection_id] = (repository, binding)
        return inspection_id

    def inspection_binding(repository: Repository, inspection_id: str) -> dict[str, Any]:
        with preview_lock:
            retained = inspections.get(inspection_id)
            if retained is None or retained[0] != repository:
                raise WebError(
                    "inspection_unknown",
                    "This inspection is unavailable. No execution was retried."
                )
            return dict(retained[1])

    def shared_read(
        repository: Repository, operation: str, request: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Include coalesced waiters in admission so slow board reads cannot
        # occupy every HTTP thread needed for a mutation.
        if not read_request_slots.acquire(blocking=False):
            raise WebError("busy", "Too many concurrent reads. Try again shortly.")
        try:
            # Each handler owns its top-level projection and opaque references.
            return dict(coalesced_read(repository, operation, request))
        finally:
            read_request_slots.release()

    def coalesced_read(
        repository: Repository, operation: str, request: dict[str, Any] | None,
    ) -> dict[str, Any]:
        # Coalesce only overlapping reads. No TTL cache: every later refresh
        # reads State again and every mutation keeps its own fresh engine checks.
        key = operation + repository.model_dump_json() + json.dumps(request, sort_keys=True)
        with read_lock:
            pending = reads.get(key)
            owner = pending is None
            if pending is None:
                pending = Future()
                reads[key] = pending
        if owner:
            try:
                if allow_writes and _native_writer(repository):
                    result = _action_worker(repository, actor or "", operation, request)
                else:
                    result = {"snapshot": read_snapshot(repository)}
                pending.set_result(result)
            except BaseException as error:
                pending.set_exception(error)
            finally:
                with read_lock:
                    reads.pop(key, None)
        return pending.result()

    class Handler(BaseHTTPRequestHandler):
        server: WebHTTPServer

        def _authorized_origin(self, *, mutation: bool = False) -> bool:
            hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            origins = {f"http://{host}" for host in hosts}
            hosts.update(urlsplit(origin).netloc for origin in config.allowed_https_origins)
            origins.update(config.allowed_https_origins)
            origin = self.headers.get("Origin")
            invalid = (
                len(self.headers.get_all("Host", [])) != 1
                or len(self.headers.get_all("Origin", [])) > 1
                or self.headers.get("Host") not in hosts
                or (origin is not None and origin not in origins)
                or (
                    self.path.startswith("/api/")
                    and self.headers.get("Sec-Fetch-Site") == "cross-site"
                )
                or (
                    mutation
                    and (origin is None or urlsplit(origin).netloc != self.headers.get("Host"))
                )
            )
            if invalid:
                self.send_error(403)
            return not invalid

        def _send(
            self, body: bytes, content_type: str = "application/json", status: int = 200
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if status == 503:
                self.send_header("Retry-After", "1")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def _refuse(self, error: WebError, *, mutation: bool = False) -> None:
            status = 503 if error.code == "busy" else (409 if mutation else 502)
            if error.code == "invalid_batch_query":
                status = 400
            self._send(
                json.dumps(
                    {
                        "ok": False,
                        "error": {
                            "code": error.code,
                            "message": str(error),
                        },
                    }
                ).encode(),
                status=status,
            )

        def _repository(self, repository_id: str) -> Repository:
            repository = next(
                (
                    item
                    for item in load_config(config_path).repositories
                    if item.id == repository_id
                ),
                None,
            )
            if repository is None:
                raise WebError("repository_not_found", "Repository is no longer enrolled.")
            return repository

        def do_GET(self) -> None:
            if not self._authorized_origin():
                return
            try:
                if self.path == "/":
                    body, content_type = page, "text/html; charset=utf-8"
                elif self.path == "/api/catalog":
                    data = catalog(load_config(config_path))
                    data.update(
                        csrf_token=csrf_token,
                        workflow={
                            "enabled": allow_writes,
                            "actor": actor if allow_writes else None,
                            "reason": None if allow_writes else "Read-only session.",
                        },
                    )
                    body, content_type = json.dumps(data).encode(), "application/json"
                else:
                    request_path, _, query = self.path.partition("?")
                    execution_match = re.fullmatch(
                        r"/api/repositories/([0-9a-f]{24})/(execution-capabilities|execution-status)",
                        request_path,
                    )
                    if execution_match:
                        repository = self._repository(execution_match[1])
                        if not allow_writes or not _native_writer(repository):
                            raise WebError(
                                "read_only_repository", "Execution needs a native writer."
                            )
                        if execution_match[2] == "execution-capabilities":
                            if query:
                                raise WebError(
                                    "invalid_request", "Capabilities takes no parameters."
                                )
                            result = shared_read(repository, "execution_capabilities")
                        else:
                            try:
                                if len(query) > 128:
                                    raise ValueError("Oversized inspection")
                                pairs = parse_qs(query, strict_parsing=True, max_num_fields=1)
                                if any(len(values) != 1 for values in pairs.values()):
                                    raise ValueError("Duplicate inspection")
                                selection = ExecutionStatusRequest.model_validate({
                                    key: values[0] for key, values in pairs.items()
                                })
                            except (ValueError, ValidationError) as error:
                                raise WebError(
                                    "invalid_request", "Invalid inspection handle."
                                ) from error
                            result = shared_read(repository, "execution_status", {
                                "binding": inspection_binding(repository, selection.inspection_id),
                            })
                            result["inspection_id"] = selection.inspection_id
                        self._send(json.dumps(result).encode())
                        return
                    bundle_match = re.fullmatch(
                        r"/api/repositories/([0-9a-f]{24})/bundles", request_path
                    )
                    if bundle_match:
                        try:
                            if len(query) > 1024:
                                raise ValueError("Query exceeds its bound")
                            parameters = parse_qs(
                                query, keep_blank_values=True, strict_parsing=True,
                                max_num_fields=2, errors="strict",
                            )
                            if any(len(values) != 1 for values in parameters.values()):
                                raise ValueError("Duplicate query parameter")
                            selection = BundleListRequest.model_validate({
                                key: values[0] for key, values in parameters.items()
                            })
                        except (ValueError, ValidationError) as error:
                            raise WebError(
                                "invalid_batch_query", "Invalid execution-batch page or campaign."
                            ) from error
                        repository = self._repository(bundle_match[1])
                        if not allow_writes or not _native_writer(repository):
                            raise WebError(
                                "read_only_repository", "Batch actions need a compatible writer."
                            )
                        bundle_body = json.dumps(shared_read(
                            repository, "bundles", selection.model_dump(),
                        )).encode()
                        if len(bundle_body) > MAX_OUTPUT_BYTES:
                            raise WebError("response_limit", "Batch response exceeds its budget.")
                        self._send(bundle_body)
                        return
                    prefix, suffix = "/api/repositories/", "/snapshot"
                    if not self.path.startswith(prefix) or not self.path.endswith(suffix):
                        self.send_error(404)
                        return
                    repository_id = self.path[len(prefix) : -len(suffix)]
                    try:
                        repository = self._repository(repository_id)
                    except WebError:
                        self.send_error(404)
                        return
                    if allow_writes and _native_writer(repository):
                        assert actor is not None
                        inspected = shared_read(repository, "snapshot")
                        snapshot = _validate_snapshot(
                            repository, {"ok": True, "data": inspected["snapshot"]}
                        )
                        workflow = inspected["workflow"]
                    else:
                        snapshot = shared_read(repository, "snapshot")["snapshot"]
                        workflow = {
                            "enabled": False,
                            "actions": {},
                            "versions": {},
                            "closures": {},
                            "reason": (
                                "This repository uses a separate reader; "
                                "changes need a compatible writer."
                            )
                            if allow_writes
                            else "Read-only session.",
                        }
                    body = json.dumps(
                        {
                            "schema_id": "anvil.web.repository.v1",
                            "observed_at": datetime.now(UTC).isoformat(),
                            "machine": machine_identity(),
                            "repository": repository.model_dump(),
                            "snapshot": snapshot,
                            "workflow": workflow,
                        }
                    ).encode()
                    content_type = "application/json"
                if len(body) > MAX_OUTPUT_BYTES + MAX_CONFIG_BYTES:
                    raise WebError("response_limit", "Web response exceeds its bounded budget")
            except WebError as error:
                self._refuse(error)
                return
            self._send(body, content_type)

        def do_POST(self) -> None:
            if not self._authorized_origin(mutation=True):
                return
            if not allow_writes:
                self.send_error(405)
                return
            supplied_csrf = self.headers.get("X-Anvil-CSRF", "")
            if (
                len(self.headers.get_all("X-Anvil-CSRF", [])) != 1
                or not supplied_csrf.isascii()
                or not secrets.compare_digest(supplied_csrf, csrf_token)
            ):
                self.send_error(403)
                return
            if (
                self.headers.get("Content-Type") != "application/json"
                or self.headers.get("Transfer-Encoding") is not None
                or len(self.headers.get_all("Content-Length", [])) != 1
            ):
                self.send_error(400)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if not 0 < length <= MAX_ACTION_BYTES:
                self.send_error(413)
                return
            match = re.fullmatch(
                r"/api/repositories/([0-9a-f]{24})/(preview|transition|bundle-preview|bundle-claim|"
                r"execution-preview|execution-confirm|execution-inspect|metadata-preview|metadata-confirm)",
                self.path,
            )
            if match is None:
                self.send_error(404)
                return
            try:
                repository = self._repository(match[1])
                if not _native_writer(repository):
                    raise WebError(
                        "read_only_repository",
                        "A separate reader cannot be used to change this repository.",
                    )
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise WebError("invalid_request", "Incomplete action request.")
                assert actor is not None
                if match[2] == "execution-inspect":
                    request = ExecutionInspectionRequest.model_validate_json(raw)
                    result = shared_read(repository, "execution_inspect", request.model_dump())
                    binding = result.pop("binding")
                    result["inspection_id"] = retain_inspection(repository, binding)
                    self._send(json.dumps(result).encode())
                    return
                if match[2] in {"execution-preview", "metadata-preview"}:
                    if match[2] == "execution-preview":
                        selection = ExecutionPreviewRequest.model_validate_json(raw)
                        execution_request = selection.model_dump(exclude_none=True)
                        if selection.prior_inspection_id is not None:
                            execution_request["prior_binding"] = inspection_binding(
                                repository, selection.prior_inspection_id,
                            )
                        result = _action_worker(
                            repository, actor, "execution_preview", execution_request,
                        )
                    else:
                        metadata = MetadataPreviewRequest.model_validate_json(raw)
                        result = _action_worker(
                            repository, actor, "metadata_preview",
                            metadata.model_dump(exclude_none=True),
                        )
                    binding = result.pop("binding")
                    preview_id = secrets.token_urlsafe(32)
                    with preview_lock:
                        now = time.monotonic()
                        for key, (expires, _, __) in list(previews.items()):
                            if expires <= now:
                                del previews[key]
                        if len(previews) >= 128:
                            raise WebError("busy", "Too many pending previews.")
                        previews[preview_id] = (now + PREVIEW_LIFETIME_SECONDS, repository, binding)
                    inspection_id = retain_inspection(repository, binding)
                    binding["inspection_id"] = inspection_id
                    result.update(preview_id=preview_id, inspection_id=inspection_id)
                    self._send(json.dumps(result).encode())
                    return
                if match[2] in {"execution-confirm", "metadata-confirm"}:
                    confirmation = ExecutionConfirmation.model_validate_json(raw)
                    with preview_lock:
                        pending = previews.pop(confirmation.preview_id, None)
                        if pending is None or pending[0] <= time.monotonic():
                            raise WebError(
                                "preview_expired", "Preview expired or was already used."
                            )
                        if pending[1] != repository:
                            raise WebError(
                                "repository_changed", "Repository configuration changed."
                            )
                        binding = pending[2]
                        expected = (
                            {"execution_run", "execution_continue"}
                            if match[2] == "execution-confirm" else {"execution_metadata"}
                        )
                        if binding.get("kind") not in expected:
                            raise WebError(
                                "preview_kind_mismatch", "Preview does not match this action."
                            )
                        binding["confirmation_attempted"] = True
                    result = _action_worker(
                        repository, actor, match[2].replace("-", "_"), {"binding": binding},
                    )
                    with preview_lock:
                        binding["run_id"] = result["run_id"]
                    result["inspection_id"] = binding["inspection_id"]
                    self._send(json.dumps(result).encode(), status=202)
                    return
                if match[2] in {"preview", "bundle-preview"}:
                    if match[2] == "bundle-preview":
                        bundle_request = BundlePreviewRequest.model_validate_json(raw)
                        result = _action_worker(
                            repository, actor, "bundle_preview", bundle_request.model_dump()
                        )
                    else:
                        request = ActionPreviewRequest.model_validate_json(raw)
                        result = _action_worker(repository, actor, "preview", request.model_dump())
                    binding = result.pop("binding")
                    preview_id = secrets.token_urlsafe(32)
                    with preview_lock:
                        now = time.monotonic()
                        for key, (expires, _, __) in list(previews.items()):
                            if expires <= now:
                                del previews[key]
                        if len(previews) >= 128:
                            raise WebError("busy", "Too many pending previews. Try again shortly.")
                        previews[preview_id] = (now + PREVIEW_LIFETIME_SECONDS, repository, binding)
                    result["preview_id"] = preview_id
                else:
                    confirmation = ActionConfirmation.model_validate_json(raw)
                    with preview_lock:
                        pending = previews.pop(confirmation.preview_id, None)
                    if pending is None or pending[0] <= time.monotonic():
                        raise WebError(
                            "preview_expired",
                            "This preview expired or was already used. Preview the move again.",
                        )
                    if pending[1] != repository:
                        raise WebError(
                            "repository_changed",
                            "Repository configuration changed. Preview the move again.",
                        )
                    bundle_claim = match[2] == "bundle-claim"
                    kind = pending[2].get("kind", "task_transition")
                    if kind != ("bundle_claim" if bundle_claim else "task_transition"):
                        raise WebError(
                            "preview_kind_mismatch", "Preview does not match this action."
                        )
                    if bundle_claim and any(
                        value is not None for value in (
                            confirmation.reason, confirmation.resolution, confirmation.duplicate_of
                        )
                    ):
                        raise WebError(
                            "invalid_request", "Batch claims do not accept task action fields."
                        )
                    result = _action_worker(
                        repository,
                        actor,
                        "execute",
                        {
                            **confirmation.model_dump(
                                exclude={"preview_id", "confirmed"}, exclude_none=True
                            ),
                            "binding": pending[2],
                        },
                    )
                self._send(json.dumps(result).encode())
            except ValidationError:
                self._refuse(WebError("invalid_request", "Invalid action fields."), mutation=True)
            except WebError as error:
                self._refuse(error, mutation=True)

    return WebHTTPServer(("127.0.0.1", config.port if port is None else port), Handler)


def _exec_reader() -> None:
    argv = json.loads(sys.argv[2])
    if os.name == "posix":
        import resource

        # A reader may only create bounded scratch files. A writer appends to
        # canonical State, whose size must not be capped by a scratch budget.
        if sys.argv[1] == "--reader":
            resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_SCRATCH_FILE_BYTES,) * 2)
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.execv(argv[0], argv)


if __name__ == "__main__":  # Internal worker; argv comes only from owner configuration.
    if len(sys.argv) != 3 or sys.argv[1] not in {"--reader", "--writer"}:
        raise SystemExit(2)
    _exec_reader()
