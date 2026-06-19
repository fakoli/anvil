"""Tests for ``anvil mcp-config <client>`` — paste-ready MCP server config.

The command is read-only and project-free (mirrors ``describe``): it prints the
config block for a target client with the ``anvil`` server pointed at this
checkout's ``bin/anvil-mcp`` by ABSOLUTE path (never ``${CLAUDE_PLUGIN_ROOT}``).
These tests drive it through Typer's ``CliRunner`` (as ``test_cli.py`` does) and
assert each client's envelope divergence, the path resolution, the flags, and
the JSON envelope.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from anvil.cli import app
from anvil.cli.mcp_config import CLIENTS

# Default CliRunner keeps stdout/stderr separate (click 8.x), so we can assert
# stdout is paste-clean while the `# paste into …` hint goes to stderr.
runner = CliRunner()


# Top-level key each client's JSON envelope must carry.
_TOP_KEY = {
    "claude-code": "mcpServers",
    "cursor": "mcpServers",
    "windsurf": "mcpServers",
    "cline": "mcpServers",
    "vscode": "servers",
    "zed": "context_servers",
    "opencode": "mcp",
}


@pytest.mark.parametrize("client", sorted(CLIENTS))
def test_each_client_emits_expected_top_key(client: str) -> None:
    """Every client prints the right envelope shape on stdout."""
    result = runner.invoke(app, ["mcp-config", client], catch_exceptions=False)
    assert result.exit_code == 0, result.stdout
    if client == "codex":
        # TOML: a table header line, not JSON.
        assert "[mcp_servers.anvil]" in result.stdout
    else:
        data = json.loads(result.stdout)
        top = _TOP_KEY[client]
        assert top in data
        assert "anvil" in data[top]


def test_server_points_at_real_wrapper() -> None:
    """The server `args` path ends with bin/anvil-mcp, is absolute, and exists.

    Also guarantees no ${CLAUDE_PLUGIN_ROOT} token leaks into the output.
    """
    from pathlib import Path

    result = runner.invoke(app, ["mcp-config", "cursor"], catch_exceptions=False)
    assert result.exit_code == 0, result.stdout
    assert "CLAUDE_PLUGIN_ROOT" not in result.stdout

    spec = json.loads(result.stdout)["mcpServers"]["anvil"]
    wrapper = spec["args"][-1]
    assert wrapper.endswith("bin/anvil-mcp")
    p = Path(wrapper)
    assert p.is_absolute()
    assert p.is_file(), f"wrapper not found on disk: {wrapper}"


def test_uv_run_flag() -> None:
    """`--uv-run` emits the explicit uv invocation at an existing bin/ dir."""
    from pathlib import Path

    result = runner.invoke(
        app, ["mcp-config", "--uv-run", "cursor"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.stdout
    spec = json.loads(result.stdout)["mcpServers"]["anvil"]
    assert spec["command"] == "uv"
    assert spec["args"][0] == "run"
    # contains `python -m anvil.mcp_server`
    assert "python" in spec["args"]
    assert "anvil.mcp_server" in spec["args"]
    # --project points at an existing bin/ dir
    proj_idx = spec["args"].index("--project")
    proj = spec["args"][proj_idx + 1]
    assert Path(proj).is_dir()
    assert Path(proj).name == "bin"


def test_root_flag_injects_env() -> None:
    """`--root /x` puts env.ANVIL_ROOT; without it there is no env key."""
    with_root = runner.invoke(
        app, ["mcp-config", "--root", "/x", "cursor"], catch_exceptions=False
    )
    assert with_root.exit_code == 0, with_root.stdout
    spec = json.loads(with_root.stdout)["mcpServers"]["anvil"]
    assert spec["env"]["ANVIL_ROOT"] == "/x"

    without = runner.invoke(app, ["mcp-config", "cursor"], catch_exceptions=False)
    spec2 = json.loads(without.stdout)["mcpServers"]["anvil"]
    assert "env" not in spec2


def test_vscode_has_stdio_type() -> None:
    result = runner.invoke(app, ["mcp-config", "vscode"], catch_exceptions=False)
    spec = json.loads(result.stdout)["servers"]["anvil"]
    assert spec["type"] == "stdio"


def test_zed_top_key() -> None:
    result = runner.invoke(app, ["mcp-config", "zed"], catch_exceptions=False)
    data = json.loads(result.stdout)
    assert "context_servers" in data
    assert data["context_servers"]["anvil"]["source"] == "custom"


def test_opencode_shape() -> None:
    """opencode uses a unique entry: argv-array command, type local, enabled."""
    from pathlib import Path

    result = runner.invoke(app, ["mcp-config", "opencode"], catch_exceptions=False)
    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)
    assert data["$schema"] == "https://opencode.ai/config.json"
    spec = data["mcp"]["anvil"]
    assert spec["type"] == "local"
    assert spec["enabled"] is True
    # command is a single argv array, not a command/args split.
    assert isinstance(spec["command"], list)
    assert "args" not in spec
    wrapper = spec["command"][-1]
    assert wrapper.endswith("bin/anvil-mcp") and Path(wrapper).is_absolute()


def test_opencode_root_uses_environment_key() -> None:
    """`--root` injects ANVIL_ROOT under `environment` (not `env`) for opencode."""
    result = runner.invoke(
        app, ["mcp-config", "--root", "/x", "opencode"], catch_exceptions=False
    )
    spec = json.loads(result.stdout)["mcp"]["anvil"]
    assert spec["environment"]["ANVIL_ROOT"] == "/x"
    assert "env" not in spec


def test_codex_is_toml() -> None:
    """codex output is TOML (not JSON) with the [mcp_servers.anvil] table."""
    result = runner.invoke(app, ["mcp-config", "codex"], catch_exceptions=False)
    assert result.exit_code == 0, result.stdout
    out = result.stdout
    assert "[mcp_servers.anvil]" in out
    assert "command = " in out
    assert "args = " in out
    # Not JSON.
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_root_flag_injects_env_in_toml() -> None:
    result = runner.invoke(
        app, ["mcp-config", "--root", "/x", "codex"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.stdout
    out = result.stdout
    assert "[mcp_servers.anvil.env]" in out
    assert 'ANVIL_ROOT = "/x"' in out


def test_json_envelope() -> None:
    """`--json cursor` emits a single parseable success envelope; config_text
    re-parses to valid JSON."""
    result = runner.invoke(
        app, ["mcp-config", "--json", "cursor"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.stdout
    env = json.loads(result.stdout.strip())
    assert env["ok"] is True
    assert env["command"] == "mcp-config"
    data = env["data"]
    assert set(data) == {"client", "target_file", "format", "config_text"}
    assert data["client"] == "cursor"
    assert data["format"] == "json"
    # config_text re-parses to valid JSON.
    inner = json.loads(data["config_text"])
    assert "mcpServers" in inner
    # Nothing leaked to stderr under --json.
    assert result.stderr == ""


def test_unknown_client_fails() -> None:
    """Bad client exits 2; under --json emits error.code == bad_request."""
    result = runner.invoke(app, ["mcp-config", "nope"], catch_exceptions=False)
    assert result.exit_code == 2

    j = runner.invoke(app, ["mcp-config", "--json", "nope"], catch_exceptions=False)
    assert j.exit_code == 2
    env = json.loads(j.stdout.strip())
    assert env["ok"] is False
    assert env["error"]["code"] == "bad_request"


def test_stdout_is_paste_clean() -> None:
    """In text mode stdout is ONLY the config; the hint goes to stderr."""
    result = runner.invoke(app, ["mcp-config", "cursor"], catch_exceptions=False)
    assert result.exit_code == 0, result.stdout
    # stdout parses directly with no leading comment line.
    assert "# paste into" not in result.stdout
    json.loads(result.stdout)  # would raise if a comment leaked in
    # The hint is on stderr.
    assert "# paste into" in result.stderr
