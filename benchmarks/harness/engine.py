"""Engine layer: own the real anvil binary and stand up a project from a TaskSpec list.

The benchmark drives the *actual* anvil CLI (the same console script a user
runs), not a reimplementation. This module locates/builds that binary once and renders
a PRD from an internal TaskSpec list, then runs the real setup pipeline
(init -> parse -> review -> approve -> plan -> score -> review tasks) so tasks land in
`ready` and are claimable.
"""
from __future__ import annotations

import contextlib
import ctypes
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import UTC
from functools import lru_cache
from pathlib import Path

_MAX_CAPTURE_BYTES = 1024 * 1024
# Once active work reaches its deadline, containment gets one shared best-effort
# allowance to stop the process tree, close capture streams, and join readers. OS
# process creation/termination calls are not themselves interruptible, so callers
# must treat this as a bounded cleanup target rather than an exact wall-clock cap.
PROCESS_CLEANUP_ALLOWANCE_SECONDS = 1.0

# Linux commands run below a tiny subreaper. Unlike a process group alone, the
# subreaper retains ownership of grandchildren that call setsid(2), kills them
# before reporting the command's exit, and handles the parent's cleanup signal.
# Other POSIX kernels fail closed below because this contract depends on prctl and
# /proc; silently falling back to killpg would let detached mutators escape.
_LINUX_SUBREAPER = r"""
import ctypes
import os
import signal
import subprocess
import sys
import time

libc = ctypes.CDLL(None, use_errno=True)
if libc.prctl(36, 1, 0, 0, 0) != 0:
    raise SystemExit(126)

stopping = False
def stop(_signum, _frame):
    global stopping
    stopping = True

signal.signal(signal.SIGTERM, stop)
child = subprocess.Popen(sys.argv[1:])
returncode = None
while returncode is None and not stopping:
    try:
        returncode = child.wait(timeout=0.02)
    except subprocess.TimeoutExpired:
        pass

def descendants():
    parents = {}
    for entry in os.scandir('/proc'):
        if not entry.name.isdigit():
            continue
        try:
            raw = open(entry.path + '/stat', encoding='utf-8').read()
            tail = raw[raw.rfind(')') + 2:].split()
            parents[int(entry.name)] = int(tail[1])
        except (OSError, ValueError, IndexError):
            pass
    owned = set()
    frontier = {os.getpid()}
    while frontier:
        found = {pid for pid, ppid in parents.items() if ppid in frontier}
        found -= owned
        if not found:
            break
        owned |= found
        frontier = found
    return owned

deadline = time.monotonic() + 0.75
while time.monotonic() < deadline:
    owned = descendants()
    if not owned:
        break
    for pid in owned:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    while True:
        try:
            waited, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            break
        if waited == 0:
            break
    time.sleep(0.01)

if descendants():
    raise SystemExit(126)
if stopping:
    raise SystemExit(143)
if returncode is None:
    returncode = child.wait()
raise SystemExit(returncode if returncode >= 0 else 128 - returncode)
"""


def _windows_kill_on_close_job(proc: subprocess.Popen) -> int | None:
    """Put a Windows subprocess tree in a kill-on-close Job Object."""
    if os.name != "nt":
        return None

    class _BasicLimit(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

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
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel32.SetInformationJobObject.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        return None
    info = _ExtendedLimit()
    info.BasicLimitInformation.LimitFlags = 0x00002000
    if not kernel32.SetInformationJobObject(
        handle, 9, ctypes.byref(info), ctypes.sizeof(info)
    ) or not kernel32.AssignProcessToJobObject(
        handle, ctypes.c_void_p(proc._handle)  # type: ignore[attr-defined]
    ):
        if not _windows_close_handle_bounded(
            int(handle), PROCESS_CLEANUP_ALLOWANCE_SECONDS
        ):
            raise OSError("failed to close unusable Job Object")
        return None
    return int(handle)


def _windows_close_handle(handle: int) -> None:
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        if not kernel32.CloseHandle(ctypes.c_void_p(handle)):
            raise OSError(ctypes.get_last_error(), "CloseHandle failed")


def _windows_close_handle_bounded(handle: int, timeout: float) -> bool:
    """Retry a failed CloseHandle inside a fixed wall-clock allowance."""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        try:
            _windows_close_handle(handle)
            return True
        except OSError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.01, remaining))


