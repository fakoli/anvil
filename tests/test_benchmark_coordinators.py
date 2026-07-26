import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from benchmarks.harness import coordinators, engine

_REAL_RECORD_VERIFICATION_PROOFS = coordinators._record_verification_proofs


@pytest.fixture(autouse=True)
def _stub_verification_proof_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        coordinators,
        "_record_verification_proofs",
        lambda *_args, **_kwargs: None,
    )


def _task(task_id: str = "T001") -> engine.TaskSpec:
    return engine.TaskSpec(
        id=task_id,
        title="benchmark task",
        files=("shared.txt",),
        verification=("pytest -q",),
    )


def _coordinator(task: engine.TaskSpec) -> coordinators.AnvilCoordinator:
    return coordinators.AnvilCoordinator(
        engine.Project(root=Path("project"), tasks=[task])
    )


def _submit_success(
    task: engine.TaskSpec,
    actor: str,
    *,
    claim_id: str = "C1234ABCD",
    commands: list[str] | None = None,
    files: list[str] | None = None,
    gate_passed: bool = True,
) -> engine.RunResult:
    return engine.RunResult(
        0,
        json.dumps({
            "ok": True,
            "command": "submit",
            "data": {
                "evidence_id": "EV001",
                "claim_id": claim_id,
                "submitted_by": actor,
                "commands_run": (
                    commands if commands is not None else list(task.verification)
                ),
                "files_changed": files if files is not None else list(task.files),
                "evidence_gate": {"passed": gate_passed, "missing": []},
                "task": {"id": task.id, "status": "needs_review"},
            },
        }),
        "",
    )


def _apply_success(
    task: engine.TaskSpec, *, gate_passed: bool = True
) -> engine.RunResult:
    return engine.RunResult(
        0,
        json.dumps({
            "ok": True,
            "command": "apply",
            "data": {
                "task_id": task.id,
                "status": "done",
                "decision": "accepted",
                "reviewer": "bench",
                "has_evidence": True,
                "evidence_gate": {"passed": gate_passed, "missing": []},
                "task": {"id": task.id, "status": "done"},
            },
        }),
        "",
    )


def test_submit_failure_prevents_apply_and_returns_bounded_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task("T-submit")
    calls: list[list[str]] = []

    def fake_run(args: list[str], *_args, **_kwargs) -> engine.RunResult:
        calls.append(args)
        assert args[0] == "submit", "apply must not run after a failed submit"
        return engine.RunResult(17, "unbounded output", "traceback\nsecret")

    monkeypatch.setattr(coordinators.engine, "run", fake_run)

    outcome = _coordinator(task).complete(
        "agent-a", task, gamed=False, claim_id="C1234ABCD"
    )

    assert [call[0] for call in calls] == ["submit"]
    assert outcome.completed is False
    assert outcome.evidence_valid is None
    assert outcome.failure is not None
    assert outcome.failure.diagnostic == (
        "completion_failure phase=submit task=T-submit actor=agent-a exit_code=17"
    )
    assert "secret" not in outcome.failure.diagnostic


def test_submit_invocation_exception_is_bounded_and_prevents_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task("T-submit-exception")
    calls: list[str] = []

    def failed_submit(args: list[str], *_args, **_kwargs) -> engine.RunResult:
        calls.append(args[0])
        raise OSError("PRIVATE_TOKEN" * 100_000)

    monkeypatch.setattr(coordinators.engine, "run", failed_submit)

    with pytest.raises(
        coordinators.CoordinationInfrastructureError,
        match=r"phase=submit invocation=failed$",
    ) as exc_info:
        _coordinator(task).complete(
            "agent", task, gamed=False, claim_id="C1234ABCD"
        )

    assert calls == ["submit"]
    assert "PRIVATE_TOKEN" not in str(exc_info.value)
    assert len(str(exc_info.value).encode("utf-8")) <= 4096


