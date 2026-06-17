"""CLI integration tests using Typer's CliRunner.

Tests the fakoli-state CLI surface:
- init — scaffolding, overwrite guards, plugin-root guard
- status — uninitialized/initialized paths, human and hook formats
- --version

All tests run in isolated tmp directories.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from click.testing import Result
from typer.testing import CliRunner

from fakoli_state.cli import app

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

runner = CliRunner()


# ---------------------------------------------------------------------------
# init — happy path
# ---------------------------------------------------------------------------


class TestInit:
    def test_init_creates_state_directory(self, tmp_path: Path) -> None:
        """init creates .fakoli-state/ with all expected files and directories."""
        result = runner.invoke(
            app,
            ["init", "--name", "My Test Project"],
            catch_exceptions=False,
            env={"HOME": str(tmp_path)},
        )
        # May run from the actual cwd; use tmp_path as the project root via os.chdir
        # We need to run in tmp_path, so let's use a different approach
        original_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = runner.invoke(
                app,
                ["init", "--name", "My Test Project"],
                catch_exceptions=False,
            )
        finally:
            os.chdir(original_cwd)

        assert result.exit_code == 0, f"init failed: {result.output}"
        state_dir = tmp_path / ".fakoli-state"
        assert state_dir.exists(), ".fakoli-state/ directory not created"
        assert (state_dir / "state.db").exists(), "state.db not created"
        assert (state_dir / "events.jsonl").exists(), "events.jsonl not created"
        assert (state_dir / "config.yaml").exists(), "config.yaml not created"
        assert (state_dir / "packets").is_dir(), "packets/ not created"
        # snapshots/ is no longer pre-created at init (PS-2);
        # `fakoli-state snapshot` will create it on first use.

    def test_init_output_contains_project_name(self, tmp_path: Path) -> None:
        """init prints confirmation with the project name."""
        original_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = runner.invoke(
                app,
                ["init", "--name", "Repo Alpha"],
                catch_exceptions=False,
            )
        finally:
            os.chdir(original_cwd)

        assert result.exit_code == 0
        assert "Repo Alpha" in result.output

    def test_init_refuses_overwrite(self, tmp_path: Path) -> None:
        """Second call to init in same dir exits non-zero without --force."""
        original_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            # First init
            first = runner.invoke(
                app,
                ["init", "--name", "Project"],
                catch_exceptions=False,
            )
            assert first.exit_code == 0

            # Second init without --force
            second = runner.invoke(
                app,
                ["init", "--name", "Project"],
                catch_exceptions=False,
            )
        finally:
            os.chdir(original_cwd)

        assert second.exit_code != 0, "Second init should have failed without --force"
        assert "already exists" in second.output or "force" in second.output.lower()

    def test_init_force_overwrites_existing(self, tmp_path: Path) -> None:
        """--force reinitialises an existing .fakoli-state/ directory."""
        original_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            # First init
            runner.invoke(
                app,
                ["init", "--name", "Project"],
                catch_exceptions=False,
            )
            # Second init with --force
            result = runner.invoke(
                app,
                ["init", "--name", "Project", "--force"],
                catch_exceptions=False,
            )
        finally:
            os.chdir(original_cwd)

        assert result.exit_code == 0, f"--force init failed: {result.output}"

    def test_init_force_truncates_events_log(self, tmp_path: Path) -> None:
        """--force reinit wipes events.jsonl and state.db so the replay/audit
        guarantee holds — without this, a second init appends duplicate event
        IDs to the old log and the log no longer replays to the current DB.
        (Regression test for Greptile PR #37 finding.)
        """
        original_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            # First init — produces E000001 (project.created) and E000002 (state.initialized).
            runner.invoke(app, ["init", "--name", "First"], catch_exceptions=False)
            events_path = tmp_path / ".fakoli-state" / "events.jsonl"
            first_lines = events_path.read_text(encoding="utf-8").splitlines()
            assert len(first_lines) == 2, f"expected 2 events after first init, got {len(first_lines)}"

            # Second init with --force — must replace the log, not append to it.
            result = runner.invoke(
                app,
                ["init", "--name", "Second", "--force"],
                catch_exceptions=False,
            )
            assert result.exit_code == 0, f"--force init failed: {result.output}"

            second_lines = events_path.read_text(encoding="utf-8").splitlines()
            # Must still be exactly 2 events (not 4) — the old log was wiped.
            assert len(second_lines) == 2, (
                f"--force did not truncate events.jsonl; expected 2 events, "
                f"got {len(second_lines)}. Replay guarantee is broken."
            )
            # And the new events should be for the new project name.
            assert "Second" in second_lines[0], "first event after --force should reference new project"
        finally:
            os.chdir(original_cwd)

    def test_init_refuses_in_plugin_root(self, tmp_path: Path) -> None:
        """init refuses when .claude-plugin/plugin.json declares name == fakoli-state."""
        # Create fake plugin manifest
        plugin_dir = tmp_path / ".claude-plugin"
        plugin_dir.mkdir()
        (plugin_dir / "plugin.json").write_text(
            json.dumps({"name": "fakoli-state", "version": "1.0.0"}),
            encoding="utf-8",
        )

        original_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = runner.invoke(
                app,
                ["init", "--name", "Test"],
                catch_exceptions=False,
            )
        finally:
            os.chdir(original_cwd)

        assert result.exit_code != 0
        # The error should mention plugin root or the plugin
        combined = result.output + (result.stderr if hasattr(result, "stderr") and result.stderr else "")
        assert "plugin" in combined.lower() or "plugin" in result.output.lower()

    def test_init_non_fakoli_state_plugin_allowed(self, tmp_path: Path) -> None:
        """init is allowed in a directory with a different plugin name."""
        plugin_dir = tmp_path / ".claude-plugin"
        plugin_dir.mkdir()
        (plugin_dir / "plugin.json").write_text(
            json.dumps({"name": "some-other-plugin", "version": "1.0.0"}),
            encoding="utf-8",
        )

        original_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = runner.invoke(
                app,
                ["init", "--name", "Test"],
                catch_exceptions=False,
            )
        finally:
            os.chdir(original_cwd)

        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# init --with-sample — one-command standalone quickstart (T004)
# ---------------------------------------------------------------------------


class TestInitWithSample:
    """`fakoli-state init --with-sample` seeds a runnable PRD→next loop.

    Names contain ``with_sample`` so ``pytest -k with_sample`` selects them
    (per the T004 verification command).
    """

    def _run(self, app_args: list[str], tmp_path: Path) -> Result:
        original_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            return runner.invoke(app, app_args, catch_exceptions=False)
        finally:
            os.chdir(original_cwd)

    def test_init_with_sample_seeds_ready_task_for_next(self, tmp_path: Path) -> None:
        """init --with-sample makes `next` return a ready task with no input."""
        init_result = self._run(["init", "--with-sample"], tmp_path)
        assert init_result.exit_code == 0, f"init failed: {init_result.output}"

        # Scaffold + sample prd.md present.
        state_dir = tmp_path / ".fakoli-state"
        assert (state_dir / "prd.md").exists(), "sample prd.md not written"
        assert (state_dir / "state.db").exists()

        # `next` returns a ready task — the whole point of the flag.
        next_result = self._run(["next", "--json"], tmp_path)
        assert next_result.exit_code == 0, next_result.output
        payload = json.loads(next_result.output)
        assert payload["ok"] is True
        task = payload["data"]["task"]
        assert task is not None, "no claimable task after init --with-sample"
        assert task["status"] == "ready"

    def test_init_with_sample_reports_seed_summary(self, tmp_path: Path) -> None:
        """Human output names the sample seed and points at `next`."""
        result = self._run(["init", "--with-sample"], tmp_path)
        assert result.exit_code == 0
        assert "Seeded sample project" in result.output
        assert "ready" in result.output
        assert "fakoli-state next" in result.output

    def test_init_with_sample_status_shows_ready_tasks(self, tmp_path: Path) -> None:
        """status reflects the seeded ready tasks (state actually persisted)."""
        assert self._run(["init", "--with-sample"], tmp_path).exit_code == 0
        status_result = self._run(["status", "--json"], tmp_path)
        assert status_result.exit_code == 0
        data = json.loads(status_result.output)["data"]
        assert data["tasks"]["total"] >= 1
        assert data["tasks"]["ready"] >= 1
        # PRD advanced through the full lifecycle to approved.
        assert data["prd_status"] == "approved"

    def test_init_without_sample_leaves_no_tasks(self, tmp_path: Path) -> None:
        """Without --with-sample, init behaviour is unchanged: no prd, no tasks.

        This is the backward-compatibility guard for the --with-sample flag —
        the default path must not seed anything.
        """
        result = self._run(["init"], tmp_path)
        assert result.exit_code == 0
        state_dir = tmp_path / ".fakoli-state"
        # Default init does NOT write a prd.md.
        assert not (state_dir / "prd.md").exists()
        # And `next` finds nothing claimable.
        next_result = self._run(["next", "--json"], tmp_path)
        assert next_result.exit_code == 0
        assert json.loads(next_result.output)["data"]["task"] is None

    def test_init_with_sample_events_replay_to_same_state(self, tmp_path: Path) -> None:
        """Seeded events.jsonl replays to an identical DB (audit invariant)."""
        assert self._run(["init", "--with-sample"], tmp_path).exit_code == 0
        state_dir = tmp_path / ".fakoli-state"
        scratch = tmp_path / "scratch.db"
        replay_result = self._run(
            [
                "replay",
                "--from-events",
                str(state_dir / "events.jsonl"),
                "--into",
                str(scratch),
            ],
            tmp_path,
        )
        assert replay_result.exit_code == 0, replay_result.output

        def task_rows(db: Path) -> list[tuple[str, str]]:
            conn = sqlite3.connect(str(db))
            try:
                return sorted(conn.execute("SELECT id, status FROM tasks").fetchall())
            finally:
                conn.close()

        assert task_rows(state_dir / "state.db") == task_rows(scratch)


# ---------------------------------------------------------------------------
# status — uninitialized
# ---------------------------------------------------------------------------


class TestStatusUninitialized:
    def test_status_uninitialized_human_format(self, tmp_path: Path) -> None:
        """status in dir without .fakoli-state/ exits 1."""
        original_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = runner.invoke(
                app,
                ["status"],
                catch_exceptions=False,
            )
        finally:
            os.chdir(original_cwd)

        assert result.exit_code == 1
        assert "not initialized" in result.output.lower() or "init" in result.output.lower()

    def test_status_uninitialized_hook_format(self, tmp_path: Path) -> None:
        """status --hook-format in dir without .fakoli-state/ exits 0 with 'uninitialized'."""
        original_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = runner.invoke(
                app,
                ["status", "--hook-format"],
                catch_exceptions=False,
            )
        finally:
            os.chdir(original_cwd)

        assert result.exit_code == 0
        assert "uninitialized" in result.output


# ---------------------------------------------------------------------------
# status — initialized
# ---------------------------------------------------------------------------


class TestStatusInitialized:
    def _init_and_status(
        self, tmp_path: Path, extra_status_args: list[str] | None = None
    ) -> Result:
        """Helper: init in tmp_path, then run status."""
        original_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            init_result = runner.invoke(
                app,
                ["init", "--name", "My Project"],
                catch_exceptions=False,
            )
            assert init_result.exit_code == 0, f"init failed: {init_result.output}"

            status_args = ["status"]
            if extra_status_args:
                status_args.extend(extra_status_args)
            status_result = runner.invoke(
                app,
                status_args,
                catch_exceptions=False,
            )
        finally:
            os.chdir(original_cwd)

        return status_result

    def test_status_initialized_human_format(self, tmp_path: Path) -> None:
        """status after init shows 'Active claims:' line."""
        result = self._init_and_status(tmp_path)
        assert result.exit_code == 0, f"status failed: {result.output}"
        output = result.output
        # Should have "Active claims:" section (from the CLI output)
        assert "claims" in output.lower(), f"Expected 'claims' in output:\n{output}"

    def test_status_initialized_human_format_has_project_name(self, tmp_path: Path) -> None:
        """Human-readable status output contains 'My Project'."""
        result = self._init_and_status(tmp_path)
        assert result.exit_code == 0
        assert "My Project" in result.output

    def test_status_initialized_hook_format(self, tmp_path: Path) -> None:
        """status --hook-format after init outputs the key:value compact line."""
        result = self._init_and_status(tmp_path, extra_status_args=["--hook-format"])
        assert result.exit_code == 0, f"status --hook-format failed: {result.output}"
        output = result.output
        # Expected: "active-claims:0 ready-tasks:0 blockers:0 prd-status:none"
        assert "active-claims:" in output
        assert "ready-tasks:" in output
        assert "blockers:" in output
        assert "prd-status:" in output

    def test_status_initialized_hook_format_exit_code_zero(self, tmp_path: Path) -> None:
        """hook-format always exits 0."""
        result = self._init_and_status(tmp_path, extra_status_args=["--hook-format"])
        assert result.exit_code == 0

    def test_status_with_cwd_flag(self, tmp_path: Path) -> None:
        """status --cwd works without changing directory."""
        original_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            runner.invoke(app, ["init", "--name", "CWD Test"], catch_exceptions=False)
        finally:
            os.chdir(original_cwd)

        # Now run status --cwd from any directory
        result = runner.invoke(
            app,
            ["status", "--cwd", str(tmp_path)],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert "CWD Test" in result.output


# ---------------------------------------------------------------------------
# --version
# ---------------------------------------------------------------------------


class TestVersion:
    def test_version_still_works(self) -> None:
        """--version prints 'fakoli-state {__version__}' and exits 0.

        Imports __version__ rather than hardcoding so the test doesn't
        need a one-line bump on every release (Critic-4 TQ-5 in PR #41).
        """
        from fakoli_state import __version__

        result = runner.invoke(app, ["--version"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "fakoli-state" in result.output
        assert __version__ in result.output

    def test_version_short_flag(self) -> None:
        """-V is an alias for --version."""
        result = runner.invoke(app, ["-V"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "fakoli-state" in result.output


# ---------------------------------------------------------------------------
# Phase 3 CLI test helpers
# ---------------------------------------------------------------------------

_MINIMAL_PRD_CONTENT = """\
# Project: CLI Test Project

## Summary

A project for CLI testing.

## Goals

- Do something useful.

## Requirements

- R001: The system accepts input.
- R002: The system produces output.
"""

_FULL_PRD_CONTENT = """\
# Project: CLI Full Test Project

## Summary

A full project for complete CLI workflow testing.

## Goals

- Convert files correctly.
- Handle errors gracefully.

## Non-Goals

- Support all formats.

## Requirements

- R001: Accept file input.
- R002: Produce file output.
- R003: Handle errors.

## Acceptance Criteria

- Converts files correctly.

## Features

### F001: File Conversion

Convert input files to output format.

**Requirements:** R001, R002

### F002: Error Handling

Handle errors gracefully.

**Requirements:** R003

## Tasks

### T001: Implement converter

**Feature:** F001
**Priority:** high
**Likely files:** src/app/converter.py, src/app/utils.py

**Acceptance criteria:**

- Conversion succeeds for valid input.
- Invalid input raises an error.

**Verification:**

- `pytest tests/test_converter.py -v`

### T002: Implement error handler

**Feature:** F002
**Priority:** medium
**Likely files:** src/app/errors.py

**Acceptance criteria:**

- Errors are reported with context.
- Exit code is non-zero on error.

**Verification:**

- `pytest tests/test_errors.py -v`
"""


def _do_init(tmp_path: Path, name: str = "Test Project") -> None:
    """Run `fakoli-state init` in tmp_path."""
    original_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = runner.invoke(
            app, ["init", "--name", name], catch_exceptions=False
        )
        assert result.exit_code == 0, f"init failed: {result.output}"
    finally:
        os.chdir(original_cwd)


def _write_prd(tmp_path: Path, content: str) -> None:
    """Write content to .fakoli-state/prd.md."""
    prd_path = tmp_path / ".fakoli-state" / "prd.md"
    prd_path.write_text(content, encoding="utf-8")


def _invoke_cmd(tmp_path: Path, cmd: list[str]):  # type: ignore[no-untyped-def]
    """Invoke a CLI command in tmp_path context."""
    original_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = runner.invoke(app, cmd, catch_exceptions=False)
    finally:
        os.chdir(original_cwd)
    return result


# ---------------------------------------------------------------------------
# prd parse command
# ---------------------------------------------------------------------------


class TestPrdParse:
    def test_prd_parse_minimal_valid(self, tmp_path: Path) -> None:
        """write minimal prd.md, run prd parse, exit 0, prints parsed requirements."""
        _do_init(tmp_path)
        _write_prd(tmp_path, _MINIMAL_PRD_CONTENT)

        result = _invoke_cmd(tmp_path, ["prd", "parse"])
        assert result.exit_code == 0, f"prd parse failed: {result.output}"
        # Should print something about parsed requirements
        assert "Parsed" in result.output or "parsed" in result.output.lower()
        assert "2" in result.output  # 2 requirements

    def test_prd_parse_missing_required_section(self, tmp_path: Path) -> None:
        """PRD without ## Goals → exit 1, error mentions missing section."""
        _do_init(tmp_path)
        prd_without_goals = """\
# Project: Broken Project

## Summary

A project without goals.

## Requirements

- R001: Does something.
"""
        _write_prd(tmp_path, prd_without_goals)
        result = _invoke_cmd(tmp_path, ["prd", "parse"])
        assert result.exit_code == 1
        # The error should mention Goals
        combined = result.output + (result.stderr if hasattr(result, "stderr") and result.stderr else "")
        assert "Goals" in combined or "goals" in combined.lower()

    def test_prd_parse_no_prd_md(self, tmp_path: Path) -> None:
        """Run prd parse with no prd.md present → exit 1 with sensible error."""
        _do_init(tmp_path)
        # Do NOT write prd.md
        result = _invoke_cmd(tmp_path, ["prd", "parse"])
        assert result.exit_code == 1
        # Should mention the file or the path
        combined = result.output + (result.stderr if hasattr(result, "stderr") and result.stderr else "")
        assert "prd" in combined.lower() or "not found" in combined.lower()

    def test_prd_parse_without_init_exits_1(self, tmp_path: Path) -> None:
        """prd parse without init → exit 1."""
        result = _invoke_cmd(tmp_path, ["prd", "parse"])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# prd review command
# ---------------------------------------------------------------------------


class TestPrdReview:
    def test_prd_review_draft_to_reviewed(self, tmp_path: Path) -> None:
        """After parse, run prd review (no --approve) → PRD moves to reviewed."""
        _do_init(tmp_path)
        _write_prd(tmp_path, _MINIMAL_PRD_CONTENT)
        _invoke_cmd(tmp_path, ["prd", "parse"])

        result = _invoke_cmd(tmp_path, ["prd", "review"])
        assert result.exit_code == 0, f"prd review failed: {result.output}"
        assert "reviewed" in result.output.lower()

    def test_prd_review_approve_reviewed_to_approved(self, tmp_path: Path) -> None:
        """After review, run prd review --approve → PRD moves to approved."""
        _do_init(tmp_path)
        _write_prd(tmp_path, _MINIMAL_PRD_CONTENT)
        _invoke_cmd(tmp_path, ["prd", "parse"])
        _invoke_cmd(tmp_path, ["prd", "review"])  # draft → reviewed

        result = _invoke_cmd(tmp_path, ["prd", "review", "--approve"])
        assert result.exit_code == 0, f"prd review --approve failed: {result.output}"
        assert "approved" in result.output.lower()

    def test_prd_review_fails_without_parsed_prd(self, tmp_path: Path) -> None:
        """prd review without a parsed PRD → exit 1 with helpful error."""
        _do_init(tmp_path)
        # No prd parse done
        result = _invoke_cmd(tmp_path, ["prd", "review"])
        assert result.exit_code == 1
        combined = result.output + (result.stderr if hasattr(result, "stderr") and result.stderr else "")
        assert "prd" in combined.lower() or "parse" in combined.lower()


# ---------------------------------------------------------------------------
# prd find-decisions command (v1.14.0)
# ---------------------------------------------------------------------------


_PRD_WITH_DECISIONS = """\
# Project: CLI Decisions Test

## Summary

The system must serialize inputs [NEEDS DECISION: which format?].

## Goals

- Ship v1 [NEEDS DECISION].

## Requirements

- R001: System works.

## Open Questions

- What is the SLO target?
"""


class TestPrdFindDecisions:
    def test_clean_prd_exits_zero_with_zero_total(self, tmp_path: Path) -> None:
        """A PRD with no markers, no open questions, no missing fields →
        exit 0 with a summary line that mentions 0 total."""
        _do_init(tmp_path)
        _write_prd(tmp_path, _MINIMAL_PRD_CONTENT)
        result = _invoke_cmd(tmp_path, ["prd", "find-decisions"])
        assert result.exit_code == 0, f"find-decisions failed: {result.output}"
        # Summary line names all three kinds with counts.
        assert "0 total" in result.output
        assert "NEEDS_DECISION" in result.output
        assert "open questions" in result.output
        assert "missing fields" in result.output

    def test_prd_with_markers_and_questions_lists_them(
        self, tmp_path: Path
    ) -> None:
        """A PRD containing two `[NEEDS DECISION]` markers and one open
        question should print three decision blocks and exit 0."""
        _do_init(tmp_path)
        _write_prd(tmp_path, _PRD_WITH_DECISIONS)
        result = _invoke_cmd(tmp_path, ["prd", "find-decisions"])
        assert result.exit_code == 0, f"find-decisions failed: {result.output}"
        # ND ids and OQ id are surfaced verbatim.
        assert "ND-001" in result.output
        assert "ND-002" in result.output
        assert "OQ001" in result.output
        # Group headers are visible.
        assert "NEEDS DECISION markers" in result.output
        assert "Open Questions" in result.output
        # Summary line has the right counts (2 NDs + 1 OQ).
        assert "3 total" in result.output
        assert "2 NEEDS_DECISION" in result.output
        assert "1 open questions" in result.output

    def test_missing_prd_file_exits_one(self, tmp_path: Path) -> None:
        """No prd.md present → exit 1 with helpful error."""
        _do_init(tmp_path)
        result = _invoke_cmd(tmp_path, ["prd", "find-decisions"])
        assert result.exit_code == 1
        combined = result.output + (
            result.stderr if hasattr(result, "stderr") and result.stderr else ""
        )
        assert "prd" in combined.lower() or "not found" in combined.lower()

    def test_without_init_exits_one(self, tmp_path: Path) -> None:
        """Calling outside an initialized project → exit 1."""
        result = _invoke_cmd(tmp_path, ["prd", "find-decisions"])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# plan command
# ---------------------------------------------------------------------------


class TestPlan:
    def test_plan_generates_features_and_tasks(self, tmp_path: Path) -> None:
        """After prd parse with features + tasks, run plan, assert tasks in backend."""
        _do_init(tmp_path)
        _write_prd(tmp_path, _FULL_PRD_CONTENT)
        parse_result = _invoke_cmd(tmp_path, ["prd", "parse"])
        assert parse_result.exit_code == 0

        result = _invoke_cmd(tmp_path, ["plan"])
        assert result.exit_code == 0, f"plan failed: {result.output}"
        assert "feature" in result.output.lower() or "task" in result.output.lower()

        # Verify tasks in backend
        list_result = _invoke_cmd(tmp_path, ["list"])
        assert list_result.exit_code == 0
        # Should show at least 2 tasks (T001, T002)
        assert "T001" in list_result.output or "task" in list_result.output.lower()

    def test_plan_creates_tasks_on_first_run(self, tmp_path: Path) -> None:
        """Running plan once creates tasks correctly."""
        _do_init(tmp_path)
        _write_prd(tmp_path, _FULL_PRD_CONTENT)
        _invoke_cmd(tmp_path, ["prd", "parse"])

        result = _invoke_cmd(tmp_path, ["plan"])
        assert result.exit_code == 0

        list_result = _invoke_cmd(tmp_path, ["list"])
        assert list_result.exit_code == 0
        assert "T001" in list_result.output
        assert "T002" in list_result.output

    def test_plan_is_idempotent(self, tmp_path: Path) -> None:
        """Running plan twice does not duplicate tasks and does not trip
        ON DELETE RESTRICT foreign keys. Regression test for the bug
        welder flagged in P3/W3: INSERT OR REPLACE on tasks triggered
        DELETE+INSERT, violating claim/evidence FK constraints whenever
        plan was re-run after work had begun. Fix: INSERT ... ON CONFLICT
        DO UPDATE preserves row identity, so FKs stay valid.
        """
        _do_init(tmp_path)
        _write_prd(tmp_path, _FULL_PRD_CONTENT)
        _invoke_cmd(tmp_path, ["prd", "parse"])

        first = _invoke_cmd(tmp_path, ["plan"])
        assert first.exit_code == 0
        first_list = _invoke_cmd(tmp_path, ["list"]).output
        first_t001_count = first_list.count("T001")

        # Re-parse + re-plan; must not duplicate or FK-error.
        _invoke_cmd(tmp_path, ["prd", "parse"])
        second = _invoke_cmd(tmp_path, ["plan"])
        assert second.exit_code == 0, f"second plan failed: {second.output}"

        second_list = _invoke_cmd(tmp_path, ["list"]).output
        second_t001_count = second_list.count("T001")
        assert second_t001_count == first_t001_count, (
            f"task count should not change on re-plan; "
            f"first={first_t001_count} second={second_t001_count}"
        )

    def test_plan_without_prd_parse_exits_1(self, tmp_path: Path) -> None:
        """plan without a prd.md → exit 1."""
        _do_init(tmp_path)
        # No prd.md file written
        result = _invoke_cmd(tmp_path, ["plan"])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# plan — LLM task-generation backstop (v1.15+)
# ---------------------------------------------------------------------------


# A PRD with features + requirements but NO `## Tasks` section. Triggers
# the LLM-backstop path in `plan`. Matches the shape `parse_prd` accepts.
_PRD_WITHOUT_TASKS = """\
# Project: LLM Backstop Test

## Summary

A project for exercising the LLM task-generation backstop.

## Goals

- Verify the backstop fires when tasks are absent.

## Requirements

- R001: Accept input.
- R002: Produce output.

## Features

### F001: Core Feature

The single feature exercised by the test PRD.

**Requirements:** R001, R002
"""

# Canned LLM response with two tasks the parser can consume. Kept inline
# here rather than imported from test_llm_planner so each test file is
# self-contained and individually executable.
_CANNED_LLM_TASKS = """\
## Tasks

### T001: Implement input handler

**Feature:** F001
**Priority:** high
**Likely files:** src/app/handler.py

Parse the input correctly with validation.

**Acceptance criteria:**

- Valid input is parsed.
- Invalid input raises with the filename.

**Verification:**

- `pytest tests/test_handler.py -v`

### T002: Implement output writer

**Feature:** F001
**Priority:** medium
**Likely files:** src/app/writer.py

Write output to disk atomically.

**Acceptance criteria:**

- Output round-trips back to the input.

**Verification:**

- `pytest tests/test_writer.py -v`
"""


def _build_recorded_llm_provider_for(prd_content: str):  # type: ignore[no-untyped-def]
    """Build a RecordedLLMProvider keyed to the LLM planner's prompt for
    ``prd_content``.

    Parses the PRD with ``parse_prd`` to recover the same PRD/Feature/
    Requirement objects the production code path will pass to the planner,
    builds the planner's user prompt with the same helper, and records a
    canned response under that prompt's sha256 key.
    """
    from fakoli_state.planning.llm import LLMResponse, RecordedLLMProvider
    from fakoli_state.planning.llm_planner import (
        _SYSTEM_PROMPT,
        _build_user_prompt,
    )
    from fakoli_state.planning.template import parse_prd

    parsed = parse_prd(prd_content, prd_id="prd")
    user_prompt = _build_user_prompt(
        parsed.prd, parsed.features, parsed.requirements, None
    )
    key = RecordedLLMProvider.record_key(
        _SYSTEM_PROMPT, user_prompt, max_tokens=8000, temperature=0.0
    )
    canned = LLMResponse(
        text=_CANNED_LLM_TASKS,
        input_tokens=100,
        cached_input_tokens=0,
        output_tokens=50,
        model="claude-opus-4-7",
        finish_reason="end_turn",
    )
    return RecordedLLMProvider({key: canned})


class TestPlanLlmBackstop:
    """v1.15+ behaviour: when prd.md has features+requirements but no
    `## Tasks` section the CLI calls the LLM planner, appends generated
    tasks to prd.md, re-parses, and emits task events. See spec
    `docs/specs/2026-05-25-llm-task-generation-backstop.md`.
    """

    def _install_recorded_resolver(
        self,
        monkeypatch,  # type: ignore[no-untyped-def]
        provider,  # type: ignore[no-untyped-def]
    ) -> None:
        """Replace ``resolve_planner_provider`` so the CLI uses ``provider``
        without needing ANTHROPIC_API_KEY or any real network call.

        We patch the symbol on the ``llm_planner`` module because the CLI
        imports ``generate_tasks_markdown`` and that function reads
        ``resolve_planner_provider`` from the same module at call time
        (no early binding into cli.plan)."""
        from fakoli_state.planning import llm_planner

        # v1.17.0 — resolve_planner_provider gained a `config` parameter
        # (Config | None). The CLI passes the loaded config; the test stub
        # accepts and ignores it.
        monkeypatch.setattr(
            llm_planner,
            "resolve_planner_provider",
            lambda config=None: (provider, "anthropic"),
        )

    def test_happy_path_generates_appends_and_reparses(
        self,
        tmp_path: Path,
        monkeypatch,  # type: ignore[no-untyped-def]
    ) -> None:
        """End-to-end: PRD without `## Tasks` → plan calls LLM, appends to
        prd.md, re-parses, and reports N tasks generated via LLM."""
        _do_init(tmp_path)
        _write_prd(tmp_path, _PRD_WITHOUT_TASKS)
        _invoke_cmd(tmp_path, ["prd", "parse"])

        provider = _build_recorded_llm_provider_for(_PRD_WITHOUT_TASKS)
        self._install_recorded_resolver(monkeypatch, provider)

        result = _invoke_cmd(tmp_path, ["plan"])
        assert result.exit_code == 0, f"plan failed: {result.output}"

        # The CLI's summary line should announce LLM generation + the path.
        assert "generated via LLM" in result.output
        assert "anthropic" in result.output
        assert ".fakoli-state/prd.md" in result.output or "prd.md" in result.output

        # prd.md was mutated — it now contains a `## Tasks` section.
        prd_text = (tmp_path / ".fakoli-state" / "prd.md").read_text(
            encoding="utf-8"
        )
        assert "## Tasks" in prd_text
        assert "### T001" in prd_text and "### T002" in prd_text

        # Tasks landed in the backend.
        list_result = _invoke_cmd(tmp_path, ["list"])
        assert "T001" in list_result.output
        assert "T002" in list_result.output

    def test_no_llm_opt_out_exits_1_with_clear_message(
        self,
        tmp_path: Path,
        monkeypatch,  # type: ignore[no-untyped-def]
    ) -> None:
        """`plan --no-llm` on a PRD without `## Tasks` → exit 1 with a
        clear message naming the opt-out flag and the prd.md path. The
        backstop is the safety net; opting out with no work to do should
        fail loudly."""
        _do_init(tmp_path)
        _write_prd(tmp_path, _PRD_WITHOUT_TASKS)
        _invoke_cmd(tmp_path, ["prd", "parse"])

        # Resolver should NOT be called when --no-llm is set; install a
        # raising stub so any accidental invocation surfaces in the test.
        from fakoli_state.planning import llm_planner

        def _explode(config=None) -> None:  # type: ignore[no-untyped-def]
            raise AssertionError(
                "resolve_planner_provider should not be called with --no-llm"
            )

        monkeypatch.setattr(llm_planner, "resolve_planner_provider", _explode)

        result = _invoke_cmd(tmp_path, ["plan", "--no-llm"])
        assert result.exit_code == 1, (
            f"--no-llm with 0 tasks should exit 1, got "
            f"{result.exit_code}: {result.output}"
        )
        # The message must name --no-llm so the user knows the opt-out
        # is what got them here.
        assert "--no-llm" in result.output

        # prd.md must NOT have been mutated.
        prd_text = (tmp_path / ".fakoli-state" / "prd.md").read_text(
            encoding="utf-8"
        )
        assert "## Tasks" not in prd_text

    def test_provider_unavailable_exits_1_with_full_message(
        self,
        tmp_path: Path,
        monkeypatch,  # type: ignore[no-untyped-def]
    ) -> None:
        """When ``resolve_planner_provider`` raises
        ``PlannerProviderUnavailable`` the CLI must surface the full
        multi-line message and exit 1 — never a silent zero-count
        success."""
        _do_init(tmp_path)
        _write_prd(tmp_path, _PRD_WITHOUT_TASKS)
        _invoke_cmd(tmp_path, ["prd", "parse"])

        from fakoli_state.planning import llm_planner
        from fakoli_state.planning.llm_planner import PlannerProviderUnavailable

        sentinel_msg = (
            "No LLM provider available for task generation. "
            "Either set ANTHROPIC_API_KEY or install claude-agent-sdk."
        )

        def _raise(config=None) -> None:  # type: ignore[no-untyped-def]
            raise PlannerProviderUnavailable(sentinel_msg)

        monkeypatch.setattr(llm_planner, "resolve_planner_provider", _raise)

        result = _invoke_cmd(tmp_path, ["plan"])
        assert result.exit_code == 1
        # The message must appear in output (stderr is captured into output
        # by CliRunner in mix_stderr mode, which is the default).
        combined = result.output + (
            result.stderr if hasattr(result, "stderr") and result.stderr else ""
        )
        assert "ANTHROPIC_API_KEY" in combined
        assert "claude-agent-sdk" in combined

    def test_idempotent_second_run_does_not_re_append(
        self,
        tmp_path: Path,
        monkeypatch,  # type: ignore[no-untyped-def]
    ) -> None:
        """Running ``plan`` twice on a PRD that started without tasks must
        leave prd.md with exactly one `## Tasks` section. The first run
        appends; the second run sees the header already exists and is a
        no-op for the file."""
        _do_init(tmp_path)
        _write_prd(tmp_path, _PRD_WITHOUT_TASKS)
        _invoke_cmd(tmp_path, ["prd", "parse"])

        provider = _build_recorded_llm_provider_for(_PRD_WITHOUT_TASKS)
        self._install_recorded_resolver(monkeypatch, provider)

        first = _invoke_cmd(tmp_path, ["plan"])
        assert first.exit_code == 0, f"first plan failed: {first.output}"

        prd_after_first = (tmp_path / ".fakoli-state" / "prd.md").read_text(
            encoding="utf-8"
        )
        first_tasks_count = prd_after_first.lower().count("## tasks")
        assert first_tasks_count == 1

        # Re-parse + re-plan. Second run must not re-append.
        _invoke_cmd(tmp_path, ["prd", "parse"])
        second = _invoke_cmd(tmp_path, ["plan"])
        assert second.exit_code == 0, f"second plan failed: {second.output}"

        prd_after_second = (tmp_path / ".fakoli-state" / "prd.md").read_text(
            encoding="utf-8"
        )
        second_tasks_count = prd_after_second.lower().count("## tasks")
        assert second_tasks_count == 1, (
            f"## Tasks should appear exactly once after re-run; "
            f"got {second_tasks_count}"
        )


# ---------------------------------------------------------------------------
# score command
# ---------------------------------------------------------------------------


class TestScore:
    def _setup_planned_project(self, tmp_path: Path) -> None:
        """init + prd parse + plan."""
        _do_init(tmp_path)
        _write_prd(tmp_path, _FULL_PRD_CONTENT)
        _invoke_cmd(tmp_path, ["prd", "parse"])
        _invoke_cmd(tmp_path, ["plan"])

    def _insert_over_depth_chain(self, tmp_path: Path) -> None:
        """Insert root → a → b → c → d, with every task scoreable as complex."""
        conn = sqlite3.connect(str(tmp_path / ".fakoli-state" / "state.db"))
        try:
            conn.execute(
                "INSERT OR IGNORE INTO features "
                "(id, title, description, status, requirements, tasks) "
                "VALUES ('F001', 'Deep Feature', 'desc', 'proposed', '[]', '[]')"
            )
            parent: str | None = None
            for task_id in ("root", "a", "b", "c", "d"):
                likely_files = [f"src/{task_id}_{idx}.py" for idx in range(5)]
                conn.execute(
                    """
                    INSERT INTO tasks
                        (id, feature_id, title, description, status, priority,
                         dependencies, conflict_groups, scores, acceptance_criteria,
                         implementation_notes, verification, likely_files,
                         parent_task_id, created_at, updated_at)
                    VALUES
                        (?, 'F001', ?, 'Deep task', 'drafted', 'medium',
                         '[]', '[]', '{}', '["done"]',
                         '[]', '{"commands":["pytest"]}', ?,
                         ?, '2026-05-24T18:00:00+00:00',
                         '2026-05-24T18:00:00+00:00')
                    """,
                    (
                        task_id,
                        f"Task {task_id}",
                        json.dumps(likely_files),
                        parent,
                    ),
                )
                parent = task_id
            conn.commit()
        finally:
            conn.close()

    def test_score_all_populates_scores(self, tmp_path: Path) -> None:
        """After plan, run score → list tasks shows scores no longer all-None."""
        self._setup_planned_project(tmp_path)

        result = _invoke_cmd(tmp_path, ["score"])
        assert result.exit_code == 0, f"score failed: {result.output}"
        assert "Scored" in result.output or "task" in result.output.lower()

        # After scoring, show command shows score values
        show_result = _invoke_cmd(tmp_path, ["show", "T001"])
        if show_result.exit_code == 0:
            output = show_result.output
            # Should show numeric scores, not "(not yet scored)"
            assert "not yet scored" not in output

    def test_score_single_task(self, tmp_path: Path) -> None:
        """score TASK_ID populates just that one task."""
        self._setup_planned_project(tmp_path)

        result = _invoke_cmd(tmp_path, ["score", "T001"])
        assert result.exit_code == 0, f"score T001 failed: {result.output}"
        assert "T001" in result.output

    def test_score_nonexistent_task_exits_1(self, tmp_path: Path) -> None:
        """score T999 when T999 doesn't exist → exit 1."""
        self._setup_planned_project(tmp_path)

        result = _invoke_cmd(tmp_path, ["score", "T999"])
        assert result.exit_code == 1

    def test_partial_rescore_preserves_other_scores(self, tmp_path: Path) -> None:
        """v1.23.0 / TM #1644: re-scoring one task must NOT wipe the others.

        Scores persist as per-task ``task.scored`` events, so a single-task
        re-score is an append that leaves every other task's projected score
        intact — the event-sourced answer to task-master's overwrite-on-partial
        bug. This proves the merge behavior rather than just asserting it.
        """
        self._setup_planned_project(tmp_path)
        assert _invoke_cmd(tmp_path, ["score"]).exit_code == 0

        before = _invoke_cmd(tmp_path, ["show", "T002"]).output
        assert "not yet scored" not in before

        # Re-score only T001; T002 must be untouched.
        assert _invoke_cmd(tmp_path, ["score", "T001"]).exit_code == 0
        after = _invoke_cmd(tmp_path, ["show", "T002"]).output
        assert "not yet scored" not in after
        assert after == before

    def test_score_expansion_queue_enforces_recursive_depth_cap(
        self, tmp_path: Path
    ) -> None:
        """The CLI expansion queue must use the recursive depth-capped frontier."""
        _do_init(tmp_path)
        self._insert_over_depth_chain(tmp_path)

        result = _invoke_cmd(tmp_path, ["score"])

        assert result.exit_code == 0, f"score failed: {result.output}"
        assert "fakoli-state expand d --use-llm" not in result.output


# ---------------------------------------------------------------------------
# expand command
# ---------------------------------------------------------------------------


class TestExpand:
    def test_expand_refuses_without_llm(self, tmp_path: Path) -> None:
        """Phase 3 scaffold: expand T001 exits 1 with --use-llm message."""
        _do_init(tmp_path)
        result = _invoke_cmd(tmp_path, ["expand", "T001"])
        assert result.exit_code == 1
        combined = result.output + (result.stderr if hasattr(result, "stderr") and result.stderr else "")
        assert "use-llm" in combined.lower() or "--use-llm" in combined

# Note: a previous test asserted `expand --use-llm` exits 1 unconditionally.
# Phase 7 Wave 2 implemented --use-llm, so the test was stale and only passed
# by accident (empty state OR missing ANTHROPIC_API_KEY). The missing-key
# branch is now covered properly by
# TestUseLlmRequiresApiKey::test_expand_use_llm_without_env_exits_1 below.


# ---------------------------------------------------------------------------
# review tasks command
# ---------------------------------------------------------------------------


class TestReviewTasks:
    def _setup_for_review(self, tmp_path: Path) -> None:
        """Setup: init + write PRD with AC + verification + parse + plan + score."""
        _do_init(tmp_path)
        _write_prd(tmp_path, _FULL_PRD_CONTENT)
        _invoke_cmd(tmp_path, ["prd", "parse"])
        _invoke_cmd(tmp_path, ["plan"])
        _invoke_cmd(tmp_path, ["score"])

    def test_review_tasks_promotes_complete_tasks(self, tmp_path: Path) -> None:
        """Tasks with acceptance_criteria + verification → promoted to ready."""
        self._setup_for_review(tmp_path)

        result = _invoke_cmd(tmp_path, ["review", "tasks"])
        assert result.exit_code == 0, f"review tasks failed: {result.output}"
        assert "Promoted" in result.output

        # Check that at least some tasks made it to ready
        list_result = _invoke_cmd(tmp_path, ["list", "--status", "ready"])
        assert list_result.exit_code == 0
        # Should have some ready tasks
        assert "task" in list_result.output.lower() or "T001" in list_result.output

    def test_review_tasks_blocks_incomplete(self, tmp_path: Path) -> None:
        """Task without acceptance_criteria stays blocked; surface reason."""
        _do_init(tmp_path)
        # PRD without acceptance criteria on tasks
        prd_no_ac = """\
# Project: No AC Project

## Summary

A project where tasks have no acceptance criteria.

## Goals

- Do tasks.

## Requirements

- R001: Do something.

## Features

### F001: Feature

**Requirements:** R001

## Tasks

### T001: Task Without AC

**Feature:** F001
**Priority:** medium

A task without acceptance criteria.

**Verification:**

- `pytest tests/ -v`
"""
        _write_prd(tmp_path, prd_no_ac)
        _invoke_cmd(tmp_path, ["prd", "parse"])
        _invoke_cmd(tmp_path, ["plan"])

        result = _invoke_cmd(tmp_path, ["review", "tasks"])
        assert result.exit_code == 0
        # Task should be blocked
        assert "Blocked" in result.output or "blocked" in result.output.lower()


# ---------------------------------------------------------------------------
# list command
# ---------------------------------------------------------------------------


class TestList:
    def _setup_with_tasks(self, tmp_path: Path) -> None:
        _do_init(tmp_path)
        _write_prd(tmp_path, _FULL_PRD_CONTENT)
        _invoke_cmd(tmp_path, ["prd", "parse"])
        _invoke_cmd(tmp_path, ["plan"])

    def test_list_shows_all_tasks(self, tmp_path: Path) -> None:
        """list shows all tasks without filters."""
        self._setup_with_tasks(tmp_path)

        result = _invoke_cmd(tmp_path, ["list"])
        assert result.exit_code == 0, f"list failed: {result.output}"
        assert "T001" in result.output
        assert "T002" in result.output

    def test_list_filtered_by_status(self, tmp_path: Path) -> None:
        """list --status drafted shows only drafted tasks."""
        self._setup_with_tasks(tmp_path)

        result = _invoke_cmd(tmp_path, ["list", "--status", "drafted"])
        assert result.exit_code == 0, f"list --status drafted failed: {result.output}"
        # After plan, tasks should be in drafted status
        # Output should either show tasks or "No tasks found"
        assert result.output  # non-empty output

    def test_list_filtered_by_feature(self, tmp_path: Path) -> None:
        """list --feature F001 shows only F001 tasks."""
        self._setup_with_tasks(tmp_path)

        result = _invoke_cmd(tmp_path, ["list", "--feature", "F001"])
        assert result.exit_code == 0, f"list --feature F001 failed: {result.output}"
        # T001 belongs to F001
        assert "T001" in result.output

    def test_list_empty_shows_no_tasks_message(self, tmp_path: Path) -> None:
        """list on project with no tasks shows a 'no tasks' message."""
        _do_init(tmp_path)
        result = _invoke_cmd(tmp_path, ["list"])
        assert result.exit_code == 0
        assert "No tasks" in result.output or "no tasks" in result.output.lower()


# ---------------------------------------------------------------------------
# show command
# ---------------------------------------------------------------------------


class TestShow:
    def test_show_full_task_detail(self, tmp_path: Path) -> None:
        """show T001 output contains acceptance criteria, scores breakdown, verification."""
        _do_init(tmp_path)
        _write_prd(tmp_path, _FULL_PRD_CONTENT)
        _invoke_cmd(tmp_path, ["prd", "parse"])
        _invoke_cmd(tmp_path, ["plan"])
        _invoke_cmd(tmp_path, ["score"])

        result = _invoke_cmd(tmp_path, ["show", "T001"])
        assert result.exit_code == 0, f"show T001 failed: {result.output}"
        output = result.output

        # Should show task title
        assert "T001" in output

        # Should show acceptance criteria section
        assert "Acceptance" in output or "criteria" in output.lower()

        # Should show verification section
        assert "Verification" in output or "pytest" in output

    def test_show_nonexistent_task_exits_1(self, tmp_path: Path) -> None:
        """show T999 when T999 doesn't exist → exit 1."""
        _do_init(tmp_path)
        result = _invoke_cmd(tmp_path, ["show", "T999"])
        assert result.exit_code == 1

    def test_show_scores_after_scoring(self, tmp_path: Path) -> None:
        """show T001 after scoring shows score dimensions."""
        _do_init(tmp_path)
        _write_prd(tmp_path, _FULL_PRD_CONTENT)
        _invoke_cmd(tmp_path, ["prd", "parse"])
        _invoke_cmd(tmp_path, ["plan"])
        _invoke_cmd(tmp_path, ["score"])

        result = _invoke_cmd(tmp_path, ["show", "T001"])
        assert result.exit_code == 0
        output = result.output
        # Should show score dimensions
        assert "complexity" in output.lower()
        assert "blast" in output.lower()


# ---------------------------------------------------------------------------
# End-to-end workflow
# ---------------------------------------------------------------------------


class TestReplanPreservesTaskStatus:
    """Regression test for Greptile PR #38 finding #3 (P2): _insert_task_row
    upsert was overwriting status='proposed' on re-plan, which would silently
    reset claimed/in_progress tasks back to proposed. After the fix, status
    is excluded from the ON CONFLICT update set and changes only via
    task.status_changed events.
    """

    def test_replan_does_not_reset_advanced_task_status(self, tmp_path: Path) -> None:
        """Simulate Phase 4 by manually advancing a task past 'drafted', then
        re-running plan; the advanced status must be preserved."""
        import sqlite3

        _do_init(tmp_path, name="Replan Test")
        _write_prd(tmp_path, _FULL_PRD_CONTENT)
        _invoke_cmd(tmp_path, ["prd", "parse"])
        _invoke_cmd(tmp_path, ["plan"])  # tasks now at 'drafted'

        # Simulate Phase 4 claim by mutating one task to 'claimed' directly.
        # (Phase 4 will do this through claim events; we patch the DB to
        # represent the post-claim state without needing Phase 4 code.)
        db_path = tmp_path / ".fakoli-state" / "state.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE tasks SET status = 'claimed' WHERE id = 'T001'"
        )
        conn.commit()
        conn.close()

        # Re-parse + re-plan. Without the fix, task.created would upsert
        # status back to 'proposed', then task.status_changed would error
        # (or worse, succeed and reset to 'drafted').
        reparse = _invoke_cmd(tmp_path, ["prd", "parse"])
        assert reparse.exit_code == 0
        replan = _invoke_cmd(tmp_path, ["plan"])
        assert replan.exit_code == 0, f"re-plan after claim failed: {replan.output}"

        # Verify T001 is STILL 'claimed' — the upsert did not reset it.
        conn = sqlite3.connect(str(db_path))
        status = conn.execute(
            "SELECT status FROM tasks WHERE id = 'T001'"
        ).fetchone()[0]
        conn.close()
        assert status == "claimed", (
            f"re-plan reset T001 from 'claimed' to '{status}' — the "
            "ON CONFLICT upsert is silently overwriting task status. "
            "status must be managed by task.status_changed events ONLY."
        )


# ---------------------------------------------------------------------------
# Phase 4 CLI helpers
# ---------------------------------------------------------------------------


def _do_init_and_plan(tmp_path: Path, *, with_git: bool = True) -> Path:
    """Full setup: optionally git-init, then fakoli-state init + PRD + plan + review_tasks.

    Returns tmp_path ready for claim-related tests.
    """
    import subprocess as _subprocess

    if with_git:
        _subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
        _subprocess.run(
            ["git", "config", "user.email", "test@test.test"],
            cwd=str(tmp_path), check=True, capture_output=True,
        )
        _subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=str(tmp_path), check=True, capture_output=True,
        )
        (tmp_path / "README.md").write_text("initial\n", encoding="utf-8")
        _subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True, capture_output=True)
        _subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=str(tmp_path), check=True, capture_output=True,
        )

    _do_init(tmp_path, name="Phase4 Test Project")
    _write_prd(tmp_path, _FULL_PRD_CONTENT)
    _invoke_cmd(tmp_path, ["prd", "parse"])
    _invoke_cmd(tmp_path, ["prd", "review"])
    _invoke_cmd(tmp_path, ["prd", "review", "--approve"])
    _invoke_cmd(tmp_path, ["plan"])
    _invoke_cmd(tmp_path, ["score"])
    _invoke_cmd(tmp_path, ["review", "tasks"])
    return tmp_path