def _windows_resume_process(proc: subprocess.Popen) -> bool:
    """Resume a process created suspended after containment is established."""
    if os.name != "nt":
        return True
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    ntdll.NtResumeProcess.argtypes = [ctypes.c_void_p]
    ntdll.NtResumeProcess.restype = ctypes.c_long
    return ntdll.NtResumeProcess(
        ctypes.c_void_p(proc._handle)  # type: ignore[attr-defined]
    ) == 0


def _dispose_unstarted_process(proc: subprocess.Popen) -> None:
    """Kill a suspended child and close its inherited capture handles."""
    with contextlib.suppress(Exception):
        proc.kill()
    with contextlib.suppress(Exception):
        proc.wait(timeout=PROCESS_CLEANUP_ALLOWANCE_SECONDS)
    for stream in (getattr(proc, "stdout", None), getattr(proc, "stderr", None)):
        if stream is not None:
            with contextlib.suppress(Exception):
                stream.close()


def _dispose_spawned_process(
    proc: subprocess.Popen,
    job_handle: int | None,
) -> None:
    """Emergency cleanup usable before run_process has initialized any locks."""
    deadline = time.monotonic() + PROCESS_CLEANUP_ALLOWANCE_SECONDS
    job_closed = job_handle is None
    if job_handle is not None:
        job_closed = _windows_close_handle_bounded(
            job_handle, PROCESS_CLEANUP_ALLOWANCE_SECONDS / 2
        )
    if os.name == "nt":
        if not job_closed:
            with contextlib.suppress(Exception):
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=PROCESS_CLEANUP_ALLOWANCE_SECONDS / 2,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
    else:
        # The Linux subreaper handles SIGTERM by killing even setsid descendants.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(proc.pid, signal.SIGTERM)
        with contextlib.suppress(Exception):
            proc.wait(timeout=max(0.0, deadline - time.monotonic()))
    with contextlib.suppress(Exception):
        proc.kill()
    with contextlib.suppress(Exception):
        proc.wait(timeout=max(0.0, deadline - time.monotonic()))
    for stream in (getattr(proc, "stdout", None), getattr(proc, "stderr", None)):
        if stream is not None:
            with contextlib.suppress(Exception):
                stream.close()


@dataclass(frozen=True)
class TaskSpec:
    """One unit of work. We render these into a PRD the real parser consumes."""

    id: str                       # e.g. "T001"
    title: str
    files: tuple[str, ...]        # target files this task "writes" (the likely_files)
    priority: str = "medium"      # high | medium | low
    deps: tuple[str, ...] = ()    # task ids this depends on
    verification: tuple[str, ...] = ("git diff --check",)  # verification commands
    feature: str = "F001"


@dataclass
class RunResult:
    code: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.code == 0


@dataclass
class _ProcessOwnership:
    """Preallocated handoff used to close the first-line post-Popen gap."""

    proc: subprocess.Popen | None = None
    job_handle: int | None = None


class _OwnedPopen(subprocess.Popen):
    """Publish the Popen object before its constructor can spawn and escape."""

    def __init__(self, ownership: _ProcessOwnership, *args, **kwargs) -> None:
        # ``self`` exists before subprocess initialization. Publishing it here lets
        # the outer ownership guard clean a live, partially initialized Popen if
        # its constructor raises after CreateProcess/fork succeeds.
        ownership.proc = self
        super().__init__(*args, **kwargs)
        if os.name == "nt":
            # The process was created suspended. Attach it to kill-on-close
            # containment before any post-construction hook can resume or fail.
            ownership.job_handle = _windows_kill_on_close_job(self)
        self._after_spawn()

    def _after_spawn(self) -> None:
        """Test seam for a constructor failure after a real child was created."""


