"""Standalone-docs self-sufficiency gate (backlog T011 / feature F002).

anvil is a standalone product, driven entirely through its own CLI and MCP
surface. The onboarding docs are the contract for that promise: the Getting
Started walkthrough and the README Quick Start must lead with the full
init → prd → plan → claim → execute → finish loop on the CLI/MCP surface
alone, and must carry **no** reference to any other plugin.

This module enforces that contract so the standalone story cannot silently
rot back into "you also need another plugin". The onboarding docs must not
name a sibling plugin (``fakoli-flow`` / ``fakoli-crew``), invoke a
``flow:`` / ``crew:`` command token, or use "trinity" framing. (The
``github.com/fakoli`` org URL and the ``fakoli`` marketplace name are the
org/marketplace, not a sibling plugin, and do not match the pattern.)

Layout note: this test lives at ``<repo-root>/tests/test_standalone_docs.py``
and the docs live at ``<repo-root>/docs/`` and ``<repo-root>/README.md``;
``parents[1]`` is the repo root — matching ``test_token_budget.py`` and
``test_version_sync.py``. Pytest is run from ``bin/`` whose
``pyproject.toml`` points ``testpaths`` at ``../tests``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    """``<root>/tests/test_standalone_docs.py`` → ``parents[1]`` is the root."""
    return Path(__file__).resolve().parents[1]


def _getting_started() -> Path:
    return _repo_root() / "docs" / "how-to" / "getting-started.md"


def _readme() -> Path:
    return _repo_root() / "README.md"


# ---------------------------------------------------------------------------
# Sibling-reference detection
# ---------------------------------------------------------------------------

#: A sibling reference is the plugin name (``fakoli-flow`` / ``fakoli-crew``),
#: a ``flow:`` / ``crew:`` command token (e.g. ``/flow:execute``,
#: ``fakoli-crew:welder``), or "trinity" framing. ``\b`` before the command
#: token keeps ``workflow:`` from matching ``flow:``.
_SIBLING_RE = re.compile(
    r"\bfakoli-flow\b|\bfakoli-crew\b|\btrinity\b|\b(?:flow|crew):",
    re.IGNORECASE,
)


def _sibling_refs(text: str) -> list[str]:
    return _SIBLING_RE.findall(text)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_onboarding_docs_exist() -> None:
    """Guard: the gate is meaningless if the docs it checks are missing."""
    for path in (_getting_started(), _readme()):
        assert path.is_file(), (
            f"Expected onboarding doc at {path}. The standalone-docs gate "
            "cannot run; check the repo layout."
        )


@pytest.mark.parametrize("doc", ["getting_started", "readme"])
def test_onboarding_doc_has_no_sibling_references(doc: str) -> None:
    """Onboarding docs must not reference any sibling plugin.

    anvil is documented as a standalone product. A ``fakoli-flow`` /
    ``fakoli-crew`` name, a ``flow:`` / ``crew:`` command token, or "trinity"
    framing in the README or Getting Started walkthrough breaks that promise.
    """
    path = _getting_started() if doc == "getting_started" else _readme()
    refs = _sibling_refs(path.read_text(encoding="utf-8"))
    assert not refs, (
        f"{path.name} must not reference any sibling plugin "
        f"(found {sorted(set(refs))}). anvil is standalone — drop the "
        "fakoli-flow / fakoli-crew / flow: / crew: / trinity references. "
        "(github.com/fakoli org URLs are fine and do not match.)"
    )


@pytest.mark.parametrize(
    "marker",
    [
        "anvil init",  # standalone scaffold
        "anvil prd parse",  # standalone prd parse
        "anvil plan",  # standalone plan
        "anvil claim",  # standalone claim
        "anvil submit",  # standalone evidence
        "anvil apply",  # standalone finish
    ],
)
def test_getting_started_covers_full_standalone_loop(marker: str) -> None:
    """The walkthrough must cover every loop stage via the CLI.

    Acceptance criterion: the standalone path runs init → prd → plan → claim →
    execute → finish through CLI/MCP only. Each stage's CLI command must be
    present in the walkthrough.
    """
    text = _getting_started().read_text(encoding="utf-8")
    assert marker in text, (
        f"docs/how-to/getting-started.md must demonstrate the standalone loop "
        f"stage `{marker}` so the path is complete end-to-end."
    )
