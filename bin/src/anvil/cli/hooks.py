"""hook sub-app: check-claim, record-file-change, capture-evidence.

Internal helpers invoked by the plugin's bash hooks.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import typer

from anvil.cli._helpers import (
    _resolve_state_dir,
    resolve_actor,
)

hook_app = typer.Typer(
    name="hook",
    help="Internal hook helpers — invoked by the plugin's bash hooks.",
    no_args_is_help=True,
)

_VERIFICATION_PATTERNS = (
    "pytest",
    "ruff check",
    "mypy",
    "npm test",
    "cargo test",
    "bun test",
)
_HOOK_ENGINE_PROBE_TIMEOUT_SECONDS = 1.0
_MAX_HOOK_CONTEXT_BYTES = 4_096
_MAX_PLUGIN_MANIFEST_BYTES = 4_096
_MAX_VERSION_OUTPUT_BYTES = 1_024
_VERSION_PATTERN = re.compile(
    r"anvil ([A-Za-z0-9][A-Za-z0-9.+_-]{0,63}) \(schema ([0-9]{1,10})\)"
)
_HOOK_SCHEMA_PATTERN = re.compile(
    r"(?P<code>schema_mismatch|schema_probe_failed) "
    r"engine-version:(?P<engine>[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}) "
    r"supported-schema:(?P<supported>[0-9]{1,10}) "
    r"database-schema:(?P<database>[0-9]{1,10}|unknown) "
    r"direction:(?P<direction>newer|older|unknown) "
    r"remediation-code:(?P<remediation>[A-Za-z0-9_-]{1,64}) "
    r"restart-required:(?P<restart>true|false)"
)


@dataclass(frozen=True, slots=True)
class _EngineIdentity:
    engine_version: str
    schema_version: int


@dataclass(frozen=True, slots=True)
class _InstallationProbe:
    status: str
    engine_version: str | None = None
    schema_version: int | None = None


def _active_hook_identity() -> _EngineIdentity:
    from anvil import __version__
    from anvil.state.schema import get_schema_version

    return _EngineIdentity(__version__, get_schema_version())


def _probe_plugin_manifest() -> _InstallationProbe:
    """Read the packaged plugin version through a bounded closed parser."""
    root_raw = os.environ.get("CLAUDE_PLUGIN_ROOT")
    root = Path(root_raw) if root_raw else Path(__file__).resolve().parents[4]
    manifest = root / ".claude-plugin" / "plugin.json"
    try:
        with manifest.open("rb") as stream:
            content = stream.read(_MAX_PLUGIN_MANIFEST_BYTES + 1)
    except OSError:
        return _InstallationProbe("unavailable")
    if len(content) > _MAX_PLUGIN_MANIFEST_BYTES:
        return _InstallationProbe("malformed")
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _InstallationProbe("malformed")
    version = payload.get("version") if isinstance(payload, dict) else None
    if not isinstance(version, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}", version
    ):
        return _InstallationProbe("malformed")
    return _InstallationProbe("ok", engine_version=version)


def _create_windows_kill_job(process: subprocess.Popen[bytes]) -> int | None:
    """Put a Windows probe in a kill-on-close job so descendants cannot escape."""
    if os.name != "nt":
        return None
    job = None
    kernel32 = None
    try:
        import ctypes
        from ctypes import wintypes

        class _BasicLimit(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )]

        class _ExtendedLimit(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimit),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        limits = _ExtendedLimit()
        limits.BasicLimitInformation.LimitFlags = 0x00002000
        configured = kernel32.SetInformationJobObject(
            job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        )
        assigned = configured and kernel32.AssignProcessToJobObject(
            job, wintypes.HANDLE(int(process._handle))  # type: ignore[attr-defined]
        )
        if not assigned:
            kernel32.CloseHandle(job)
            job = None
            return None
        return int(job)
    except BaseException as exc:
        if job and kernel32 is not None:
            kernel32.CloseHandle(job)
        if isinstance(exc, Exception):
            # taskkill remains the safe fallback for ordinary API failures.
            return None
        raise


def _close_windows_job(job_handle: int | None) -> None:
    if job_handle is None or os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(
        wintypes.HANDLE(job_handle)
    )


def _terminate_probe_tree(
    process: subprocess.Popen[bytes], job_handle: int | None
) -> None:
    """Terminate the probe and all descendants, then reap the direct child."""
    if os.name == "nt":
        if job_handle is not None:
            _close_windows_job(job_handle)
        else:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=0.5,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            # Do not SIGKILL the watchdog while SIGTERM may be blocked around
            # target creation. It will consume the pending signal once its
            # target-group cleanup handler is installed; if this parent exits,
            # its parent-PID monitor performs the same cleanup.
            return
    if os.name == "nt" and process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass


def _probe_path_engine(*, which_fn=None, popen_fn=None) -> _InstallationProbe:  # noqa: ANN001
    """Probe trusted PATH with capped streams and a killable process tree.

    PATH resolution is an executable trust boundary, just as it is for an
    interactive ``anvil`` command. The one-second budget covers the contained
    probe response; the hook manifest's five-second timeout bounds synchronous
    operating-system process setup. Windows job handles and the POSIX worker's
    parent-death monitor tear down the contained process tree with this parent.
    """
    resolver = which_fn or shutil.which
    launcher = popen_fn or subprocess.Popen
    # SessionStart must report the independently installed CLI identity, so it
    # intentionally resolves PATH rather than importing another environment.
    executable = resolver("anvil")
    if not executable:
        return _InstallationProbe("unavailable")
    request = json.dumps(
        {"executable": executable, "parent_pid": os.getpid()},
        ensure_ascii=True,
    ).encode("utf-8")
    if len(request) > _MAX_VERSION_OUTPUT_BYTES:
        return _InstallationProbe("malformed")
    injected_launcher = popen_fn is not None
    try:
        process = launcher(
            [sys.executable, "-m", "anvil.cli._version_probe_worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            bufsize=0,
            start_new_session=os.name != "nt",
            creationflags=(
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            ),
        )
    except (OSError, ValueError):
        return _InstallationProbe("unavailable")

    job_handle: int | None = None
    stdout_buffer = bytearray()
    stderr_buffer = bytearray()
    overflow = threading.Event()
    readers: list[threading.Thread] = []

    def read_capped(stream, destination: bytearray) -> None:  # noqa: ANN001
        try:
            while True:
                chunk = stream.read(256)
                if not chunk:
                    return
                remaining = _MAX_VERSION_OUTPUT_BYTES + 1 - len(destination)
                destination.extend(chunk[:remaining])
                if len(destination) > _MAX_VERSION_OUTPUT_BYTES:
                    overflow.set()
                    return
        except (OSError, ValueError):
            overflow.set()

    timed_out = False
    try:
        job_handle = _create_windows_kill_job(process)
        deadline = time.monotonic() + _HOOK_ENGINE_PROBE_TIMEOUT_SECONDS
        if os.name == "nt" and job_handle is None and not injected_launcher:
            # The trusted worker has not received a target yet, so failure to
            # establish containment is safe: terminate it and do not launch.
            _terminate_probe_tree(process, None)
            return _InstallationProbe("unavailable")
        if time.monotonic() >= deadline or process.stdin is None:
            _terminate_probe_tree(process, job_handle)
            job_handle = None
            return _InstallationProbe("timeout")
        try:
            process.stdin.write(request + b"\n")
            process.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            _terminate_probe_tree(process, job_handle)
            job_handle = None
            return _InstallationProbe("malformed")

        readers = [
            threading.Thread(
                target=read_capped,
                args=(process.stdout, stdout_buffer),
                name="anvil-hook-version-stdout",
            ),
            threading.Thread(
                target=read_capped,
                args=(process.stderr, stderr_buffer),
                name="anvil-hook-version-stderr",
            ),
        ]
        for reader in readers:
            reader.start()
        while process.poll() is None and not overflow.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            overflow.wait(min(remaining, 0.01))
        if timed_out or overflow.is_set():
            _terminate_probe_tree(process, job_handle)
            job_handle = None
        else:
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
            # Tear down the containment boundary on success too: a hostile
            # version executable may print a valid line, exit zero, and leave
            # descendants behind.
            if injected_launcher:
                _close_windows_job(job_handle)
            else:
                _terminate_probe_tree(process, job_handle)
            job_handle = None
        for reader in readers:
            reader.join(timeout=0.5)
    except BaseException:
        _terminate_probe_tree(process, job_handle)
        job_handle = None
        for reader in readers:
            if reader.ident is not None:
                reader.join(timeout=0.5)
        return _InstallationProbe("timeout")
    finally:
        _close_windows_job(job_handle)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass

    if timed_out:
        return _InstallationProbe("timeout")
    if overflow.is_set() or process.returncode != 0 or stderr_buffer:
        return _InstallationProbe("malformed")
    try:
        stdout = stdout_buffer.decode("utf-8")
    except UnicodeDecodeError:
        return _InstallationProbe("malformed")
    matched = _VERSION_PATTERN.fullmatch(stdout.strip())
    if matched is None:
        return _InstallationProbe("malformed")
    schema = int(matched.group(2))
    if schema > 0xFFFFFFFF:
        return _InstallationProbe("malformed")
    return _InstallationProbe("ok", matched.group(1), schema)


def _identity_relationship(
    active: _EngineIdentity, probe: _InstallationProbe
) -> str:
    if probe.status != "ok":
        return probe.status
    if (
        probe.engine_version == active.engine_version
        and probe.schema_version == active.schema_version
    ):
        return "equal"
    if probe.engine_version == active.engine_version:
        return "equal-version/unequal-schema"
    if probe.schema_version == active.schema_version:
        return "unequal-version/schema-compatible"
    return "unequal-version/schema-incompatible"


def _probe_label(probe: _InstallationProbe) -> str:
    if probe.status != "ok":
        return probe.status
    if probe.schema_version is None:
        return str(probe.engine_version)
    return f"{probe.engine_version}/schema{probe.schema_version}"


def _numeric_version_order(candidate: str | None, active: str) -> int | None:
    """Compare ordinary dotted releases without guessing about prereleases."""
    if candidate == active:
        return 0
    if candidate is None or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", candidate):
        return None
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", active):
        return None
    candidate_parts = [int(part) for part in candidate.split(".")]
    active_parts = [int(part) for part in active.split(".")]
    width = max(len(candidate_parts), len(active_parts))
    candidate_key = tuple(candidate_parts + [0] * (width - len(candidate_parts)))
    active_key = tuple(active_parts + [0] * (width - len(active_parts)))
    return (candidate_key > active_key) - (candidate_key < active_key)


def _healthy_installation_actions(
    active: _EngineIdentity,
    manifest: _InstallationProbe,
    path_probe: _InstallationProbe,
) -> list[str]:
    """Target only the component proven stale by the reported identities."""
    actions: list[str] = []
    manifest_order = _numeric_version_order(
        manifest.engine_version if manifest.status == "ok" else None,
        active.engine_version,
    )
    path_order = _numeric_version_order(
        path_probe.engine_version if path_probe.status == "ok" else None,
        active.engine_version,
    )

    if path_probe.status != "ok":
        actions.append("repair the PATH anvil-state installation")
    elif path_probe.schema_version is not None:
        if (
            path_probe.engine_version == active.engine_version
            and path_probe.schema_version != active.schema_version
        ):
            actions.append("align the Anvil plugin and PATH to one build")
        elif path_probe.schema_version < active.schema_version or path_order == -1:
            actions.append("run `uv tool upgrade anvil-state`")
        elif path_probe.schema_version > active.schema_version or path_order == 1:
            if (
                manifest.status != "ok"
                or manifest.engine_version != path_probe.engine_version
            ):
                actions.append("update the Anvil plugin")
        elif path_order is None and manifest.engine_version != path_probe.engine_version:
            actions.append("align the Anvil plugin and PATH to one release")

    if manifest.status != "ok" or manifest_order == -1:
        actions.append("update the Anvil plugin")
    elif manifest_order is None and manifest.engine_version != path_probe.engine_version:
        actions.append("align the Anvil plugin and PATH to one release")

    actions.append("restart the harness/MCP server")
    return list(dict.fromkeys(actions))


def _render_schema_mismatch_context(status_line: str, language: str) -> str:
    """Enrich a closed hook record with bounded installation identities."""
    matched = _HOOK_SCHEMA_PATTERN.fullmatch(status_line)
    if matched is None:
        return (
            f"[anvil] Language: {language} | schema_mismatch | installation "
            "probe malformed | action: upgrade anvil-state, update the Anvil "
            "plugin, and restart the harness/MCP server. Do not delete state."
        )

    active = _active_hook_identity()
    manifest = _probe_plugin_manifest()
    path_probe = _probe_path_engine()
    relationship = _identity_relationship(active, path_probe)
    database_raw = matched.group("database")
    database_schema = int(database_raw) if database_raw != "unknown" else None

    actions: list[str] = []
    path_matches_database = (
        path_probe.status == "ok" and path_probe.schema_version == database_schema
    )
    if matched.group("code") == "schema_probe_failed":
        actions.append("retry when state is idle")
        if path_probe.status != "ok":
            actions.append("repair the PATH anvil-state installation")
    elif path_matches_database:
        if (
            active.engine_version == path_probe.engine_version
            and active.schema_version != path_probe.schema_version
        ):
            actions.append("align the Anvil plugin and PATH to one build")
        elif manifest.status == "ok" and manifest.engine_version == path_probe.engine_version:
            pass
        elif active.schema_version != path_probe.schema_version:
            actions.append("update the Anvil plugin to match PATH")
        else:
            if matched.group("direction") == "newer":
                actions.append("update the Anvil plugin to match PATH")
            else:
                actions.append("align the Anvil plugin with PATH")
    elif (
        matched.group("direction") == "newer"
        and database_schema is not None
        and (
            path_probe.status != "ok"
            or path_probe.schema_version is None
            or path_probe.schema_version < database_schema
        )
    ):
        actions.append("run `uv tool upgrade anvil-state`")
        actions.append("update the Anvil plugin after the engine upgrade")
    else:
        actions.append(
            f"install an Anvil engine compatible with database schema {database_raw}"
        )
        actions.append("align the Anvil plugin with that engine")
    actions.append("restart the harness/MCP server")

    context = (
        f"[anvil] Language: {language} | {matched.group('code')} | "
        f"active-hook:{active.engine_version}/schema{active.schema_version} | "
        f"plugin-manifest:{_probe_label(manifest)} | "
        f"PATH:{_probe_label(path_probe)} ({relationship}) | "
        f"database:schema{database_raw} ({matched.group('direction')}) | "
        f"action:{'; '.join(dict.fromkeys(actions))}. Do not delete state."
    )
    if len(context.encode("utf-8")) <= _MAX_HOOK_CONTEXT_BYTES:
        return context
    return (
        f"[anvil] Language: {language} | schema_mismatch | action: align "
        "Anvil installations and restart the harness/MCP server. Do not "
        "delete state."
    )


def _render_healthy_installation_context(
    status_line: str, language: str
) -> str:
    """Preserve the status banner unless an installation component is skewed."""
    active = _active_hook_identity()
    manifest = _probe_plugin_manifest()
    path_probe = _probe_path_engine()
    relationship = _identity_relationship(active, path_probe)
    manifest_equal = (
        manifest.status == "ok" and manifest.engine_version == active.engine_version
    )
    if manifest_equal and relationship == "equal":
        return f"[anvil] Language: {language} | {status_line}"

    actions = _healthy_installation_actions(active, manifest, path_probe)
    context = (
        f"[anvil] Language: {language} | {status_line} | install-skew "
        f"active-hook:{active.engine_version}/schema{active.schema_version} "
        f"plugin-manifest:{_probe_label(manifest)} "
        f"PATH:{_probe_label(path_probe)} ({relationship}) "
        f"database:schema{active.schema_version} | "
        f"action:{'; '.join(dict.fromkeys(actions))}"
    )
    if len(context.encode("utf-8")) <= _MAX_HOOK_CONTEXT_BYTES:
        return context
    return (
        f"[anvil] Language: {language} | install-skew | action: align Anvil "
        "installations and restart the harness/MCP server"
    )


def _read_hook_payload() -> dict[str, object]:
    import sys

    try:
        raw = "" if sys.stdin.isatty() else sys.stdin.read()
        if not raw.strip():
            return {}
        loaded = json.loads(raw)
        return loaded if isinstance(loaded, dict) else {}
    except Exception:  # noqa: BLE001 - hook dispatch must never break the harness
        return {}


def _payload_cwd(payload: dict[str, object]) -> Path | None:
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        return Path(cwd)
    return None


def _project_cwd(cwd: Path | None) -> Path:
    return (cwd or Path.cwd()).resolve()


def _has_any_anvil_state(cwd: Path | None) -> bool:
    project = _project_cwd(cwd)
    home_raw = os.environ.get("HOME")
    home = Path(home_raw).expanduser() if home_raw else Path.home()
    return (
        (project / ".anvil").is_dir()
        or (project / "bin" / ".anvil").is_dir()
        or (home / ".anvil" / "workspaces").is_dir()
    )


def _payload_tool_input(payload: dict[str, object]) -> dict[str, object]:
    tool_input = payload.get("tool_input")
    return tool_input if isinstance(tool_input, dict) else {}


def _payload_file_path(payload: dict[str, object]) -> str:
    tool_input = _payload_tool_input(payload)
    value = tool_input.get("path") or tool_input.get("notebook_path") or ""
    return str(value) if value is not None else ""


def _payload_actor(payload: dict[str, object], default: str = "unknown") -> str:
    value = payload.get("session_id")
    actor = str(value).strip() if value is not None else ""
    return actor or default


def _outside_project(file_path: str, cwd: Path | None) -> bool:
    path = Path(file_path)
    if not path.is_absolute():
        return False
    try:
        path.resolve().relative_to(_project_cwd(cwd))
        return False
    except ValueError:
        return True
    except OSError:
        return True


def _run_hook_callable(fn, *args, **kwargs) -> None:  # noqa: ANN001, ANN002, ANN003
    try:
        fn(*args, **kwargs)
    except typer.Exit:
        pass
    except SystemExit:
        pass
    except Exception:  # noqa: BLE001 - dispatch must preserve the hook contract
        pass


def _language_for_cwd(cwd: Path | None) -> str:
    root = _project_cwd(cwd)
    detected = "unknown"
    if (root / "Cargo.toml").is_file():
        detected = "Rust"
    if (root / "pyproject.toml").is_file():
        detected = "Python"
    if (root / "setup.py").is_file():
        detected = "Python"
    if (root / "package.json").is_file():
        detected = "TypeScript"
    if (root / "tsconfig.json").is_file():
        detected = "TypeScript"
    return detected


def _status_hook_line(cwd: Path | None) -> tuple[str, int]:
    from anvil.cli.init_status import status

    stdout = io.StringIO()
    prior_logging_disable = logging.root.manager.disable

    class _DiscardText:
        def write(self, value: str) -> int:
            return len(value)

        def flush(self) -> None:
            return None

    try:
        logging.disable(logging.CRITICAL)
        with (
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(_DiscardText()),
        ):
            # Called programmatically (no Click context), so every Typer
            # Option must be passed explicitly — an omitted ``prd`` leaks the
            # OptionInfo sentinel into resolve_prd_id(), which crashes on
            # ``.strip()`` and degrades SessionStart to "status check
            # unavailable" for every initialized project.
            status(hook_format=True, prd=None, json_output=False, cwd=cwd)
    except typer.Exit as exc:
        code = int(exc.exit_code or 0)
    except SystemExit as exc:
        code = int(exc.code or 0) if isinstance(exc.code, int) else 1
    except Exception:  # noqa: BLE001
        return "", 1
    else:
        code = 0
    finally:
        logging.disable(prior_logging_disable)
    return stdout.getvalue().strip().splitlines()[0] if stdout.getvalue().strip() else "", code


def _emit_session_start_context(text: str) -> None:
    typer.echo(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": text,
                }
            }
        )
    )


def _dispatch_detect_state(payload: dict[str, object], cwd: Path | None) -> None:
    language = _language_for_cwd(cwd)
    root = _project_cwd(cwd)
    legacy = (root / ".anvil").is_dir() or (root / "bin" / ".anvil").is_dir()
    status_line, status_exit = _status_hook_line(cwd)

    if status_exit == 0 and status_line.startswith(
        ("schema_mismatch ", "schema_probe_failed ")
    ):
        _emit_session_start_context(
            _render_schema_mismatch_context(status_line, language)
        )
        return
    if status_exit == 0 and status_line and status_line != "uninitialized":
        _emit_session_start_context(
            _render_healthy_installation_context(status_line, language)
        )
        return
    if status_line == "uninitialized":
        if legacy:
            _emit_session_start_context(
                "[anvil] Language: "
                f"{language} | legacy in-repo .anvil found — run "
                "`anvil migrate-workspace` to move it into the home workspace"
            )
        else:
            _emit_session_start_context(
                "[anvil] not initialized in this project — run `anvil init` to start"
            )
        return
    reason = status_line or f"status check returned exit {status_exit}"
    _emit_session_start_context(
        f"[anvil] Language: {language} | status check unavailable: {reason}"
    )


def _dispatch_check_claim(payload: dict[str, object], cwd: Path | None) -> None:
    if not _has_any_anvil_state(cwd):
        return
    file_path = _payload_file_path(payload)
    if not file_path or _outside_project(file_path, cwd):
        return
    _run_hook_callable(
        hook_check_claim,
        file=file_path,
        actor=_payload_actor(payload),
        cwd=cwd,
    )


def _dispatch_record_file_change(payload: dict[str, object], cwd: Path | None) -> None:
    if not _has_any_anvil_state(cwd):
        return
    file_path = _payload_file_path(payload)
    if not file_path:
        return
    tool = str(payload.get("tool_name") or "unknown")
    _run_hook_callable(
        hook_record_file_change,
        file=file_path,
        tool=tool,
        actor=_payload_actor(payload),
        cwd=cwd,
    )


def _dispatch_capture_evidence(payload: dict[str, object], cwd: Path | None) -> None:
    if not _has_any_anvil_state(cwd):
        return
    tool_input = _payload_tool_input(payload)
    response_raw = payload.get("tool_response")
    tool_response = response_raw if isinstance(response_raw, dict) else {}
    command = str(tool_input.get("command") or "")
    if not command or not any(pattern in command for pattern in _VERIFICATION_PATTERNS):
        return
    try:
        exit_code = int(tool_response.get("exit_code") or 0)
    except (TypeError, ValueError):
        exit_code = 0

    tmp_paths: list[Path] = []
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as out:
            out.write(str(tool_response.get("stdout") or ""))
            stdout_path = Path(out.name)
        tmp_paths.append(stdout_path)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as err:
            err.write(str(tool_response.get("stderr") or ""))
            stderr_path = Path(err.name)
        tmp_paths.append(stderr_path)
        _run_hook_callable(
            hook_capture_evidence,
            command=command,
            exit_code=exit_code,
            stdout_file=stdout_path,
            stderr_file=stderr_path,
            actor=_payload_actor(payload),
            cwd=cwd,
        )
    finally:
        for path in tmp_paths:
            try:
                path.unlink()
            except OSError:
                pass


def _dispatch_heartbeat(_payload: dict[str, object], cwd: Path | None) -> None:
    if not _has_any_anvil_state(cwd):
        return
    _run_hook_callable(hook_heartbeat, actor=None, cwd=cwd)


@hook_app.command("dispatch")
def hook_dispatch(
    name: str = typer.Argument(  # noqa: B008
        ...,
        help="Hook dispatcher name: detect-state, check-claim, record-file-change, "
        "capture-evidence, or heartbeat.",
    ),
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory. Defaults to the hook payload's cwd, then current dir.",
        hidden=True,
    ),
) -> None:
    """Shell-free dispatcher for hooks/hooks.json.

    This keeps the shipped hook manifest portable across Windows, Linux, and macOS:
    the harness launches ``uv`` directly, this command parses the hook JSON payload,
    and the existing hook subcommands perform the state work. All dispatch paths are
    non-blocking and exit 0 by construction.
    """
    payload = _read_hook_payload()
    resolved_cwd = cwd or _payload_cwd(payload)
    try:
        dispatch = {
            "detect-state": _dispatch_detect_state,
            "check-claim": _dispatch_check_claim,
            "record-file-change": _dispatch_record_file_change,
            "capture-evidence": _dispatch_capture_evidence,
            "heartbeat": _dispatch_heartbeat,
        }.get(name)
        if dispatch is not None:
            dispatch(payload, resolved_cwd)
    except Exception:  # noqa: BLE001 - hook dispatch must never break the harness
        pass
    raise typer.Exit(code=0)


@hook_app.command("check-claim")
def hook_check_claim(
    file: str = typer.Option(..., "--file", help="Path of the file about to be modified."),  # noqa: B008,A002
    actor: str = typer.Option(..., "--actor", help="Session actor / session_id."),  # noqa: B008
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory. Defaults to the current working directory.",
        hidden=True,
    ),
) -> None:
    """Used by hooks/check-claim.sh — exit 0 always; output goes to stderr.

    Checks whether FILE is within the scope of an active claim.
    - If FILE is in expected_files of a claim by THIS actor: silent exit 0.
    - If FILE is in expected_files of a claim by ANOTHER actor: warn to stderr.
    - If no active claims exist: silent exit 0.
    """
    # Defer all imports inside the body — this hook fires on every file edit,
    # so startup latency is the primary concern.
    try:
        from anvil.clock import SystemClock as _SystemClock
        from anvil.state.sqlite import SqliteBackend as _SqliteBackend

        state_dir = _resolve_state_dir(cwd)
        if not state_dir.exists():
            raise typer.Exit(code=0)

        db_path = str(state_dir / "state.db")
        events_path = str(state_dir / "events.jsonl")
        backend = _SqliteBackend(
            db_path=db_path,
            events_path=events_path,
            clock=_SystemClock(),
        )
        backend.initialize()
        try:
            active_claims = backend.list_active_claims()
        finally:
            backend.close()

        if not active_claims:
            raise typer.Exit(code=0)

        normalized = file.lstrip("./")
        for active_claim in active_claims:
            # Normalize expected_files the same way for comparison.
            claim_files = {f.lstrip("./") for f in active_claim.expected_files}
            if normalized in claim_files or file in claim_files:
                if active_claim.claimed_by != actor:
                    typer.echo(
                        f"[anvil:check-claim] WARNING: file '{file}' is "
                        f"in the scope of claim '{active_claim.id}' owned by "
                        f"'{active_claim.claimed_by}', not '{actor}'.",
                        err=True,
                    )
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        pass  # hook must never block the tool
    raise typer.Exit(code=0)


@hook_app.command("record-file-change")
def hook_record_file_change(
    file: str = typer.Option(..., "--file", help="Path of the file that was modified."),  # noqa: B008,A002
    tool: str = typer.Option(..., "--tool", help="Tool name (Edit, Write, NotebookEdit)."),  # noqa: B008
    actor: str = typer.Option(..., "--actor", help="Session actor / session_id."),  # noqa: B008
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory. Defaults to the current working directory.",
        hidden=True,
    ),
) -> None:
    """Used by hooks/record-file-change.sh — appends a file_changed event.

    Writes a file_changed event to both the SQLite events table and events.jsonl.
    Exits 0 always; any failure is silently swallowed so the hook never blocks
    the tool that triggered it.
    """
    # Defer all imports — this hook fires on every file write; keep startup fast.
    try:
        from anvil.clock import SystemClock as _SystemClock
        from anvil.state.models import EventDraft as _EventDraft
        from anvil.state.sqlite import SqliteBackend as _SqliteBackend

        state_dir = _resolve_state_dir(cwd)
        if not state_dir.exists():
            raise typer.Exit(code=0)

        db_path = str(state_dir / "state.db")
        events_path = str(state_dir / "events.jsonl")
        clock = _SystemClock()
        backend = _SqliteBackend(
            db_path=db_path,
            events_path=events_path,
            clock=clock,
        )
        backend.initialize()
        try:
            now = clock.now()
            draft = _EventDraft(
                timestamp=now,
                actor=actor or "hook",
                action="file_changed",
                target_kind="file",
                target_id=file,
                payload_json={
                    "file": file,
                    "tool": tool,
                    "actor": actor,
                    "changed_at": now.isoformat(),
                },
            )
            backend.append(draft)
        finally:
            backend.close()
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        pass  # hook must never block the tool
    raise typer.Exit(code=0)


def _resolve_capture_claim(active_claims: list, actor: str) -> str | None:
    """Pick the claim a captured proof belongs to (evidence-contracts:T007).

    The retro incident: a claim made with an explicit ``--actor`` different
    from the session-derived actor never accumulated CommandProofs, because
    the hook required ``claimed_by == actor`` — silently disabling the
    strongest audit feature anvil has. Now a single active claim OWNS every
    capture regardless of actor; with several, an exact actor match
    disambiguates; anything still ambiguous falls back to the orphan buffer
    (never cross-attach a proof to the wrong claim).
    """
    if not active_claims:
        return None
    if len(active_claims) == 1:
        return active_claims[0].id
    actor_matches = [c for c in active_claims if c.claimed_by == actor]
    if len(actor_matches) == 1:
        return actor_matches[0].id
    return None  # zero or ambiguous actor matches among many claims → orphan


@hook_app.command("capture-evidence")
def hook_capture_evidence(
    command: str = typer.Option(..., "--command", help="Full bash command string that was run."),  # noqa: B008
    exit_code: int = typer.Option(..., "--exit-code", help="Exit code of the command."),  # noqa: B008
    stdout_file: Path | None = typer.Option(  # noqa: B008
        None,
        "--stdout-file",
        help="Path to a temp file containing the command's stdout.",
    ),
    stderr_file: Path | None = typer.Option(  # noqa: B008
        None,
        "--stderr-file",
        help="Path to a temp file containing the command's stderr.",
    ),
    actor: str = typer.Option(..., "--actor", help="Session actor / session_id."),  # noqa: B008
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory. Defaults to the current working directory.",
        hidden=True,
    ),
) -> None:
    """Append a verification-command capture to .anvil/.evidence-buffer/.

    Called by hooks/capture-evidence.sh after every bash tool invocation.
    Failures are swallowed — this hook must never break the session.
    Always exits 0.
    """
    # All failures are silently swallowed — hook must never break the session.
    try:
        import datetime

        state_dir = _resolve_state_dir(cwd)
        if not state_dir.exists():
            raise typer.Exit(code=0)

        import hashlib

        # Read FULL stdout/stderr from temp files. The output hash is over the
        # full output (before truncation) so output_sha256 records what actually
        # ran, not a truncated excerpt (SL-3 / B48). The 4000-char excerpts are
        # kept only as human-readable descriptive metadata.
        stdout_raw = ""
        if stdout_file is not None:
            try:
                stdout_raw = stdout_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass

        stderr_raw = ""
        if stderr_file is not None:
            try:
                stderr_raw = stderr_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass

        output_sha256 = hashlib.sha256(
            (stdout_raw + stderr_raw).encode("utf-8")
        ).hexdigest()

        # Build the evidence record — a CommandProof-shaped buffer line that
        # ``anvil submit`` reconciles into Evidence.proofs. ``kind`` +
        # ``output_sha256`` are what make it a typed, observed proof.
        now = datetime.datetime.now(datetime.UTC)
        record: dict[str, object] = {
            "kind": "command",
            "timestamp": now.isoformat(),
            "command": command,
            "exit_code": exit_code,
            "output_sha256": output_sha256,
            "stdout_excerpt": stdout_raw[:4000],
            "stderr_excerpt": stderr_raw[:4000],
            "actor": actor,
        }

        # Determine which buffer file to append to by looking up the active claim.
        buffer_dir = state_dir / ".evidence-buffer"
        buffer_dir.mkdir(exist_ok=True)

        claim_id: str | None = None
        try:
            from anvil.clock import SystemClock as _SystemClock
            from anvil.state.sqlite import SqliteBackend as _SqliteBackend

            db_path = str(state_dir / "state.db")
            events_path = str(state_dir / "events.jsonl")
            _backend = _SqliteBackend(
                db_path=db_path,
                events_path=events_path,
                clock=_SystemClock(),
            )
            _backend.initialize()
            try:
                claim_id = _resolve_capture_claim(
                    list(_backend.list_active_claims()), actor
                )
            finally:
                _backend.close()
        except Exception:  # noqa: BLE001
            pass  # if the DB is unavailable, fall through to orphan

        if claim_id is not None:
            buffer_file = buffer_dir / f"{claim_id}.json"
        else:
            # No active claim found — write to orphan buffer. Recovery path
            # uses the existing `submit --output-file` flag; the previously-
            # referenced `evidence attach` subcommand did not exist (Critic-2
            # flagged that following the error message produced Typer's
            # "No such command 'evidence'" error).
            record["note"] = (
                "orphan — no active claim found at capture time; "
                "pass this file via: anvil submit TASK_ID --output-file <THIS_FILE>"
            )
            buffer_file = buffer_dir / "orphan.json"

        # Append the JSON record as a single line (JSONL).
        with buffer_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        pass  # hook must never block the session

    raise typer.Exit(code=0)


@hook_app.command("stop-gate")
def hook_stop_gate(
    actor: str | None = typer.Option(  # noqa: B008
        None,
        "--actor",
        help="Actor whose active claims to gate. Defaults to $ANVIL_GATE_ACTOR or 'agent'.",
    ),
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory. Defaults to the Stop payload's cwd, then the current dir.",
        hidden=True,
    ),
) -> None:
    """Stop-hook EVIDENCE GATE for Codex / Claude Code (B41 — OPT-IN).

    The Codex/Claude analogue of the OpenClaw before_agent_finalize finish-gate:
    when the turn ends with a claimed anvil task that has no submitted verification
    evidence, emit ``{"decision":"block","reason":...}`` on stdout AND exit 2 (a
    continuation prompt on stderr too) to force one more pass; otherwise exit 0.

    NOT wired by default — anvil's bundled hooks are non-blocking by design
    (docs/design.md). Opt in by adding a Stop hook that runs ``anvil hook
    stop-gate`` (see docs/reference/codex.md), and trust it via ``/hooks``. Reuses
    ``gate-check``'s decision logic; default-OPEN on every uncertain path; loop-
    guarded via the payload's ``stop_hook_active``.
    """
    import sys

    # Best-effort parse of the Stop payload on stdin (stop_hook_active, cwd).
    payload: dict[str, object] = {}
    try:
        raw = "" if sys.stdin.isatty() else sys.stdin.read()
        if raw.strip():
            loaded = json.loads(raw)
            if isinstance(loaded, dict):
                payload = loaded
    except Exception:  # noqa: BLE001 — a malformed payload must not break the turn
        payload = {}

    # Loop guard: already inside a continuation we requested — never re-block.
    if payload.get("stop_hook_active"):
        raise typer.Exit(code=0)

    resolved_actor = resolve_actor(actor)
    payload_cwd = payload.get("cwd")
    resolved_cwd = cwd
    if resolved_cwd is None and isinstance(payload_cwd, str) and payload_cwd:
        resolved_cwd = Path(payload_cwd)

    # Default-OPEN: any resolution/read failure ⇒ allow the turn to end (exit 0).
    try:
        from anvil.cli._helpers import _open_backend
        from anvil.cli.gate_check import _read_actor_rows, decide_from_rows

        state_dir = _resolve_state_dir(resolved_cwd)
        if not state_dir.exists():
            raise typer.Exit(code=0)
        backend = _open_backend(state_dir)
        try:
            rows = _read_actor_rows(backend, resolved_actor)
        finally:
            backend.close()
        decision = decide_from_rows(resolved_actor, rows)
    except typer.Exit:
        raise
    except Exception:  # noqa: BLE001 — never break the turn on an anvil error
        raise typer.Exit(code=0) from None

    if not decision.get("block"):
        raise typer.Exit(code=0)

    reason = str(decision.get("instruction") or "Submit verification evidence before finishing.")
    # Emit BOTH contracts: Codex/Claude honor {"decision":"block","reason":...} on
    # stdout; the exit-2-with-stderr-reason path is the fallback for harnesses that
    # read the continuation prompt from stderr.
    typer.echo(json.dumps({"decision": "block", "reason": reason}))
    typer.echo(reason, err=True)
    raise typer.Exit(code=2)


def _warn_expiring_leases(
    backend,  # noqa: ANN001
    clock,  # noqa: ANN001
    actor: str,
    warn_minutes: float,
    state_dir: Path,
) -> None:
    """Emit ONE stderr warning per claim per threshold-crossing (T008).

    Debounced via a plain marker file in the state tmp dir (no extra DB
    round-trip: the hook fires on every PostToolUse). Crossing back above
    the threshold — a successful renew — removes the marker so a later
    crossing warns again. Best-effort: every error is swallowed by the
    caller's hook guard.
    """
    now = clock.now()
    markers_dir = state_dir / "tmp"
    for claim in backend.list_active_claims():
        if claim.claimed_by != actor:
            continue
        remaining = (claim.lease_expires_at - now).total_seconds() / 60.0
        marker = markers_dir / f"lease-warn-{claim.id}"
        if remaining < warn_minutes:
            if not marker.exists():
                typer.echo(
                    f"[anvil:lease] WARNING: claim {claim.id} "
                    f"(task {claim.task_id}) lease expires in "
                    f"{max(remaining, 0):.0f}m — commit progress or run "
                    f"'anvil renew {claim.id}'.",
                    err=True,
                )
                markers_dir.mkdir(parents=True, exist_ok=True)
                marker.touch()
        else:
            marker.unlink(missing_ok=True)


@hook_app.command("heartbeat")
def hook_heartbeat(
    actor: str | None = typer.Option(  # noqa: B008
        None,
        "--actor",
        help="Actor whose claim lease(s) to renew (default $ANVIL_GATE_ACTOR or 'agent').",
    ),
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory. Defaults to the current working directory.",
        hidden=True,
    ),
) -> None:
    """PostToolUse lease HEARTBEAT (B41) — renew the actor's active claim lease(s)
    on tool activity so a lazy lease stays fresh while real work is happening.

    Purely side-effecting and non-blocking: always exits 0, swallows every error
    (an expired lease raises — that is fine, the next claim/reclaim handles it).
    Wired into the bundled PostToolUse hooks (cross-harness, Claude + Codex).
    """
    resolved_actor = resolve_actor(actor)
    try:
        from anvil.claims.manager import ClaimManager
        from anvil.cli._helpers import (
            _lease_manager_kwargs,
            _load_config_optional,
            _open_backend,
        )
        from anvil.clock import SystemClock

        state_dir = _resolve_state_dir(cwd)
        if not state_dir.exists():
            raise typer.Exit(code=0)
        clock = SystemClock()
        backend = _open_backend(state_dir)
        try:
            cfg = _load_config_optional(state_dir)
            lease_kwargs = _lease_manager_kwargs(cfg, lease_override=None)
            # Session filter (distinct-actor fail-fast, v10): under a shared
            # pinned ANVIL_ACTOR this loop's heartbeat must renew only ITS OWN
            # claims — renewing a sibling loop's lease is the corruption the
            # retro corpus documented. NULL sessions (either side) renew as
            # before: local-first, never guess.
            from anvil.naming import session_discriminator

            _hb_session = session_discriminator()
            claim_ids = [
                c.id for c in backend.list_active_claims()
                if c.claimed_by == resolved_actor
                and (
                    _hb_session is None
                    or c.session_id is None
                    or c.session_id == _hb_session
                )
            ]
            for claim_id in claim_ids:
                try:
                    manager = ClaimManager(backend, clock, actor=resolved_actor, **lease_kwargs)
                    manager.renew(claim_id)
                except Exception:  # noqa: BLE001 — expired/contended lease: skip, not fatal
                    pass

            # retro-opps T008 — pre-expiry advisory warning. Runs AFTER the
            # renew loop and reads the post-renew lease_expires_at, whatever
            # the B46 progress gate decided — the progress-gated decline is
            # precisely the case where a lease silently dies mid-work.
            warn_minutes = (
                cfg.lease_warning_minutes if cfg is not None else 10.0
            )
            if warn_minutes > 0:
                _warn_expiring_leases(
                    backend, clock, resolved_actor, warn_minutes, state_dir
                )
        finally:
            backend.close()
    except typer.Exit:
        raise
    except Exception:  # noqa: BLE001 — a heartbeat must never break the session
        pass

    raise typer.Exit(code=0)
