"""Unresolved-decision detection for fakoli-state PRDs.

Scans a parsed PRD and its raw markdown source for items that need a human
decision before downstream work (planning, scoring, claiming) can produce
trustworthy output. Returns a flat list of `UnresolvedDecision` records that
the `resolve-decisions` skill drives as Q&A turns with the user.

Three kinds of unresolved items are detected:

1. **`needs_decision`** — inline `[NEEDS DECISION]` markers anywhere in the
   raw markdown. The marker may carry a short question after a colon, e.g.
   `[NEEDS DECISION: which serialization format?]`. Detection happens against
   the raw markdown (not the parsed model) because parsed bullets normalise
   away the marker position.

2. **`open_question`** — items under the `## Open Questions` section that
   are not the explicit "none identified" placeholder. Each becomes one
   unresolved decision the agent can drive Q&A on.

3. **`missing_field`** — task-level fields the review gate requires
   (`acceptance_criteria`, `verification.commands`) that are empty. Surfacing
   these as decisions means the agent can drive the user to fill them
   conversationally rather than handing them a "this task is blocked, go
   edit the PRD" message.

The module is pure — no I/O, no backend access. CLI and MCP both call it
on a parse result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fakoli_state.state.models import PRD, Feature, Requirement, Task

__all__ = [
    "DecisionKind",
    "UnresolvedDecision",
    "find_unresolved_decisions",
]


class DecisionKind(str, Enum):
    """Three categories of unresolved PRD items the resolver can drive Q&A on."""

    needs_decision = "needs_decision"
    open_question = "open_question"
    missing_field = "missing_field"


@dataclass(frozen=True)
class UnresolvedDecision:
    """One item the agent should drive a Q&A turn on.

    Attributes:
        id: Stable identifier so multiple resolution passes can correlate the
            same decision across re-parses. Format depends on kind:
            ``ND-001`` / ``OQ001`` / ``MF-T001-AC``.
        kind: Which detection rule produced this entry.
        location: Human-readable position (e.g. ``"## Open Questions item 3"``
            or ``"R007 (requirement)"`` or ``"T012 acceptance criteria"``).
            Used in agent-facing prompts and in resolution rewriting.
        text: The raw text of the question/marker. For `needs_decision` this
            is the question after the colon (or empty if no colon). For
            `open_question` it is the bullet text. For `missing_field` it is
            a synthesised description like "Acceptance criteria is empty".
        context_paragraph: Surrounding prose to help the agent propose
            concrete options without re-reading the whole PRD. Typically the
            paragraph that contains the marker, or the requirement/task
            description.
        suggested_resolution_field: Hint to the agent (and to the resolver
            skill) about where to write the answer. For `needs_decision`,
            "inline rewrite". For `open_question`, "move to ## Decisions".
            For `missing_field`, the target field name (e.g.
            "T012.acceptance_criteria").
    """

    id: str
    kind: DecisionKind
    location: str
    text: str
    context_paragraph: str
    suggested_resolution_field: str


# Inline `[NEEDS DECISION]` marker. Optional `: <question>` payload captured
# in group(1). The marker is intentionally case-sensitive — agents and users
# both type it the same way, and a fuzzy match here risks false positives on
# prose like "needs decision on the auth flow" inside a paragraph.
_NEEDS_DECISION_RE = re.compile(r"\[NEEDS DECISION(?::\s*([^\]]+))?\]")

# Section headers used to compute the location of an inline marker.
_H2_RE = re.compile(r"^##\s+(.+?)\s*$")
_H3_RE = re.compile(r"^###\s+(.+?)\s*$")

# Explicit "no items" placeholders in ## Open Questions / ## Risks bullets.
# Compared lower-cased and after stripping surrounding punctuation.
_NONE_PLACEHOLDERS = frozenset({
    "none",
    "none identified",
    "none declared",
    "n/a",
    "na",
    "tbd",
})


def _strip_html_comments(text: str) -> str:
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)


def _is_none_placeholder(text: str) -> bool:
    """True for explicit "no items" bullets — they are not unresolved."""
    cleaned = text.strip().rstrip(".").strip().lower()
    return cleaned in _NONE_PLACEHOLDERS


def _find_needs_decision_markers(
    markdown: str,
) -> list[tuple[int, str, str, str]]:
    """Walk the raw markdown line by line, tracking the enclosing H2/H3 and
    paragraph context for every `[NEEDS DECISION]` marker.

    Returns list of ``(line_number, section, paragraph, question_text)``.
    `section` is "Top-Level" if the marker appears before any `##` heading.
    """
    cleaned = _strip_html_comments(markdown)
    lines = cleaned.splitlines()

    current_h2 = "Top-Level"
    current_h3: str | None = None
    paragraph_buffer: list[str] = []
    paragraph_start_line = 1
    out: list[tuple[int, str, str, str]] = []

    def flush_paragraph() -> None:
        # Process the paragraph *as it closes* so the line/section for each
        # marker is anchored to its own paragraph, not a later one. Markers
        # landed in paragraph_buffer are emitted at paragraph_start_line+offset.
        if not paragraph_buffer:
            return
        paragraph_text = " ".join(s.strip() for s in paragraph_buffer if s.strip())
        for offset, raw in enumerate(paragraph_buffer):
            for match in _NEEDS_DECISION_RE.finditer(raw):
                question = (match.group(1) or "").strip()
                section_name = current_h2
                if current_h3:
                    section_name = f"{current_h2} → {current_h3}"
                out.append(
                    (
                        paragraph_start_line + offset,
                        section_name,
                        paragraph_text,
                        question,
                    )
                )

    for idx, raw in enumerate(lines, start=1):
        m_h2 = _H2_RE.match(raw)
        m_h3 = _H3_RE.match(raw)
        if m_h2:
            flush_paragraph()
            paragraph_buffer = []
            paragraph_start_line = idx + 1
            current_h2 = m_h2.group(1)
            current_h3 = None
            continue
        if m_h3:
            flush_paragraph()
            paragraph_buffer = []
            paragraph_start_line = idx + 1
            current_h3 = m_h3.group(1)
            continue
        if raw.strip() == "":
            flush_paragraph()
            paragraph_buffer = []
            paragraph_start_line = idx + 1
            continue
        paragraph_buffer.append(raw)

    # End-of-file paragraph.
    flush_paragraph()
    return out


def find_unresolved_decisions(
    markdown: str,
    *,
    prd: PRD | None,
    requirements: list[Requirement] | None = None,  # noqa: ARG001 — reserved
    features: list[Feature] | None = None,  # noqa: ARG001 — reserved
    tasks: list[Task] | None = None,
) -> list[UnresolvedDecision]:
    """Scan a PRD for items needing human decision before downstream work.

    Args:
        markdown: Raw PRD markdown source. Used to detect inline
            `[NEEDS DECISION]` markers, which are stripped from the parsed
            model and so cannot be detected from `prd` alone.
        prd: Parsed PRD model. Used to walk `## Open Questions` items.
            When `None`, only `needs_decision` markers are reported (this
            supports calling the detector before a successful parse).
        requirements: Reserved for future per-requirement detection (e.g.
            requirements with empty text). Not used yet — accepted now to
            avoid a signature break later.
        features: Reserved for future per-feature detection. Not used yet.
        tasks: Parsed tasks. Used to detect empty `acceptance_criteria` and
            empty `verification.commands` — both are review-gate failures
            that the resolver can drive Q&A on instead of blocking.

    Returns:
        Flat list of `UnresolvedDecision`. Order is stable: all
        `needs_decision` first (in source order), then `open_question`
        (in PRD order), then `missing_field` (in task ID order). Stable
        order matters because resolution applies edits to the PRD and the
        agent will iterate the list one at a time.
    """
    out: list[UnresolvedDecision] = []

    # Kind 1: inline [NEEDS DECISION] markers.
    for nd_idx, (lineno, section, paragraph, question) in enumerate(
        _find_needs_decision_markers(markdown), start=1
    ):
        marker_id = f"ND-{nd_idx:03d}"
        out.append(
            UnresolvedDecision(
                id=marker_id,
                kind=DecisionKind.needs_decision,
                location=f"{section} (line {lineno})",
                text=question or "(no question provided)",
                context_paragraph=paragraph,
                suggested_resolution_field="inline rewrite",
            )
        )

    # Kind 2: ## Open Questions items.
    # The OQ ID counter only advances for items that survive the placeholder
    # filter, so callers see contiguous IDs (OQ001, OQ002, ...) even when
    # the PRD interleaves real questions with "none identified" placeholders.
    # Non-contiguous IDs would confuse the resolver skill — it iterates
    # decisions sequentially and a missing OQ001 could read as "skipped."
    if prd is not None:
        oq_idx = 0
        for source_position, item in enumerate(prd.open_questions, start=1):
            if _is_none_placeholder(item):
                continue
            oq_idx += 1
            out.append(
                UnresolvedDecision(
                    id=f"OQ{oq_idx:03d}",
                    kind=DecisionKind.open_question,
                    location=f"## Open Questions item {source_position}",
                    text=item,
                    context_paragraph=item,
                    suggested_resolution_field="move to ## Decisions",
                )
            )

    # Kind 3: missing acceptance criteria / verification on tasks.
    if tasks:
        for task in tasks:
            if not task.acceptance_criteria:
                out.append(
                    UnresolvedDecision(
                        id=f"MF-{task.id}-AC",
                        kind=DecisionKind.missing_field,
                        location=f"{task.id} acceptance criteria",
                        text=(
                            f"Task '{task.title or task.id}' has no acceptance "
                            "criteria. The review gate requires at least one."
                        ),
                        context_paragraph=(task.description or task.title or "").strip(),
                        suggested_resolution_field=f"{task.id}.acceptance_criteria",
                    )
                )
            if not task.verification.commands:
                out.append(
                    UnresolvedDecision(
                        id=f"MF-{task.id}-V",
                        kind=DecisionKind.missing_field,
                        location=f"{task.id} verification",
                        text=(
                            f"Task '{task.title or task.id}' has no verification "
                            "commands. The review gate requires at least one."
                        ),
                        context_paragraph=(task.description or task.title or "").strip(),
                        suggested_resolution_field=f"{task.id}.verification.commands",
                    )
                )

    return out
