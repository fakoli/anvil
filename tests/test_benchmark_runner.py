import json
import os
import sys
import time
from pathlib import Path

import pytest
from benchmarks.harness import coordinators, engine, runner
from benchmarks.harness.scenarios import Scenario


def test_remove_trial_directory_retries_transient_permission_error(
    monkeypatch,
) -> None:
    attempts = 0

    def flaky_rmtree(path: Path) -> None:
        nonlocal attempts
        attempts += 1
        assert path == Path("trial")
        if attempts < 3:
            raise PermissionError("handle still closing")

    monkeypatch.setattr(runner.shutil, "rmtree", flaky_rmtree)

    runner._remove_trial_directory(
        Path("trial"),
        max_attempts=3,
        retry_delay_seconds=0,
    )

    assert attempts == 3


def test_configure_utf8_stdout_reconfigures_legacy_stream(monkeypatch) -> None:
    calls: list[dict[str, str]] = []

    class LegacyStream:
        def reconfigure(self, **kwargs: str) -> None:
            calls.append(kwargs)

    monkeypatch.setattr(runner.sys, "stdout", LegacyStream())

    runner._configure_utf8_stdout()

    assert calls == [{"encoding": "utf-8", "errors": "replace"}]


def _scenario() -> Scenario:
    return Scenario(
        key="fixture",
        title="Fixture",
        description="Synthetic runner test.",
        tasks=[],
        actors=1,
        headline=("final_state_valid",),
    )


def test_gamed_task_selection_uses_exact_deterministic_share() -> None:
    tasks = [engine.TaskSpec(f"T{idx:03d}", "task", ()) for idx in range(1, 9)]
    scenario = Scenario(
        key="gaming",
        title="Gaming",
        description="Synthetic gaming test.",
        tasks=tasks,
        actors=1,
        headline=("gamed_detected_pct",),
        gamed_fraction=0.5,
    )

    selected = runner._gamed_task_ids(scenario, 42)

    assert len(selected) == 4
    assert selected == runner._gamed_task_ids(scenario, 42)


def _metrics(
    *,
    completed_all: int = 1,
    completion_observations_valid: int = 1,
    final_state_valid: int = 1,
    completion_failures: int = 0,
    invalid_honest_evidence: int = 0,
    details: list[str] | None = None,
) -> dict:
    return {
        "collisions": 0,
        "duplicate_completions": 0,
        "ordering_violations": 0,
        "completed_all": completed_all,
        "completion_observations_valid": completion_observations_valid,
        "evidence_records": 1,
        "gamed_detected_pct": 0.0,
        "final_state_valid": final_state_valid,
        "completion_failures": completion_failures,
        "invalid_honest_evidence": invalid_honest_evidence,
        "_completion_failure_details": details or [],
    }


def _result(anvil_trials: list[dict], *, markdown_valid: bool = True) -> dict:
    markdown = _metrics(
        completed_all=int(markdown_valid),
        final_state_valid=int(markdown_valid),
    )
    return {
        "markdown": {"agg": markdown, "trials": [markdown]},
        "anvil": {"agg": anvil_trials[0], "trials": anvil_trials},
    }


def _patch_completed_run(
    monkeypatch: pytest.MonkeyPatch,
    result: dict,
) -> None:
    scenario = _scenario()
    monkeypatch.setattr(runner, "all_scenarios", lambda: {scenario.key: scenario})
    monkeypatch.setattr(runner, "run_scenario", lambda *_args, **_kwargs: result)
    monkeypatch.setattr(
        runner.engine,
        "run",
        lambda *_args, **_kwargs: engine.RunResult(
            0,
            "anvil 0.6.0 (schema 16)\n",
            "",
        ),
    )


def test_validate_anvil_trials_checks_each_trial_and_keeps_failure_details() -> None:
    detail = "completion_failure phase=apply task=T002 actor=a1 exit_code=9"
    results = {
        "fixture": _result([
            _metrics(),
            _metrics(
                completed_all=0,
                final_state_valid=0,
                completion_failures=1,
                details=[detail],
            ),
        ])
    }

    failures = runner.validate_anvil_trials(results)

    assert len(failures) == 1
    assert failures[0] == runner.TrialValidityFailure(
        scenario="fixture",
        trial=2,
        reasons=(
            "completed_all!=1",
            "final_state_valid!=1",
            "completion_failures>0",
        ),
        completion_details=(detail,),
    )


def test_validate_anvil_trials_rejects_invalid_honest_evidence() -> None:
    results = {
        "fixture": _result([_metrics(invalid_honest_evidence=1)]),
    }

    failures = runner.validate_anvil_trials(results)

    assert len(failures) == 1
    assert failures[0].reasons == ("invalid_honest_evidence>0",)


def test_validate_anvil_trials_rejects_missing_observation_metric() -> None:
    malformed_metrics = _metrics()
    malformed_metrics.pop("completion_observations_valid")

    failures = runner.validate_anvil_trials({
        "fixture": _result([malformed_metrics]),
    })

    assert len(failures) == 1
    assert failures[0].reasons == ("completion_observations_valid!=1",)


@pytest.mark.parametrize(
    "metric",
    ["completion_failures", "invalid_honest_evidence"],
)
def test_validate_anvil_trials_rejects_missing_safety_counter(metric: str) -> None:
    malformed_metrics = _metrics()
    malformed_metrics.pop(metric)

    failures = runner.validate_anvil_trials({
        "fixture": _result([malformed_metrics]),
    })

    assert len(failures) == 1
    assert failures[0].reasons == (f"{metric}=invalid",)