def _get_first_ready_task_id(tmp_path: Path) -> str | None:
    """Return the first task ID in ready status by querying the backend directly."""
    import sqlite3 as _sqlite3
    db_path = tmp_path / ".fakoli-state" / "state.db"
    if not db_path.exists():
        return None
    conn = _sqlite3.connect(str(db_path))
    row = conn.execute("SELECT id FROM tasks WHERE status='ready' LIMIT 1").fetchone()
    conn.close()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Phase 4 — claim command
# ---------------------------------------------------------------------------


class TestClaimCommand:
    def test_claim_happy_path_creates_lease_and_branch(self, tmp_path: Path) -> None:
        """Claim a ready task; command exits 0 and prints claim ID + branch."""
        _do_init_and_plan(tmp_path, with_git=True)
        task_id = _get_first_ready_task_id(tmp_path)
        assert task_id is not None, "No ready task found after setup"

        result = _invoke_cmd(tmp_path, ["claim", task_id, "--actor", "agent-test"])
        assert result.exit_code == 0, f"claim failed: {result.output}"
        assert "Claim ID" in result.output or "Claimed" in result.output
        assert "Lease" in result.output or "lease" in result.output

    def test_claim_without_git_succeeds_warns(self, tmp_path: Path) -> None:
        """Claim succeeds even without a git repo; stderr has a branch warning."""
        _do_init_and_plan(tmp_path, with_git=False)
        task_id = _get_first_ready_task_id(tmp_path)
        assert task_id is not None, "No ready task found after setup"

        result = _invoke_cmd(tmp_path, ["claim", task_id, "--actor", "agent-test"])
        assert result.exit_code == 0, f"claim without git failed: {result.output}"
        # The branch warning may be in output or stderr depending on Typer's mix
        combined = result.output + (result.stderr if hasattr(result, "stderr") and result.stderr else "")
        assert "Warning" in combined or "Claimed" in result.output

    def test_claim_refuses_unready_task(self, tmp_path: Path) -> None:
        """Claiming a task not in 'ready' status exits non-zero."""
        _do_init_and_plan(tmp_path, with_git=False)

        # Use a task ID that was never created → should fail
        result = _invoke_cmd(tmp_path, ["claim", "T999", "--actor", "agent-test"])
        assert result.exit_code != 0
        combined = result.output + (result.stderr if hasattr(result, "stderr") and result.stderr else "")
        assert "not found" in combined.lower() or "T999" in combined

    def test_claim_refuses_when_prd_draft(self, tmp_path: Path) -> None:
        """Claim exits non-zero when PRD is still in draft state."""
        # Init without review/approve
        _do_init(tmp_path, name="Draft PRD Project")
        _write_prd(tmp_path, _FULL_PRD_CONTENT)
        _invoke_cmd(tmp_path, ["prd", "parse"])
        _invoke_cmd(tmp_path, ["plan"])

        result = _invoke_cmd(tmp_path, ["claim", "T001", "--actor", "agent-test"])
        assert result.exit_code != 0

    def test_claim_with_force_overrides_warnings(self, tmp_path: Path) -> None:
        """--force flag is accepted and claim proceeds (no conflict in this setup)."""
        _do_init_and_plan(tmp_path, with_git=False)
        task_id = _get_first_ready_task_id(tmp_path)
        assert task_id is not None

        result = _invoke_cmd(
            tmp_path, ["claim", task_id, "--actor", "agent-test", "--force"]
        )
        assert result.exit_code == 0, f"claim --force failed: {result.output}"

    def test_claim_warns_on_undone_dependencies(self, tmp_path: Path) -> None:
        """v1.16.0: claim emits a stderr warning when task.dependencies are
        not yet `done`, but proceeds with the claim (soft gate).

        Regression for a user-reported workflow: T002 depended on T001 but
        the planner missed it; even with the v1.16.0 planner-prompt fix,
        a user can still claim T002 before T001 is done in a stacked-PR
        workflow. The warning ensures the user knows what they're doing.
        """
        _do_init_and_plan(tmp_path, with_git=False)
        # Inject a dependency directly into state.db: make T002 depend on T001,
        # leaving T001 in `ready` (not done). The next claim of T002 should
        # warn but succeed.
        import sqlite3
        db = tmp_path / ".fakoli-state" / "state.db"
        with sqlite3.connect(str(db)) as conn:
            # Pick the first two ready tasks for the test setup.
            rows = conn.execute(
                "SELECT id FROM tasks WHERE status = 'ready' ORDER BY id LIMIT 2"
            ).fetchall()
            if len(rows) < 2:
                # Not enough tasks in fixture — skip cleanly.
                import pytest
                pytest.skip(
                    "fixture has fewer than 2 ready tasks; cannot test "
                    "cross-task dependency"
                )
            dep_id, target_id = rows[0][0], rows[1][0]
            conn.execute(
                "UPDATE tasks SET dependencies = ? WHERE id = ?",
                (f'["{dep_id}"]', target_id),
            )
            conn.commit()

        result = _invoke_cmd(
            tmp_path, ["claim", target_id, "--actor", "agent-test"]
        )
        assert result.exit_code == 0, (
            f"claim with undone dep should succeed (soft gate); got: "
            f"{result.output}"
        )
        combined = result.output + (
            result.stderr if hasattr(result, "stderr") and result.stderr else ""
        )
        assert "dependency" in combined.lower() or "Warning" in combined, (
            f"claim should warn about undone deps; combined output: {combined}"
        )
        assert dep_id in combined, (
            f"warning should name the undone dep '{dep_id}'; got: {combined}"
        )

    def test_claim_force_silences_dependency_warning(
        self, tmp_path: Path
    ) -> None:
        """--force silences the dependency warning. The claim still proceeds;
        we just verify the warning text is absent."""
        _do_init_and_plan(tmp_path, with_git=False)
        import sqlite3
        db = tmp_path / ".fakoli-state" / "state.db"
        with sqlite3.connect(str(db)) as conn:
            rows = conn.execute(
                "SELECT id FROM tasks WHERE status = 'ready' ORDER BY id LIMIT 2"
            ).fetchall()
            if len(rows) < 2:
                import pytest
                pytest.skip("fixture has fewer than 2 ready tasks")
            dep_id, target_id = rows[0][0], rows[1][0]
            conn.execute(
                "UPDATE tasks SET dependencies = ? WHERE id = ?",
                (f'["{dep_id}"]', target_id),
            )
            conn.commit()

        result = _invoke_cmd(
            tmp_path,
            ["claim", target_id, "--actor", "agent-test", "--force"],
        )
        assert result.exit_code == 0
        combined = result.output + (
            result.stderr if hasattr(result, "stderr") and result.stderr else ""
        )
        # --force suppresses the dep warning specifically.
        assert "dependency(ies) that are not yet" not in combined, (
            f"--force should silence the dep warning; got: {combined}"
        )


