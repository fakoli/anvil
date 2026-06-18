"""Token-footprint CI budget gate (backlog T013 / feature F003).

anvil ships as a Claude Code plugin whose skills are *always* visible:
Claude Code injects every skill's frontmatter ``name`` + ``description`` into
the system prompt so the model can decide when to invoke a skill. That
frontmatter is paid for on *every* turn of *every* conversation, whether or
not the skill ever fires. The full ``SKILL.md`` body is loaded only when the
skill is actually invoked, but a runaway body still bloats the context the
moment a user reaches for that skill.

This module is a self-audit: it measures the combined token footprint of the
always-loaded skill frontmatter (the plugin's "command surface" — the slash
commands a user sees) and the full per-skill bodies, and fails CI if either
exceeds an explicit budget. The budgets are set from the current measured
baseline plus headroom, so the gate catches *growth* (a skill quietly
doubling in size) without flapping on small edits.

Design choices that keep this CI-safe and deterministic:

  * **No external tokenizer dependency.** We approximate token count with the
    well-known ``ceil(chars / 4)`` heuristic rather than pulling in
    ``tiktoken`` (a heavy, network-fetching, model-specific dependency). The
    heuristic is stable across machines and Python versions, which is exactly
    what a regression gate needs — we care about *relative* growth against a
    fixed baseline, not byte-exact agreement with a specific model's BPE.
  * **Budgets live next to the test and in docs.** The numbers below mirror
    ``docs/context-budget.md``; that doc is the human-readable contract and
    this test enforces it.

If a budget legitimately needs to grow (a new skill, a deliberate expansion),
bump the constant here AND update ``docs/context-budget.md`` in the same
change — the test asserts the doc exists and names the gate so the two cannot
silently drift.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Budgets — set from the measured baseline (see docs/context-budget.md) with
# headroom. Baseline as of T013:
#   * always-loaded frontmatter total ≈ 688 tokens  → budget 1000
#   * largest single SKILL.md         ≈ 5097 tokens  → ceiling 6000
#   * all SKILL.md bodies combined    ≈ 33200 tokens → budget 40000
# Headroom (~15-45%) absorbs ordinary edits; doubling a skill trips the gate.
# ---------------------------------------------------------------------------

#: Max tokens for the combined always-loaded skill frontmatter (the command
#: surface that Claude Code injects on every turn).
ALWAYS_LOADED_FRONTMATTER_BUDGET = 1000

#: Max tokens for any single SKILL.md body (full file, loaded on invocation).
PER_SKILL_FULL_CEILING = 6000

#: Max tokens for all SKILL.md bodies combined.
TOTAL_FULL_BUDGET = 40000


def _plugin_root() -> Path:
    """Return the absolute path of the anvil plugin (repo) root.

    The test file lives at ``<root>/tests/test_token_budget.py``, so
    ``parents[1]`` is the repo root — matching ``test_version_sync.py``.
    """
    return Path(__file__).resolve().parents[1]


def _skill_files() -> list[Path]:
    """All shipped ``SKILL.md`` files, sorted for deterministic reporting."""
    return sorted(_plugin_root().glob("skills/*/SKILL.md"))


def _estimate_tokens(text: str) -> int:
    """Approximate token count with the standard ``ceil(chars / 4)`` heuristic.

    Deterministic across machines and Python versions — no tokenizer download,
    no model-specific BPE. Good enough for a relative-growth regression gate.
    """
    return math.ceil(len(text) / 4)


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Return ``(frontmatter, body)`` for a SKILL.md.

    Frontmatter is the YAML block between the first two ``---`` fences (this is
    the part Claude Code always loads). If a file has no frontmatter we treat
    the whole thing as body so the per-file ceiling still applies.
    """
    match = _FRONTMATTER_RE.match(text)
    if match:
        return match.group(1), match.group(2)
    return "", text


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_skill_files_are_discoverable() -> None:
    """Guard: the gate is meaningless if it silently measures zero skills."""
    skills = _skill_files()
    assert skills, (
        "No skills/*/SKILL.md files found under "
        f"{_plugin_root()}. The token-budget gate cannot run; check the "
        "repo layout / glob in _skill_files()."
    )


