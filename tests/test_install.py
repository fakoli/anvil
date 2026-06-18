"""Tests for ``anvil install <harness> [--write]`` — the MCP+instruction writer.

``install`` is a thin writer layered on ``mcp-config``: it reuses the same
``CLIENTS`` envelope (never re-encodes it) and drops the canonical ``AGENTS.md``
bytes where each harness reads them. Default is a safe dry-run; ``--write`` does
idempotent merges (JSON/TOML) + an instruction-file overwrite.

These drive the command through Typer's ``CliRunner`` (as ``test_mcp_config.py``
does), with ``HOME`` monkeypatched and the project root pinned via ``ANVIL_ROOT``
so writes land under ``tmp_path`` and never touch the real machine.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from anvil.cli import app
from anvil.cli.install import HARNESSES

runner = CliRunner()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Isolate HOME and the project root under tmp_path.

    - HOME → ``tmp_path/home`` (home-scoped writes land here).
    - ANVIL_ROOT → ``tmp_path/project`` (project-scoped writes land here).
    """
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ANVIL_ROOT", str(project))
    return {"home": home, "project": project}


def test_known_harnesses_present() -> None:
    """The verified harnesses from the spec are all in the registry."""
    for name in ("codex", "copilot", "gemini", "openclaw", "cursor", "windsurf",
                 "cline", "zed", "openhands"):
        assert name in HARNESSES


@pytest.mark.parametrize("harness", sorted(HARNESSES))
def test_dry_run_writes_nothing(harness: str, sandbox: dict[str, Path]) -> None:
    """No ``--write`` → dry-run: exit 0, NOTHING written to disk, paths printed."""
    result = runner.invoke(app, ["install", harness], catch_exceptions=False)
    assert result.exit_code == 0, result.stdout + result.stderr

    # Nothing created under either sandbox root.
    home_files = list(sandbox["home"].rglob("*"))
    project_files = list(sandbox["project"].rglob("*"))
    assert [p for p in home_files if p.is_file()] == []
    assert [p for p in project_files if p.is_file()] == []

    # The intended instruction path is surfaced (on stderr — stdout stays clean).
    assert "Instruction file" in result.stderr


