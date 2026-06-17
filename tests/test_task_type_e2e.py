"""End-to-end test for T015 — a non-feature (bugfix) task through the loop.

Drives the real CLI (init → prd parse → review → approve → plan → score →
review tasks → list/next filter → claim → submit → apply) against a PRD whose
single task declares ``**Type:** bugfix``. Asserts the task_type survives every
hop: parsing, planning/scoring persistence, the ``--type`` filter on
``list``/``next`` (with the v1.24 ``--json`` envelope), the work packet header,
and the final ``done`` state after evidence is submitted and approved.

Mirrors the harness in tests/test_strict_evidence.py (same _invoke / chdir
pattern) so the flow stays consistent with the rest of the CLI test suite.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from fakoli_state.cli import app

runner = CliRunner()


_BUGFIX_PRD = """\
# Project: Task Type E2E

## Summary

A project exercising a non-feature bugfix task end-to-end.

## Goals

- Fix the broken thing.

## Requirements

- R001: The thing must not break.

## Acceptance Criteria

- The thing works.

## Features

### F001: Reliability

Keep the thing working.

**Requirements:** R001

## Tasks

### T001: Fix the off-by-one in the parser

**Feature:** F001
**Priority:** high
**Type:** bugfix
**Likely files:** src/app/parser.py

**Acceptance criteria:**

- The boundary case no longer crashes.

**Verification:**

- `pytest tests/test_parser.py -v`
"""


def _invoke(tmp_path: Path, cmd: list[str]):  # type: ignore[no-untyped-def]
    original_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        return runner.invoke(app, cmd, catch_exceptions=False)
    finally:
        os.chdir(original_cwd)


def _json_data(tmp_path: Path, cmd: list[str]) -> dict:
    res = _invoke(tmp_path, [*cmd, "--json"])
    assert res.exit_code == 0, res.output
    env = json.loads(res.output)
    assert env["ok"] is True, env
    return env["data"]


def _status(tmp_path: Path, task_id: str) -> str | None:
    conn = sqlite3.connect(str(tmp_path / ".fakoli-state" / "state.db"))
    try:
        row = conn.execute(
            "SELECT status FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def _plan_bugfix(tmp_path: Path) -> str:
    """init → bugfix PRD → review → approve → plan → score → review tasks."""
    assert _invoke(tmp_path, ["init", "--name", "Task Type E2E"]).exit_code == 0
    (tmp_path / ".fakoli-state" / "prd.md").write_text(
        _BUGFIX_PRD, encoding="utf-8"
    )
    assert _invoke(tmp_path, ["prd", "parse"]).exit_code == 0
    assert _invoke(tmp_path, ["prd", "review"]).exit_code == 0
    assert _invoke(tmp_path, ["prd", "review", "--approve"]).exit_code == 0
    assert _invoke(tmp_path, ["plan", "--no-llm"]).exit_code == 0
    assert _invoke(tmp_path, ["score"]).exit_code == 0
    assert _invoke(tmp_path, ["review", "tasks"]).exit_code == 0
    return "T001"


class TestBugfixTaskTypeEndToEnd:
    def test_plan_persists_bugfix_task_type(self, tmp_path: Path) -> None:
        """The parsed **Type:** bugfix survives planning into the DB."""
        task_id = _plan_bugfix(tmp_path)
        data = _json_data(tmp_path, ["show", task_id])
        assert data["task"]["task_type"] == "bugfix"

    def test_list_type_filter_json_envelope(self, tmp_path: Path) -> None:
        """`list --type bugfix --json` returns the typed task + echoes the filter."""
        _plan_bugfix(tmp_path)
        data = _json_data(tmp_path, ["list", "--type", "bugfix"])
        assert data["count"] == 1
        assert data["filters"]["task_type"] == "bugfix"
        assert [t["task_type"] for t in data["tasks"]] == ["bugfix"]

        # A different type filters the task out entirely.
        empty = _json_data(tmp_path, ["list", "--type", "feature"])
        assert empty["count"] == 0

    def test_next_type_filter_json_envelope(self, tmp_path: Path) -> None:
        """`next --type` scopes the recommendation to the requested type."""
        _plan_bugfix(tmp_path)
        bug = _json_data(tmp_path, ["next", "--type", "bugfix"])
        assert bug["task"] is not None
        assert bug["task"]["task_type"] == "bugfix"

        # No refactor task exists → null (empty queue, not an error).
        ref = _json_data(tmp_path, ["next", "--type", "refactor"])
        assert ref["task"] is None

    def test_bugfix_claims_executes_submits_and_completes(
        self, tmp_path: Path
    ) -> None:
        """A bugfix task claims, submits evidence, and reaches done — typed throughout."""
        task_id = _plan_bugfix(tmp_path)

        assert _invoke(
            tmp_path, ["claim", task_id, "--actor", "agent-test"]
        ).exit_code == 0
        assert _status(tmp_path, task_id) == "claimed"

        submit = _invoke(
            tmp_path,
            [
                "submit",
                task_id,
                "--commands",
                "pytest tests/test_parser.py -v",
                "--files-changed",
                "src/app/parser.py",
                "--actor",
                "agent-test",
            ],
        )
        assert submit.exit_code == 0, submit.output
        assert _status(tmp_path, task_id) == "needs_review"

        approve = _invoke(
            tmp_path,
            ["apply", task_id, "--approve", "--reviewer", "human"],
        )
        assert approve.exit_code == 0, approve.output
        assert _status(tmp_path, task_id) == "done"

        # task_type is preserved all the way to the terminal state, and an
        # immutable evidence record exists for the completed bugfix.
        data = _json_data(tmp_path, ["show", task_id])
        assert data["task"]["task_type"] == "bugfix"

        conn = sqlite3.connect(str(tmp_path / ".fakoli-state" / "state.db"))
        try:
            ev_count = conn.execute(
                "SELECT COUNT(*) FROM evidence WHERE task_id=?", (task_id,)
            ).fetchone()[0]
        finally:
            conn.close()
        assert ev_count >= 1, "completed bugfix must leave an evidence record"

    def test_work_packet_header_shows_bugfix_type(self, tmp_path: Path) -> None:
        """The rendered work packet markdown carries the **Type:** bugfix line."""
        task_id = _plan_bugfix(tmp_path)
        assert _invoke(
            tmp_path, ["claim", task_id, "--actor", "agent-test"]
        ).exit_code == 0
        assert _invoke(tmp_path, ["packet", task_id]).exit_code == 0
        packet_md = (
            tmp_path / ".fakoli-state" / "packets" / f"{task_id}.md"
        ).read_text(encoding="utf-8")
        assert "**Type:** bugfix" in packet_md
