"""Deterministic PRD template parser — no LLM, no I/O.

Turns a structured markdown PRD into Pydantic models.  All parse failures are
collected into ``ParseResult.errors``; nothing is raised.  Silent fallback is
explicitly rejected: if the parser cannot produce a coherent result it adds a
``ParseError`` and returns a partial (or empty) result so the caller can surface
the issue to the user.

Expected PRD structure (must match docs/prd-template.md):

    # Project: <Name>

    ## Summary
    <paragraph>

    ## Goals
    - <goal>

    ## Non-Goals          (optional)
    - <non-goal>

    ## Requirements
    - R001: <text>        (IDs auto-assigned if absent)

    ## Acceptance Criteria  (optional)
    - <criterion>

    ## Risks              (optional)
    - <risk>

    ## Open Questions     (optional)
    - <question>

    ## Assumptions        (optional)

    ### A001: <bounded statement>
    **Rationale:** <why this is a safe working premise>
    **Requirements:** R001, R002   (optional; absent means global)

    ## Features           (optional)

    ### F001: <Title>
    **Requirements:** R001, R002
    <description>

    ## Tasks              (optional)

    ### T001: <Title>
    **Feature:** F001
    **Priority:** medium
    **Type:** feature        (optional; feature|bugfix|refactor|modify)
    **Likely files:** path/to/foo.py
    **Acceptance criteria:**
    - <criterion>
    **Verification:**
    - `pytest ...`
    <description>
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, NamedTuple

import yaml

from anvil.state.models import (
    DEFAULT_PRD_ID,
    MAX_PRD_ASSUMPTION_ID_LENGTH,
    MAX_PRD_ASSUMPTION_RATIONALE_LENGTH,
    MAX_PRD_ASSUMPTION_REQUIREMENTS,
    MAX_PRD_ASSUMPTION_STATEMENT_LENGTH,
    MAX_PRD_ASSUMPTIONS,
    PRD,
    ArtifactAssertion,
    Feature,
    PRDAssumption,
    ProofKind,
    ProofRequirement,
    Requirement,
    Score,
    Task,
    TaskClaim,
    TaskPriority,
    TaskStatus,
    TaskType,
    Verification,
)

if TYPE_CHECKING:
    from anvil.clock import Clock
    from anvil.planning.llm import LLMProvider

__all__ = [
    "AcceptanceClause",
    "ParseError",
    "ParseResult",
    "parse_acceptance_grammar",
    "parse_prd",
]

# ---------------------------------------------------------------------------
# LLM augmentation constants (Phase 7 Wave 2)
# ---------------------------------------------------------------------------

_DESCRIPTION_ENRICH_SYSTEM_PROMPT = (
    "You are turning a one-line requirement into a self-contained task "
    "description for an AI agent. Keep it under 4 sentences. "
    "No marketing language."
)
_DESCRIPTION_ENRICH_MAX_TOKENS = 400

# Public so callers / docs / CLI help text can reference the threshold via
# `template.DESCRIPTION_SHORT_THRESHOLD` instead of duplicating the literal.
DESCRIPTION_SHORT_THRESHOLD = 50
# Backwards-compat alias for any internal/test imports of the private name.
_DESCRIPTION_SHORT_THRESHOLD = DESCRIPTION_SHORT_THRESHOLD

# The default ``prd_id`` for ``parse_prd``. When ``prd_id`` equals this
# sentinel the parser keeps the historical BARE id shape (``T001``, ``F001``,
# ``R001``) byte-identical to the pre-multi-PRD parser; any other ``prd_id``
# makes every minted id PREFIXED (``v0.2:T001``). Keeping the default PRD's ids
# bare deliberately limits the blast radius of prefixed ids to newly-named PRDs
# (a `^T\d+` matcher in claims/skills/drift still matches the default PRD).
DEFAULT_PARSE_PRD_ID = "prd"

# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------


class ParseError(NamedTuple):
    """A single parse failure collected into ParseResult.errors."""

    section: str
    line: int
    message: str


@dataclass
class ParseResult:
    """Output of parse_prd — always returned, never raised."""

    prd: PRD
    requirements: list[Requirement]
    features: list[Feature]
    tasks: list[Task]
    errors: list[ParseError]


class AcceptanceClause(NamedTuple):
    """A single acceptance criterion, optionally decomposed into a structured grammar.

    ``kind`` is one of:
        - ``"ears"``     — EARS-style "WHEN <trigger> THEN <response>" (and the
          related WHILE/WHERE/IF preconditions, plus the ubiquitous-form
          "THE SYSTEM SHALL <response>").
        - ``"gherkin"``  — Gherkin "Given <context> When <event> Then <outcome>".
        - ``"freeform"`` — no structured grammar detected; ``clauses`` is empty
          and ``text`` is the criterion verbatim.

    ``text`` always holds the original criterion text (re-joined for the
    multi-line Gherkin form).  ``clauses`` maps the recognised keyword (e.g.
    ``"when"``, ``"then"``, ``"given"``, ``"while"``, ``"if"``, ``"where"``,
    ``"shall"``) to its captured fragment.  The structured form is purely
    additive — callers that only want the raw strings keep using
    ``Task.acceptance_criteria`` unchanged.
    """

    text: str
    kind: str
    clauses: dict[str, str]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# Matches the bolded field lines in Feature / Task blocks.
# e.g. "**Requirements:** R001, R002" or "**Feature:** F001"
# Note: the colon may appear inside the bold markers (**Field:**) or outside.
# group(1) captures the field name (may include trailing colon).
# group(2) captures the value after the delimiter.
_FIELD_RE = re.compile(r"^\*\*([^*]+?)\*\*\s*:?\s*(.*)")

# Matches "### PREFIX: Title" or "### PREFIX Title" (colon optional for tolerance).
_H3_RE = re.compile(r"^###\s+(\S+?):?\s+(.*)")

# Matches a bullet list item starting with "- ".
_BULLET_RE = re.compile(r"^-\s+(.*)")

# ID patterns: R001, F001, T001 (case-insensitive for tolerance).
_REQ_ID_RE = re.compile(r"^(R\d+)\s*:?\s*(.*)", re.IGNORECASE)
_FEAT_ID_RE = re.compile(r"^(F\d{3,})", re.IGNORECASE)
_TASK_ID_RE = re.compile(r"^(T\d{3,})", re.IGNORECASE)
_ASSUMPTION_ID_RE = re.compile(
    r"^(A[0-9]{3,31})(?=\s|:|$)\s*:?\s*(.*)$",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Structured acceptance grammar (EARS / Gherkin) — T028
# ---------------------------------------------------------------------------
#
# Two optional grammars are recognised inside acceptance criteria.  Detection
# is best-effort and never raises; anything that doesn't match falls back to a
# ``freeform`` clause so the raw text is always preserved.
#
# EARS (Easy Approach to Requirements Syntax) — single-line forms:
#   "WHEN <trigger>, THE SYSTEM SHALL <response>"
#   "WHEN <trigger> THEN <response>"
#   "WHILE <state>, WHEN <trigger>, THE SYSTEM SHALL <response>"
#   "IF <condition>, THEN <response>"
#   "WHERE <feature>, THE SYSTEM SHALL <response>"
#   "THE SYSTEM SHALL <response>"            (ubiquitous form)
#
# Gherkin — single-line OR multi-line forms:
#   "Given <context> When <event> Then <outcome>"
#   (or each keyword on its own bullet line within the same criteria block)
#
# The leading optional Gherkin clause keywords (Scenario:, And, But) are
# tolerated and folded into the nearest preceding keyword's fragment.

# A leading "the system shall" / "system shall" / "it shall" response phrase.
_EARS_SHALL_RE = re.compile(
    r"\b(?:the\s+system\s+shall|the\s+system\s+must|system\s+shall|it\s+shall)\b\s*",
    re.IGNORECASE,
)
# WHEN ... [THEN | , (the system) shall] ... response.
_EARS_WHEN_RE = re.compile(r"^\s*when\b\s+(.*)$", re.IGNORECASE | re.DOTALL)
_EARS_WHILE_RE = re.compile(r"^\s*while\b\s+(.*)$", re.IGNORECASE | re.DOTALL)
_EARS_IF_RE = re.compile(r"^\s*if\b\s+(.*)$", re.IGNORECASE | re.DOTALL)
_EARS_WHERE_RE = re.compile(r"^\s*where\b\s+(.*)$", re.IGNORECASE | re.DOTALL)
# Split a trigger fragment on a THEN / ", the system shall" boundary.
_EARS_THEN_RE = re.compile(r"\bthen\b\s*", re.IGNORECASE)

# Gherkin keyword at the start of a (sub)clause.  "And"/"But" continue the
# previous keyword; "Scenario"/"Feature"/"Background" are structural noise we
# skip.
_GHERKIN_KEYWORD_RE = re.compile(
    r"^\s*(given|when|then|and|but)\b\s*:?\s*(.*)$",
    re.IGNORECASE | re.DOTALL,
)
_GHERKIN_NOISE_RE = re.compile(
    r"^\s*(scenario|feature|background|scenario\s+outline|examples)\b",
    re.IGNORECASE,
)


def _strip_html_comments(text: str) -> str:
    """Remove <!-- ... --> comments (may span multiple lines)."""
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)


def _auto_id(prefix: str, index: int) -> str:
    """Produce R001, F001, T001 style IDs."""
    return f"{prefix}{index:03d}"


def _is_default_prd(prd_id: str) -> bool:
    """True when ``prd_id`` denotes the implicit/default PRD.

    Two spellings reach the parser for the same concept: the public ``parse_prd``
    default sentinel (``DEFAULT_PARSE_PRD_ID == "prd"``, what every current
    caller passes) and the stored model id (``DEFAULT_PRD_ID == "default"``).
    Either one keeps ids BARE.
    """
    return prd_id in (DEFAULT_PARSE_PRD_ID, DEFAULT_PRD_ID)


def _normalize_id(raw_id: str, prd_id: str) -> str:
    """Resolve a raw id to its canonical form for the active ``prd_id``.

    The default PRD keeps BARE ids byte-identical to the pre-multi-PRD parser;
    any named PRD gets a ``<prd_id>:`` prefix. The rules, in order:

    * default PRD (``_is_default_prd``) → return ``raw_id`` unchanged (bare).
    * ``raw_id`` is already prefixed with ``<prd_id>:`` → return it unchanged
      (author wrote the prefix explicitly; no double-prefixing, no warning).
    * otherwise → return ``<prd_id>:<raw_id>``.

    This is what lets a bare cross-ref (``**Feature:** F001``) resolve against a
    prefixed feature id (``v0.2:F001``) within the same PRD: callers normalise
    both the minted id and the reference through this helper, so they land on
    the same canonical string.
    """
    if _is_default_prd(prd_id):
        return raw_id
    prefix = f"{prd_id}:"
    if raw_id.startswith(prefix):
        return raw_id
    return f"{prefix}{raw_id}"


def _strip_prd_prefix(raw_id: str, prd_id: str) -> str:
    """Strip a leading ``<prd_id>:`` prefix from an author-written id.

    Used so an author may write either the bare (``F001``) or the explicitly
    prefixed (``v0.2:F001``) form in a heading or cross-ref; both collapse to
    the same bare token before ``_normalize_id`` re-applies the canonical
    prefix. No-op for the default PRD.
    """
    if _is_default_prd(prd_id):
        return raw_id
    prefix = f"{prd_id}:"
    if raw_id.startswith(prefix):
        return raw_id[len(prefix):]
    return raw_id


def _model_prd_id(prd_id: str) -> str:
    """Resolve the value to store in a model's ``prd_id`` field.

    The default sentinel(s) collapse to ``DEFAULT_PRD_ID`` so the parse-time
    ``"prd"`` sentinel never leaks into persisted state; named PRDs pass through.
    """
    return DEFAULT_PRD_ID if _is_default_prd(prd_id) else prd_id


def _has_meaningful_content(body: list[str]) -> bool:
    """True when a section body has any non-blank, non-comment-only line.

    Used by `_parse_features` / `_parse_tasks` to distinguish "section is
    genuinely empty" (acceptable — emit no error) from "section has content the
    H3 parser ignored" (bug — emit ParseError so the user knows their work was
    dropped). HTML comments are already stripped upstream by
    `_strip_html_comments`; this is a belt-and-braces check.
    """
    for raw in body:
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("<!--") and stripped.endswith("-->"):
            continue
        return True
    return False


def _is_malformed_id_prefix(raw_id: str, expected_letter: str) -> bool:
    """True when raw_id looks like an attempted Fxxx/Txxx ID but is malformed.

    Catches cases like `F-DURABILITY`, `T-1`, `F_PERF`, `T.001` — where the
    user clearly meant a custom ID but didn't follow the `^[FT]\\d{3,}` format.
    Returns False for plain English headings like `Foo: bar` or `Tool: x` so
    we don't false-positive on legitimate auto-ID fallbacks.
    """
    if len(raw_id) < 2:
        return False
    if raw_id[0].upper() != expected_letter.upper():
        return False
    # Second character is a separator → user attempted a custom ID.
    return raw_id[1] in "-_."


# ---------------------------------------------------------------------------
# Section splitting
# ---------------------------------------------------------------------------


def _split_sections(lines: list[str]) -> dict[str, tuple[int, list[str]]]:
    """Split the document on ## headings.

    Returns a dict mapping normalised section name → (start_line, body_lines).
    The special key "__project__" holds the # Project heading line.
    """
    sections: dict[str, tuple[int, list[str]]] = {}
    current_name: str | None = None
    current_start: int = 0
    current_body: list[str] = []

    def _store(name: str, start: int, body: list[str]) -> None:
        # First # heading wins: the splitter is fence-blind, so a later
        # top-level line (a trailing `# Appendix` H1, or a `# comment` inside
        # a fenced code block) must not replace the PRD's title block — it
        # would hijack the extracted title or, for a fenced `# Project:`
        # line, turn a valid PRD into a parse error.
        if name == "__project__" and name in sections:
            return
        sections[name] = (start, body)

    for lineno, raw in enumerate(lines, start=1):
        if raw.startswith("# ") and not raw.startswith("## "):
            # Top-level heading — project title.
            if current_name is not None:
                _store(current_name, current_start, current_body)
            current_name = "__project__"
            current_start = lineno
            current_body = [raw]
        elif raw.startswith("## "):
            if current_name is not None:
                _store(current_name, current_start, current_body)
            heading = raw[3:].strip()
            current_name = heading.strip().lower().replace(" ", "_")
            current_start = lineno
            current_body = []
        else:
            if current_name is not None:
                current_body.append(raw)

    if current_name is not None:
        _store(current_name, current_start, current_body)

    return sections


# ---------------------------------------------------------------------------
# List extraction helpers
# ---------------------------------------------------------------------------


def _extract_bullet_list(body: list[str]) -> list[str]:
    """Return all bullet list items from a section body."""
    items: list[str] = []
    for line in body:
        m = _BULLET_RE.match(line.strip())
        if m:
            items.append(m.group(1).strip())
    return items


# ---------------------------------------------------------------------------
# Release / target-version parsing
# ---------------------------------------------------------------------------

# A "**Release:** ..." field line. The colon may sit inside (``**Release:**``)
# or outside (``**Release**:``) the bold markers, mirroring _FIELD_RE.
_RELEASE_FIELD_RE = re.compile(
    r"^\*\*\s*release\s*:?\s*\*\*\s*:?\s*(.*)$", re.IGNORECASE
)
# An inline tag qualifier on a release value: "v0.2.0 (tag: v0.2)" or
# "v0.2.0 (v0.2)". The parenthetical sets target_tag; the leading token sets
# target_version.
_RELEASE_TAG_RE = re.compile(r"^(.*?)\s*\(\s*(?:tag\s*:\s*)?([^)]+?)\s*\)\s*$")
# The top-level PRD heading: ``# Project: <Name>`` (template form) or a bare
# ``# <Name>``. The optional ``Project:`` prefix is boilerplate, not part of
# the readable name, so it is stripped from the captured title.
_PROJECT_TITLE_RE = re.compile(
    r"^#\s+(?:project\s*:\s*)?(?P<title>.*)$", re.IGNORECASE
)


def _split_release_value(value: str) -> tuple[str | None, str | None]:
    """Split a release value into ``(target_version, target_tag)``.

    Accepted forms (all round-trip into the PRD release fields):

    * ``v0.2.0``                 → version=v0.2.0, tag=None
    * ``v0.2.0 (tag: v0.2)``     → version=v0.2.0, tag=v0.2
    * ``v0.2.0 (v0.2)``          → version=v0.2.0, tag=v0.2

    An empty value yields ``(None, None)``.
    """
    value = value.strip()
    if not value:
        return None, None
    m = _RELEASE_TAG_RE.match(value)
    if m:
        version = m.group(1).strip() or None
        tag = m.group(2).strip() or None
        return version, tag
    return value, None


def _parse_release(
    sections: dict[str, tuple[int, list[str]]],
) -> tuple[str | None, str | None]:
    """Extract ``(target_version, target_tag)`` from the PRD.

    Two equivalent spellings are recognised (absent => ``(None, None)``):

    * A ``## Release`` section. The first ``**Version:**`` / ``**Tag:**`` field
      lines win; otherwise the first non-blank bullet/line is the version, and
      an inline ``(tag: ...)`` qualifier sets the tag.
    * A ``**Release:** <value>`` field line anywhere in the ## Summary section
      (the natural home for a one-line release marker).
    """
    # 1. Dedicated ## Release section.
    rel_block = sections.get("release")
    if rel_block is not None:
        version: str | None = None
        tag: str | None = None
        for raw in rel_block[1]:
            stripped = raw.strip()
            if not stripped:
                continue
            m_field = _FIELD_RE.match(stripped)
            if m_field:
                key = m_field.group(1).strip().lower().rstrip(":")
                val = m_field.group(2).strip()
                if key == "version" and val:
                    version = val
                elif key == "tag" and val:
                    tag = val
                continue
            # First non-field, non-blank line (possibly a bullet) is the value.
            if version is None and tag is None:
                m_bullet = _BULLET_RE.match(stripped)
                content = m_bullet.group(1).strip() if m_bullet else stripped
                version, tag = _split_release_value(content)
        return version, tag

    # 2. A "**Release:**" field line, conventionally under ## Summary.
    summary_block = sections.get("summary")
    if summary_block is not None:
        for raw in summary_block[1]:
            m = _RELEASE_FIELD_RE.match(raw.strip())
            if m:
                return _split_release_value(m.group(1))

    return None, None


# ---------------------------------------------------------------------------
# Requirement parsing
# ---------------------------------------------------------------------------


def _parse_requirements(
    body: list[str],
    start_line: int,
    errors: list[ParseError],
    prd_id: str,
) -> list[Requirement]:
    """Parse the ## Requirements section body into Requirement models.

    Items may be:
    - "- R001: text"  (explicit ID)
    - "- text"        (auto-assign ID)

    ``prd_id`` controls id shape: the default PRD keeps bare ids; any named PRD
    gets a ``<prd_id>:`` prefix (see ``_normalize_id``). Author-written explicit
    ids may be bare (``R001``) or already prefixed (``v0.2:R001``); both land on
    the same canonical id.
    """
    reqs: list[Requirement] = []
    auto_index = 1

    for raw in body:
        line = raw.strip()
        if not line:
            continue
        m_bullet = _BULLET_RE.match(line)
        if not m_bullet:
            continue
        content = m_bullet.group(1).strip()
        # Let an author write the id either bare or already prd-prefixed.
        content = _strip_prd_prefix(content, prd_id)
        m_id = _REQ_ID_RE.match(content)
        if m_id:
            raw_id = m_id.group(1).upper()
            text = m_id.group(2).strip()
            # Refuse suffixed ids like 'R003a' instead of silently truncating
            # to 'R003' (which collides with a sibling 'R003b' and, before the
            # duplicate-id guard below, aborted the state write mid-append —
            # bricking the workspace; see _flag_duplicate_ids).
            rest = content[m_id.end(1):]
            if rest[:1].isalpha() or rest[:1] == "_":
                suffix = rest.split(":", 1)[0].split()[0]
                errors.append(
                    ParseError(
                        section="requirements",
                        line=start_line,
                        message=(
                            f"Requirement id '{m_id.group(1)}{suffix}' is not "
                            "canonical ('R' + digits, e.g. R001). Suffixed ids "
                            "would truncate and collide — renumber it "
                            f"(e.g. split into its own RNNN id)."
                        ),
                    )
                )
                continue
        else:
            raw_id = _auto_id("R", auto_index)
            text = content

        req_id = _normalize_id(raw_id, prd_id)
        auto_index += 1

        if not text:
            errors.append(
                ParseError(
                    section="requirements",
                    line=start_line,
                    message=f"Requirement '{req_id}' has empty text — skipped.",
                )
            )
            continue

        reqs.append(
            Requirement(
                id=req_id,
                prd_id=_model_prd_id(prd_id),
                prd_section="requirements",
                text=text,
            )
        )

    return reqs


def _flag_duplicate_ids(
    items: list, kind: str, errors: list[ParseError]
) -> None:
    """Refuse duplicate ids at parse time, before any state write.

    A duplicate id that reached the state engine aborted the write transaction
    on the UNIQUE constraint AFTER the ``prd.parsed`` event line was appended
    to ``events.jsonl`` — poisoning replay so every subsequent command failed
    with the same TransactionAborted until ``anvil init --force`` (data loss).
    Reproduced 2026-07-02 with a duplicated ``R005`` bullet. Parse-time
    validation is the guard: errors here fail the parse cleanly with nothing
    written.
    """
    seen: set[str] = set()
    for item in items:
        if item.id in seen:
            errors.append(
                ParseError(
                    section=f"{kind}s",
                    line=0,
                    message=(
                        f"Duplicate {kind} id '{item.id}' — ids must be "
                        "unique; renumber one of the entries."
                    ),
                )
            )
        seen.add(item.id)


# ---------------------------------------------------------------------------
# Feature parsing (within ## Features)
# ---------------------------------------------------------------------------


def _parse_h3_blocks(
    body: list[str],
    base_line: int,
) -> list[tuple[int, str, list[str]]]:
    """Split a section body on ### headings.

    Returns list of (line_number, heading_text, block_lines).
    """
    blocks: list[tuple[int, str, list[str]]] = []
    current_heading: str | None = None
    current_start: int = base_line
    current_lines: list[str] = []

    for offset, raw in enumerate(body, start=1):
        if raw.startswith("### "):
            if current_heading is not None:
                blocks.append((current_start, current_heading, current_lines))
            current_heading = raw[4:].strip()
            current_start = base_line + offset
            current_lines = []
        else:
            if current_heading is not None:
                current_lines.append(raw)

    if current_heading is not None:
        blocks.append((current_start, current_heading, current_lines))

    return blocks


def _parse_assumptions(
    body: list[str],
    start_line: int,
    known_req_ids: set[str],
    errors: list[ParseError],
    prd_id: str,
) -> list[PRDAssumption]:
    """Parse the optional, typed ``## Assumptions`` section.

    Assumptions are intentionally small PRD records, not a second spec format:
    a stable ``A###`` id, a statement, a rationale, and optional requirement
    references. Unreferenced assumptions are global by design.
    """
    assumptions: list[PRDAssumption] = []
    if not _has_meaningful_content(body):
        return assumptions

    blocks = _parse_h3_blocks(body, start_line)
    if not blocks:
        errors.append(
            ParseError(
                section="## Assumptions",
                line=start_line,
                message=(
                    "Assumptions must use '### A001: statement' blocks with a "
                    "'**Rationale:**' field."
                ),
            )
        )
        return assumptions
    if len(blocks) > MAX_PRD_ASSUMPTIONS:
        errors.append(
            ParseError(
                section="## Assumptions",
                line=start_line,
                message=(
                    f"Assumptions are limited to {MAX_PRD_ASSUMPTIONS} records; "
                    f"found {len(blocks)}."
                ),
            )
        )
        return assumptions

    for line, heading, block in blocks:
        match = _ASSUMPTION_ID_RE.match(heading)
        if match is None:
            errors.append(
                ParseError(
                    section="## Assumptions",
                    line=line,
                    message=(
                        f"Assumption heading '{heading}' must start with a "
                        f"stable A### id no longer than "
                        f"{MAX_PRD_ASSUMPTION_ID_LENGTH} characters "
                        "(for example, '### A001: ...')."
                    ),
                )
            )
            continue
        assumption_id, statement = match.group(1).upper(), match.group(2).strip()
        if not statement:
            errors.append(
                ParseError(
                    section="## Assumptions",
                    line=line,
                    message=f"Assumption '{assumption_id}' has an empty statement.",
                )
            )
            continue
        if len(statement) > MAX_PRD_ASSUMPTION_STATEMENT_LENGTH:
            errors.append(
                ParseError(
                    section="## Assumptions",
                    line=line,
                    message=(
                        f"Assumption '{assumption_id}' statement exceeds "
                        f"{MAX_PRD_ASSUMPTION_STATEMENT_LENGTH} characters."
                    ),
                )
            )
            continue

        rationale = ""
        requirement_ids: list[str] = []
        requirements_field_seen = False
        invalid_field = False
        seen_fields: set[str] = set()
        for raw in block:
            stripped = raw.strip()
            if not stripped:
                continue
            field = _FIELD_RE.match(stripped)
            if field is None:
                errors.append(
                    ParseError(
                        section="## Assumptions",
                        line=line,
                        message=(
                            f"Assumption '{assumption_id}' has unlabelled content: "
                            f"'{stripped}'. Use typed fields only."
                        ),
                    )
                )
                invalid_field = True
                continue
            name = field.group(1).strip().lower().rstrip(":")
            value = field.group(2).strip()
            canonical_name = (
                "requirements"
                if name in {"requirements", "requirement references"}
                else name
            )
            if canonical_name in seen_fields:
                errors.append(
                    ParseError(
                        section="## Assumptions",
                        line=line,
                        message=(
                            f"Assumption '{assumption_id}' repeats the "
                            f"'**{field.group(1).strip()}**' field."
                        ),
                    )
                )
                invalid_field = True
                continue
            seen_fields.add(canonical_name)
            if name == "rationale":
                rationale = value
            elif name in {"requirements", "requirement references"}:
                requirements_field_seen = True
                requirement_ids = [
                    _normalize_id(
                        _strip_prd_prefix(token.strip(), prd_id).upper(), prd_id
                    )
                    for token in value.split(",")
                    if token.strip()
                ]
            else:
                errors.append(
                    ParseError(
                        section="## Assumptions",
                        line=line,
                        message=(
                            f"Assumption '{assumption_id}' has unknown field "
                            f"'**{field.group(1).strip()}**'. Use '**Rationale:**' "
                            "and optional '**Requirements:**'."
                        ),
                    )
                )
                invalid_field = True

        if invalid_field:
            continue
        if requirements_field_seen and not requirement_ids:
            errors.append(
                ParseError(
                    section="## Assumptions",
                    line=line,
                    message=(
                        f"Assumption '{assumption_id}' has an empty "
                        "'**Requirements:**' field; omit the field for a global "
                        "assumption."
                    ),
                )
            )
            continue

        if not rationale:
            errors.append(
                ParseError(
                    section="## Assumptions",
                    line=line,
                    message=f"Assumption '{assumption_id}' needs a '**Rationale:**' field.",
                )
            )
            continue
        if len(rationale) > MAX_PRD_ASSUMPTION_RATIONALE_LENGTH:
            errors.append(
                ParseError(
                    section="## Assumptions",
                    line=line,
                    message=(
                        f"Assumption '{assumption_id}' rationale exceeds "
                        f"{MAX_PRD_ASSUMPTION_RATIONALE_LENGTH} characters."
                    ),
                )
            )
            continue
        if len(requirement_ids) > MAX_PRD_ASSUMPTION_REQUIREMENTS:
            errors.append(
                ParseError(
                    section="## Assumptions",
                    line=line,
                    message=(
                        f"Assumption '{assumption_id}' references more than "
                        f"{MAX_PRD_ASSUMPTION_REQUIREMENTS} requirements."
                    ),
                )
            )
            continue
        unknown = sorted(set(requirement_ids) - known_req_ids)
        if unknown:
            errors.append(
                ParseError(
                    section="## Assumptions",
                    line=line,
                    message=(
                        f"Assumption '{assumption_id}' references unknown "
                        f"requirement(s): {', '.join(unknown)}."
                    ),
                )
            )
            continue
        assumptions.append(
            PRDAssumption(
                id=assumption_id,
                statement=statement,
                rationale=rationale,
                requirement_ids=requirement_ids,
            )
        )

    _flag_duplicate_ids(assumptions, "assumption", errors)
    return assumptions


def _parse_features(
    body: list[str],
    start_line: int,
    known_req_ids: set[str],
    errors: list[ParseError],
    prd_id: str,
) -> list[Feature]:
    """Parse all ### FXxx: Title blocks within ## Features.

    ``prd_id`` controls id shape (see ``_normalize_id``). Feature ids and the
    ``**Requirements:**`` cross-refs are both normalised so bare refs resolve
    against the (possibly prefixed) requirement ids minted for this PRD.
    """
    features: list[Feature] = []
    auto_index = 1
    blocks = _parse_h3_blocks(body, start_line)

    # The Features section requires '### Fxxx: Title' H3 blocks; bullets or
    # prose are silently invisible to the rest of the parser. If the body has
    # any non-empty, non-comment content but produced zero H3 blocks, the user
    # almost certainly wrote bullets — surface a loud error rather than emit
    # an empty feature list. (See template.py docstring: silent fallback is
    # explicitly rejected.)
    if not blocks and _has_meaningful_content(body):
        errors.append(
            ParseError(
                section="features",
                line=start_line,
                message=(
                    "## Features section has content but no '### Fxxx: Title' "
                    "blocks were parsed. Each feature must be a level-3 heading "
                    "(e.g. '### F001: User signup') optionally followed by a "
                    "'**Requirements:** R001, R002' line. Bullets and prose at "
                    "the section level are not parsed. See docs/prd-template.md "
                    "for the canonical format; omit the section entirely if no "
                    "features are defined."
                ),
            )
        )
        return []

    for block_line, heading, block_lines in blocks:
        m_h3 = _H3_RE.match(f"### {heading}")
        if m_h3:
            # Let the author write the id bare (F001) or prd-prefixed
            # (v0.2:F001); strip the prefix before pattern-matching so both
            # spellings hit the same branch and re-prefix uniformly below.
            raw_id = _strip_prd_prefix(m_h3.group(1), prd_id)
            title = m_h3.group(2).strip()
            if _FEAT_ID_RE.match(raw_id):
                feat_id = _normalize_id(raw_id.upper(), prd_id)
            else:
                if _is_malformed_id_prefix(raw_id, "F"):
                    errors.append(
                        ParseError(
                            section="features",
                            line=block_line,
                            message=(
                                f"Feature heading '### {heading}' uses a "
                                f"custom ID ('{raw_id}') that does not match "
                                "the required 'Fxxx' format (F + 3+ digits, "
                                "e.g. F001). Rename the heading to "
                                "'### F001: <title>' (or any `F<3+digits>:` "
                                "form) and re-run `anvil prd parse`."
                            ),
                        )
                    )
                # ID-looking prefix doesn't match pattern — treat whole heading as title.
                feat_id = _normalize_id(_auto_id("F", auto_index), prd_id)
                title = heading
        else:
            feat_id = _normalize_id(_auto_id("F", auto_index), prd_id)
            title = heading

        auto_index += 1

        # Parse field lines and description.
        req_ids: list[str] = []
        description_parts: list[str] = []
        i = 0
        while i < len(block_lines):
            raw = block_lines[i].strip()
            m_field = _FIELD_RE.match(raw)
            if m_field:
                # Strip trailing colon: **Field:** → key="field", **Field** → key="field".
                key = m_field.group(1).strip().lower().rstrip(":")
                val = m_field.group(2).strip()
                if key == "requirements":
                    # Normalise each cross-ref so a bare ``R001`` resolves
                    # against this PRD's (possibly prefixed) requirement ids.
                    req_ids = [
                        _normalize_id(_strip_prd_prefix(r.strip(), prd_id).upper(), prd_id)
                        for r in val.split(",")
                        if r.strip()
                    ]
                # Other fields on features are ignored at parse time.
            elif raw:
                description_parts.append(raw)
            i += 1

        description = " ".join(description_parts).strip()

        # Validate referenced requirement IDs (warn, don't fail).
        for rid in req_ids:
            if rid not in known_req_ids:
                errors.append(
                    ParseError(
                        section="features",
                        line=block_line,
                        message=(
                            f"Feature '{feat_id}' references unknown "
                            f"requirement '{rid}' — included anyway."
                        ),
                    )
                )

        features.append(
            Feature(
                id=feat_id,
                prd_id=_model_prd_id(prd_id),
                title=title,
                description=description,
                requirements=req_ids,
            )
        )

    return features


# ---------------------------------------------------------------------------
# Task parsing (within ## Tasks)
# ---------------------------------------------------------------------------


def _parse_claims_field(
    val: str, task_id: str, block_line: int
) -> tuple[list[TaskClaim], list[ParseError]]:
    """Parse a ``**Claims:**`` field value into TaskClaims (T002).

    Comma-separated tokens: ``id``, ``id (kind)``, or ``id (kind: subject)``.
    Unknown kinds are loud ParseErrors (not silently defaulted) — an
    evidence contract with a typo'd kind is a contract the author did not
    write.
    """
    claims: list[TaskClaim] = []
    errors: list[ParseError] = []
    for token in (t.strip() for t in val.split(",")):
        if not token:
            continue
        m = re.match(r"^(?P<id>[A-Za-z0-9_.-]+)(?:\s*\((?P<detail>[^)]*)\))?$", token)
        if not m:
            errors.append(
                ParseError(
                    section="tasks",
                    line=block_line,
                    message=(
                        f"Task '{task_id}' has a malformed **Claims:** token "
                        f"{token!r}. Expected 'id', 'id (kind)', or "
                        "'id (kind: subject)'."
                    ),
                )
            )
            continue
        kind_raw, _, subject = (m.group("detail") or "").partition(":")
        data: dict[str, str] = {"id": m.group("id")}
        if subject.strip():
            data["subject"] = subject.strip()
        if kind_raw.strip():
            data["kind"] = kind_raw.strip()
        try:
            claims.append(TaskClaim.model_validate(data))
        except Exception as exc:  # noqa: BLE001 — surfaced as a ParseError
            errors.append(
                ParseError(
                    section="tasks",
                    line=block_line,
                    message=(
                        f"Task '{task_id}' claim {token!r} is invalid: {exc}"
                    ),
                )
            )
    return claims, errors


def _parse_assertions_block(
    block_lines: list[str], start: int, task_id: str, block_line: int
) -> tuple[int, list[ArtifactAssertion], list[ParseError]]:
    """Consume the fenced YAML block after ``**Artifact assertions:**`` (T002).

    Returns ``(lines_consumed_after_field_line, assertions, errors)``.
    Loud on every malformation (missing fence, unclosed fence, invalid YAML,
    schema mismatch) — consistent with the bullets-not-parsed guard: an
    evidence contract must never be silently dropped.
    """

    def _err(message: str) -> tuple[int, list[ArtifactAssertion], list[ParseError]]:
        return 0, [], [
            ParseError(section="tasks", line=block_line, message=message)
        ]

    j = start
    while j < len(block_lines) and not block_lines[j].strip():
        j += 1
    if j >= len(block_lines) or not block_lines[j].strip().startswith("```"):
        return _err(
            f"Task '{task_id}': **Artifact assertions:** must be followed by "
            "a fenced ```yaml block. See docs/prd-template.md."
        )
    j += 1  # past the opening fence
    yaml_lines: list[str] = []
    while j < len(block_lines) and block_lines[j].strip() != "```":
        yaml_lines.append(block_lines[j])
        j += 1
    if j >= len(block_lines):
        return _err(
            f"Task '{task_id}': unclosed ```yaml block under "
            "**Artifact assertions:**."
        )
    consumed = j - start + 1  # through the closing fence

    try:
        loaded = yaml.safe_load("\n".join(yaml_lines))
    except yaml.YAMLError as exc:
        _, _, errs = _err(
            f"Task '{task_id}': invalid YAML in **Artifact assertions:** "
            f"block: {exc}"
        )
        return consumed, [], errs
    if loaded is None:
        loaded = []
    if not isinstance(loaded, list):
        _, _, errs = _err(
            f"Task '{task_id}': **Artifact assertions:** YAML must be a "
            "list of assertion entries."
        )
        return consumed, [], errs

    assertions: list[ArtifactAssertion] = []
    errors: list[ParseError] = []
    for idx, entry in enumerate(loaded):
        try:
            assertions.append(ArtifactAssertion.model_validate(entry))
        except Exception as exc:  # noqa: BLE001 — surfaced as a ParseError
            errors.append(
                ParseError(
                    section="tasks",
                    line=block_line,
                    message=(
                        f"Task '{task_id}': artifact assertion #{idx + 1} "
                        f"is invalid: {exc}"
                    ),
                )
            )
    return consumed, assertions, errors


def _parse_tasks(
    body: list[str],
    start_line: int,
    known_feat_ids: set[str],
    errors: list[ParseError],
    clock: Clock,
    prd_id: str,
) -> list[Task]:
    """Parse all ### TXxx: Title blocks within ## Tasks.

    CL-11: ``clock`` is required (not Optional with a default) so callers
    cannot accidentally regress to ``datetime.now()``. ``parse_prd`` supplies
    a ``SystemClock`` when callers do not pass one, preserving backwards
    compatibility at the public-API boundary.

    ``prd_id`` controls id shape (see ``_normalize_id``). Task ids, the
    ``**Feature:**`` cross-ref, and ``**Dependencies:**`` are all normalised so
    bare refs resolve against this PRD's (possibly prefixed) ids.
    """
    tasks: list[Task] = []
    auto_index = 1
    blocks = _parse_h3_blocks(body, start_line)
    now = clock.now()
    # task_id → block_line, so the post-loop dependency validation can
    # report the right line number on each task (greptile PR #64 fix —
    # without this map the unknown-dep warning points at the ## Tasks
    # heading instead of the offending ### Txxx: block).
    task_block_lines: dict[str, int] = {}

    # Mirror of the Features guard: a Tasks section written with bullets
    # instead of '### Txxx: Title' blocks must fail loudly rather than be
    # silently dropped.
    if not blocks and _has_meaningful_content(body):
        errors.append(
            ParseError(
                section="tasks",
                line=start_line,
                message=(
                    "## Tasks section has content but no '### Txxx: Title' "
                    "blocks were parsed. Each task must be a level-3 heading "
                    "(e.g. '### T001: Implement parser') followed by "
                    "'**Feature:** F001', '**Acceptance criteria:**', and "
                    "'**Verification:**' blocks. Bullets and prose at the "
                    "section level are not parsed. See docs/prd-template.md "
                    "for the canonical format; omit the section entirely to "
                    "let `anvil plan` generate tasks from features."
                ),
            )
        )
        return []

    for block_line, heading, block_lines in blocks:
        m_h3 = _H3_RE.match(f"### {heading}")
        if m_h3:
            # Accept a bare (T001) or prd-prefixed (v0.2:T001) heading id.
            raw_id = _strip_prd_prefix(m_h3.group(1), prd_id)
            title = m_h3.group(2).strip()
            if _TASK_ID_RE.match(raw_id):
                task_id = _normalize_id(raw_id.upper(), prd_id)
            else:
                if _is_malformed_id_prefix(raw_id, "T"):
                    errors.append(
                        ParseError(
                            section="tasks",
                            line=block_line,
                            message=(
                                f"Task heading '### {heading}' uses a "
                                f"custom ID ('{raw_id}') that does not match "
                                "the required 'Txxx' format (T + 3+ digits, "
                                "e.g. T001). Rename the heading to "
                                "'### T001: <title>' (or any `T<3+digits>:` "
                                "form) and re-run `anvil prd parse`."
                            ),
                        )
                    )
                task_id = _normalize_id(_auto_id("T", auto_index), prd_id)
                title = heading
        else:
            task_id = _normalize_id(_auto_id("T", auto_index), prd_id)
            title = heading

        auto_index += 1

        # Parse structured fields and description.
        feature_id: str = ""
        priority: TaskPriority = TaskPriority.medium
        task_type: TaskType = TaskType.feature
        likely_files: list[str] = []
        acceptance_criteria: list[str] = []
        verification_commands: list[str] = []
        dependencies: list[str] = []
        description_parts: list[str] = []
        claims: list[TaskClaim] = []
        artifact_assertions: list[ArtifactAssertion] = []

        i = 0
        in_acceptance_criteria = False
        in_verification = False

        while i < len(block_lines):
            raw = block_lines[i]
            stripped = raw.strip()

            m_field = _FIELD_RE.match(stripped)
            if m_field:
                in_acceptance_criteria = False
                in_verification = False
                # Strip trailing colon from field name to normalise
                # "**Feature:**" and "**Feature**" to the same key.
                key = m_field.group(1).strip().lower().rstrip(":").replace(" ", "_")
                val = m_field.group(2).strip()

                if key == "feature":
                    # Normalise so a bare ``F001`` cross-ref resolves against
                    # this PRD's (possibly prefixed) feature ids.
                    feature_id = _normalize_id(
                        _strip_prd_prefix(val, prd_id).upper(), prd_id
                    )
                elif key == "priority":
                    try:
                        priority = TaskPriority(val.lower())
                    except ValueError:
                        errors.append(
                            ParseError(
                                section="tasks",
                                line=block_line,
                                message=(
                                    f"Task '{task_id}' has unknown priority "
                                    f"'{val}' — defaulting to 'medium'."
                                ),
                            )
                        )
                elif key == "type":
                    # T015: non-feature task types. Unknown values fall back
                    # to 'feature' with a warning rather than aborting the
                    # parse — same forgiving policy as **Priority:**.
                    try:
                        task_type = TaskType(val.lower())
                    except ValueError:
                        errors.append(
                            ParseError(
                                section="tasks",
                                line=block_line,
                                message=(
                                    f"Task '{task_id}' has unknown type "
                                    f"'{val}' — defaulting to 'feature'. "
                                    "Valid types: feature, bugfix, refactor, "
                                    "modify."
                                ),
                            )
                        )
                elif key == "likely_files":
                    likely_files = [f.strip() for f in val.split(",") if f.strip()]
                elif key == "dependencies":
                    # v1.16.0 — explicit semantic dependencies. Comma-separated
                    # TaskIDs (e.g. "T001, T002"). Normalised to upper-case and
                    # to this PRD's id shape so a bare ``T002`` ref resolves
                    # against a prefixed task id (``v0.2:T002``).
                    # Unknown-ID validation happens in a post-loop pass at the
                    # end of _parse_tasks once every task ID in this section
                    # has been collected (allows forward refs within the same
                    # ## Tasks section).
                    dependencies = [
                        _normalize_id(_strip_prd_prefix(d.strip(), prd_id).upper(), prd_id)
                        for d in val.split(",")
                        if d.strip()
                    ]
                elif key == "acceptance_criteria":
                    in_acceptance_criteria = True
                    if val:
                        acceptance_criteria.append(val)
                elif key == "verification":
                    in_verification = True
                    if val:
                        verification_commands.append(val.strip("`"))
                elif key == "claims":
                    # Evidence contracts (T002): comma-separated claim tokens —
                    # ``id``, ``id (kind)``, or ``id (kind: subject)``.
                    claims_parsed, claim_errs = _parse_claims_field(
                        val, task_id, block_line
                    )
                    claims.extend(claims_parsed)
                    errors.extend(claim_errs)
                elif key == "artifact_assertions":
                    # Evidence contracts (T002): the field line is followed by a
                    # fenced ```yaml block; consume it here so it never leaks
                    # into the description prose.
                    consumed, parsed, block_errs = _parse_assertions_block(
                        block_lines, i + 1, task_id, block_line
                    )
                    artifact_assertions.extend(parsed)
                    errors.extend(block_errs)
                    i += consumed
            elif stripped.startswith("- ") and in_acceptance_criteria:
                m = _BULLET_RE.match(stripped)
                if m:
                    acceptance_criteria.append(m.group(1).strip())
            elif stripped.startswith("- ") and in_verification:
                m = _BULLET_RE.match(stripped)
                if m:
                    verification_commands.append(m.group(1).strip().strip("`"))
            elif stripped.startswith("- "):
                # Bullet not under a known field — treat as description.
                in_acceptance_criteria = False
                in_verification = False
                description_parts.append(stripped)
            elif stripped:
                in_acceptance_criteria = False
                in_verification = False
                description_parts.append(stripped)

            i += 1

        description = " ".join(description_parts).strip()

        # Evidence contracts (T002, review finding): an assertion bound to an
        # undeclared claim id is a dangling reference - loud, never silent,
        # in a feature whose premise is "no silent gaps".
        declared_claim_ids = {c.id for c in claims}
        for assertion in artifact_assertions:
            if assertion.claim and assertion.claim not in declared_claim_ids:
                errors.append(
                    ParseError(
                        section="tasks",
                        line=block_line,
                        message=(
                            f"Task {task_id!r}: artifact assertion for "
                            f"{assertion.artifact!r} references claim "
                            f"{assertion.claim!r}, which is not declared "
                            "in **Claims:**."
                        ),
                    )
                )

        if not feature_id:
            errors.append(
                ParseError(
                    section="tasks",
                    line=block_line,
                    message=(
                        f"Task '{task_id}' has no **Feature:** field — "
                        "feature_id will be empty."
                    ),
                )
            )

        if feature_id and feature_id not in known_feat_ids:
            errors.append(
                ParseError(
                    section="tasks",
                    line=block_line,
                    message=(
                        f"Task '{task_id}' references unknown feature "
                        f"'{feature_id}' — included anyway."
                    ),
                )
            )

        tasks.append(
            Task(
                id=task_id,
                prd_id=_model_prd_id(prd_id),
                feature_id=feature_id,
                title=title,
                description=description,
                status=TaskStatus.proposed,
                priority=priority,
                task_type=task_type,
                scores=Score(),
                acceptance_criteria=acceptance_criteria,
                claims=claims,
                verification=Verification(
                    commands=verification_commands,
                    artifact_assertions=artifact_assertions,
                    # SL-3 / B48: turn each verification command into a typed
                    # requirement — the task is accepted only once a CommandProof
                    # records the command exiting 0 (captured by the run hooks;
                    # authenticity rests on a trusted hook writer).
                    required_proofs=[
                        ProofRequirement(
                            kind=ProofKind.command,
                            command=cmd,
                            passing_exit_codes=[0],
                            label=f"`{cmd}` exits 0",
                        )
                        for cmd in verification_commands
                    ],
                ),
                likely_files=likely_files,
                dependencies=dependencies,
                created_at=now,
                updated_at=now,
            )
        )
        task_block_lines[task_id] = block_line

    # Post-loop: validate dependency references. Two distinct failure modes:
    # 1. Self-dependency (T001 with **Dependencies:** T001) — at claim time
    #    this creates a perpetual spurious warning because T001 will never
    #    be `done` before T001 is claimed. Strip the self-ref from the
    #    task's dependencies AND emit a warning so the author sees it.
    # 2. Unknown-task dependency (**Dependencies:** T099 when T099 doesn't
    #    exist in this ## Tasks section). Warn but keep the dep so
    #    downstream tooling can see the author's intent.
    # Both warnings carry the per-task block line (greptile PR #64 fix).
    known_task_ids = {t.id for t in tasks}
    for task in tasks:
        block_line = task_block_lines.get(task.id, start_line)
        cleaned_deps: list[str] = []
        for dep_id in task.dependencies:
            if dep_id == task.id:
                errors.append(
                    ParseError(
                        section="tasks",
                        line=block_line,
                        message=(
                            f"Task '{task.id}' lists itself in "
                            "**Dependencies:** — self-references are "
                            "stripped (would otherwise create a perpetual "
                            "claim-time warning, since a task can never be "
                            "`done` before being claimed). Remove "
                            f"'{task.id}' from {task.id}'s **Dependencies:** "
                            "field."
                        ),
                    )
                )
                continue  # skip — do NOT re-add the self-ref to cleaned_deps
            if dep_id not in known_task_ids:
                errors.append(
                    ParseError(
                        section="tasks",
                        line=block_line,
                        message=(
                            f"Task '{task.id}' depends on unknown task "
                            f"'{dep_id}' — included anyway. Either add a "
                            f"### {dep_id}: block to ## Tasks, or remove "
                            f"'{dep_id}' from {task.id}'s **Dependencies:** "
                            "field."
                        ),
                    )
                )
            cleaned_deps.append(dep_id)
        # Replace the task in-place when self-refs were stripped (Pydantic
        # Task is immutable; model_copy + index replace).
        if cleaned_deps != task.dependencies:
            idx = tasks.index(task)
            tasks[idx] = task.model_copy(update={"dependencies": cleaned_deps})

    return tasks


# ---------------------------------------------------------------------------
# Structured acceptance grammar parsing (T028) — pure, additive, never raises
# ---------------------------------------------------------------------------


def _freeform_clause(text: str) -> AcceptanceClause:
    """Wrap a criterion as a freeform clause (no structured grammar)."""
    return AcceptanceClause(text=text.strip(), kind="freeform", clauses={})


def _split_then(fragment: str) -> tuple[str, str | None]:
    """Split an EARS trigger on a THEN / ", the system shall" boundary.

    Returns ``(trigger, response_or_None)``.  A trailing comma on the trigger
    is trimmed.  ``response`` is ``None`` when no boundary is found.
    """
    # Prefer an explicit "shall" boundary (most specific EARS form), then a
    # bare "then" keyword.
    m_shall = _EARS_SHALL_RE.search(fragment)
    if m_shall:
        trigger = fragment[: m_shall.start()].rstrip().rstrip(",").strip()
        response = fragment[m_shall.end():].strip()
        return trigger, response or None
    m_then = _EARS_THEN_RE.search(fragment)
    if m_then:
        trigger = fragment[: m_then.start()].rstrip().rstrip(",").strip()
        response = fragment[m_then.end():].strip()
        return trigger, response or None
    return fragment.strip().rstrip(",").strip(), None


def _parse_ears(text: str) -> AcceptanceClause | None:
    """Parse a single-line EARS criterion, or return None if it isn't EARS.

    Recognises the WHEN / WHILE / IF / WHERE preconditions plus the
    ubiquitous "the system shall" form.  Requires a response clause (a THEN or
    a "shall") for the conditional forms so that an ordinary sentence merely
    *starting* with "When" but lacking a response is not misclassified.
    """
    stripped = text.strip()

    # Conditional forms must yield a response to count as EARS.
    for keyword, regex in (
        ("while", _EARS_WHILE_RE),
        ("if", _EARS_IF_RE),
        ("when", _EARS_WHEN_RE),
        ("where", _EARS_WHERE_RE),
    ):
        m = regex.match(stripped)
        if not m:
            continue
        rest = m.group(1)
        trigger, response = _split_then(rest)

        # WHILE/WHERE often nest a WHEN: "WHILE x, WHEN y, the system shall z".
        clauses: dict[str, str] = {}
        nested_when = re.search(r"\bwhen\b\s*", trigger, re.IGNORECASE)
        if keyword in ("while", "where") and nested_when:
            inner = trigger[nested_when.end():]
            inner_trigger, inner_response = _split_then(inner)
            # The pre-WHEN portion is the precondition for the keyword.
            pre = trigger[: nested_when.start()].rstrip().rstrip(",").strip()
            clauses[keyword] = pre
            clauses["when"] = inner_trigger
            if inner_response is not None:
                response = inner_response
        else:
            clauses[keyword] = trigger

        if response is None:
            # No response clause → not a well-formed EARS requirement.
            return None
        clauses["then"] = response
        return AcceptanceClause(text=stripped, kind="ears", clauses=clauses)

    # Ubiquitous form: "THE SYSTEM SHALL <response>" with no precondition.
    m_shall = _EARS_SHALL_RE.match(stripped)
    if m_shall:
        response = stripped[m_shall.end():].strip()
        if response:
            return AcceptanceClause(
                text=stripped,
                kind="ears",
                clauses={"shall": response, "then": response},
            )

    return None


def _parse_gherkin_inline(text: str) -> AcceptanceClause | None:
    """Parse a single-line "Given ... When ... Then ..." criterion.

    Returns None unless the text *starts* with a Gherkin keyword (Given / When)
    and contains at least a When and a Then (the minimal viable scenario).
    Anchoring on the leading keyword keeps ordinary prose that merely contains
    the words "when"/"then" mid-sentence out of the structured bucket — that
    prose falls back to a freeform clause.
    """
    if not _GHERKIN_KEYWORD_RE.match(text) or re.match(
        r"^\s*(?:and|but)\b", text, re.IGNORECASE
    ):
        # Must lead with given/when/then (not a bare And/But continuation).
        return None
    if not re.match(r"^\s*(?:given|when)\b", text, re.IGNORECASE):
        return None
    lowered = text.lower()
    # Find keyword positions in order of appearance.
    positions: list[tuple[int, str]] = []
    for kw in ("given", "when", "then", "and", "but"):
        for m in re.finditer(rf"\b{kw}\b", lowered):
            positions.append((m.start(), kw))
    if not positions:
        return None
    positions.sort()

    clauses: dict[str, str] = {}
    last_real_kw: str | None = None
    for idx, (start, kw) in enumerate(positions):
        end = positions[idx + 1][0] if idx + 1 < len(positions) else len(text)
        # Slice the fragment after the keyword token itself.
        frag = text[start:end]
        # Drop the leading keyword word from the fragment.
        frag = re.sub(rf"^\s*{kw}\b\s*:?\s*", "", frag, flags=re.IGNORECASE).strip()
        if kw in ("and", "but"):
            target = last_real_kw
            if target is None:
                continue
            if frag:
                clauses[target] = (clauses.get(target, "") + " and " + frag).strip()
            continue
        last_real_kw = kw
        if frag:
            clauses[kw] = frag

    if "when" in clauses and "then" in clauses:
        return AcceptanceClause(text=text.strip(), kind="gherkin", clauses=clauses)
    return None


def parse_acceptance_grammar(criteria: list[str]) -> list[AcceptanceClause]:
    """Decompose acceptance criteria into structured EARS/Gherkin clauses.

    Pure and total — never raises, never performs I/O.  For each criterion the
    parser tries, in order:

    1. EARS single-line ("WHEN ... THEN ...", "THE SYSTEM SHALL ...", etc.).
    2. Gherkin single-line ("Given ... When ... Then ...").
    3. Multi-line Gherkin: consecutive criteria written as separate
       Given/When/Then bullets are merged into one ``gherkin`` clause.
    4. Freeform fallback — the criterion is preserved verbatim.

    This is *additive*: the canonical ``Task.acceptance_criteria`` list of raw
    strings is unchanged.  Callers that want structured intent (e.g. richer
    scoring or planning) opt in by calling this helper; everything else keeps
    working exactly as before.

    Args:
        criteria: The raw acceptance-criteria strings from a parsed Task / PRD.

    Returns:
        One ``AcceptanceClause`` per logical criterion.  Multi-line Gherkin
        blocks collapse several input strings into a single clause, so the
        output length may be shorter than the input length.
    """
    if not criteria:
        return []

    out: list[AcceptanceClause] = []
    i = 0
    n = len(criteria)
    while i < n:
        raw = criteria[i]
        text = raw.strip()
        if not text:
            i += 1
            continue

        # Structural Gherkin noise lines (Scenario:, Feature:) are skipped —
        # they carry no acceptance content of their own.
        if _GHERKIN_NOISE_RE.match(text):
            i += 1
            continue

        ears = _parse_ears(text)
        if ears is not None:
            out.append(ears)
            i += 1
            continue

        inline_gherkin = _parse_gherkin_inline(text)
        if inline_gherkin is not None:
            out.append(inline_gherkin)
            i += 1
            continue

        # Multi-line Gherkin: a "Given"/"When" bullet followed by sibling
        # And/But/When/Then bullets.  Greedily consume the contiguous run.
        m_kw = _GHERKIN_KEYWORD_RE.match(text)
        if m_kw and m_kw.group(1).lower() in ("given", "when"):
            block: list[str] = [text]
            seen_then = False
            j = i + 1
            while j < n:
                nxt = criteria[j].strip()
                if not nxt:
                    j += 1
                    continue
                nm = _GHERKIN_KEYWORD_RE.match(nxt)
                next_keyword = nm.group(1).lower() if nm else None
                if next_keyword in ("given", "when") and seen_then:
                    break
                if next_keyword == "given":
                    break
                if next_keyword in ("when", "then", "and", "but"):
                    block.append(nxt)
                    seen_then = seen_then or next_keyword == "then"
                    j += 1
                    continue
                break
            merged = _parse_gherkin_inline(" ".join(block))
            if merged is not None:
                # Preserve the original multi-line text joined by newlines.
                out.append(
                    AcceptanceClause(
                        text="\n".join(block),
                        kind="gherkin",
                        clauses=merged.clauses,
                    )
                )
                i = j
                continue

        out.append(_freeform_clause(text))
        i += 1

    return out


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def parse_prd(
    markdown: str,
    *,
    prd_id: str = DEFAULT_PARSE_PRD_ID,
    provider: LLMProvider | None = None,
    clock: Clock | None = None,
) -> ParseResult:
    """Parse a structured markdown PRD into Pydantic models.

    Args:
        markdown: The full PRD markdown source.
        prd_id:   Identity of the PRD being parsed. Load-bearing: the default
                  PRD (``DEFAULT_PARSE_PRD_ID`` / ``DEFAULT_PRD_ID``) keeps BARE
                  ids (``T001``, ``F001``, ``R001``) byte-identical to the
                  pre-multi-PRD parser; any named PRD (e.g. ``"v0.2"``) prefixes
                  every minted id with ``<prd_id>:`` (``v0.2:T001``) and stamps
                  ``Requirement/Feature/Task.prd_id``. Author-written ids may be
                  bare or already prefixed; bare cross-refs resolve within the
                  same PRD via ``_normalize_id``.
        provider: Optional LLM provider used to enrich short Task descriptions
                  (Phase 7 Wave 2).  Pure-deterministic when ``None``.
        clock:    Optional Clock used to stamp ``Task.created_at`` /
                  ``updated_at``. Defaults to ``SystemClock()`` for backwards
                  compatibility. CL-11: the parser used to call
                  ``datetime.now()`` directly, bypassing the project's Clock
                  abstraction and forcing tests into monkeypatch territory.

    Returns:
        A ParseResult containing the parsed PRD, Requirements, Features, and
        Tasks, plus any ParseError instances.  Never raises.

    Design:
        - Errors are surfaced in ParseResult.errors, never swallowed.
        - Missing optional sections produce empty lists.
        - Missing required sections (# Project, ## Summary, ## Goals,
          ## Requirements) produce ParseError entries.
        - IDs are auto-assigned when absent.
        - HTML comments are stripped before parsing.
        - LLM augmentation is additive: deterministic parse runs first, then
          each Task with a short description (<50 chars) gets an enrichment
          pass.  LLM failures fall back to the deterministic description with
          a warning to stderr — they NEVER abort the parse.
    """
    if clock is None:
        # Default to SystemClock at call time so existing callers don't have
        # to thread a clock through. Tests inject FrozenClock explicitly.
        from anvil.clock import SystemClock
        clock = SystemClock()

    errors: list[ParseError] = []

    # --- Pre-processing --------------------------------------------------
    # HTML comments are stripped here so the LLM augmentation pass at the
    # bottom of this function sees the cleaned text — never the raw PRD
    # markup with comments inside it.
    cleaned = _strip_html_comments(markdown)
    lines = cleaned.splitlines()

    sections = _split_sections(lines)

    # --- Required: # Project heading ------------------------------------
    # The heading names the PRD: ``# Project: <Name>`` (the template form) or a
    # bare ``# <Name>``. The extracted name becomes ``PRD.title`` — the
    # canonical human-readable label that ``prd list`` and API consumers
    # surface. A heading that yields an empty name is a parse error, so a
    # parse-persisted PRD always carries a non-empty title.
    title = ""
    proj_block = sections.get("__project__")
    if proj_block is None:
        errors.append(
            ParseError(
                section="# Project",
                line=0,
                message="Missing required '# Project: <Name>' heading.",
            )
        )
    else:
        proj_line = proj_block[1][0] if proj_block[1] else ""
        m_title = _PROJECT_TITLE_RE.match(proj_line.strip())
        title = m_title.group("title").strip() if m_title else ""
        if not title:
            errors.append(
                ParseError(
                    section="# Project",
                    line=proj_block[0],
                    message=(
                        "Could not extract project name from heading "
                        f"'{proj_line.strip()}'."
                    ),
                )
            )

    # --- Required: ## Summary -------------------------------------------
    summary = ""
    summary_block = sections.get("summary")
    if summary_block is None:
        errors.append(
            ParseError(
                section="## Summary",
                line=0,
                message="Missing required '## Summary' section.",
            )
        )
    else:
        # A "**Release:** ..." marker may live inside ## Summary; it is pulled
        # out into the PRD release fields below, so exclude it from the prose.
        summary = " ".join(
            line.strip()
            for line in summary_block[1]
            if line.strip() and not _RELEASE_FIELD_RE.match(line.strip())
        ).strip()

    # --- Required: ## Goals ---------------------------------------------
    goals: list[str] = []
    goals_block = sections.get("goals")
    if goals_block is None:
        errors.append(
            ParseError(
                section="## Goals",
                line=0,
                message="Missing required '## Goals' section.",
            )
        )
    else:
        goals = _extract_bullet_list(goals_block[1])

    # --- Optional: ## Non-Goals -----------------------------------------
    non_goals: list[str] = []
    non_goals_block = sections.get("non-goals") or sections.get("non_goals")
    if non_goals_block is not None:
        non_goals = _extract_bullet_list(non_goals_block[1])

    # --- Required: ## Requirements --------------------------------------
    requirements: list[Requirement] = []
    req_block = sections.get("requirements")
    if req_block is None:
        errors.append(
            ParseError(
                section="## Requirements",
                line=0,
                message="Missing required '## Requirements' section.",
            )
        )
    else:
        requirements = _parse_requirements(
            req_block[1], req_block[0], errors, prd_id
        )

    known_req_ids = {r.id for r in requirements}

    # --- Optional: ## Acceptance Criteria --------------------------------
    acceptance_criteria: list[str] = []
    ac_block = sections.get("acceptance_criteria")
    if ac_block is not None:
        acceptance_criteria = _extract_bullet_list(ac_block[1])

    # --- Optional: ## Risks ---------------------------------------------
    risks: list[str] = []
    risks_block = sections.get("risks")
    if risks_block is not None:
        risks = _extract_bullet_list(risks_block[1])

    # --- Optional: ## Open Questions ------------------------------------
    open_questions: list[str] = []
    oq_block = sections.get("open_questions")
    if oq_block is not None:
        open_questions = _extract_bullet_list(oq_block[1])

    # --- Optional: ## Assumptions ---------------------------------------
    assumptions: list[PRDAssumption] = []
    assumptions_block = sections.get("assumptions")
    if assumptions_block is not None:
        assumptions = _parse_assumptions(
            assumptions_block[1],
            assumptions_block[0],
            known_req_ids,
            errors,
            prd_id,
        )

    # --- Optional: Release marker ---------------------------------------
    # A "**Release:**" line (in ## Summary) or a dedicated "## Release" section
    # round-trips into PRD.target_version / PRD.target_tag; absent => None/None.
    target_version, target_tag = _parse_release(sections)

    # --- Build PRD model ------------------------------------------------
    prd = PRD(
        id=_model_prd_id(prd_id),
        title=title,
        target_version=target_version,
        target_tag=target_tag,
        summary=summary,
        goals=goals,
        non_goals=non_goals,
        requirements=[r.id for r in requirements],
        acceptance_criteria=acceptance_criteria,
        risks=risks,
        open_questions=open_questions,
        assumptions=assumptions,
    )

    # --- Optional: ## Features ------------------------------------------
    features: list[Feature] = []
    feat_block = sections.get("features")
    if feat_block is not None:
        features = _parse_features(
            feat_block[1], feat_block[0], known_req_ids, errors, prd_id
        )

    known_feat_ids = {f.id for f in features}

    # --- Optional: ## Tasks ---------------------------------------------
    tasks: list[Task] = []
    task_block = sections.get("tasks")
    if task_block is not None:
        tasks = _parse_tasks(
            task_block[1], task_block[0], known_feat_ids, errors, clock, prd_id
        )

    # --- Link task IDs back onto their Features -------------------------
    for task in tasks:
        for feat in features:
            if feat.id == task.feature_id and task.id not in feat.tasks:
                feat.tasks.append(task.id)

    # --- Optional: LLM enrichment of short task descriptions ------------
    if provider is not None:
        tasks = _augment_short_descriptions(tasks, provider)

    # --- Uniqueness gate: refuse duplicate ids before ANY state write ----
    _flag_duplicate_ids(requirements, "requirement", errors)
    _flag_duplicate_ids(features, "feature", errors)
    _flag_duplicate_ids(tasks, "task", errors)

    return ParseResult(
        prd=prd,
        requirements=requirements,
        features=features,
        tasks=tasks,
        errors=errors,
    )


# ---------------------------------------------------------------------------
# LLM augmentation — additive enrichment of short Task descriptions
# ---------------------------------------------------------------------------


def _augment_short_descriptions(
    tasks: list[Task],
    provider: LLMProvider,
) -> list[Task]:
    """Return a new task list where short descriptions are LLM-enriched.

    A task qualifies for enrichment when ``len(description) < 50``.  The
    requirement-style prompt is built from the task's ``title`` (acts as the
    one-line requirement) and current short description.  Failures fall back
    to the deterministic description with a stderr warning — never raise.
    """
    # Local import to keep the optional LLM dep out of the main import graph.
    from anvil.planning.llm import LLMProviderError

    enriched: list[Task] = []
    for task in tasks:
        if len(task.description) >= _DESCRIPTION_SHORT_THRESHOLD:
            enriched.append(task)
            continue

        user_payload = (
            f"Requirement: {task.title}\n"
            f"Existing short description: {task.description!r}"
        )
        try:
            response = provider.generate(
                system=_DESCRIPTION_ENRICH_SYSTEM_PROMPT,
                user=user_payload,
                max_tokens=_DESCRIPTION_ENRICH_MAX_TOKENS,
            )
        except LLMProviderError as exc:
            print(
                f"warning: LLM enrichment of {task.id} description failed "
                f"({exc}); keeping deterministic description.",
                file=sys.stderr,
            )
            enriched.append(task)
            continue
        except Exception as exc:  # noqa: BLE001 — Phase 7 contract: LLM never aborts
            # Non-conforming custom provider; preserve the deterministic baseline.
            print(
                f"warning: LLM enrichment of {task.id} raised non-conforming "
                f"{type(exc).__name__}: {exc}; keeping deterministic description.",
                file=sys.stderr,
            )
            enriched.append(task)
            continue

        new_text = response.text.strip()
        if not new_text:
            enriched.append(task)
            continue

        enriched.append(task.model_copy(update={"description": new_text}))

    return enriched
