"""Unresolved-decision detection for anvil PRDs.

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
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from anvil.state.models import PRD, Feature, Requirement, Task

__all__ = [
    "DecisionKind",
    "DecisionResolution",
    "UnresolvedDecision",
    "apply_decision_to_markdown",
    "find_unresolved_decisions",
]


class DecisionKind(StrEnum):
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
        prd_ref: Back-reference to the PRD location the resolution writes
            back to (T018). This is the *anchor* the back-propagation uses to
            find the span to rewrite without touching unrelated content:
            - `needs_decision`: ``"line:<N>"`` — the exact source line the
              marker sits on, so the rewrite cannot collide with an
              identical marker elsewhere.
            - `open_question`: ``"open_question:<source_position>"`` — the
              1-based position of the bullet under ``## Open Questions``.
            - `missing_field`: ``"task:<TASK_ID>:<field>"`` — the task block
              and the field (``acceptance_criteria`` / ``verification``) the
              resolution should populate.
            Defaults to ``""`` for hand-built decisions that do not target a
            specific span (the resolver then has nothing to back-propagate).
    """

    id: str
    kind: DecisionKind
    location: str
    text: str
    context_paragraph: str
    suggested_resolution_field: str
    prd_ref: str = ""


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
                prd_ref=f"line:{lineno}",
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
                    prd_ref=f"open_question:{source_position}",
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
                        prd_ref=f"task:{task.id}:acceptance_criteria",
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
                        prd_ref=f"task:{task.id}:verification",
                    )
                )

    return out


# ===========================================================================
# T018 — decision back-propagation to the PRD
# ===========================================================================
#
# `find_unresolved_decisions` is the *detection* half: it walks the PRD and
# surfaces what still needs a human answer. The functions below are the
# *resolution* half: given a decision and the human's answer, they write the
# answer back into the referenced PRD span and leave every unrelated line of
# the document byte-for-byte unchanged.
#
# The split matters. Detection is run on every `prd find-decisions`; resolution
# is a deliberate mutation the CLI records as an additive event in the log
# (`prd.decision_resolved`). Keeping the markdown surgery pure here means the
# CLI command and the MCP tool both apply *identical* edits, and tests can
# assert on the rewrite without a backend.


class ResolutionError(ValueError):
    """A decision could not be back-propagated to the PRD.

    Raised when the ``prd_ref`` anchor cannot be located in the supplied
    markdown (e.g. the marker was already removed, the task block is absent,
    or the open-question position is out of range). The message names the
    anchor so the caller can surface a precise error rather than silently
    no-op'ing — a silent no-op would record a `prd.decision_resolved` event
    that does not match the file, breaking the audit trail.
    """


@dataclass(frozen=True)
class DecisionResolution:
    """The result of back-propagating one decision into the PRD markdown.

    Attributes:
        markdown: The full updated PRD source. Differs from the input only in
            the span the ``prd_ref`` anchored — every other line is preserved
            verbatim (the "without overwriting unrelated content" contract).
        prd_ref: The anchor that was resolved (echoed for the event payload).
        kind: The decision kind that drove the rewrite strategy.
        before: The exact original text of the span that changed.
        after: The exact replacement text. For an open-question move this is
            the ``## Decisions`` entry that was added.
        section: A human-readable name of the PRD location that changed
            (e.g. ``"## Open Questions"``, ``"T012 acceptance criteria"``,
            or the H2/H3 the marker lived under).
    """

    markdown: str
    prd_ref: str
    kind: DecisionKind
    before: str
    after: str
    section: str


# A `[NEEDS DECISION...]` marker plus any immediately-trailing whitespace, so
# replacing the marker with prose does not leave a double space behind.
_NEEDS_DECISION_TRAILING_RE = re.compile(r"\[NEEDS DECISION(?::\s*[^\]]+)?\]\s*")

_BULLET_PREFIX_RE = re.compile(r"^(\s*[-*]\s+)(.*)$")


def _parse_prd_ref(prd_ref: str) -> tuple[str, list[str]]:
    """Split ``"<kind>:<arg>[:<arg>]"`` into ``(kind, [args])``.

    Raises ResolutionError on an empty/malformed anchor so the caller fails
    loudly rather than guessing.
    """
    if not prd_ref:
        raise ResolutionError(
            "decision has no prd_ref anchor; nothing to back-propagate."
        )
    head, _, rest = prd_ref.partition(":")
    if not head or not rest:
        raise ResolutionError(f"malformed prd_ref {prd_ref!r}.")
    return head, rest.split(":")


