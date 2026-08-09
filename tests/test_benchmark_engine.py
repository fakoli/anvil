import io
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from benchmarks.harness import engine, runner
from benchmarks.harness.engine import RunResult, _venv_anvil_candidate
from benchmarks.harness.scenarios import Scenario


class _FakeProcess:
    def __init__(self) -> None:
        self.pid = 987654
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO()
        self.returncode = 0
        self.killed = False
        self.wait_saw_killed: list[bool] = []

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.wait_saw_killed.append(self.killed)
        return self.returncode

    def kill(self) -> None:
        self.killed = True


class _MidReadFailure(io.BytesIO):
    def __init__(self, exc_type: type[BaseException]) -> None:
        super().__init__(b"partial output")
        self._exc_type = exc_type
        self._reads = 0

    def read(self, size: int = -1) -> bytes:
        self._reads += 1
        if self._reads == 2:
            raise self._exc_type("capture pipe failed")
        return super().read(size)

    def read1(self, size: int = -1) -> bytes:
        return self.read(size)


class _WaitFailureProcess(_FakeProcess):
    def __init__(self) -> None:
        super().__init__()
        self.wait_calls = 0

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.wait_calls += 1
        self.wait_saw_killed.append(self.killed)
        if self.wait_calls == 1:
            raise OSError("process wait failed")
        return self.returncode


def _stub_process_containment(
    monkeypatch: pytest.MonkeyPatch,
    process: _FakeProcess,
) -> list[int]:
    closed_jobs: list[int] = []
    monkeypatch.setattr(engine, "_windows_resume_process", lambda _proc: True)
    monkeypatch.setattr(
        engine,
        "_windows_kill_on_close_job",
        lambda _proc: 77 if engine.os.name == "nt" else None,
    )
    monkeypatch.setattr(engine, "_windows_close_handle", closed_jobs.append)
    if engine.os.name == "nt":
        monkeypatch.setattr(engine.subprocess, "run", lambda *_args, **_kwargs: None)
    else:
        monkeypatch.setattr(engine.os, "killpg", lambda *_args, **_kwargs: None)
    def fake_owned_popen(ownership, *_args, **_kwargs):
        ownership.proc = process
        return process

    monkeypatch.setattr(engine, "_popen_owned", fake_owned_popen)
    return closed_jobs


def test_venv_anvil_candidate_uses_windows_console_script() -> None:
    assert _venv_anvil_candidate(Path("project"), os_name="nt") == (
        Path("project") / ".venv" / "Scripts" / "anvil.exe"
    )


def test_venv_anvil_candidate_uses_posix_console_script() -> None:
    assert _venv_anvil_candidate(Path("project"), os_name="posix") == (
        Path("project") / ".venv" / "bin" / "anvil"
    )


def test_anvil_binary_applies_timeout_to_first_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine.anvil_binary.cache_clear()
    monkeypatch.setattr(engine, "_plugin_bin_dir", lambda: tmp_path)
    monkeypatch.setattr(engine.shutil, "which", lambda _name: "uv")

    observed: dict[str, object] = {}

    def timed_out(
        args: list[str],
        cwd: Path,
        *,
        timeout: float,
    ) -> RunResult:
        observed.update(args=args, cwd=cwd, timeout=timeout)
        return RunResult(124, "", "timeout")

    monkeypatch.setattr(engine, "run_process", timed_out)
    with pytest.raises(subprocess.TimeoutExpired):
        engine.anvil_binary(0.25)
    assert observed == {
        "args": ["uv", "sync", "--quiet"],
        "cwd": tmp_path,
        "timeout": 0.25,
    }
    engine.anvil_binary.cache_clear()