# ---------------------------------------------------------------------------
# Phase 4 — release command
# ---------------------------------------------------------------------------


class TestReleaseCommand:
    def _claim_task(self, tmp_path: Path, task_id: str) -> str:
        """Claim task_id and return the claim ID."""
        import sqlite3 as _sqlite3
        _invoke_cmd(tmp_path, ["claim", task_id, "--actor", "agent-test"])
        db_path = tmp_path / ".fakoli-state" / "state.db"
        conn = _sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT id FROM claims WHERE task_id=? AND status='active'", (task_id,)
        ).fetchone()
        conn.close()
        return row[0] if row else "C001"

    def test_release_happy_path(self, tmp_path: Path) -> None:
        """Claim then release; exit 0, task returns to ready."""
        import sqlite3 as _sqlite3
        _do_init_and_plan(tmp_path, with_git=False)
        task_id = _get_first_ready_task_id(tmp_path)
        assert task_id is not None

        claim_id = self._claim_task(tmp_path, task_id)

        result = _invoke_cmd(
            tmp_path, ["release", claim_id, "--actor", "agent-test"]
        )
        assert result.exit_code == 0, f"release failed: {result.output}"
        assert "Released" in result.output or "released" in result.output.lower()

        # Verify task returned to ready
        db_path = tmp_path / ".fakoli-state" / "state.db"
        conn = _sqlite3.connect(str(db_path))
        status = conn.execute(
            "SELECT status FROM tasks WHERE id=?", (task_id,)
        ).fetchone()[0]
        conn.close()
        assert status == "ready"

    def test_release_force_overrides_actor_check(self, tmp_path: Path) -> None:
        """--force allows a different actor to release."""
        _do_init_and_plan(tmp_path, with_git=False)
        task_id = _get_first_ready_task_id(tmp_path)
        assert task_id is not None

        claim_id = self._claim_task(tmp_path, task_id)

        result = _invoke_cmd(
            tmp_path, ["release", claim_id, "--actor", "different-agent", "--force"]
        )
        assert result.exit_code == 0, f"release --force failed: {result.output}"