def _popen_owned(
    ownership: _ProcessOwnership,
    *args,
    **kwargs,
) -> subprocess.Popen:
    return _OwnedPopen(ownership, *args, **kwargs)


# --- binary management ------------------------------------------------------

def _plugin_bin_dir() -> Path:
    # benchmarks/harness/engine.py -> plugins/anvil/bin
    return Path(__file__).resolve().parents[2] / "bin"


def _venv_anvil_candidate(bin_dir: Path, *, os_name: str | None = None) -> Path:
    """Return the native console-script path created by ``uv sync``."""
    platform_name = os.name if os_name is None else os_name
    if platform_name == "nt":
        return bin_dir / ".venv" / "Scripts" / "anvil.exe"
    return bin_dir / ".venv" / "bin" / "anvil"


@lru_cache(maxsize=1)
def anvil_binary(sync_timeout: float | None = None) -> str:
    """Return an absolute path to a runnable `anvil` console script.

    Prefers an already-synced venv; otherwise runs `uv sync` once. The resulting
    binary is cwd-independent, so each actor subprocess can run it with its own
    working directory (the project under test).
    """
    bin_dir = _plugin_bin_dir()
    candidate = _venv_anvil_candidate(bin_dir)
    if candidate.exists():
        return str(candidate)
    if shutil.which("uv") is None:
        raise RuntimeError(
            "uv not found and no synced venv at "
            f"{candidate}. Install uv or pre-sync the bin project."
        )
    sync_args = ["uv", "sync", "--quiet"]
    sync_budget = 60.0 if sync_timeout is None else sync_timeout
    sync_result = run_process(
        sync_args,
        bin_dir,
        timeout=sync_budget,
    )
    if sync_result.code == 124:
        raise subprocess.TimeoutExpired(sync_args, sync_budget)
    if not sync_result.ok:
        raise subprocess.CalledProcessError(sync_result.code, sync_args)
    if not candidate.exists():
        raise RuntimeError(f"uv sync did not produce {candidate}")
    return str(candidate)


def run(
    args: list[str],
    cwd: Path,
    actor: str | None = None,
    timeout: float = 60.0,
    output_limit_bytes: int = _MAX_CAPTURE_BYTES,
) -> RunResult:
    """Invoke the real CLI with bounded stdout/stderr capture.

    ``actor`` is threaded through as ``--actor`` where relevant. A child that
    exceeds either output bound is killed and reported as an infrastructure
    failure without retaining or returning its raw output.
    """
    started_at = time.monotonic()
    binary = anvil_binary(timeout)
    remaining = timeout - (time.monotonic() - started_at)
    if remaining <= 0:
        return RunResult(code=124, out="", err="timeout")
    cmd = [binary, *args]
    if actor is not None and "--actor" not in args:
        cmd += ["--actor", actor]
    # Each scenario is an isolated git repo with its own in-repo .anvil/; force the
    # legacy local layout so state stays in `cwd/.anvil` rather than resolving to the
    # shared ~/.anvil/workspaces/<repo>/ home workspace (the production default).
    env = {**os.environ, "NO_COLOR": "1", "ANVIL_STATE_LAYOUT": "local"}
    return run_process(
        cmd,
        cwd,
        timeout=remaining,
        output_limit_bytes=output_limit_bytes,
        env=env,
    )


