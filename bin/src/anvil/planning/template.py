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

from anvil.state.models import (
    PRD,
    Feature,
    ProofKind,
    ProofRequirement,
    Requirement,
    Score,
    Task,
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

    for lineno, raw in enumerate(lines, start=1):
        if raw.startswith("# ") and not raw.startswith("## "):
            # Top-level heading — project title.
            if current_name is not None:
                sections[current_name] = (current_start, current_body)
            current_name = "__project__"
            current_start = lineno
            current_body = [raw]
        elif raw.startswith("## "):
            if current_name is not None:
                sections[current_name] = (current_start, current_body)
            heading = raw[3:].strip()
            current_name = heading.strip().lower().replace(" ", "_")
            current_start = lineno
            current_body = []
        else:
            if current_name is not None:
                current_body.append(raw)

    if current_name is not None:
        sections[current_name] = (current_start, current_body)

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
# Requirement parsing
# ---------------------------------------------------------------------------


def _parse_requirements(
    body: list[str],
    start_line: int,
    errors: list[ParseError],
) -> list[Requirement]:
    """Parse the ## Requirements section body into Requirement models.

    Items may be:
    - "- R001: text"  (explicit ID)
    - "- text"        (auto-assign ID)
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
        m_id = _REQ_ID_RE.match(content)
        if m_id:
            req_id = m_id.group(1).upper()
            text = m_id.group(2).strip()
        else:
            req_id = _auto_id("R", auto_index)
            text = content

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
                prd_section="requirements",
                text=text,
            )
        )

    return reqs


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


def _parse_features(
    body: list[str],
    start_line: int,
    known_req_ids: set[str],
    errors: list[ParseError],
) -> list[Feature]:
    """Parse all ### FXxx: Title blocks within ## Features."""
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
            raw_id = m_h3.group(1)
            title = m_h3.group(2).strip()
            if _FEAT_ID_RE.match(raw_id):
                feat_id = raw_id.upper()
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
                feat_id = _auto_id("F", auto_index)
                title = heading
        else:
            feat_id = _auto_id("F", auto_index)
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
                    req_ids = [r.strip().upper() for r in val.split(",") if r.strip()]
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
                title=title,
                description=description,
                requirements=req_ids,
            )
        )

    return features


# ---------------------------------------------------------------------------
# Task parsing (within ## Tasks)
# ---------------------------------------------------------------------------


def _parse_tasks(
    body: list[str],
    start_line: int,
    known_feat_ids: set[str],
    errors: list[ParseError],
    clock: Clock,
) -> list[Task]:
    """Parse all ### TXxx: Title blocks within ## Tasks.

    CL-11: ``clock`` is required (not Optional with a default) so callers
    cannot accidentally regress to ``datetime.now()``. ``parse_prd`` supplies
    a ``SystemClock`` when callers do not pass one, preserving backwards
    compatibility at the public-API boundary.
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
            raw_id = m_h3.group(1)
            title = m_h3.group(2).strip()
            if _TASK_ID_RE.match(raw_id):
                task_id = raw_id.upper()
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
                task_id = _auto_id("T", auto_index)
                title = heading
        else:
            task_id = _auto_id("T", auto_index)
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
                    feature_id = val.upper()
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
                    # TaskIDs (e.g. "T001, T002"). Normalised to upper-case.
                    # Unknown-ID validation happens in a post-loop pass at the
                    # end of _parse_tasks once every task ID in this section
                    # has been collected (allows forward refs within the same
                    # ## Tasks section).
                    dependencies = [
                        d.strip().upper()
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
                feature_id=feature_id,
                title=title,
                description=description,
                status=TaskStatus.proposed,
                priority=priority,
                task_type=task_type,
                scores=Score(),
                acceptance_criteria=acceptance_criteria,
                verification=Verification(
                    commands=verification_commands,
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
            j = i + 1
            while j < n:
                nxt = criteria[j].strip()
                if not nxt:
                    j += 1
                    continue
                nm = _GHERKIN_KEYWORD_RE.match(nxt)
                if nm and nm.group(1).lower() in ("given", "when", "then", "and", "but"):
                    block.append(nxt)
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
    prd_id: str = "prd",  # noqa: ARG001 — reserved for future multi-PRD support
    provider: LLMProvider | None = None,
    clock: Clock | None = None,
) -> ParseResult:
    """Parse a structured markdown PRD into Pydantic models.

    Args:
        markdown: The full PRD markdown source.
        prd_id:   Reserved for future multi-PRD support. Currently accepted
                  for API stability but not surfaced anywhere downstream
                  (not threaded into ParseError messages or any other path).
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
    # The project name lives in the heading but is not stored on PRD (which has
    # no name field).  We validate its presence and emit an error if absent.
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
        if not re.match(r"^#\s+\S", proj_line.strip()):
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
        summary = " ".join(
            line.strip()
            for line in summary_block[1]
            if line.strip()
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
            req_block[1], req_block[0], errors
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

    # --- Build PRD model ------------------------------------------------
    prd = PRD(
        summary=summary,
        goals=goals,
        non_goals=non_goals,
        requirements=[r.id for r in requirements],
        acceptance_criteria=acceptance_criteria,
        risks=risks,
        open_questions=open_questions,
    )

    # --- Optional: ## Features ------------------------------------------
    features: list[Feature] = []
    feat_block = sections.get("features")
    if feat_block is not None:
        features = _parse_features(
            feat_block[1], feat_block[0], known_req_ids, errors
        )

    known_feat_ids = {f.id for f in features}

    # --- Optional: ## Tasks ---------------------------------------------
    tasks: list[Task] = []
    task_block = sections.get("tasks")
    if task_block is not None:
        tasks = _parse_tasks(
            task_block[1], task_block[0], known_feat_ids, errors, clock
        )

    # --- Link task IDs back onto their Features -------------------------
    for task in tasks:
        for feat in features:
            if feat.id == task.feature_id and task.id not in feat.tasks:
                feat.tasks.append(task.id)

    # --- Optional: LLM enrichment of short task descriptions ------------
    if provider is not None:
        tasks = _augment_short_descriptions(tasks, provider)

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