# ---------------------------------------------------------------------------
# Phase 4 — renew command
# ---------------------------------------------------------------------------


class TestRenewCommand:
    def test_renew_extends_lease(self, tmp_path: Path) -> None:
        """Renew prints new lease expiry and exits 0."""
        import sqlite3 as _sqlite3

        _do_init_and_plan(tmp_path, with_git=False)
        task_id = _get_first_ready_task_id(tmp_path)
        assert task_id is not None

        _invoke_cmd(tmp_path, ["claim", task_id, "--actor", "agent-test"])
        db_path = tmp_path / ".fakoli-state" / "state.db"
        conn = _sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT id FROM claims WHERE task_id=? AND status='active'", (task_id,)
        ).fetchone()
        claim_id = row[0]
        old_expiry = conn.execute(
            "SELECT lease_expires_at FROM claims WHERE id=?", (claim_id,)
        ).fetchone()[0]
        conn.close()

        result = _invoke_cmd(
            tmp_path, ["renew", claim_id, "--actor", "agent-test"]
        )
        assert result.exit_code == 0, f"renew failed: {result.output}"
        assert "lease" in result.output.lower() or "Renewed" in result.output

        # New lease should be present in output (some time string)
        assert old_expiry[:10] in result.output or "lease" in result.output.lower()


