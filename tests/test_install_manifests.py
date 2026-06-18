"""Static packaging-manifest validity for the VERIFIED harnesses.

Each manifest under ``packaging/<harness>/`` is checked to parse and carry the
required fields. For NON-verified harnesses we commit a STUB + TODO instead of a
guessed manifest — those STUBs are guarded here too (must contain ``TODO`` and
must NOT contain a JSON/TOML manifest body, so a stub can't silently become a
guessed config).

Layout note: this file lives at ``<repo-root>/tests/`` so ``parents[1]`` is the
repo root (matching ``test_agents_md.py`` / ``test_version_sync.py``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _packaging() -> Path:
    return _repo_root() / "packaging"


# --- codex: plugin.json (VERIFIED) ---------------------------------------


def test_codex_plugin_json_parses_and_has_fields() -> None:
    p = _packaging() / "codex" / ".codex-plugin" / "plugin.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    for field in ("name", "version", "description", "license", "skills",
                  "mcpServers", "interface"):
        assert field in data, f"codex plugin.json missing {field!r}"
    assert data["name"] == "anvil"
    # The Codex validator rejects `hooks` — it must be ABSENT.
    assert "hooks" not in data
    # mcpServers points at the bundled .mcp.json.
    assert data["mcpServers"] == "./.mcp.json"


def test_codex_plugin_version_matches_anvil_version() -> None:
    """plugin.json version is synced to anvil.__version__ (reuse the
    test_version_sync.py spirit)."""
    import anvil

    p = _packaging() / "codex" / ".codex-plugin" / "plugin.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["version"] == anvil.__version__, (
        f"codex plugin.json version {data['version']!r} != "
        f"anvil.__version__ {anvil.__version__!r} — keep them synced."
    )


def test_codex_mcp_json_matches_codex_envelope() -> None:
    """The bundled .mcp.json carries an `anvil` server pointing at bin/anvil-mcp."""
    p = _packaging() / "codex" / ".mcp.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert "mcpServers" in data
    spec = data["mcpServers"]["anvil"]
    assert spec["command"] == "bash"
    assert spec["args"][-1].endswith("bin/anvil-mcp")


# --- codex: marketplace.json (VERIFIED) ----------------------------------


def test_codex_marketplace_json_parses_one_plugin() -> None:
    p = _packaging() / "codex" / ".agents" / "plugins" / "marketplace.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    plugins = data["plugins"]
    assert len(plugins) == 1
    entry = plugins[0]
    assert entry["name"] == "anvil"
    assert entry["source"]["source"] == "local"
    assert entry["policy"]["installation"] == "AVAILABLE"
    assert entry["policy"]["authentication"] == "ON_USE"


def test_codex_marketplace_version_synced() -> None:
    import anvil

    p = _packaging() / "codex" / ".agents" / "plugins" / "marketplace.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["plugins"][0]["version"] == anvil.__version__


# --- gemini: gemini-extension.json (VERIFIED) ----------------------------


def test_gemini_extension_json() -> None:
    p = _packaging() / "gemini" / "gemini-extension.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["name"] == "anvil"
    # contextFileName points at AGENTS.md ITSELF (not a copy) → drift-guard
    # asserts the field value, not a file copy (see test_install_drift.py).
    assert data["contextFileName"] == "AGENTS.md"
    assert "anvil" in data["mcpServers"]
    spec = data["mcpServers"]["anvil"]
    assert spec["command"] == "bash"
    # Uses Gemini's ${extensionPath} substitution — portable inside the ext dir.
    assert any("${extensionPath}" in a for a in spec["args"])


def test_gemini_version_synced() -> None:
    import anvil

    p = _packaging() / "gemini" / "gemini-extension.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["version"] == anvil.__version__


# --- openclaw: README only (manifestless) --------------------------------


def test_openclaw_readme_exists() -> None:
    p = _packaging() / "openclaw" / "README.md"
    assert p.is_file()
    text = p.read_text(encoding="utf-8")
    # Documents that hooks are detected-but-not-executed.
    assert "hooks" in text.lower()


# --- STUBs (NOT verified) ------------------------------------------------


@pytest.mark.parametrize("harness", ["openhands", "cline"])
def test_stub_has_todo_and_no_manifest_body(harness: str) -> None:
    """STUBs must contain TODO and carry NO parseable JSON/TOML manifest body —
    guard against a stub silently becoming a guessed manifest."""
    p = _packaging() / harness / "STUB.md"
    assert p.is_file(), f"missing STUB for {harness}"
    text = p.read_text(encoding="utf-8")
    assert "TODO" in text, f"{harness} STUB.md must name what to verify (TODO)"

    # No JSON manifest body: a fenced ```json block, or a bare {...} object that
    # parses as a dict, would mean a guessed manifest leaked into the stub.
    assert "```json" not in text
    assert "```toml" not in text
    # A `{` followed later by a `}` that json.loads accepts as a dict is banned.
    if "{" in text and "}" in text:
        snippet = text[text.index("{"): text.rindex("}") + 1]
        try:
            parsed = json.loads(snippet)
        except json.JSONDecodeError:
            parsed = None
        assert not isinstance(parsed, dict), (
            f"{harness} STUB.md contains a parseable JSON object — that looks "
            "like a guessed manifest. STUBs must stay manifest-free."
        )
