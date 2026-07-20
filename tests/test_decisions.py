"""Tests for anvil.planning.decisions.find_unresolved_decisions."""

from __future__ import annotations

import datetime

import pytest

from anvil.clock import FrozenClock
from anvil.planning.decisions import (
    DecisionKind,
    DecisionResolution,
    ResolutionError,
    UnresolvedDecision,
    apply_decision_to_markdown,
    find_unresolved_decisions,
)
from anvil.planning.template import parse_prd
from anvil.state.models import (
    PRD,
    Score,
    Task,
    TaskPriority,
    TaskStatus,
    Verification,
)

_FROZEN = FrozenClock(datetime.datetime(2026, 5, 26, 12, 0, tzinfo=datetime.UTC))


def test_decision_kind_preserves_legacy_string_formatting() -> None:
    kind = DecisionKind.needs_decision

    assert str(kind) == "DecisionKind.needs_decision"
    assert f"{kind}" == "DecisionKind.needs_decision"
    assert format(kind) == "DecisionKind.needs_decision"


# ---------------------------------------------------------------------------
# needs_decision (inline [NEEDS DECISION] markers)
# ---------------------------------------------------------------------------


class TestNeedsDecisionDetection:
    def test_single_marker_with_question(self) -> None:
        markdown = """\
# Project: Test

## Summary

The system must validate inputs [NEEDS DECISION: which encoding?].
"""
        result = find_unresolved_decisions(markdown, prd=None)
        assert len(result) == 1
        decision = result[0]
        assert decision.kind == DecisionKind.needs_decision
        assert decision.id == "ND-001"
        assert "which encoding?" in decision.text
        assert "Summary" in decision.location
        assert "validate inputs" in decision.context_paragraph

    def test_marker_without_question_payload(self) -> None:
        markdown = """\
# Project: Test

## Goals

- Ship v1 [NEEDS DECISION]
"""
        result = find_unresolved_decisions(markdown, prd=None)
        assert len(result) == 1
        assert result[0].text == "(no question provided)"
        assert "Goals" in result[0].location

    def test_multiple_markers_get_sequential_ids(self) -> None:
        markdown = """\
# Project: Test

## Summary

First [NEEDS DECISION: A?]. Second [NEEDS DECISION: B?].

## Goals

Third [NEEDS DECISION: C?].
"""
        result = find_unresolved_decisions(markdown, prd=None)
        ids = [d.id for d in result]
        assert ids == ["ND-001", "ND-002", "ND-003"]

    def test_marker_inside_h3_records_section_path(self) -> None:
        markdown = """\
# Project: Test

## Features

### F001: Auth

Validates the token [NEEDS DECISION: JWT or session?].
"""
        result = find_unresolved_decisions(markdown, prd=None)
        assert len(result) == 1
        # Location includes both the H2 and the H3 it nested under.
        assert "Features" in result[0].location
        assert "F001" in result[0].location

    def test_no_markers_returns_empty(self) -> None:
        markdown = """\
# Project: Clean

## Summary

Nothing unresolved here.
"""
        assert find_unresolved_decisions(markdown, prd=None) == []

    def test_marker_inside_html_comment_is_ignored(self) -> None:
        """Comments are stripped before scanning — drafts can keep TODO-style
        notes in comments without triggering the resolver."""
        markdown = """\
# Project: Test

## Summary

Body text.

<!-- [NEEDS DECISION: ignore me] -->
"""
        assert find_unresolved_decisions(markdown, prd=None) == []

    def test_fuzzy_prose_does_not_false_positive(self) -> None:
        """The marker is case-sensitive bracket-enclosed; prose like 'needs
        decision' should not trip it."""
        markdown = """\
# Project: Test

## Summary

This needs decision on the auth flow eventually.
"""
        assert find_unresolved_decisions(markdown, prd=None) == []


# ---------------------------------------------------------------------------
# open_question (## Open Questions items)
# ---------------------------------------------------------------------------


