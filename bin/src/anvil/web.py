"""Owner-enrolled, read-only web projection of canonical Anvil snapshots.

The initial Kanban view derives from user-owned fakoli/layerspeak PRs 20 and 21.
Anvil's MIT license applies; original attribution is retained.
Enrollment is private presentation configuration, never a second task database.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from anvil.read_contracts import EventCursorV1, ProjectSnapshotPayloadV1, snapshot_digest

MAX_CONFIG_BYTES = 65_536
MAX_OUTPUT_BYTES = 17 * 1024 * 1024
READER_TIMEOUT_SECONDS = 20
MAX_SCRATCH_FILE_BYTES = 64 * 1024 * 1024


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


def _reader_output(repository: Repository) -> bytes:
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
                        "--reader",
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
            or snapshot.get("operation_version") != 1
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
        # Publish the validated canonical model, including contract defaults.
        # Readers may legally omit optional arrays that the UI iterates.
        return {**snapshot, "payload": payload.model_dump(mode="json")}
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


# ponytail: one serialized reader; qualify capacity before introducing parallel project reads.
class WebHTTPServer(HTTPServer):
    """Serial reader budget; idle socket deadlines prevent a stalled viewer."""

    def get_request(self) -> tuple[Any, Any]:
        connection, address = super().get_request()
        connection.settimeout(5)
        return connection, address


def make_server(config_path: Path, port: int | None = None) -> WebHTTPServer:
    config = load_config(config_path)
    page = files("anvil").joinpath("_data/web/index.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        server: WebHTTPServer

        def do_GET(self) -> None:
            hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            origins = {f"http://{host}" for host in hosts}
            hosts.update(urlsplit(origin).netloc for origin in config.allowed_https_origins)
            origins.update(config.allowed_https_origins)
            origin = self.headers.get("Origin")
            if (
                self.headers.get("Host") not in hosts
                or (origin is not None and origin not in origins)
                or (
                    self.path.startswith("/api/")
                    and self.headers.get("Sec-Fetch-Site") == "cross-site"
                )
            ):
                self.send_error(403)
                return
            try:
                if self.path == "/":
                    body, content_type = page, "text/html; charset=utf-8"
                elif self.path == "/api/catalog":
                    body, content_type = (
                        json.dumps(catalog(load_config(config_path))).encode(),
                        "application/json",
                    )
                else:
                    prefix, suffix = "/api/repositories/", "/snapshot"
                    if not self.path.startswith(prefix) or not self.path.endswith(suffix):
                        self.send_error(404)
                        return
                    repository_id = self.path[len(prefix) : -len(suffix)]
                    repository = next(
                        (
                            item
                            for item in load_config(config_path).repositories
                            if item.id == repository_id
                        ),
                        None,
                    )
                    if repository is None:
                        self.send_error(404)
                        return
                    snapshot = read_snapshot(repository)
                    body = json.dumps(
                        {
                            "schema_id": "anvil.web.repository.v1",
                            "observed_at": datetime.now(UTC).isoformat(),
                            "machine": machine_identity(),
                            "repository": repository.model_dump(),
                            "snapshot": snapshot,
                        }
                    ).encode()
                    content_type = "application/json"
                if len(body) > MAX_OUTPUT_BYTES + MAX_CONFIG_BYTES:
                    raise WebError("response_limit", "Web response exceeds its bounded budget")
                self.send_response(200)
            except WebError as error:
                body = json.dumps(
                    {"ok": False, "error": {"code": error.code, "message": str(error)}}
                ).encode()
                content_type = "application/json"
                self.send_response(502)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

    return WebHTTPServer(("127.0.0.1", config.port if port is None else port), Handler)


def _exec_reader() -> None:
    argv = json.loads(sys.argv[2])
    if os.name == "posix":
        import resource

        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_SCRATCH_FILE_BYTES,) * 2)
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.execv(argv[0], argv)


if __name__ == "__main__":  # Internal worker; argv comes only from owner configuration.
    if len(sys.argv) != 3 or sys.argv[1] != "--reader":
        raise SystemExit(2)
    _exec_reader()