@pytest.mark.parametrize(
    ("metric", "invalid_value"),
    [
        (metric, invalid_value)
        for metric in ("completion_failures", "invalid_honest_evidence")
        for invalid_value in (None, False, 0.0, "0", -1)
    ],
)
def test_validate_anvil_trials_rejects_invalid_safety_counter(
    metric: str,
    invalid_value: object,
) -> None:
    malformed_metrics = _metrics()
    malformed_metrics[metric] = invalid_value

    failures = runner.validate_anvil_trials({
        "fixture": _result([malformed_metrics]),
    })

    assert len(failures) == 1
    assert failures[0].reasons == (f"{metric}=invalid",)


def test_completion_failure_propagates_from_metrics_into_trial_gate() -> None:
    failure = coordinators.CompletionFailure(
        phase="submit",
        task="T001",
        actor="a0",
        exit_code=11,
    )
    metrics = runner.M.compute(
        _scenario(),
        rows=[],
        completions=[{
            "task": "T001",
            "actor": "a0",
            "gamed": False,
            "completed": False,
            "evidence_valid": None,
            "failure": failure,
        }],
        statuses={},
        coord_name="anvil",
    )

    failures = runner.validate_anvil_trials({
        "fixture": _result([metrics]),
    })

    assert metrics["completion_failures"] == 1
    assert metrics["_completion_failure_details"] == [failure.diagnostic]
    assert failures[0].reasons == ("completion_failures>0",)