class TestOpenQuestionDetection:
    def test_open_questions_become_decisions(self) -> None:
        markdown = """\
# Project: Test

## Summary

x.

## Goals

- y.

## Requirements

- R001: z.

## Open Questions

- Which serialization format should we use?
- What is the upper bound on payload size?
"""
        parsed = parse_prd(markdown, clock=_FROZEN)
        result = find_unresolved_decisions(markdown, prd=parsed.prd)
        oq_decisions = [d for d in result if d.kind == DecisionKind.open_question]
        assert len(oq_decisions) == 2
        assert oq_decisions[0].id == "OQ001"
        assert "serialization" in oq_decisions[0].text
        assert oq_decisions[1].id == "OQ002"

    def test_none_placeholders_are_skipped(self) -> None:
        markdown = """\
# Project: Test

## Summary

x.

## Goals

- y.

## Requirements

- R001: z.

## Open Questions

- none identified
"""
        parsed = parse_prd(markdown, clock=_FROZEN)
        result = find_unresolved_decisions(markdown, prd=parsed.prd)
        oq_decisions = [d for d in result if d.kind == DecisionKind.open_question]
        assert oq_decisions == []

    def test_missing_open_questions_section_is_ok(self) -> None:
        markdown = """\
# Project: Test

## Summary

x.

## Goals

- y.

## Requirements

- R001: z.
"""
        parsed = parse_prd(markdown, clock=_FROZEN)
        result = find_unresolved_decisions(markdown, prd=parsed.prd)
        assert [d for d in result if d.kind == DecisionKind.open_question] == []

    def test_prd_none_skips_open_questions(self) -> None:
        markdown = """\
# Project: Test

## Open Questions

- foo?
"""
        # Caller didn't parse the PRD — only inline markers can be detected.
        result = find_unresolved_decisions(markdown, prd=None)
        assert [d for d in result if d.kind == DecisionKind.open_question] == []

    def test_oq_ids_are_contiguous_when_placeholders_are_skipped(self) -> None:
        """Regression for greptile PR #62 finding: previously the OQ counter
        advanced for every item including 'none identified' placeholders, so
        a PRD with [placeholder, real, placeholder, real] produced IDs
        OQ002 and OQ004 instead of OQ001 and OQ002. Non-contiguous IDs
        confuse the resolver skill which iterates the list sequentially.
        """
        markdown = """\
# Project: Test

## Summary

x.

## Goals

- y.

## Requirements

- R001: z.

## Open Questions

- none identified
- What is the SLO?
- n/a
- Which protocol should we use?
"""
        parsed = parse_prd(markdown, clock=_FROZEN)
        result = find_unresolved_decisions(markdown, prd=parsed.prd)
        oq_decisions = [d for d in result if d.kind == DecisionKind.open_question]
        ids = [d.id for d in oq_decisions]
        # Two real items between two placeholders should produce OQ001 + OQ002,
        # not OQ002 + OQ004.
        assert ids == ["OQ001", "OQ002"], (
            f"Expected contiguous OQ IDs after placeholder filter, got: {ids}"
        )
        # The `location` field carries the SOURCE position (so users can
        # find the item in the file), even though the ID is the contiguous
        # resolver counter.
        assert oq_decisions[0].location == "## Open Questions item 2"
        assert oq_decisions[1].location == "## Open Questions item 4"


# ---------------------------------------------------------------------------
# missing_field (tasks with empty acceptance_criteria or verification)
# ---------------------------------------------------------------------------


def _task(
    task_id: str,
    *,
    acceptance_criteria: list[str] | None = None,
    verification_commands: list[str] | None = None,
) -> Task:
    now = _FROZEN.now()
    return Task(
        id=task_id,
        feature_id="F001",
        title=f"Task {task_id}",
        description="",
        status=TaskStatus.drafted,
        priority=TaskPriority.medium,
        scores=Score(),
        acceptance_criteria=acceptance_criteria or [],
        verification=Verification(commands=verification_commands or []),
        likely_files=[],
        created_at=now,
        updated_at=now,
    )


class TestMissingFieldDetection:
    def test_empty_acceptance_criteria_emits_decision(self) -> None:
        task = _task("T001", verification_commands=["pytest"])
        result = find_unresolved_decisions(
            "", prd=PRD(), tasks=[task]
        )
        mf = [d for d in result if d.kind == DecisionKind.missing_field]
        assert len(mf) == 1
        assert mf[0].id == "MF-T001-AC"
        assert "T001" in mf[0].location
        assert "acceptance" in mf[0].location

    def test_empty_verification_emits_decision(self) -> None:
        task = _task("T001", acceptance_criteria=["Works"])
        result = find_unresolved_decisions(
            "", prd=PRD(), tasks=[task]
        )
        mf = [d for d in result if d.kind == DecisionKind.missing_field]
        assert len(mf) == 1
        assert mf[0].id == "MF-T001-V"
        assert "verification" in mf[0].location

    def test_both_empty_emits_both(self) -> None:
        task = _task("T001")
        result = find_unresolved_decisions(
            "", prd=PRD(), tasks=[task]
        )
        mf_ids = {d.id for d in result if d.kind == DecisionKind.missing_field}
        assert mf_ids == {"MF-T001-AC", "MF-T001-V"}

    def test_well_formed_task_emits_nothing(self) -> None:
        task = _task("T001", acceptance_criteria=["Works"], verification_commands=["pytest"])
        result = find_unresolved_decisions(
            "", prd=PRD(), tasks=[task]
        )
        assert [d for d in result if d.kind == DecisionKind.missing_field] == []

    def test_tasks_none_skips_missing_field_check(self) -> None:
        result = find_unresolved_decisions("", prd=PRD(), tasks=None)
        assert [d for d in result if d.kind == DecisionKind.missing_field] == []