def run_process(
    cmd: list[str],
    cwd: Path,
    *,
    timeout: float = 60.0,
    output_limit_bytes: int = _MAX_CAPTURE_BYTES,
    env: dict[str, str] | None = None,
) -> RunResult:
    """Run a process while owning it atomically from the Popen return onward."""
    ownership = _ProcessOwnership()
    try:
        return _run_process_owned(
            cmd,
            cwd,
            timeout=timeout,
            output_limit_bytes=output_limit_bytes,
            env=env,
            ownership=ownership,
        )
    except BaseException:
        # This outer guard is active while Popen executes. The implementation stores
        # the returned process into ``ownership`` in that same statement, so a trace
        # hook or asynchronous BaseException on the very next Python line cannot
        # precede cleanup ownership.
        if ownership.proc is not None:
            _dispose_spawned_process(ownership.proc, ownership.job_handle)
            ownership.proc = None
            ownership.job_handle = None
        raise


def _run_process_owned(
    cmd: list[str],
    cwd: Path,
    *,
    timeout: float = 60.0,
    output_limit_bytes: int = _MAX_CAPTURE_BYTES,
    env: dict[str, str] | None = None,
    ownership: _ProcessOwnership,
) -> RunResult:
    """Run one argv vector with bounded active work and process containment.

    ``timeout`` is charged from immediately before process creation through normal
    capture completion. A timed-out or partially-started process then receives one
    shared ``PROCESS_CLEANUP_ALLOWANCE_SECONDS`` best-effort cleanup window. Native
    process creation and termination calls cannot be interrupted on every platform,
    so they may overrun either target before Python regains control.
    """
    active_deadline = time.monotonic() + timeout
    if env is None:
        env = dict(os.environ)
    process_group: dict[str, object]
    if os.name == "nt":
        process_group = {
            "creationflags": (
                subprocess.CREATE_NEW_PROCESS_GROUP
                | getattr(subprocess, "CREATE_SUSPENDED", 0x00000004)
            )
        }
    else:
        if not sys.platform.startswith("linux"):
            return RunResult(
                code=126,
                out="",
                err="process containment unavailable",
            )
        process_group = {"start_new_session": True}
        cmd = [sys.executable, "-c", _LINUX_SUBREAPER, *cmd]
    ownership.proc = _popen_owned(
        ownership,
        cmd,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        **process_group,
    )
    proc = ownership.proc
    assert proc is not None
    # Ownership starts at the Popen return, before any lock/Event/allocation that
    # tests or resource exhaustion can make fail with a BaseException.
    job_handle: int | None = None
    try:
        if ownership.job_handle is None:
            ownership.job_handle = _windows_kill_on_close_job(proc)
        job_handle = ownership.job_handle
        if os.name == "nt" and job_handle is None:
            _dispose_unstarted_process(proc)
            return RunResult(code=126, out="", err="process containment unavailable")
        if not _windows_resume_process(proc):
            if job_handle is not None:
                if _windows_close_handle_bounded(
                    job_handle, PROCESS_CLEANUP_ALLOWANCE_SECONDS / 2
                ):
                    job_handle = None
                    ownership.job_handle = None
            _dispose_spawned_process(proc, job_handle)
            return RunResult(code=126, out="", err="process containment unavailable")
    except Exception:
        _dispose_spawned_process(proc, job_handle)
        return RunResult(code=126, out="", err="process containment unavailable")
    except BaseException:
        raise
    try:
        job_handle_lock = threading.Lock()
        cleanup_deadline_lock = threading.Lock()
        cleanup_deadline: float | None = None
        captured = {"out": bytearray(), "err": bytearray()}
        reader_finished_at: list[float] = []
        reader_finished_lock = threading.Lock()
        overflowed = threading.Event()
        capture_failed = threading.Event()
        reader_errors: dict[str, BaseException] = {}
        reader_errors_lock = threading.Lock()
    except BaseException:
        raise

    def _shared_cleanup_deadline() -> float:
        nonlocal cleanup_deadline
        with cleanup_deadline_lock:
            if cleanup_deadline is None:
                cleanup_deadline = (
                    time.monotonic() + PROCESS_CLEANUP_ALLOWANCE_SECONDS
                )
            return cleanup_deadline

    def _cleanup_remaining() -> float:
        return max(0.0, _shared_cleanup_deadline() - time.monotonic())

    def _close_job_handle() -> None:
        nonlocal job_handle
        with job_handle_lock:
            if job_handle is not None:
                _windows_close_handle(job_handle)
                job_handle = None
                ownership.job_handle = None

    def _retry_close_job_handle() -> bool:
        nonlocal job_handle
        with job_handle_lock:
            if job_handle is None:
                return True
            if not _windows_close_handle_bounded(job_handle, _cleanup_remaining()):
                return False
            job_handle = None
            ownership.job_handle = None
            return True

    def _terminate_tree() -> None:
        """Terminate the isolated process group without waiting indefinitely."""
        # Losing the Job Object close must not prevent the direct-child fallback
        # from running. A later cleanup pass retries the handle close if it failed.
        with contextlib.suppress(Exception):
            _close_job_handle()
        if os.name == "nt":
            # A successful kill-on-close already terminated the entire job. Invoke
            # taskkill only as the bounded fallback when CloseHandle failed.
            if job_handle is not None:
                remaining = _cleanup_remaining()
                if remaining > 0:
                    with contextlib.suppress(OSError, subprocess.SubprocessError):
                        subprocess.run(
                            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=remaining,
                            check=False,
                            creationflags=getattr(
                                subprocess, "CREATE_NO_WINDOW", 0
                            ),
                        )
        else:
            # Ask the subreaper to kill and reap its complete descendant tree,
            # including grandchildren that established a new session. Only fall
            # back to the launch session after its bounded grace period expires.
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(proc.pid, signal.SIGTERM)
            remaining = _cleanup_remaining()
            supervisor_stopped = False
            if remaining > 0:
                try:
                    proc.wait(timeout=remaining)
                    supervisor_stopped = True
                except (subprocess.TimeoutExpired, OSError):
                    pass
            if not supervisor_stopped:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()

    def _bounded_cleanup(readers_to_join: list[threading.Thread]) -> bool:
        """Best-effort containment for every failure after process creation."""
        with contextlib.suppress(Exception):
            _terminate_tree()
        # Even if process-group containment itself failed, make a direct-child
        # termination attempt before waiting or touching the capture streams.
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()
        with contextlib.suppress(Exception):
            remaining = _cleanup_remaining()
            if remaining > 0:
                proc.wait(timeout=remaining)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                with contextlib.suppress(Exception):
                    stream.close()
        for reader in readers_to_join:
            with contextlib.suppress(Exception):
                reader.join(timeout=_cleanup_remaining())
        # Retry a failed Job Object close after the process and readers have been
        # contained. Keeping this last makes a supervision exception fail closed.
        try:
            return _retry_close_job_handle()
        except Exception:
            return False

    def _drain(stream, key: str) -> None:
        try:
            # BufferedReader.read(size) may wait for all ``size`` bytes or EOF.
            # read1 performs one raw pipe read, so a flushed limit+1 payload is
            # classified immediately even if the child remains alive afterward.
            read_chunk = getattr(stream, "read1", stream.read)
            while chunk := read_chunk(8192):
                remaining = output_limit_bytes - len(captured[key])
                if remaining > 0:
                    captured[key].extend(chunk[:remaining])
                if len(chunk) > remaining:
                    overflowed.set()
                    _terminate_tree()
                    return
        except BaseException as exc:
            # A broken capture pipe is an infrastructure failure even when the
            # direct child reports success: accepting the bytes collected so far
            # would make empty or truncated evidence look authoritative. This is
            # deliberately BaseException: MemoryError/SystemExit in a daemon reader
            # must be reported to its supervising thread, not disappear silently.
            try:
                with reader_errors_lock:
                    reader_errors[key] = exc
            finally:
                capture_failed.set()
                _terminate_tree()
        finally:
            # Take the completion sample after acquiring the final bookkeeping
            # lock so a reader that crosses the active deadline waiting here is
            # still classified as timed out when the parent resumes late.
            with reader_finished_lock:
                reader_finished_at.append(time.monotonic())

    # Both streams must be drained concurrently or a verbose child can deadlock
    # while the parent waits on the other pipe.
    readers: list[threading.Thread] = []
    started_readers: list[threading.Thread] = []
    try:
        readers = [
            threading.Thread(target=_drain, args=(proc.stdout, "out"), daemon=True),
            threading.Thread(target=_drain, args=(proc.stderr, "err"), daemon=True),
        ]
        for reader in readers:
            reader.start()
            started_readers.append(reader)
    except BaseException:
        _bounded_cleanup(started_readers)
        raise

    try:
        remaining = active_deadline - time.monotonic()
        timed_out = remaining <= 0
        if not timed_out:
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                timed_out = True
        if not timed_out:
            for reader in readers:
                reader.join(timeout=max(0.0, active_deadline - time.monotonic()))
            timed_out = (
                any(reader.is_alive() for reader in readers)
                or len(reader_finished_at) != len(readers)
                or any(
                    finished_at >= active_deadline
                    for finished_at in reader_finished_at
                )
            )
        if timed_out or overflowed.is_set() or capture_failed.is_set():
            if overflowed.is_set():
                failure = RunResult(code=125, out="", err="output limit exceeded")
            elif capture_failed.is_set():
                failure = RunResult(code=126, out="", err="output capture failed")
            else:
                failure = RunResult(code=124, out="", err="timeout")
            if not _bounded_cleanup(readers):
                return RunResult(code=126, out="", err="process cleanup failed")
            return failure
        # On Linux, the supervisor does not exit until it has killed and reaped all
        # remaining descendants. Thus a successful wait above is the containment
        # proof; there is no live process group left to clean up here.
        _close_job_handle()
        return RunResult(
            code=proc.returncode,
            out=captured["out"].decode("utf-8", errors="replace"),
            err=captured["err"].decode("utf-8", errors="replace"),
        )
    except Exception:
        _bounded_cleanup(readers)
        return RunResult(code=126, out="", err="process supervision failed")
    except BaseException:
        _bounded_cleanup(readers)
        raise


