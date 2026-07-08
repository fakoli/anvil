"""Tests for ``anvil doctor --preflight`` (retro-opps:T013) — the GO/NO-GO
gate before long workflows: PRD-parse + unresolved-decision probes layered
on the standard doctor chassis. Plain ``doctor`` must stay byte-compatible."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from anvil.cli import app

runner = CliRunner()

_CLEAN_PRD = """# Project: Preflight Fixture

## Summary

Fixture for doctor --preflight tests.

## Goals

- Gate long workflows on PRD health.

## Requirements

- R001: Preflight reports GO on a clean PRD.

## Features

### F001: Fixture feature

**Requirements:** R001

## Tasks

### T001: Fixture task

**Feature:** F001

A well-formed task.

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


def _init_project(tmp_path: Path, prd: str = _CLEAN_PRD) -> None:
    assert _invoke(tmp_path, ["init", "--name", "Preflight Test"]).exit_code == 0
    (tmp_path / ".anvil" / "prd.md").write_text(prd, encoding="utf-8")


class TestDoctorPreflight:
    def test_clean_project_prints_go_exit_0(self, tmp_path: Path) -> None:
        """AC: healthy project + clean PRD → PREFLIGHT: GO, exit 0."""
        _init_project(tmp_path)
        result = _invoke(tmp_path, ["doctor", "--preflight"])
        assert result.exit_code == 0, result.output
        assert "PREFLIGHT: GO" in result.output
        assert "prd_parse" in result.output

    def test_needs_decision_marker_is_no_go_exit_1(self, tmp_path: Path) -> None:
        """AC: an unresolved needs-decision marker → ERROR finding,
        PREFLIGHT: NO-GO, exit 1."""
        marked = _CLEAN_PRD.replace(
            "- R001: Preflight reports GO on a clean PRD.",
            "- R001: Preflight reports GO [NEEDS DECISION] which backend?",
        )
        _init_project(tmp_path, marked)
        result = _invoke(tmp_path, ["doctor", "--preflight"])
        assert result.exit_code == 1, result.output
        assert "PREFLIGHT: NO-GO" in result.output
        assert "unresolved decision" in result.output

    def test_broken_prd_missing_goals_is_error_naming_path(
        self, tmp_path: Path
    ) -> None:
        """AC: syntactically broken PRD (missing ## Goals) → parse probe is
        an ERROR naming the PRD path, exit 1."""
        broken = _CLEAN_PRD.replace("## Goals", "## Gols")
        _init_project(tmp_path, broken)
        result = _invoke(tmp_path, ["doctor", "--preflight"])
        assert result.exit_code == 1, result.output
        assert "PREFLIGHT: NO-GO" in result.output
        assert "prd.md" in result.output  # the path is named

    def test_json_envelope_carries_preflight_and_go(self, tmp_path: Path) -> None:
        """AC: --json emits one valid JSON line with data.preflight == true
        and data.go matching the exit code."""
        _init_project(tmp_path)
        ok = _invoke(tmp_path, ["doctor", "--preflight", "--json"])
        assert ok.exit_code == 0
        data = json.loads(ok.output.strip().splitlines()[-1])["data"]
        assert data["preflight"] is True
        assert data["go"] is True

        # Same project, PRD rewritten with a blocking marker → go flips.
        marked = _CLEAN_PRD.replace(
            "A well-formed task.", "A task. [NEEDS DECISION] scope?"
        )
        (tmp_path / ".anvil" / "prd.md").write_text(marked, encoding="utf-8")
        bad = _invoke(tmp_path, ["doctor", "--preflight", "--json"])
        assert bad.exit_code == 1
        envelope = json.loads(bad.output.strip().splitlines()[-1])
        assert envelope["data"]["preflight"] is True
        assert envelope["data"]["go"] is False

    def test_plain_doctor_unchanged_no_preflight_probes(
        self, tmp_path: Path
    ) -> None:
        """AC: plain `anvil doctor` output/exit byte-compatible — no
        preflight probes run, no PREFLIGHT line, and a PRD problem that
        would NO-GO the preflight does not affect it."""
        marked = _CLEAN_PRD.replace(
            "A well-formed task.", "A task. [NEEDS DECISION] scope?"
        )
        _init_project(tmp_path, marked)
        result = _invoke(tmp_path, ["doctor"])
        assert result.exit_code == 0, result.output
        assert "PREFLIGHT" not in result.output
        assert "prd_parse" not in result.output
        assert "prd_decisions" not in result.output

        result_json = _invoke(tmp_path, ["doctor", "--json"])
        data = json.loads(result_json.output.strip().splitlines()[-1])["data"]
        assert "preflight" not in data
        assert "go" not in data

    def test_missing_prd_file_is_error(self, tmp_path: Path) -> None:
        assert _invoke(tmp_path, ["init", "--name", "No PRD"]).exit_code == 0
        # init does not scaffold prd.md — a fresh project has none to parse.
        result = _invoke(tmp_path, ["doctor", "--preflight"])
        assert result.exit_code == 1
        assert "PRD source not found" in result.output
        assert "PREFLIGHT: NO-GO" in result.output

    def test_named_prd_partition_probed(self, tmp_path: Path) -> None:
        """Coverage (review finding): --prd <name> probes prds/<name>.md."""
        assert _invoke(tmp_path, ["init", "--name", "Named"]).exit_code == 0
        prds_dir = tmp_path / ".anvil" / "prds"
        prds_dir.mkdir()
        (prds_dir / "v2.md").write_text(_CLEAN_PRD, encoding="utf-8")
        result = _invoke(tmp_path, ["doctor", "--preflight", "--prd", "v2"])
        assert result.exit_code == 0, result.output
        assert "PREFLIGHT: GO" in result.output
        missing = _invoke(tmp_path, ["doctor", "--preflight", "--prd", "ghost"])
        assert missing.exit_code == 1
        assert "prds/ghost.md" in missing.output.replace("\\", "/")

    def test_plain_doctor_ignores_anvil_prd_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Coverage (review finding): $ANVIL_PRD set must not change plain
        doctor at all — prd is only consumed under --preflight."""
        _init_project(tmp_path)
        baseline = _invoke(tmp_path, ["doctor", "--json"])
        monkeypatch.setenv("ANVIL_PRD", "nonexistent-partition")
        with_env = _invoke(tmp_path, ["doctor", "--json"])
        assert with_env.exit_code == baseline.exit_code == 0
        assert with_env.output == baseline.output  # byte-identical

    def test_open_questions_warn_but_still_go(self, tmp_path: Path) -> None:
        """Open questions are informational by template convention →
        WARNING, still GO."""
        with_oq = _CLEAN_PRD + "\n## Open Questions\n\n- Should we cache?\n"
        _init_project(tmp_path, with_oq)
        result = _invoke(tmp_path, ["doctor", "--preflight"])
        assert result.exit_code == 0, result.output
        assert "open question" in result.output
        assert "PREFLIGHT: GO" in result.output
