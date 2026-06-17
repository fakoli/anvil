"""FAKOLI_STATE_ROOT env-override resolution tests (T005/B07).

Covers the project-root resolution precedence applied by BOTH the CLI
(``cli/_helpers._resolve_state_dir``) and the MCP server
(``mcp_server._resolve_state_dir``):

    explicit path arg/flag (--cwd)  >  FAKOLI_STATE_ROOT env  >  cwd / walk-up

and the fail-loud contract: FAKOLI_STATE_ROOT set to a dir with no
``.fakoli-state/`` must error (non-zero exit / ToolError), never silently fall
back to cwd.

Pattern mirrors ``tests/test_cli.py`` (Typer ``CliRunner`` + ``os.chdir`` into
a per-test ``tmp_path``) plus ``monkeypatch.setenv`` for the env var.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fakoli_state.cli import app

runner = CliRunner()


def _init_project(root: Path, name: str) -> None:
    """init a fresh project under *root* (run with cwd == root)."""
    original_cwd = os.getcwd()
    os.chdir(root)
    try:
        res = runner.invoke(app, ["init", "--name", name], catch_exceptions=False)
        assert res.exit_code == 0, res.output
    finally:
        os.chdir(original_cwd)


# ---------------------------------------------------------------------------
# Env points at a project while cwd is ELSEWHERE
# ---------------------------------------------------------------------------


class TestEnvPointsAtProject:
    def test_status_uses_env_project_when_cwd_elsewhere(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FAKOLI_STATE_ROOT -> project A; cwd -> empty dir B; status sees A."""
        project = tmp_path / "project"
        elsewhere = tmp_path / "elsewhere"
        project.mkdir()
        elsewhere.mkdir()
        _init_project(project, "Env Project")

        monkeypatch.setenv("FAKOLI_STATE_ROOT", str(project))
        monkeypatch.chdir(elsewhere)

        res = runner.invoke(app, ["status"], catch_exceptions=False)
        assert res.exit_code == 0, res.output
        assert "Env Project" in res.output

    def test_status_json_uses_env_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """status --json from an unrelated cwd reads the env-pointed project."""
        project = tmp_path / "project"
        elsewhere = tmp_path / "elsewhere"
        project.mkdir()
        elsewhere.mkdir()
        _init_project(project, "Env JSON Project")

        monkeypatch.setenv("FAKOLI_STATE_ROOT", str(project))
        monkeypatch.chdir(elsewhere)

        res = runner.invoke(app, ["status", "--json"], catch_exceptions=False)
        assert res.exit_code == 0, res.output
        env = json.loads(res.stdout.strip())
        assert env["ok"] is True
        assert env["data"]["project"]["name"] == "Env JSON Project"

    def test_list_uses_env_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A second command (list) also honors the env override (centralized)."""
        project = tmp_path / "project"
        elsewhere = tmp_path / "elsewhere"
        project.mkdir()
        elsewhere.mkdir()
        _init_project(project, "Env List Project")

        monkeypatch.setenv("FAKOLI_STATE_ROOT", str(project))
        monkeypatch.chdir(elsewhere)

        # list operates on the env-pointed project (no tasks yet -> exit 0).
        res = runner.invoke(app, ["list", "--json"], catch_exceptions=False)
        assert res.exit_code == 0, res.output
        env = json.loads(res.stdout.strip())
        assert env["ok"] is True
        assert env["command"] == "list"


# ---------------------------------------------------------------------------
# MUST-FIX 1: init honors the env override (no silent directory divergence)
# ---------------------------------------------------------------------------


class TestInitHonorsEnv:
    def test_init_then_status_use_same_env_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """init + status both operate on the env root, NOT the (empty) cwd.

        Regression for the silent-divergence bug: init used Path.cwd() directly
        while reads honored FAKOLI_STATE_ROOT, so init wrote .fakoli-state to
        cwd while status inspected the env dir -> two different directories.
        """
        env_root = tmp_path / "env_root"
        elsewhere = tmp_path / "elsewhere"
        env_root.mkdir()
        elsewhere.mkdir()

        monkeypatch.setenv("FAKOLI_STATE_ROOT", str(env_root))
        monkeypatch.chdir(elsewhere)

        # init (no --cwd) must create the project under the ENV root.
        res_init = runner.invoke(
            app, ["init", "--name", "Env Init Project"], catch_exceptions=False
        )
        assert res_init.exit_code == 0, res_init.output
        assert (env_root / ".fakoli-state").is_dir(), res_init.output
        # It must NOT have leaked a project into the cwd.
        assert not (elsewhere / ".fakoli-state").exists(), res_init.output

        # status (still from elsewhere) sees the SAME env project.
        res_status = runner.invoke(
            app, ["status", "--json"], catch_exceptions=False
        )
        assert res_status.exit_code == 0, res_status.output
        env = json.loads(res_status.stdout.strip())
        assert env["ok"] is True
        assert env["data"]["project"]["name"] == "Env Init Project"


# ---------------------------------------------------------------------------
# MUST-FIX 3: --hook-format stays exit-0-safe even on invalid env / no project
# ---------------------------------------------------------------------------


class TestHookFormatExitZero:
    def test_hook_format_invalid_env_from_non_project_dir_exits_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """status --hook-format must never error/block the agent.

        With an invalid FAKOLI_STATE_ROOT (a dir with no .fakoli-state) and a
        non-project cwd, resolution would normally raise StateRootError; in
        hook-format mode it must be swallowed -> benign output, exit 0.
        """
        bad_root = tmp_path / "no_project"
        non_project_cwd = tmp_path / "empty_cwd"
        bad_root.mkdir()
        non_project_cwd.mkdir()

        monkeypatch.setenv("FAKOLI_STATE_ROOT", str(bad_root))
        monkeypatch.chdir(non_project_cwd)

        res = runner.invoke(
            app, ["status", "--hook-format"], catch_exceptions=False
        )
        assert res.exit_code == 0, res.output
        assert res.output.strip() == "uninitialized"

    def test_hook_format_nonexistent_env_exits_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A FAKOLI_STATE_ROOT pointing at a missing path is also swallowed."""
        missing = tmp_path / "does_not_exist"
        monkeypatch.setenv("FAKOLI_STATE_ROOT", str(missing))
        monkeypatch.chdir(tmp_path)

        res = runner.invoke(
            app, ["status", "--hook-format"], catch_exceptions=False
        )
        assert res.exit_code == 0, res.output
        assert res.output.strip() == "uninitialized"


