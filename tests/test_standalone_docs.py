"""Standalone-docs self-sufficiency gate (backlog T011 / feature F002).

anvil must be usable *standalone* — driven entirely through its own CLI
and MCP surface, with no `fakoli-flow` or `fakoli-crew` plugin installed. The
onboarding docs are the contract for that promise: the Getting Started
walkthrough and the README Quick Start must lead with a crew/flow-free path
(init → prd → plan → claim → execute → finish via CLI/MCP only), and any
mention of the sibling plugins must be an explicitly *optional*, additive
section — never a required step.

This module enforces that contract so the standalone story cannot silently rot
back into "you also need flow/crew". It mirrors the literal verification
command from the backlog item::

    grep -L -e 'flow:' -e 'crew:' docs/how-to/getting-started.md

i.e. the standalone walkthrough must reference no `flow:`/`crew:` command
token. We add two further structural checks: the standalone walkthrough must
come *first* (lead), and an explicit "Optional" integration section must exist
and be marked additive.

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
    """Absolute path of the anvil repo root.

    ``<root>/tests/test_standalone_docs.py`` → ``parents[1]`` is the root.
    """
    return Path(__file__).resolve().parents[1]


def _getting_started() -> Path:
    return _repo_root() / "docs" / "how-to" / "getting-started.md"


def _readme() -> Path:
    return _repo_root() / "README.md"


# ---------------------------------------------------------------------------
# Command-token detection
# ---------------------------------------------------------------------------

#: A "crew/flow command token" is the literal substring the backlog
#: verification greps for: ``flow:`` or ``crew:``. These appear in slash
#: commands (``/flow:execute``), skill refs (``fakoli-flow:execute``), and
#: agent refs (``fakoli-crew:welder``) — i.e. anywhere a flow/crew *command*
#: is invoked. Plain prose mentions of the plugin *names* (``fakoli-flow``,
#: ``fakoli-crew``) do NOT contain a colon and are intentionally allowed, so
#: the standalone doc can still *describe* the optional siblings by name.
_COMMAND_TOKEN_RE = re.compile(r"(?:flow|crew):")


def _command_tokens(text: str) -> list[str]:
    """Return every ``flow:`` / ``crew:`` command token found in ``text``."""
    return _COMMAND_TOKEN_RE.findall(text)


# ---------------------------------------------------------------------------
# Section helpers
# ---------------------------------------------------------------------------

_OPTIONAL_HEADING_RE = re.compile(
    r"^#{2,3}\s+Optional.*?(?:fakoli-flow|fakoli-crew|flow|crew|integrat)",
    re.IGNORECASE | re.MULTILINE,
)


def _optional_section_start(text: str) -> int | None:
    """Index of the first explicit *Optional* integration heading, or None."""
    match = _OPTIONAL_HEADING_RE.search(text)
    return match.start() if match else None


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


def test_getting_started_walkthrough_is_crew_flow_free() -> None:
    """The Getting Started walkthrough must reference no flow/crew command.

    This is the backlog's literal verification:
        grep -L -e 'flow:' -e 'crew:' docs/how-to/getting-started.md
    The standalone init → prd → plan → claim → execute → finish path must run
    on the CLI/MCP surface alone. Plugin *names* (no colon) are fine; a
    ``flow:`` / ``crew:`` *command token* is not.
    """
    text = _getting_started().read_text(encoding="utf-8")
    tokens = _command_tokens(text)
    assert not tokens, (
        "docs/how-to/getting-started.md must not reference any flow/crew "
        f"command token (found {sorted(set(tokens))}). The standalone "
        "walkthrough has to run on the anvil CLI/MCP surface alone. "
        "Refer to the optional siblings by plugin name (fakoli-flow / "
        "fakoli-crew, no colon) and link to "
        "integrating-with-fakoli-flow-and-crew.md instead of naming a "
        "flow:/crew: command as a step."
    )


def test_getting_started_leads_with_standalone_then_optional() -> None:
    """Standalone walkthrough leads; the Optional integration section follows.

    The first core CLI step (``anvil init``) must appear *before* any
    explicit Optional integration heading, proving the doc leads with the
    crew/flow-free path rather than front-loading integration.
    """
    text = _getting_started().read_text(encoding="utf-8")

    init_idx = text.find("anvil init")
    assert init_idx != -1, (
        "docs/how-to/getting-started.md should walk through `anvil "
        "init` as the first standalone step."
    )

    optional_idx = _optional_section_start(text)
    assert optional_idx is not None, (
        "docs/how-to/getting-started.md must contain an explicit "
        "'## Optional: fakoli-flow / fakoli-crew integration' section so the "
        "additive nature of the siblings is unambiguous."
    )
    assert init_idx < optional_idx, (
        "The standalone walkthrough (starting at `anvil init`) must "
        "lead; the Optional integration section must come after it, not "
        "before."
    )


def test_getting_started_optional_section_marked_additive() -> None:
    """The Optional section must call itself out as additive / not required."""
    text = _getting_started().read_text(encoding="utf-8")
    start = _optional_section_start(text)
    assert start is not None, (
        "docs/how-to/getting-started.md is missing the explicit Optional "
        "integration heading."
    )
    section = text[start:]
    lowered = section.lower()
    assert any(
        phrase in lowered
        for phrase in ("additive", "opt-in", "optional", "not a prerequisite")
    ), (
        "The Optional integration section must explicitly mark the siblings "
        "as additive / opt-in (e.g. 'purely additive', 'opt-in upgrade', "
        "'never a prerequisite')."
    )


def test_readme_quick_start_leads_before_optional_integration() -> None:
    """README must lead with the standalone Quick Start, then mark integration
    optional.

    The Quick Start heading must appear before the integration section, and
    that integration section must be explicitly titled *Optional* and flagged
    additive — so a first-time reader sees the crew/flow-free path first.
    """
    text = _readme().read_text(encoding="utf-8")

    quick_start_idx = text.find("## Quick Start")
    assert quick_start_idx != -1, "README is missing a '## Quick Start' section."

    optional_idx = _optional_section_start(text)
    assert optional_idx is not None, (
        "README must title its flow/crew section 'Optional: integration with "
        "fakoli-flow and fakoli-crew' so the additive nature is explicit."
    )
    assert quick_start_idx < optional_idx, (
        "README must lead with the standalone Quick Start before the Optional "
        "integration section."
    )

    section = text[optional_idx:]
    # Bound the section to its own heading block for the additive check.
    next_heading = re.search(r"\n##\s", section[3:])
    if next_heading:
        section = section[: next_heading.start() + 3]
    lowered = section.lower()
    assert "additive" in lowered or "optional" in lowered, (
        "The README Optional integration section must explicitly mark the "
        "siblings as additive / optional."
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
    """The crew/flow-free walkthrough must cover every loop stage via the CLI.

    Acceptance criterion: the standalone path runs init → prd → plan → claim →
    execute → finish through CLI/MCP only. Each stage's CLI command must be
    present in the walkthrough.
    """
    text = _getting_started().read_text(encoding="utf-8")
    assert marker in text, (
        f"docs/how-to/getting-started.md must demonstrate the standalone loop "
        f"stage `{marker}` so the crew/flow-free path is complete end-to-end."
    )
