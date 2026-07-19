"""Tests for the deterministic, advisory PRD readiness assessor."""

from __future__ import annotations

import pytest

from anvil.planning.behavioral_readiness import (
    assess_behavioral_readiness,
    findings_as_dicts,
)
from anvil.planning.template import ParseResult, parse_prd

_GAPPED_PRD = """\
# Project: Internal migration

## Summary

Move the data.

## Goals

- Make migration better and reliable.

## Requirements

- R001: Implement a database migration.

## Acceptance Criteria

- It works well.

## Features

### F001: Migration

**Requirements:** R001

Move the data.

## Tasks

### T001: Implement migration

**Feature:** F001

Implement it.
"""


_BEHAVIOURAL_PRD = """\
# Project: Report publishing

## Summary

An editor publishes a report for readers from a draft workspace.

## Goals

- Allow an editor to publish a report and see its published status.

## Non-Goals

- Add multi-region replication.

## Requirements

- R001: When an editor publishes a draft, the system shall make the report visible.

## Acceptance Criteria

- Given an editor with a draft report When they publish it Then the status is visible as published.
- When a report is missing, the system shall return a not found response.

## Risks

- A dependent publishing service may be unavailable.

## Features

### F001: Publishing

**Requirements:** R001

Publish reports.

## Tasks

### T001: Publish a report

**Feature:** F001

**Acceptance criteria:**

- When an editor publishes a report, the system shall show its published status.

**Verification:**

- `pytest -q`

Publish the report.
"""


def test_assessment_reports_each_readiness_category_in_stable_order() -> None:
    result = parse_prd(_GAPPED_PRD)
    assert not result.errors
    before = result.prd.model_dump(mode="json")

    findings = assess_behavioral_readiness(result)

    assert [finding.id for finding in findings] == sorted(finding.id for finding in findings)
    categories = {finding.category for finding in findings}
    assert {
        "user_context",
        "outcome_clarity",
        "scope_boundary",
        "risk_boundary",
        "implementation_led_requirement",
        "unstructured_acceptance",
        "non_observable_acceptance",
        "failure_boundary",
        "task_verification",
    } <= categories
    assert result.prd.model_dump(mode="json") == before


def test_structured_criteria_remain_valid_and_do_not_get_structure_warning() -> None:
    result = parse_prd(_BEHAVIOURAL_PRD)
    assert not result.errors

    findings = assess_behavioral_readiness(result)

    assert not any(f.category == "unstructured_acceptance" for f in findings)
    assert findings_as_dicts(findings) == [finding.__dict__ for finding in findings]


def test_acceptance_finding_ids_survive_unrelated_prd_clarifications() -> None:
    base = parse_prd(_GAPPED_PRD)
    clarified = parse_prd(
        _GAPPED_PRD.replace(
            "## Requirements",
            """## Non-Goals

- Replace the database engine.

## Risks

- A failed migration may require rollback.

## Requirements""",
        )
    )
    criterion_inserted = parse_prd(
        _GAPPED_PRD.replace(
            "- It works well.",
            "- Given a completed run When viewed Then its status is displayed.\n"
            "- It works well.",
        )
    )
    assert not base.errors
    assert not clarified.errors
    assert not criterion_inserted.errors

    def acceptance_ids(result: ParseResult) -> set[str]:
        return {
            finding.id
            for finding in assess_behavioral_readiness(result)
            if finding.id.startswith("BR-AC-")
        }

    assert acceptance_ids(base) == acceptance_ids(clarified)
    assert acceptance_ids(base) == acceptance_ids(criterion_inserted)