def test_apply_failure_cannot_create_successful_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task("T-apply")
    results = iter([
        _submit_success(task, "agent-b"),
        engine.RunResult(23, "", "raw apply traceback"),
    ])
    calls: list[list[str]] = []

    def fake_run(args: list[str], *_args, **_kwargs) -> engine.RunResult:
        calls.append(args)
        return next(results)

    monkeypatch.setattr(coordinators.engine, "run", fake_run)

    outcome = _coordinator(task).complete(
        "agent-b", task, gamed=False, claim_id="C1234ABCD"
    )

    assert [call[0] for call in calls] == ["submit", "apply"]
    assert outcome.completed is False
    assert outcome.evidence_valid is True
    assert outcome.failure is not None
    assert outcome.failure.diagnostic == (
        "completion_failure phase=apply task=T-apply actor=agent-b exit_code=23"
    )


def test_apply_invocation_exception_is_bounded_after_successful_submit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task("T-apply-exception")
    calls: list[str] = []

    def failed_apply(args: list[str], *_args, **_kwargs) -> engine.RunResult:
        calls.append(args[0])
        if args[0] == "submit":
            return _submit_success(task, "agent")
        raise OSError("PRIVATE_TOKEN" * 100_000)

    monkeypatch.setattr(coordinators.engine, "run", failed_apply)

    with pytest.raises(
        coordinators.CoordinationInfrastructureError,
        match=r"phase=apply invocation=failed$",
    ) as exc_info:
        _coordinator(task).complete(
            "agent", task, gamed=False, claim_id="C1234ABCD"
        )

    assert calls == ["submit", "apply"]
    assert "PRIVATE_TOKEN" not in str(exc_info.value)
    assert len(str(exc_info.value).encode("utf-8")) <= 4096


def test_successful_completion_has_no_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    task = _task()
    monkeypatch.setattr(
        coordinators.engine,
        "run",
        lambda args, *_args, **_kwargs: (
            _submit_success(task, "agent")
            if args[0] == "submit"
            else _apply_success(task)
        ),
    )

    outcome = _coordinator(task).complete(
        "agent", task, gamed=False, claim_id="C1234ABCD"
    )

    assert outcome == coordinators.CompletionOutcome(
        completed=True,
        evidence_valid=True,
    )


def test_completion_refuses_apply_after_submit_consumes_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task()
    calls: list[str] = []

    def delayed_submit(args: list[str], *_args, **_kwargs) -> engine.RunResult:
        calls.append(args[0])
        time.sleep(0.02)
        return _submit_success(task, "agent")

    monkeypatch.setattr(coordinators.engine, "run", delayed_submit)

    with pytest.raises(
        coordinators.CoordinationInfrastructureError,
        match=r"phase=apply deadline=exceeded$",
    ):
        _coordinator(task).complete(
            "agent",
            task,
            gamed=False,
            timeout=0.005,
            claim_id="C1234ABCD",
        )

    assert calls == ["submit"]


def test_completion_validity_is_bound_to_persisted_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task()
    mismatched = _submit_success(task, "agent")
    envelope = json.loads(mismatched.out)
    envelope["data"]["commands_run"] = ["echo done"]
    envelope["data"]["files_changed"] = []
    results = iter(
        [engine.RunResult(0, json.dumps(envelope), ""), _apply_success(task)]
    )
    monkeypatch.setattr(
        coordinators.engine,
        "run",
        lambda *_args, **_kwargs: next(results),
    )

    outcome = _coordinator(task).complete(
        "agent", task, gamed=False, claim_id="C1234ABCD"
    )

    assert outcome.completed is True
    assert outcome.evidence_valid is False
    assert outcome.failure is None