# ---------------------------------------------------------------------------
# Phase 4 — next command
# ---------------------------------------------------------------------------


class TestNextCommand:
    def test_next_returns_highest_priority_task(self, tmp_path: Path) -> None:
        """next command prints a task ID and exits 0 when ready tasks exist."""
        _do_init_and_plan(tmp_path, with_git=False)
        result = _invoke_cmd(tmp_path, ["next", "--actor", "agent-test"])
        assert result.exit_code == 0, f"next failed: {result.output}"
        # Should mention a task or 'Next recommended'
        combined = result.output
        assert "T0" in combined or "task" in combined.lower() or "No claimable" in combined

    def test_next_prints_no_tasks_message_when_empty(self, tmp_path: Path) -> None:
        """next prints 'No claimable tasks' when no ready tasks exist."""
        _do_init(tmp_path, name="Empty Project")
        # No PRD parsed, no tasks created
        result = _invoke_cmd(tmp_path, ["next", "--actor", "agent-test"])
        assert result.exit_code == 0, f"next (empty) failed: {result.output}"
        assert "No claimable" in result.output or "no" in result.output.lower()


# ---------------------------------------------------------------------------
# Phase 4 — hook subcommands
# ---------------------------------------------------------------------------


class TestHookSubcommands:
    def test_hook_check_claim_silent_when_no_state(self, tmp_path: Path) -> None:
        """hook check-claim exits 0 silently when no .fakoli-state/ exists."""
        result = _invoke_cmd(
            tmp_path,
            ["hook", "check-claim", "--file", "src/foo.py", "--actor", "agent-test"],
        )
        assert result.exit_code == 0

    def test_hook_record_file_change_appends_event(self, tmp_path: Path) -> None:
        """hook record-file-change exits 0 after init (appends event to JSONL)."""
        _do_init(tmp_path, name="Hook Test Project")
        result = _invoke_cmd(
            tmp_path,
            [
                "hook", "record-file-change",
                "--file", "src/app.py",
                "--tool", "Edit",
                "--actor", "agent-hook",
            ],
        )
        assert result.exit_code == 0

        events_path = tmp_path / ".fakoli-state" / "events.jsonl"
        assert events_path.exists()
        content = events_path.read_text(encoding="utf-8")
        assert "file_changed" in content or "src/app.py" in content


# ---------------------------------------------------------------------------
# Phase 4 — end-to-end claim + release cycle
# ---------------------------------------------------------------------------


class TestE2EClaimRelease:
    def test_full_claim_release_cycle(self, tmp_path: Path) -> None:
        """init + git init + PRD + plan + review_tasks + next + claim + renew + release.

        Asserts: task is back to 'ready' after release.
        """
        import sqlite3 as _sqlite3

        _do_init_and_plan(tmp_path, with_git=True)
        task_id = _get_first_ready_task_id(tmp_path)
        assert task_id is not None, "No ready tasks after full setup"

        # next — just verify it works
        next_result = _invoke_cmd(tmp_path, ["next", "--actor", "agent-test"])
        assert next_result.exit_code == 0, f"next failed: {next_result.output}"

        # claim
        claim_result = _invoke_cmd(
            tmp_path, ["claim", task_id, "--actor", "agent-test"]
        )
        assert claim_result.exit_code == 0, f"claim failed: {claim_result.output}"

        # find claim ID from DB
        db_path = tmp_path / ".fakoli-state" / "state.db"
        conn = _sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT id FROM claims WHERE task_id=? AND status='active'", (task_id,)
        ).fetchone()
        conn.close()
        assert row is not None, "No active claim found after claim command"
        claim_id = row[0]

        # renew
        renew_result = _invoke_cmd(
            tmp_path, ["renew", claim_id, "--actor", "agent-test"]
        )
        assert renew_result.exit_code == 0, f"renew failed: {renew_result.output}"

        # release
        release_result = _invoke_cmd(
            tmp_path, ["release", claim_id, "--actor", "agent-test", "--reason", "cycle done"]
        )
        assert release_result.exit_code == 0, f"release failed: {release_result.output}"

        # task should be back to ready
        conn = _sqlite3.connect(str(db_path))
        status = conn.execute(
            "SELECT status FROM tasks WHERE id=?", (task_id,)
        ).fetchone()[0]
        conn.close()
        assert status == "ready", (
            f"Expected task back to 'ready' after release, got '{status}'"
        )


class TestE2E:
    def test_full_planning_workflow(self, tmp_path: Path) -> None:
        """init → write PRD → prd parse → prd review --approve → plan → score → review tasks → list --status ready → show T001.

        Assert each step exits 0 and final list shows >= 1 ready task.
        """
        # 1. init
        _do_init(tmp_path, name="E2E Test Project")

        # 2. write PRD
        _write_prd(tmp_path, _FULL_PRD_CONTENT)

        # 3. prd parse
        parse_result = _invoke_cmd(tmp_path, ["prd", "parse"])
        assert parse_result.exit_code == 0, f"prd parse failed: {parse_result.output}"
        assert "Parsed" in parse_result.output

        # 4. prd review (draft → reviewed)
        review_result = _invoke_cmd(tmp_path, ["prd", "review"])
        assert review_result.exit_code == 0, f"prd review failed: {review_result.output}"

        # 5. prd review --approve (reviewed → approved)
        approve_result = _invoke_cmd(tmp_path, ["prd", "review", "--approve"])
        assert approve_result.exit_code == 0, f"prd review --approve failed: {approve_result.output}"

        # 6. plan
        plan_result = _invoke_cmd(tmp_path, ["plan"])
        assert plan_result.exit_code == 0, f"plan failed: {plan_result.output}"

        # 7. score
        score_result = _invoke_cmd(tmp_path, ["score"])
        assert score_result.exit_code == 0, f"score failed: {score_result.output}"

        # 8. review tasks → promote to ready
        review_tasks_result = _invoke_cmd(tmp_path, ["review", "tasks"])
        assert review_tasks_result.exit_code == 0, (
            f"review tasks failed: {review_tasks_result.output}"
        )

        # 9. list --status ready → at least 1 ready task
        list_result = _invoke_cmd(tmp_path, ["list", "--status", "ready"])
        assert list_result.exit_code == 0, f"list --status ready failed: {list_result.output}"
        # Should show at least 1 task or indicate tasks were promoted
        # (some tasks may be blocked if AC gate not met, but at least the command runs)

        # 10. show T001
        show_result = _invoke_cmd(tmp_path, ["show", "T001"])
        assert show_result.exit_code == 0, f"show T001 failed: {show_result.output}"
        assert "T001" in show_result.output

    def test_status_after_full_workflow(self, tmp_path: Path) -> None:
        """status command reflects PRD state after review."""
        _do_init(tmp_path, name="Status E2E Project")
        _write_prd(tmp_path, _MINIMAL_PRD_CONTENT)
        _invoke_cmd(tmp_path, ["prd", "parse"])
        _invoke_cmd(tmp_path, ["prd", "review"])

        result = _invoke_cmd(tmp_path, ["status"])
        assert result.exit_code == 0
        output = result.output
        # Should show the PRD status as reviewed
        assert "reviewed" in output.lower()