def test_each_weak_acceptance_criterion_is_reported_independently() -> None:
    markdown = _BEHAVIOURAL_PRD.replace(
        "- When a report is missing, the system shall return a not found response.",
        """- When a report is missing, the system shall return a not found response.
- It works well.
- Easy and reliable.""",
    )
    result = parse_prd(markdown)
    assert not result.errors

    findings = assess_behavioral_readiness(result)
    weak_global = [
        finding
        for finding in findings
        if finding.location.startswith("## Acceptance Criteria [")
        and finding.location != "## Acceptance Criteria [1]"
        and finding.location != "## Acceptance Criteria [2]"
    ]

    assert {finding.location for finding in weak_global} == {
        "## Acceptance Criteria [3]",
        "## Acceptance Criteria [4]",
    }
    assert {finding.category for finding in weak_global} == {
        "unstructured_acceptance",
        "non_observable_acceptance",
    }


def test_implementation_and_observable_verb_forms_do_not_mask_each_other() -> None:
    implementation_led = parse_prd(
        _GAPPED_PRD.replace(
            "- R001: Implement a database migration.",
            "- R001: Create a SQLite database table.",
        )
    )
    passive_observable = parse_prd(
        _BEHAVIOURAL_PRD.replace(
            "- When a report is missing, the system shall return a not found response.",
            "- When publishing completes, the status is displayed.",
        )
    )
    assert not implementation_led.errors
    assert not passive_observable.errors

    assert any(
        finding.category == "implementation_led_requirement"
        for finding in assess_behavioral_readiness(implementation_led)
    )
    assert not any(
        finding.category == "non_observable_acceptance"
        and finding.location == "## Acceptance Criteria [2]"
        for finding in assess_behavioral_readiness(passive_observable)
    )


def test_multibullet_gherkin_is_assessed_as_one_structured_scenario() -> None:
    result = parse_prd(
        _BEHAVIOURAL_PRD.replace(
            "- Given an editor with a draft report When they publish it Then the status is visible as published.\n"
            "- When a report is missing, the system shall return a not found response.",
            "- Given an editor with a draft report\n"
            "- When they publish it\n"
            "- Then the status is visible as published\n"
            "- When a report is missing, the system shall return a not found response.",
        )
    )
    assert not result.errors

    findings = assess_behavioral_readiness(result)

    assert not any(
        finding.location in {
            "## Acceptance Criteria [1]",
            "## Acceptance Criteria [2]",
            "## Acceptance Criteria [3]",
        }
        and finding.category in {"unstructured_acceptance", "non_observable_acceptance"}
        for finding in findings
    )


def test_structured_scenario_observability_uses_then_response() -> None:
    result = parse_prd(
        _BEHAVIOURAL_PRD.replace(
            "- Given an editor with a draft report When they publish it Then the status is visible as published.\n"
            "- When a report is missing, the system shall return a not found response.",
            "- Scenario: publish\n"
            "- Given a report exists\n"
            "- When an operator publishes the report\n"
            "- Then it works well\n"
            "- When a report is missing, the system shall return a not found response.",
        )
    )
    assert not result.errors

    findings = assess_behavioral_readiness(result)

    assert any(
        finding.category == "non_observable_acceptance"
        and finding.location == "## Acceptance Criteria [4]"
        for finding in findings
    )


def test_repeated_gherkin_setup_reports_the_vague_response_location() -> None:
    scenarios = """- Scenario: concrete
- Given an operator has a draft
- When the operator publishes it
- Then its status is displayed
- Scenario: vague
- Given an operator has a draft
- When the operator publishes it again
- Then it works well"""
    result = parse_prd(
        _BEHAVIOURAL_PRD.replace(
            "- Given an editor with a draft report When they publish it Then the status is visible as published.\n"
            "- When a report is missing, the system shall return a not found response.",
            scenarios,
        )
    )
    assert not result.errors

    findings = [
        finding
        for finding in assess_behavioral_readiness(result)
        if finding.category == "non_observable_acceptance"
    ]

    assert [finding.location for finding in findings] == ["## Acceptance Criteria [8]"]


def test_user_observable_view_language_is_recognized() -> None:
    result = parse_prd(
        _BEHAVIOURAL_PRD.replace(
            "- When a report is missing, the system shall return a not found response.",
            "- When a reader opens a dashboard Then the reader can view its contents.",
        )
    )
    assert not result.errors

    assert not any(
        finding.category == "non_observable_acceptance"
        and finding.location == "## Acceptance Criteria [2]"
        for finding in assess_behavioral_readiness(result)
    )