# --- PRD rendering ----------------------------------------------------------

def render_prd(name: str, tasks: list[TaskSpec]) -> str:
    lines = [
        f"# Project: {name}",
        "",
        "## Summary",
        "Benchmark fixture project. Tasks are synthetic units of coordination work.",
        "",
        "## Goals",
        "- Exercise multi-actor task coordination.",
        "",
        "## Requirements",
        "- R001: Actors complete every task exactly once.",
        "- R002: No two actors mutate the same file concurrently.",
        "",
        "## Features",
        "",
        "### F001: Core",
        "**Requirements:** R001, R002",
        "",
        "## Tasks",
        "",
    ]
    for t in tasks:
        lines.append(f"### {t.id}: {t.title}")
        lines.append(f"**Feature:** {t.feature}")
        lines.append(f"**Priority:** {t.priority}")
        if t.files:
            lines.append(f"**Likely files:** {', '.join(t.files)}")
        if t.deps:
            lines.append(f"**Dependencies:** {', '.join(t.deps)}")
        lines.append("")
        lines.append("**Acceptance criteria:**")
        lines.append(f"- {t.title} completes and writes its target files.")
        lines.append("")
        lines.append("**Verification:**")
        for cmd in t.verification:
            lines.append(f"- `{cmd}`")
        lines.append("")
    return "\n".join(lines)