# ---------------------------------------------------------------------------
# Precedence: flag > env > cwd
# ---------------------------------------------------------------------------


class TestPrecedence:
    def test_explicit_flag_beats_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--cwd wins over FAKOLI_STATE_ROOT when both point at valid projects."""
        proj_flag = tmp_path / "flag"
        proj_env = tmp_path / "env"
        proj_flag.mkdir()
        proj_env.mkdir()
        _init_project(proj_flag, "Flag Project")
        _init_project(proj_env, "Env Project")

        monkeypatch.setenv("FAKOLI_STATE_ROOT", str(proj_env))
        monkeypatch.chdir(tmp_path)

        res = runner.invoke(
            app, ["status", "--cwd", str(proj_flag)], catch_exceptions=False
        )
        assert res.exit_code == 0, res.output
        assert "Flag Project" in res.output
        assert "Env Project" not in res.output

    def test_env_beats_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FAKOLI_STATE_ROOT wins over the current working directory.

        Both cwd and the env target are valid projects, so a win is
        observable by project name (not just by which one happens to exist).
        """
        proj_cwd = tmp_path / "cwd_proj"
        proj_env = tmp_path / "env_proj"
        proj_cwd.mkdir()
        proj_env.mkdir()
        _init_project(proj_cwd, "Cwd Project")
        _init_project(proj_env, "Env Wins Project")

        monkeypatch.setenv("FAKOLI_STATE_ROOT", str(proj_env))
        monkeypatch.chdir(proj_cwd)

        res = runner.invoke(app, ["status"], catch_exceptions=False)
        assert res.exit_code == 0, res.output
        assert "Env Wins Project" in res.output
        assert "Cwd Project" not in res.output


# ---------------------------------------------------------------------------
# Fail-loud: env set to a dir with no .fakoli-state/
# ---------------------------------------------------------------------------