def test_crash_claim_sqlite_operations_obey_absolute_deadline(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / ".anvil"
    state_dir.mkdir()
    db = state_dir / "state.db"
    original_expiry = "2099-01-01T00:00:00+00:00"
    owner = sqlite3.connect(db)
    owner.execute(
        "CREATE TABLE claims ("
        "id TEXT, task_id TEXT, claimed_by TEXT, status TEXT, "
        "lease_expires_at TEXT, released_at TEXT)"
    )
    owner.execute(
        "INSERT INTO claims VALUES ('C001', 'T001', 'dead', 'active', ?, NULL)",
        (original_expiry,),
    )
    owner.commit()
    owner.execute("BEGIN EXCLUSIVE")
    owner.execute("UPDATE claims SET status = status WHERE id = 'C001'")
    proj = engine.Project(root=tmp_path, tasks=[])

    started = time.monotonic()
    with pytest.raises(TimeoutError, match="SQLite deadline exceeded"):
        engine.expire_claims_for(
            proj,
            "T001",
            deadline=time.monotonic() + 0.05,
        )
    with pytest.raises(TimeoutError, match="SQLite deadline exceeded"):
        engine.claim_is_expired(
            proj,
            "C001",
            "T001",
            "dead",
            deadline=time.monotonic() + 0.05,
        )
    elapsed = time.monotonic() - started
    owner.rollback()
    owner.close()

    check = sqlite3.connect(db)
    expiry = check.execute(
        "SELECT lease_expires_at FROM claims WHERE id = 'C001'"
    ).fetchone()[0]
    check.close()
    assert elapsed < 0.5
    assert expiry == original_expiry


def test_run_charges_binary_resolution_to_command_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, float] = {}

    def delayed_binary(_timeout: float | None = None) -> str:
        time.sleep(0.03)
        return "anvil"

    def fake_run_process(
        _cmd: list[str],
        _cwd: Path,
        *,
        timeout: float,
        output_limit_bytes: int,
        env: dict[str, str] | None,
    ) -> RunResult:
        del output_limit_bytes, env
        seen["timeout"] = timeout
        return RunResult(0, "", "")

    monkeypatch.setattr(engine, "anvil_binary", delayed_binary)
    monkeypatch.setattr(engine, "run_process", fake_run_process)

    assert engine.run(["status"], tmp_path, timeout=0.1).ok
    assert 0 < seen["timeout"] < 0.09


def test_run_process_charges_process_setup_to_active_work_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _FakeProcess()
    closed_jobs = _stub_process_containment(monkeypatch, process)

    def delayed_popen(ownership, *_args: object, **_kwargs: object) -> _FakeProcess:
        ownership.proc = process
        time.sleep(0.02)
        return process

    monkeypatch.setattr(engine, "_popen_owned", delayed_popen)

    result = engine.run_process(["anvil", "status"], tmp_path, timeout=0.005)

    assert result == RunResult(124, "", "timeout")
    assert process.killed is True
    assert process.wait_saw_killed and process.wait_saw_killed[-1] is True
    if engine.os.name == "nt":
        assert all(process.wait_saw_killed)
    if engine.os.name == "nt":
        assert closed_jobs == [77]


def test_reader_finishing_after_deadline_cannot_return_success_when_parent_is_late(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess()
    _stub_process_containment(monkeypatch, process)
    main_thread = engine.threading.get_ident()
    parent_resumed_late = [False]
    original_join = engine.threading.Thread.join

    def monotonic() -> float:
        if engine.threading.get_ident() != main_thread:
            return 101.001
        return 101.001 if parent_resumed_late[0] else 100.0

    def resume_parent_after_reader_exit(
        reader: engine.threading.Thread,
        timeout: float | None = None,
    ) -> None:
        del timeout
        original_join(reader)
        parent_resumed_late[0] = True

    monkeypatch.setattr(engine.time, "monotonic", monotonic)
    monkeypatch.setattr(engine.threading.Thread, "join", resume_parent_after_reader_exit)

    result = engine.run_process(["anvil", "status"], tmp_path, timeout=1.0)

    assert result == RunResult(124, "", "timeout")
    assert process.killed is True


def test_readers_finishing_on_time_survive_late_parent_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess()
    _stub_process_containment(monkeypatch, process)
    main_thread = engine.threading.get_ident()
    parent_resumed_late = [False]
    original_join = engine.threading.Thread.join

    def monotonic() -> float:
        if engine.threading.get_ident() != main_thread:
            return 100.5
        return 101.001 if parent_resumed_late[0] else 100.0

    def resume_parent_after_reader_exit(
        reader: engine.threading.Thread,
        timeout: float | None = None,
    ) -> None:
        del timeout
        original_join(reader)
        parent_resumed_late[0] = True

    monkeypatch.setattr(engine.time, "monotonic", monotonic)
    monkeypatch.setattr(engine.threading.Thread, "join", resume_parent_after_reader_exit)

    result = engine.run_process(["anvil", "status"], tmp_path, timeout=1.0)

    assert result == RunResult(0, "", "")
    assert process.killed is False


def test_reader_start_failure_terminates_process_and_closes_streams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _FakeProcess()
    closed_jobs = _stub_process_containment(monkeypatch, process)
    monkeypatch.setattr(
        engine.threading.Thread,
        "start",
        lambda _self: (_ for _ in ()).throw(RuntimeError("thread unavailable")),
    )

    with pytest.raises(RuntimeError, match="thread unavailable"):
        engine.run_process(["anvil", "status"], tmp_path, timeout=1)

    assert process.killed is True
    assert process.stdout.closed is True
    assert process.stderr.closed is True
    assert process.wait_saw_killed and process.wait_saw_killed[-1] is True
    if engine.os.name == "nt":
        assert all(process.wait_saw_killed)
    if engine.os.name == "nt":
        assert closed_jobs == [77]


@pytest.mark.parametrize(
    ("stream_name", "exc_type"),
    [
        ("stdout", OSError),
        ("stderr", ValueError),
        ("stdout", MemoryError),
        ("stderr", SystemExit),
    ],
)
def test_mid_read_capture_failure_is_typed_infrastructure_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stream_name: str,
    exc_type: type[BaseException],
) -> None:
    process = _FakeProcess()
    setattr(process, stream_name, _MidReadFailure(exc_type))
    closed_jobs = _stub_process_containment(monkeypatch, process)

    result = engine.run_process(["anvil", "status"], tmp_path, timeout=1)

    assert result == RunResult(126, "", "output capture failed")
    assert process.killed is True
    assert process.stdout.closed is True
    assert process.stderr.closed is True
    if engine.os.name == "nt":
        assert closed_jobs == [77]