def _resolve_needs_decision(
    markdown: str, lineno: int, resolution: str
) -> tuple[str, str, str]:
    """Rewrite the `[NEEDS DECISION]` marker on *lineno* (1-based) inline.

    The marker is replaced with the resolution text; the rest of the line —
    and every other line in the document — is preserved verbatim. Returns
    ``(new_markdown, before_line, after_line)``.
    """
    lines = markdown.splitlines(keepends=True)
    idx = lineno - 1
    if idx < 0 or idx >= len(lines):
        raise ResolutionError(
            f"prd_ref line:{lineno} is out of range "
            f"(document has {len(lines)} lines)."
        )
    original = lines[idx]
    if not _NEEDS_DECISION_RE.search(original):
        raise ResolutionError(
            f"no [NEEDS DECISION] marker found on line {lineno}; "
            "the PRD may have changed since detection — re-run "
            "`prd find-decisions`."
        )

    # Replace only the first marker on the line (detection emits one decision
    # per marker, in source order, so resolving them one at a time keeps the
    # remaining markers' line anchors valid).
    replacement = resolution.strip()
    new_line = _NEEDS_DECISION_TRAILING_RE.sub(
        lambda _m: (replacement + " ") if replacement else "",
        original,
        count=1,
    )
    # A marker at end-of-sentence often leaves " ." — tidy the common case of
    # a stray space before sentence punctuation without touching other text.
    new_line = re.sub(r"\s+([.,;:!?])", r"\1", new_line)
    lines[idx] = new_line
    return "".join(lines), original.rstrip("\n"), new_line.rstrip("\n")


def _find_section_bounds(
    lines: list[str], heading_lower: str
) -> tuple[int, int] | None:
    """Return ``(start, end)`` line indices (0-based, end-exclusive) of the
    body of the ``## <heading>`` section, or None if the section is absent.

    ``start`` is the line after the heading; ``end`` is the next ``## `` or
    EOF. The heading line itself is excluded from the returned range.
    """
    start: int | None = None
    for i, raw in enumerate(lines):
        if raw.startswith("## ") and raw[3:].strip().lower() == heading_lower:
            start = i + 1
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    return start, end


def _resolve_open_question(
    markdown: str, source_position: int, resolution: str
) -> tuple[str, str, str]:
    """Move the *source_position*-th ``## Open Questions`` bullet to ``## Decisions``.

    The original bullet is deleted from ``## Open Questions``; a resolved
    entry is appended to ``## Decisions`` (the section is created just above
    ``## Risks``, or at end-of-file, when absent). Every other bullet and
    section is preserved. Returns ``(new_markdown, before_bullet, after_entry)``.
    """
    lines = markdown.splitlines()
    bounds = _find_section_bounds(lines, "open questions")
    if bounds is None:
        raise ResolutionError("PRD has no '## Open Questions' section.")
    start, end = bounds

    # Enumerate bullets within the section, tracking their absolute index.
    bullet_indices: list[int] = []
    for i in range(start, end):
        if _BULLET_PREFIX_RE.match(lines[i]):
            bullet_indices.append(i)
    if source_position < 1 or source_position > len(bullet_indices):
        raise ResolutionError(
            f"open_question position {source_position} is out of range "
            f"({len(bullet_indices)} bullet(s) under ## Open Questions)."
        )
    target_idx = bullet_indices[source_position - 1]
    before_bullet = lines[target_idx]
    question_text = _BULLET_PREFIX_RE.match(before_bullet).group(2).strip()

    # Build the audit entry for ## Decisions.
    after_entry = (
        f"- **{question_text}** "
        f"→ **Decision:** {resolution.strip()}"
    )

    # Delete the resolved bullet from ## Open Questions.
    del lines[target_idx]

    # Append to ## Decisions (create if missing). Recompute bounds because the
    # deletion shifted indices.
    decisions_bounds = _find_section_bounds(lines, "decisions")
    if decisions_bounds is not None:
        _d_start, d_end = decisions_bounds
        # Insert the new entry as the last bullet of the section, before any
        # trailing blank line that separates it from the next section.
        insert_at = d_end
        while insert_at > _d_start and lines[insert_at - 1].strip() == "":
            insert_at -= 1
        lines.insert(insert_at, after_entry)
    else:
        # Create the section. Prefer placing it just above ## Risks so the
        # audit trail sits near the open questions it resolves; otherwise at EOF.
        risks_heading = next(
            (i for i, ln in enumerate(lines) if ln.startswith("## ")
             and ln[3:].strip().lower() == "risks"),
            None,
        )
        block = ["## Decisions", "", after_entry, ""]
        if risks_heading is not None:
            for offset, blk_line in enumerate(block):
                lines.insert(risks_heading + offset, blk_line)
        else:
            if lines and lines[-1].strip() != "":
                lines.append("")
            lines.extend(block[:-1])

    trailing_nl = "\n" if markdown.endswith("\n") else ""
    return "\n".join(lines) + trailing_nl, before_bullet, after_entry