def test_completion_rejects_submit_bound_to_a_different_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task()
    calls: list[list[str]] = []

    def fake_run(args: list[str], *_args, **_kwargs) -> engine.RunResult:
        calls.append(args)
        return _submit_success(task, "agent", claim_id="C-other")

    monkeypatch.setattr(coordinators.engine, "run", fake_run)

    outcome = _coordinator(task).complete(
        "agent", task, gamed=False, claim_id="C-acquired"
    )

    assert [call[0] for call in calls] == ["submit"]
    assert outcome.completed is False
    assert outcome.evidence_valid is None
    assert outcome.failure is not None
    assert outcome.failure.phase == "submit"


@pytest.mark.parametrize(
    ("gate_passed", "expected_valid"),
    [(False, False), (True, True)],
)
def test_gamed_evidence_validity_comes_from_persisted_gates(
    monkeypatch: pytest.MonkeyPatch,
    gate_passed: bool,
    expected_valid: bool,
) -> None:
    task = _task()
    results = iter([
        _submit_success(
            task,
            "agent",
            commands=[coordinators.AnvilCoordinator.GAMED_COMMAND],
            gate_passed=gate_passed,
        ),
        _apply_success(task, gate_passed=gate_passed),
    ])
    monkeypatch.setattr(
        coordinators.engine,
        "run",
        lambda *_args, **_kwargs: next(results),
    )

    outcome = _coordinator(task).complete(
        "agent", task, gamed=True, claim_id="C1234ABCD"
    )

    assert outcome.completed is True
    assert outcome.evidence_valid is expected_valid
    assert outcome.failure is None


def test_completion_failure_diagnostic_is_single_line_and_utf8_bounded() -> None:
    failure = coordinators.CompletionFailure(
        phase="submit",
        task="T\n" + "é" * 5000,
        actor="actor\r\n" + "U0001f680" * 5000,
        exit_code=124,
    )

    diagnostic = failure.diagnostic

    assert "\n" not in diagnostic
    assert "\r" not in diagnostic
    assert len(diagnostic.encode("utf-8")) <= 4096
    assert "phase=submit" in diagnostic
    assert "task=" in diagnostic
    assert "actor=" in diagnostic
    assert diagnostic.endswith("exit_code=124")


def _next_result(task_id: str | None, *, code: int = 0) -> engine.RunResult:
    task = None if task_id is None else {"id": task_id}
    return engine.RunResult(
        code,
        json.dumps({"ok": True, "command": "next", "data": {"task": task}}),
        "",
    )


def _claim_error(code: str, message: str, *, exit_code: int = 1) -> engine.RunResult:
    return engine.RunResult(
        exit_code,
        json.dumps({
            "ok": False,
            "command": "claim",
            "error": {"code": code, "message": message},
        }),
        "",
    )


def _claim_result(
    task_id: str = "T001",
    actor: str = "agent",
    **claim_overrides,
) -> engine.RunResult:
    claim = {
        "id": "C1234ABCD",
        "task_id": task_id,
        "claimed_by": actor,
        "status": "active",
        **claim_overrides,
    }
    return engine.RunResult(
        0,
        json.dumps({"ok": True, "command": "claim", "data": {"claim": claim}}),
        "",
    )


def test_acquire_refuses_claim_after_next_consumes_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def delayed_next(args: list[str], *_args, **_kwargs) -> engine.RunResult:
        calls.append(args[0])
        time.sleep(0.02)
        return _next_result("T001")

    monkeypatch.setattr(coordinators.engine, "run", delayed_next)

    with pytest.raises(
        coordinators.CoordinationInfrastructureError,
        match=r"phase=claim deadline=exceeded$",
    ):
        _coordinator(_task()).acquire("agent", object(), timeout=0.005)

    assert calls == ["next"]


def test_acquire_treats_successful_empty_next_as_normal_no_ready_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], *_args, **_kwargs) -> engine.RunResult:
        calls.append(args)
        return _next_result(None)

    monkeypatch.setattr(coordinators.engine, "run", fake_run)

    assert _coordinator(_task()).acquire("agent", object()) is None
    assert calls == [["next", "--json"]]