def test_first_line_after_popen_baseexception_is_already_owned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _FakeProcess()
    _stub_process_containment(monkeypatch, process)
    popen_returned = False

    def fake_popen(ownership, *_args: object, **_kwargs: object) -> _FakeProcess:
        nonlocal popen_returned
        ownership.proc = process
        popen_returned = True
        return process

    def interrupt_first_owned_line(frame, event: str, _arg):
        if (
            event == "line"
            and popen_returned
            and frame.f_code is engine._run_process_owned.__code__
        ):
            raise KeyboardInterrupt("first post-Popen line")
        return interrupt_first_owned_line

    monkeypatch.setattr(engine, "_popen_owned", fake_popen)
    sys.settrace(interrupt_first_owned_line)
    try:
        with pytest.raises(KeyboardInterrupt, match="first post-Popen line"):
            engine.run_process(["anvil", "status"], tmp_path, timeout=1)
    finally:
        sys.settrace(None)

    assert process.killed is True
    assert process.stdout.closed is True
    assert process.stderr.closed is True


@pytest.mark.skipif(os.name != "nt", reason="Windows suspended-process ownership")
def test_owned_popen_cleans_live_child_when_constructor_raises_after_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready = tmp_path / "constructor-child-ready"
    escaped = tmp_path / "constructor-child-escaped"
    command = (
        "import pathlib, time; "
        f"pathlib.Path({str(ready)!r}).write_text('ready'); "
        "time.sleep(0.8); "
        f"pathlib.Path({str(escaped)!r}).write_text('escaped')"
    )
    spawned_pid: list[int] = []

    def resume_then_interrupt(proc: engine._OwnedPopen) -> None:
        spawned_pid.append(proc.pid)
        assert engine._windows_resume_process(proc)
        deadline = time.monotonic() + 1
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        raise KeyboardInterrupt("constructor interrupted after spawn")

    monkeypatch.setattr(engine._OwnedPopen, "_after_spawn", resume_then_interrupt)

    with pytest.raises(KeyboardInterrupt, match="constructor interrupted after spawn"):
        engine.run_process([sys.executable, "-c", command], tmp_path, timeout=3)

    assert spawned_pid
    assert ready.exists()
    time.sleep(1)
    assert not escaped.exists()