# ---------------------------------------------------------------------------
# Cross-kind: stable ordering
# ---------------------------------------------------------------------------


class TestStableOrdering:
    def test_needs_decision_first_then_open_questions_then_missing_fields(
        self,
    ) -> None:
        """Ordering is the contract: agent iterates the list one Q&A at a
        time, so the order determines the user's conversation flow. We want
        inline markers first (they often shape Open Questions), then Open
        Questions, then missing fields."""
        markdown = """\
# Project: Test

## Summary

x [NEEDS DECISION: protocol?].

## Goals

- y.

## Requirements

- R001: z.

## Open Questions

- What is the SLO?
"""
        parsed = parse_prd(markdown, clock=_FROZEN)
        task = _task("T001")
        result = find_unresolved_decisions(
            markdown,
            prd=parsed.prd,
            tasks=[task],
        )
        kinds = [d.kind for d in result]
        # All needs_decision must precede all open_question must precede all missing_field.
        assert kinds.index(DecisionKind.needs_decision) < kinds.index(
            DecisionKind.open_question
        )
        assert kinds.index(DecisionKind.open_question) < kinds.index(
            DecisionKind.missing_field
        )


# ---------------------------------------------------------------------------
# Smoke test: clean PRD returns empty list
# ---------------------------------------------------------------------------


class TestCleanPrd:
    def test_fully_resolved_prd_returns_empty(self) -> None:
        markdown = """\
# Project: Clean

## Summary

Everything is resolved.

## Goals

- Ship.

## Requirements

- R001: System works.

## Open Questions

- none identified
"""
        parsed = parse_prd(markdown, clock=_FROZEN)
        task = _task("T001", acceptance_criteria=["Works"], verification_commands=["pytest"])
        result = find_unresolved_decisions(
            markdown, prd=parsed.prd, tasks=[task]
        )
        # The UnresolvedDecision NamedTuple shape sanity check.
        assert all(isinstance(d, UnresolvedDecision) for d in result)
        assert result == []


# ---------------------------------------------------------------------------
# T018 — decision back-propagation (prd_ref population + apply_decision)
# ---------------------------------------------------------------------------


def _resolve_first(markdown, *, prd=None, tasks=None, kind, resolution):
    """Detect decisions, pick the first of *kind*, and back-propagate it."""
    decisions = find_unresolved_decisions(markdown, prd=prd, tasks=tasks)
    target = next(d for d in decisions if d.kind == kind)
    return target, apply_decision_to_markdown(
        markdown, decision=target, resolution=resolution
    )


class TestBackpropRefPopulation:
    """The detector tags every decision with a prd_ref anchor (T018)."""

    def test_backprop_needs_decision_carries_line_ref(self) -> None:
        markdown = """\
# Project: Test

## Summary

The system must validate inputs [NEEDS DECISION: which encoding?].
"""
        result = find_unresolved_decisions(markdown, prd=None)
        assert result[0].prd_ref == "line:5"

    def test_backprop_open_question_carries_position_ref(self) -> None:
        markdown = """\
# Project: Test

## Summary

x.

## Goals

- y.

## Requirements

- R001: z.

## Open Questions

- Which serialization format should we use?
"""
        parsed = parse_prd(markdown, clock=_FROZEN)
        oq = next(
            d
            for d in find_unresolved_decisions(markdown, prd=parsed.prd)
            if d.kind == DecisionKind.open_question
        )
        assert oq.prd_ref == "open_question:1"

    def test_backprop_missing_field_carries_task_ref(self) -> None:
        task_ac = _task("T001", verification_commands=["pytest"])
        task_v = _task("T002", acceptance_criteria=["Works"])
        result = find_unresolved_decisions("", prd=PRD(), tasks=[task_ac, task_v])
        by_id = {d.id: d.prd_ref for d in result}
        assert by_id["MF-T001-AC"] == "task:T001:acceptance_criteria"
        assert by_id["MF-T002-V"] == "task:T002:verification"

    def test_backprop_default_prd_ref_is_empty(self) -> None:
        """Hand-built decisions without an anchor default to empty (back-compat)."""
        d = UnresolvedDecision(
            id="X",
            kind=DecisionKind.needs_decision,
            location="loc",
            text="t",
            context_paragraph="c",
            suggested_resolution_field="f",
        )
        assert d.prd_ref == ""


