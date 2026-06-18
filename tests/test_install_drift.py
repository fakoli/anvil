"""Drift guard: per-harness instruction copies stay byte-identical to AGENTS.md.

Any committed file that is *meant to be* a copy of ``AGENTS.md`` must be
byte-identical to it. ``install --write`` regenerates these copies from
``AGENTS.md``, so the fix for a failure here is a one-command regen — the guard
stays cheap. Gemini's ``contextFileName`` points at ``AGENTS.md`` itself (not a
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
]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_instruction_copies_match_agents_md() -> None:
    agents = (_repo_root() / "AGENTS.md").read_bytes()
    for rel in COPIES:
        p = _repo_root() / rel
        assert p.is_file(), f"missing instruction copy: {rel}"
        assert p.read_bytes() == agents, (
            f"{rel} has drifted from AGENTS.md — regenerate it "
            "(`anvil install <harness> --write`)."
        )


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
