"""T002 — parser for the WF-3 declarative workflow format.

Covers the three acceptance criteria (valid parse → ordered typed steps;
unknown `needs` id named in the error; malformed step rejected) plus the
control-flow ceiling locked in docs/decisions/wf3-format.md.
"""

from __future__ import annotations

import pytest

from anvil.workflows import Workflow, WorkflowParseError, parse_workflow

VALID = """
name: fix-flaky
description: Find flaky tests, fix each in parallel, gate on green.
trigger: { schedule: "0 9 * * 1-5" }
steps:
  - id: find
    run: "List flaky tests"
  - id: fix
    needs: [find]
    fan_out: "${{ steps.find.output }}"
    run: "Fix the flaky test: ${{ item }}"
    on_fail: reopen
    proof:
      - command: "uv run pytest ${{ item }}"
        passing_exit_codes: [0]
  - id: report
    needs: [fix]
    run: "Summarize the fixes"
"""


def test_valid_workflow_parses_to_ordered_typed_steps():
    wf = parse_workflow(VALID)
    assert isinstance(wf, Workflow)
    assert wf.name == "fix-flaky"
    assert [s.id for s in wf.steps] == ["find", "fix", "report"]  # order preserved
    fix = wf.steps[1]
    assert fix.needs == ["find"]
    assert fix.fan_out == "${{ steps.find.output }}"
    assert fix.on_fail == "reopen"
    assert fix.proof[0].command == "uv run pytest ${{ item }}"
    assert fix.proof[0].passing_exit_codes == [0]


def test_unknown_needs_id_names_the_step():
    bad = """
name: x
steps:
  - id: a
    run: "do a"
  - id: b
    run: "do b"
    needs: [nope]
"""
    with pytest.raises(WorkflowParseError) as exc:
        parse_workflow(bad)
    msg = str(exc.value)
    assert "b" in msg and "nope" in msg


def test_missing_id_is_rejected():
    bad = """
name: x
steps:
  - run: "no id here"
"""
    with pytest.raises(WorkflowParseError, match="id"):
        parse_workflow(bad)


def test_missing_run_is_rejected():
    bad = """
name: x
steps:
  - id: a
"""
    with pytest.raises(WorkflowParseError, match="run"):
        parse_workflow(bad)


def test_uses_code_satisfies_the_run_requirement():
    ok = """
name: x
steps:
  - id: a
    uses_code: workflows/custom.py
"""
    wf = parse_workflow(ok)
    assert wf.steps[0].uses_code == "workflows/custom.py"
    assert wf.steps[0].run is None


def test_unknown_key_hits_the_control_flow_ceiling():
    # `when:` is exactly the conditional the locked ceiling excludes.
    bad = """
name: x
steps:
  - id: a
    run: "do a"
    when: "${{ something }}"
"""
    with pytest.raises(WorkflowParseError, match="unknown key"):
        parse_workflow(bad)


def test_invalid_on_fail_is_rejected():
    bad = """
name: x
steps:
  - id: a
    run: "do a"
    on_fail: explode
"""
    with pytest.raises(WorkflowParseError, match="on_fail"):
        parse_workflow(bad)


def test_non_mapping_and_empty_steps_are_rejected():
    with pytest.raises(WorkflowParseError, match="mapping"):
        parse_workflow("- just\n- a\n- list")
    with pytest.raises(WorkflowParseError, match="steps"):
        parse_workflow("name: x\nsteps: []")
    with pytest.raises(WorkflowParseError, match="duplicate"):
        parse_workflow("name: x\nsteps:\n  - id: a\n    run: r\n  - id: a\n    run: r2")