def test_run_trial_returns_observed_completion_failure_without_polling(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    task = engine.TaskSpec("T001", "submit failure", ("workspace/x.txt",))
    scenario = Scenario(
        key="submit_failure",
        title="Submit failure",
        description="A terminal observed command failure invalidates the trial.",
        tasks=[task],
        actors=1,
        headline=("final_state_valid",),
    )
    project = engine.Project(root=tmp_path, tasks=[task])
    monkeypatch.setattr(runner, "setup_project", lambda *_args, **_kwargs: project)
    monkeypatch.setattr(
        runner.engine,
        "task_status",
        lambda *_args, **_kwargs: {task.id: "claimed"},
    )
    monkeypatch.setattr(
        coordinators,
        "_record_verification_proofs",
        lambda *_args, **_kwargs: None,
    )
    results = iter([
        engine.RunResult(
            0,
            json.dumps({
                "ok": True,
                "command": "next",
                "data": {"task": {"id": task.id}},
            }),
            "",
        ),
        engine.RunResult(
            0,
            json.dumps({
                "ok": True,
                "command": "claim",
                "data": {
                    "claim": {
                        "id": "C1234ABCD",
                        "task_id": task.id,
                        "claimed_by": "a0",
                        "status": "active",
                    },
                },
            }),
            "",
        ),
        engine.RunResult(
            7,
            json.dumps({
                "ok": False,
                "command": "submit",
                "error": {"code": "submit_failed", "message": "refused"},
            }),
            "",
        ),
    ])
    calls = 0

    def run_once(*_args: object, **_kwargs: object) -> engine.RunResult:
        nonlocal calls
        calls += 1
        return next(results)

    monkeypatch.setattr(runner.engine, "run", run_once)

    metrics = runner.run_trial(scenario, "anvil", 1, 0, tmp_path, 0, 5)

    assert calls == 3
    assert metrics["completion_failures"] == 1
    assert metrics["completed_all"] == 0
    assert metrics["final_state_valid"] == 0
    assert metrics["_completion_failure_details"] == [
        "completion_failure phase=submit task=T001 actor=a0 exit_code=7"
    ]


@pytest.mark.parametrize(
    ("evidence_valid", "expected_pct"),
    [(False, 100.0), (True, 0.0)],
)
def test_gamed_detection_uses_evidence_gate_result(
    evidence_valid: bool,
    expected_pct: float,
) -> None:
    metrics = runner.M.compute(
        _scenario(),
        rows=[],
        completions=[{
            "task": "T001",
            "actor": "a0",
            "gamed": True,
            "completed": True,
            "evidence_valid": evidence_valid,
            "failure": None,
        }],
        statuses={},
        coord_name="anvil",
    )

    assert metrics["gamed_detected_pct"] == expected_pct


def test_final_state_requires_exact_expected_task_ids() -> None:
    scenario = Scenario(
        key="exact_state",
        title="Exact state",
        description="Reject substituted or extra canonical task rows.",
        tasks=[
            engine.TaskSpec("T001", "one", ()),
            engine.TaskSpec("T002", "two", ()),
        ],
        actors=1,
        headline=("final_state_valid",),
    )

    metrics = runner.M.compute(
        scenario,
        rows=[],
        completions=[],
        statuses={"T001": "done", "ROGUE": "done"},
        coord_name="anvil",
    )

    assert metrics["completed_all"] == 0
    assert metrics["final_state_valid"] == 0


def test_final_state_requires_observed_completion_evidence_for_every_task() -> None:
    scenario = Scenario(
        key="completion_coverage",
        title="Completion coverage",
        description="Reject canonical done state without observed completion evidence.",
        tasks=[
            engine.TaskSpec("T001", "one", ()),
            engine.TaskSpec("T002", "two", ()),
        ],
        actors=1,
        headline=("final_state_valid",),
    )
    completions = [{
        "task": "T001",
        "actor": "a0",
        "gamed": False,
        "completed": True,
        "evidence_valid": True,
        "failure": None,
    }]

    metrics = runner.M.compute(
        scenario,
        rows=[],
        completions=completions,
        statuses={"T001": "done", "T002": "done"},
        coord_name="anvil",
    )

    assert metrics["completion_observations_valid"] == 0
    assert metrics["completed_all"] == 0
    assert metrics["final_state_valid"] == 0


def test_completion_observation_coverage_accepts_persisted_gamed_evidence() -> None:
    scenario = Scenario(
        key="completion_coverage",
        title="Completion coverage",
        description="Every expected task has an observed persisted completion.",
        tasks=[
            engine.TaskSpec("T001", "honest", ()),
            engine.TaskSpec("T002", "gamed", ()),
        ],
        actors=1,
        headline=("final_state_valid",),
    )
    completions = [
        {
            "task": "T001",
            "actor": "a0",
            "gamed": False,
            "completed": True,
            "evidence_valid": True,
            "failure": None,
        },
        {
            "task": "T002",
            "actor": "a0",
            "gamed": True,
            "completed": True,
            "evidence_valid": False,
            "failure": None,
        },
    ]

    metrics = runner.M.compute(
        scenario,
        rows=[],
        completions=completions,
        statuses={"T001": "done", "T002": "done"},
        coord_name="anvil",
    )

    assert metrics["completion_observations_valid"] == 1
    assert metrics["completed_all"] == 1
    assert metrics["final_state_valid"] == 1


def test_run_trial_rejects_done_state_without_observed_completion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    task = engine.TaskSpec("T001", "state drift", ("workspace/x.txt",))
    scenario = Scenario(
        key="state_drift",
        title="State drift",
        description="Canonical state changed without a benchmark completion.",
        tasks=[task],
        actors=1,
        headline=("final_state_valid",),
    )
    project = engine.Project(root=tmp_path, tasks=[task])
    monkeypatch.setattr(runner, "setup_project", lambda *_args, **_kwargs: project)
    monkeypatch.setattr(
        runner.engine,
        "task_status",
        lambda *_args, **_kwargs: {"T001": "done"},
    )

    metrics = runner.run_trial(scenario, "anvil", 1, 0, tmp_path, 0, 1)
    failures = runner.validate_anvil_trials({
        scenario.key: {
            "markdown": {"agg": metrics, "trials": [metrics]},
            "anvil": {"agg": metrics, "trials": [metrics]},
        },
    })

    assert metrics["evidence_records"] == 0
    assert metrics["completion_observations_valid"] == 0
    assert len(failures) == 1
    assert failures[0].reasons == (
        "completed_all!=1",
        "final_state_valid!=1",
        "completion_observations_valid!=1",
    )


def test_invalid_anvil_result_writes_report_then_exits_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    detail = "completion_failure phase=submit task=T001 actor=a0 exit_code=7"
    _patch_completed_run(
        monkeypatch,
        _result([
            _metrics(
                completed_all=0,
                final_state_valid=0,
                completion_failures=1,
                details=[detail],
            )
        ]),
    )
    out = tmp_path / "invalid.md"

    exit_code = runner.main(["--scenarios", "fixture", "--out", str(out)])

    assert exit_code == 1
    report = out.read_text(encoding="utf-8")
    assert "**Result validity:** INVALID" in report
    assert detail in report


def test_invalid_honest_evidence_writes_invalid_report_then_exits_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_completed_run(
        monkeypatch,
        _result([_metrics(invalid_honest_evidence=1)]),
    )
    out = tmp_path / "invalid-evidence.md"

    exit_code = runner.main(["--scenarios", "fixture", "--out", str(out)])

    assert exit_code == 1
    report = out.read_text(encoding="utf-8")
    assert "**Result validity:** INVALID" in report
    assert "invalid_honest_evidence>0" in report


def test_allow_invalid_results_is_report_only_opt_in(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_completed_run(
        monkeypatch,
        _result([_metrics(completed_all=0, final_state_valid=0)]),
    )
    out = tmp_path / "report-only.md"

    exit_code = runner.main([
        "--scenarios", "fixture",
        "--out", str(out),
        "--allow-invalid-results",
    ])

    assert exit_code == 0
    assert "**Result validity:** INVALID" in out.read_text(encoding="utf-8")


def test_allow_invalid_results_overrides_observed_completion_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    detail = "completion_failure phase=submit task=T001 actor=a0 exit_code=7"
    _patch_completed_run(
        monkeypatch,
        _result([
            _metrics(
                completed_all=0,
                final_state_valid=0,
                completion_failures=1,
                details=[detail],
            )
        ]),
    )
    out = tmp_path / "report-only-completion-failure.md"

    exit_code = runner.main([
        "--scenarios", "fixture",
        "--out", str(out),
        "--allow-invalid-results",
    ])

    assert exit_code == 0
    report = out.read_text(encoding="utf-8")
    assert "**Result validity:** INVALID" in report
    assert detail in report


def test_markdown_control_invalidity_does_not_fail_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_completed_run(monkeypatch, _result([_metrics()], markdown_valid=False))
    out = tmp_path / "valid.md"

    exit_code = runner.main(["--scenarios", "fixture", "--out", str(out)])

    assert exit_code == 0
    assert "**Result validity:** VALID" in out.read_text(encoding="utf-8")


def test_allow_invalid_results_does_not_mask_unexpected_runtime_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    scenario = _scenario()
    monkeypatch.setattr(runner, "all_scenarios", lambda: {scenario.key: scenario})
    monkeypatch.setattr(
        runner,
        "run_scenario",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr(
        runner.engine,
        "run",
        lambda *_args, **_kwargs: engine.RunResult(
            0,
            "anvil 0.6.0 (schema 16)\n",
            "",
        ),
    )
    out = tmp_path / "not-written.md"

    with pytest.raises(RuntimeError, match="boom"):
        runner.main([
            "--scenarios", "fixture",
            "--out", str(out),
            "--allow-invalid-results",
        ])

    assert not out.exists()


def test_allow_invalid_results_does_not_mask_scenario_argument_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "all_scenarios", lambda: {})

    assert runner.main([
        "--scenarios", "missing",
        "--allow-invalid-results",
    ]) == 2


@pytest.mark.parametrize("trial_count", ["0", "-1"])
def test_nonpositive_trial_count_is_argument_error_even_in_report_only_mode(
    trial_count: str,
    tmp_path: Path,
) -> None:
    out = tmp_path / "must-not-exist.md"

    with pytest.raises(SystemExit) as exc_info:
        runner.main([
            "--trials", trial_count,
            "--out", str(out),
            "--allow-invalid-results",
        ])

    assert exc_info.value.code == 2
    assert not out.exists()


@pytest.mark.parametrize("deadline", ["0", "-1", "nan", "inf", "-inf"])
def test_nonpositive_deadline_is_argument_error_even_in_report_only_mode(
    deadline: str,
    tmp_path: Path,
) -> None:
    out = tmp_path / "must-not-exist.md"

    with pytest.raises(SystemExit) as exc_info:
        runner.main([
            "--deadline", deadline,
            "--out", str(out),
            "--allow-invalid-results",
        ])

    assert exc_info.value.code == 2
    assert not out.exists()


@pytest.mark.parametrize("jitter", ["-1", "nan", "inf", "-inf"])
def test_invalid_jitter_is_argument_error(jitter: str, tmp_path: Path) -> None:
    out = tmp_path / "must-not-exist.md"

    with pytest.raises(SystemExit) as exc_info:
        runner.main(["--jitter", jitter, "--out", str(out)])

    assert exc_info.value.code == 2
    assert not out.exists()


@pytest.mark.parametrize(
    "version_result",
    [
        engine.RunResult(70, "anvil 0.6.0 (schema 16)\n", "PRIVATE_TOKEN"),
        engine.RunResult(0, "not-an-anvil-version\n", ""),
        engine.RunResult(
            0,
            "anvil 0.6.0 (schema 16)\n## Summary\nPRIVATE_TOKEN\n",
            "",
        ),
        engine.RunResult(0, "x" * 257, ""),
        engine.RunResult(0, None, ""),
        engine.RunResult(0, "anvil 0.6.0 (schema 16)\n", None),
    ],
)
def test_version_failures_are_bounded_and_never_masked_by_report_only_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    version_result: engine.RunResult,
) -> None:
    scenario = _scenario()
    phases: list[str] = []
    monkeypatch.setattr(runner, "all_scenarios", lambda: {scenario.key: scenario})

    def version_probe(*_args, **_kwargs) -> engine.RunResult:
        phases.append("version")
        return version_result

    monkeypatch.setattr(runner.engine, "run", version_probe)
    monkeypatch.setattr(
        runner,
        "run_scenario",
        lambda *_args, **_kwargs: (
            phases.append("trial") or _result([_metrics()])
        ),
    )
    out = tmp_path / "must-not-exist.md"

    assert runner.main([
        "--scenarios", "fixture",
        "--out", str(out),
        "--allow-invalid-results",
    ]) == 2

    captured = capsys.readouterr()
    assert "phase=version" in captured.err
    assert "PRIVATE_TOKEN" not in captured.out + captured.err
    assert "## Summary" not in captured.out + captured.err
    assert not out.exists()
    assert not runner._report_lock_path(out).exists()
    assert phases == ["trial", "version"]


def test_version_probe_requests_a_hard_output_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_run(*args, **kwargs) -> engine.RunResult:
        observed["args"] = args
        observed["kwargs"] = kwargs
        return engine.RunResult(0, "anvil 0.6.0 (schema 16)\n", "")

    monkeypatch.setattr(runner.engine, "run", fake_run)

    assert runner._engine_version() == (
        "anvil 0.6.0 (schema 16)",
        "0.6.0 (schema 16)",
    )
    assert observed["kwargs"] == {
        "timeout": runner._VERSION_PROBE_TIMEOUT_SECONDS,
        "output_limit_bytes": runner._VERSION_OUTPUT_LIMIT,
    }


def test_setup_failure_does_not_disclose_raw_cli_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        engine,
        "run",
        lambda *_args, **_kwargs: engine.RunResult(
            70,
            "PRIVATE_TOKEN",
            "raw traceback",
        ),
    )

    with pytest.raises(
        RuntimeError,
        match=r"phase=init exit_code=70$",
    ) as exc_info:
        engine.setup_project(tmp_path / "project", "fixture", [])

    assert "PRIVATE_TOKEN" not in str(exc_info.value)
    assert "raw traceback" not in str(exc_info.value)


