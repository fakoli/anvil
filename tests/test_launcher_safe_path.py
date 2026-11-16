"""Launchers must not import Python modules from the caller's working directory.

Plugin hooks, the plugin MCP manifest and the checkout wrappers start anvil as
``python -m anvil...`` while the working directory is the *user's project*.
Without safe-path mode (``-P``) CPython puts that directory first on
``sys.path``, so a ``typer.py`` or ``anvil/`` package inside a repository the
operator merely opened would run in place of anvil.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from anvil.cli.mcp_config import _uv_run_spec

REPO_ROOT = Path(__file__).resolve().parent.parent


def _assert_safe_module_argv(argv: list[str]) -> None:
    index = argv.index("-m")
    assert argv[index - 1] == "-P", argv
    assert argv[index - 2] == "python", argv


def test_hook_manifest_uses_safe_path_mode() -> None:
    data = json.loads((REPO_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    commands = [
        hook["command"]
        for specs in data["hooks"].values()
        for spec in specs
        for hook in spec["hooks"]
    ]
    assert commands
    for command in commands:
        assert " python -P -m anvil.cli " in command, command


@pytest.mark.parametrize("manifest", [".mcp.json", "packaging/codex/.mcp.json"])
def test_mcp_manifests_use_safe_path_mode(manifest: str) -> None:
    data = json.loads((REPO_ROOT / manifest).read_text(encoding="utf-8"))
    _assert_safe_module_argv(data["mcpServers"]["anvil"]["args"])


def test_generated_uv_run_spec_uses_safe_path_mode(tmp_path: Path) -> None:
    _assert_safe_module_argv(_uv_run_spec(tmp_path)["args"])


@pytest.mark.parametrize(
    ("wrapper", "module"),
    [("bin/anvil", "anvil.cli"), ("bin/anvil-mcp", "anvil.mcp_server")],
)
def test_checkout_wrappers_use_safe_path_mode(wrapper: str, module: str) -> None:
    text = (REPO_ROOT / wrapper).read_text(encoding="utf-8")
    assert f"python -P -m {module} " in text
    assert f"python -m {module}" not in text


def test_docker_entrypoint_uses_safe_path_mode() -> None:
    text = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert 'ENTRYPOINT ["python", "-P", "-m", "anvil.mcp_server"]' in text


@pytest.mark.parametrize("module", ["anvil.cli", "anvil.mcp_server"])
def test_safe_path_launch_ignores_modules_planted_in_cwd(
    tmp_path: Path, module: str
) -> None:
    marker = tmp_path / "PLANTED_MODULE_RAN"
    planted = f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n"
    (tmp_path / "typer.py").write_text(planted, encoding="utf-8")
    package = tmp_path / "anvil"
    package.mkdir()
    (package / "__init__.py").write_text(planted, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-P", "-m", module, "--version"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr[-800:]
    assert not marker.exists()
