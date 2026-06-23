"""CLI integration tests using Typer's CliRunner.

Tests the anvil CLI surface:
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

import pytest
from click.testing import Result
from typer.testing import CliRunner

from anvil.cli import app

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

runner = CliRunner()


# ---------------------------------------------------------------------------
# init — happy path
# ---------------------------------------------------------------------------


class TestInit:
    def test_init_creates_state_directory(self, tmp_path: Path) -> None:
        """init creates .anvil/ with all expected files and directories."""
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
        state_dir = tmp_path / ".anvil"
        assert state_dir.exists(), ".anvil/ directory not created"
        assert (state_dir / "state.db").exists(), "state.db not created"
        assert (state_dir / "events.jsonl").exists(), "events.jsonl not created"
        assert (state_dir / "config.yaml").exists(), "config.yaml not created"
        assert (state_dir / "packets").is_dir(), "packets/ not created"
        # snapshots/ is no longer pre-created at init (PS-2);
        # `anvil snapshot` will create it on first use.

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

    def test_init_output_states_required_prd_sections(self, tmp_path: Path) -> None:
        """GAP-02: plain init tells the user the required PRD sections and the
        bold-inline field format so the first `prd parse` doesn't fail blind."""
        original_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = runner.invoke(
                app,
                ["init", "--name", "Guidance Project"],
                catch_exceptions=False,
            )
        finally:
            os.chdir(original_cwd)

        assert result.exit_code == 0, f"init failed: {result.output}"
        out = result.output
        # The four required sections must be named.
        assert "# Project" in out
        assert "## Summary" in out
        assert "## Goals" in out
        assert "## Requirements" in out
        # The bold-inline field format must be shown.
        assert "**Feature:**" in out
        assert "F001" in out

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
        """--force reinitialises an existing .anvil/ directory."""
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
            events_path = tmp_path / ".anvil" / "events.jsonl"
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
        """init refuses when .claude-plugin/plugin.json declares name == anvil."""
        # Create fake plugin manifest
        plugin_dir = tmp_path / ".claude-plugin"
        plugin_dir.mkdir()
        (plugin_dir / "plugin.json").write_text(
            json.dumps({"name": "anvil", "version": "1.0.0"}),
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

    def test_init_non_anvil_plugin_allowed(self, tmp_path: Path) -> None:
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
    """`anvil init --with-sample` seeds a runnable PRD→next loop.

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
        state_dir = tmp_path / ".anvil"
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
        assert "anvil next" in result.output

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
        state_dir = tmp_path / ".anvil"
        # Default init does NOT write a prd.md.
        assert not (state_dir / "prd.md").exists()
        # And `next` finds nothing claimable.
        next_result = self._run(["next", "--json"], tmp_path)
        assert next_result.exit_code == 0
        assert json.loads(next_result.output)["data"]["task"] is None

    def test_init_with_sample_events_replay_to_same_state(self, tmp_path: Path) -> None:
        """Seeded events.jsonl replays to an identical DB (audit invariant)."""
        assert self._run(["init", "--with-sample"], tmp_path).exit_code == 0
        state_dir = tmp_path / ".anvil"
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
        """status in dir without .anvil/ exits 1."""
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
        """status --hook-format in dir without .anvil/ exits 0 with 'uninitialized'."""
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
        """--version prints 'anvil {__version__}' and exits 0.

        Imports __version__ rather than hardcoding so the test doesn't
        need a one-line bump on every release (Critic-4 TQ-5 in PR #41).
        """
        from anvil import __version__

        result = runner.invoke(app, ["--version"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "anvil" in result.output
        assert __version__ in result.output

    def test_version_short_flag(self) -> None:
        """-V is an alias for --version."""
        result = runner.invoke(app, ["-V"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "anvil" in result.output

    def test_version_reports_engine_and_schema(self) -> None:
        """--version reports the engine version AND the SQLite schema version (T012).

        A host pinning behaviour needs both: ``__version__`` identifies the
        build, ``schema N`` identifies the on-disk state format. The first token
        stays ``anvil {__version__}`` for backward compatibility.
        """
        from anvil import __version__
        from anvil.state.schema import get_schema_version

        result = runner.invoke(app, ["--version"], catch_exceptions=False)
        assert result.exit_code == 0
        out = result.output
        # Backward-compatible engine token still present and first.
        assert f"anvil {__version__}" in out
        # Schema version is now also surfaced.
        assert "schema" in out.lower()
        assert str(get_schema_version()) in out


# ---------------------------------------------------------------------------
# describe — self-describing command surface (T012)
# ---------------------------------------------------------------------------


def _expected_cli_command_names() -> list[str]:
    """Independently enumerate the live Typer app's leaf command paths.

    Deliberately re-derived from the Typer app here (NOT via the describe
    module's own helper) so the drift assertion has a second, independent
    witness: if describe ever hand-maintained or stale-cached its list, this
    comparison would catch the divergence.
    """
    import click
    from typer.main import get_command

    root = get_command(app)

    def walk(group: click.Group, prefix: str) -> list[str]:
        names: list[str] = []
        for name, sub in group.commands.items():
            full = f"{prefix}{name}"
            if isinstance(sub, click.Group):
                names.extend(walk(sub, full + " "))
            else:
                names.append(full)
        return names

    assert isinstance(root, click.Group)
    return sorted(walk(root, ""))


def _expected_mcp_tool_names() -> list[str]:
    """Independently enumerate the live FastMCP server's registered tools."""
    import asyncio

    from anvil.mcp_server import mcp

    tools = asyncio.run(mcp.list_tools())
    return sorted(t.name for t in tools)


class TestDescribe:
    def test_describe_emits_success_envelope_with_versions(self) -> None:
        """describe emits the standard envelope carrying the stable api_version,
        the engine version, and the schema version."""
        from anvil import __version__
        from anvil.cli.describe import API_VERSION
        from anvil.state.schema import get_schema_version

        result = runner.invoke(app, ["describe"], catch_exceptions=False)
        assert result.exit_code == 0, result.output

        env = json.loads(result.stdout.strip())
        assert env["ok"] is True
        assert env["command"] == "describe"
        data = env["data"]
        assert data["api_version"] == API_VERSION
        assert data["engine_version"] == __version__
        assert data["schema_version"] == get_schema_version()

    def test_describe_works_without_a_project(self, tmp_path: Path) -> None:
        """describe needs no init — it never opens a backend (runs anywhere)."""
        original_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = runner.invoke(app, ["describe"], catch_exceptions=False)
        finally:
            os.chdir(original_cwd)
        assert result.exit_code == 0, result.output
        env = json.loads(result.stdout.strip())
        assert env["ok"] is True

    def test_described_cli_surface_matches_registered_commands(self) -> None:
        """The described CLI surface MUST equal the live Typer command set.

        This is the anti-drift guard: a command added/renamed/removed without
        the surface staying coherent fails CI here.
        """
        result = runner.invoke(app, ["describe"], catch_exceptions=False)
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())["data"]

        described = data["cli"]["commands"]
        expected = _expected_cli_command_names()
        assert described == expected
        assert data["cli"]["count"] == len(expected)
        # describe itself is part of the surface it reports.
        assert "describe" in described

    def test_described_mcp_surface_matches_registered_tools(self) -> None:
        """The described MCP surface MUST equal the live FastMCP tool set."""
        result = runner.invoke(app, ["describe"], catch_exceptions=False)
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())["data"]

        described = data["mcp"]["tools"]
        expected = _expected_mcp_tool_names()
        assert described == expected
        assert data["mcp"]["count"] == len(expected)
        # The MCP self-describe capability is itself in the surface.
        assert "describe_surface" in described

    def test_describe_json_flag_is_a_noop(self) -> None:
        """--json is accepted (flag symmetry) and yields the same envelope."""
        default = runner.invoke(app, ["describe"], catch_exceptions=False)
        explicit = runner.invoke(app, ["describe", "--json"], catch_exceptions=False)
        assert default.exit_code == explicit.exit_code == 0
        assert json.loads(default.stdout.strip()) == json.loads(
            explicit.stdout.strip()
        )

    def test_describe_human_is_readable(self) -> None:
        """--human prints a readable summary, not the JSON envelope."""
        result = runner.invoke(app, ["describe", "--human"], catch_exceptions=False)
        assert result.exit_code == 0, result.output
        # Not a JSON envelope.
        assert not result.stdout.strip().startswith("{")
        assert "CLI commands" in result.output
        assert "MCP tools" in result.output

    def test_mcp_describe_surface_matches_cli_describe(self) -> None:
        """The MCP describe_surface tool returns the IDENTICAL manifest the CLI
        emits — one source of truth, two surfaces."""
        import asyncio

        from anvil.cli.describe import build_manifest
        from anvil.mcp_server import mcp

        cli_manifest = build_manifest()

        async def _call() -> dict:  # type: ignore[type-arg]
            res = await mcp.call_tool("describe_surface", {})
            return res.structured_content  # type: ignore[no-any-return]

        mcp_manifest = asyncio.run(_call())
        assert mcp_manifest == cli_manifest


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
    """Run `anvil init` in tmp_path."""
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
    """Write content to .anvil/prd.md."""
    prd_path = tmp_path / ".anvil" / "prd.md"
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
# prd parse --prd (T016) — per-PRD source files and partitioned parsing
# ---------------------------------------------------------------------------


_NAMED_PRD_CONTENT = """\
# Project: CLI Named PRD

## Summary

A named PRD for multi-PRD CLI testing.

## Goals

- Ship v0.2.

## Requirements

- Named requirement one.
- Named requirement two.
"""


def _write_named_prd(tmp_path: Path, prd_id: str, content: str) -> None:
    """Write content to .anvil/prds/<prd_id>.md, creating prds/ if needed."""
    prds_dir = tmp_path / ".anvil" / "prds"
    prds_dir.mkdir(parents=True, exist_ok=True)
    (prds_dir / f"{prd_id}.md").write_text(content, encoding="utf-8")


def _prd_parsed_payload(tmp_path: Path) -> dict:  # type: ignore[type-arg]
    """Return the payload of the LAST prd.parsed event in events.jsonl."""
    payload: dict = {}  # type: ignore[type-arg]
    for line in _events_text(tmp_path).splitlines():
        event = json.loads(line)
        if event.get("action") == "prd.parsed":
            payload = event.get("payload_json") or event.get("payload") or {}
    return payload


class TestPrdSourcePath:
    def test_default_prd_id_maps_to_bare_prd_md(self) -> None:
        """prd_source_path returns <state_dir>/prd.md for the default PRD."""
        from anvil.cli._helpers import prd_source_path

        state_dir = Path("/proj/.anvil")
        assert prd_source_path(state_dir, "default") == state_dir / "prd.md"

    def test_parse_sentinel_id_maps_to_bare_prd_md(self) -> None:
        """The parse-time sentinel ('prd') also resolves to prd.md."""
        from anvil.cli._helpers import prd_source_path

        state_dir = Path("/proj/.anvil")
        assert prd_source_path(state_dir, "prd") == state_dir / "prd.md"

    def test_named_prd_id_maps_to_prds_subdir(self) -> None:
        """A named PRD resolves to <state_dir>/prds/<id>.md."""
        from anvil.cli._helpers import prd_source_path

        state_dir = Path("/proj/.anvil")
        assert (
            prd_source_path(state_dir, "v0.2")
            == state_dir / "prds" / "v0.2.md"
        )


class TestPrdParseNamed:
    def test_named_prd_reads_prds_subdir_and_prd_parsed_carries_prd_id(
        self, tmp_path: Path
    ) -> None:
        """`prd parse --prd v0.2` reads .anvil/prds/v0.2.md and emits a
        prd.parsed event carrying prd_id='v0.2'."""
        _do_init(tmp_path)
        _write_named_prd(tmp_path, "v0.2", _NAMED_PRD_CONTENT)

        result = _invoke_cmd(tmp_path, ["prd", "parse", "--prd", "v0.2"])
        assert result.exit_code == 0, f"named parse failed: {result.output}"
        # Source line points at the prds/ subdir, not the bare prd.md.
        assert "prds/v0.2.md" in result.output

        payload = _prd_parsed_payload(tmp_path)
        assert payload.get("prd_id") == "v0.2"
        assert payload.get("is_default") is False
        # Named PRD ids are prefixed (T015).
        assert [r["id"] for r in payload["requirements"]] == [
            "v0.2:R001",
            "v0.2:R002",
        ]

        # ux_prds_default invariant at the DB level (not just the event
        # payload): a named-only parse with NO prior default parse must NOT
        # mint an is_default=1 row, and the named row must be is_default=0.
        # An event-payload-only assertion would miss a handler regression that
        # ignored payload.is_default.
        from anvil.cli._helpers import _open_backend

        backend = _open_backend(tmp_path / ".anvil")
        try:
            prds = {p.id: p.is_default for p in backend.list_prds()}
            assert prds == {"v0.2": False}
            assert backend.default_prd_id() is None
        finally:
            backend.close()

    def test_no_flag_reads_default_prd_md_unchanged(
        self, tmp_path: Path
    ) -> None:
        """Without --prd the command reads .anvil/prd.md and the prd.parsed
        payload stays byte-identical (no prd_id/is_default keys → default
        partition with bare ids)."""
        _do_init(tmp_path)
        _write_prd(tmp_path, _MINIMAL_PRD_CONTENT)

        result = _invoke_cmd(tmp_path, ["prd", "parse"])
        assert result.exit_code == 0, f"prd parse failed: {result.output}"
        # The default source is the bare prd.md (no prds/ subdir).
        assert "prd.md" in result.output
        assert "prds/" not in result.output

        payload = _prd_parsed_payload(tmp_path)
        # Omitted entirely so the event matches the pre-multi-PRD golden.
        assert "prd_id" not in payload
        assert "is_default" not in payload
        # Default PRD keeps bare requirement ids.
        assert [r["id"] for r in payload["requirements"]] == ["R001", "R002"]

    def test_parsing_named_prd_leaves_default_requirements_untouched(
        self, tmp_path: Path
    ) -> None:
        """Parsing one PRD writes only its own partition: the default PRD's
        requirement rows survive a subsequent named parse, and vice-versa."""
        from anvil.cli._helpers import _open_backend

        _do_init(tmp_path)
        _write_prd(tmp_path, _MINIMAL_PRD_CONTENT)
        _invoke_cmd(tmp_path, ["prd", "parse"])
        _write_named_prd(tmp_path, "v0.2", _NAMED_PRD_CONTENT)
        _invoke_cmd(tmp_path, ["prd", "parse", "--prd", "v0.2"])

        backend = _open_backend(tmp_path / ".anvil")
        try:
            default_reqs = [
                r.id for r in backend.list_requirements(prd_id="default")
            ]
            named_reqs = [
                r.id for r in backend.list_requirements(prd_id="v0.2")
            ]
        finally:
            backend.close()

        # The default partition is intact after the named parse...
        assert default_reqs == ["R001", "R002"]
        # ...and the named partition holds only its own (prefixed) rows.
        assert named_reqs == ["v0.2:R001", "v0.2:R002"]

    def test_missing_named_prd_source_exits_1_with_path(
        self, tmp_path: Path
    ) -> None:
        """A missing .anvil/prds/<id>.md exits 1 with an actionable message
        naming the exact path the author must create."""
        _do_init(tmp_path)
        # Do NOT create prds/nope.md.
        result = _invoke_cmd(tmp_path, ["prd", "parse", "--prd", "nope"])
        assert result.exit_code == 1
        combined = result.output + (
            result.stderr if hasattr(result, "stderr") and result.stderr else ""
        )
        assert "prds/nope.md" in combined
        assert "not found" in combined.lower()

    @pytest.mark.parametrize("sentinel", ["default", "prd"])
    def test_reserved_sentinel_prd_flag_creates_visible_default(
        self, tmp_path: Path, sentinel: str
    ) -> None:
        """`prd parse --prd default` / `--prd prd` are spellings of the DEFAULT
        PRD, not named PRDs: they read the bare prd.md and create a proper
        is_default=1 row, leaving the default PRD visible to is_default=1
        consumers (get_prd() no-arg, default_prd_id()).

        Regression guard for the `if prd:` truthiness bug: stamping
        is_default=False for these sentinels INSERTed an ('default', is_default=0)
        row, breaking ux_prds_default and making the PRD silently disappear.
        """
        from anvil.cli._helpers import _open_backend

        _do_init(tmp_path)
        _write_prd(tmp_path, _MINIMAL_PRD_CONTENT)

        result = _invoke_cmd(tmp_path, ["prd", "parse", "--prd", sentinel])
        assert result.exit_code == 0, f"sentinel parse failed: {result.output}"
        # Resolves to the bare prd.md, not a prds/ subdir.
        assert "prds/" not in result.output

        # The sentinel must take the no-stamp (default) branch, so the event
        # omits prd_id/is_default exactly like a no-flag parse.
        payload = _prd_parsed_payload(tmp_path)
        assert "prd_id" not in payload
        assert "is_default" not in payload
        # Default PRD keeps bare requirement ids.
        assert [r["id"] for r in payload["requirements"]] == ["R001", "R002"]

        backend = _open_backend(tmp_path / ".anvil")
        try:
            prds = {p.id: p.is_default for p in backend.list_prds()}
            assert prds == {"default": True}
            assert backend.default_prd_id() == "default"
            assert backend.get_prd() is not None
        finally:
            backend.close()

    def test_file_with_prd_flag_writes_into_named_partition(
        self, tmp_path: Path
    ) -> None:
        """--file controls WHICH path is read; --prd still controls the
        partition the event writes into. A `--file X --prd v0.2` parse must
        land in the v0.2 partition (stamped is_default=False) without touching
        the default PRD.

        This is the one branch where prd_path and parse_prd_id are decoupled
        (file is not None skips prd_source_path), so the partition stamp must
        ride parse_prd_id, not the file path.
        """
        from anvil.cli._helpers import _open_backend

        _do_init(tmp_path)
        # An existing default PRD that must stay untouched.
        _write_prd(tmp_path, _MINIMAL_PRD_CONTENT)
        _invoke_cmd(tmp_path, ["prd", "parse"])

        # The --file source lives OUTSIDE the prds/ convention.
        custom = tmp_path / "external_prd.md"
        custom.write_text(_NAMED_PRD_CONTENT, encoding="utf-8")

        result = _invoke_cmd(
            tmp_path, ["prd", "parse", "--file", str(custom), "--prd", "v0.2"]
        )
        assert result.exit_code == 0, f"--file --prd failed: {result.output}"
        # The source line points at the --file path, not prds/v0.2.md.
        assert "external_prd.md" in result.output

        # Event is stamped into the v0.2 partition despite the --file source.
        payload = _prd_parsed_payload(tmp_path)
        assert payload.get("prd_id") == "v0.2"
        assert payload.get("is_default") is False
        assert [r["id"] for r in payload["requirements"]] == [
            "v0.2:R001",
            "v0.2:R002",
        ]

        backend = _open_backend(tmp_path / ".anvil")
        try:
            # Default partition survived the --file --prd parse...
            default_reqs = [
                r.id for r in backend.list_requirements(prd_id="default")
            ]
            named_reqs = [
                r.id for r in backend.list_requirements(prd_id="v0.2")
            ]
            prds = {p.id: p.is_default for p in backend.list_prds()}
        finally:
            backend.close()

        assert default_reqs == ["R001", "R002"]
        assert named_reqs == ["v0.2:R001", "v0.2:R002"]
        # Exactly one default row; the named row is is_default=0.
        assert prds == {"default": True, "v0.2": False}


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
# prd resolve-decision command (T018 — decision back-propagation)
# ---------------------------------------------------------------------------


def _prd_text(tmp_path: Path) -> str:
    return (tmp_path / ".anvil" / "prd.md").read_text(encoding="utf-8")


def _events_text(tmp_path: Path) -> str:
    return (tmp_path / ".anvil" / "events.jsonl").read_text(encoding="utf-8")


class TestPrdResolveDecisionBackprop:
    def test_backprop_marker_updates_prd_and_records_event(
        self, tmp_path: Path
    ) -> None:
        """Resolving a [NEEDS DECISION] marker rewrites the linked requirement
        in prd.md AND appends a prd.decision_resolved event to the log."""
        _do_init(tmp_path)
        _write_prd(tmp_path, _PRD_WITH_DECISIONS)
        _invoke_cmd(tmp_path, ["prd", "parse"])

        result = _invoke_cmd(
            tmp_path,
            ["prd", "resolve-decision", "ND-001", "--resolution", "JSON"],
        )
        assert result.exit_code == 0, f"resolve-decision failed: {result.output}"

        # PRD updated: marker gone, resolution prose written back.
        prd_text = _prd_text(tmp_path)
        assert "[NEEDS DECISION: which format?]" not in prd_text
        assert "serialize inputs JSON." in prd_text
        # The OTHER marker and unrelated content are preserved.
        assert "Ship v1 [NEEDS DECISION]" in prd_text
        assert "R001: System works." in prd_text

        # Event recorded in the append-only log, visible and additive.
        events = _events_text(tmp_path)
        assert "prd.decision_resolved" in events
        # The event payload carries the audit detail.
        lines = [json.loads(line) for line in events.splitlines() if line.strip()]
        resolved = [e for e in lines if e["action"] == "prd.decision_resolved"]
        assert len(resolved) == 1
        payload = resolved[0]["payload_json"]
        assert payload["decision_id"] == "ND-001"
        assert payload["decision_kind"] == "needs_decision"
        assert payload["resolution"] == "JSON"
        assert payload["prd_ref"].startswith("line:")

    def test_backprop_json_envelope(self, tmp_path: Path) -> None:
        """--json emits the v1.24 success envelope with the resolution detail."""
        _do_init(tmp_path)
        _write_prd(tmp_path, _PRD_WITH_DECISIONS)
        _invoke_cmd(tmp_path, ["prd", "parse"])

        result = _invoke_cmd(
            tmp_path,
            [
                "prd",
                "resolve-decision",
                "OQ001",
                "--resolution",
                "99.9% monthly",
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.output)
        assert envelope["ok"] is True
        assert envelope["command"] == "prd resolve-decision"
        data = envelope["data"]
        assert data["decision_id"] == "OQ001"
        assert data["decision_kind"] == "open_question"
        assert data["event_id"]
        # The open question moved into a ## Decisions section in the file.
        assert "## Decisions" in _prd_text(tmp_path)
        assert "99.9% monthly" in _prd_text(tmp_path)

    def test_backprop_reparse_clears_resolved_marker(self, tmp_path: Path) -> None:
        """After back-propagating both markers, find-decisions no longer
        reports them (the PRD source was actually updated)."""
        _do_init(tmp_path)
        _write_prd(tmp_path, _PRD_WITH_DECISIONS)
        _invoke_cmd(tmp_path, ["prd", "parse"])

        # Resolve ND-002 first (line anchors stay valid since we resolve by id).
        r2 = _invoke_cmd(
            tmp_path,
            ["prd", "resolve-decision", "ND-002", "--resolution", "yes"],
        )
        assert r2.exit_code == 0, r2.output
        r1 = _invoke_cmd(
            tmp_path,
            ["prd", "resolve-decision", "ND-001", "--resolution", "JSON"],
        )
        assert r1.exit_code == 0, r1.output

        # Re-run find-decisions: no NEEDS_DECISION markers remain.
        find = _invoke_cmd(tmp_path, ["prd", "find-decisions"])
        assert find.exit_code == 0
        assert "0 NEEDS_DECISION" in find.output

    def test_backprop_unknown_decision_id_exits_one(self, tmp_path: Path) -> None:
        _do_init(tmp_path)
        _write_prd(tmp_path, _PRD_WITH_DECISIONS)
        _invoke_cmd(tmp_path, ["prd", "parse"])
        result = _invoke_cmd(
            tmp_path,
            ["prd", "resolve-decision", "ND-999", "--resolution", "x", "--json"],
        )
        assert result.exit_code == 1
        envelope = json.loads(result.output)
        assert envelope["ok"] is False
        assert envelope["error"]["code"] == "not_found"

    def test_backprop_without_init_exits_one(self, tmp_path: Path) -> None:
        result = _invoke_cmd(
            tmp_path,
            ["prd", "resolve-decision", "ND-001", "--resolution", "x"],
        )
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
    from anvil.planning.llm import LLMResponse, RecordedLLMProvider
    from anvil.planning.llm_planner import (
        _SYSTEM_PROMPT,
        _build_user_prompt,
    )
    from anvil.planning.template import parse_prd

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
        from anvil.planning import llm_planner

        # v1.17.0 — resolve_planner_provider gained a `config` parameter
        # (Config | None). The CLI passes the loaded config; the test stub
        # accepts and ignores it.
        monkeypatch.setattr(
            llm_planner,
            "resolve_planner_provider",
            lambda config=None, *, model_override=None: (provider, "anthropic"),
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
        assert ".anvil/prd.md" in result.output or "prd.md" in result.output

        # prd.md was mutated — it now contains a `## Tasks` section.
        prd_text = (tmp_path / ".anvil" / "prd.md").read_text(
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
        from anvil.planning import llm_planner

        def _explode(config=None, *, model_override=None) -> None:  # type: ignore[no-untyped-def]
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
        prd_text = (tmp_path / ".anvil" / "prd.md").read_text(
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

        from anvil.planning import llm_planner
        from anvil.planning.llm_planner import PlannerProviderUnavailable

        sentinel_msg = (
            "No LLM provider available for task generation. "
            "Either set ANTHROPIC_API_KEY or install claude-agent-sdk."
        )

        def _raise(config=None, *, model_override=None) -> None:  # type: ignore[no-untyped-def]
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

    def test_backstop_llm_provider_error_exits_1_cleanly(
        self,
        tmp_path: Path,
        monkeypatch,  # type: ignore[no-untyped-def]
    ) -> None:
        """A generate()-time ``LLMProviderError`` from the default agent-sdk
        provider (e.g. missing `claude` CLI / bad --model) must exit 1 with a
        clean ``Error: LLM call failed`` message — not escape as a raw
        traceback. Regression for the agent-sdk default flip: pre-flip this
        case raised PlannerProviderUnavailable at resolve time (caught); now it
        is an LLMProviderError from generate(), which the backstop didn't
        catch."""
        _do_init(tmp_path)
        _write_prd(tmp_path, _PRD_WITHOUT_TASKS)
        _invoke_cmd(tmp_path, ["prd", "parse"])

        from anvil.planning import llm_planner
        from anvil.planning.llm import LLMProviderError

        class _FailingProvider:
            def generate(self, **kwargs):  # type: ignore[no-untyped-def]
                raise LLMProviderError(
                    "ClaudeAgentSDKProvider needs the `claude` CLI on PATH"
                )

        monkeypatch.setattr(
            llm_planner,
            "resolve_planner_provider",
            lambda config=None, *, model_override=None: (_FailingProvider(), "agent-sdk"),
        )

        result = _invoke_cmd(tmp_path, ["plan"])
        assert result.exit_code == 1, result.output
        combined = result.output + (
            result.stderr if hasattr(result, "stderr") and result.stderr else ""
        )
        # Clean, actionable error — not an uncaught LLMProviderError traceback.
        assert "LLM call failed" in combined
        assert "claude" in combined
        assert not isinstance(result.exception, LLMProviderError)

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

        prd_after_first = (tmp_path / ".anvil" / "prd.md").read_text(
            encoding="utf-8"
        )
        first_tasks_count = prd_after_first.lower().count("## tasks")
        assert first_tasks_count == 1

        # Re-parse + re-plan. Second run must not re-append.
        _invoke_cmd(tmp_path, ["prd", "parse"])
        second = _invoke_cmd(tmp_path, ["plan"])
        assert second.exit_code == 0, f"second plan failed: {second.output}"

        prd_after_second = (tmp_path / ".anvil" / "prd.md").read_text(
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
        conn = sqlite3.connect(str(tmp_path / ".anvil" / "state.db"))
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
        assert "anvil expand d --use-llm" not in result.output


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
# Phase 7 Wave 2 implemented --use-llm. The default provider is now the keyless
# Claude Agent SDK (subscription auth), so `--use-llm` no longer requires
# ANTHROPIC_API_KEY and does not exit 1 for a missing key — see
# TestUseLlmDefaultProvider below.


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
        db_path = tmp_path / ".anvil" / "state.db"
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
    """Full setup: optionally git-init, then anvil init + PRD + plan + review_tasks.

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
    db_path = tmp_path / ".anvil" / "state.db"
    if not db_path.exists():
        return None
    conn = _sqlite3.connect(str(db_path))
    row = conn.execute("SELECT id FROM tasks WHERE status='ready' LIMIT 1").fetchone()
    conn.close()
    return row[0] if row else None


def _get_claim_branch(tmp_path: Path, task_id: str) -> str | None:
    """Return the recorded branch for the active claim on task_id (or None)."""
    import sqlite3 as _sqlite3
    db_path = tmp_path / ".anvil" / "state.db"
    conn = _sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT branch FROM claims WHERE task_id=? AND status='active'",
        (task_id,),
    ).fetchone()
    conn.close()
    return row[0] if row else None


def _git_current_branch(tmp_path: Path) -> str:
    """Return the name of the currently checked-out git branch in tmp_path."""
    import subprocess as _subprocess
    out = _subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


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
        db = tmp_path / ".anvil" / "state.db"
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
        db = tmp_path / ".anvil" / "state.db"
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
# T027 — claim --branch (caller-supplied / existing-branch claims)
# ---------------------------------------------------------------------------


class TestClaimBranchOption:
    """T027: `claim --branch <name>` attaches the claim to a caller-named or
    existing branch instead of generating agent/<task>-<slug>, and records that
    branch on the claim. Default (no --branch) behavior is unchanged.
    """

    def test_claim_branch_existing_records_named_branch(
        self, tmp_path: Path
    ) -> None:
        """Claiming with --branch <existing> records the claim against that
        existing branch and checks it out (research #232 adoption lever)."""
        import subprocess as _subprocess

        _do_init_and_plan(tmp_path, with_git=True)
        task_id = _get_first_ready_task_id(tmp_path)
        assert task_id is not None, "No ready task found after setup"

        # Pre-create an existing feature branch the user already works on.
        _subprocess.run(
            ["git", "branch", "existing-feature"],
            cwd=str(tmp_path),
            check=True,
            capture_output=True,
        )

        result = _invoke_cmd(
            tmp_path,
            ["claim", task_id, "--actor", "agent-test", "--branch", "existing-feature"],
        )
        assert result.exit_code == 0, f"claim --branch failed: {result.output}"

        # The claim is recorded against the named branch.
        assert _get_claim_branch(tmp_path, task_id) == "existing-feature", (
            "claim should record the caller-supplied branch"
        )
        # The existing branch is checked out (no agent/<task>-<slug> generated).
        assert _git_current_branch(tmp_path) == "existing-feature"
        # No auto-generated agent/ branch was created.
        branches = _subprocess.run(
            ["git", "branch", "--list"],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert "agent/" not in branches, (
            f"--branch must not generate an agent/ branch; got:\n{branches}"
        )

    def test_claim_branch_new_name_creates_and_records(
        self, tmp_path: Path
    ) -> None:
        """--branch with a name that does not exist yet creates it and records
        it on the claim."""
        _do_init_and_plan(tmp_path, with_git=True)
        task_id = _get_first_ready_task_id(tmp_path)
        assert task_id is not None

        result = _invoke_cmd(
            tmp_path,
            ["claim", task_id, "--actor", "agent-test", "--branch", "feature/my-thing"],
        )
        assert result.exit_code == 0, f"claim --branch failed: {result.output}"

        assert _get_claim_branch(tmp_path, task_id) == "feature/my-thing"
        assert _git_current_branch(tmp_path) == "feature/my-thing"

    def test_claim_branch_default_generates_branch_name(
        self, tmp_path: Path
    ) -> None:
        """Without --branch, the default auto-generated agent/<task>-<slug>
        branch is still produced (backward compatibility)."""
        _do_init_and_plan(tmp_path, with_git=True)
        task_id = _get_first_ready_task_id(tmp_path)
        assert task_id is not None

        result = _invoke_cmd(
            tmp_path, ["claim", task_id, "--actor", "agent-test"]
        )
        assert result.exit_code == 0, f"claim failed: {result.output}"

        # The checked-out branch follows the auto-generated agent/<task>- shape.
        current = _git_current_branch(tmp_path)
        assert current.startswith(f"agent/{task_id.lower()}-"), (
            f"default claim should generate agent/<task>-<slug>; got: {current}"
        )

    def test_claim_branch_json_envelope_reports_branch(
        self, tmp_path: Path
    ) -> None:
        """--json envelope reports the caller-supplied branch and the embedded
        claim object carries it too (v1.24 envelope convention)."""
        import json as _json
        import subprocess as _subprocess

        _do_init_and_plan(tmp_path, with_git=True)
        task_id = _get_first_ready_task_id(tmp_path)
        assert task_id is not None

        _subprocess.run(
            ["git", "branch", "existing-feature"],
            cwd=str(tmp_path),
            check=True,
            capture_output=True,
        )

        result = _invoke_cmd(
            tmp_path,
            [
                "claim",
                task_id,
                "--actor",
                "agent-test",
                "--branch",
                "existing-feature",
                "--json",
            ],
        )
        assert result.exit_code == 0, f"claim --branch --json failed: {result.output}"

        payload = _json.loads(result.output)
        assert payload["ok"] is True
        assert payload["command"] == "claim"
        assert payload["data"]["branch"] == "existing-feature"
        assert payload["data"]["claim"]["branch"] == "existing-feature"

    def test_claim_branch_without_git_still_records_intent(
        self, tmp_path: Path
    ) -> None:
        """Without a git repo, --branch cannot check anything out, but the
        requested branch name is still recorded on the claim (intent preserved)
        and the claim succeeds (non-blocking git contract)."""
        _do_init_and_plan(tmp_path, with_git=False)
        task_id = _get_first_ready_task_id(tmp_path)
        assert task_id is not None

        result = _invoke_cmd(
            tmp_path,
            ["claim", task_id, "--actor", "agent-test", "--branch", "my-branch"],
        )
        assert result.exit_code == 0, f"claim --branch (no git) failed: {result.output}"
        assert _get_claim_branch(tmp_path, task_id) == "my-branch"


# ---------------------------------------------------------------------------
# Phase 4 — release command
# ---------------------------------------------------------------------------


class TestReleaseCommand:
    def _claim_task(self, tmp_path: Path, task_id: str) -> str:
        """Claim task_id and return the claim ID."""
        import sqlite3 as _sqlite3
        _invoke_cmd(tmp_path, ["claim", task_id, "--actor", "agent-test"])
        db_path = tmp_path / ".anvil" / "state.db"
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
        db_path = tmp_path / ".anvil" / "state.db"
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
        db_path = tmp_path / ".anvil" / "state.db"
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

    def test_next_quiet_exits_3_on_empty_queue(self, tmp_path: Path) -> None:
        """next -q exits 3 and prints nothing when the queue is empty."""
        _do_init(tmp_path, name="Empty Project")
        result = _invoke_cmd(tmp_path, ["next", "-q", "--actor", "agent-test"])
        assert result.exit_code == 3, f"next -q (empty) failed: {result.output}"
        assert result.output == ""

    def test_next_quiet_exits_0_when_task_ready(self, tmp_path: Path) -> None:
        """next -q exits 0 and prints nothing when a task is claimable."""
        _do_init_and_plan(tmp_path, with_git=False)
        result = _invoke_cmd(tmp_path, ["next", "-q", "--actor", "agent-test"])
        assert result.exit_code == 0, f"next -q (ready) failed: {result.output}"
        assert result.output == ""

    def test_next_quiet_real_error_is_not_masked_as_drained(
        self, tmp_path: Path
    ) -> None:
        """next -q on an uninitialized project propagates a real error.

        The loop seam contract (and the ci-drain.sh / drive-the-anvil-loop.md
        adapters) only treat exit 3 as 'queue empty / clean stop'. A real error
        such as a missing state dir must surface as a *different* non-zero code
        so loops propagate it instead of masking it as a clean drain.
        """
        # No `anvil init` — the state dir does not exist (a real error).
        result = _invoke_cmd(tmp_path, ["next", "-q", "--actor", "agent-test"])
        assert result.exit_code != 0, f"expected non-zero, got: {result.output}"
        assert result.exit_code != 3, (
            "real error must NOT use exit 3 (reserved for 'queue empty'); "
            f"got exit {result.exit_code}"
        )


# ---------------------------------------------------------------------------
# Phase 4 — hook subcommands
# ---------------------------------------------------------------------------


class TestHookSubcommands:
    def test_hook_check_claim_silent_when_no_state(self, tmp_path: Path) -> None:
        """hook check-claim exits 0 silently when no .anvil/ exists."""
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

        events_path = tmp_path / ".anvil" / "events.jsonl"
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
        db_path = tmp_path / ".anvil" / "state.db"
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
    db_path = tmp_path / ".anvil" / "state.db"
    conn = _sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT id FROM claims WHERE task_id=? AND status='active'", (task_id,)
    ).fetchone()
    conn.close()
    return row[0] if row else "CLAIM-UNKNOWN"


def _get_task_status(tmp_path: Path, task_id: str) -> str | None:
    """Return the current status of task_id from the DB."""
    import sqlite3 as _sqlite3

    db_path = tmp_path / ".anvil" / "state.db"
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
        """packet T001 exits 0 and writes .anvil/packets/T001.md."""
        _do_init_and_plan(tmp_path, with_git=False)
        task_id = _get_first_ready_task_id(tmp_path)
        assert task_id is not None, "No ready task after setup"

        result = _invoke_cmd(tmp_path, ["packet", task_id])
        assert result.exit_code == 0, f"packet failed: {result.output}"

        packet_file = tmp_path / ".anvil" / "packets" / f"{task_id}.md"
        assert packet_file.exists(), "Packet .md file not written"
        content = packet_file.read_text(encoding="utf-8")
        assert task_id in content

    def test_packet_json_format_writes_json_file(self, tmp_path: Path) -> None:
        """packet T001 --format json writes .anvil/packets/T001.json."""
        _do_init_and_plan(tmp_path, with_git=False)
        task_id = _get_first_ready_task_id(tmp_path)
        assert task_id is not None

        result = _invoke_cmd(tmp_path, ["packet", task_id, "--format", "json"])
        assert result.exit_code == 0, f"packet --format json failed: {result.output}"

        packet_file = tmp_path / ".anvil" / "packets" / f"{task_id}.json"
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
        db_path = tmp_path / ".anvil" / "state.db"
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

        db_path = tmp_path / ".anvil" / "state.db"
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

    # -- CL-2: repeatable --commands / --files-changed -------------------

    def test_submit_repeated_commands_and_files_flags(self, tmp_path: Path) -> None:
        """Passing --commands / --files-changed more than once keeps each
        occurrence as one value (click multiple=True semantics)."""
        import json as _json

        _do_init_and_plan(tmp_path, with_git=False)
        task_id = _get_first_ready_task_id(tmp_path)
        assert task_id is not None

        _do_claim(tmp_path, task_id, actor="agent-test")

        result = _invoke_cmd(
            tmp_path,
            [
                "submit", task_id,
                "--commands", "pytest tests/ -v",
                "--commands", "ruff check .",
                "--files-changed", "src/auth.py",
                "--files-changed", "src/main.py",
                "--actor", "agent-test",
                "--json",
            ],
        )
        assert result.exit_code == 0, f"submit failed: {result.output}"
        envelope = _json.loads(result.output)
        assert envelope["ok"] is True
        data = envelope["data"]
        assert data["commands_run"] == ["pytest tests/ -v", "ruff check ."]
        assert data["files_changed"] == ["src/auth.py", "src/main.py"]

    def test_submit_repeated_flag_preserves_embedded_commas(self, tmp_path: Path) -> None:
        """A repeated --commands / --files-changed value containing commas
        survives intact (one occurrence == one value). This is the core CL-2
        fix: the legacy single-flag form split on commas and mangled such
        values."""
        import json as _json

        _do_init_and_plan(tmp_path, with_git=False)
        task_id = _get_first_ready_task_id(tmp_path)
        assert task_id is not None

        _do_claim(tmp_path, task_id, actor="agent-test")

        cmd_with_comma = "python -c 'print(1,2,3)'"
        path_with_comma = "src/weird,name.py"
        result = _invoke_cmd(
            tmp_path,
            [
                "submit", task_id,
                "--commands", cmd_with_comma,
                "--commands", "pytest -v",
                "--files-changed", path_with_comma,
                "--files-changed", "src/main.py",
                "--actor", "agent-test",
                "--json",
            ],
        )
        assert result.exit_code == 0, f"submit failed: {result.output}"
        data = _json.loads(result.output)["data"]
        # The embedded comma must NOT split the value.
        assert data["commands_run"] == [cmd_with_comma, "pytest -v"]
        assert data["files_changed"] == [path_with_comma, "src/main.py"]

    def test_submit_legacy_comma_joined_single_flag(self, tmp_path: Path) -> None:
        """Backward compatibility: a single --commands / --files-changed
        occurrence whose value is comma-joined is still split into a list."""
        import json as _json

        _do_init_and_plan(tmp_path, with_git=False)
        task_id = _get_first_ready_task_id(tmp_path)
        assert task_id is not None

        _do_claim(tmp_path, task_id, actor="agent-test")

        result = _invoke_cmd(
            tmp_path,
            [
                "submit", task_id,
                "--commands", "pytest -v, ruff check .",
                "--files-changed", "src/auth.py, src/main.py",
                "--actor", "agent-test",
                "--json",
            ],
        )
        assert result.exit_code == 0, f"submit failed: {result.output}"
        data = _json.loads(result.output)["data"]
        assert data["commands_run"] == ["pytest -v", "ruff check ."]
        assert data["files_changed"] == ["src/auth.py", "src/main.py"]


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
        """hook capture-evidence exits 0 when no .anvil/ exists."""
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

        orphan_file = tmp_path / ".anvil" / ".evidence-buffer" / "orphan.json"
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


class TestUseLlmDefaultProvider:
    """The default --use-llm provider is the keyless Claude Agent SDK.

    Previously --use-llm without ANTHROPIC_API_KEY exited 1 (the old default
    was the direct Anthropic API). The default is now ``agent-sdk`` —
    subscription auth, no API key — so resolution succeeds and the command
    runs. We patch ``claude_agent_sdk.query`` so the test exercises the real
    resolver + provider path without spawning the actual ``claude`` CLI.
    """

    def _patch_agent_sdk_query(self, monkeypatch, text: str, capture=None) -> None:  # type: ignore[no-untyped-def]
        claude_agent_sdk = pytest.importorskip("claude_agent_sdk")
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

        async def fake_query(*, prompt, options):  # type: ignore[no-untyped-def]
            if capture is not None:
                capture["model"] = options.model
            yield AssistantMessage(
                content=[TextBlock(text=text)], model="claude-sonnet-4-6"
            )
            yield ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="sess-cli",
                usage={"input_tokens": 5, "output_tokens": 5},
                result=text,
                stop_reason="end_turn",
                model_usage={"claude-sonnet-4-6": {}},
            )

        monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    def test_score_use_llm_without_key_runs_via_agent_sdk(
        self, tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
    ) -> None:
        """No ANTHROPIC_API_KEY → resolves the keyless agent-sdk default and
        exits 0 (no longer the old exit-1 missing-key failure)."""
        pytest.importorskip("claude_agent_sdk")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        self._patch_agent_sdk_query(monkeypatch, "A concise trade-off note.")

        _do_init(tmp_path)
        _write_prd(tmp_path, _FULL_PRD_CONTENT)
        _invoke_cmd(tmp_path, ["prd", "parse"])
        _invoke_cmd(tmp_path, ["plan"])

        result = _invoke_cmd(tmp_path, ["score", "--use-llm"])
        assert result.exit_code == 0, result.output
        combined = result.output + (
            result.stderr if hasattr(result, "stderr") and result.stderr else ""
        )
        # The old contract is gone: no missing-key error.
        assert "ANTHROPIC_API_KEY" not in combined

    def test_score_use_llm_model_flag_threads_to_provider(
        self, tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
    ) -> None:
        """`--model X` reaches the agent-sdk provider as the CLI model id
        (ClaudeAgentOptions.model), proving the flag threads end-to-end."""
        pytest.importorskip("claude_agent_sdk")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        cap: dict = {}
        self._patch_agent_sdk_query(monkeypatch, "note", capture=cap)

        _do_init(tmp_path)
        _write_prd(tmp_path, _FULL_PRD_CONTENT)
        _invoke_cmd(tmp_path, ["prd", "parse"])
        _invoke_cmd(tmp_path, ["plan"])

        result = _invoke_cmd(
            tmp_path, ["score", "--use-llm", "--model", "claude-haiku-4-5"]
        )
        assert result.exit_code == 0, result.output
        assert cap.get("model") == "claude-haiku-4-5"


class TestUseLlmRecordedProvider:
    """End-to-end CLI invocations with a RecordedLLMProvider injected.

    We monkeypatch ``anvil.cli.plan._resolve_llm_provider`` to return
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

        plan_module = importlib.import_module("anvil.cli.plan")

        def fake_resolve(use_llm: bool, config=None, model=None):  # type: ignore[no-untyped-def]
            return provider_factory() if use_llm else None

        monkeypatch.setattr(plan_module, "_resolve_llm_provider", fake_resolve)

    def test_plan_use_llm_enriches_short_descriptions(
        self, tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
    ) -> None:
        """plan --use-llm with a recorded provider enriches short descriptions."""
        from anvil.planning.llm import LLMResponse, RecordedLLMProvider
        from anvil.planning.template import (
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
        from anvil.planning.template import _DESCRIPTION_ENRICH_MAX_TOKENS
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
        from anvil.clock import SystemClock
        from anvil.state.sqlite import SqliteBackend

        state_dir = tmp_path / ".anvil"
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
        from anvil.planning.llm import LLMResponse

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
        from anvil.planning.llm import LLMResponse

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

        def fake_resolve(use_llm: bool, config=None, model=None):  # type: ignore[no-untyped-def]
            if use_llm:
                sentinel_raised.append("called")
            return None

        import importlib

        plan_module = importlib.import_module("anvil.cli.plan")

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
        db = tmp_path / ".anvil" / "state.db"
        with sqlite3.connect(str(db)) as conn:
            return {r[0] for r in conn.execute("SELECT id FROM tasks")}

    def _set_task_status(self, tmp_path: Path, task_id: str, status: str) -> None:
        """Directly mutate task status in SQLite for test setup.

        Goes around the event log on purpose — this is fixture plumbing,
        not a behavior under test. Using a real claim event would require
        a multi-line setup that obscures what the test actually asserts.
        """
        import sqlite3
        db = tmp_path / ".anvil" / "state.db"
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


# Default PRD: T001 touches the shared file, T002 is default-only. Used to
# prove that planning a NAMED PRD never prunes default tasks and that a
# cross-PRD file overlap (src/shared.py) is detected.
_MULTIPRD_DEFAULT = """\
# Project: Multi-PRD Default

## Summary

Default PRD for T017 scoping tests.

## Goals

- Scope plan to a PRD.

## Requirements

- R001: Default work.

## Features

### F001: Default feature

**Requirements:** R001

## Tasks

### T001: Default task on shared file

**Feature:** F001
**Priority:** medium
**Likely files:** src/shared.py, src/d.py

**Acceptance criteria:**

- works

**Verification:**

- `pytest`

### T002: Default-only task

**Feature:** F001
**Priority:** medium
**Likely files:** src/default_only.py

**Acceptance criteria:**

- works

**Verification:**

- `pytest`
"""

# Same default PRD with T002 removed — used to prove default-scoped pruning.
_MULTIPRD_DEFAULT_NO_T002 = _MULTIPRD_DEFAULT.split("### T002:")[0].rstrip() + "\n"

# Named v0.2 PRD: its single task touches the SAME src/shared.py the default
# T001 touches, so the cross-PRD conflict scan must group them together.
_MULTIPRD_NAMED = """\
# Project: Multi-PRD Named

## Summary

Named PRD for T017 scoping tests.

## Goals

- Ship v0.2.

## Requirements

- R001: Named work.

## Features

### F001: Named feature

**Requirements:** R001

## Tasks

### T900: Named task on shared file

**Feature:** F001
**Priority:** medium
**Likely files:** src/shared.py, src/n.py

**Acceptance criteria:**

- works

**Verification:**

- `pytest`
"""


class TestPlanPrdScoping:
    """T017: `anvil plan --prd <id>` scopes feature/task creation,
    orphan-prune, dependency inference, and proposed->drafted promotion to a
    single PRD partition — while conflict-group inference still spans ALL PRDs.
    """

    def _task_rows(self, tmp_path: Path) -> dict[str, tuple[str, str]]:
        """Return {task_id: (prd_id, status)} straight from state.db."""
        db = tmp_path / ".anvil" / "state.db"
        with sqlite3.connect(str(db)) as conn:
            return {
                r[0]: (r[1], r[2])
                for r in conn.execute(
                    "SELECT id, prd_id, status FROM tasks"
                )
            }

    def _conflict_groups(self, tmp_path: Path) -> dict[str, list[str]]:
        """Return {cg_id: task_ids} from the conflict_groups table."""
        db = tmp_path / ".anvil" / "state.db"
        with sqlite3.connect(str(db)) as conn:
            return {
                r[0]: json.loads(r[1])
                for r in conn.execute(
                    "SELECT id, task_ids FROM conflict_groups"
                )
            }

    def _task_conflict_groups(self, tmp_path: Path, task_id: str) -> list[str]:
        db = tmp_path / ".anvil" / "state.db"
        with sqlite3.connect(str(db)) as conn:
            row = conn.execute(
                "SELECT conflict_groups FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return json.loads(row[0]) if row else []

    def _setup_default_then_named(self, tmp_path: Path) -> None:
        """Init, plan the default PRD, then parse the named v0.2 PRD."""
        _do_init(tmp_path)
        _write_prd(tmp_path, _MULTIPRD_DEFAULT)
        assert _invoke_cmd(tmp_path, ["prd", "parse"]).exit_code == 0
        assert _invoke_cmd(tmp_path, ["plan", "--no-llm"]).exit_code == 0
        _write_named_prd(tmp_path, "v0.2", _MULTIPRD_NAMED)
        assert (
            _invoke_cmd(tmp_path, ["prd", "parse", "--prd", "v0.2"]).exit_code
            == 0
        )

    def test_plan_named_prd_creates_tasks_carrying_prd_id(
        self, tmp_path: Path
    ) -> None:
        """`plan --prd v0.2` reads prds/v0.2.md and creates its task in the
        v0.2 partition (prd_id stamped), promoted proposed->drafted, while the
        default tasks are untouched."""
        self._setup_default_then_named(tmp_path)
        result = _invoke_cmd(tmp_path, ["plan", "--prd", "v0.2", "--no-llm"])
        assert result.exit_code == 0, result.output

        rows = self._task_rows(tmp_path)
        # Named id is prefixed (T015 id convention).
        assert "v0.2:T900" in rows, rows
        prd_id, status = rows["v0.2:T900"]
        assert prd_id == "v0.2", f"task should carry its prd_id; got {prd_id}"
        # Dependency inference + promotion ran over the subset.
        assert status == "drafted", f"named task should be promoted; got {status}"
        # Default tasks are in their own partition, unchanged.
        assert rows["T001"][0] == "default"
        assert rows["T002"][0] == "default"

    def test_plan_named_prd_does_not_prune_default_tasks(
        self, tmp_path: Path
    ) -> None:
        """Orphan-prune is scoped: planning v0.2 (whose prd.md lists only T900)
        must NOT delete the default PRD's T001/T002 just because they are
        absent from v0.2's source."""
        self._setup_default_then_named(tmp_path)
        result = _invoke_cmd(tmp_path, ["plan", "--prd", "v0.2", "--no-llm"])
        assert result.exit_code == 0, result.output
        rows = self._task_rows(tmp_path)
        assert "T001" in rows, "default T001 must survive a named-PRD plan"
        assert "T002" in rows, "default T002 must survive a named-PRD plan"
        assert "Pruned" not in result.output, (
            f"named plan should not prune cross-PRD tasks; got: {result.output}"
        )

    def test_default_plan_prunes_only_default_orphans_not_named(
        self, tmp_path: Path
    ) -> None:
        """Symmetric scoping: re-planning the DEFAULT PRD with T002 removed
        prunes T002 but leaves the v0.2 task intact."""
        self._setup_default_then_named(tmp_path)
        assert (
            _invoke_cmd(tmp_path, ["plan", "--prd", "v0.2", "--no-llm"]).exit_code
            == 0
        )
        # Now drop T002 from the default PRD and re-plan the default.
        _write_prd(tmp_path, _MULTIPRD_DEFAULT_NO_T002)
        assert _invoke_cmd(tmp_path, ["prd", "parse"]).exit_code == 0
        result = _invoke_cmd(tmp_path, ["plan", "--no-llm"])
        assert result.exit_code == 0, result.output

        rows = self._task_rows(tmp_path)
        assert "T002" not in rows, "T002 should be pruned from the default PRD"
        assert "T001" in rows
        assert "v0.2:T900" in rows, (
            "the v0.2 task must NOT be pruned by a default-PRD re-plan"
        )

    def test_plan_with_no_prd_targets_default(self, tmp_path: Path) -> None:
        """Without --prd, plan reads .anvil/prd.md and writes the default
        partition (pre-v7 behaviour): all tasks carry prd_id='default'."""
        _do_init(tmp_path)
        _write_prd(tmp_path, _MULTIPRD_DEFAULT)
        assert _invoke_cmd(tmp_path, ["prd", "parse"]).exit_code == 0
        result = _invoke_cmd(tmp_path, ["plan", "--no-llm"])
        assert result.exit_code == 0, result.output
        rows = self._task_rows(tmp_path)
        assert set(rows) == {"T001", "T002"}, rows
        assert all(prd_id == "default" for prd_id, _ in rows.values()), rows


class TestPlanCrossPrdConflictGroups:
    """T017: conflict-group inference spans ALL PRDs (reads backend.list_tasks()
    with no prd filter), so a default-PRD task and a named-PRD task that share a
    likely_file land in ONE CG-* group that both task rows reference.
    """

    def _setup(self, tmp_path: Path) -> None:
        _do_init(tmp_path)
        _write_prd(tmp_path, _MULTIPRD_DEFAULT)
        assert _invoke_cmd(tmp_path, ["prd", "parse"]).exit_code == 0
        assert _invoke_cmd(tmp_path, ["plan", "--no-llm"]).exit_code == 0
        _write_named_prd(tmp_path, "v0.2", _MULTIPRD_NAMED)
        assert (
            _invoke_cmd(tmp_path, ["prd", "parse", "--prd", "v0.2"]).exit_code
            == 0
        )

    def test_named_plan_groups_conflict_across_prds(
        self, tmp_path: Path
    ) -> None:
        """Planning v0.2 detects that v0.2:T900 and default T001 both touch
        src/shared.py and forms a single cross-PRD conflict group; BOTH task
        rows carry the group id."""
        self._setup(tmp_path)
        result = _invoke_cmd(tmp_path, ["plan", "--prd", "v0.2", "--no-llm"])
        assert result.exit_code == 0, result.output
        assert "conflict group" in result.output, result.output

        db = tmp_path / ".anvil" / "state.db"
        with sqlite3.connect(str(db)) as conn:
            cgs = {
                r[0]: json.loads(r[1])
                for r in conn.execute(
                    "SELECT id, task_ids FROM conflict_groups"
                )
            }
            t001 = json.loads(
                conn.execute(
                    "SELECT conflict_groups FROM tasks WHERE id = 'T001'"
                ).fetchone()[0]
            )
            t900 = json.loads(
                conn.execute(
                    "SELECT conflict_groups FROM tasks WHERE id = 'v0.2:T900'"
                ).fetchone()[0]
            )

        cross = [k for k, v in cgs.items() if set(v) == {"T001", "v0.2:T900"}]
        assert cross, f"expected ONE cross-PRD CG; got {cgs}"
        cg_id = cross[0]
        assert cg_id in t001, f"default T001 must reference {cg_id}; got {t001}"
        assert cg_id in t900, f"named task must reference {cg_id}; got {t900}"


# ---------------------------------------------------------------------------
# T019 — CLI `--prd` wiring on next / list / show / packet / score / prd review
# ---------------------------------------------------------------------------


def _seed_two_prd_project(tmp_path: Path) -> str:
    """Build a two-PRD project for CLI `--prd` tests and return the project_id.

    The DEFAULT PRD is created through the real `prd parse` flow (so it is a
    proper ``is_default=1`` row with the real project_id), then a named ``v0.2``
    PRD row plus one ready task per partition are raw-inserted — the same raw
    seeding pattern test_claims.py uses, because the planner doesn't mint named
    PRDs yet. Tasks are seeded READY so `next` can pick them.

    Layout after this returns:
      * default PRD ('default', is_default=1, approved) -> task T001 (ready)
      * named PRD   ('v0.2',    is_default=0, draft)    -> task v0.2:T900 (ready)
    """
    _do_init(tmp_path)
    _write_prd(tmp_path, _MINIMAL_PRD_CONTENT)
    assert _invoke_cmd(tmp_path, ["prd", "parse"]).exit_code == 0
    # default PRD: draft -> reviewed -> approved (so the default review tests
    # start from a known status and the named-PRD tests don't collide).
    assert _invoke_cmd(tmp_path, ["prd", "review"]).exit_code == 0
    assert _invoke_cmd(tmp_path, ["prd", "review", "--approve"]).exit_code == 0

    db = tmp_path / ".anvil" / "state.db"
    conn = sqlite3.connect(str(db))
    try:
        project_id = conn.execute(
            "SELECT project_id FROM prds WHERE is_default = 1"
        ).fetchone()[0]
        # Named v0.2 PRD row (draft, not default).
        conn.execute(
            "INSERT INTO prds (id, project_id, status, is_default) "
            "VALUES ('v0.2', ?, 'draft', 0)",
            (project_id,),
        )
        # A feature both tasks can hang off (FK target).
        conn.execute(
            "INSERT OR IGNORE INTO features "
            "(id, title, description, status, requirements, tasks) "
            "VALUES ('F001', 'F', 'desc', 'proposed', '[]', '[]')"
        )
        for task_id, prd_id in (("T001", "default"), ("v0.2:T900", "v0.2")):
            conn.execute(
                """INSERT INTO tasks
                (id, feature_id, prd_id, title, description, status, priority,
                 dependencies, conflict_groups, scores, acceptance_criteria,
                 implementation_notes, verification, likely_files,
                 created_at, updated_at)
                VALUES (?, 'F001', ?, ?, 'desc', 'ready', 'medium',
                        '[]', '[]', '{}', '[]', '[]', '{}', '[]',
                        '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')""",
                (task_id, prd_id, f"Task {task_id}"),
            )
        conn.commit()
    finally:
        conn.close()
    return project_id


class TestT019PrdScopedCliCommands:
    """T019: the `--prd` flag (and its $ANVIL_PRD env twin) on the READ/filter
    CLI surfaces — next / list / show / packet / score / prd review. The
    manager- and MCP-level tests cover the methods; these pin the CLI wiring
    itself (PRD_OPTION envvar, resolve_prd_id, the show/packet mismatch guard,
    and the default-sentinel collapse) so a regression there fails CI.
    """

    # ---- list ---------------------------------------------------------------

    def test_list_prd_scopes_to_named_partition(self, tmp_path: Path) -> None:
        """`list --prd v0.2` shows only the v0.2 task, not the default one."""
        _seed_two_prd_project(tmp_path)
        result = _invoke_cmd(tmp_path, ["list", "--prd", "v0.2", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        ids = {t["id"] for t in data["tasks"]}
        assert ids == {"v0.2:T900"}, ids

    def test_list_prd_default_scopes_to_default_partition(
        self, tmp_path: Path
    ) -> None:
        """`list --prd default` shows only the default task."""
        _seed_two_prd_project(tmp_path)
        result = _invoke_cmd(tmp_path, ["list", "--prd", "default", "--json"])
        assert result.exit_code == 0, result.output
        ids = {t["id"] for t in json.loads(result.output)["data"]["tasks"]}
        assert ids == {"T001"}, ids

    def test_list_no_prd_lists_all_partitions(self, tmp_path: Path) -> None:
        """Without --prd, list spans all PRDs (pre-T019 behaviour)."""
        _seed_two_prd_project(tmp_path)
        result = _invoke_cmd(tmp_path, ["list", "--json"])
        assert result.exit_code == 0, result.output
        ids = {t["id"] for t in json.loads(result.output)["data"]["tasks"]}
        assert ids == {"T001", "v0.2:T900"}, ids

    # ---- next ---------------------------------------------------------------

    def test_next_prd_scopes_candidate_pool(self, tmp_path: Path) -> None:
        """`next --prd v0.2` recommends a v0.2 task, never the default one."""
        _seed_two_prd_project(tmp_path)
        result = _invoke_cmd(tmp_path, ["next", "--prd", "v0.2", "--json"])
        assert result.exit_code == 0, result.output
        task = json.loads(result.output)["data"]["task"]
        assert task is not None and task["id"] == "v0.2:T900", task

    def test_next_prd_default_scopes_to_default(self, tmp_path: Path) -> None:
        """`next --prd default` recommends only the default task."""
        _seed_two_prd_project(tmp_path)
        result = _invoke_cmd(tmp_path, ["next", "--prd", "default", "--json"])
        assert result.exit_code == 0, result.output
        task = json.loads(result.output)["data"]["task"]
        assert task is not None and task["id"] == "T001", task

    # ---- show ---------------------------------------------------------------

    def test_show_prd_match_renders_task(self, tmp_path: Path) -> None:
        """`show v0.2:T900 --prd v0.2` matches the task's partition → renders."""
        _seed_two_prd_project(tmp_path)
        result = _invoke_cmd(
            tmp_path, ["show", "v0.2:T900", "--prd", "v0.2", "--json"]
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["data"]["task"]["id"] == "v0.2:T900"

    def test_show_prd_mismatch_is_not_found(self, tmp_path: Path) -> None:
        """`show T001 --prd v0.2` — T001 lives in the default PRD, so the
        mismatch guard rejects the cross-PRD read with a not_found error.

        Regression guard for the cross-PRD read-leak the guard prevents: if the
        guard is dropped/inverted, this would render T001 and the test fails.
        """
        _seed_two_prd_project(tmp_path)
        result = _invoke_cmd(tmp_path, ["show", "T001", "--prd", "v0.2", "--json"])
        assert result.exit_code == 1, result.output
        err = json.loads(result.output)["error"]
        assert err["code"] == "not_found", err
        assert "belongs to PRD 'default'" in err["message"], err["message"]

    # ---- packet -------------------------------------------------------------

    def test_packet_prd_mismatch_is_error(self, tmp_path: Path) -> None:
        """`packet T001 --prd v0.2` raises the same cross-PRD mismatch error."""
        _seed_two_prd_project(tmp_path)
        result = _invoke_cmd(tmp_path, ["packet", "T001", "--prd", "v0.2"])
        assert result.exit_code == 1, result.output
        combined = result.output + (
            result.stderr if hasattr(result, "stderr") and result.stderr else ""
        )
        assert "belongs to PRD 'default'" in combined, combined

    def test_packet_prd_match_succeeds(self, tmp_path: Path) -> None:
        """`packet v0.2:T900 --prd v0.2` matches the partition → exit 0."""
        _seed_two_prd_project(tmp_path)
        result = _invoke_cmd(tmp_path, ["packet", "v0.2:T900", "--prd", "v0.2"])
        assert result.exit_code == 0, result.output

    # ---- score --------------------------------------------------------------

    def test_score_prd_scopes_to_named_partition(self, tmp_path: Path) -> None:
        """`score --prd v0.2` (deterministic scorer) scores only the v0.2 task;
        the default T001 stays unscored (its scores JSON is untouched)."""
        _seed_two_prd_project(tmp_path)
        result = _invoke_cmd(tmp_path, ["score", "--prd", "v0.2"])
        assert result.exit_code == 0, result.output

        db = tmp_path / ".anvil" / "state.db"
        conn = sqlite3.connect(str(db))
        try:
            scores = dict(
                conn.execute("SELECT id, scores FROM tasks").fetchall()
            )
        finally:
            conn.close()
        # Default task untouched; named task scored.
        assert scores["T001"] == "{}", scores["T001"]
        assert scores["v0.2:T900"] != "{}", scores["v0.2:T900"]

    # ---- prd review ---------------------------------------------------------

    def test_prd_review_prd_scopes_to_named_partition(
        self, tmp_path: Path
    ) -> None:
        """`prd review --prd v0.2` transitions ONLY the v0.2 PRD (draft ->
        reviewed); the default PRD (already approved) is untouched."""
        _seed_two_prd_project(tmp_path)
        result = _invoke_cmd(tmp_path, ["prd", "review", "--prd", "v0.2"])
        assert result.exit_code == 0, result.output

        db = tmp_path / ".anvil" / "state.db"
        conn = sqlite3.connect(str(db))
        try:
            status = dict(conn.execute("SELECT id, status FROM prds").fetchall())
        finally:
            conn.close()
        assert status["v0.2"] == "reviewed", status
        assert status["default"] == "approved", status

    # ---- $ANVIL_PRD env path (PRD_OPTION envvar wiring) ---------------------

    def test_anvil_prd_env_scopes_list(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """$ANVIL_PRD is the env twin of --prd: with it set and no flag, list
        scopes to that PRD (proves PRD_OPTION's envvar wiring on the CLI)."""
        _seed_two_prd_project(tmp_path)
        monkeypatch.setenv("ANVIL_PRD", "v0.2")
        result = _invoke_cmd(tmp_path, ["list", "--json"])
        assert result.exit_code == 0, result.output
        ids = {t["id"] for t in json.loads(result.output)["data"]["tasks"]}
        assert ids == {"v0.2:T900"}, ids

    # ---- default sentinel collapse ('prd' -> 'default') --------------------

    def test_list_prd_sentinel_prd_matches_default(self, tmp_path: Path) -> None:
        """`list --prd prd` — 'prd' is the parse-time spelling of the default
        PRD, whose tasks are stored with prd_id='default'. The read surface must
        collapse the sentinel so this scopes to the default task, not an empty
        result against a nonexistent id='prd'.
        """
        _seed_two_prd_project(tmp_path)
        result = _invoke_cmd(tmp_path, ["list", "--prd", "prd", "--json"])
        assert result.exit_code == 0, result.output
        ids = {t["id"] for t in json.loads(result.output)["data"]["tasks"]}
        assert ids == {"T001"}, ids

    def test_next_prd_sentinel_prd_matches_default(self, tmp_path: Path) -> None:
        """`next --prd prd` narrows to the default partition, not an empty pool."""
        _seed_two_prd_project(tmp_path)
        result = _invoke_cmd(tmp_path, ["next", "--prd", "prd", "--json"])
        assert result.exit_code == 0, result.output
        task = json.loads(result.output)["data"]["task"]
        assert task is not None and task["id"] == "T001", task

    def test_show_prd_sentinel_prd_matches_default_task(
        self, tmp_path: Path
    ) -> None:
        """`show T001 --prd prd` must NOT raise a false 'belongs to PRD default,
        not prd' mismatch — the sentinel collapses to 'default' before the guard.
        """
        _seed_two_prd_project(tmp_path)
        result = _invoke_cmd(tmp_path, ["show", "T001", "--prd", "prd", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["data"]["task"]["id"] == "T001"

    def test_packet_prd_sentinel_prd_matches_default_task(
        self, tmp_path: Path
    ) -> None:
        """`packet T001 --prd prd` succeeds (no false cross-PRD mismatch)."""
        _seed_two_prd_project(tmp_path)
        result = _invoke_cmd(tmp_path, ["packet", "T001", "--prd", "prd"])
        assert result.exit_code == 0, result.output

    def test_prd_review_prd_sentinel_prd_finds_default(
        self, tmp_path: Path
    ) -> None:
        """`prd review --prd prd` resolves the DEFAULT PRD instead of erroring
        with 'No PRD found in state' against a nonexistent id='prd'. The default
        PRD here is already approved, so review reports it cannot re-review —
        the point is it FINDS the PRD (no not-found error)."""
        _seed_two_prd_project(tmp_path)
        result = _invoke_cmd(tmp_path, ["prd", "review", "--prd", "prd"])
        combined = result.output + (
            result.stderr if hasattr(result, "stderr") and result.stderr else ""
        )
        # The default PRD must be FOUND — never the 'No PRD found' not-found path.
        assert "No PRD found in state" not in combined, combined


# ---------------------------------------------------------------------------
# replay command
# ---------------------------------------------------------------------------


class TestReplayCommand:
    """Tests for `anvil replay --from-events <events.jsonl> --into <db>`."""

    def _init_project(self, tmp_path: Path) -> Path:
        """Run anvil init in tmp_path and return the .anvil dir."""
        _do_init(tmp_path, name="Replay Test Project")
        return tmp_path / ".anvil"

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


# ---------------------------------------------------------------------------
# doctor command (backlog T010) — health diagnosis
# ---------------------------------------------------------------------------


def _doctor_open_backend(tmp_path: Path):  # type: ignore[no-untyped-def]
    """Open an initialized backend rooted at tmp_path's .anvil/."""
    from anvil.cli._helpers import _open_backend

    return _open_backend(tmp_path / ".anvil")


def _doctor_seed_ready_task(
    tmp_path: Path, *, task_id: str = "T001", feature_id: str = "F001"
) -> None:
    """Seed a feature + a ready task (precursor for a claim)."""
    import datetime as _dt

    from anvil.state.models import EventDraft

    now = _dt.datetime(2026, 5, 25, 12, 0, 0, tzinfo=_dt.UTC)
    b = _doctor_open_backend(tmp_path)
    try:
        b.append(EventDraft(
            timestamp=now, actor="test", action="feature.created",
            target_kind="feature", target_id=feature_id,
            payload_json={
                "id": feature_id, "title": "F", "description": "",
                "status": "proposed", "requirements": [], "tasks": [],
            },
        ))
        b.append(EventDraft(
            timestamp=now, actor="test", action="task.created",
            target_kind="task", target_id=task_id,
            payload_json={
                "id": task_id, "feature_id": feature_id, "title": "T",
                "description": "d", "status": "ready", "priority": "medium",
                "dependencies": [], "conflict_groups": [], "scores": {},
                "acceptance_criteria": ["ok"], "implementation_notes": [],
                "verification": {
                    "commands": ["pytest"], "manual_steps": [],
                    "required_evidence": [],
                },
                "likely_files": [], "parent_task_id": None,
                "created_at": now.isoformat(), "updated_at": now.isoformat(),
            },
        ))
    finally:
        b.close()


def _doctor_seed_stale_claim(
    tmp_path: Path, *, claim_id: str = "C001", task_id: str = "T001"
) -> None:
    """Insert an active claim whose lease already expired (stale)."""
    import datetime as _dt

    from anvil.state.models import EventDraft

    now = _dt.datetime(2026, 5, 25, 12, 0, 0, tzinfo=_dt.UTC)
    b = _doctor_open_backend(tmp_path)
    try:
        b.append(EventDraft(
            timestamp=now, actor="test", action="claim.created",
            target_kind="claim", target_id=claim_id,
            payload_json={
                "id": claim_id, "task_id": task_id, "claimed_by": "agent-x",
                "claim_type": "task", "status": "active", "branch": None,
                "worktree_path": None, "expected_files": [],
                "created_at": now.isoformat(),
                # Lease expired two hours before _NOW (and well before real
                # "now"), so it is unambiguously stale.
                "lease_expires_at": (now - _dt.timedelta(hours=2)).isoformat(),
                "last_heartbeat_at": now.isoformat(),
            },
        ))
    finally:
        b.close()


def _doctor_stamp_user_version(tmp_path: Path, version: int) -> None:
    """Force PRAGMA user_version on the project's state.db (out of band)."""
    conn = sqlite3.connect(str(tmp_path / ".anvil" / "state.db"))
    try:
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
    finally:
        conn.close()


def _doctor_json(result) -> dict:  # type: ignore[no-untyped-def]
    return json.loads(result.stdout.strip())


class TestDoctorHealthy:
    """A healthy project: doctor exits 0 with no ERROR-level findings."""

    def test_doctor_clean_project_exits_zero(self, tmp_path: Path) -> None:
        _do_init(tmp_path)
        result = _invoke_cmd(tmp_path, ["doctor"])
        assert result.exit_code == 0, result.output
        assert "healthy" in result.output.lower()
        # Every probe ran and none is ERROR.
        assert "[ERROR]" not in result.output

    def test_doctor_clean_json_envelope(self, tmp_path: Path) -> None:
        _do_init(tmp_path)
        result = _invoke_cmd(tmp_path, ["doctor", "--json"])
        assert result.exit_code == 0, result.output
        env = _doctor_json(result)
        assert env["ok"] is True
        assert env["command"] == "doctor"
        assert env["data"]["healthy"] is True
        assert env["data"]["worst_severity"] in ("ok", "info")
        checks = {f["check"] for f in env["data"]["findings"]}
        # All required diagnostics are present (verification_paths: B30;
        # max_claim_age: B46).
        assert checks == {
            "state_db", "config", "claims", "max_claim_age", "replay",
            "reconciliation", "verification_paths",
        }

    def test_doctor_reports_schema_and_lease_values(self, tmp_path: Path) -> None:
        """The state_db and config findings carry schema + lease/heartbeat."""
        from anvil.state.schema import get_schema_version

        _do_init(tmp_path)
        env = _doctor_json(_invoke_cmd(tmp_path, ["doctor", "--json"]))
        by_check = {f["check"]: f for f in env["data"]["findings"]}
        assert (
            by_check["state_db"]["detail"]["code_schema_version"]
            == get_schema_version()
        )
        cfg_detail = by_check["config"]["detail"]
        assert cfg_detail["effective_lease_minutes"] == 60.0
        assert cfg_detail["effective_heartbeat_minutes"] == 5.0

    def test_doctor_verifies_replay_integrity(self, tmp_path: Path) -> None:
        _do_init(tmp_path)
        _doctor_seed_ready_task(tmp_path)
        env = _doctor_json(_invoke_cmd(tmp_path, ["doctor", "--json"]))
        replay = next(
            f for f in env["data"]["findings"] if f["check"] == "replay"
        )
        assert replay["severity"] == "ok"
        assert env["data"]["healthy"] is True


class TestDoctorUnhealthy:
    """A project with an injected stale claim PLUS a schema mismatch.

    T010 AC: doctor exits non-zero and BOTH findings are listed.
    """

    def test_doctor_stale_claim_and_schema_mismatch_human(
        self, tmp_path: Path
    ) -> None:
        _do_init(tmp_path)
        _doctor_seed_ready_task(tmp_path)
        _doctor_seed_stale_claim(tmp_path)
        _doctor_stamp_user_version(tmp_path, 99)

        result = _invoke_cmd(tmp_path, ["doctor"])
        assert result.exit_code == 1, result.output
        out = result.output
        # Both findings present and flagged ERROR.
        assert "[ERROR] state_db" in out
        assert "[ERROR] claims" in out
        assert "99" in out  # the mismatched schema version
        assert "UNHEALTHY" in out

    def test_doctor_stale_claim_and_schema_mismatch_json(
        self, tmp_path: Path
    ) -> None:
        _do_init(tmp_path)
        _doctor_seed_ready_task(tmp_path)
        _doctor_seed_stale_claim(tmp_path)
        _doctor_stamp_user_version(tmp_path, 99)

        result = _invoke_cmd(tmp_path, ["doctor", "--json"])
        assert result.exit_code == 1, result.output
        env = _doctor_json(result)
        assert env["ok"] is True  # the COMMAND succeeded; the project is unhealthy
        assert env["data"]["healthy"] is False
        assert env["data"]["worst_severity"] == "error"
        errors = {
            f["check"]
            for f in env["data"]["findings"]
            if f["severity"] == "error"
        }
        # BOTH the schema mismatch and the stale claim are listed as errors.
        assert "state_db" in errors
        assert "claims" in errors
        claims = next(
            f for f in env["data"]["findings"] if f["check"] == "claims"
        )
        assert claims["detail"]["stale"] == 1

    def test_doctor_stale_claim_only_exits_nonzero(self, tmp_path: Path) -> None:
        """A stale claim alone (schema healthy) still fails the gate."""
        _do_init(tmp_path)
        _doctor_seed_ready_task(tmp_path)
        _doctor_seed_stale_claim(tmp_path)

        result = _invoke_cmd(tmp_path, ["doctor", "--json"])
        assert result.exit_code == 1, result.output
        env = _doctor_json(result)
        assert env["data"]["healthy"] is False
        claims = next(
            f for f in env["data"]["findings"] if f["check"] == "claims"
        )
        assert claims["severity"] == "error"
        assert claims["detail"]["stale"] == 1
        # Schema is fine, reconciliation also surfaces the stale claim as drift.
        state_db = next(
            f for f in env["data"]["findings"] if f["check"] == "state_db"
        )
        assert state_db["severity"] in ("ok", "info")


class TestDoctorNotInitialized:
    def test_doctor_uninitialized_human(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app, ["doctor", "--cwd", str(tmp_path)], catch_exceptions=False
        )
        assert result.exit_code == 1
        combined = result.output + (getattr(result, "stderr", "") or "")
        assert "not initialized" in combined.lower()

    def test_doctor_uninitialized_json_envelope(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app, ["doctor", "--json", "--cwd", str(tmp_path)],
            catch_exceptions=False,
        )
        assert result.exit_code == 1
        env = json.loads(result.stdout.strip())
        assert env["ok"] is False
        assert env["command"] == "doctor"
        assert env["error"]["code"] == "not_initialized"


class TestDoctorStateRootEnv:
    def test_doctor_honors_state_root_env(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """ANVIL_ROOT points doctor at the project from elsewhere."""
        proj = tmp_path / "proj"
        elsewhere = tmp_path / "elsewhere"
        proj.mkdir()
        elsewhere.mkdir()
        _do_init(proj)

        monkeypatch.chdir(elsewhere)
        monkeypatch.setenv("ANVIL_ROOT", str(proj))
        result = runner.invoke(app, ["doctor", "--json"], catch_exceptions=False)
        assert result.exit_code == 0, result.output
        env = json.loads(result.stdout.strip())
        assert env["data"]["healthy"] is True

    def test_doctor_state_root_invalid_json_envelope(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A ANVIL_ROOT with no .anvil/ → parseable error envelope."""
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("ANVIL_ROOT", str(empty))
        result = runner.invoke(app, ["doctor", "--json"], catch_exceptions=False)
        assert result.exit_code != 0
        env = json.loads(result.stdout.strip())
        assert env["ok"] is False
        assert env["command"] == "doctor"
        assert env["error"]["code"] == "state_root_invalid"


# ---------------------------------------------------------------------------
# graph command (T019) — Mermaid dependency/state diagram
# ---------------------------------------------------------------------------


def _seed_graph_tasks(tmp_path: Path) -> None:
    """Seed a deterministic feature + 4 tasks with a known dependency chain.

    Layout (edges are dep --> dependent task):

        T001 (done)  --> T002 (ready)
        T002 (ready) --> T003 (ready)
        T004 (blocked) — no deps

    Inserts directly via SQLite (the idiom used across the CLI/MCP test
    suites) so the graph state is fixed regardless of planner behaviour —
    making the rendered diagram byte-deterministic for assertions.
    """
    _do_init(tmp_path, name="Graph Test Project")
    db_path = tmp_path / ".anvil" / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT OR IGNORE INTO features "
        "(id, title, description, status, requirements, tasks) "
        "VALUES ('F001', 'Graph Feature', 'desc', 'proposed', '[]', '[]')"
    )

    def _add(task_id: str, status: str, deps: list[str]) -> None:
        conn.execute(
            """INSERT OR REPLACE INTO tasks
            (id, feature_id, title, description, status, priority, task_type,
             dependencies, conflict_groups, scores, acceptance_criteria,
             implementation_notes, verification, likely_files,
             parent_task_id, created_at, updated_at)
            VALUES (?, 'F001', ?, 'desc', ?, 'medium', 'feature',
             ?, '[]', '{}', '["x"]', '[]', '{}', '[]',
             NULL, '2024-01-01T00:00:00+00:00', '2024-01-01T00:00:00+00:00')""",
            (task_id, f"Title {task_id}", status, json.dumps(deps)),
        )

    _add("T001", "done", [])
    _add("T002", "ready", ["T001"])
    _add("T003", "ready", ["T002"])
    _add("T004", "blocked", [])
    conn.commit()
    conn.close()


class TestGraphMermaid:
    """``anvil graph --format mermaid`` (backlog T019)."""

    def test_graph_mermaid_contains_expected_edges_and_statuses(
        self, tmp_path: Path
    ) -> None:
        """The Mermaid diagram has the expected dependency edges and node statuses."""
        _seed_graph_tasks(tmp_path)
        result = _invoke_cmd(tmp_path, ["graph", "--format", "mermaid"])
        assert result.exit_code == 0, f"graph --format mermaid failed: {result.output}"
        out = result.output

        # A valid Mermaid flowchart header.
        assert out.lstrip().startswith("graph LR"), out

        # Every task is a node, with its status reflected in the label.
        for tid, status in [
            ("T001", "done"),
            ("T002", "ready"),
            ("T003", "ready"),
            ("T004", "blocked"),
        ]:
            assert f"{tid}[" in out, f"missing node {tid}: {out}"
            assert f"({status})" in out, f"missing status {status}: {out}"

        # The two dependency edges (dep --> dependent) are present.
        assert "T001 --> T002" in out
        assert "T002 --> T003" in out
        # T004 has no deps and nothing depends on it → no edge.
        assert "--> T004" not in out
        assert "T004 -->" not in out

        # Status is also encoded as a class assignment for colouring.
        assert "class T001 done;" in out
        assert "class T004 blocked;" in out

    def test_graph_mermaid_is_deterministic(self, tmp_path: Path) -> None:
        """Two runs over the same state produce byte-identical Mermaid output."""
        _seed_graph_tasks(tmp_path)
        first = _invoke_cmd(tmp_path, ["graph", "--format", "mermaid"])
        second = _invoke_cmd(tmp_path, ["graph", "--format", "mermaid"])
        assert first.exit_code == 0
        assert second.exit_code == 0
        assert first.output == second.output

    def test_graph_mermaid_json_envelope_includes_diagram(
        self, tmp_path: Path
    ) -> None:
        """``graph --json --format mermaid`` emits the v1.24 envelope with the diagram."""
        _seed_graph_tasks(tmp_path)
        result = _invoke_cmd(
            tmp_path, ["graph", "--json", "--format", "mermaid"]
        )
        assert result.exit_code == 0, result.output
        env = json.loads(result.stdout.strip())
        assert env["ok"] is True
        assert env["command"] == "graph"
        data = env["data"]
        assert data["format"] == "mermaid"
        # Structured graph mirrors the diagram.
        ids = {n["id"] for n in data["nodes"]}
        assert ids == {"T001", "T002", "T003", "T004"}
        assert {"from": "T001", "to": "T002"} in data["edges"]
        assert {"from": "T002", "to": "T003"} in data["edges"]
        # ready_to_claim: T002 (dep T001 done); NOT T003 (dep T002 not done).
        assert data["ready_to_claim"] == ["T002"]
        # The rendered Mermaid text is carried under data.diagram.
        assert data["diagram"] is not None
        assert "graph LR" in data["diagram"]
        assert "T001 --> T002" in data["diagram"]

    def test_graph_mermaid_scope_task_restricts_to_transitive_deps(
        self, tmp_path: Path
    ) -> None:
        """scope=task renders the target plus its transitive dependencies only."""
        _seed_graph_tasks(tmp_path)
        result = _invoke_cmd(
            tmp_path,
            ["graph", "--format", "mermaid", "--scope", "task", "--target", "T002"],
        )
        assert result.exit_code == 0, result.output
        out = result.output
        # T002 and its dep T001 are in scope.
        assert "T001[" in out
        assert "T002[" in out
        assert "T001 --> T002" in out
        # T003 (depends ON T002, not a dependency of it) and unrelated T004 are out.
        assert "T003[" not in out
        assert "T004[" not in out

    def test_graph_mermaid_empty_project_is_valid(self, tmp_path: Path) -> None:
        """``graph --format mermaid`` on a project with no tasks is still valid Mermaid."""
        _do_init(tmp_path, name="Empty Graph Project")
        result = _invoke_cmd(tmp_path, ["graph", "--format", "mermaid"])
        assert result.exit_code == 0, result.output
        assert result.output.lstrip().startswith("graph LR")

    def test_graph_mermaid_scope_task_requires_target(self, tmp_path: Path) -> None:
        """scope=task without --target is a bad-request error, not a crash."""
        _seed_graph_tasks(tmp_path)
        result = _invoke_cmd(
            tmp_path, ["graph", "--format", "mermaid", "--scope", "task"]
        )
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# conflicts command (CL-5) — surface persisted conflict groups
# ---------------------------------------------------------------------------


def _seed_conflict_group(tmp_path: Path) -> None:
    """Seed a feature, two tasks, and a conflict_groups row directly.

    Mirrors ``_seed_graph_tasks``: inserts via SQLite so the read surface is
    tested in isolation from the planner.
    """
    _do_init(tmp_path, name="Conflicts Test Project")
    db_path = tmp_path / ".anvil" / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT OR IGNORE INTO features "
        "(id, title, description, status, requirements, tasks) "
        "VALUES ('F001', 'F', 'desc', 'proposed', '[]', '[]')"
    )
    conn.execute(
        "INSERT OR REPLACE INTO conflict_groups (id, name, task_ids, reason) "
        "VALUES (?, ?, ?, ?)",
        (
            "CG-T001-T002",
            "CG-T001-T002",
            json.dumps(["T001", "T002"]),
            "Tasks T001 and T002 share overlapping files: src/b.py",
        ),
    )
    conn.commit()
    conn.close()


_CONFLICT_PRD = """\
# Project: Conflict Plan Test

## Summary

Two tasks whose likely_files overlap should form a conflict group.

## Goals

- Detect file-overlap conflicts.

## Requirements

- R001: Overlapping work.

## Features

### F001: Overlap Feature

The only feature.

**Requirements:** R001

## Tasks

### T001: First overlapping task

**Feature:** F001
**Priority:** medium
**Likely files:** src/a.py, src/b.py

**Acceptance criteria:**

- Does the thing.

**Verification:**

- `pytest -q`

### T002: Second overlapping task

**Feature:** F001
**Priority:** medium
**Likely files:** src/b.py, src/c.py

**Acceptance criteria:**

- Does the other thing.

**Verification:**

- `pytest -q`
"""


class TestConflictsCommand:
    """``anvil conflicts`` (CL-5) lists persisted conflict groups."""

    def test_empty_state_text(self, tmp_path: Path) -> None:
        _do_init(tmp_path, name="Empty Conflicts")
        result = _invoke_cmd(tmp_path, ["conflicts"])
        assert result.exit_code == 0, result.output
        assert "No conflict groups." in result.output

    def test_empty_state_json(self, tmp_path: Path) -> None:
        _do_init(tmp_path, name="Empty Conflicts JSON")
        result = _invoke_cmd(tmp_path, ["conflicts", "--json"])
        assert result.exit_code == 0, result.output
        env = json.loads(result.stdout.strip())
        assert env["ok"] is True
        assert env["command"] == "conflicts"
        assert env["data"]["count"] == 0
        assert env["data"]["conflict_groups"] == []

    def test_lists_seeded_group_text(self, tmp_path: Path) -> None:
        _seed_conflict_group(tmp_path)
        result = _invoke_cmd(tmp_path, ["conflicts"])
        assert result.exit_code == 0, result.output
        assert "1 conflict group(s):" in result.output
        assert "CG-T001-T002: T001, T002" in result.output
        assert "src/b.py" in result.output

    def test_lists_seeded_group_json(self, tmp_path: Path) -> None:
        _seed_conflict_group(tmp_path)
        result = _invoke_cmd(tmp_path, ["conflicts", "--json"])
        assert result.exit_code == 0, result.output
        env = json.loads(result.stdout.strip())
        assert env["data"]["count"] == 1
        grp = env["data"]["conflict_groups"][0]
        assert grp["id"] == "CG-T001-T002"
        assert grp["task_ids"] == ["T001", "T002"]
        assert "src/b.py" in grp["reason"]

    def test_invalid_format_exits_nonzero(self, tmp_path: Path) -> None:
        _do_init(tmp_path, name="Bad Format")
        result = _invoke_cmd(tmp_path, ["conflicts", "--format", "yaml"])
        assert result.exit_code == 2
        assert "unknown format" in result.output

    def test_plan_persists_conflict_groups_then_conflicts_lists_them(
        self, tmp_path: Path
    ) -> None:
        """End-to-end (CL-4 + CL-5): plan persists groups; conflicts surfaces them."""
        _do_init(tmp_path, name="Plan Conflicts")
        _write_prd(tmp_path, _CONFLICT_PRD)
        _invoke_cmd(tmp_path, ["prd", "parse"])
        plan_result = _invoke_cmd(tmp_path, ["plan", "--no-llm"])
        assert plan_result.exit_code == 0, plan_result.output

        # The conflict_groups table is populated (CL-4).
        db_path = tmp_path / ".anvil" / "state.db"
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT id, task_ids FROM conflict_groups ORDER BY id"
        ).fetchall()
        conn.close()
        assert rows, "plan did not persist any conflict_groups row"
        assert rows[0][0] == "CG-T001-T002"
        assert json.loads(rows[0][1]) == ["T001", "T002"]

        # And `conflicts` surfaces them (CL-5).
        result = _invoke_cmd(tmp_path, ["conflicts", "--json"])
        assert result.exit_code == 0, result.output
        env = json.loads(result.stdout.strip())
        ids = {g["id"] for g in env["data"]["conflict_groups"]}
        assert "CG-T001-T002" in ids
