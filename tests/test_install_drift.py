"""Drift guard: per-harness instruction copies stay byte-identical to AGENTS.md.

Any committed file that is *meant to be* a copy of ``AGENTS.md`` must be
byte-identical to it. These are hand-maintained committed references (the MCP-only
harnesses no longer auto-splice an instruction file, so ``install`` does not
regenerate them) — the fix for a failure here is to copy ``AGENTS.md`` over the
drifted file. Gemini's ``contextFileName`` points at ``AGENTS.md`` itself (not a
copy), so it is drift-guarded by ``test_install_manifests.py`` asserting the
manifest field value, not here.

Layout note: this file lives at ``<repo-root>/tests/`` so ``parents[1]`` is the
repo root (matching ``test_agents_md.py``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Committed files that MUST be byte-identical copies of AGENTS.md.
# Add a row whenever a harness ships an AGENTS.md duplicate.
COPIES = [
    "packaging/copilot/.github/copilot-instructions.md",
    # Package data: `anvil install` reads this via importlib.resources so the
    # instruction splice works from a wheel install (no checkout). Keep it in sync
    # with the canonical root AGENTS.md.
    "bin/src/anvil/_data/AGENTS.md",
]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_instruction_copies_match_agents_md() -> None:
    agents = (_repo_root() / "AGENTS.md").read_bytes()
    for rel in COPIES:
        p = _repo_root() / rel
        assert p.is_file(), f"missing instruction copy: {rel}"
        assert p.read_bytes() == agents, (
            f"{rel} has drifted from AGENTS.md — copy AGENTS.md over it "
            "(it's a hand-maintained reference, not install-regenerated)."
        )


def test_codex_automation_package_data_matches_source() -> None:
    """Codex automation templates ship as package data (anvil/_data/) so a wheel
    install can find them via importlib.resources. The committed package copy must
    match the canonical packaging/ tree, file-set and bytes."""
    src = _repo_root() / "packaging" / "codex" / "automations"
    pkg = _repo_root() / "bin" / "src" / "anvil" / "_data" / "packaging" / "codex" / "automations"
    src_files = sorted(p.relative_to(src) for p in src.rglob("*") if p.is_file())
    pkg_files = sorted(p.relative_to(pkg) for p in pkg.rglob("*") if p.is_file())
    assert pkg_files == src_files, (
        "codex automation template file set drifted — re-copy "
        "packaging/codex/automations into bin/src/anvil/_data/packaging/codex/automations"
    )
    for rel in src_files:
        assert (pkg / rel).read_bytes() == (src / rel).read_bytes(), f"{rel} drifted from source"


@pytest.mark.parametrize("rel", COPIES)
def test_instruction_copies_have_no_plugin_root_token(rel: str) -> None:
    """Cross-harness copies must carry no Claude-Code-only token.

    Reuses the no-${CLAUDE_PLUGIN_ROOT} guard from ``test_agents_md.py`` for each
    committed copy.
    """
    text = (_repo_root() / rel).read_text(encoding="utf-8")
    assert "CLAUDE_PLUGIN_ROOT" not in text, (
        f"{rel} must not reference ${{CLAUDE_PLUGIN_ROOT}} — it is a "
        "Claude-Code-only token and these files are cross-harness."
    )
