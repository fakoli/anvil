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
``test_version_sync.py``. Explicit pytest paths run from ``bin/`` discover the
repository-root ``pytest.ini`` through ``../tests``.
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


def _published_docs() -> list[Path]:
    """Maintained Markdown published by MkDocs, excluding preserved archives."""
    docs_root = _repo_root() / "docs"
    return [
        path
        for path in docs_root.rglob("*.md")
        if "archive" not in path.relative_to(docs_root).parts
    ]


_PUBLISHED_PRD_COMMAND_DOCS = (
    "docs/backlog/anvil-backlog.prd.md",
    "docs/backlog/multi-prd-revisable.prd.md",
)


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


def test_published_docs_do_not_advertise_stale_pytest_launch_shapes() -> None:
    """Published commands must use the current root project and real test names."""
    stale_shapes = (
        "cd bin && uv run pytest tests/",
        "cd plugins/anvil",
        "--project plugins/anvil/bin",
        "test_mcp_server.py",
    )
    findings: list[str] = []
    for path in _published_docs():
        text = path.read_text(encoding="utf-8")
        for stale in stale_shapes:
            if stale in text:
                findings.append(f"{path.relative_to(_repo_root())}: {stale}")
    assert not findings, "stale published pytest commands:\n" + "\n".join(findings)


def test_published_prd_pytest_commands_reference_existing_test_files() -> None:
    """Verification bullets in maintained PRDs may only name tests that exist."""
    missing: list[str] = []
    pattern = re.compile(r"(?<![A-Za-z0-9_./-])(?:\.\./)?(tests/[A-Za-z0-9_./-]+\.py)")
    for relative in _PUBLISHED_PRD_COMMAND_DOCS:
        path = _repo_root() / relative
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.lstrip().startswith("- `") or "pytest" not in line:
                continue
            for match in pattern.finditer(line):
                test_path = _repo_root() / match.group(1)
                if not test_path.is_file():
                    missing.append(f"{relative}:{line_number}: {match.group(0)}")
    assert not missing, "published pytest commands name missing tests:\n" + "\n".join(missing)


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
