"""Integrity-first native-Windows pytest timing harness.

The PowerShell entrypoint owns the stable public interface.  This helper keeps
the process, repository, and artifact contracts unit-testable without running
the expensive suite.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import ntpath
import os
import re
import statistics
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from contextlib import ExitStack
from ctypes import wintypes
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Sequence


FILE_ATTRIBUTE_REPARSE_POINT = 0x400
ERROR_ALREADY_EXISTS = 183
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
SYNCHRONIZE = 0x00100000
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
CREATE_NO_WINDOW = 0x08000000
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
CREATE_NEW = 1
FILE_ATTRIBUTE_NORMAL = 0x80
UNTRACKED_SCAN_EXCLUDES = (
    ".anvil-build/**",
    ".pytest_cache/**",
    ".ruff_cache/**",
    "artifacts/windows-pytest-timing-logs/**",
    "bin/.venv/**",
    "**/__pycache__/**",
)
TIMING_TEST_TARGETS = (
    "tests/test_git_ops.py",
    "tests/test_reconciliation.py",
)
TIMING_EXPECTED_NODE_COUNT = 167
TIMING_EXPECTED_NODE_IDS_SHA256 = (
    "05e7981aeb118af1a647f290dcd0410021e464b2f71ca1395ae7f7772a8ab769"
)
DEFENDER_STATUS_FIELDS = (
    "antivirus_enabled",
    "realtime_protection_enabled",
    "behavior_monitor_enabled",
    "ioav_protection_enabled",
    "tamper_protected",
)
DEFENDER_EXCLUSION_GROUPS = ("paths", "processes", "extensions")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def _safe_error(exc: BaseException) -> str:
    """Return a public-safe error classification, never an exception message."""
    message = str(exc)
    if re.fullmatch(r"[a-z0-9_]+", message):
        return message
    return type(exc).__name__


def _atomic_json(path: Path, value: Any) -> None:
    validate_no_reparse_components(path)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = (json.dumps(value, indent=2, sort_keys=False) + "\n").encode("utf-8")
    with temporary.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    validate_no_reparse_components(path)
    # Windows FlushFileBuffers on the file is the durable boundary available
    # without opening the parent directory with backup semantics.
    with path.open("r+b") as stream:
        os.fsync(stream.fileno())


def _is_reparse(path: Path) -> bool:
    stat_result = path.lstat()
    return path.is_symlink() or bool(
        getattr(stat_result, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
    )


def validate_no_reparse_components(path: Path) -> None:
    """Reject an existing reparse point anywhere in an absolute path."""
    absolute = Path(os.path.abspath(path))
    components = list(reversed((absolute, *absolute.parents)))
    for component in components:
        if component.exists() or component.is_symlink():
            if _is_reparse(component):
                raise ValueError("path_component_is_reparse_point")


def validate_child_path(repo: Path, candidate: Path, *, direct_parent: Path | None = None) -> Path:
    repo_absolute = Path(os.path.abspath(repo))
    candidate_absolute = Path(os.path.abspath(candidate))
    try:
        common = Path(os.path.commonpath((repo_absolute, candidate_absolute)))
    except ValueError as exc:
        raise ValueError("path_outside_repository") from exc
    if common != repo_absolute:
        raise ValueError("path_outside_repository")
    if direct_parent is not None and candidate_absolute.parent != Path(os.path.abspath(direct_parent)):
        raise ValueError("output_must_be_direct_artifacts_child")
    validate_no_reparse_components(candidate_absolute)
    return candidate_absolute


def _run_text(command: Sequence[str], *, cwd: Path, timeout: int = 60) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_controlled_environment(),
    )
    return completed.stdout.strip()


@dataclass(frozen=True)
class GitIdentity:
    commit: str
    tree: str
    index_tree: str


def require_clean_git(
    repo: Path,
    expected: GitIdentity | None = None,
    *,
    allowed_untracked: Iterable[str] = (),
) -> GitIdentity:
    commit = _run_text(("git", "rev-parse", "HEAD"), cwd=repo)
    tree = _run_text(("git", "rev-parse", "HEAD^{tree}"), cwd=repo)
    index_tree = _run_text(("git", "write-tree"), cwd=repo)
    status = subprocess.run(
        ("git", "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored=no"),
        cwd=repo,
        check=True,
        capture_output=True,
        env=_controlled_environment(),
    ).stdout
    allowed_records = {"?? " + path.replace("\\", "/") for path in allowed_untracked}
    records = {
        record.decode("utf-8", errors="surrogateescape").replace("\\", "/")
        for record in status.split(b"\0")
        if record
    }
    untracked_command = ["git", "ls-files", "--others", "-z"]
    for pattern in UNTRACKED_SCAN_EXCLUDES:
        untracked_command.extend(("--exclude", pattern))
    untracked = subprocess.run(
        untracked_command,
        cwd=repo,
        check=True,
        capture_output=True,
        env=_controlled_environment(),
    ).stdout
    all_untracked_records = {
        "?? " + record.decode("utf-8", errors="surrogateescape").replace("\\", "/")
        for record in untracked.split(b"\0")
        if record
    }
    flags = subprocess.run(
        ("git", "ls-files", "-v", "-z"),
        cwd=repo,
        check=True,
        capture_output=True,
        env=_controlled_environment(),
    ).stdout
    unsafe_index_flags = [
        record[:1]
        for record in flags.split(b"\0")
        if record and (record[:1].islower() or record.startswith(b"S "))
    ]
    if unsafe_index_flags:
        raise RuntimeError("tracked_file_has_hidden_index_flag")
    if (records | all_untracked_records) - allowed_records:
        raise RuntimeError("repository_not_fully_clean")
    identity = GitIdentity(commit=commit, tree=tree, index_tree=index_tree)
    if expected is not None and identity != expected:
        raise RuntimeError("repository_identity_changed")
    return identity


def _controlled_environment() -> dict[str, str]:
    allowed = {
        "APPDATA",
        "COMSPEC",
        "LOCALAPPDATA",
        "NUMBER_OF_PROCESSORS",
        "OS",
        "PATH",
        "PATHEXT",
        "PROCESSOR_ARCHITECTURE",
        "PROGRAMDATA",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
    }
    result = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    result.update(
        {
            "PYTHONHASHSEED": "0",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "UV_NO_PROGRESS": "1",
        }
    )
    return result


class WindowsNamedMutex:
    """Non-waiting Windows named mutex used for repo and output ownership."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.handle: int | None = None

    def __enter__(self) -> WindowsNamedMutex:
        if os.name != "nt":
            raise OSError("windows_mutex_required")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.ReleaseMutex.argtypes = (wintypes.HANDLE,)
        kernel32.ReleaseMutex.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        ctypes.set_last_error(0)
        handle = kernel32.CreateMutexW(None, True, self.name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            raise RuntimeError("measurement_mutex_already_held")
        self.handle = int(handle)
        return self

    def __exit__(self, *_: object) -> None:
        if self.handle is None:
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.ReleaseMutex.argtypes = (wintypes.HANDLE,)
        kernel32.ReleaseMutex.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.ReleaseMutex(wintypes.HANDLE(self.handle))
        kernel32.CloseHandle(wintypes.HANDLE(self.handle))
        self.handle = None


class ExclusiveOutput:
    """Reserve one absent output with CREATE_NEW and deny all sharing."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.stream: Any = None

    def __enter__(self) -> ExclusiveOutput:
        if os.name != "nt":
            raise OSError("windows_exclusive_output_required")
        validate_no_reparse_components(self.path)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        kernel32.CreateFileW.restype = wintypes.HANDLE
        handle = kernel32.CreateFileW(
            str(self.path),
            GENERIC_READ | GENERIC_WRITE,
            0,
            None,
            CREATE_NEW,
            FILE_ATTRIBUTE_NORMAL,
            None,
        )
        if handle == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        import msvcrt

        raw_handle = int(handle)
        try:
            descriptor = msvcrt.open_osfhandle(raw_handle, os.O_RDWR | os.O_BINARY)
        except BaseException:
            kernel32.CloseHandle(wintypes.HANDLE(raw_handle))
            raise
        try:
            self.stream = os.fdopen(descriptor, "r+b", buffering=0)
        except BaseException:
            os.close(descriptor)
            raise
        return self

    def write_json(self, value: Any) -> None:
        if self.stream is None:
            raise RuntimeError("exclusive_output_not_open")
        payload = (json.dumps(value, indent=2, sort_keys=False) + "\n").encode("utf-8")
        self.stream.seek(0)
        self.stream.truncate(0)
        self.stream.write(payload)
        self.stream.flush()
        os.fsync(self.stream.fileno())

    def __exit__(self, *_: object) -> None:
        if self.stream is not None:
            self.stream.close()
            self.stream = None


class WindowsEventGate:
    """A named event that remains unsignaled until Job assignment succeeds."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.handle: int | None = None

    def __enter__(self) -> WindowsEventGate:
        if os.name != "nt":
            raise OSError("windows_event_gate_required")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateEventW.argtypes = (
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        )
        kernel32.CreateEventW.restype = wintypes.HANDLE
        ctypes.set_last_error(0)
        handle = kernel32.CreateEventW(None, True, False, self.name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            raise RuntimeError("event_gate_already_exists")
        self.handle = int(handle)
        return self

    def signal(self) -> None:
        if self.handle is None:
            raise RuntimeError("event_gate_not_open")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.SetEvent.argtypes = (wintypes.HANDLE,)
        kernel32.SetEvent.restype = wintypes.BOOL
        if not kernel32.SetEvent(wintypes.HANDLE(self.handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def __exit__(self, *_: object) -> None:
        if self.handle is not None:
            ctypes.WinDLL("kernel32").CloseHandle(wintypes.HANDLE(self.handle))
            self.handle = None


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_int64),
        ("TotalKernelTime", ctypes.c_int64),
        ("ThisPeriodTotalUserTime", ctypes.c_int64),
        ("ThisPeriodTotalKernelTime", ctypes.c_int64),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


class WindowsJob:
    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("windows_job_required")
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        self.kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel32.SetInformationJobObject.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        )
        self.kernel32.SetInformationJobObject.restype = wintypes.BOOL
        self.kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        self.kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        self.kernel32.QueryInformationJobObject.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        )
        self.kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        self.kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        self.kernel32.TerminateJobObject.restype = wintypes.BOOL
        self.kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.kernel32.CloseHandle.restype = wintypes.BOOL
        self.handle = self.kernel32.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel32.SetInformationJobObject(
            self.handle,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())

    def assign(self, process: subprocess.Popen[bytes]) -> None:
        if not self.kernel32.AssignProcessToJobObject(self.handle, wintypes.HANDLE(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def active_processes(self) -> int:
        accounting = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
        if not self.kernel32.QueryInformationJobObject(
            self.handle,
            JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            ctypes.byref(accounting),
            ctypes.sizeof(accounting),
            None,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(accounting.ActiveProcesses)

    def wait_empty(self, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.active_processes() == 0:
                return True
            time.sleep(0.05)
        return self.active_processes() == 0

    def terminate(self, exit_code: int) -> None:
        if not self.kernel32.TerminateJobObject(self.handle, exit_code):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        if self.handle:
            self.kernel32.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self) -> WindowsJob:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _gate_main(arguments: Sequence[str]) -> int:
    gate_name = arguments[0]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenEventW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.OpenEventW.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    gate = kernel32.OpenEventW(SYNCHRONIZE, False, gate_name)
    if not gate:
        return 125
    try:
        timeout_ms = int(float(os.environ["ANVIL_GATE_TIMEOUT_SECONDS"]) * 1000)
        wait_result = kernel32.WaitForSingleObject(gate, timeout_ms)
        if wait_result == WAIT_TIMEOUT:
            return 125
        if wait_result != WAIT_OBJECT_0:
            return 126
    finally:
        kernel32.CloseHandle(gate)
    executable, *child_arguments = arguments[1:]
    # Spawning from the already-assigned gate keeps the measured process and
    # every descendant in the same non-breakaway Job Object.  os.exec* is not a
    # process replacement on Windows and can crash CPython, so keep the gate as
    # the root until its child exits.
    return subprocess.run((executable, *child_arguments), check=False, env=os.environ).returncode


def _power_source() -> tuple[str, bool]:
    class SYSTEM_POWER_STATUS(ctypes.Structure):
        _fields_ = [
            ("ACLineStatus", ctypes.c_ubyte),
            ("BatteryFlag", ctypes.c_ubyte),
            ("BatteryLifePercent", ctypes.c_ubyte),
            ("SystemStatusFlag", ctypes.c_ubyte),
            ("BatteryLifeTime", wintypes.DWORD),
            ("BatteryFullLifeTime", wintypes.DWORD),
        ]

    status = SYSTEM_POWER_STATUS()
    if not ctypes.WinDLL("kernel32").GetSystemPowerStatus(ctypes.byref(status)):
        return "unobservable", False
    return {0: "battery", 1: "ac"}.get(status.ACLineStatus, "unknown"), status.ACLineStatus in {0, 1}


def _normalize_lines(text: str) -> str:
    return "\n".join(" ".join(line.split()) for line in text.splitlines() if line.strip())


def _power_snapshot() -> dict[str, Any]:
    try:
        scheme = subprocess.run(
            ("powercfg", "/getactivescheme"),
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
            env=_controlled_environment(),
        ).stdout
        settings = subprocess.run(
            ("powercfg", "/query", "SCHEME_CURRENT"),
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
            env=_controlled_environment(),
        ).stdout
        source, source_observable = _power_source()
        guid_match = re.search(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", scheme)
        return {
            "observable": bool(guid_match) and source_observable,
            "active_scheme_guid": guid_match.group(0).lower() if guid_match else None,
            "full_settings_sha256": _sha256_bytes(_normalize_lines(settings).encode("utf-8")),
            "power_source": source,
            "error": None,
        }
    except Exception as exc:
        return {
            "observable": False,
            "active_scheme_guid": None,
            "full_settings_sha256": None,
            "power_source": "unobservable",
            "error": _safe_error(exc),
        }


def _normalized_exclusion_group(values: Iterable[Any]) -> dict[str, Any]:
    normalized = sorted(
        {
            ntpath.normcase(ntpath.normpath(str(value).strip().replace("/", "\\")))
            for value in values
            if str(value).strip()
        }
    )
    permission_limited = any(
        "n/a:" in value or "administrator" in value for value in normalized
    )
    return {
        "availability": "unavailable" if permission_limited else "available",
        "unavailable_reason": "permission_limited" if permission_limited else None,
        "count": None if permission_limited else len(normalized),
        "sha256": None if permission_limited else _sha256_json(normalized),
    }


def _as_values(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _defender_snapshot() -> dict[str, Any]:
    status_command = r"""
$ErrorActionPreference='Stop'
$s=Get-MpComputerStatus
[ordered]@{
  antivirus_enabled=[bool]$s.AntivirusEnabled
  realtime_protection_enabled=[bool]$s.RealTimeProtectionEnabled
  behavior_monitor_enabled=[bool]$s.BehaviorMonitorEnabled
  ioav_protection_enabled=[bool]$s.IoavProtectionEnabled
  tamper_protected=[bool]$s.IsTamperProtected
}|ConvertTo-Json -Compress
"""
    exclusions_command = r"""
$ErrorActionPreference='Stop'
$p=Get-MpPreference
[ordered]@{
  paths=@($p.ExclusionPath)
  processes=@($p.ExclusionProcess)
  extensions=@($p.ExclusionExtension)
}|ConvertTo-Json -Compress -Depth 3
"""
    try:
        status_completed = subprocess.run(
            (
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                status_command,
            ),
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
            env=_controlled_environment(),
        )
        status = json.loads(status_completed.stdout)
    except Exception as exc:
        status = None
        status_error = _safe_error(exc)
    else:
        status_error = None
        if not (
            isinstance(status, dict)
            and set(status) == set(DEFENDER_STATUS_FIELDS)
            and all(type(status[field]) is bool for field in DEFENDER_STATUS_FIELDS)
        ):
            status = None
            status_error = "defender_status_schema_invalid"

    try:
        exclusions_completed = subprocess.run(
            (
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                exclusions_command,
            ),
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
            env=_controlled_environment(),
        )
        raw_exclusions = json.loads(exclusions_completed.stdout)
        if not isinstance(raw_exclusions, dict) or set(raw_exclusions) != set(
            DEFENDER_EXCLUSION_GROUPS
        ):
            raise ValueError("defender_exclusions_schema_invalid")
        exclusions = {
            group: _normalized_exclusion_group(_as_values(raw_exclusions.get(group)))
            for group in DEFENDER_EXCLUSION_GROUPS
        }
    except Exception as exc:
        exclusions = None
        exclusions_error = _safe_error(exc)
    else:
        exclusions_error = None

    return {
        "status": status,
        "status_error": status_error,
        "exclusions": exclusions,
        "exclusions_error": exclusions_error,
        "error": status_error or exclusions_error,
    }


def control_snapshot() -> dict[str, Any]:
    power = _power_snapshot()
    defender = _defender_snapshot()
    snapshot = {"power": power, "defender": defender}
    # Get-MpPreference deliberately redacts exclusion values from a normal
    # Windows shell.  Those values are useful context when visible, but they
    # must not turn elevation into a benchmark prerequisite.  Bind the
    # comparison to the observable Defender status and record whether the
    # exclusion set was visible; include its values only when Windows exposes
    # them.  A change in visibility or any comparable control still fails the
    # measurement protocol.
    defender_status_comparable = bool(
        defender.get("status_error") is None
        and isinstance(defender.get("status"), dict)
        and set(defender["status"]) == set(DEFENDER_STATUS_FIELDS)
        and all(
            type(defender["status"][field]) is bool
            for field in DEFENDER_STATUS_FIELDS
        )
    )
    exclusions = defender.get("exclusions")
    defender_exclusions_comparable = bool(
        defender.get("exclusions_error") is None
        and isinstance(exclusions, dict)
        and set(exclusions) == set(DEFENDER_EXCLUSION_GROUPS)
        and all(
            isinstance(exclusions[group], dict)
            and exclusions[group].get("availability") in {"available", "unavailable"}
            for group in DEFENDER_EXCLUSION_GROUPS
        )
    )
    defender_exclusions_visibility = {
        group: exclusions[group]["availability"]
        for group in DEFENDER_EXCLUSION_GROUPS
    } if defender_exclusions_comparable else None
    comparison_payload = {
        "power": power,
        "defender_status": defender.get("status"),
        "defender_exclusions_visibility": defender_exclusions_visibility,
        "defender_exclusions": exclusions if defender_exclusions_comparable else None,
    }
    snapshot["comparison_ready"] = bool(
        power["observable"]
        and defender_status_comparable
        and defender_exclusions_comparable
    )
    snapshot["defender_exclusions_visibility"] = defender_exclusions_visibility
    snapshot["comparison_fingerprint_sha256"] = _sha256_json(comparison_payload)
    snapshot["observation_fingerprint_sha256"] = _sha256_json(snapshot)
    return snapshot


def _junit_evidence(path: Path) -> tuple[dict[str, int], str]:
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    # A testsuites root normally carries aggregate attributes.  Only sum leaf
    # suites when those aggregates are absent, avoiding nested double counts.
    if root.tag == "testsuites" and root.get("tests") is not None:
        suites = [root]
    result = {
        key: sum(int(float(suite.get(key, "0"))) for suite in suites)
        for key in ("tests", "failures", "errors", "skipped")
    }
    result["passed"] = result["tests"] - result["failures"] - result["errors"] - result["skipped"]
    testcase_ids = [
        "\x1f".join(
            (
                case.get("file", ""),
                case.get("classname", ""),
                case.get("name", ""),
            )
        )
        for case in root.iter("testcase")
    ]
    if len(testcase_ids) != result["tests"]:
        raise ValueError("junit_testcase_count_mismatch")
    if len(set(testcase_ids)) != len(testcase_ids):
        raise ValueError("junit_testcase_identity_duplicate")
    testcase_ids.sort()
    return result, _sha256_bytes(("\n".join(testcase_ids) + "\n").encode("utf-8"))


def _junit_counts_pass(counts: dict[str, int] | None, expected: int) -> bool:
    return counts == {
        "tests": expected,
        "failures": 0,
        "errors": 0,
        "skipped": 0,
        "passed": expected,
    }


def _distribution(values: Sequence[float]) -> dict[str, float | None]:
    if not values:
        return {"median": None, "min": None, "max": None, "mad": None, "stdev": None}
    median = statistics.median(values)
    return {
        "median": round(median, 4),
        "min": round(min(values), 4),
        "max": round(max(values), 4),
        "mad": round(statistics.median(abs(value - median) for value in values), 4),
        "stdev": round(statistics.pstdev(values), 4),
    }


def result_exit_code(
    *,
    probe: bool,
    measurement_valid: bool,
    affected_slice_median_budget_met: bool | None,
) -> int:
    if probe:
        return 0 if measurement_valid else 1
    return 0 if measurement_valid and affected_slice_median_budget_met else 1


@dataclass
class Configuration:
    repo: Path
    output: Path
    artifacts: Path
    cache: Path
    log_root: Path
    log_relative_root: str
    warmups: int
    samples: int
    workers: int
    timeout_seconds: int
    expected_commit: str | None
    host_label: str
    probe: bool


def _public_command(workers: int, junit_relative: str, probe: bool) -> list[str]:
    if probe:
        return ["python", "-c", "<fixed-probe>"]
    command = [
        "uv",
        "run",
        "--locked",
        "--exact",
        "--project",
        "bin",
        "pytest",
        *TIMING_TEST_TARGETS,
        "-q",
        "-n",
        str(workers),
        "--junitxml",
        junit_relative,
    ]
    return command


def _actual_command(config: Configuration, public: Sequence[str], gate: str) -> list[str]:
    if config.probe:
        child = [sys.executable, "-c", "print('probe passed')"]
    else:
        child = list(public)
        child[-1] = str(config.repo / child[-1])
    return [sys.executable, str(Path(__file__).resolve()), "_gate", str(gate), *child]


def _managed_process(
    config: Configuration,
    command: Sequence[str],
    *,
    gate: str,
    stdout_path: Path,
    stderr_path: Path,
) -> dict[str, Any]:
    environment = _controlled_environment()
    environment["UV_CACHE_DIR"] = str(config.cache)
    environment["ANVIL_GATE_TIMEOUT_SECONDS"] = str(config.timeout_seconds)
    start = time.perf_counter()
    process: subprocess.Popen[bytes] | None = None
    exit_code: int | None = None
    timed_out = False
    containment_verified = False
    process_error: str | None = None
    with stdout_path.open("xb") as stdout_stream, stderr_path.open("xb") as stderr_stream:
        try:
            with WindowsEventGate(gate) as event, WindowsJob() as job:
                process = subprocess.Popen(
                    command,
                    cwd=config.repo,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_stream,
                    stderr=stderr_stream,
                    env=environment,
                    creationflags=CREATE_NO_WINDOW,
                )
                try:
                    # The gate process cannot launch uv/pytest until assignment
                    # to the kill-on-close job succeeds.
                    job.assign(process)
                except Exception:
                    process.kill()
                    process.wait(timeout=15)
                    raise
                event.signal()
                try:
                    exit_code = process.wait(timeout=config.timeout_seconds)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    job.terminate(124)
                    exit_code = process.wait(timeout=15)
                containment_verified = job.wait_empty(15)
                if not containment_verified:
                    job.close()  # KILL_ON_JOB_CLOSE is the final fail-safe.
                    process_error = "JobActiveProcessesNonzero"
        except Exception as exc:
            process_error = process_error or _safe_error(exc)
            if process is not None and process.poll() is None:
                process.kill()
                try:
                    process.wait(timeout=15)
                except Exception:
                    process_error = "ProcessContainmentFailed"
    return {
        "elapsed_seconds": round(time.perf_counter() - start, 4),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "containment_verified": containment_verified,
        "process_error": process_error,
    }


def _write_checkpoint(config: Configuration, artifact: dict[str, Any]) -> None:
    checkpoint = config.log_root / "checkpoint.json"
    _atomic_json(checkpoint, artifact)


def _owned_output(config: Configuration) -> tuple[str]:
    return (config.output.relative_to(config.repo).as_posix(),)


def _run_one(
    config: Configuration,
    identity: GitIdentity,
    baseline_control: dict[str, Any],
    *,
    sequence: int,
    phase: str,
    sample: int | None,
    collection: dict[str, Any],
) -> dict[str, Any]:
    before_error: str | None = None
    try:
        require_clean_git(
            config.repo, identity, allowed_untracked=_owned_output(config)
        )
    except Exception as exc:
        before_error = _safe_error(exc)

    prefix = f"{sequence:02d}-{phase}-parallel"
    raw_relative = f"{config.log_relative_root}/{prefix}.log"
    junit_relative = f"{config.log_relative_root}/{prefix}.xml"
    raw_path = config.repo / raw_relative
    junit_path = config.repo / junit_relative
    stdout_path = config.log_root / f"{prefix}.stdout"
    stderr_path = config.log_root / f"{prefix}.stderr"
    gate = f"Local\\AnvilPytestTiming-Gate-{uuid.uuid4().hex}"
    public = _public_command(config.workers, junit_relative, config.probe)
    control_before = control_snapshot()
    process_result = {
        "elapsed_seconds": None,
        "exit_code": None,
        "timed_out": False,
        "containment_verified": False,
        "process_error": before_error,
    }
    if before_error is None:
        try:
            require_clean_git(
                config.repo, identity, allowed_untracked=_owned_output(config)
            )
            process_result = _managed_process(
                config,
                _actual_command(config, public, gate),
                gate=gate,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
        except Exception as exc:
            process_result["process_error"] = _safe_error(exc)
    control_after = control_snapshot()

    stdout = stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.exists() else ""
    stderr = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.exists() else ""
    raw = (
        f"command: {' '.join(public)}\n"
        f"exit_code: {process_result['exit_code']}\n"
        f"timed_out: {str(process_result['timed_out']).lower()}\n"
        "--- stdout ---\n"
        f"{stdout}\n"
        "--- stderr ---\n"
        f"{stderr}\n"
    )
    with raw_path.open("x", encoding="utf-8", newline="\n") as raw_stream:
        raw_stream.write(raw)
        raw_stream.flush()
        os.fsync(raw_stream.fileno())
    for temporary in (stdout_path, stderr_path):
        temporary.unlink(missing_ok=True)

    repository_after_error: str | None = None
    try:
        require_clean_git(
            config.repo, identity, allowed_untracked=_owned_output(config)
        )
    except Exception as exc:
        repository_after_error = _safe_error(exc)

    counts: dict[str, int] | None = None
    testcase_ids_sha256: str | None = None
    junit_error: str | None = None
    if config.probe and process_result["exit_code"] == 0:
        counts = {"tests": 1, "failures": 0, "errors": 0, "skipped": 0, "passed": 1}
        testcase_ids_sha256 = _sha256_bytes(b"probe::test\n")
    elif junit_path.exists():
        try:
            validate_no_reparse_components(junit_path)
            counts, testcase_ids_sha256 = _junit_evidence(junit_path)
        except Exception as exc:
            junit_error = _safe_error(exc)
    else:
        junit_error = "JUnitMissing"

    controls_match = (
        control_before["comparison_fingerprint_sha256"]
        == baseline_control["comparison_fingerprint_sha256"]
        and control_after["comparison_fingerprint_sha256"]
        == baseline_control["comparison_fingerprint_sha256"]
    )
    comparison_qualified = bool(
        baseline_control["comparison_ready"]
        and control_before["comparison_ready"]
        and control_after["comparison_ready"]
        and controls_match
    )
    count_matches = bool(counts and counts["tests"] == collection["count"])
    junit_counts_pass = _junit_counts_pass(counts, collection["count"])
    timing_valid = bool(
        before_error is None
        and repository_after_error is None
        and process_result["exit_code"] == 0
        and not process_result["timed_out"]
        and process_result["process_error"] is None
        and process_result["containment_verified"]
        and junit_error is None
        and count_matches
        and junit_counts_pass
    )
    return {
        "sequence": sequence,
        "phase": phase,
        "sample": sample,
        "mode": "parallel",
        "worker_count": config.workers,
        "command": public,
        "elapsed_seconds": process_result["elapsed_seconds"],
        "exit_code": process_result["exit_code"],
        "timed_out": process_result["timed_out"],
        "process_error": process_result["process_error"],
        "containment_verified": process_result["containment_verified"],
        "repository_before_error": before_error,
        "repository_after_error": repository_after_error,
        "junit_error": junit_error,
        "junit_counts": counts,
        "junit_counts_pass": junit_counts_pass,
        "junit_testcase_ids_sha256": testcase_ids_sha256,
        "collection_count_matches": count_matches,
        "control_before_fingerprint": control_before["comparison_fingerprint_sha256"],
        "control_after_fingerprint": control_after["comparison_fingerprint_sha256"],
        "controls_match": controls_match,
        "comparison_qualified": comparison_qualified,
        "timing_valid": timing_valid,
        "raw_log": {
            "relative_path": raw_relative,
            "sha256": _sha256_bytes(raw_path.read_bytes()),
        },
        "junit": {
            "relative_path": junit_relative if junit_path.exists() else None,
            "sha256": _sha256_bytes(junit_path.read_bytes()) if junit_path.exists() else None,
        },
    }


def _metadata_probe(config: Configuration) -> dict[str, Any]:
    code = """
import importlib.metadata as m,json,platform
eps=m.entry_points(group='pytest11')
plugins={}
for ep in eps:
    dist=getattr(ep,'dist',None)
    if dist:
        plugins[dist.metadata['Name']] = dist.version
print(json.dumps({'python':platform.python_version(),'pytest':m.version('pytest'),'pytest_xdist':m.version('pytest-xdist'),'anvil_state':m.version('anvil-state'),'pytest11_plugin_distributions':[{'name':name,'version':plugins[name]} for name in sorted(plugins,key=str.lower)]}))
"""
    try:
        output = _run_text(
            (
                "uv",
                "run",
                "--locked",
                "--exact",
                "--project",
                "bin",
                "python",
                "-c",
                code,
            ),
            cwd=config.repo,
            timeout=120,
        )
        value = json.loads(output)
        value["uv"] = _run_text(("uv", "--version"), cwd=config.repo)
        value["observable"] = True
        value["error"] = None
        return value
    except Exception as exc:
        return {
            "python": None,
            "pytest": None,
            "pytest_xdist": None,
            "anvil_state": None,
            "pytest11_plugin_distributions": [],
            "uv": None,
            "observable": False,
            "error": _safe_error(exc),
        }


def _validate_collected_nodes(
    nodes: Sequence[str], tracked_files: Iterable[str]
) -> tuple[list[str], str]:
    tracked = {path.replace("\\", "/") for path in tracked_files}
    sources = sorted({node.split("::", 1)[0] for node in nodes})
    if any(source not in tracked for source in sources):
        raise RuntimeError("collection_contains_untracked_source")
    return sources, _sha256_bytes(("\n".join(sources) + "\n").encode("utf-8"))


def _validate_timing_collection(nodes: Sequence[str], sources: Sequence[str]) -> str:
    node_ids_sha256 = _sha256_bytes(("\n".join(nodes) + "\n").encode("utf-8"))
    if (
        len(nodes) != TIMING_EXPECTED_NODE_COUNT
        or node_ids_sha256 != TIMING_EXPECTED_NODE_IDS_SHA256
        or list(sources) != sorted(TIMING_TEST_TARGETS)
    ):
        raise RuntimeError("timing_collection_contract_mismatch")
    return node_ids_sha256


def _collect(config: Configuration, identity: GitIdentity) -> dict[str, Any]:
    if config.probe:
        return {
            "command": ["python", "-c", "<fixed-probe-collection>"],
            "count": 1,
            "node_ids_sha256": _sha256_bytes(b"probe::test\n"),
            "source_files_count": 1,
            "source_files_sha256": _sha256_bytes(b"probe\n"),
            "error": None,
        }
    public_command = [
        "uv",
        "run",
        "--locked",
        "--exact",
        "--project",
        "bin",
        "pytest",
        *TIMING_TEST_TARGETS,
        "--collect-only",
        "-q",
        "-n",
        "0",
    ]
    require_clean_git(
        config.repo, identity, allowed_untracked=_owned_output(config)
    )
    completed = subprocess.run(
        public_command,
        cwd=config.repo,
        capture_output=True,
        text=True,
        timeout=config.timeout_seconds,
        env={**_controlled_environment(), "UV_CACHE_DIR": str(config.cache)},
    )
    require_clean_git(
        config.repo, identity, allowed_untracked=_owned_output(config)
    )
    nodes = [
        line.strip().replace("\\", "/")
        for line in completed.stdout.splitlines()
        if "::" in line and not line.startswith(("=", " "))
    ]
    tracked_output = subprocess.run(
        ("git", "ls-files", "-z", "--", *TIMING_TEST_TARGETS),
        cwd=config.repo,
        check=True,
        capture_output=True,
        env=_controlled_environment(),
    ).stdout
    tracked_files = [
        record.decode("utf-8", errors="surrogateescape").replace("\\", "/")
        for record in tracked_output.split(b"\0")
        if record
    ]
    error = None
    source_files: list[str] = []
    source_files_sha256: str | None = None
    node_ids_sha256 = _sha256_bytes(("\n".join(nodes) + "\n").encode("utf-8"))
    if completed.returncode != 0:
        error = "CollectionNonzero"
    elif not nodes:
        error = "CollectionEmpty"
    else:
        try:
            source_files, source_files_sha256 = _validate_collected_nodes(
                nodes, tracked_files
            )
            node_ids_sha256 = _validate_timing_collection(nodes, source_files)
        except Exception as exc:
            error = _safe_error(exc)
    return {
        "command": public_command,
        "count": len(nodes),
        "node_ids_sha256": node_ids_sha256,
        "expected_count": TIMING_EXPECTED_NODE_COUNT,
        "expected_node_ids_sha256": TIMING_EXPECTED_NODE_IDS_SHA256,
        "expected_source_files": list(TIMING_TEST_TARGETS),
        "source_files_count": len(source_files),
        "source_files_sha256": source_files_sha256,
        "error": error,
    }


def _host_metadata(label: str) -> dict[str, Any]:
    try:
        # Values are hardware/OS descriptors only; never persist machine name,
        # user name, or an absolute path.
        ps = r"""
$c=Get-CimInstance Win32_ComputerSystem
$p=Get-CimInstance Win32_Processor|Select-Object -First 1
$o=Get-CimInstance Win32_OperatingSystem
[ordered]@{ram_bytes=[int64]$c.TotalPhysicalMemory;cpu=[ordered]@{name=$p.Name.Trim();physical_cores=[int]$p.NumberOfCores;logical_processors=[int]$p.NumberOfLogicalProcessors};os=[ordered]@{caption=$o.Caption;version=$o.Version;build_number=$o.BuildNumber}}|ConvertTo-Json -Compress -Depth 4
"""
        details = json.loads(
            subprocess.run(
                ("powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps),
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
                env=_controlled_environment(),
            ).stdout
        )
    except Exception as exc:
        details = {"ram_bytes": None, "cpu": None, "os": None, "error": _safe_error(exc)}
    return {"label": label, "native_windows": os.name == "nt", **details}


def _artifact_base(config: Configuration, identity: GitIdentity) -> dict[str, Any]:
    return {
        "schema_version": 3,
        "generated_at_utc": _utc_now(),
        "commit": identity.commit,
        "tree": identity.tree,
        "index_tree": identity.index_tree,
        "host": _host_metadata(config.host_label),
        "protocol": {
            "mode": "parallel",
            "warmups": config.warmups,
            "measured_runs": config.samples,
            "timeout_seconds": config.timeout_seconds,
            "parallel_workers": config.workers,
            "workload": "git_fixture_contract",
            "test_targets": list(TIMING_TEST_TARGETS),
            "expected_node_count": TIMING_EXPECTED_NODE_COUNT,
            "expected_node_ids_sha256": TIMING_EXPECTED_NODE_IDS_SHA256,
            "uv_cache": ".anvil-build/windows-pytest-uv-cache",
            "controlled_environment_sha256": _sha256_json(_controlled_environment()),
            "pytest_addopts": "rejected_and_removed",
            "dependency_resolution": "uv_run_locked_exact",
            "pytest_plugin_metadata": (
                "locked_exact_distribution_names_and_versions_only"
            ),
            "control_observation": "comparable_snapshots_immediately_before_and_after_each_run",
            "defender_exclusions": "record_when_visible_otherwise_redacted_context_not_elevation_gate",
            "raw_logs": f"{config.log_relative_root}/",
            "process_containment": "windows_job_kill_on_close_gated_before_assignment_zero_active_verified",
            "probe": config.probe,
        },
        "versions": None,
        "controls": None,
        "collection": None,
        "runs": [],
        "result": {"status": "running"},
    }


def _finish_artifact(
    config: Configuration,
    artifact: dict[str, Any],
    output: ExclusiveOutput | None = None,
) -> int:
    runs = artifact["runs"]
    measured = [run for run in runs if run["phase"] == "measured"]
    warmups = [run for run in runs if run["phase"] == "warmup"]
    count_signatures = {
        tuple(sorted((run["junit_counts"] or {}).items()))
        for run in runs
        if run["junit_counts"] is not None
    }
    junit_counts_identical = bool(
        runs
        and all(run["junit_counts"] is not None for run in runs)
        and len(count_signatures) == 1
    )
    testcase_hashes = {
        run.get("junit_testcase_ids_sha256")
        for run in runs
        if run.get("junit_testcase_ids_sha256") is not None
    }
    junit_testcase_ids_identical = bool(
        runs
        and all(run.get("junit_testcase_ids_sha256") is not None for run in runs)
        and len(testcase_hashes) == 1
    )
    expected_count = artifact.get("collection", {}).get("count", 0)
    junit_counts_pass = bool(
        expected_count > 0
        and runs
        and all(
            _junit_counts_pass(run.get("junit_counts"), expected_count)
            for run in runs
        )
    )
    collection_valid = bool(
        artifact.get("collection")
        and artifact["collection"].get("error") is None
        and artifact["collection"].get("count", 0) > 0
        and artifact["collection"].get("node_ids_sha256")
        and artifact["collection"].get("source_files_sha256")
    )
    timing_valid = bool(
        len(measured) == config.samples
        and len(warmups) == config.warmups
        and all(run["timing_valid"] for run in measured)
        and all(run["timing_valid"] for run in warmups)
        and junit_counts_identical
        and junit_testcase_ids_identical
        and junit_counts_pass
        and collection_valid
        and artifact.get("repository_final_error") is None
    )
    comparison_qualified = bool(
        artifact["versions"]["observable"]
        and artifact["controls"]["comparison_ready"]
        and all(run["comparison_qualified"] for run in runs)
    )
    parallel = [
        float(run["elapsed_seconds"])
        for run in measured
        if run["timing_valid"]
    ]
    parallel_distribution = _distribution(parallel)
    measurement_valid = bool(timing_valid and comparison_qualified)
    affected_slice_median_budget_met = (
        float(parallel_distribution["median"]) <= 35.0
        if measurement_valid and parallel_distribution["median"] is not None
        else None
    )
    reasons: list[str] = []
    if not timing_valid:
        reasons.append("timing_protocol_incomplete_or_invalid")
    if not junit_counts_identical:
        reasons.append("junit_counts_differ_across_runs")
    if not junit_testcase_ids_identical:
        reasons.append("junit_testcase_ids_differ_across_runs")
    if not junit_counts_pass:
        reasons.append("junit_counts_do_not_show_all_tests_passed")
    if not collection_valid:
        reasons.append("collection_invalid")
    if artifact.get("repository_final_error") is not None:
        reasons.append("repository_integrity_failed_after_runs")
    if not comparison_qualified:
        reasons.append("controls_not_comparable")
    result_status = (
        "passed"
        if measurement_valid and affected_slice_median_budget_met
        else "regression"
        if measurement_valid
        else "insufficient"
    )
    artifact["result"] = {
        "status": result_status,
        "measurement_valid": measurement_valid,
        "comparison_qualified": comparison_qualified,
        "defender_exclusions_visibility": artifact["controls"].get(
            "defender_exclusions_visibility"
        ),
        "junit_counts_identical_across_runs": junit_counts_identical,
        "junit_testcase_ids_identical_across_runs": junit_testcase_ids_identical,
        "junit_counts_pass": junit_counts_pass,
        "collection_valid": collection_valid,
        "insufficient_reasons": reasons,
        "parallel_seconds": parallel_distribution,
        "affected_slice_median_budget_seconds": 35.0,
        "affected_slice_median_budget_met": affected_slice_median_budget_met,
        "all_scheduled_runs_recorded": len(runs) == config.warmups + config.samples,
    }
    artifact["generated_at_utc"] = _utc_now()
    if output is None:
        if config.output.exists():
            raise FileExistsError("output_reservation_violated")
        validate_no_reparse_components(config.output)
        _atomic_json(config.output, artifact)
    else:
        output.write_json(artifact)
    return result_exit_code(
        probe=config.probe,
        measurement_valid=measurement_valid,
        affected_slice_median_budget_met=affected_slice_median_budget_met,
    )


def _configuration(arguments: argparse.Namespace) -> Configuration:
    repo = Path(os.path.abspath(arguments.repo))
    if not arguments.probe and (arguments.warmups != 1 or arguments.samples != 3):
        raise ValueError("non_probe_protocol_is_fixed_at_one_warmup_and_three_runs")
    if not arguments.probe and arguments.workers != 16:
        raise ValueError("non_probe_workers_fixed_at_16")
    if not arguments.probe and arguments.timeout_seconds != 120:
        raise ValueError("non_probe_timeout_fixed_at_120_seconds")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", arguments.host_label):
        raise ValueError("invalid_host_label")
    if os.environ.get("PYTEST_ADDOPTS"):
        raise ValueError("pytest_addopts_must_be_unset")
    artifacts = validate_child_path(repo, repo / "artifacts")
    output_input = Path(arguments.output)
    output = output_input if output_input.is_absolute() else repo / output_input
    output = validate_child_path(repo, output, direct_parent=artifacts)
    if not re.fullmatch(r"[A-Za-z0-9._-]+\.json", output.name):
        raise ValueError("unsafe_output_filename")
    cache = validate_child_path(repo, repo / ".anvil-build" / "windows-pytest-uv-cache")
    run_id = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}-{os.getpid()}"
    log_root = validate_child_path(repo, artifacts / "windows-pytest-timing-logs" / run_id)
    return Configuration(
        repo=repo,
        output=output,
        artifacts=artifacts,
        cache=cache,
        log_root=log_root,
        log_relative_root=f"artifacts/windows-pytest-timing-logs/{run_id}",
        warmups=arguments.warmups,
        samples=arguments.samples,
        workers=arguments.workers,
        timeout_seconds=arguments.timeout_seconds,
        expected_commit=arguments.expected_commit,
        host_label=arguments.host_label,
        probe=arguments.probe,
    )


def _execute_protocol(
    config: Configuration,
    identity: GitIdentity,
    output: ExclusiveOutput,
) -> int:
    artifact = _artifact_base(config, identity)
    try:
        artifact["versions"] = _metadata_probe(config)
        artifact["controls"] = control_snapshot()
        try:
            artifact["collection"] = _collect(config, identity)
        except Exception as exc:
            artifact["collection"] = {
                "command": [
                    "uv",
                    "run",
                    "--locked",
                    "--exact",
                    "--project",
                    "bin",
                    "pytest",
                    *TIMING_TEST_TARGETS,
                    "--collect-only",
                    "-q",
                    "-n",
                    "0",
                ],
                "count": 0,
                "node_ids_sha256": None,
                "source_files_count": 0,
                "source_files_sha256": None,
                "error": _safe_error(exc),
            }
        _write_checkpoint(config, artifact)

        sequence = 0
        for _ in range(config.warmups):
            sequence += 1
            artifact["runs"].append(
                _run_one(
                    config,
                    identity,
                    artifact["controls"],
                    sequence=sequence,
                    phase="warmup",
                    sample=None,
                    collection=artifact["collection"],
                )
            )
            _write_checkpoint(config, artifact)
        for sample in range(1, config.samples + 1):
            sequence += 1
            artifact["runs"].append(
                _run_one(
                    config,
                    identity,
                    artifact["controls"],
                    sequence=sequence,
                    phase="measured",
                    sample=sample,
                    collection=artifact["collection"],
                )
            )
            _write_checkpoint(config, artifact)
        try:
            require_clean_git(
                config.repo, identity, allowed_untracked=_owned_output(config)
            )
            artifact["repository_final_error"] = None
        except Exception as exc:
            artifact["repository_final_error"] = _safe_error(exc)
        return _finish_artifact(config, artifact, output)
    except BaseException as exc:
        artifact["generated_at_utc"] = _utc_now()
        artifact["result"] = {
            "status": "failed",
            "error": _safe_error(exc),
            "all_scheduled_runs_recorded": False,
        }
        output.write_json(artifact)
        raise


def run(arguments: argparse.Namespace) -> int:
    if os.name != "nt":
        raise OSError("native_windows_required")
    config = _configuration(arguments)
    if config.output.exists():
        raise FileExistsError("output_must_be_absent")
    identity = require_clean_git(config.repo)
    if config.expected_commit and config.expected_commit.lower() != identity.commit.lower():
        raise RuntimeError("expected_commit_mismatch")
    output_relative = config.output.relative_to(config.repo).as_posix()
    tracked = subprocess.run(
        ("git", "ls-files", "--error-unmatch", "--", output_relative),
        cwd=config.repo,
        capture_output=True,
        env=_controlled_environment(),
    )
    if tracked.returncode == 0:
        raise RuntimeError("output_must_be_untracked")
    common_git_dir = Path(
        _run_text(
            ("git", "rev-parse", "--path-format=absolute", "--git-common-dir"), cwd=config.repo
        )
    ).resolve(strict=True)
    repository_key = _sha256_bytes(os.path.normcase(str(common_git_dir)).encode("utf-8"))
    output_key = _sha256_bytes(
        os.path.normcase(str(config.output.resolve(strict=False))).encode("utf-8")
    )
    with ExitStack() as locks:
        locks.enter_context(WindowsNamedMutex(f"Global\\AnvilPytestTiming-Repo-{repository_key}"))
        locks.enter_context(WindowsNamedMutex(f"Global\\AnvilPytestTiming-Output-{output_key}"))
        require_clean_git(config.repo, identity)
        if config.output.exists():
            raise FileExistsError("output_reservation_violated")
        for directory in (
            config.artifacts,
            config.cache.parent,
            config.cache,
            config.log_root.parent,
            config.log_root,
        ):
            validate_no_reparse_components(directory.parent)
            directory.mkdir(exist_ok=True)
            validate_no_reparse_components(directory)
        ignored_probe = f"{config.log_relative_root}/probe.log"
        ignored = subprocess.run(
            ("git", "check-ignore", "-q", "--", ignored_probe),
            cwd=config.repo,
            env=_controlled_environment(),
        )
        if ignored.returncode != 0:
            raise RuntimeError("raw_logs_must_be_git_ignored")
        require_clean_git(config.repo, identity)
        output = locks.enter_context(ExclusiveOutput(config.output))
        require_clean_git(
            config.repo, identity, allowed_untracked=_owned_output(config)
        )
        return _execute_protocol(config, identity, output)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--workers", type=int, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--output", default="artifacts/windows-pytest-timing.json")
    parser.add_argument("--expected-commit")
    parser.add_argument("--host-label", required=True)
    parser.add_argument("--probe", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(argv if argv is not None else sys.argv[1:])
    if arguments and arguments[0] == "_gate":
        return _gate_main(arguments[1:])
    try:
        return run(_parser().parse_args(arguments))
    except Exception as exc:
        print(f"timing harness preflight failed: {_safe_error(exc)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
