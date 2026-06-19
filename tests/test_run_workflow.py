"""T003 — sequential `anvil run-workflow` runner.

Engine-level tests drive the runner against a real backend (one Evidence row
per applied step; needs-ordering; on_fail:reopen). CLI tests cover `--help`, the
missing-file path, and a full end-to-end run via the Typer app.
"""

from __future__ import annotations

import os
from pathlib import Path

from typer.testing import CliRunner

from anvil.cli import app
from anvil.state.models import TaskStatus
from anvil.workflows.parse import parse_workflow
from anvil.workflows.runner import StepOutcome, WorkflowRunError, run_workflow
from anvil.workflows.tasks import is_workflow_task

runner = CliRunner()

_SEQ = """
name: seq
steps:
  - id: a
    run: "do a"
  - id: b
    needs: [a]
    run: "do b"
"""


def _all_pass(step):  # type: ignore[no-untyped-def]
    return StepOutcome(success=True, commands_run=[f"ran {step.id}"])


# --------------------------------------------------------------------------- #
# Engine-level
# --------------------------------------------------------------------------- #


def test_run_only_workflow_applies_each_step_with_one_evidence_row(
    approved_backend, frozen_clock
):  # type: ignore[no-untyped-def]
    wf = parse_workflow(_SEQ)
    records = run_workflow(
        approved_backend, wf, executor=_all_pass, actor="runner", clock=frozen_clock
    )
    assert [r.step_id for r in records] == ["a", "b"]
    assert all(r.status == "applied" for r in records)
    for r in records:
        task = approved_backend.get_task(r.task_id)
        assert task.status == TaskStatus.done
        assert is_workflow_task(task)
    # exactly one evidence row per step
    ev = [e for e in approved_backend.list_evidence()]
    assert len(ev) == 2


def test_needs_ordering_is_respected(approved_backend, frozen_clock):  # type: ignore[no-untyped-def]
    # 'b' depends on 'a' but is declared first → toposort must still run a before b.
    wf = parse_workflow("""
name: ord
steps:
  - id: b
    needs: [a]
    run: "do b"
  - id: a
    run: "do a"
""")
    seen: list[str] = []

    def exec_record(step):  # type: ignore[no-untyped-def]
        seen.append(step.id)
        return StepOutcome(success=True)

    run_workflow(approved_backend, wf, executor=exec_record, actor="r", clock=frozen_clock)
    assert seen == ["a", "b"]


def test_on_fail_reopen_continues_instead_of_aborting(
    approved_backend, frozen_clock
):  # type: ignore[no-untyped-def]
    wf = parse_workflow("""
name: rf
steps:
  - id: flaky
    run: "do flaky"
    on_fail: reopen
  - id: after
    run: "do after"
""")

    def exec_fail_first(step):  # type: ignore[no-untyped-def]
        return StepOutcome(success=step.id != "flaky")

    records = run_workflow(
        approved_backend, wf, executor=exec_fail_first, actor="r", clock=frozen_clock
    )
    by_id = {r.step_id: r for r in records}
    assert by_id["flaky"].status == "reopened"  # did not abort
    assert by_id["after"].status == "applied"  # run continued
    # the reopened task is back to ready, not done
    assert approved_backend.get_task(by_id["flaky"].task_id).status == TaskStatus.ready


def test_failure_without_reopen_aborts(approved_backend, frozen_clock):  # type: ignore[no-untyped-def]
    wf = parse_workflow("""
name: hard
steps:
  - id: boom
    run: "do boom"
""")
    try:
        run_workflow(
            approved_backend, wf,
            executor=lambda s: StepOutcome(success=False), actor="r", clock=frozen_clock,
        )
        raise AssertionError("expected WorkflowRunError")
    except WorkflowRunError:
        pass


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_cli_help_lists_run_workflow():
    result = runner.invoke(app, ["run-workflow", "--help"])
    assert result.exit_code == 0
    assert "run-workflow" in result.output or "NAME" in result.output


def test_cli_missing_workflow_file_errors(tmp_path: Path):
    runner.invoke(app, ["init", "--name", "X"], catch_exceptions=False)
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        runner.invoke(app, ["init", "--name", "X"], catch_exceptions=False)
        result = runner.invoke(app, ["run-workflow", "nope"])
        assert result.exit_code == 1
        assert "no workflow" in result.output.lower()
    finally:
        os.chdir(cwd)


def test_cli_end_to_end_runs_workflow(tmp_path: Path):
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        runner.invoke(app, ["init", "--name", "E2E"], catch_exceptions=False)
        Path(".anvil/prd.md").write_text(
            "# Project: E2E\n\n## Summary\n\nS.\n\n## Goals\n\n- G.\n\n"
            "## Requirements\n\n- R001: r.\n",
            encoding="utf-8",
        )
        assert runner.invoke(app, ["prd", "parse"]).exit_code == 0
        assert runner.invoke(app, ["prd", "review"]).exit_code == 0
        assert runner.invoke(app, ["prd", "review", "--approve"]).exit_code == 0

        wf_dir = Path(".anvil/workflows")
        wf_dir.mkdir(parents=True)
        # proof command that passes → step applies end to end
        (wf_dir / "demo.yaml").write_text(
            'name: demo\nsteps:\n  - id: check\n    run: "verify"\n'
            '    proof:\n      - command: "true"\n',
            encoding="utf-8",
        )
        result = runner.invoke(app, ["run-workflow", "demo"], catch_exceptions=False)
        assert result.exit_code == 0, result.output
        assert "1 step(s) applied" in result.output
    finally:
        os.chdir(cwd)