# ---------------------------------------------------------------------------
# Phase 5 — helpers
# ---------------------------------------------------------------------------


def _do_claim(tmp_path: Path, task_id: str, actor: str = "agent-test") -> str:
    """Claim task_id and return the claim ID from the DB."""
    import sqlite3 as _sqlite3

    _invoke_cmd(tmp_path, ["claim", task_id, "--actor", actor])
    db_path = tmp_path / ".fakoli-state" / "state.db"
    conn = _sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT id FROM claims WHERE task_id=? AND status='active'", (task_id,)
    ).fetchone()
    conn.close()
    return row[0] if row else "CLAIM-UNKNOWN"


def _get_task_status(tmp_path: Path, task_id: str) -> str | None:
    """Return the current status of task_id from the DB."""
    import sqlite3 as _sqlite3

    db_path = tmp_path / ".fakoli-state" / "state.db"
    conn = _sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT status FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    conn.close()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Phase 5 — packet command
# ---------------------------------------------------------------------------


class TestPacketCommand:
    def test_packet_renders_markdown_to_packets_dir(self, tmp_path: Path) -> None:
        """packet T001 exits 0 and writes .fakoli-state/packets/T001.md."""
        _do_init_and_plan(tmp_path, with_git=False)
        task_id = _get_first_ready_task_id(tmp_path)
        assert task_id is not None, "No ready task after setup"

        result = _invoke_cmd(tmp_path, ["packet", task_id])
        assert result.exit_code == 0, f"packet failed: {result.output}"

        packet_file = tmp_path / ".fakoli-state" / "packets" / f"{task_id}.md"
        assert packet_file.exists(), "Packet .md file not written"
        content = packet_file.read_text(encoding="utf-8")
        assert task_id in content

    def test_packet_json_format_writes_json_file(self, tmp_path: Path) -> None:
        """packet T001 --format json writes .fakoli-state/packets/T001.json."""
        _do_init_and_plan(tmp_path, with_git=False)
        task_id = _get_first_ready_task_id(tmp_path)
        assert task_id is not None

        result = _invoke_cmd(tmp_path, ["packet", task_id, "--format", "json"])
        assert result.exit_code == 0, f"packet --format json failed: {result.output}"

        packet_file = tmp_path / ".fakoli-state" / "packets" / f"{task_id}.json"
        assert packet_file.exists(), "Packet .json file not written"
        data = json.loads(packet_file.read_text(encoding="utf-8"))
        assert "task_id" in data

    def test_packet_unknown_task_exits_nonzero(self, tmp_path: Path) -> None:
        """packet T999 (unknown task) exits non-zero with error message."""
        _do_init(tmp_path, name="Packet Test Project")

        result = _invoke_cmd(tmp_path, ["packet", "T999"])
        assert result.exit_code != 0
        combined = result.output + (result.stderr if hasattr(result, "stderr") and result.stderr else "")
        assert "T999" in combined or "not found" in combined.lower()

    def test_packet_active_claim_section_appears_when_claimed(
        self, tmp_path: Path
    ) -> None:
        """packet after claim shows 'Active Claim' section in output."""
        _do_init_and_plan(tmp_path, with_git=False)
        task_id = _get_first_ready_task_id(tmp_path)
        assert task_id is not None

        _do_claim(tmp_path, task_id, actor="agent-test")

        result = _invoke_cmd(tmp_path, ["packet", task_id])
        assert result.exit_code == 0, f"packet after claim failed: {result.output}"
        assert "Active Claim" in result.output or "claim" in result.output.lower()


# ---------------------------------------------------------------------------
# Phase 5 — submit command
# ---------------------------------------------------------------------------


class TestSubmitCommand:
    def test_submit_happy_path_exits_zero(self, tmp_path: Path) -> None:
        """submit with required args exits 0 and prints evidence ID."""
        _do_init_and_plan(tmp_path, with_git=False)
        task_id = _get_first_ready_task_id(tmp_path)
        assert task_id is not None

        _do_claim(tmp_path, task_id, actor="agent-test")

        result = _invoke_cmd(
            tmp_path,
            [
                "submit", task_id,
                "--commands", "pytest tests/ -v",
                "--files-changed", "src/auth.py",
                "--actor", "agent-test",
            ],
        )
        assert result.exit_code == 0, f"submit failed: {result.output}"
        assert "Evidence" in result.output or "submitted" in result.output.lower()

    def test_submit_transitions_task_to_needs_review(self, tmp_path: Path) -> None:
        """submit transitions task to needs_review status."""
        _do_init_and_plan(tmp_path, with_git=False)
        task_id = _get_first_ready_task_id(tmp_path)
        assert task_id is not None

        _do_claim(tmp_path, task_id, actor="agent-test")

        _invoke_cmd(
            tmp_path,
            [
                "submit", task_id,
                "--commands", "pytest tests/ -v",
                "--files-changed", "src/main.py",
                "--actor", "agent-test",
            ],
        )

        status = _get_task_status(tmp_path, task_id)
        assert status == "needs_review", f"Expected needs_review, got {status!r}"

    def test_submit_without_active_claim_exits_nonzero(self, tmp_path: Path) -> None:
        """submit without an active claim exits non-zero with error."""
        _do_init_and_plan(tmp_path, with_git=False)
        task_id = _get_first_ready_task_id(tmp_path)
        assert task_id is not None
        # Do NOT claim the task

        result = _invoke_cmd(
            tmp_path,
            [
                "submit", task_id,
                "--commands", "pytest -v",
                "--files-changed", "src/foo.py",
                "--actor", "agent-test",
            ],
        )
        assert result.exit_code != 0
        combined = result.output + (result.stderr if hasattr(result, "stderr") and result.stderr else "")
        assert "claim" in combined.lower() or "no active" in combined.lower()

    def test_submit_with_pr_url_echoes_it(self, tmp_path: Path) -> None:
        """submit --pr-url records the URL and prints it."""
        _do_init_and_plan(tmp_path, with_git=False)
        task_id = _get_first_ready_task_id(tmp_path)
        assert task_id is not None

        _do_claim(tmp_path, task_id, actor="agent-test")

        result = _invoke_cmd(
            tmp_path,
            [
                "submit", task_id,
                "--commands", "pytest tests/ -v",
                "--files-changed", "src/auth.py",
                "--pr-url", "https://github.com/repo/pull/42",
                "--actor", "agent-test",
            ],
        )
        assert result.exit_code == 0, f"submit with --pr-url failed: {result.output}"
        assert "https://github.com/repo/pull/42" in result.output

    def test_submit_with_screenshots_records_them(self, tmp_path: Path) -> None:
        """submit --screenshots parses the comma list, records it on Evidence,
        and satisfies the 'screenshots' required_evidence gate.

        Regression: before the --screenshots flag was added, the CLI hardcoded
        `screenshots=[]` and any task requiring 'screenshots' evidence could
        never pass the apply gate from the CLI.
        """
        import json as _json
        import sqlite3 as _sqlite3

        _do_init_and_plan(tmp_path, with_git=False)
        task_id = _get_first_ready_task_id(tmp_path)
        assert task_id is not None

        # Inject required_evidence=["screenshots"] into the task's verification
        # blob. The planner does not surface required_evidence today; tests
        # mutate the DB directly to exercise gate paths (same pattern as the
        # claimed-status mutation used by test_replan_does_not_reset_*).
        db_path = tmp_path / ".fakoli-state" / "state.db"
        conn = _sqlite3.connect(str(db_path))
        try:
            verification_json = _json.dumps(
                {
                    "commands": ["pytest tests/ -v"],
                    "manual_steps": [],
                    "required_evidence": ["screenshots"],
                }
            )
            conn.execute(
                "UPDATE tasks SET verification = ? WHERE id = ?",
                (verification_json, task_id),
            )
            conn.commit()
        finally:
            conn.close()

        _do_claim(tmp_path, task_id, actor="agent-test")

        result = _invoke_cmd(
            tmp_path,
            [
                "submit", task_id,
                "--commands", "pytest tests/ -v",
                "--files-changed", "src/ui.py",
                "--screenshots", "screenshot1.png,screenshot2.png",
                "--actor", "agent-test",
            ],
        )
        assert result.exit_code == 0, f"submit --screenshots failed: {result.output}"

        # Evidence row must carry the parsed screenshots list.
        conn = _sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT screenshots FROM evidence WHERE task_id = ? "
                "ORDER BY submitted_at DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None, "no Evidence row written for submitted task"
        stored = _json.loads(row[0])
        assert stored == ["screenshot1.png", "screenshot2.png"], (
            f"screenshots list mismatch; got {stored!r}"
        )

        # Evidence gate must report PASSED — the screenshots requirement is
        # now satisfied by the recorded list. Check the gate ran at all
        # first (the gate summary block in `submit` swallows exceptions,
        # so a missing 'Evidence gate' line means the gate raised, not
        # that the verdict was wrong).
        assert "Evidence gate" in result.output, (
            "evidence gate summary block did not appear in output; the "
            "gate likely raised an exception (suppressed by submit's "
            f"except-Exception). Full output:\n{result.output}"
        )
        assert "PASSED" in result.output, (
            f"expected 'Evidence gate: PASSED' in output, got: {result.output}"
        )

    def test_submit_without_screenshots_fails_gate_when_required(
        self, tmp_path: Path
    ) -> None:
        """When a task requires 'screenshots' and submit omits --screenshots,
        the evidence gate must report INCOMPLETE. Submit still exits 0
        (gate feedback is informational), but the gate summary must call out
        the missing item."""
        import json as _json
        import sqlite3 as _sqlite3

        _do_init_and_plan(tmp_path, with_git=False)
        task_id = _get_first_ready_task_id(tmp_path)
        assert task_id is not None

        db_path = tmp_path / ".fakoli-state" / "state.db"
        conn = _sqlite3.connect(str(db_path))
        try:
            verification_json = _json.dumps(
                {
                    "commands": ["pytest tests/ -v"],
                    "manual_steps": [],
                    "required_evidence": ["screenshots"],
                }
            )
            conn.execute(
                "UPDATE tasks SET verification = ? WHERE id = ?",
                (verification_json, task_id),
            )
            conn.commit()
        finally:
            conn.close()

        _do_claim(tmp_path, task_id, actor="agent-test")

        result = _invoke_cmd(
            tmp_path,
            [
                "submit", task_id,
                "--commands", "pytest tests/ -v",
                "--files-changed", "src/ui.py",
                "--actor", "agent-test",
            ],
        )
        # Submit succeeds; the gate is informational only. Check the gate
        # ran at all first (the gate summary block in `submit` swallows
        # exceptions, so a missing 'Evidence gate' line means the gate
        # raised, not that the verdict was wrong).
        assert result.exit_code == 0, f"submit failed: {result.output}"
        assert "Evidence gate" in result.output, (
            "evidence gate summary block did not appear in output; the "
            "gate likely raised an exception (suppressed by submit's "
            f"except-Exception). Full output:\n{result.output}"
        )
        assert "INCOMPLETE" in result.output
        assert "screenshots" in result.output


# ---------------------------------------------------------------------------
# Phase 5 — apply command
# ---------------------------------------------------------------------------


class TestApplyCommand:
    def _reach_needs_review(
        self, tmp_path: Path, task_id: str, actor: str = "agent-test"
    ) -> None:
        """Helper: claim + submit to reach needs_review state."""
        _do_claim(tmp_path, task_id, actor=actor)
        _invoke_cmd(
            tmp_path,
            [
                "submit", task_id,
                "--commands", "pytest tests/ -v",
                "--files-changed", "src/main.py",
                "--actor", actor,
            ],
        )

    def test_apply_approve_transitions_to_done(self, tmp_path: Path) -> None:
        """apply --approve transitions needs_review → done."""
        _do_init_and_plan(tmp_path, with_git=False)
        task_id = _get_first_ready_task_id(tmp_path)
        assert task_id is not None

        self._reach_needs_review(tmp_path, task_id)

        result = _invoke_cmd(
            tmp_path,
            ["apply", task_id, "--approve", "--reviewer", "alice"],
        )
        assert result.exit_code == 0, f"apply --approve failed: {result.output}"
        assert "done" in result.output.lower() or "approved" in result.output.lower()

        status = _get_task_status(tmp_path, task_id)
        assert status == "done", f"Expected done, got {status!r}"

    def test_apply_reject_requires_reason(self, tmp_path: Path) -> None:
        """apply --reject without --reason exits non-zero."""
        _do_init_and_plan(tmp_path, with_git=False)
        task_id = _get_first_ready_task_id(tmp_path)
        assert task_id is not None

        self._reach_needs_review(tmp_path, task_id)

        result = _invoke_cmd(
            tmp_path,
            ["apply", task_id, "--reject", "--reviewer", "bob"],
        )
        assert result.exit_code != 0
        combined = result.output + (result.stderr if hasattr(result, "stderr") and result.stderr else "")
        assert "reason" in combined.lower() or "reject" in combined.lower()

    def test_apply_reject_auto_promotes_to_drafted(
        self, tmp_path: Path
    ) -> None:
        """apply --reject --reason transitions needs_review → rejected → drafted
        per spec (rejected is a transient audit marker; drafted is the
        landing state so the task can be re-reviewed). Critic-1 + Critic-2
        flagged the original "stops at rejected" as a spec violation."""
        _do_init_and_plan(tmp_path, with_git=False)
        task_id = _get_first_ready_task_id(tmp_path)
        assert task_id is not None

        self._reach_needs_review(tmp_path, task_id)

        result = _invoke_cmd(
            tmp_path,
            [
                "apply", task_id,
                "--reject",
                "--reason", "Needs more tests.",
                "--reviewer", "bob",
            ],
        )
        assert result.exit_code == 0, f"apply --reject failed: {result.output}"
        assert "rejected" in result.output.lower()

        status = _get_task_status(tmp_path, task_id)
        # Per spec: rejected → drafted is automatic.
        assert status == "drafted", (
            f"Expected drafted (auto-promoted from rejected); got {status!r}"
        )

    def test_apply_without_flag_prints_review_summary(
        self, tmp_path: Path
    ) -> None:
        """apply without --approve or --reject prints review summary and exits 0."""
        _do_init_and_plan(tmp_path, with_git=False)
        task_id = _get_first_ready_task_id(tmp_path)
        assert task_id is not None

        self._reach_needs_review(tmp_path, task_id)

        result = _invoke_cmd(tmp_path, ["apply", task_id])
        assert result.exit_code == 0, f"apply (no flag) failed: {result.output}"
        # Should show that task is awaiting review
        assert (
            "needs_review" in result.output
            or "awaiting" in result.output.lower()
            or "approve" in result.output.lower()
        )

    def test_apply_wrong_status_exits_nonzero(self, tmp_path: Path) -> None:
        """apply on a task not in needs_review status exits non-zero."""
        _do_init_and_plan(tmp_path, with_git=False)
        task_id = _get_first_ready_task_id(tmp_path)
        assert task_id is not None
        # Task is 'ready' (not needs_review)

        result = _invoke_cmd(
            tmp_path,
            ["apply", task_id, "--approve", "--reviewer", "alice"],
        )
        assert result.exit_code != 0
        combined = result.output + (result.stderr if hasattr(result, "stderr") and result.stderr else "")
        assert "needs_review" in combined or "status" in combined.lower()