def test_acquire_propagates_failed_next_as_infrastructure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        coordinators.engine,
        "run",
        lambda *_args, **_kwargs: engine.RunResult(
            70,
            "",
            "backend unavailable\n" + "raw" * 100_000,
        ),
    )

    with pytest.raises(
        coordinators.CoordinationInfrastructureError,
        match=r"phase=next exit_code=70$",
    ) as exc_info:
        _coordinator(_task()).acquire("agent", object())

    assert "backend unavailable" not in str(exc_info.value)


def test_acquire_bounds_next_invocation_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        coordinators.engine,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("PRIVATE_TOKEN")
        ),
    )

    with pytest.raises(
        coordinators.CoordinationInfrastructureError,
        match=r"phase=next invocation=failed$",
    ) as exc_info:
        _coordinator(_task()).acquire("agent", object())

    assert "PRIVATE_TOKEN" not in str(exc_info.value)


def test_acquire_rejects_wrong_next_command_on_zero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        coordinators.engine,
        "run",
        lambda *_args, **_kwargs: engine.RunResult(
            0,
            json.dumps({
                "ok": True,
                "command": "wrong-command",
                "data": {"task": {"id": "T001"}},
            }),
            "",
        ),
    )

    with pytest.raises(
        coordinators.CoordinationInfrastructureError,
        match=r"phase=next exit_code=0$",
    ):
        _coordinator(_task()).acquire("agent", object())


def test_acquire_accepts_only_exact_claim_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task()
    results = iter([_next_result(task.id), _claim_result(task.id, "agent")])
    monkeypatch.setattr(
        coordinators.engine,
        "run",
        lambda *_args, **_kwargs: next(results),
    )

    assert _coordinator(task).acquire("agent", object()) == coordinators.AcquiredTask(
        task_id=task.id,
        claim_id="C1234ABCD",
    )


@pytest.mark.parametrize(
    "claim_id",
    ["C001", "c1234abcd", "../../escape", "C1234ABCD/escape", "C1234ABCDE"],
)
def test_claim_success_rejects_noncanonical_claim_ids(claim_id: str) -> None:
    result = _claim_result("T001", "agent", id=claim_id)

    with pytest.raises(coordinators.CoordinationInfrastructureError):
        coordinators.require_claim_success(
            result,
            expected_task_id="T001",
            expected_actor="agent",
        )


def test_claim_buffer_path_is_canonical_and_cannot_escape(tmp_path: Path) -> None:
    proj = engine.Project(root=tmp_path, tasks=[])

    canonical = coordinators._claim_buffer_file(proj, "C1234ABCD")

    assert canonical == (
        tmp_path / ".anvil" / ".evidence-buffer" / "C1234ABCD.json"
    ).resolve()
    with pytest.raises(
        coordinators.CoordinationInfrastructureError,
        match=r"phase=verification_record claim_id=invalid$",
    ):
        coordinators._claim_buffer_file(proj, "../../escape")
    assert not (tmp_path / "escape.json").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows command-line parsing")
def test_windows_verification_command_preserves_quoted_arguments() -> None:
    expected = [
        r"C:\Program Files\Python\python.exe",
        "--output",
        r"C:\path with spaces\result file.json",
        "C:\\path with spaces\\trailing\\",
        'say "hello"',
        "",
    ]

    parsed = coordinators._split_verification_command(
        subprocess.list2cmdline(expected)
    )

    assert parsed == expected


@pytest.mark.skipif(os.name != "nt", reason="Windows command-line parsing")
def test_windows_quoted_failure_is_recorded_with_exact_exit_code(
    tmp_path: Path,
) -> None:
    project = engine.Project(root=tmp_path, tasks=[])
    command = subprocess.list2cmdline(
        [sys.executable, "-c", "raise SystemExit(7)"]
    )

    _REAL_RECORD_VERIFICATION_PROOFS(
        project,
        "agent",
        "C1234ABCD",
        [command],
        deadline=time.monotonic() + 10,
    )

    record_path = (
        tmp_path / ".anvil" / ".evidence-buffer" / "C1234ABCD.json"
    )
    records = [json.loads(line) for line in record_path.read_text().splitlines()]
    assert records == [
        {
            "kind": "command",
            "timestamp": records[0]["timestamp"],
            "command": command,
            "exit_code": 7,
            "output_sha256": hashlib.sha256(b"").hexdigest(),
            "actor": "agent",
        }
    ]


