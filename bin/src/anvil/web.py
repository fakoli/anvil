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
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

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
    if not _READER_SLOTS.acquire(blocking=False):
        raise WebError("busy", "All readers are busy. Try again shortly.")
    try:
        return _bounded_reader_output(repository, mutation=mutation)
    finally:
        _READER_SLOTS.release()


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
        response = json.loads(_reader_output(worker, mutation=operation == "execute"))
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
        if operation == "execute" and error.code != "busy":
            raise WebError(
                "outcome_unknown",
                "The result could not be confirmed. Refresh the board "
                "and inspect the task before trying another change.",
            ) from error
        raise
    except (ValueError, TypeError) as error:
        raise WebError(
            "outcome_unknown" if operation == "execute" else "invalid_response",
            "The result could not be confirmed. Refresh and inspect the task before retrying.",
        ) from error
    if response["ok"] is False:
        raise WebError(response["error"]["code"], response["error"]["message"])
    data = response["data"]
    assert isinstance(data, dict)  # validated above; retain the typed return contract
    return data


class WebHTTPServer(ThreadingHTTPServer):
    """Eight bounded request threads; four bounded isolated readers across repositories."""

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
    preview_lock = threading.Lock()

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
                        inspected = _action_worker(repository, actor, "snapshot")
                        snapshot = _validate_snapshot(
                            repository, {"ok": True, "data": inspected["snapshot"]}
                        )
                        workflow = inspected["workflow"]
                    else:
                        snapshot = read_snapshot(repository)
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
                r"/api/repositories/([0-9a-f]{24})/(preview|transition)", self.path
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
                if match[2] == "preview":
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