class TestBackpropNeedsDecision:
    """Resolving a [NEEDS DECISION] marker rewrites the linked requirement."""

    def test_backprop_marker_replaced_inline(self) -> None:
        markdown = """\
# Project: Test

## Requirements

- R007: The system must validate inputs [NEEDS DECISION: which encoding?].
"""
        target, res = _resolve_first(
            markdown,
            kind=DecisionKind.needs_decision,
            resolution="UTF-8 only",
        )
        assert isinstance(res, DecisionResolution)
        # Marker is gone; resolution prose is in its place.
        assert "[NEEDS DECISION" not in res.markdown
        assert "validate inputs UTF-8 only." in res.markdown

    def test_backprop_preserves_unrelated_lines(self) -> None:
        """Only the marker's line changes; everything else is byte-identical."""
        markdown = """\
# Project: Test

## Summary

Untouched summary line.

## Requirements

- R001: First requirement is fine.
- R007: Validate inputs [NEEDS DECISION: which encoding?].
- R008: Third requirement is also fine.
"""
        _, res = _resolve_first(
            markdown,
            kind=DecisionKind.needs_decision,
            resolution="UTF-8 only",
        )
        out = res.markdown
        assert "Untouched summary line." in out
        assert "R001: First requirement is fine." in out
        assert "R008: Third requirement is also fine." in out
        # The only difference between input and output is the single marker line.
        diff_lines = [
            (a, b)
            for a, b in zip(markdown.splitlines(), out.splitlines(), strict=False)
            if a != b
        ]
        assert len(diff_lines) == 1
        assert "[NEEDS DECISION" in diff_lines[0][0]
        assert "[NEEDS DECISION" not in diff_lines[0][1]

    def test_backprop_picks_correct_marker_among_duplicates(self) -> None:
        """Two identical markers — the line anchor resolves the right one."""
        markdown = """\
# Project: Test

## Requirements

- R001: alpha [NEEDS DECISION: pick?].
- R002: beta [NEEDS DECISION: pick?].
"""
        decisions = find_unresolved_decisions(markdown, prd=None)
        # Resolve the SECOND marker only.
        second = decisions[1]
        assert second.prd_ref == "line:6"
        res = apply_decision_to_markdown(
            markdown, decision=second, resolution="answer-2"
        )
        # R002's marker resolved, R001's marker untouched.
        assert "R001: alpha [NEEDS DECISION: pick?]." in res.markdown
        assert "R002: beta answer-2." in res.markdown

    def test_backprop_stale_marker_raises(self) -> None:
        markdown = "# Project: Test\n\n## Summary\n\nNo marker here.\n"
        decision = UnresolvedDecision(
            id="ND-001",
            kind=DecisionKind.needs_decision,
            location="Summary",
            text="q",
            context_paragraph="",
            suggested_resolution_field="inline rewrite",
            prd_ref="line:5",
        )
        with pytest.raises(ResolutionError):
            apply_decision_to_markdown(
                markdown, decision=decision, resolution="x"
            )