@pytest.mark.skipif(os.name != "nt", reason="Windows command-line parsing")
@pytest.mark.parametrize(
    "command",
    [
        "",
        " \t\r\n",
        '""',
        f'{sys.executable}\0 -c "raise SystemExit(9)"',
        f'"{sys.executable}" -c "raise SystemExit(0)',
    ],
)
def test_windows_invalid_verification_command_is_refused_before_execution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    command: str,
) -> None:
    project = engine.Project(root=tmp_path, tasks=[])

    def unexpected_run(*_args, **_kwargs) -> engine.RunResult:
        pytest.fail("invalid verification command must not be executed")

    monkeypatch.setattr(coordinators.engine, "run_process", unexpected_run)

    with pytest.raises(
        coordinators.CoordinationInfrastructureError,
        match=r"phase=verification_command parse=failed$",
    ):
        _REAL_RECORD_VERIFICATION_PROOFS(
            project,
            "agent",
            "C1234ABCD",
            [command],
            deadline=time.monotonic() + 10,
        )
    assert not (
        tmp_path / ".anvil" / ".evidence-buffer" / "C1234ABCD.json"
    ).exists()


@pytest.mark.parametrize(
    "command",
    [
        "python -c pass && bad",
        "python -c pass || bad",
        "python -c pass; bad",
        "python -c pass | bad",
        "python -c pass > result.txt",
        "python -c pass < input.txt",
        "echo $HOME",
        "echo $(bad)",
        "echo `bad`",
        "python *.py",
        "python file?.py",
        "python test[12].py",
        "cat ~/result.txt",
        "echo {first,second}",
        "FOO=bar python -V",
        'FOO="bar baz" python -V',
        "env FOO=~/proof python -V",
        "env PATH=/bin:~/bin python -V",
        "echo result # ignored",
        "(echo grouped)",
        "! false",
        "echo first\\\nsecond",
        'python -c "print(1)\\\nprint(2)"',
        "echo dangling\\",
        'python -c "print(1)',
        "first\nsecond",
    ],
)
def test_posix_shell_syntax_is_refused(command: str) -> None:
    with pytest.raises(ValueError, match="requires shell interpretation"):
        coordinators._validate_single_process_command(command, windows=False)


@pytest.mark.parametrize(
    "command",
    [
        "python -c pass && bad",
        "python -c pass || bad",
        "python -c pass; bad",
        "python -c pass | bad",
        "python -c pass > result.txt",
        "python -c pass < input.txt",
        "echo %TEMP%",
        "echo !TEMP!",
        "echo $env:TEMP",
        "echo ^& bad",
        "(echo first) & echo second",
        "python *.py",
        "python file?.py",
        "python test[12].py",
        "type ~/result.txt",
        "echo {first,second}",
        "FOO=bar python -V",
        'FOO="bar baz" python -V',
        "env FOO=~/proof python -V",
        "env PATH=/bin:~/bin python -V",
        "echo result # ignored",
        "(echo grouped)",
        "! false",
        "first\r\nsecond",
    ],
)
def test_windows_shell_syntax_is_refused(command: str) -> None:
    with pytest.raises(ValueError, match="requires shell interpretation"):
        coordinators._validate_single_process_command(command, windows=True)