def test_always_loaded_frontmatter_within_budget() -> None:
    """Combined always-loaded frontmatter must stay under the per-turn budget.

    This is the cost paid on *every* conversation turn regardless of which
    skills fire — the most important number to keep frugal.
    """
    per_skill: list[tuple[str, int]] = []
    total = 0
    for skill in _skill_files():
        frontmatter, _body = _split_frontmatter(skill.read_text(encoding="utf-8"))
        toks = _estimate_tokens(frontmatter)
        per_skill.append((skill.parent.name, toks))
        total += toks

    report = "\n".join(f"    {name:<20} {toks:>5} tok" for name, toks in per_skill)
    assert total <= ALWAYS_LOADED_FRONTMATTER_BUDGET, (
        f"Always-loaded skill frontmatter footprint {total} tokens exceeds "
        f"budget {ALWAYS_LOADED_FRONTMATTER_BUDGET}.\n"
        f"Per-skill frontmatter token counts:\n{report}\n"
        f"Trim skill descriptions, or raise ALWAYS_LOADED_FRONTMATTER_BUDGET "
        f"and docs/context-budget.md together if the growth is intentional."
    )


def test_no_skill_body_exceeds_per_file_ceiling() -> None:
    """Flag any single SKILL.md whose full body blows the per-file ceiling.

    The body loads only on invocation, but an oversized one still floods the
    context the moment a user reaches for that skill. Report every offender
    (not just the first) so a single run names all the work.
    """
    over_ceiling: list[tuple[str, int]] = []
    per_skill: list[tuple[str, int]] = []
    for skill in _skill_files():
        toks = _estimate_tokens(skill.read_text(encoding="utf-8"))
        per_skill.append((skill.parent.name, toks))
        if toks > PER_SKILL_FULL_CEILING:
            over_ceiling.append((skill.parent.name, toks))

    report = "\n".join(f"    {name:<20} {toks:>6} tok" for name, toks in per_skill)
    assert not over_ceiling, (
        "These SKILL.md files exceed the per-file ceiling "
        f"{PER_SKILL_FULL_CEILING} tokens: "
        + ", ".join(f"{name} ({toks})" for name, toks in over_ceiling)
        + f"\nPer-skill full-body token counts:\n{report}\n"
        + "Split the skill, move detail into linked reference docs, or raise "
        + "PER_SKILL_FULL_CEILING and docs/context-budget.md together."
    )


def test_total_skill_body_footprint_within_budget() -> None:
    """All SKILL.md bodies combined must stay under the aggregate budget."""
    per_skill: list[tuple[str, int]] = []
    total = 0
    for skill in _skill_files():
        toks = _estimate_tokens(skill.read_text(encoding="utf-8"))
        per_skill.append((skill.parent.name, toks))
        total += toks

    report = "\n".join(f"    {name:<20} {toks:>6} tok" for name, toks in per_skill)
    assert total <= TOTAL_FULL_BUDGET, (
        f"Combined SKILL.md body footprint {total} tokens exceeds budget "
        f"{TOTAL_FULL_BUDGET}.\nPer-skill full-body token counts:\n{report}\n"
        f"Trim or split skills, or raise TOTAL_FULL_BUDGET and "
        f"docs/context-budget.md together if intentional."
    )


def test_budget_is_documented() -> None:
    """The budget must be documented (acceptance criterion #3).

    docs/context-budget.md is the human-readable contract; it must exist and
    name every budget knob so the doc and this test cannot silently drift.
    """
    doc = _plugin_root() / "docs" / "context-budget.md"
    assert doc.is_file(), (
        f"Expected the context budget to be documented at {doc} (or referenced "
        "from docs/architecture.md). Acceptance criterion: the budget is "
        "documented."
    )
    text = doc.read_text(encoding="utf-8")
    for knob in (
        "ALWAYS_LOADED_FRONTMATTER_BUDGET",
        "PER_SKILL_FULL_CEILING",
        "TOTAL_FULL_BUDGET",
    ):
        assert knob in text, (
            f"docs/context-budget.md must reference the budget knob {knob!r} "
            "so the doc and tests/test_token_budget.py stay in sync."
        )