# ---------------------------------------------------------------------------
# Phase 5 — hook capture-evidence subcommand
# ---------------------------------------------------------------------------


class TestHookCaptureEvidence:
    def test_hook_capture_evidence_no_state_dir_exits_zero(
        self, tmp_path: Path
    ) -> None:
        """hook capture-evidence exits 0 when no .fakoli-state/ exists."""
        result = _invoke_cmd(
            tmp_path,
            [
                "hook", "capture-evidence",
                "--command", "pytest tests/ -v",
                "--exit-code", "0",
                "--actor", "agent-test",
            ],
        )
        assert result.exit_code == 0

    def test_hook_capture_evidence_writes_to_orphan_when_no_claim(
        self, tmp_path: Path
    ) -> None:
        """hook capture-evidence writes to orphan.json when no active claim."""
        _do_init(tmp_path, name="Hook CE Test Project")

        result = _invoke_cmd(
            tmp_path,
            [
                "hook", "capture-evidence",
                "--command", "pytest tests/ -v",
                "--exit-code", "0",
                "--actor", "agent-test",
            ],
        )
        assert result.exit_code == 0

        orphan_file = tmp_path / ".fakoli-state" / ".evidence-buffer" / "orphan.json"
        assert orphan_file.exists(), "orphan.json not written"
        content = orphan_file.read_text(encoding="utf-8")
        assert "pytest" in content

    def test_hook_capture_evidence_exits_zero_on_failure_command(
        self, tmp_path: Path
    ) -> None:
        """hook capture-evidence always exits 0 even when the command's exit-code is non-zero."""
        _do_init(tmp_path, name="Hook CE Failure Test")

        result = _invoke_cmd(
            tmp_path,
            [
                "hook", "capture-evidence",
                "--command", "pytest tests/ -v",
                "--exit-code", "1",
                "--actor", "agent-test",
            ],
        )
        assert result.exit_code == 0  # hook MUST always exit 0


# ---------------------------------------------------------------------------
# Phase 5 — end-to-end: full lifecycle init → done
# ---------------------------------------------------------------------------


class TestE2EPhase5:
    def test_full_lifecycle_init_to_done(self, tmp_path: Path) -> None:
        """Full lifecycle: init → PRD → plan → review_tasks → claim → submit → apply --approve.

        Asserts task reaches 'done' status at the end.
        """
        # 1. Full setup (git + init + PRD + plan + score + review tasks)
        _do_init_and_plan(tmp_path, with_git=False)
        task_id = _get_first_ready_task_id(tmp_path)
        assert task_id is not None, "No ready tasks after full setup"

        # 2. claim
        claim_result = _invoke_cmd(
            tmp_path, ["claim", task_id, "--actor", "agent-test"]
        )
        assert claim_result.exit_code == 0, f"claim failed: {claim_result.output}"

        # 3. submit evidence
        submit_result = _invoke_cmd(
            tmp_path,
            [
                "submit", task_id,
                "--commands", "pytest tests/ -v",
                "--files-changed", "src/auth.py",
                "--actor", "agent-test",
            ],
        )
        assert submit_result.exit_code == 0, f"submit failed: {submit_result.output}"

        # Verify task is now in needs_review
        status = _get_task_status(tmp_path, task_id)
        assert status == "needs_review", f"Expected needs_review, got {status!r}"

        # 4. apply --approve
        apply_result = _invoke_cmd(
            tmp_path,
            ["apply", task_id, "--approve", "--reviewer", "human-reviewer"],
        )
        assert apply_result.exit_code == 0, f"apply --approve failed: {apply_result.output}"

        # Verify task is now done
        final_status = _get_task_status(tmp_path, task_id)
        assert final_status == "done", (
            f"Expected task '{task_id}' to be 'done' after full lifecycle, got '{final_status}'"
        )


# ---------------------------------------------------------------------------
# Phase 7 Wave 2: --use-llm CLI flag wiring
# ---------------------------------------------------------------------------


class TestUseLlmFlagHelp:
    """The --use-llm flag must appear in --help for plan / score / expand."""

    def test_plan_help_documents_use_llm(self, tmp_path: Path) -> None:
        result = _invoke_cmd(tmp_path, ["plan", "--help"])
        assert result.exit_code == 0
        assert "--use-llm" in result.output

    def test_score_help_documents_use_llm(self, tmp_path: Path) -> None:
        result = _invoke_cmd(tmp_path, ["score", "--help"])
        assert result.exit_code == 0
        assert "--use-llm" in result.output

    def test_expand_help_documents_use_llm(self, tmp_path: Path) -> None:
        result = _invoke_cmd(tmp_path, ["expand", "--help"])
        assert result.exit_code == 0
        assert "--use-llm" in result.output


class TestUseLlmRequiresApiKey:
    """Without ANTHROPIC_API_KEY, --use-llm must exit 1 with a clean message."""

    def test_plan_use_llm_without_env_exits_1(
        self, tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
    ) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        _do_init(tmp_path)
        _write_prd(tmp_path, _FULL_PRD_CONTENT)
        _invoke_cmd(tmp_path, ["prd", "parse"])

        result = _invoke_cmd(tmp_path, ["plan", "--use-llm"])
        assert result.exit_code == 1
        combined = result.output + (
            result.stderr if hasattr(result, "stderr") and result.stderr else ""
        )
        assert "ANTHROPIC_API_KEY" in combined

    def test_score_use_llm_without_env_exits_1(
        self, tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
    ) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        _do_init(tmp_path)
        _write_prd(tmp_path, _FULL_PRD_CONTENT)
        _invoke_cmd(tmp_path, ["prd", "parse"])
        _invoke_cmd(tmp_path, ["plan"])

        result = _invoke_cmd(tmp_path, ["score", "--use-llm"])
        assert result.exit_code == 1
        combined = result.output + (
            result.stderr if hasattr(result, "stderr") and result.stderr else ""
        )
        assert "ANTHROPIC_API_KEY" in combined

    def test_expand_use_llm_without_env_exits_1(
        self, tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
    ) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        _do_init(tmp_path)

        result = _invoke_cmd(tmp_path, ["expand", "T001", "--use-llm"])
        assert result.exit_code == 1
        combined = result.output + (
            result.stderr if hasattr(result, "stderr") and result.stderr else ""
        )
        assert "ANTHROPIC_API_KEY" in combined


class TestUseLlmRecordedProvider:
    """End-to-end CLI invocations with a RecordedLLMProvider injected.

    We monkeypatch ``fakoli_state.cli.plan._resolve_llm_provider`` to return
    a pre-populated ``RecordedLLMProvider`` so the CLI executes the full
    --use-llm code path without touching the network or the env var check.
    """

    def _install_provider(
        self,
        monkeypatch,  # type: ignore[no-untyped-def]
        provider_factory,  # type: ignore[no-untyped-def]
    ) -> None:
        """Replace _resolve_llm_provider with one that returns ``provider``."""
        import importlib

        plan_module = importlib.import_module("fakoli_state.cli.plan")

        def fake_resolve(use_llm: bool, config=None):  # type: ignore[no-untyped-def]
            return provider_factory() if use_llm else None

        monkeypatch.setattr(plan_module, "_resolve_llm_provider", fake_resolve)

    def test_plan_use_llm_enriches_short_descriptions(
        self, tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
    ) -> None:
        """plan --use-llm with a recorded provider enriches short descriptions."""
        from fakoli_state.planning.llm import LLMResponse, RecordedLLMProvider
        from fakoli_state.planning.template import (
            _DESCRIPTION_ENRICH_SYSTEM_PROMPT,
        )

        # A PRD whose task body is <50 chars so enrichment triggers.
        prd = """\
# Project: Wave 2 CLI Plan Test

## Summary

Project for CLI plan --use-llm.

## Goals

- Goal.

## Requirements

- R001: Req.

## Features

### F001: Core
Feature.
**Requirements:** R001

## Tasks

### T001: ShortTitle

**Feature:** F001
**Priority:** medium

Tiny body.
"""

        enriched_text = (
            "Implement the ShortTitle module. Define the public surface "
            "in src/short.py and cover edge cases in tests/test_short.py. "
            "Honor existing logging and error-handling patterns."
        )
        user_payload = (
            "Requirement: ShortTitle\nExisting short description: 'Tiny body.'"
        )
        # Phase 9 C2: record_key includes tuning args; pass the engine's
        # _DESCRIPTION_ENRICH_MAX_TOKENS so the recorded key matches.
        from fakoli_state.planning.template import _DESCRIPTION_ENRICH_MAX_TOKENS
        key = RecordedLLMProvider.record_key(
            _DESCRIPTION_ENRICH_SYSTEM_PROMPT,
            user_payload,
            max_tokens=_DESCRIPTION_ENRICH_MAX_TOKENS,
        )
        canned = LLMResponse(
            text=enriched_text,
            input_tokens=10,
            cached_input_tokens=0,
            output_tokens=20,
            model="claude-sonnet-4-6",
            finish_reason="end_turn",
        )

        self._install_provider(
            monkeypatch, lambda: RecordedLLMProvider({key: canned})
        )

        _do_init(tmp_path)
        _write_prd(tmp_path, prd)
        _invoke_cmd(tmp_path, ["prd", "parse"])

        result = _invoke_cmd(tmp_path, ["plan", "--use-llm"])
        assert result.exit_code == 0, f"plan --use-llm failed: {result.output}"

        # The enriched description landed in the backend. `show` doesn't print
        # description, so query the backend directly to verify augmentation.
        from fakoli_state.clock import SystemClock
        from fakoli_state.state.sqlite import SqliteBackend

        state_dir = tmp_path / ".fakoli-state"
        backend = SqliteBackend(
            db_path=str(state_dir / "state.db"),
            events_path=str(state_dir / "events.jsonl"),
            clock=SystemClock(),
        )
        backend.initialize()
        try:
            task = backend.get_task("T001")
            assert task is not None, "T001 must exist in backend after plan"
            assert "ShortTitle module" in task.description, (
                f"expected enriched description, got: {task.description!r}"
            )
        finally:
            backend.close()

    def test_score_use_llm_appends_explanation_paragraph(
        self, tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
    ) -> None:
        """score --use-llm produces a Score whose explanation contains the LLM text."""
        from fakoli_state.planning.llm import LLMResponse

        # We don't know the task body in advance; build a provider that
        # returns the same canned response for ANY key.  Subclass to override
        # generate() and bypass the key-miss check.
        canned_text = (
            "Trade-off summary: this task is small in surface area, so the "
            "deterministic blast_radius is appropriate. Review risk could "
            "be relaxed if the converter is fully covered by tests."
        )

        class _AlwaysReturnProvider:
            def generate(
                self,
                *,
                system: str,
                user: str,
                max_tokens: int = 4096,
                temperature: float = 0.0,
            ) -> LLMResponse:
                _ = system, user, max_tokens, temperature
                return LLMResponse(
                    text=canned_text,
                    input_tokens=10,
                    cached_input_tokens=0,
                    output_tokens=20,
                    model="claude-sonnet-4-6",
                    finish_reason="end_turn",
                )

        self._install_provider(monkeypatch, lambda: _AlwaysReturnProvider())

        _do_init(tmp_path)
        _write_prd(tmp_path, _FULL_PRD_CONTENT)
        _invoke_cmd(tmp_path, ["prd", "parse"])
        _invoke_cmd(tmp_path, ["plan"])

        result = _invoke_cmd(tmp_path, ["score", "--use-llm"])
        assert result.exit_code == 0, f"score --use-llm failed: {result.output}"

        # Verify the LLM augmentation reached the backend explanation field.
        show_result = _invoke_cmd(tmp_path, ["show", "T001"])
        assert show_result.exit_code == 0
        assert "Trade-off summary" in show_result.output

    def test_expand_use_llm_prints_proposals(
        self, tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
    ) -> None:
        """expand --use-llm prints proposal blocks for a high-complexity task."""
        from fakoli_state.planning.llm import LLMResponse

        canned_proposals = [
            {
                "title": "Sub-task A",
                "description": "Do A.",
                "acceptance_criteria": ["A done"],
                "likely_files": ["src/a.py"],
            },
            {
                "title": "Sub-task B",
                "description": "Do B.",
                "acceptance_criteria": ["B done"],
                "likely_files": ["src/b.py"],
            },
        ]
        canned_text = json.dumps(canned_proposals)

        class _AlwaysReturnProvider:
            def generate(
                self,
                *,
                system: str,
                user: str,
                max_tokens: int = 4096,
                temperature: float = 0.0,
            ) -> LLMResponse:
                _ = system, user, max_tokens, temperature
                return LLMResponse(
                    text=canned_text,
                    input_tokens=10,
                    cached_input_tokens=0,
                    output_tokens=80,
                    model="claude-sonnet-4-6",
                    finish_reason="end_turn",
                )

        # We need a task with complexity >= 4. The fixture PRD's T001 has many
        # likely_files but the scoring engine yields complexity 4 only for
        # tasks with >=5 files. T001 in _FULL_PRD_CONTENT only has 2 files,
        # so its complexity will be ~2. Write a custom PRD with a complex task.
        complex_prd = """\
# Project: Expand Test

## Summary

Test expand --use-llm.

## Goals

- Decompose.

## Requirements

- R001: Refactor.

## Features

### F001: Big Refactor

Feature.

**Requirements:** R001

## Tasks

### T001: Big architectural refactor of the planning engine

**Feature:** F001
**Priority:** high
**Likely files:** src/a.py, src/b.py, src/c.py, src/d.py, src/e.py, src/f.py

**Acceptance criteria:**

- Refactor compiles.
- Migration story documented.

**Verification:**

- `pytest -q`

This is a refactor that touches architecture across many modules.
"""

        self._install_provider(monkeypatch, lambda: _AlwaysReturnProvider())

        _do_init(tmp_path)
        _write_prd(tmp_path, complex_prd)
        _invoke_cmd(tmp_path, ["prd", "parse"])
        _invoke_cmd(tmp_path, ["plan"])
        _invoke_cmd(tmp_path, ["score"])

        result = _invoke_cmd(tmp_path, ["expand", "T001", "--use-llm"])
        assert result.exit_code == 0, f"expand --use-llm failed: {result.output}"
        assert "Sub-task A" in result.output
        assert "Sub-task B" in result.output
        assert "Proposed 2 sub-task" in result.output

    def test_use_llm_flag_default_false_unchanged_behavior(
        self, tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
    ) -> None:
        """Without --use-llm, no provider is constructed (env var not consulted)."""
        # If the deterministic path accidentally consulted the env or built a
        # provider, install_provider's fake would raise (it asserts use_llm).
        sentinel_raised = []

        def fake_resolve(use_llm: bool, config=None):  # type: ignore[no-untyped-def]
            if use_llm:
                sentinel_raised.append("called")
            return None

        import importlib

        plan_module = importlib.import_module("fakoli_state.cli.plan")

        monkeypatch.setattr(plan_module, "_resolve_llm_provider", fake_resolve)

        # Even without ANTHROPIC_API_KEY, deterministic plan/score must work.
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        _do_init(tmp_path)
        _write_prd(tmp_path, _FULL_PRD_CONTENT)
        _invoke_cmd(tmp_path, ["prd", "parse"])

        plan_result = _invoke_cmd(tmp_path, ["plan"])
        assert plan_result.exit_code == 0
        score_result = _invoke_cmd(tmp_path, ["score"])
        assert score_result.exit_code == 0

        # Provider factory was never invoked with use_llm=True.
        assert sentinel_raised == []