@pytest.mark.parametrize("windows", [False, True])
def test_quoted_shell_characters_remain_literal_argv_content(windows: bool) -> None:
    command = (
        'python -c "print(1 > 0); '
        "print('*.py ? [a] ~ {a,b} # () ! FOO=~/proof $HOME && %TEMP%')\""
    )

    coordinators._validate_single_process_command(command, windows=windows)


@pytest.mark.parametrize("windows", [False, True])
def test_quoted_leading_assignment_is_a_literal_executable_name(windows: bool) -> None:
    coordinators._validate_single_process_command('"FOO=bar" --version', windows=windows)


@pytest.mark.parametrize("windows", [False, True])
def test_shell_characters_after_escaped_quote_remain_quoted(windows: bool) -> None:
    command = r'python -c "print(\"literal && value\")"'

    coordinators._validate_single_process_command(command, windows=windows)


@pytest.mark.parametrize(
    "command",
    [
        'python -c "pass" && python -c "pass"',
        "FOO=bar python -V",
        "python *.py",
        "env FOO=~/proof python -V",
        "echo result # ignored",
        "(echo grouped)",
        "! false",
        "echo first\\\nsecond",
        'python -c "print(1)\\\nprint(2)"',
        'python -c "print(1)',
    ],
)
def test_shell_authored_command_is_refused_before_execution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    command: str,
) -> None:
    project = engine.Project(root=tmp_path, tasks=[])

    def unexpected_run(*_args, **_kwargs) -> engine.RunResult:
        pytest.fail("shell-authored command must not be executed as inert argv")

    monkeypatch.setattr(coordinators.engine, "run_process", unexpected_run)

    with pytest.raises(
        coordinators.CoordinationInfrastructureError,
        match=r"phase=verification_command parse=failed$",
    ):
        _REAL_RECORD_VERIFICATION_PROOFS(
            project,
            "agent",
            "C1234ABCD",
            [command],
            deadline=time.monotonic() + 10,
        )


def test_acquire_bounds_claim_invocation_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task()
    results = iter([_next_result(task.id)])

    def fake_run(*_args, **_kwargs) -> engine.RunResult:
        try:
            return next(results)
        except StopIteration:
            raise OSError("PRIVATE_TOKEN") from None

    monkeypatch.setattr(coordinators.engine, "run", fake_run)

    with pytest.raises(
        coordinators.CoordinationInfrastructureError,
        match=r"phase=claim invocation=failed$",
    ) as exc_info:
        _coordinator(task).acquire("agent", object())

    assert "PRIVATE_TOKEN" not in str(exc_info.value)


@pytest.mark.parametrize(
    "claim_envelope",
    [
        {"ok": True, "command": "claim"},
        {"ok": True, "command": "claim", "data": {}},
        {
            "ok": True,
            "command": "claim",
            "data": {"claim": {"id": "C1234ABCD", "task_id": "T001"}},
        },
        {
            "ok": True,
            "command": "wrong-command",
            "data": {
                "claim": {
                    "id": "C1234ABCD",
                    "task_id": "T001",
                    "claimed_by": "agent",
                    "status": "active",
                }
            },
        },
        {
            "ok": True,
            "command": "claim",
            "data": {
                "claim": {
                    "id": "",
                    "task_id": "T001",
                    "claimed_by": "agent",
                    "status": "active",
                }
            },
        },
        {
            "ok": True,
            "command": "claim",
            "data": {
                "claim": {
                    "id": "C1234ABCD",
                    "task_id": "T999",
                    "claimed_by": "agent",
                    "status": "active",
                }
            },
        },
        {
            "ok": True,
            "command": "claim",
            "data": {
                "claim": {
                    "id": "C1234ABCD",
                    "task_id": "T001",
                    "claimed_by": "other",
                    "status": "active",
                }
            },
        },
        {
            "ok": True,
            "command": "claim",
            "data": {
                "claim": {
                    "id": "C1234ABCD",
                    "task_id": "T001",
                    "claimed_by": "agent",
                    "status": "released",
                }
            },
        },
    ],
)
def test_acquire_rejects_malformed_zero_exit_claim_success(
    monkeypatch: pytest.MonkeyPatch,
    claim_envelope: dict,
) -> None:
    task = _task()
    results = iter([
        _next_result(task.id),
        engine.RunResult(0, json.dumps(claim_envelope), "PRIVATE_TOKEN"),
    ])
    monkeypatch.setattr(
        coordinators.engine,
        "run",
        lambda *_args, **_kwargs: next(results),
    )

    with pytest.raises(
        coordinators.CoordinationInfrastructureError,
        match=r"phase=claim exit_code=0$",
    ) as exc_info:
        _coordinator(task).acquire("agent", object())

    assert "PRIVATE_TOKEN" not in str(exc_info.value)