class TestInvalidEnv:
    def test_env_without_state_dir_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FAKOLI_STATE_ROOT -> dir with no .fakoli-state -> non-zero, clear error.

        Crucially it must NOT silently fall back to a valid project in cwd.
        """
        bad_root = tmp_path / "no_project"
        cwd_proj = tmp_path / "cwd_proj"
        bad_root.mkdir()
        cwd_proj.mkdir()
        # A valid project in cwd that must NOT be used as a fallback.
        _init_project(cwd_proj, "Should Not Be Used")

        monkeypatch.setenv("FAKOLI_STATE_ROOT", str(bad_root))
        monkeypatch.chdir(cwd_proj)

        res = runner.invoke(app, ["status"])
        assert res.exit_code != 0, res.output
        combined = res.output + (getattr(res, "stderr", "") or "")
        assert "FAKOLI_STATE_ROOT" in combined
        assert ".fakoli-state" in combined
        # Did NOT fall back to the cwd project.
        assert "Should Not Be Used" not in combined

    def test_env_pointing_at_nonexistent_path_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A FAKOLI_STATE_ROOT that does not even exist errors clearly."""
        missing = tmp_path / "does_not_exist"
        monkeypatch.setenv("FAKOLI_STATE_ROOT", str(missing))
        monkeypatch.chdir(tmp_path)

        res = runner.invoke(app, ["status"])
        assert res.exit_code != 0, res.output


# ---------------------------------------------------------------------------
# Default (no env) behaviour is UNCHANGED
# ---------------------------------------------------------------------------


class TestDefaultUnchanged:
    def test_no_env_uses_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With FAKOLI_STATE_ROOT unset, status resolves from cwd as before."""
        monkeypatch.delenv("FAKOLI_STATE_ROOT", raising=False)
        _init_project(tmp_path, "Default Project")
        monkeypatch.chdir(tmp_path)

        res = runner.invoke(app, ["status"], catch_exceptions=False)
        assert res.exit_code == 0, res.output
        assert "Default Project" in res.output

    def test_empty_env_is_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty FAKOLI_STATE_ROOT is treated as unset (falls through to cwd)."""
        _init_project(tmp_path, "Empty Env Project")
        monkeypatch.setenv("FAKOLI_STATE_ROOT", "")
        monkeypatch.chdir(tmp_path)

        res = runner.invoke(app, ["status"], catch_exceptions=False)
        assert res.exit_code == 0, res.output
        assert "Empty Env Project" in res.output


# ---------------------------------------------------------------------------
# MCP server resolution applies the same precedence
# ---------------------------------------------------------------------------


class TestMcpResolution:
    def test_mcp_resolve_uses_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MCP _resolve_state_dir(None) honors FAKOLI_STATE_ROOT."""
        from fakoli_state.mcp_server import _resolve_state_dir as mcp_resolve

        project = tmp_path / "project"
        project.mkdir()
        _init_project(project, "MCP Env Project")

        monkeypatch.setenv("FAKOLI_STATE_ROOT", str(project))
        monkeypatch.chdir(tmp_path)

        resolved = mcp_resolve(None)
        assert resolved == (project / ".fakoli-state").resolve()

    def test_mcp_explicit_cwd_beats_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MCP _resolve_state_dir(cwd) wins over the env var."""
        from fakoli_state.mcp_server import _resolve_state_dir as mcp_resolve

        proj_flag = tmp_path / "flag"
        proj_env = tmp_path / "env"
        proj_flag.mkdir()
        proj_env.mkdir()
        _init_project(proj_flag, "MCP Flag Project")
        _init_project(proj_env, "MCP Env Project")

        monkeypatch.setenv("FAKOLI_STATE_ROOT", str(proj_env))

        resolved = mcp_resolve(str(proj_flag))
        assert resolved == (proj_flag / ".fakoli-state").resolve()

    def test_mcp_invalid_env_raises_tool_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MCP translates an invalid FAKOLI_STATE_ROOT into a ToolError."""
        from fastmcp.exceptions import ToolError

        from fakoli_state.mcp_server import _resolve_state_dir as mcp_resolve

        bad_root = tmp_path / "no_project"
        bad_root.mkdir()
        monkeypatch.setenv("FAKOLI_STATE_ROOT", str(bad_root))

        with pytest.raises(ToolError) as excinfo:
            mcp_resolve(None)
        assert "FAKOLI_STATE_ROOT" in str(excinfo.value)