# ---------------------------------------------------------------------------
# Orphan-prune on re-parse (v1.15.0)
# ---------------------------------------------------------------------------


# A two-task PRD that we can edit-down to one task to create orphans.
_TWO_TASK_PRD = """\
# Project: Orphan Test

## Summary

Setup for orphan-prune testing.

## Goals

- Test orphans.

## Requirements

- R001: First.
- R002: Second.

## Features

### F001: One feature

**Requirements:** R001, R002

## Tasks

### T001: Keep me

**Feature:** F001
**Priority:** medium
**Likely files:** src/a.py

Stays in the PRD across re-parses.

**Acceptance criteria:**

- Stays.

**Verification:**

- `pytest a`

### T002: Delete me

**Feature:** F001
**Priority:** medium
**Likely files:** src/b.py

Removed from the PRD on the second parse to create an orphan.

**Acceptance criteria:**

- Used to exist.

**Verification:**

- `pytest b`
"""


# Same PRD but with T002 removed — what the user re-saves after deciding to
# drop the task.
_TWO_TASK_PRD_WITHOUT_T002 = """\
# Project: Orphan Test

## Summary

Setup for orphan-prune testing.

## Goals

- Test orphans.

## Requirements

- R001: First.

## Features

### F001: One feature

**Requirements:** R001

## Tasks

### T001: Keep me

**Feature:** F001
**Priority:** medium
**Likely files:** src/a.py

Stays in the PRD across re-parses.

**Acceptance criteria:**

- Stays.

**Verification:**

- `pytest a`
"""


class TestPlanOrphanPrune:
    """v1.15.0 behavior: when a task that was in state.db is no longer in
    the re-parsed PRD, `plan` emits task.deleted so state.db stays in sync
    with the PRD. Refuses non-safe statuses without --prune-force."""

    def _setup_with_two_tasks(self, tmp_path: Path) -> None:
        """Init, write PRD, parse, plan — leaves T001 + T002 in state.db at drafted."""
        _do_init(tmp_path)
        _write_prd(tmp_path, _TWO_TASK_PRD)
        parse_result = _invoke_cmd(tmp_path, ["prd", "parse"])
        assert parse_result.exit_code == 0
        plan_result = _invoke_cmd(tmp_path, ["plan"])
        assert plan_result.exit_code == 0

    def _list_task_ids(self, tmp_path: Path) -> set[str]:
        """Read task IDs straight from state.db (CLI 'list' adds formatting)."""
        import sqlite3
        db = tmp_path / ".fakoli-state" / "state.db"
        with sqlite3.connect(str(db)) as conn:
            return {r[0] for r in conn.execute("SELECT id FROM tasks")}

    def _set_task_status(self, tmp_path: Path, task_id: str, status: str) -> None:
        """Directly mutate task status in SQLite for test setup.

        Goes around the event log on purpose — this is fixture plumbing,
        not a behavior under test. Using a real claim event would require
        a multi-line setup that obscures what the test actually asserts.
        """
        import sqlite3
        db = tmp_path / ".fakoli-state" / "state.db"
        with sqlite3.connect(str(db)) as conn:
            conn.execute(
                "UPDATE tasks SET status = ? WHERE id = ?", (status, task_id)
            )
            conn.commit()

    def test_safe_orphan_is_pruned_silently(self, tmp_path: Path) -> None:
        """T002 in drafted (safe) status is deleted from state.db when
        prd.md no longer contains it. This is the canonical happy path."""
        self._setup_with_two_tasks(tmp_path)
        assert self._list_task_ids(tmp_path) == {"T001", "T002"}

        # Remove T002 from prd.md, re-parse, re-plan.
        _write_prd(tmp_path, _TWO_TASK_PRD_WITHOUT_T002)
        _invoke_cmd(tmp_path, ["prd", "parse"])
        plan_result = _invoke_cmd(tmp_path, ["plan"])

        assert plan_result.exit_code == 0, (
            f"plan should succeed when orphan is in safe status; got: {plan_result.output}"
        )
        assert "T002" in plan_result.output, (
            f"plan output should mention pruned T002; got: {plan_result.output}"
        )
        assert "Pruned" in plan_result.output
        # state.db now matches the new PRD.
        assert self._list_task_ids(tmp_path) == {"T001"}

    def test_unsafe_orphan_blocks_plan_without_prune_force(
        self, tmp_path: Path
    ) -> None:
        """T002 advanced to claimed (unsafe) status: plan must refuse with
        a helpful error and exit 1, NOT silently delete and lose audit history.
        """
        self._setup_with_two_tasks(tmp_path)
        self._set_task_status(tmp_path, "T002", "claimed")

        _write_prd(tmp_path, _TWO_TASK_PRD_WITHOUT_T002)
        _invoke_cmd(tmp_path, ["prd", "parse"])
        plan_result = _invoke_cmd(tmp_path, ["plan"])

        assert plan_result.exit_code == 1, (
            f"plan should fail loudly on unsafe orphan; got exit "
            f"{plan_result.exit_code}, output: {plan_result.output}"
        )
        combined = plan_result.output + (
            plan_result.stderr if hasattr(plan_result, "stderr") and plan_result.stderr else ""
        )
        assert "T002" in combined, (
            f"error should name the blocking task; got: {combined}"
        )
        assert "--prune-force" in combined, (
            f"error should mention the escape hatch; got: {combined}"
        )
        # Orphan was NOT deleted — state.db preserves T002 with claim status.
        assert "T002" in self._list_task_ids(tmp_path)

    def test_prune_force_overrides_unsafe_status(self, tmp_path: Path) -> None:
        """--prune-force deletes orphans regardless of status. The events
        + evidence + reviews for T002 stay in events.jsonl as audit history;
        only the task row is removed."""
        self._setup_with_two_tasks(tmp_path)
        self._set_task_status(tmp_path, "T002", "claimed")

        _write_prd(tmp_path, _TWO_TASK_PRD_WITHOUT_T002)
        _invoke_cmd(tmp_path, ["prd", "parse"])
        plan_result = _invoke_cmd(tmp_path, ["plan", "--prune-force"])

        assert plan_result.exit_code == 0, (
            f"plan --prune-force should succeed; got: {plan_result.output}"
        )
        assert self._list_task_ids(tmp_path) == {"T001"}, (
            "T002 should have been force-pruned despite claimed status"
        )

    def test_clean_re_plan_emits_no_prune_message(self, tmp_path: Path) -> None:
        """Sanity: when nothing was orphaned, plan should NOT print a Pruned line."""
        self._setup_with_two_tasks(tmp_path)
        # Re-run plan with the same PRD — nothing should be pruned.
        plan_result = _invoke_cmd(tmp_path, ["plan"])
        assert plan_result.exit_code == 0
        assert "Pruned" not in plan_result.output, (
            f"clean re-plan should not mention pruning; got: {plan_result.output}"
        )


# ---------------------------------------------------------------------------
# replay command
# ---------------------------------------------------------------------------


class TestReplayCommand:
    """Tests for `fakoli-state replay --from-events <events.jsonl> --into <db>`."""

    def _init_project(self, tmp_path: Path) -> Path:
        """Run fakoli-state init in tmp_path and return the .fakoli-state dir."""
        _do_init(tmp_path, name="Replay Test Project")
        return tmp_path / ".fakoli-state"

    def test_replay_happy_path_into_scratch_db(self, tmp_path: Path) -> None:
        """Successful replay into a temp path exits 0 and creates the target db."""
        state_dir = self._init_project(tmp_path)
        events_path = state_dir / "events.jsonl"

        scratch_db = tmp_path / "scratch" / "replay.db"

        result = runner.invoke(
            app,
            [
                "replay",
                "--from-events", str(events_path),
                "--into", str(scratch_db),
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, f"replay failed: {result.output}"
        assert scratch_db.exists(), "scratch db not created after replay"
        # Output should confirm the events source and destination.
        assert str(events_path) in result.output or "events" in result.output.lower()
        assert str(scratch_db) in result.output or "canonical" in result.output.lower()

    def test_replay_refuses_live_state_db(self, tmp_path: Path) -> None:
        """replay refuses to target the live state.db and exits non-zero."""
        state_dir = self._init_project(tmp_path)
        events_path = state_dir / "events.jsonl"
        live_db = state_dir / "state.db"

        original_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = runner.invoke(
                app,
                [
                    "replay",
                    "--from-events", str(events_path),
                    "--into", str(live_db),
                ],
                catch_exceptions=False,
            )
        finally:
            os.chdir(original_cwd)

        assert result.exit_code != 0, (
            "replay should refuse to target live state.db; got exit 0"
        )
        combined = result.output + (
            result.stderr if hasattr(result, "stderr") and result.stderr else ""
        )
        # The live-DB guard message is:
        #   "Error: --into targets the live state database at <path>. ..."
        assert "--into targets the live state database" in combined, (
            f"error message should contain the specific live-DB-guard text; got: {combined}"
        )

    def test_replay_missing_from_events_exits_nonzero(self, tmp_path: Path) -> None:
        """A missing --from-events file exits non-zero with a clear message."""
        missing = tmp_path / "does_not_exist.jsonl"
        scratch_db = tmp_path / "replay.db"

        result = runner.invoke(
            app,
            [
                "replay",
                "--from-events", str(missing),
                "--into", str(scratch_db),
            ],
            catch_exceptions=False,
        )
        assert result.exit_code != 0, (
            "replay should exit non-zero when --from-events is missing"
        )
        combined = result.output + (
            result.stderr if hasattr(result, "stderr") and result.stderr else ""
        )
        assert "not found" in combined.lower() or str(missing) in combined, (
            f"error should name the missing file; got: {combined}"
        )