@pytest.mark.parametrize(
    ("error_code", "message"),
    [
        (
            "conflict",
            "task 'T001' has file conflicts with active claims: "
            "claim C1234ABCD by 'a1' overlaps ['workspace/x.txt']. "
            "Pass --force to override.",
        ),
        (
            "claim_error",
            "Task 'T001' cannot be claimed: status is 'claimed', "
            "expected 'ready'.",
        ),
        (
            "claim_error",
            "claim.created: concurrency guard failed for task 'T001'. Expected "
            "status 'ready', got 'needs_review'. Another claim may have already "
            "acquired this task.",
        ),
        (
            "claim_error",
            "Task 'T001' conflicts with active claims: claim C1234ABCD by a1 "
            "(files: ['workspace/x.txt']). Use force=True to override.",
        ),
        (
            "claim_error",
            "Task 'T001' shares a conflict_group with already-claimed tasks: "
            "task T002 claimed by a1. Use force=True to override.",
        ),
        (
            "claim_error",
            "claim.created: concurrency guard failed for task 'T001'. Active claim "
            "'C1234ABCD' by 'a1' already holds this task. Another claim acquired it "
            "first.",
        ),
        (
            "claim_error",
            "claim.created: concurrency guard failed for task 'T001'. "
            "expected_files overlap active claim 'C1234ABCD' by 'a1' "
            "(files: ['workspace/x.txt']). Another claim acquired these files first; "
            "re-pick a task or use --force to override.",
        ),
        (
            "claim_error",
            "claim.created: concurrency guard failed for task 'T001'. "
            "conflict_group overlap (groups: ['shared']) with active claim "
            "'C1234ABCD' on task 'T002' by 'a1'. Another claim in this group is "
            "active; re-pick a task or use --force to override.",
        ),
    ],
)
def test_acquire_treats_real_claim_contention_as_retryable_no_work(
    monkeypatch: pytest.MonkeyPatch,
    error_code: str,
    message: str,
) -> None:
    task = _task()
    results = iter([_next_result(task.id), _claim_error(error_code, message)])
    monkeypatch.setattr(
        coordinators.engine,
        "run",
        lambda *_args, **_kwargs: next(results),
    )

    assert _coordinator(task).acquire("agent", object()) is None