def _resolve_missing_field(
    markdown: str, task_id: str, field: str, resolution: str
) -> tuple[str, str, str]:
    """Append an acceptance-criterion / verification entry under ``### <task_id>:``.

    The resolution is inserted as a bullet under the appropriate field block
    inside the task's ``### <TASK_ID>:`` H3 section, creating the field label
    (``**Acceptance criteria:**`` / ``**Verification:**``) if the task has
    none yet. Surrounding task blocks are untouched. Returns
    ``(new_markdown, before_block, after_block)``.
    """
    if field not in {"acceptance_criteria", "verification"}:
        raise ResolutionError(
            f"unsupported task field {field!r} "
            "(expected 'acceptance_criteria' or 'verification')."
        )
    lines = markdown.splitlines()

    # Locate the task's H3 block: `### <TASK_ID>:` ... up to the next `### ` or `## `.
    h3_re = re.compile(rf"^###\s+{re.escape(task_id)}\b")
    start = next((i for i, ln in enumerate(lines) if h3_re.match(ln)), None)
    if start is None:
        raise ResolutionError(
            f"no '### {task_id}' task block found in the PRD."
        )
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("### ") or lines[j].startswith("## "):
            end = j
            break

    label = (
        "**Acceptance criteria:**"
        if field == "acceptance_criteria"
        else "**Verification:**"
    )
    is_verification = field == "verification"
    entry = f"- `{resolution.strip()}`" if is_verification else f"- {resolution.strip()}"

    before_block = "\n".join(lines[start:end]).rstrip()

    # Find the field label within the block.
    label_idx = next(
        (i for i in range(start, end) if lines[i].strip().lower() == label.lower()),
        None,
    )
    if label_idx is not None:
        # Insert after the last existing bullet under the label (or right after
        # the label if it has none yet).
        insert_at = label_idx + 1
        i = label_idx + 1
        while i < end and (lines[i].strip() == "" or _BULLET_PREFIX_RE.match(lines[i])):
            if _BULLET_PREFIX_RE.match(lines[i]):
                insert_at = i + 1
            i += 1
        lines.insert(insert_at, entry)
    else:
        # No label yet — append the label + entry at the end of the block,
        # trimming trailing blank lines first so the new block reads cleanly.
        insert_at = end
        while insert_at > start and lines[insert_at - 1].strip() == "":
            insert_at -= 1
        block = ["", label, "", entry]
        for offset, blk_line in enumerate(block):
            lines.insert(insert_at + offset, blk_line)
        end += len(block)

    after_block = "\n".join(lines[start:end + 1]).rstrip()
    trailing_nl = "\n" if markdown.endswith("\n") else ""
    return "\n".join(lines) + trailing_nl, before_block, after_block


def apply_decision_to_markdown(
    markdown: str,
    *,
    decision: UnresolvedDecision,
    resolution: str,
) -> DecisionResolution:
    """Back-propagate one resolved decision into the PRD markdown (T018).

    Writes *resolution* into the PRD span anchored by ``decision.prd_ref`` and
    returns a :class:`DecisionResolution` carrying the updated markdown plus a
    structured before/after the CLI records in the event log. Every line of the
    document the anchor does **not** point at is preserved byte-for-byte — the
    rewrite is additive and surgical, never a whole-file regeneration.

    Strategy by kind (driven entirely by ``prd_ref``):

    - ``needs_decision`` (``line:<N>``): replace the inline ``[NEEDS DECISION]``
      marker on line N with the resolution prose, keeping the rest of the
      sentence. This is the "resolving a [NEEDS DECISION] marker updates the
      linked PRD requirement" criterion.
    - ``open_question`` (``open_question:<pos>``): delete the bullet from
      ``## Open Questions`` and append a resolved entry to ``## Decisions``.
    - ``missing_field`` (``task:<ID>:<field>``): add an acceptance-criterion or
      verification bullet under the task's ``### <ID>:`` block.

    Args:
        markdown: The current PRD source.
        decision: The decision being resolved (its ``prd_ref`` is the anchor).
        resolution: The human's chosen answer to write into the PRD.

    Returns:
        A :class:`DecisionResolution`.

    Raises:
        ResolutionError: The anchor could not be located (the PRD changed
            since detection, or the decision carries no usable ``prd_ref``),
            or *resolution* is empty.
    """
    if not resolution.strip():
        raise ResolutionError("resolution text is empty; nothing to write back.")

    ref_kind, args = _parse_prd_ref(decision.prd_ref)

    if ref_kind == "line":
        new_md, before, after = _resolve_needs_decision(
            markdown, int(args[0]), resolution
        )
        section = decision.location
    elif ref_kind == "open_question":
        new_md, before, after = _resolve_open_question(
            markdown, int(args[0]), resolution
        )
        section = "## Open Questions → ## Decisions"
    elif ref_kind == "task":
        task_id, field = args[0], args[1]
        new_md, before, after = _resolve_missing_field(
            markdown, task_id, field, resolution
        )
        section = f"{task_id} {field}"
    else:
        raise ResolutionError(f"unknown prd_ref kind {ref_kind!r}.")

    return DecisionResolution(
        markdown=new_md,
        prd_ref=decision.prd_ref,
        kind=decision.kind,
        before=before,
        after=after,
        section=section,
    )
