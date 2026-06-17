"""Tests for fakoli_state.planning.decisions.find_unresolved_decisions."""

from __future__ import annotations

import datetime

from fakoli_state.clock import FrozenClock
from fakoli_state.planning.decisions import (
    DecisionKind,
    UnresolvedDecision,
    find_unresolved_decisions,
)
from fakoli_state.planning.template import parse_prd
from fakoli_state.state.models import (
    PRD,
    Score,
    Task,
    TaskPriority,
    TaskStatus,
    Verification,
)

_FROZEN = FrozenClock(datetime.datetime(2026, 5, 26, 12, 0, tzinfo=datetime.UTC))


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