# --- project setup ----------------------------------------------------------

@dataclass
class Project:
    root: Path
    tasks: list[TaskSpec]
    lease_minutes: float = 60.0

    @property
    def workspace(self) -> Path:
        return self.root / "workspace"


def _setup_timeout(deadline: float | None) -> float:
    """Return a bounded timeout for the next setup subprocess."""
    if deadline is None:
        return 60.0
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("benchmark setup deadline exceeded")
    return remaining


def setup_project(
    root: Path,
    name: str,
    tasks: list[TaskSpec],
    lease_minutes: float = 60.0,
    *,
    deadline: float | None = None,
) -> Project:
    """Stand up a ready-to-claim anvil project via the real pipeline.

    Raises a bounded RuntimeError if any setup step fails, so a broken fixture is
    loud without copying potentially sensitive CLI output into diagnostics.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "workspace").mkdir(exist_ok=True)
    git_init = run_process(
        ["git", "init", "-q"],
        root,
        timeout=_setup_timeout(deadline),
        output_limit_bytes=64 * 1024,
    )
    if git_init.code == 124:
        raise TimeoutError("benchmark setup deadline exceeded")
    if git_init.code != 0:
        raise RuntimeError(
            "benchmark setup infrastructure failure: "
            f"phase=git-init exit_code={git_init.code}"
        )

    steps = [
        (["init", "--name", name], None),
    ]
    for args, actor in steps:
        r = run(
            args,
            cwd=root,
            actor=actor,
            timeout=_setup_timeout(deadline),
        )
        if r.code == 124:
            raise TimeoutError("benchmark setup deadline exceeded")
        if not r.ok:
            raise RuntimeError(
                f"benchmark setup infrastructure failure: "
                f"phase=init exit_code={r.code}"
            )

    # Configure a short lease for crash-recovery scenarios.
    _set_lease_minutes(root, lease_minutes)

    (root / ".anvil" / "prd.md").write_text(
        render_prd(name, tasks), encoding="utf-8"
    )

    pipeline = [
        ["prd", "parse"],
        ["prd", "review"],
        ["prd", "review", "--approve"],
        ["plan", "--no-llm"],
        ["score"],
        ["review", "tasks"],
    ]
    for args in pipeline:
        r = run(args, cwd=root, timeout=_setup_timeout(deadline))
        if r.code == 124:
            raise TimeoutError("benchmark setup deadline exceeded")
        if not r.ok:
            raise RuntimeError(
                f"benchmark setup infrastructure failure: "
                f"phase={args[0]} exit_code={r.code}"
            )

    proj = Project(root=root, tasks=tasks, lease_minutes=lease_minutes)
    ready = ready_task_ids(proj, timeout=_setup_timeout(deadline))
    _setup_timeout(deadline)
    if not ready:
        raise RuntimeError(
            "setup produced no `ready` tasks; check PRD rendering / review gates"
        )
    return proj


def _set_lease_minutes(root: Path, minutes: float) -> None:
    """Patch default_lease_minutes in config.yaml (line-level, no yaml dep)."""
    cfg = root / ".anvil" / "config.yaml"
    if not cfg.exists():
        return
    # The engine coerces this via int(str(value)); a fractional string raises and
    # silently falls back to 60. So emit an integer (floor 1 minute = the real lease
    # granularity everywhere in anvil).
    value = max(1, int(round(minutes)))
    out = []
    seen = False
    for line in cfg.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("default_lease_minutes:"):
            out.append(f"default_lease_minutes: {value}")
            seen = True
        else:
            out.append(line)
    if not seen:
        out.append(f"default_lease_minutes: {value}")
    cfg.write_text("\n".join(out) + "\n", encoding="utf-8")


def ready_task_ids(proj: Project, *, timeout: float = 5.0) -> list[str]:
    """Authoritative ready set, read straight from the canonical SQLite state."""
    return _task_ids_with_status(proj, "ready", timeout=timeout)


def _task_ids_with_status(
    proj: Project,
    status: str,
    *,
    timeout: float = 5.0,
) -> list[str]:
    import sqlite3
    db = proj.root / ".anvil" / "state.db"
    if not db.exists():
        return []
    con = sqlite3.connect(str(db), timeout=max(0.0, timeout))
    try:
        rows = con.execute(
            "SELECT id FROM tasks WHERE status = ? ORDER BY id", (status,)
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()
    return [r[0] for r in rows]


def _sqlite_timeout(deadline: float | None) -> float:
    """Return a positive SQLite busy timeout bounded by an absolute deadline."""
    if deadline is None:
        return 5.0
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("benchmark SQLite deadline exceeded")
    return remaining


def _set_sqlite_busy_deadline(con, deadline: float | None) -> None:
    """Keep each SQLite lock wait inside the operation's remaining budget."""
    remaining = _sqlite_timeout(deadline)
    if deadline is not None:
        con.execute(f"PRAGMA busy_timeout = {max(1, int(remaining * 1000))}")