def test_trial_setup_failure_is_typed_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        runner,
        "setup_project",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("PRIVATE_TOKEN raw traceback")
        ),
    )

    with pytest.raises(
        coordinators.CoordinationInfrastructureError,
        match=r"phase=setup invocation=failed$",
    ) as exc_info:
        runner.run_trial(
            _scenario(),
            "anvil",
            42,
            0,
            tmp_path,
            0,
            1,
        )

    assert "PRIVATE_TOKEN" not in str(exc_info.value)
    assert "raw traceback" not in str(exc_info.value)


def test_trial_setup_deadline_is_typed_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        runner,
        "setup_project",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            TimeoutError("PRIVATE_TOKEN")
        ),
    )

    with pytest.raises(
        coordinators.CoordinationInfrastructureError,
        match=r"phase=setup deadline=exceeded$",
    ) as exc_info:
        runner.run_trial(
            _scenario(),
            "anvil",
            42,
            0,
            tmp_path,
            0,
            0.01,
        )

    assert "PRIVATE_TOKEN" not in str(exc_info.value)


def test_worker_crossing_deadline_fails_instead_of_returning_partial_metrics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    task = engine.TaskSpec("T001", "deadline probe", ("workspace/x.txt",))
    scenario = Scenario(
        key="deadline_probe",
        title="Deadline probe",
        description="Synthetic deadline test.",
        tasks=[task],
        actors=1,
        headline=("completed_all",),
    )
    root = tmp_path / "project"
    root.mkdir()
    project = engine.Project(root=root, tasks=[task])
    monkeypatch.setattr(runner, "setup_project", lambda *_args, **_kwargs: project)
    original_acquire = coordinators.MarkdownCoordinator.acquire

    def delayed_acquire(*args: object, **kwargs: object):
        time.sleep(0.06)
        return original_acquire(*args, **kwargs)

    monkeypatch.setattr(
        coordinators.MarkdownCoordinator,
        "acquire",
        delayed_acquire,
    )

    with pytest.raises(
        coordinators.CoordinationInfrastructureError,
        match=r"phase=trial deadline=exceeded$",
    ):
        runner.run_trial(scenario, "markdown", 1, 0, root, 0, 0.05)


