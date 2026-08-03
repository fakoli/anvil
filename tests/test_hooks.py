"""SessionStart schema and installation-skew contracts (issue #180)."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import threading
from pathlib import Path

import pytest
from typer.testing import CliRunner

import anvil.cli.hooks as hooks
from anvil.cli import _version_probe_worker, app
from anvil.state.schema import SCHEMA_VERSION

runner = CliRunner()


class _RecordingInput(io.BytesIO):
    def __init__(self) -> None:
        super().__init__()
        self.closed_by_probe = False

    def close(self) -> None:
        self.closed_by_probe = True


class _InterruptingInput(_RecordingInput):
    def write(self, _data: bytes) -> int:
        raise KeyboardInterrupt


class _WorkerProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.killed = False
        self.pid = 789

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = 1


class _FakeProcess:
    def __init__(
        self,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int | None = 0,
    ) -> None:
        self.stdin = _RecordingInput()
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.returncode = returncode
        self.pid = 999_999

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        if self.returncode is None:
            raise subprocess.TimeoutExpired("anvil", 0)
        return self.returncode

    def kill(self) -> None:
        self.returncode = 1


def _future_schema_project(tmp_path: Path) -> Path:
    original_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        initialized = runner.invoke(app, ["init", "--name", "Hook Test"])
    finally:
        os.chdir(original_cwd)
    assert initialized.exit_code == 0, initialized.output
    state_dir = tmp_path / ".anvil"
    connection = sqlite3.connect(state_dir / "state.db")
    try:
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        connection.commit()
    finally:
        connection.close()
    return state_dir


def test_hook_format_schema_mismatch_is_honest_and_nonblocking(
    tmp_path: Path,
) -> None:
    state_dir = _future_schema_project(tmp_path)
    original_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = runner.invoke(app, ["status", "--hook-format"])
    finally:
        os.chdir(original_cwd)

    assert result.exit_code == 0
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert len(lines) == 1
    assert lines[0].startswith("schema_mismatch ")
    assert f"supported-schema:{SCHEMA_VERSION}" in lines[0]
    assert f"database-schema:{SCHEMA_VERSION + 1}" in lines[0]
    assert "prd-status:" not in lines[0]
    assert str(state_dir.resolve()) not in lines[0]
    assert len(lines[0].encode("utf-8")) <= 4_096


def _schema_line() -> str:
    return (
        "schema_mismatch engine-version:0.6.0 supported-schema:16 "
        "database-schema:17 direction:newer remediation-code:upgrade_engine "
        "restart-required:true"
    )


def _dispatch_context(tmp_path: Path) -> tuple[int, str]:
    result = runner.invoke(
        app,
        ["hook", "dispatch", "detect-state", "--cwd", str(tmp_path)],
        input="{}",
    )
    payload = json.loads(result.stdout)
    return result.exit_code, payload["hookSpecificOutput"]["additionalContext"]


def test_session_start_targets_stale_plugin_when_path_supports_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hooks, "_status_hook_line", lambda _cwd: (_schema_line(), 0))
    monkeypatch.setattr(
        hooks,
        "_active_hook_identity",
        lambda: hooks._EngineIdentity("0.6.0", 16),
    )
    monkeypatch.setattr(
        hooks,
        "_probe_plugin_manifest",
        lambda: hooks._InstallationProbe("ok", "0.6.0"),
    )
    monkeypatch.setattr(
        hooks,
        "_probe_path_engine",
        lambda: hooks._InstallationProbe("ok", "0.7.0", 17),
    )

    exit_code, context = _dispatch_context(tmp_path)
    assert exit_code == 0
    assert "active-hook:0.6.0/schema16" in context
    assert "plugin-manifest:0.6.0" in context
    assert "PATH:0.7.0/schema17" in context
    assert "database:schema17" in context
    assert "update the Anvil plugin" in context
    assert "restart the harness/MCP server" in context
    assert "uv tool upgrade" not in context
    assert "migrate-workspace" not in context
    assert len(context.encode("utf-8")) <= 4_096


def test_session_start_targets_stale_path_and_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hooks, "_status_hook_line", lambda _cwd: (_schema_line(), 0))
    monkeypatch.setattr(
        hooks,
        "_active_hook_identity",
        lambda: hooks._EngineIdentity("0.6.0", 16),
    )
    monkeypatch.setattr(
        hooks,
        "_probe_plugin_manifest",
        lambda: hooks._InstallationProbe("ok", "0.6.0"),
    )
    monkeypatch.setattr(
        hooks,
        "_probe_path_engine",
        lambda: hooks._InstallationProbe("ok", "0.6.0", 16),
    )

    exit_code, context = _dispatch_context(tmp_path)
    assert exit_code == 0
    assert "run `uv tool upgrade anvil-state`" in context
    assert "update the Anvil plugin" in context
    assert "restart the harness/MCP server" in context
    assert "Do not delete state" in context


def test_session_start_reports_schema_compatible_version_skew(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        hooks,
        "_status_hook_line",
        lambda _cwd: (
            "active-claims:0 ready-tasks:1 blockers:0 prd-status:approved",
            0,
        ),
    )
    monkeypatch.setattr(hooks, "_language_for_cwd", lambda _cwd: "Python")
    monkeypatch.setattr(
        hooks,
        "_active_hook_identity",
        lambda: hooks._EngineIdentity("0.6.0", 16),
    )
    monkeypatch.setattr(
        hooks,
        "_probe_plugin_manifest",
        lambda: hooks._InstallationProbe("ok", "0.6.0"),
    )
    monkeypatch.setattr(
        hooks,
        "_probe_path_engine",
        lambda: hooks._InstallationProbe("ok", "0.7.0", 16),
    )

    exit_code, context = _dispatch_context(tmp_path)
    assert exit_code == 0
    assert "ready-tasks:1" in context
    assert "install-skew" in context
    assert "unequal-version/schema-compatible" in context
    assert "database:schema16" in context
    assert "update the Anvil plugin" in context
    assert "uv tool upgrade" not in context


def test_session_start_only_restarts_for_newer_plugin_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        hooks,
        "_status_hook_line",
        lambda _cwd: (
            "active-claims:0 ready-tasks:1 blockers:0 prd-status:approved",
            0,
        ),
    )
    monkeypatch.setattr(
        hooks,
        "_active_hook_identity",
        lambda: hooks._EngineIdentity("0.6.0", 16),
    )
    monkeypatch.setattr(
        hooks,
        "_probe_plugin_manifest",
        lambda: hooks._InstallationProbe("ok", "0.7.0"),
    )
    monkeypatch.setattr(
        hooks,
        "_probe_path_engine",
        lambda: hooks._InstallationProbe("ok", "0.6.0", 16),
    )

    exit_code, context = _dispatch_context(tmp_path)
    assert exit_code == 0
    assert "restart the harness/MCP server" in context
    assert "update the Anvil plugin" not in context
    assert "uv tool upgrade" not in context


def test_schema_mismatch_older_database_does_not_assume_newer_path_compatible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        hooks,
        "_status_hook_line",
        lambda _cwd: (
            "schema_mismatch engine-version:0.7.0 supported-schema:17 "
            "database-schema:16 direction:older "
            "remediation-code:use_compatible_engine restart-required:true",
            0,
        ),
    )
    monkeypatch.setattr(
        hooks,
        "_active_hook_identity",
        lambda: hooks._EngineIdentity("0.7.0", 17),
    )
    monkeypatch.setattr(
        hooks,
        "_probe_plugin_manifest",
        lambda: hooks._InstallationProbe("ok", "0.7.0"),
    )
    monkeypatch.setattr(
        hooks,
        "_probe_path_engine",
        lambda: hooks._InstallationProbe("ok", "0.8.0", 18),
    )

    exit_code, context = _dispatch_context(tmp_path)
    assert exit_code == 0
    assert "compatible with database schema 16" in context
    assert "uv tool upgrade" not in context


def test_schema_mismatch_only_restarts_when_manifest_and_path_support_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hooks, "_status_hook_line", lambda _cwd: (_schema_line(), 0))
    monkeypatch.setattr(
        hooks,
        "_active_hook_identity",
        lambda: hooks._EngineIdentity("0.6.0", 16),
    )
    monkeypatch.setattr(
        hooks,
        "_probe_plugin_manifest",
        lambda: hooks._InstallationProbe("ok", "0.7.0"),
    )
    monkeypatch.setattr(
        hooks,
        "_probe_path_engine",
        lambda: hooks._InstallationProbe("ok", "0.7.0", 17),
    )

    exit_code, context = _dispatch_context(tmp_path)
    assert exit_code == 0
    assert "restart the harness/MCP server" in context
    assert "update the Anvil plugin" not in context
    assert "uv tool upgrade" not in context


def test_schema_mismatch_equal_version_unequal_schema_requires_build_alignment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        hooks,
        "_status_hook_line",
        lambda _cwd: (
            "schema_mismatch engine-version:0.6.0 supported-schema:16 "
            "database-schema:17 direction:newer "
            "remediation-code:upgrade_engine restart-required:true",
            0,
        ),
    )
    monkeypatch.setattr(
        hooks,
        "_active_hook_identity",
        lambda: hooks._EngineIdentity("0.6.0", 16),
    )
    monkeypatch.setattr(
        hooks,
        "_probe_plugin_manifest",
        lambda: hooks._InstallationProbe("ok", "0.6.0"),
    )
    monkeypatch.setattr(
        hooks,
        "_probe_path_engine",
        lambda: hooks._InstallationProbe("ok", "0.6.0", 17),
    )

    exit_code, context = _dispatch_context(tmp_path)
    assert exit_code == 0
    assert "equal-version/unequal-schema" in context
    assert "align the Anvil plugin and PATH to one build" in context
    assert "uv tool upgrade" not in context


def test_healthy_prerelease_manifest_and_path_agreement_only_restarts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        hooks,
        "_status_hook_line",
        lambda _cwd: (
            "active-claims:0 ready-tasks:1 blockers:0 prd-status:approved",
            0,
        ),
    )
    monkeypatch.setattr(
        hooks,
        "_active_hook_identity",
        lambda: hooks._EngineIdentity("0.6.0", 16),
    )
    monkeypatch.setattr(
        hooks,
        "_probe_plugin_manifest",
        lambda: hooks._InstallationProbe("ok", "0.7.0rc1"),
    )
    monkeypatch.setattr(
        hooks,
        "_probe_path_engine",
        lambda: hooks._InstallationProbe("ok", "0.7.0rc1", 16),
    )

    exit_code, context = _dispatch_context(tmp_path)
    assert exit_code == 0
    assert "restart the harness/MCP server" in context
    assert "align the Anvil plugin and PATH" not in context
    assert "update the Anvil plugin" not in context


def test_session_start_reports_equal_version_unequal_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        hooks,
        "_status_hook_line",
        lambda _cwd: (
            "active-claims:0 ready-tasks:0 blockers:0 prd-status:none",
            0,
        ),
    )
    monkeypatch.setattr(
        hooks,
        "_active_hook_identity",
        lambda: hooks._EngineIdentity("0.6.0", 16),
    )
    monkeypatch.setattr(
        hooks,
        "_probe_plugin_manifest",
        lambda: hooks._InstallationProbe("ok", "0.6.0"),
    )
    monkeypatch.setattr(
        hooks,
        "_probe_path_engine",
        lambda: hooks._InstallationProbe("ok", "0.6.0", 17),
    )

    exit_code, context = _dispatch_context(tmp_path)
    assert exit_code == 0
    assert "equal-version/unequal-schema" in context
    assert "PATH:0.6.0/schema17" in context
    assert "align the Anvil plugin and PATH to one build" in context
    assert "uv tool upgrade" not in context


@pytest.mark.parametrize(
    ("resolver", "process", "expected"),
    [
        (lambda _name: None, None, "unavailable"),
        (
            lambda _name: "C:/Program Files/Anvil/anvil.exe",
            _FakeProcess(returncode=None),
            "timeout",
        ),
        (
            lambda _name: "C:/Program Files/Anvil/anvil.exe",
            _FakeProcess(stdout=b"not a version"),
            "malformed",
        ),
    ],
    ids=["unavailable", "timeout", "malformed"],
)
def test_path_probe_distinguishes_closed_failure_states(
    resolver, process, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    monkeypatch.setattr(hooks, "_HOOK_ENGINE_PROBE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(
        hooks,
        "_terminate_probe_tree",
        lambda child, _job: child.kill(),
    )
    launcher = None if process is None else lambda *_args, **_kwargs: process
    probe = hooks._probe_path_engine(which_fn=resolver, popen_fn=launcher)
    assert probe == hooks._InstallationProbe(expected)


def test_version_probe_worker_rejects_trailing_request_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launched: list[list[str]] = []
    request_stream = io.TextIOWrapper(
        io.BytesIO(
            b'{"executable":"C:/Anvil/anvil.exe","parent_pid":123}\nTRAILING\n'
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(_version_probe_worker.sys, "stdin", request_stream)
    monkeypatch.setattr(
        _version_probe_worker.subprocess,
        "Popen",
        lambda argv, **_kwargs: launched.append(argv),
    )

    assert _version_probe_worker.main() == 2
    assert launched == []


def test_version_probe_worker_refuses_if_posix_parent_already_exited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launched: list[list[str]] = []
    request_stream = io.TextIOWrapper(
        io.BytesIO(b'{"executable":"/opt/anvil","parent_pid":123}\n'),
        encoding="utf-8",
    )
    monkeypatch.setattr(_version_probe_worker.sys, "stdin", request_stream)
    monkeypatch.setattr(_version_probe_worker.os, "name", "posix")
    monkeypatch.setattr(_version_probe_worker.os, "getppid", lambda: 1)
    monkeypatch.setattr(
        _version_probe_worker.subprocess,
        "Popen",
        lambda argv, **_kwargs: launched.append(argv),
    )

    assert _version_probe_worker.main() == 124
    assert launched == []


def test_version_probe_worker_kills_posix_group_when_parent_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _WorkerProcess()
    parent_ids = iter([123, 1])
    killed_groups: list[tuple[int, int]] = []
    request_stream = io.TextIOWrapper(
        io.BytesIO(b'{"executable":"/opt/anvil","parent_pid":123}\n'),
        encoding="utf-8",
    )
    monkeypatch.setattr(_version_probe_worker.sys, "stdin", request_stream)
    monkeypatch.setattr(_version_probe_worker.os, "name", "posix")
    monkeypatch.setattr(
        _version_probe_worker.signal, "SIGKILL", 9, raising=False
    )
    monkeypatch.setattr(
        _version_probe_worker.os, "getppid", lambda: next(parent_ids)
    )
    monkeypatch.setattr(_version_probe_worker.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(
        _version_probe_worker.os,
        "killpg",
        lambda group, sent_signal: killed_groups.append((group, sent_signal)),
        raising=False,
    )
    monkeypatch.setattr(
        _version_probe_worker.subprocess,
        "Popen",
        lambda *_args, **_kwargs: process,
    )

    assert _version_probe_worker.main() == 124
    assert killed_groups == [(789, 9), (789, 9)]
    assert process.killed


def test_version_probe_worker_cleans_posix_group_after_target_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _WorkerProcess()
    process.returncode = 0
    killed_groups: list[tuple[int, int]] = []
    request_stream = io.TextIOWrapper(
        io.BytesIO(b'{"executable":"/opt/anvil","parent_pid":123}\n'),
        encoding="utf-8",
    )
    monkeypatch.setattr(_version_probe_worker.sys, "stdin", request_stream)
    monkeypatch.setattr(_version_probe_worker.os, "name", "posix")
    monkeypatch.setattr(_version_probe_worker.os, "getppid", lambda: 123)
    monkeypatch.setattr(
        _version_probe_worker.signal, "SIGKILL", 9, raising=False
    )
    monkeypatch.setattr(_version_probe_worker.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(
        _version_probe_worker.os,
        "killpg",
        lambda group, sent_signal: killed_groups.append((group, sent_signal)),
        raising=False,
    )
    monkeypatch.setattr(
        _version_probe_worker.subprocess,
        "Popen",
        lambda *_args, **_kwargs: process,
    )

    assert _version_probe_worker.main() == 0
    assert killed_groups == [(789, 9)]


def test_version_probe_worker_cleans_target_if_handler_setup_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _WorkerProcess()
    killed_groups: list[tuple[int, int]] = []
    request_stream = io.TextIOWrapper(
        io.BytesIO(b'{"executable":"/opt/anvil","parent_pid":123}\n'),
        encoding="utf-8",
    )
    monkeypatch.setattr(_version_probe_worker.sys, "stdin", request_stream)
    monkeypatch.setattr(_version_probe_worker.os, "name", "posix")
    monkeypatch.setattr(_version_probe_worker.os, "getppid", lambda: 123)
    monkeypatch.setattr(
        _version_probe_worker.signal, "SIGKILL", 9, raising=False
    )
    monkeypatch.setattr(
        _version_probe_worker.signal,
        "signal",
        lambda *_args: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    monkeypatch.setattr(
        _version_probe_worker.os,
        "killpg",
        lambda group, sent_signal: killed_groups.append((group, sent_signal)),
        raising=False,
    )
    monkeypatch.setattr(
        _version_probe_worker.subprocess,
        "Popen",
        lambda *_args, **_kwargs: process,
    )

    with pytest.raises(KeyboardInterrupt):
        _version_probe_worker.main()
    assert killed_groups == [(789, 9)]


def test_path_probe_executes_space_path_without_shell_parsing() -> None:
    calls: list[list[str]] = []
    process = _FakeProcess(stdout=b"anvil 0.7.0 (schema 17)\n")

    def fake_popen(argv: list[str], **_kwargs: object) -> _FakeProcess:
        calls.append(argv)
        return process

    probe = hooks._probe_path_engine(
        which_fn=lambda _name: "C:/Program Files/Anvil Tool/anvil.exe",
        popen_fn=fake_popen,
    )
    assert calls == [
        [hooks.sys.executable, "-m", "anvil.cli._version_probe_worker"]
    ]
    request = json.loads(process.stdin.getvalue())
    assert request == {
        "executable": "C:/Program Files/Anvil Tool/anvil.exe",
        "parent_pid": hooks.os.getpid(),
    }
    assert process.stdin.closed_by_probe
    assert probe == hooks._InstallationProbe("ok", "0.7.0", 17)


def test_path_probe_response_budget_starts_after_worker_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess(stdout=b"anvil 0.7.0 (schema 17)\n")
    monkeypatch.setattr(hooks, "_HOOK_ENGINE_PROBE_TIMEOUT_SECONDS", 0.01)

    def delayed_launch(*_args: object, **_kwargs: object) -> _FakeProcess:
        hooks.time.sleep(0.02)
        return process

    probe = hooks._probe_path_engine(
        which_fn=lambda _name: "C:/Anvil/anvil.exe",
        popen_fn=delayed_launch,
    )

    assert probe == hooks._InstallationProbe("ok", "0.7.0", 17)


def test_path_probe_tears_down_containment_after_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess(stdout=b"anvil 0.7.0 (schema 17)\n")
    terminated: list[tuple[_FakeProcess, int | None]] = []
    monkeypatch.setattr(hooks.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(hooks, "_create_windows_kill_job", lambda _child: 123)
    monkeypatch.setattr(
        hooks,
        "_terminate_probe_tree",
        lambda child, job: terminated.append((child, job)),
    )

    probe = hooks._probe_path_engine(
        which_fn=lambda _name: "C:/Program Files/Anvil Tool/anvil.exe"
    )

    assert probe == hooks._InstallationProbe("ok", "0.7.0", 17)
    assert terminated == [(process, 123)]


def test_path_probe_cleans_up_when_request_handshake_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess()
    process.stdin = _InterruptingInput()
    terminated: list[tuple[_FakeProcess, int | None]] = []
    monkeypatch.setattr(hooks.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(hooks, "_create_windows_kill_job", lambda _child: 123)
    monkeypatch.setattr(
        hooks,
        "_terminate_probe_tree",
        lambda child, job: terminated.append((child, job)),
    )

    probe = hooks._probe_path_engine(
        which_fn=lambda _name: "C:/Program Files/Anvil Tool/anvil.exe"
    )

    assert probe == hooks._InstallationProbe("timeout")
    assert terminated == [(process, 123)]
    assert process.stdin.closed_by_probe


def test_path_probe_cleans_up_when_job_creation_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess()
    terminated: list[tuple[_FakeProcess, int | None]] = []
    monkeypatch.setattr(hooks.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(
        hooks,
        "_create_windows_kill_job",
        lambda _child: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    monkeypatch.setattr(
        hooks,
        "_terminate_probe_tree",
        lambda child, job: terminated.append((child, job)),
    )

    probe = hooks._probe_path_engine(
        which_fn=lambda _name: "C:/Program Files/Anvil Tool/anvil.exe"
    )

    assert probe == hooks._InstallationProbe("timeout")
    assert terminated == [(process, None)]
    assert process.stdin.closed_by_probe


def test_posix_cleanup_does_not_hard_kill_watchdog_during_target_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess(returncode=None)
    sent_signals: list[tuple[int, int]] = []
    monkeypatch.setattr(hooks.os, "name", "posix")
    monkeypatch.setattr(hooks.signal, "SIGTERM", 15)
    monkeypatch.setattr(
        hooks.os,
        "killpg",
        lambda group, sent_signal: sent_signals.append((group, sent_signal)),
        raising=False,
    )

    hooks._terminate_probe_tree(process, None)

    assert sent_signals == [(process.pid, 15)]
    assert process.returncode is None


def test_path_probe_rejects_output_at_stream_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess(stdout=b"x" * 2_000, returncode=None)
    monkeypatch.setattr(
        hooks,
        "_terminate_probe_tree",
        lambda child, _job: child.kill(),
    )
    probe = hooks._probe_path_engine(
        which_fn=lambda _name: "C:/Anvil/anvil.exe",
        popen_fn=lambda *_args, **_kwargs: process,
    )
    assert probe == hooks._InstallationProbe("malformed")
    assert all(
        not thread.name.startswith("anvil-hook-version-")
        for thread in threading.enumerate()
    )


def test_identity_relationship_distinguishes_version_and_schema_skew() -> None:
    active = hooks._EngineIdentity("0.6.0", 16)
    assert (
        hooks._identity_relationship(
            active, hooks._InstallationProbe("ok", "0.6.0", 17)
        )
        == "equal-version/unequal-schema"
    )
    assert (
        hooks._identity_relationship(
            active, hooks._InstallationProbe("ok", "0.7.0", 16)
        )
        == "unequal-version/schema-compatible"
    )


def test_plugin_manifest_probe_reports_packaged_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_dir = tmp_path / ".claude-plugin"
    manifest_dir.mkdir()
    (manifest_dir / "plugin.json").write_text(
        json.dumps({"version": "0.8.0"}), encoding="utf-8"
    )
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
    assert hooks._probe_plugin_manifest() == hooks._InstallationProbe(
        "ok", "0.8.0"
    )


def test_session_start_discards_malformed_config_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state_dir = _future_schema_project(tmp_path)
    connection = sqlite3.connect(state_dir / "state.db")
    try:
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.commit()
    finally:
        connection.close()
    (state_dir / "config.yaml").write_text("events_storage: [", encoding="utf-8")

    line, exit_code = hooks._status_hook_line(tmp_path)
    captured = capsys.readouterr()
    assert exit_code == 0
    assert line.startswith("active-claims:")
    assert captured.err == ""
    assert str((state_dir / "config.yaml").resolve()) not in captured.out