def test_output_limit_detects_flushed_limit_plus_one_without_waiting_for_eof(
    tmp_path: Path,
) -> None:
    survived = tmp_path / "over-limit-child-survived"
    command = (
        "import pathlib, sys, time; "
        "sys.stdout.buffer.write(b'x' * 129); "
        "sys.stdout.buffer.flush(); "
        "time.sleep(1); "
        f"pathlib.Path({str(survived)!r}).write_text('survived'); "
        "time.sleep(5)"
    )

    result = engine.run_process(
        [sys.executable, "-c", command],
        tmp_path,
        timeout=4,
        output_limit_bytes=128,
    )

    assert result == RunResult(125, "", "output limit exceeded")
    time.sleep(1.2)
    assert not survived.exists()


def test_close_handle_failure_is_retried_and_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _FakeProcess()
    closed_jobs = _stub_process_containment(monkeypatch, process)
    close_attempts: list[int] = []

    def transient_close_failure(handle: int) -> None:
        close_attempts.append(handle)
        if len(close_attempts) == 1:
            raise OSError("CloseHandle failed")
        closed_jobs.append(handle)

    monkeypatch.setattr(engine, "_windows_close_handle", transient_close_failure)

    result = engine.run_process(["anvil", "status"], tmp_path, timeout=1)

    if engine.os.name == "nt":
        assert result == RunResult(126, "", "process supervision failed")
        assert close_attempts == [77, 77]
        assert closed_jobs == [77]
        assert process.killed is True
    else:
        assert result == RunResult(0, "", "")


def test_windows_close_handle_checks_false_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FalseCloseHandle:
        argtypes: list[object] = []
        restype: object = None

        def __call__(self, _handle: object) -> int:
            return 0

    class Kernel32:
        CloseHandle = FalseCloseHandle()

    monkeypatch.setattr(engine.os, "name", "nt")
    monkeypatch.setattr(
        engine.ctypes,
        "WinDLL",
        lambda *_args, **_kwargs: Kernel32(),
        raising=False,
    )
    monkeypatch.setattr(engine.ctypes, "get_last_error", lambda: 6, raising=False)

    with pytest.raises(OSError, match="CloseHandle failed"):
        engine._windows_close_handle(77)


@pytest.mark.parametrize("phase", ["job", "resume"])
def test_containment_setup_baseexception_cleans_up_before_rethrow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    process = _FakeProcess()
    closed_jobs = _stub_process_containment(monkeypatch, process)
    if phase == "job":
        monkeypatch.setattr(
            engine,
            "_windows_kill_on_close_job",
            lambda _proc: (_ for _ in ()).throw(
                KeyboardInterrupt("job assignment interrupted")
            ),
        )
    else:
        monkeypatch.setattr(
            engine,
            "_windows_resume_process",
            lambda _proc: (_ for _ in ()).throw(
                KeyboardInterrupt("resume interrupted")
            ),
        )

    with pytest.raises(KeyboardInterrupt, match="interrupted"):
        engine.run_process(["anvil", "status"], tmp_path, timeout=1)

    assert process.killed is True
    assert process.stdout.closed is True
    assert process.stderr.closed is True
    if engine.os.name == "nt" and phase == "resume":
        assert closed_jobs == [77]


@pytest.mark.parametrize("failure", [MemoryError, KeyboardInterrupt])
def test_post_popen_state_initialization_baseexception_cleans_up_before_rethrow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: type[BaseException],
) -> None:
    process = _FakeProcess()
    closed_jobs = _stub_process_containment(monkeypatch, process)
    monkeypatch.setattr(
        engine.threading,
        "Lock",
        lambda: (_ for _ in ()).throw(failure("state initialization failed")),
    )

    with pytest.raises(failure, match="state initialization failed"):
        engine.run_process(["anvil", "status"], tmp_path, timeout=1)

    assert process.killed is True
    assert process.stdout.closed is True
    assert process.stderr.closed is True
    if engine.os.name == "nt":
        assert closed_jobs == [77]


def test_setup_project_rejects_failed_git_init(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        engine,
        "run_process",
        lambda *_args, **_kwargs: RunResult(17, "PRIVATE_TOKEN", "raw output"),
    )

    with pytest.raises(
        RuntimeError,
        match="phase=git-init exit_code=17",
    ) as exc_info:
        engine.setup_project(tmp_path / "fixture", "broken", [])

    assert "PRIVATE_TOKEN" not in str(exc_info.value)
    assert "raw output" not in str(exc_info.value)
    assert not (tmp_path / "fixture" / ".anvil").exists()