def test_worker_finishing_just_after_deadline_cannot_return_metrics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    task = engine.TaskSpec("T001", "deadline exit probe", ("workspace/x.txt",))
    scenario = Scenario(
        key="deadline_exit_probe",
        title="Deadline exit probe",
        description="Synthetic just-finished deadline test.",
        tasks=[task],
        actors=1,
        headline=("completed_all",),
    )
    project = engine.Project(root=tmp_path, tasks=[task])
    clock = [100.0]

    class JustLateCoordinator:
        def finished(self, timeout: float | None = None) -> bool:
            assert timeout is not None and timeout > 0
            clock[0] = 101.001
            return True

    monkeypatch.setattr(runner, "setup_project", lambda *_args, **_kwargs: project)
    monkeypatch.setattr(runner, "_coord", lambda *_args, **_kwargs: JustLateCoordinator())
    monkeypatch.setattr(runner.time, "monotonic", lambda: clock[0])

    with pytest.raises(
        coordinators.CoordinationInfrastructureError,
        match=r"phase=trial deadline=exceeded$",
    ):
        runner.run_trial(scenario, "markdown", 1, 0, tmp_path, 0, 1.0)


def test_worker_finishing_before_deadline_survives_late_parent_resume(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    scenario = Scenario(
        key="deadline_parent_probe",
        title="Deadline parent probe",
        description="Synthetic on-time worker test.",
        tasks=[],
        actors=1,
        headline=("completed_all",),
    )
    project = engine.Project(root=tmp_path, tasks=[])
    clock = [100.0]

    class OnTimeCoordinator:
        def finished(self, timeout: float | None = None) -> bool:
            assert timeout is not None and timeout > 0
            clock[0] = 100.5
            return True

    original_join = runner.threading.Thread.join

    def resume_parent_after_deadline(
        thread: runner.threading.Thread,
        timeout: float | None = None,
    ) -> None:
        original_join(thread, timeout)
        clock[0] = 101.001

    monkeypatch.setattr(runner, "setup_project", lambda *_args, **_kwargs: project)
    monkeypatch.setattr(runner, "_coord", lambda *_args, **_kwargs: OnTimeCoordinator())
    monkeypatch.setattr(runner.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(runner.threading.Thread, "join", resume_parent_after_deadline)

    metrics = runner.run_trial(scenario, "markdown", 1, 0, tmp_path, 0, 1.0)

    assert metrics["completed_all"] == 1
    assert metrics["final_state_valid"] == 1


def test_worker_crossing_deadline_in_exit_bookkeeping_cannot_return_metrics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    scenario = Scenario(
        key="deadline_bookkeeping_probe",
        title="Deadline bookkeeping probe",
        description="Synthetic worker-exit bookkeeping test.",
        tasks=[],
        actors=1,
        headline=("completed_all",),
    )
    project = engine.Project(root=tmp_path, tasks=[])
    clock = [100.0]

    class OnTimeCoordinator:
        def finished(self, timeout: float | None = None) -> bool:
            assert timeout is not None and timeout > 0
            clock[0] = 100.5
            return True

    original_lock = runner.threading.Lock
    lock_calls = 0

    class DeadlineCrossingLock:
        def __init__(self) -> None:
            self._lock = original_lock()

        def __enter__(self):
            self._lock.acquire()
            clock[0] = 101.001
            return self

        def __exit__(self, *_args: object) -> None:
            self._lock.release()

    def lock_factory():
        nonlocal lock_calls
        lock_calls += 1
        if lock_calls == 1:
            return DeadlineCrossingLock()
        return original_lock()

    monkeypatch.setattr(runner, "setup_project", lambda *_args, **_kwargs: project)
    monkeypatch.setattr(runner, "_coord", lambda *_args, **_kwargs: OnTimeCoordinator())
    monkeypatch.setattr(runner.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(runner.threading, "Lock", lock_factory)

    with pytest.raises(
        coordinators.CoordinationInfrastructureError,
        match=r"phase=trial deadline=exceeded$",
    ):
        runner.run_trial(scenario, "markdown", 1, 0, tmp_path, 0, 1.0)


def test_trial_uses_one_shared_worker_cleanup_allowance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    task = engine.TaskSpec("T001", "cleanup probe", ("workspace/x.txt",))
    scenario = Scenario(
        key="cleanup_probe",
        title="Cleanup probe",
        description="Synthetic cleanup test.",
        tasks=[task],
        actors=8,
        headline=("completed_all",),
    )
    project = engine.Project(root=tmp_path, tasks=[task])
    monkeypatch.setattr(runner, "setup_project", lambda *_args, **_kwargs: project)
    monkeypatch.setattr(runner, "TRIAL_CLEANUP_ALLOWANCE_SECONDS", 0.05)
    release_workers = runner.threading.Event()
    entered_workers: set[int] = set()
    entered_lock = runner.threading.Lock()

    def stalled_acquire(*_args: object, **_kwargs: object):
        with entered_lock:
            entered_workers.add(runner.threading.get_ident())
        release_workers.wait(2)
        return None

    monkeypatch.setattr(
        coordinators.MarkdownCoordinator,
        "acquire",
        stalled_acquire,
    )

    started = time.monotonic()
    try:
        with pytest.raises(
            coordinators.CoordinationInfrastructureError,
            match=r"phase=trial deadline=exceeded$",
        ):
            runner.run_trial(scenario, "markdown", 1, 0, tmp_path, 0, 0.5)
        elapsed = time.monotonic() - started
    finally:
        release_workers.set()

    assert len(entered_workers) == scenario.actors
    assert 0.4 < elapsed < 0.75


def test_worker_start_failure_releases_gate_and_joins_started_workers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    task = engine.TaskSpec("T001", "startup probe", ("workspace/x.txt",))
    scenario = Scenario(
        key="startup_probe",
        title="Startup probe",
        description="Synthetic worker-start test.",
        tasks=[task],
        actors=2,
        headline=("completed_all",),
    )
    project = engine.Project(root=tmp_path, tasks=[task])
    coordinator_calls: list[str] = []
    started_threads: list[runner.threading.Thread] = []
    original_start = runner.threading.Thread.start
    start_calls = 0

    class ProbeCoordinator:
        def finished(self, timeout: float | None = None) -> bool:
            del timeout
            coordinator_calls.append("finished")
            return True

    monkeypatch.setattr(runner, "setup_project", lambda *_args, **_kwargs: project)
    monkeypatch.setattr(runner, "_coord", lambda *_args, **_kwargs: ProbeCoordinator())

    def fail_second_start(thread: runner.threading.Thread) -> None:
        nonlocal start_calls
        start_calls += 1
        if start_calls == 2:
            raise RuntimeError("thread unavailable")
        original_start(thread)
        started_threads.append(thread)

    monkeypatch.setattr(runner.threading.Thread, "start", fail_second_start)

    with pytest.raises(RuntimeError, match="thread unavailable"):
        runner.run_trial(scenario, "markdown", 1, 0, tmp_path, 0, 1)

    assert started_threads
    assert all(not thread.is_alive() for thread in started_threads)
    assert coordinator_calls == []


def test_trial_bounds_finished_and_final_status_reads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    scenario = _scenario()
    project = engine.Project(root=tmp_path, tasks=[])
    observed_timeouts: list[float] = []
    monkeypatch.setattr(runner, "setup_project", lambda *_args, **_kwargs: project)

    def bounded_status(_project: engine.Project, *, timeout: float) -> dict[str, str]:
        observed_timeouts.append(timeout)
        return {}

    monkeypatch.setattr(runner.engine, "task_status", bounded_status)

    metrics = runner.run_trial(scenario, "anvil", 1, 0, tmp_path, 0, 0.2)

    assert metrics["final_state_valid"] == 1
    assert len(observed_timeouts) == 2
    assert all(0 < timeout <= 0.201 for timeout in observed_timeouts)


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_engine_run_kills_children_that_exceed_output_bound(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stream: str,
) -> None:
    monkeypatch.setattr(engine, "anvil_binary", lambda _timeout=None: sys.executable)
    script = f"import sys; sys.{stream}.write('PRIVATE_TOKEN' * 10000)"

    result = engine.run(
        ["-c", script],
        tmp_path,
        output_limit_bytes=64,
    )

    assert result == engine.RunResult(125, "", "output limit exceeded")
    assert "PRIVATE_TOKEN" not in result.out + result.err


def test_engine_timeout_kills_descendants_holding_capture_pipes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(engine, "anvil_binary", lambda _timeout=None: sys.executable)
    script = (
        "import subprocess,sys; "
        "subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])"
    )

    started = time.monotonic()
    result = engine.run(["-c", script], tmp_path, timeout=0.2)

    assert result == engine.RunResult(124, "", "timeout")
    assert time.monotonic() - started < 10


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group cleanup")
def test_engine_success_kills_background_descendants_that_close_capture_pipes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    marker = tmp_path / "background-child-ran"
    monkeypatch.setattr(engine, "anvil_binary", lambda _timeout=None: sys.executable)
    child = (
        "import time; from pathlib import Path; "
        f"time.sleep(0.4); Path({str(marker)!r}).touch()"
    )
    script = (
        "import subprocess,sys; "
        "subprocess.Popen([sys.executable,'-c',"
        f"{child!r}],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)"
    )

    result = engine.run(["-c", script], tmp_path, timeout=1)
    time.sleep(0.6)

    assert result.code == 0
    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object containment")
def test_engine_refuses_to_start_when_windows_containment_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    marker = tmp_path / "child-ran"
    monkeypatch.setattr(engine, "anvil_binary", lambda _timeout=None: sys.executable)
    monkeypatch.setattr(engine, "_windows_kill_on_close_job", lambda _proc: None)

    result = engine.run(
        ["-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"],
        tmp_path,
    )

    assert result == engine.RunResult(
        126, "", "process containment unavailable"
    )
    assert not marker.exists()


def test_version_invocation_exception_is_bounded_and_not_masked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scenario = _scenario()
    monkeypatch.setattr(runner, "all_scenarios", lambda: {scenario.key: scenario})
    monkeypatch.setattr(
        runner,
        "run_scenario",
        lambda *_args, **_kwargs: _result([_metrics()]),
    )
    monkeypatch.setattr(
        runner.engine,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("PRIVATE_TOKEN")),
    )
    out = tmp_path / "must-not-exist.md"

    assert runner.main([
        "--scenarios", "fixture",
        "--out", str(out),
        "--allow-invalid-results",
    ]) == 2

    captured = capsys.readouterr()
    assert "phase=version invocation=failed" in captured.err
    assert "PRIVATE_TOKEN" not in captured.out + captured.err
    assert not out.exists()


@pytest.mark.parametrize(
    "selection",
    ["fixture,typo", "typo,fixture", "fixture,fixture", "fixture,"],
)
def test_scenario_selection_rejects_any_unknown_empty_or_duplicate_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    selection: str,
) -> None:
    scenario = _scenario()
    monkeypatch.setattr(runner, "all_scenarios", lambda: {scenario.key: scenario})
    monkeypatch.setattr(
        runner.engine,
        "run",
        lambda *_args, **_kwargs: pytest.fail("version must not run"),
    )
    out = tmp_path / "must-not-exist.md"

    assert runner.main([
        "--scenarios", selection,
        "--out", str(out),
        "--allow-invalid-results",
    ]) == 2

    assert not out.exists()


def test_concurrent_report_publication_refuses_without_overwrite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_completed_run(monkeypatch, _result([_metrics()]))
    out = tmp_path / "shared.md"
    out.write_text("first invocation owns this artifact\n", encoding="utf-8")

    with runner._exclusive_report_publication(out):
        assert runner.main([
            "--scenarios", "fixture",
            "--out", str(out),
            "--allow-invalid-results",
        ]) == 2

    captured = capsys.readouterr()
    assert "already reserved" in captured.err
    assert out.read_text(encoding="utf-8") == "first invocation owns this artifact\n"
    assert not runner._report_lock_path(out).exists()


def test_stale_unlocked_report_lock_does_not_wedge_publication(
    tmp_path: Path,
) -> None:
    out = tmp_path / "report.md"
    lock_path = runner._report_lock_path(out)
    lock_path.write_text("stale abnormal-termination residue", encoding="utf-8")

    with runner._exclusive_report_publication(out):
        assert lock_path.exists()

    assert not lock_path.exists()


def test_quick_report_uses_effective_actor_count(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    scenario = Scenario(
        key="fixture",
        title="Fixture",
        description="Synthetic runner test.",
        tasks=[],
        actors=8,
        headline=("final_state_valid",),
    )
    monkeypatch.setattr(runner, "all_scenarios", lambda: {scenario.key: scenario})
    observed_deadlines: list[float] = []

    def completed_scenario(
        _scenario: Scenario,
        _trials: int,
        _seed: int,
        _jitter: float,
        deadline: float,
    ) -> dict:
        observed_deadlines.append(deadline)
        return _result([_metrics()])

    monkeypatch.setattr(runner, "run_scenario", completed_scenario)
    monkeypatch.setattr(
        runner.engine,
        "run",
        lambda *_args, **_kwargs: engine.RunResult(
            0, "anvil 0.6.0 (schema 16)\n", ""
        ),
    )
    out = tmp_path / "quick.md"

    assert runner.main([
        "--scenarios", "fixture", "--quick", "--out", str(out)
    ]) == 0
    assert "*4 actors ·" in out.read_text(encoding="utf-8")
    assert observed_deadlines == [runner._DEFAULT_TRIAL_DEADLINE_SECONDS]


@pytest.mark.parametrize(
    ("mode_args", "expected_deadline"),
    [
        ([], 120.0),
        (["--deadline", "17.5"], 17.5),
    ],
)
def test_normal_report_uses_operable_or_explicit_deadline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode_args: list[str],
    expected_deadline: float,
) -> None:
    scenario = _scenario()
    monkeypatch.setattr(runner, "all_scenarios", lambda: {scenario.key: scenario})
    observed_deadlines: list[float] = []

    def completed_scenario(
        _scenario: Scenario,
        _trials: int,
        _seed: int,
        _jitter: float,
        deadline: float,
    ) -> dict:
        observed_deadlines.append(deadline)
        return _result([_metrics()])

    monkeypatch.setattr(runner, "run_scenario", completed_scenario)
    monkeypatch.setattr(
        runner.engine,
        "run",
        lambda *_args, **_kwargs: engine.RunResult(
            0, "anvil 0.6.0 (schema 16)\n", ""
        ),
    )

    assert runner.main([
        "--scenarios",
        "fixture",
        "--out",
        str(tmp_path / "normal.md"),
        *mode_args,
    ]) == 0
    assert observed_deadlines == [expected_deadline]
    if not mode_args:
        assert expected_deadline == runner._DEFAULT_TRIAL_DEADLINE_SECONDS


def test_atomic_report_publication_replaces_existing_complete_artifact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_completed_run(monkeypatch, _result([_metrics()]))
    out = tmp_path / "report.md"
    out.write_text("old complete report\n", encoding="utf-8")

    assert runner.main(["--scenarios", "fixture", "--out", str(out)]) == 0

    assert out.read_text(encoding="utf-8").startswith(
        "# anvil coordination benchmark — results"
    )
    assert not runner._report_lock_path(out).exists()
    assert not list(tmp_path.glob(".report.md.*.tmp"))


def _crash_claim_result(task_id: str = "T001", actor: str = "dead") -> engine.RunResult:
    return engine.RunResult(
        0,
        json.dumps({
            "ok": True,
            "command": "claim",
            "data": {
                "claim": {
                    "id": "C1234ABCD",
                    "task_id": task_id,
                    "claimed_by": actor,
                    "status": "active",
                }
            },
        }),
        "",
    )


@pytest.mark.parametrize("failure", ["claim", "expiry_count", "postcondition"])
def test_crash_seed_failures_are_typed_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str,
) -> None:
    proj = engine.Project(root=tmp_path, tasks=[])
    result = (
        engine.RunResult(70, "PRIVATE_TOKEN", "raw traceback")
        if failure == "claim"
        else _crash_claim_result()
    )
    monkeypatch.setattr(runner.engine, "run", lambda *_args, **_kwargs: result)
    monkeypatch.setattr(
        runner.engine,
        "expire_claims_for",
        lambda *_args, **_kwargs: 0 if failure == "expiry_count" else 1,
    )
    monkeypatch.setattr(
        runner.engine,
        "claim_is_expired",
        lambda *_args, **_kwargs: failure != "postcondition",
    )

    with pytest.raises(coordinators.CoordinationInfrastructureError) as exc_info:
        runner._seed_crash_claim(
            proj,
            "T001",
            deadline=time.monotonic() + 1,
        )

    message = str(exc_info.value)
    assert "PRIVATE_TOKEN" not in message
    assert "raw traceback" not in message
    assert "phase=crash_" in message


