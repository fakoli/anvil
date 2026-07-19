"""Tests for the structured acceptance grammar (EARS / Gherkin) — T028.

The PRD parser's ``parse_acceptance_grammar`` helper is purely additive: it
decomposes acceptance-criteria strings into structured clauses when an EARS or
Gherkin grammar is present, and falls back to ``freeform`` clauses otherwise.
The canonical ``Task.acceptance_criteria`` list of raw strings is unchanged.

Test selector: ``pytest -k acceptance_grammar``.
"""

from __future__ import annotations

from anvil.planning.template import (
    AcceptanceClause,
    parse_acceptance_grammar,
    parse_prd,
)

# ---------------------------------------------------------------------------
# EARS recognition
# ---------------------------------------------------------------------------


class TestAcceptanceGrammarEars:
    """EARS-style criteria parse into structured when/then (and friends)."""

    def test_acceptance_grammar_ears_when_then(self) -> None:
        clauses = parse_acceptance_grammar(
            ["WHEN a claim expires THEN the task returns to the ready queue."]
        )
        assert len(clauses) == 1
        c = clauses[0]
        assert isinstance(c, AcceptanceClause)
        assert c.kind == "ears"
        assert c.clauses["when"] == "a claim expires"
        assert c.clauses["then"] == "the task returns to the ready queue."
        # The raw text is always preserved verbatim.
        assert c.text == "WHEN a claim expires THEN the task returns to the ready queue."

    def test_acceptance_grammar_ears_when_shall(self) -> None:
        clauses = parse_acceptance_grammar(
            ["WHEN the user submits the form, THE SYSTEM SHALL persist the record."]
        )
        assert len(clauses) == 1
        c = clauses[0]
        assert c.kind == "ears"
        assert c.clauses["when"] == "the user submits the form"
        assert c.clauses["then"] == "persist the record."

    def test_acceptance_grammar_ears_ubiquitous_shall(self) -> None:
        clauses = parse_acceptance_grammar(["THE SYSTEM SHALL reject malformed ports."])
        assert len(clauses) == 1
        c = clauses[0]
        assert c.kind == "ears"
        assert c.clauses["shall"] == "reject malformed ports."
        assert c.clauses["then"] == "reject malformed ports."

    def test_acceptance_grammar_ears_if_then(self) -> None:
        clauses = parse_acceptance_grammar(
            ["IF the token is invalid, THEN the request is rejected."]
        )
        assert clauses[0].kind == "ears"
        assert clauses[0].clauses["if"] == "the token is invalid"
        assert clauses[0].clauses["then"] == "the request is rejected."

    def test_acceptance_grammar_ears_while_nested_when(self) -> None:
        clauses = parse_acceptance_grammar(
            [
                "WHILE the lease is active, WHEN a renew arrives, "
                "THE SYSTEM SHALL extend the lease."
            ]
        )
        c = clauses[0]
        assert c.kind == "ears"
        assert c.clauses["while"] == "the lease is active"
        assert c.clauses["when"] == "a renew arrives"
        assert c.clauses["then"] == "extend the lease."

    def test_acceptance_grammar_ears_where(self) -> None:
        clauses = parse_acceptance_grammar(
            ["WHERE the premium flag is set, THE SYSTEM SHALL show the upgrade banner."]
        )
        assert clauses[0].kind == "ears"
        assert clauses[0].clauses["where"] == "the premium flag is set"
        assert clauses[0].clauses["then"] == "show the upgrade banner."

    def test_acceptance_grammar_ears_is_case_insensitive(self) -> None:
        clauses = parse_acceptance_grammar(
            ["when the job finishes then a notification is sent"]
        )
        assert clauses[0].kind == "ears"
        assert clauses[0].clauses["when"] == "the job finishes"
        assert clauses[0].clauses["then"] == "a notification is sent"


# ---------------------------------------------------------------------------
# Gherkin recognition
# ---------------------------------------------------------------------------