def test_setup_project_initializes_only_the_workspace_git_repository(
    tmp_path: Path,
) -> None:
    task = engine.TaskSpec("T001", "Benchmark task", files=("workspace/x.txt",))
    project = engine.setup_project(tmp_path / "fixture", "benchmark", [task])

    assert (project.workspace / ".git").is_dir()
    assert not (project.root / ".git").exists()


def test_report_only_cannot_mask_git_init_infrastructure_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scenario = Scenario(
        key="fixture",
        title="Fixture",
        description="Synthetic setup failure.",
        tasks=[],
        actors=1,
        headline=("final_state_valid",),
    )
    monkeypatch.setattr(runner, "all_scenarios", lambda: {"fixture": scenario})
    monkeypatch.setattr(
        engine,
        "run_process",
        lambda *_args, **_kwargs: RunResult(17, "PRIVATE_TOKEN", "raw output"),
    )
    output = tmp_path / "must-not-exist.md"

    exit_code = runner.main(
        [
            "--scenarios",
            "fixture",
            "--quick",
            "--out",
            str(output),
            "--allow-invalid-results",
        ]
    )

    assert exit_code == 2
    assert not output.exists()
    assert not runner._report_lock_path(output).exists()
    captured = capsys.readouterr()
    assert "PRIVATE_TOKEN" not in captured.out + captured.err
    assert "raw output" not in captured.out + captured.err


@pytest.mark.skipif(
    os.name == "nt" or not sys.platform.startswith("linux"),
    reason="Linux prctl/subreaper contract",
)
def test_linux_subreaper_kills_setsid_descendant_before_success(
    tmp_path: Path,
) -> None:
    ready = tmp_path / "detached-ready"
    escaped = tmp_path / "detached-escaped"
    descendant = (
        "import os, pathlib, time; "
        "os.setsid(); "
        f"pathlib.Path({str(ready)!r}).write_text('ready'); "
        "os.close(0); os.close(1); os.close(2); "
        "time.sleep(0.4); "
        f"pathlib.Path({str(escaped)!r}).write_text('escaped')"
    )
    command = (
        "import pathlib, subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {descendant!r}]); "
        f"p=pathlib.Path({str(ready)!r}); "
        "deadline=time.monotonic()+1; "
        "exec(\"while not p.exists() and time.monotonic() < deadline:\\n "
        " time.sleep(0.01)\")"
    )

    result = engine.run_process([sys.executable, "-c", command], tmp_path, timeout=2)

    assert result == RunResult(0, "", "")
    assert ready.exists()
    time.sleep(0.6)
    assert not escaped.exists()


@pytest.mark.skipif(
    os.name == "nt" or not sys.platform.startswith("linux"),
    reason="Linux prctl/subreaper contract",
)
def test_linux_timeout_kills_setsid_descendant(
    tmp_path: Path,
) -> None:
    ready = tmp_path / "timeout-detached-ready"
    escaped = tmp_path / "timeout-detached-escaped"
    descendant = (
        "import os, pathlib, time; "
        "os.setsid(); "
        f"pathlib.Path({str(ready)!r}).write_text('ready'); "
        "os.close(0); os.close(1); os.close(2); "
        "time.sleep(0.6); "
        f"pathlib.Path({str(escaped)!r}).write_text('escaped')"
    )
    command = (
        "import pathlib, subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {descendant!r}]); "
        f"p=pathlib.Path({str(ready)!r}); "
        "deadline=time.monotonic()+1; "
        "exec(\"while not p.exists() and time.monotonic() < deadline:\\n "
        " time.sleep(0.01)\"); "
        "time.sleep(5)"
    )

    result = engine.run_process([sys.executable, "-c", command], tmp_path, timeout=0.3)

    assert result == RunResult(124, "", "timeout")
    assert ready.exists()
    time.sleep(0.8)
    assert not escaped.exists()


def test_wait_oserror_contains_process_and_returns_infrastructure_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _WaitFailureProcess()
    closed_jobs = _stub_process_containment(monkeypatch, process)

    result = engine.run_process(["anvil", "status"], tmp_path, timeout=1)

    assert result == RunResult(126, "", "process supervision failed")
    assert process.killed is True
    assert process.wait_calls >= 2
    assert process.stdout.closed is True
    assert process.stderr.closed is True
    if engine.os.name == "nt":
        assert closed_jobs == [77]
