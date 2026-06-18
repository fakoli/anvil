"""Drift gate: the AGENTS.md capability table must stay honest.

``AGENTS.md`` at the repo root is the cross-harness instruction file (read
natively by Codex/Copilot/Cursor/Windsurf/Cline/Roo/Gemini). Its MCP-tool ⇄
CLI-command table must not rot when a tool is added/renamed — so this gate
enumerates the LIVE FastMCP tool set (the same path ``describe`` uses) and
asserts every tool name appears in AGENTS.md. It also guards that the
cross-harness docs carry no Claude-Code-only ``${CLAUDE_PLUGIN_ROOT}`` token.

Layout note: this file lives at ``<repo-root>/tests/test_agents_md.py`` so
``parents[1]`` is the repo root (matching ``test_standalone_docs.py`` /
``test_version_sync.py``).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _agents_md() -> Path:
    return _repo_root() / "AGENTS.md"


def _how_to() -> Path:
    return _repo_root() / "docs" / "how-to" / "using-anvil-on-any-harness.md"


def _live_mcp_tool_names() -> list[str]:
    from anvil.mcp_server import mcp

    tools = asyncio.run(mcp.list_tools())
    return sorted(t.name for t in tools)


def test_agents_md_exists() -> None:
    """The cross-harness instruction file is at the repo root."""
    assert _agents_md().is_file(), (
        f"Expected AGENTS.md at {_agents_md()} (repo root). It is the "
        "cross-harness instruction file other harnesses read natively."
    )


def test_every_mcp_tool_listed() -> None:
    """Every registered MCP tool name must appear in AGENTS.md.

    Keeps the capability table from drifting when a tool is added or renamed —
    the same anti-drift spirit as ``test_cli.py::TestDescribe``.
    """
    text = _agents_md().read_text(encoding="utf-8")
    missing = [name for name in _live_mcp_tool_names() if name not in text]
    assert not missing, (
        f"AGENTS.md capability table is stale — these live MCP tools are not "
        f"listed: {missing}. Add them to the MCP-tool ⇄ CLI-command table "
        "(tool names mirror bin/src/anvil/mcp_server.py)."
    )


@pytest.mark.parametrize("path_fn", [_agents_md, _how_to])
def test_no_claude_plugin_root_token(path_fn) -> None:  # type: ignore[no-untyped-def]
    """Cross-harness files must be token-free (no ${CLAUDE_PLUGIN_ROOT})."""
    path = path_fn()
    assert path.is_file(), f"missing cross-harness doc: {path}"
    text = path.read_text(encoding="utf-8")
    assert "CLAUDE_PLUGIN_ROOT" not in text, (
        f"{path.name} must not reference ${{CLAUDE_PLUGIN_ROOT}} — it is a "
        "Claude-Code-only token and these files are cross-harness."
    )