class TestAcceptanceGrammarGherkin:
    """Gherkin Given/When/Then criteria parse into structured clauses."""

    def test_acceptance_grammar_gherkin_inline(self) -> None:
        clauses = parse_acceptance_grammar(
            [
                "Given a claimed task When the agent submits evidence "
                "Then the task enters needs_review."
            ]
        )
        assert len(clauses) == 1
        c = clauses[0]
        assert c.kind == "gherkin"
        assert c.clauses["given"] == "a claimed task"
        assert c.clauses["when"] == "the agent submits evidence"
        assert c.clauses["then"] == "the task enters needs_review."

    def test_acceptance_grammar_gherkin_multiline_collapses(self) -> None:
        """Given/When/Then written as separate bullets merge into one clause."""
        clauses = parse_acceptance_grammar(
            [
                "Given a fresh project",
                "When the PRD is parsed",
                "Then features and tasks are produced",
            ]
        )
        assert len(clauses) == 1
        c = clauses[0]
        assert c.kind == "gherkin"
        assert c.clauses["given"] == "a fresh project"
        assert c.clauses["when"] == "the PRD is parsed"
        assert c.clauses["then"] == "features and tasks are produced"
        # The original lines are preserved (newline-joined).
        assert "Given a fresh project" in c.text
        assert "Then features and tasks are produced" in c.text

    def test_acceptance_grammar_gherkin_and_continuation(self) -> None:
        clauses = parse_acceptance_grammar(
            [
                "Given a user with two open tasks",
                "When they claim one",
                "And the lease is granted",
                "Then the other stays in the ready queue",
            ]
        )
        assert len(clauses) == 1
        c = clauses[0]
        assert c.kind == "gherkin"
        assert "claim one" in c.clauses["when"]
        assert "lease is granted" in c.clauses["when"]
        assert c.clauses["then"] == "the other stays in the ready queue"

    def test_multiline_gherkin_stops_before_a_new_when_after_then(self) -> None:
        clauses = parse_acceptance_grammar(
            [
                "Given a draft exists",
                "When an editor publishes it",
                "Then its status is displayed",
                "When a report is missing, the system shall return not found",
            ]
        )

        assert [clause.kind for clause in clauses] == ["gherkin", "ears"]
        assert clauses[0].clauses["then"] == "its status is displayed"
        assert clauses[1].clauses["then"] == "return not found"

    def test_acceptance_grammar_gherkin_skips_scenario_noise(self) -> None:
        clauses = parse_acceptance_grammar(
            [
                "Scenario: happy path",
                "Given a user",
                "When they log in",
                "Then they see the dashboard",
            ]
        )
        # The Scenario: line carries no acceptance content and is dropped.
        assert len(clauses) == 1
        assert clauses[0].kind == "gherkin"
        assert clauses[0].clauses["given"] == "a user"


# ---------------------------------------------------------------------------
# Freeform fallback (default behavior preserved)
# ---------------------------------------------------------------------------


class TestAcceptanceGrammarFreeform:
    """Criteria without a structured grammar fall back to freeform, unchanged."""

    def test_acceptance_grammar_freeform_passthrough(self) -> None:
        criteria = [
            "Tests pass with 100% coverage.",
            "No regressions in existing tests.",
        ]
        clauses = parse_acceptance_grammar(criteria)
        assert len(clauses) == 2
        assert all(c.kind == "freeform" for c in clauses)
        assert all(c.clauses == {} for c in clauses)
        # Freeform clauses preserve the original text exactly.
        assert [c.text for c in clauses] == criteria

    def test_acceptance_grammar_empty_input(self) -> None:
        assert parse_acceptance_grammar([]) == []

    def test_acceptance_grammar_blank_lines_dropped(self) -> None:
        clauses = parse_acceptance_grammar(["", "   ", "A real criterion."])
        assert len(clauses) == 1
        assert clauses[0].kind == "freeform"
        assert clauses[0].text == "A real criterion."

    def test_acceptance_grammar_prose_with_when_then_is_freeform(self) -> None:
        """Mid-sentence 'when'/'then' prose is NOT misclassified as a grammar."""
        clauses = parse_acceptance_grammar(
            ["The cache is invalidated when a write occurs and then refreshed lazily."]
        )
        assert len(clauses) == 1
        assert clauses[0].kind == "freeform"

    def test_acceptance_grammar_when_without_response_is_freeform(self) -> None:
        """A sentence starting with 'When' but lacking a response stays freeform."""
        clauses = parse_acceptance_grammar(
            ["When in doubt, document the decision somewhere reasonable."]
        )
        assert clauses[0].kind == "freeform"

    def test_acceptance_grammar_never_raises_on_garbage(self) -> None:
        """The parser is total — odd input never raises."""
        weird = [
            "WHEN",
            "THEN",
            "Given",
            "WHILE WHEN THEN SHALL",
            "]]] [[[ random punctuation ((( )))",
            "When then when then when then",
        ]
        clauses = parse_acceptance_grammar(weird)
        # Every input yields exactly one clause; none raised.
        assert all(isinstance(c, AcceptanceClause) for c in clauses)