class TestBackpropOpenQuestion:
    def test_backprop_open_question_moves_to_decisions(self) -> None:
        markdown = """\
# Project: Test

## Summary

x.

## Goals

- y.

## Requirements

- R001: z.

## Open Questions

- Which serialization format should we use?
- What is the upper bound on payload size?

## Risks

- none identified
"""
        parsed = parse_prd(markdown, clock=_FROZEN)
        target, res = _resolve_first(
            markdown,
            prd=parsed.prd,
            kind=DecisionKind.open_question,
            resolution="MessagePack",
        )
        out = res.markdown
        # Resolved question removed from Open Questions, second one preserved.
        assert "Which serialization format should we use?" not in _open_questions_block(out)
        assert "What is the upper bound on payload size?" in out
        # A ## Decisions section was created with the resolution.
        assert "## Decisions" in out
        assert "MessagePack" in out
        # Risks section preserved intact.
        assert "## Risks" in out
        # Re-parse to prove the document is still structurally valid.
        assert not parse_prd(out, clock=_FROZEN).errors

    def test_backprop_second_resolution_appends_to_existing_decisions(self) -> None:
        markdown = """\
# Project: Test

## Summary

x.

## Goals

- y.

## Requirements

- R001: z.

## Open Questions

- First question?
- Second question?
"""
        parsed = parse_prd(markdown, clock=_FROZEN)
        decisions = find_unresolved_decisions(markdown, prd=parsed.prd)
        oqs = [d for d in decisions if d.kind == DecisionKind.open_question]
        # Resolve the first; then re-detect and resolve the (now-first) remaining.
        res1 = apply_decision_to_markdown(
            markdown, decision=oqs[0], resolution="A1"
        )
        reparsed = parse_prd(res1.markdown, clock=_FROZEN)
        oqs2 = [
            d
            for d in find_unresolved_decisions(res1.markdown, prd=reparsed.prd)
            if d.kind == DecisionKind.open_question
        ]
        res2 = apply_decision_to_markdown(
            res1.markdown, decision=oqs2[0], resolution="A2"
        )
        # Both decisions recorded under a single ## Decisions section.
        assert res2.markdown.count("## Decisions") == 1
        assert "A1" in res2.markdown
        assert "A2" in res2.markdown


class TestBackpropMissingField:
    def test_backprop_adds_acceptance_criteria_to_task_block(self) -> None:
        markdown = """\
# Project: Test

## Tasks

### T001: Implement the thing

**Feature:** F001
**Priority:** high

**Verification:**

- `pytest`
"""
        task = _task("T001", verification_commands=["pytest"])
        target, res = _resolve_first(
            markdown,
            prd=PRD(),
            tasks=[task],
            kind=DecisionKind.missing_field,
            resolution="The thing works for valid input.",
        )
        assert target.prd_ref == "task:T001:acceptance_criteria"
        assert "**Acceptance criteria:**" in res.markdown
        assert "The thing works for valid input." in res.markdown

    def test_backprop_adds_verification_under_existing_label(self) -> None:
        markdown = """\
# Project: Test

## Tasks

### T001: Implement the thing

**Feature:** F001
**Priority:** high

**Acceptance criteria:**

- Works.

**Verification:**

- `pytest tests/test_a.py`
"""
        task = _task("T001", acceptance_criteria=["Works."])
        target = next(
            d
            for d in find_unresolved_decisions("", prd=PRD(), tasks=[task])
            if d.id == "MF-T001-V"
        )
        res = apply_decision_to_markdown(
            markdown, decision=target, resolution="pytest tests/test_b.py"
        )
        # Both the original and the new verification command are present.
        assert "pytest tests/test_a.py" in res.markdown
        assert "pytest tests/test_b.py" in res.markdown

    def test_backprop_missing_task_block_raises(self) -> None:
        markdown = "# Project: Test\n\n## Tasks\n\n### T999: Other\n"
        decision = UnresolvedDecision(
            id="MF-T001-AC",
            kind=DecisionKind.missing_field,
            location="T001 acceptance criteria",
            text="t",
            context_paragraph="",
            suggested_resolution_field="T001.acceptance_criteria",
            prd_ref="task:T001:acceptance_criteria",
        )
        with pytest.raises(ResolutionError):
            apply_decision_to_markdown(
                markdown, decision=decision, resolution="x"
            )


class TestBackpropGuards:
    def test_backprop_empty_resolution_raises(self) -> None:
        decision = UnresolvedDecision(
            id="ND-001",
            kind=DecisionKind.needs_decision,
            location="L",
            text="q",
            context_paragraph="",
            suggested_resolution_field="inline rewrite",
            prd_ref="line:1",
        )
        with pytest.raises(ResolutionError):
            apply_decision_to_markdown(
                "x [NEEDS DECISION]\n", decision=decision, resolution="   "
            )

    def test_backprop_no_prd_ref_raises(self) -> None:
        decision = UnresolvedDecision(
            id="X",
            kind=DecisionKind.needs_decision,
            location="L",
            text="q",
            context_paragraph="",
            suggested_resolution_field="inline rewrite",
        )
        with pytest.raises(ResolutionError):
            apply_decision_to_markdown("anything\n", decision=decision, resolution="y")


def _open_questions_block(markdown: str) -> str:
    """Return only the text under ## Open Questions (for assertions)."""
    lines = markdown.splitlines()
    out: list[str] = []
    capture = False
    for ln in lines:
        if ln.startswith("## "):
            capture = ln[3:].strip().lower() == "open questions"
            continue
        if capture:
            out.append(ln)
    return "\n".join(out)