@pytest.mark.parametrize(
    ("exit_code", "error_code", "message"),
    [
        (70, "conflict", "task conflicts with active claim C001"),
        (1, "conflict", ""),
        (1, "conflict", {"detail": "expected 'ready'"}),
        (1, "claim_error", {"detail": "expected 'ready'"}),
        (1, "conflict", "database corrupt"),
        (1, "claim_error", "database corrupt; expected status 'ready'"),
        (
            1,
            "claim_error",
            "Task 'T999' cannot be claimed: status is 'claimed', expected 'ready'.",
        ),
        (
            1,
            "claim_error",
            "claim.created: concurrency guard failed for task 'T999'. Expected "
            "status 'ready', got 'needs_review'. Another claim may have already "
            "acquired this task.",
        ),
        (
            1,
            "claim_error",
            "claim.created: concurrency guard failed for task 'T001'. Expected "
            "status 'ready', got 'needs_review'. Another claim may have already "
            "acquired this task. database corrupt",
        ),
        (
            1,
            "conflict",
            "task 'T001' has file conflicts with active claims: "
            "claim C1234ABCD by 'a1' overlaps []. Pass --force to override.",
        ),
        (
            1,
            "conflict",
            "task 'T001' has file conflicts with active claims: "
            "claim C1234ABCD by 'a1' overlaps [workspace/x.txt]. "
            "Pass --force to override.",
        ),
        (
            1,
            "claim_error",
            "Task 'T001' conflicts with active claims: claim C1234ABCD by a1 "
            "(files: []). Use force=True to override.",
        ),
        (
            1,
            "claim_error",
            "Task 'T001' conflicts with active claims: claim C1234ABCD by a1 "
            "(files: [workspace/x.txt]). Use force=True to override.",
        ),
        (
            1,
            "claim_error",
            "claim.created: concurrency guard failed for task 'T001'. "
            "expected_files overlap active claim 'C1234ABCD' by 'a1' (files: []). "
            "Another claim acquired these files first; re-pick a task or use --force "
            "to override.",
        ),
        (
            1,
            "claim_error",
            "claim.created: concurrency guard failed for task 'T001'. "
            "expected_files overlap active claim 'C1234ABCD' by 'a1' "
            "(files: [workspace/x.txt]). Another claim acquired these files first; "
            "re-pick a task or use --force to override.",
        ),
        (
            1,
            "claim_error",
            "claim.created: concurrency guard failed for task 'T001'. "
            "conflict_group overlap (groups: []) with active claim 'C1234ABCD' on "
            "task 'T002' by 'a1'. Another claim in this group is active; re-pick a "
            "task or use --force to override.",
        ),
        (
            1,
            "claim_error",
            "claim.created: concurrency guard failed for task 'T001'. "
            "conflict_group overlap (groups: [shared]) with active claim "
            "'C1234ABCD' on task 'T002' by 'a1'. Another claim in this group is "
            "active; re-pick a task or use --force to override.",
        ),
    ],
)
def test_acquire_rejects_malformed_contention_envelopes(
    monkeypatch: pytest.MonkeyPatch,
    exit_code: int,
    error_code: str,
    message: object,
) -> None:
    task = _task()
    result = _claim_error(error_code, "placeholder", exit_code=exit_code)
    envelope = json.loads(result.out)
    envelope["error"]["message"] = message
    results = iter([
        _next_result(task.id),
        engine.RunResult(exit_code, json.dumps(envelope), "PRIVATE_TOKEN"),
    ])
    monkeypatch.setattr(
        coordinators.engine,
        "run",
        lambda *_args, **_kwargs: next(results),
    )

    with pytest.raises(
        coordinators.CoordinationInfrastructureError,
        match=rf"phase=claim exit_code={exit_code}$",
    ) as exc_info:
        _coordinator(task).acquire("agent", object())

    assert "PRIVATE_TOKEN" not in str(exc_info.value)


@pytest.mark.parametrize(
    ("exit_code", "error_code", "message"),
    [
        (70, "backend_unavailable", "database offline"),
        (1, "claim_error", "failed to list active claims: database offline"),
    ],
)
def test_acquire_propagates_failed_claim_infrastructure(
    monkeypatch: pytest.MonkeyPatch,
    exit_code: int,
    error_code: str,
    message: str,
) -> None:
    task = _task()
    results = iter([
        _next_result(task.id),
        _claim_error(error_code, message, exit_code=exit_code),
    ])
    monkeypatch.setattr(
        coordinators.engine,
        "run",
        lambda *_args, **_kwargs: next(results),
    )

    with pytest.raises(
        coordinators.CoordinationInfrastructureError,
        match=rf"phase=claim exit_code={exit_code}$",
    ) as exc_info:
        _coordinator(task).acquire("agent", object())

    assert "database offline" not in str(exc_info.value)