# ---------------------------------------------------------------------------
# Mixed criteria + integration with parse_prd
# ---------------------------------------------------------------------------


class TestAcceptanceGrammarMixed:
    """A criteria list mixing grammars and prose is decomposed per-item."""

    def test_acceptance_grammar_mixed_kinds(self) -> None:
        clauses = parse_acceptance_grammar(
            [
                "WHEN a port is malformed THEN the request is blocked.",
                "Plain prose criterion with no grammar.",
                "Given a config file When it is missing Then defaults apply.",
            ]
        )
        kinds = [c.kind for c in clauses]
        assert kinds == ["ears", "freeform", "gherkin"]

    def test_acceptance_grammar_from_parsed_prd_ears(self) -> None:
        """End-to-end: a PRD with EARS criteria parses, then decomposes."""
        prd = """\
# Project: Grammar PRD

## Summary

Exercise the EARS acceptance grammar end-to-end.

## Goals

- Recognize structured intent.

## Requirements

- R001: Structured criteria parse into clauses.

## Features

### F001: Grammar feature

**Requirements:** R001

## Tasks

### T001: Structured task

**Feature:** F001
**Acceptance criteria:**

- WHEN the user submits the form, THE SYSTEM SHALL persist the record.
- Given a saved record When the user reloads Then the record is shown.
"""
        result = parse_prd(prd)
        assert not result.errors, f"Unexpected errors: {result.errors}"
        assert len(result.tasks) == 1
        task = result.tasks[0]
        # Default behavior unchanged: raw strings still present.
        assert len(task.acceptance_criteria) == 2
        assert task.acceptance_criteria[0].startswith("WHEN ")

        clauses = parse_acceptance_grammar(task.acceptance_criteria)
        assert len(clauses) == 2
        assert clauses[0].kind == "ears"
        assert clauses[0].clauses["when"] == "the user submits the form"
        assert clauses[1].kind == "gherkin"
        assert clauses[1].clauses["then"] == "the record is shown."

    def test_acceptance_grammar_freeform_prd_unchanged(self) -> None:
        """A freeform PRD parses identically and yields only freeform clauses."""
        prd = """\
# Project: Freeform PRD

## Summary

Exercise the freeform fallback end-to-end.

## Goals

- Keep legacy criteria working.

## Requirements

- R001: Legacy freeform criteria still parse.

## Features

### F001: Legacy feature

**Requirements:** R001

## Tasks

### T001: Legacy task

**Feature:** F001
**Acceptance criteria:**

- Tests pass with full coverage.
- No regressions in existing tests.
"""
        result = parse_prd(prd)
        assert not result.errors, f"Unexpected errors: {result.errors}"
        task = result.tasks[0]
        # Raw acceptance_criteria are unchanged from pre-T028 behavior.
        assert task.acceptance_criteria == [
            "Tests pass with full coverage.",
            "No regressions in existing tests.",
        ]
        clauses = parse_acceptance_grammar(task.acceptance_criteria)
        assert [c.kind for c in clauses] == ["freeform", "freeform"]
        assert [c.text for c in clauses] == task.acceptance_criteria
