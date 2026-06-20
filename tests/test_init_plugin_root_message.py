"""B29 / T009 — actionable `anvil init` message at the plugin root.

The guard still refuses, but the message now names a concrete project dir (a
detected sibling `pyproject.toml`) and the ANVIL_ROOT override.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from typer.testing import CliRunner

from anvil.cli import app
from anvil.cli.init_status import _suggest_project_dir

runner = CliRunner()


def _make_plugin_root(tmp_path: Path, *, with_pkg: bool) -> Path:
    manifest = tmp_path / ".claude-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"name": "anvil"}), encoding="utf-8")
    if with_pkg:
        pkg = tmp_path / "bin"
        pkg.mkdir()
        (pkg / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    return tmp_path


def test_suggest_project_dir_finds_pyproject(tmp_path: Path):
    _make_plugin_root(tmp_path, with_pkg=True)
    assert _suggest_project_dir(tmp_path) == "bin"


def test_suggest_project_dir_none_when_absent(tmp_path: Path):
    _make_plugin_root(tmp_path, with_pkg=False)
    assert _suggest_project_dir(tmp_path) is None


def test_init_at_plugin_root_refuses_with_suggestion(tmp_path: Path, monkeypatch):  # type: ignore[no-untyped-def]
    # ANVIL_ROOT takes precedence over cwd in _resolve_base_dir; unset it so the
    # guard actually resolves to tmp_path and fires (else a dev with ANVIL_ROOT
    # set gets a false pass — Greptile P1).
    monkeypatch.delenv("ANVIL_ROOT", raising=False)
    _make_plugin_root(tmp_path, with_pkg=True)
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = runner.invoke(app, ["init", "--name", "X"])
        assert result.exit_code == 1
        out = result.output
        assert "plugin root" in out
        assert "cd 'bin' && anvil init" in out  # concrete, quoted suggestion
        assert "ANVIL_ROOT" in out  # override hint
    finally:
        os.chdir(cwd)


def test_init_at_plugin_root_without_pkg_still_refuses(tmp_path: Path, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.delenv("ANVIL_ROOT", raising=False)
    _make_plugin_root(tmp_path, with_pkg=False)
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = runner.invoke(app, ["init", "--name", "X"])
        assert result.exit_code == 1
        assert "ANVIL_ROOT" in result.output  # still gives the override hint
    finally:
        os.chdir(cwd)


def test_init_at_plugin_root_allowed_in_workspace_layout(tmp_path: Path, monkeypatch):  # type: ignore[no-untyped-def]
    """B44: under the default workspace layout the plugin-root guard does NOT fire —
    state goes to ~/.anvil/... (never into the repo), so init at the plugin root is
    harmless and correct for anvil-on-anvil dogfooding."""
    monkeypatch.delenv("ANVIL_ROOT", raising=False)
    monkeypatch.setenv("ANVIL_STATE_LAYOUT", "workspace")  # override the autouse 'local'
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    _make_plugin_root(tmp_path, with_pkg=True)
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = runner.invoke(app, ["init", "--name", "X"], catch_exceptions=False)
        assert result.exit_code == 0, result.output  # NOT refused
        assert "plugin root" not in result.output
    finally:
        os.chdir(cwd)