def expire_claims_for(
    proj: Project,
    task_id: str,
    *,
    deadline: float | None = None,
) -> int:
    """Fast-forward: backdate the active lease on `task_id` so the engine's stale-claim
    reaper (which runs before every command) treats it as expired.

    This simulates elapsed time rather than waiting the real lease out. It is needed
    because (engine finding) the CLI `claim` path does not wire `default_lease_minutes`
    from config, so the lease is always the 60-minute default — too long to wait on.
    The recovery itself (reap -> task back to ready -> reclaim -> complete) still runs
    through the real engine.
    """
    import sqlite3
    from datetime import datetime, timedelta
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    db = proj.root / ".anvil" / "state.db"
    con = sqlite3.connect(str(db), timeout=_sqlite_timeout(deadline))
    try:
        _set_sqlite_busy_deadline(con, deadline)
        cur = con.execute(
            "UPDATE claims SET lease_expires_at = ? "
            "WHERE task_id = ? AND released_at IS NULL",
            (past, task_id),
        )
        _set_sqlite_busy_deadline(con, deadline)
        con.commit()
        return cur.rowcount
    except sqlite3.OperationalError as exc:
        if deadline is not None and (
            time.monotonic() >= deadline
            or getattr(exc, "sqlite_errorcode", None)
            in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
        ):
            raise TimeoutError("benchmark SQLite deadline exceeded") from exc
        raise
    finally:
        con.close()