def test_allow_invalid_results_does_not_mask_trial_infrastructure_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    scenario = _scenario()
    monkeypatch.setattr(runner, "all_scenarios", lambda: {scenario.key: scenario})
    monkeypatch.setattr(
        runner.engine,
        "run",
        lambda *_args, **_kwargs: engine.RunResult(
            0,
            "anvil 0.6.0 (schema 16)\n",
            "",
        ),
    )
    monkeypatch.setattr(
        runner,
        "run_scenario",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            coordinators.CoordinationInfrastructureError(
                "benchmark coordination infrastructure failure: phase=crash_expire"
            )
        ),
    )
    out = tmp_path / "must-not-exist.md"

    assert runner.main([
        "--scenarios", "fixture",
        "--out", str(out),
        "--allow-invalid-results",
    ]) == 2

    assert not out.exists()
    assert not runner._report_lock_path(out).exists()


@pytest.mark.parametrize("invalid", [False, True])
def test_stdout_preserves_complete_summary_table_for_valid_and_invalid_reports(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    invalid: bool,
) -> None:
    metrics = (
        _metrics(completed_all=0, final_state_valid=0)
        if invalid else _metrics()
    )
    _patch_completed_run(monkeypatch, _result([metrics]))
    args = ["--scenarios", "fixture", "--out", str(tmp_path / "report.md")]
    if invalid:
        args.append("--allow-invalid-results")

    assert runner.main(args) == 0

    stdout = capsys.readouterr().out
    assert "|---|---|---:|---:|" in stdout
    assert "| Fixture | final state valid (1=yes) |" in stdout


def test_linux_ci_smoke_uses_real_benchmark_without_report_only_override() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    workflow = (repo_root / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    command = (
        "uv run python ../benchmarks/run_benchmark.py "
        "--scenarios overlapping_files --quick"
    )
    benchmark_run_lines = [
        line.strip()
        for line in workflow.splitlines()
        if line.strip().startswith("run:") and "run_benchmark.py" in line
    ]

    assert benchmark_run_lines == [f"run: {command}"]