def test_technical_output_without_user_context_remains_implementation_led() -> None:
    result = parse_prd(
        _GAPPED_PRD.replace(
            "- R001: Implement a database migration.",
            "- R001: Implement a SQLite migration that returns status 0.",
        )
    )
    assert not result.errors

    assert any(
        finding.category == "implementation_led_requirement"
        for finding in assess_behavioral_readiness(result)
    )


def test_assessment_handles_malformed_prd_without_throwing() -> None:
    result = parse_prd("# Project: Incomplete")
    assert result.errors

    # Assessment is intentionally safe on the parser's partial result; CLI/MCP
    # surfaces parse errors first, but callers can still inspect the pure helper.
    assert isinstance(assess_behavioral_readiness(result), list)


def test_typed_assumptions_parse_and_scope_to_named_requirement_ids() -> None:
    markdown = _BEHAVIOURAL_PRD.replace(
        "## Features",
        """## Assumptions

### A001: Publishing is private by default.

**Rationale:** This is a reversible first-release default.

**Requirements:** R001

## Features""",
    )

    result = parse_prd(markdown, prd_id="v0.2")

    assert not result.errors
    assert result.prd.assumptions[0].id == "A001"
    assert result.prd.assumptions[0].requirement_ids == ["v0.2:R001"]


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("**Requirement:** R001", "unknown field"),
        ("**Requirements:**", "empty"),
    ],
)
def test_assumption_requirement_fields_fail_closed(
    field: str, expected: str
) -> None:
    markdown = _BEHAVIOURAL_PRD.replace(
        "## Features",
        f"""## Assumptions

### A001: Publishing is private by default.

**Rationale:** This is a reversible first-release default.

{field}

## Features""",
    )

    result = parse_prd(markdown)

    assert any(expected in error.message for error in result.errors)
    assert result.prd.assumptions == []


def test_assumption_id_requires_ascii_digits() -> None:
    markdown = _BEHAVIOURAL_PRD.replace(
        "## Features",
        """## Assumptions

### A٠٠١: Publishing is private by default.

**Rationale:** This is a reversible first-release default.

## Features""",
    )

    result = parse_prd(markdown)

    assert any("A###" in error.message for error in result.errors)


def test_assumption_id_length_is_bounded() -> None:
    markdown = _BEHAVIOURAL_PRD.replace(
        "## Features",
        f"""## Assumptions

### A{'1' * 32}: Publishing is private by default.

**Rationale:** This is a reversible first-release default.

## Features""",
    )

    result = parse_prd(markdown)

    assert any("A###" in error.message for error in result.errors)
    assert result.prd.assumptions == []


def test_assumption_count_is_bounded_before_packet_propagation() -> None:
    blocks = "\n\n".join(
        f"### A{index:03}: bounded\n\n**Rationale:** reversible"
        for index in range(1, 102)
    )
    result = parse_prd(
        _BEHAVIOURAL_PRD.replace(
            "## Features", f"## Assumptions\n\n{blocks}\n\n## Features"
        )
    )

    assert any("limited to 100" in error.message for error in result.errors)
    assert result.prd.assumptions == []


@pytest.mark.parametrize(
    "extra",
    [
        "**Rationale:** first\n\n**Rationale:** second",
        "**Requirements:** R001\n\n**Requirement references:** R001",
        "Unlabelled prose that would otherwise disappear.",
    ],
)
def test_assumption_blocks_reject_ambiguous_or_unlabelled_content(extra: str) -> None:
    rationale = "" if extra.startswith("**Rationale:**") else "**Rationale:** reversible\n\n"
    markdown = _BEHAVIOURAL_PRD.replace(
        "## Features",
        f"""## Assumptions

### A001: Publishing is private by default.

{rationale}{extra}

## Features""",
    )

    result = parse_prd(markdown)

    assert result.errors
    assert result.prd.assumptions == []
