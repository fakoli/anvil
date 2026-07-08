"""Tests for ``anvil progress`` (retro-opps:T011) — the CLI twin of the MCP
submit_progress tool. One ``progress.noted`` audit event per invocation;
task status never changes."""

from __future__ import annotations

import json
import os
from pathlib import Path

from typer.testing import CliRunner

from anvil.cli import app

runner = CliRunner()

_PRD = """# Project: Progress CLI Fixture

## Summary

Fixture for anvil progress tests.

## Goals

- Record progress phases.

## Requirements

- R001: Progress events carry a phase.

## Features

### F001: Fixture feature

**Requirements:** R001

## Tasks

### T001: Fixture task

**Feature:** F001

A task to note progress against.

**Acceptance criteria:**

- exists.

**Verification:**

- `echo ok`
"""


def _invoke(tmp_path: Path, cmd: list[str]):  # type: ignore[no-untyped-def]
    original = os.getcwd()
    os.chdir(tmp_path)
    try:
        return runner.invoke(app, cmd, catch_exceptions=False)
    finally:
        os.chdir(original)


def _setup_project(tmp_path: Path) -> None:
    assert _invoke(tmp_path, ["init", "--name", "Progress Test"]).exit_code == 0
    (tmp_path / ".anvil" / "prd.md").write_text(_PRD, encoding="utf-8")
    for cmd in (["prd", "parse"], ["plan"]):
        assert _invoke(tmp_path, cmd).exit_code == 0


def _progress_payloads(tmp_path: Path) -> list[dict]:
    events = (tmp_path / ".anvil" / "events.jsonl").read_text(encoding="utf-8")
    rows = [json.loads(line) for line in events.strip().splitlines()]
    payloads = []
    for row in rows:
        if row["action"] != "progress.noted":
            continue
        payload = row["payload_json"]
        payloads.append(json.loads(payload) if isinstance(payload, str) else payload)
    return payloads


class TestProgressCli:
    def test_records_one_event_with_phase_and_detail(self, tmp_path: Path) -> None:
        """AC: --json ok envelope + exactly one progress.noted event with
        phase == "build"."""
        _setup_project(tmp_path)
        result = _invoke(
            tmp_path,
            ["progress", "T001", "build", "--detail", "compiling", "--json"],
        )
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.output.strip().splitlines()[-1])
        assert envelope["ok"] is True
        assert envelope["command"] == "progress"
        assert envelope["data"]["recorded"] is True

        payloads = _progress_payloads(tmp_path)
        assert len(payloads) == 1
        assert payloads[0]["phase"] == "build"
        assert payloads[0]["detail"] == "compiling"

    def test_detail_omitted_when_absent(self, tmp_path: Path) -> None:
        """Same omit-when-None discipline as the MCP tool (T010)."""
        _setup_project(tmp_path)
        assert _invoke(tmp_path, ["progress", "T001", "tests"]).exit_code == 0
        payloads = _progress_payloads(tmp_path)
        assert payloads[0]["phase"] == "tests"
        assert "detail" not in payloads[0]

    def test_does_not_change_task_status(self, tmp_path: Path) -> None:
        _setup_project(tmp_path)
        before = _invoke(tmp_path, ["show", "T001", "--json"]).output
        status_before = json.loads(before.strip().splitlines()[-1])["data"]["task"]["status"]
        _invoke(tmp_path, ["progress", "T001", "build"])
        after = _invoke(tmp_path, ["show", "T001", "--json"]).output
        status_after = json.loads(after.strip().splitlines()[-1])["data"]["task"]["status"]
        assert status_after == status_before

    def test_unknown_task_fails_with_code(self, tmp_path: Path) -> None:
        """AC: unknown task exits 1; --json envelope carries task_not_found."""
        _setup_project(tmp_path)
        result = _invoke(tmp_path, ["progress", "T999", "build", "--json"])
        assert result.exit_code == 1
        envelope = json.loads(result.output.strip().splitlines()[-1])
        assert envelope["ok"] is False
        assert envelope["error"]["code"] == "task_not_found"

    def test_uninitialized_project_exits_1_not_initialized(
        self, tmp_path: Path
    ) -> None:
        """AC: canonical not_initialized handling."""
        result = _invoke(tmp_path, ["progress", "T001", "build", "--json"])
        assert result.exit_code == 1
        envelope = json.loads(result.output.strip().splitlines()[-1])
        assert envelope["error"]["code"] == "not_initialized"

    def test_help_documents_phase(self, tmp_path: Path) -> None:
        """AC: --help documents the phase argument."""
        result = _invoke(tmp_path, ["progress", "--help"])
        assert result.exit_code == 0
        assert "phase" in result.output.lower()