def test_dry_run_json_envelope(sandbox: dict[str, Path]) -> None:
    """`--json` dry-run emits one success envelope listing every action."""
    result = runner.invoke(
        app, ["install", "--json", "codex"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.stdout
    env = json.loads(result.stdout.strip())
    assert env["ok"] is True
    assert env["command"] == "install"
    data = env["data"]
    assert data["harness"] == "codex"
    assert data["write"] is False
    assert set(data["mcp"]) == {"path", "action", "note"}
    assert set(data["instruction"]) == {"path", "action"}


def test_write_json_config_idempotent(sandbox: dict[str, Path]) -> None:
    """`install cursor --write` writes MCP JSON with the reused top key; the
    second write is byte-identical (idempotent)."""
    r1 = runner.invoke(app, ["install", "cursor", "--write"], catch_exceptions=False)
    assert r1.exit_code == 0, r1.stdout + r1.stderr

    cfg = sandbox["home"] / ".cursor" / "mcp.json"
    assert cfg.is_file()
    data = json.loads(cfg.read_text())
    assert "mcpServers" in data  # reused CLIENTS["cursor"] top key
    assert "anvil" in data["mcpServers"]
    first = cfg.read_text()

    r2 = runner.invoke(app, ["install", "cursor", "--write"], catch_exceptions=False)
    assert r2.exit_code == 0
    assert cfg.read_text() == first  # idempotent


def test_write_json_preserves_unrelated_server(sandbox: dict[str, Path]) -> None:
    """A pre-existing unrelated server in the target JSON survives the merge."""
    cfg = sandbox["home"] / ".cursor" / "mcp.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))

    result = runner.invoke(
        app, ["install", "cursor", "--write"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    data = json.loads(cfg.read_text())
    assert data["mcpServers"]["other"] == {"command": "x"}
    assert "anvil" in data["mcpServers"]


def test_write_toml_codex_preserves_unrelated_table(sandbox: dict[str, Path]) -> None:
    """codex TOML merge: pre-seeded `[mcp_servers.other]` survives; anvil added."""
    cfg = sandbox["home"] / ".codex" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        'model = "gpt-5"\n\n'
        "[mcp_servers.other]\n"
        'command = "other-bin"\n'
        'args = ["--flag"]\n'
    )

    result = runner.invoke(
        app, ["install", "codex", "--write"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    parsed = tomllib.loads(cfg.read_text())
    assert parsed["model"] == "gpt-5"  # top-level scalar preserved
    assert parsed["mcp_servers"]["other"]["command"] == "other-bin"  # survives
    assert "anvil" in parsed["mcp_servers"]  # ours added
    assert parsed["mcp_servers"]["anvil"]["command"] == "bash"


def test_write_toml_codex_idempotent(sandbox: dict[str, Path]) -> None:
    """Re-running the codex write keeps a single anvil table (idempotent)."""
    cfg = sandbox["home"] / ".codex" / "config.toml"
    runner.invoke(app, ["install", "codex", "--write"], catch_exceptions=False)
    first = cfg.read_text()
    runner.invoke(app, ["install", "codex", "--write"], catch_exceptions=False)
    assert cfg.read_text() == first


def test_instruction_file_is_agents_md_bytes(sandbox: dict[str, Path]) -> None:
    """The dropped instruction file is byte-equal to the repo AGENTS.md."""
    result = runner.invoke(
        app, ["install", "cursor", "--write"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    instr = sandbox["project"] / "AGENTS.md"
    assert instr.is_file()
    assert instr.read_bytes() == (_repo_root() / "AGENTS.md").read_bytes()


def test_copilot_instruction_dest_and_bytes(sandbox: dict[str, Path]) -> None:
    """Copilot's instruction dest is .github/copilot-instructions.md, same bytes."""
    result = runner.invoke(
        app, ["install", "copilot", "--write"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    instr = sandbox["project"] / ".github" / "copilot-instructions.md"
    assert instr.is_file()
    assert instr.read_bytes() == (_repo_root() / "AGENTS.md").read_bytes()


@pytest.mark.parametrize("harness", ["gemini", "cline"])
def test_mcp_none_writes_only_instruction(
    harness: str, sandbox: dict[str, Path]
) -> None:
    """`mcp_merge="none"` harness writes ONLY the instruction file + shows note."""
    result = runner.invoke(
        app, ["install", harness, "--write"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # Instruction written.
    assert (sandbox["project"] / "AGENTS.md").is_file()
    # No MCP config files anywhere.
    assert not (sandbox["home"] / ".codex").exists()
    assert not (sandbox["home"] / ".cursor").exists()
    # The note is surfaced.
    assert "skipped" in result.stderr

    # And the --json envelope marks the MCP action as skipped.
    j = runner.invoke(
        app, ["install", "--json", harness], catch_exceptions=False
    )
    data = json.loads(j.stdout.strip())["data"]
    assert data["mcp"]["action"] == "skipped"
    assert data["mcp"]["path"] is None
    assert data["mcp"]["note"]  # non-empty note


def test_openhands_writes_microagent_file(sandbox: dict[str, Path]) -> None:
    """openhands install drops AGENTS.md bytes at .openhands/microagents/anvil.md."""
    result = runner.invoke(
        app, ["install", "openhands", "--write"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # Instruction written at the OpenHands microagent path, not AGENTS.md root.
    instr = sandbox["project"] / ".openhands" / "microagents" / "anvil.md"
    assert instr.is_file(), f"expected {instr} to exist"
    assert instr.read_bytes() == (_repo_root() / "AGENTS.md").read_bytes()
    # No MCP config file (mcp_merge="none").
    assert not (sandbox["home"] / ".codex").exists()
    assert not (sandbox["home"] / ".cursor").exists()
    # Note is surfaced on stderr.
    assert "skipped" in result.stderr

    # JSON envelope: MCP action is skipped, non-empty note.
    j = runner.invoke(
        app, ["install", "--json", "openhands"], catch_exceptions=False
    )
    data = json.loads(j.stdout.strip())["data"]
    assert data["mcp"]["action"] == "skipped"
    assert data["mcp"]["path"] is None
    assert data["mcp"]["note"]


def test_root_flag_propagates_into_written_block(sandbox: dict[str, Path]) -> None:
    """`--root /x` puts env.ANVIL_ROOT into the written MCP server block."""
    result = runner.invoke(
        app, ["install", "cursor", "--write", "--root", "/x"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    cfg = sandbox["home"] / ".cursor" / "mcp.json"
    spec = json.loads(cfg.read_text())["mcpServers"]["anvil"]
    assert spec["env"]["ANVIL_ROOT"] == "/x"


def test_uv_run_flag_propagates_into_written_block(sandbox: dict[str, Path]) -> None:
    """`--uv-run` emits the explicit uv invocation in the written block."""
    result = runner.invoke(
        app, ["install", "cursor", "--write", "--uv-run"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    cfg = sandbox["home"] / ".cursor" / "mcp.json"
    spec = json.loads(cfg.read_text())["mcpServers"]["anvil"]
    assert spec["command"] == "uv"
    assert spec["args"][0] == "run"
    assert "anvil.mcp_server" in spec["args"]


def test_unknown_harness_fails(sandbox: dict[str, Path]) -> None:
    """Bad harness exits 2; under --json emits error.code == bad_request."""
    result = runner.invoke(app, ["install", "nope"], catch_exceptions=False)
    assert result.exit_code == 2

    j = runner.invoke(app, ["install", "--json", "nope"], catch_exceptions=False)
    assert j.exit_code == 2
    env = json.loads(j.stdout.strip())
    assert env["ok"] is False
    assert env["error"]["code"] == "bad_request"