def claim_is_expired(
    proj: Project,
    claim_id: str,
    task_id: str,
    actor: str,
    *,
    deadline: float | None = None,
) -> bool:
    """Verify the exact seeded claim remains active but is now past its lease."""
    import sqlite3
    from datetime import datetime

    db = proj.root / ".anvil" / "state.db"
    con = sqlite3.connect(str(db), timeout=_sqlite_timeout(deadline))
    try:
        _set_sqlite_busy_deadline(con, deadline)
        row = con.execute(
            "SELECT task_id, claimed_by, status, lease_expires_at, released_at "
            "FROM claims WHERE id = ?",
            (claim_id,),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if deadline is not None and (
            time.monotonic() >= deadline
            or getattr(exc, "sqlite_errorcode", None)
            in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
        ):
            raise TimeoutError("benchmark SQLite deadline exceeded") from exc
        raise
    finally:
        con.close()
    _sqlite_timeout(deadline)
    if row is None:
        return False
    claimed_task, claimed_by, status, expires_at, released_at = row
    try:
        expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except ValueError:
        return False
    return (
        claimed_task == task_id
        and claimed_by == actor
        and status == "active"
        and released_at is None
        and expiry.tzinfo is not None
        and expiry < datetime.now(UTC)
    )


def task_status(proj: Project, *, timeout: float = 5.0) -> dict[str, str]:
    """Map every task id -> current status, from canonical state."""
    import sqlite3
    db = proj.root / ".anvil" / "state.db"
    con = sqlite3.connect(str(db), timeout=max(0.0, timeout))
    try:
        rows = con.execute("SELECT id, status FROM tasks").fetchall()
    finally:
        con.close()
    return {r[0]: r[1] for r in rows}
