"""Tests for anvil.planning.template — the deterministic PRD parser.

All tests follow three rules:
1. parse_prd() NEVER raises — errors go into ParseResult.errors.
2. Missing required sections → non-empty ParseResult.errors.
3. Missing optional sections → empty lists, no errors.
"""

from __future__ import annotations

import datetime
from pathlib import Path

from anvil.clock import FrozenClock
from anvil.planning.template import ParseResult, parse_prd
from anvil.state.models import DEFAULT_PRD_ID

# ---------------------------------------------------------------------------
# Fixture path
# ---------------------------------------------------------------------------

_FIXTURES = Path(__file__).parent / "fixtures" / "prds"


# ---------------------------------------------------------------------------
# Minimal valid PRD helper
# ---------------------------------------------------------------------------

_MINIMAL_PRD = """\
# Project: Minimal Project

## Summary

A minimal project for testing.

## Goals

- Do something useful.

## Requirements

- R001: The system does X.
- R002: The system does Y.
"""

_QUICKSTART_PATH = _FIXTURES / "quickstart.md"


# ---------------------------------------------------------------------------
# Required-section enforcement
# ---------------------------------------------------------------------------


class TestRequiredSectionEnforcement:
    """parse_prd returns ParseError entries for missing required sections, never raises."""

    def test_missing_project_section_errors(self) -> None:
        """PRD without '# Project: X' → ParseError with '# Project' in message."""
        prd_without_project = """\
## Summary

A project without a title.

## Goals

- Do something.

## Requirements

- R001: Must work.
"""
        result = parse_prd(prd_without_project)
        assert result.errors, "Expected errors for missing # Project heading"
        sections = [e.section for e in result.errors]
        assert any("Project" in s for s in sections), (
            f"Expected '# Project' in error section names, got: {sections}"
        )

    def test_missing_summary_errors(self) -> None:
        """PRD without '## Summary' → ParseError with 'Summary' in message."""
        prd_without_summary = """\
# Project: Test Project

## Goals

- Goal one.

## Requirements

- R001: Must do X.
"""
        result = parse_prd(prd_without_summary)
        assert result.errors, "Expected errors for missing ## Summary section"
        sections = [e.section for e in result.errors]
        assert any("Summary" in s for s in sections), (
            f"Expected 'Summary' in error section names, got: {sections}"
        )

    def test_missing_goals_errors(self) -> None:
        """PRD without '## Goals' → ParseError with 'Goals' in message."""
        prd_without_goals = """\
# Project: Test Project

## Summary

A summary of the project.

## Requirements

- R001: Must do X.
"""
        result = parse_prd(prd_without_goals)
        assert result.errors, "Expected errors for missing ## Goals section"
        sections = [e.section for e in result.errors]
        assert any("Goals" in s for s in sections), (
            f"Expected 'Goals' in error section names, got: {sections}"
        )

    def test_missing_requirements_errors(self) -> None:
        """PRD without '## Requirements' → ParseError with 'Requirements' in message."""
        prd_without_requirements = """\
# Project: Test Project

## Summary

A summary.

## Goals

- Do something.
"""
        result = parse_prd(prd_without_requirements)
        assert result.errors, "Expected errors for missing ## Requirements section"
        sections = [e.section for e in result.errors]
        assert any("Requirements" in s for s in sections), (
            f"Expected 'Requirements' in error section names, got: {sections}"
        )

    def test_parse_prd_never_raises_on_missing_sections(self) -> None:
        """parse_prd does not raise even when all required sections are absent."""
        # Should collect multiple errors but never raise
        result = parse_prd("")
        assert isinstance(result, ParseResult)
        assert result.errors  # at minimum # Project is missing

    def test_all_four_required_sections_error_individually(self) -> None:
        """Verify each required section generates exactly one error each when absent."""
        # PRD with nothing — should get at least 3 or 4 errors (Project, Summary, Goals, Requirements).
        result = parse_prd("Some random text without any headings.")
        # At minimum we expect errors for # Project, ## Summary, ## Goals, ## Requirements
        assert len(result.errors) >= 3


# ---------------------------------------------------------------------------
# Happy path — minimal valid PRD
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_parse_minimal_valid_prd(self) -> None:
        """PRD with only the 4 required sections produces ParseResult with 2 Requirements."""
        result = parse_prd(_MINIMAL_PRD)
        assert not result.errors, f"Unexpected errors: {result.errors}"
        assert result.prd is not None
        assert len(result.requirements) >= 1
        # Optional sections absent → empty lists
        assert result.features == []
        assert result.tasks == []

    def test_parse_minimal_prd_has_correct_summary(self) -> None:
        """Minimal PRD summary is stored correctly."""
        result = parse_prd(_MINIMAL_PRD)
        assert not result.errors
        assert "minimal project" in result.prd.summary.lower()

    def test_parse_full_prd_quickstart(self) -> None:
        """Parse the quickstart example from docs/prd-template.md (fixture file).

        Asserts all sections parsed: 6 requirements, 3 features, 4 tasks,
        non-empty goals/risks/acceptance_criteria/open_questions.
        """
        markdown = _QUICKSTART_PATH.read_text(encoding="utf-8")
        result = parse_prd(markdown)
        assert not result.errors, f"Quickstart PRD parse errors: {result.errors}"

        # Requirements: R001-R006
        assert len(result.requirements) == 6
        req_ids = {r.id for r in result.requirements}
        assert "R001" in req_ids
        assert "R006" in req_ids

        # Features: F001, F002, F003
        assert len(result.features) == 3
        feat_ids = {f.id for f in result.features}
        assert "F001" in feat_ids
        assert "F003" in feat_ids

        # Tasks: T001-T004
        assert len(result.tasks) == 4
        task_ids = {t.id for t in result.tasks}
        assert "T001" in task_ids
        assert "T004" in task_ids

        # PRD-level fields populated
        assert result.prd.goals
        assert result.prd.non_goals
        assert result.prd.risks
        assert result.prd.open_questions
        assert result.prd.acceptance_criteria

    def test_ids_auto_assigned(self) -> None:
        """Requirements without prefix get R001, R002 in document order."""
        prd = """\
# Project: Auto ID Test

## Summary

Tests auto-assignment.

## Goals

- Do the thing.

## Requirements

- First requirement without ID.
- Second requirement without ID.
"""
        result = parse_prd(prd)
        assert not result.errors
        assert len(result.requirements) == 2
        assert result.requirements[0].id == "R001"
        assert result.requirements[1].id == "R002"

    def test_explicit_ids_preserved(self) -> None:
        """Requirements with explicit RNNN: prefix keep their IDs."""
        prd = """\
# Project: Explicit ID Test

## Summary

Tests explicit IDs.

## Goals

- Goal.

## Requirements

- R005: Fifth requirement.
- R010: Tenth requirement.
"""
        result = parse_prd(prd)
        assert not result.errors
        ids = [r.id for r in result.requirements]
        assert "R005" in ids
        assert "R010" in ids


# ---------------------------------------------------------------------------
# Feature parsing
# ---------------------------------------------------------------------------


class TestFeatureParsing:
    def test_feature_block_parsed(self) -> None:
        """### F001: Title with **Requirements:** R001, R002 populates Feature.requirements."""
        prd = """\
# Project: Feature Test

## Summary

Tests feature parsing.

## Goals

- Do features.

## Requirements

- R001: First requirement.
- R002: Second requirement.

## Features

### F001: My Feature

**Requirements:** R001, R002

A description of the feature.
"""
        result = parse_prd(prd)
        assert not result.errors, f"Unexpected errors: {result.errors}"
        assert len(result.features) == 1
        feature = result.features[0]
        assert feature.id == "F001"
        assert feature.title == "My Feature"
        assert "R001" in feature.requirements
        assert "R002" in feature.requirements

    def test_feature_with_unknown_requirement_warns(self) -> None:
        """**Requirements:** R099 when R099 doesn't exist → ParseError (warning), feature still included."""
        prd = """\
# Project: Unknown Req Test

## Summary

Tests unknown req reference.

## Goals

- Do something.

## Requirements

- R001: Known requirement.

## Features

### F001: Feature With Unknown Req

**Requirements:** R001, R099
"""
        result = parse_prd(prd)
        # Feature is still created (parser doesn't drop it)
        assert len(result.features) == 1
        assert result.features[0].id == "F001"
        # But there should be a warning/error about R099
        assert result.errors, "Expected a ParseError warning for unknown requirement R099"
        messages = [e.message for e in result.errors]
        assert any("R099" in m for m in messages), (
            f"Expected error mentioning R099, got: {messages}"
        )

    def test_feature_description_captured(self) -> None:
        """Description text in feature block is stored in Feature.description."""
        prd = """\
# Project: Desc Test

## Summary

Summary.

## Goals

- Goal.

## Requirements

- R001: Req.

## Features

### F001: Feature With Desc

**Requirements:** R001

This is a feature description paragraph.
"""
        result = parse_prd(prd)
        assert not result.errors
        assert len(result.features) == 1
        assert "description" in result.features[0].description.lower()

    def test_features_section_with_only_bullets_emits_error(self) -> None:
        """## Features section with bullets and no ### Fxxx: blocks → ParseError.

        Regression for the silent-drop bug: previously the parser returned
        features=[] with no error when a user wrote features as bullets, and
        the CLI reported '0 features' before exiting 0. The user's work was
        invisibly discarded.
        """
        prd = _MINIMAL_PRD + """
## Features

- F001: Single-file conversion. Covers the basic happy path.
- F002: Multi-file batch conversion.
- F003: Error handling and validation.
"""
        result = parse_prd(prd)
        assert result.features == [], (
            "No features should be returned when the section format is wrong"
        )
        assert result.errors, "Expected a ParseError for malformed Features section"
        feature_errors = [e for e in result.errors if e.section == "features"]
        assert feature_errors, (
            f"Expected at least one 'features' ParseError, got: {result.errors}"
        )
        assert any("### Fxxx:" in e.message for e in feature_errors), (
            "Error message should point users to the canonical '### Fxxx:' "
            f"format. Got: {[e.message for e in feature_errors]}"
        )

    def test_features_section_with_only_prose_emits_error(self) -> None:
        """## Features section with prose only (no H3 blocks) → ParseError."""
        prd = _MINIMAL_PRD + """
## Features

Features will be designed during the planning phase.
"""
        result = parse_prd(prd)
        assert result.features == []
        feature_errors = [e for e in result.errors if e.section == "features"]
        assert feature_errors, "Expected a ParseError for prose-only Features section"

    def test_empty_features_section_emits_no_error(self) -> None:
        """## Features section with no body content → no ParseError, no features.

        A user may intentionally include an empty section header; that should
        be silently accepted (it's just declaratively saying "no features
        yet"). The error fires only when content is *present* but ignored.
        """
        prd = _MINIMAL_PRD + """
## Features

"""
        result = parse_prd(prd)
        assert result.features == []
        feature_errors = [e for e in result.errors if e.section == "features"]
        assert not feature_errors, (
            f"Empty Features section should not emit errors, got: {feature_errors}"
        )

    def test_malformed_feature_id_prefix_emits_warning(self) -> None:
        """### F-DURABILITY style headings → ParseError warning + auto-assigned ID.

        Regression for the silent custom-ID drop: a user writes a heading that
        looks like an ID attempt (F + separator) but doesn't match Fxxx
        format. Previously the parser silently kept the heading as the title
        and auto-assigned F001. Now it warns so the user knows their custom
        ID was discarded.
        """
        prd = _MINIMAL_PRD + """
## Features

### F-DURABILITY: Replay-safe event log

**Requirements:** R001
"""
        result = parse_prd(prd)
        assert len(result.features) == 1, (
            "Feature should still be created with auto-assigned ID"
        )
        assert result.features[0].id == "F001", (
            "Auto-assigned ID should be F001 since the custom ID didn't parse"
        )
        warnings = [
            e for e in result.errors
            if e.section == "features" and "F-DURABILITY" in e.message
        ]
        assert warnings, (
            "Expected a ParseError warning mentioning the malformed "
            f"'F-DURABILITY' prefix. Got: {[e.message for e in result.errors]}"
        )


# ---------------------------------------------------------------------------
# Task parsing
# ---------------------------------------------------------------------------


class TestTaskParsing:
    def test_task_block_parsed(self) -> None:
        """### T001: Title with all fields populates Task correctly."""
        prd = """\
# Project: Task Parse Test

## Summary

Tests task parsing.

## Goals

- Do tasks.

## Requirements

- R001: Requirement.

## Features

### F001: Feature One

**Requirements:** R001

## Tasks

### T001: First Task

**Feature:** F001
**Priority:** high
**Likely files:** src/app/main.py, src/app/utils.py

Implement the main logic.

**Acceptance criteria:**

- Tests pass with 100% coverage.
- No regressions in existing tests.

**Verification:**

- `pytest tests/ -v`
- `python -m app --help`
"""
        result = parse_prd(prd)
        assert not result.errors, f"Unexpected errors: {result.errors}"
        assert len(result.tasks) == 1
        task = result.tasks[0]
        assert task.id == "T001"
        assert task.title == "First Task"
        assert task.feature_id == "F001"
        assert task.priority.value == "high"
        assert len(task.likely_files) == 2
        assert len(task.acceptance_criteria) == 2
        assert len(task.verification.commands) == 2

    def test_task_type_field_parsed(self) -> None:
        """### T001 with **Type:** bugfix sets task_type=bugfix (T015)."""
        prd = """\
# Project: Type Parse Test

## Summary

Tests task_type parsing.

## Goals

- Fix bugs.

## Requirements

- R001: Requirement.

## Features

### F001: Feature One

**Requirements:** R001

## Tasks

### T001: Fix the off-by-one

**Feature:** F001
**Type:** bugfix

Repair the boundary check.
"""
        result = parse_prd(prd)
        assert not result.errors, f"Unexpected errors: {result.errors}"
        assert result.tasks[0].task_type.value == "bugfix"

    def test_task_type_default_feature(self) -> None:
        """Task without **Type:** defaults to feature (backward compat)."""
        prd = """\
# Project: Type Default

## Summary

Tests default task_type.

## Goals

- Build features.

## Requirements

- R001: Requirement.

## Features

### F001: Feature One

**Requirements:** R001

## Tasks

### T001: Build a thing

**Feature:** F001

A task with no explicit type.
"""
        result = parse_prd(prd)
        assert not result.errors, f"Unexpected errors: {result.errors}"
        assert result.tasks[0].task_type.value == "feature"

    def test_task_type_unknown_value_warns_and_defaults(self) -> None:
        """An unknown **Type:** emits a ParseError and falls back to feature."""
        prd = """\
# Project: Type Unknown

## Summary

Tests unknown task_type.

## Goals

- Do work.

## Requirements

- R001: Requirement.

## Features

### F001: Feature One

**Requirements:** R001

## Tasks

### T001: Mystery task

**Feature:** F001
**Type:** chore

An unknown type.
"""
        result = parse_prd(prd)
        # Non-fatal: the task is still produced, defaulted to feature, and a
        # warning is recorded (parse_prd never raises).
        assert result.tasks[0].task_type.value == "feature"
        assert any(
            "unknown type" in e.message.lower() for e in result.errors
        ), f"expected an unknown-type warning, got {result.errors}"

    def test_task_priority_default_medium(self) -> None:
        """Task without **Priority:** defaults to medium."""
        prd = """\
# Project: Priority Default

## Summary

Tests default priority.

## Goals

- Do tasks.

## Requirements

- R001: Req.

## Features

### F001: Feature

**Requirements:** R001

## Tasks

### T001: Task Without Priority

**Feature:** F001

A task that has no explicit priority.

**Acceptance criteria:**

- It works.

**Verification:**

- `pytest tests/ -v`
"""
        result = parse_prd(prd)
        assert not result.errors
        assert len(result.tasks) == 1
        assert result.tasks[0].priority.value == "medium"

    def test_task_verification_strips_backticks(self) -> None:
        """- `pytest foo` → verification.commands = ["pytest foo"] (backticks stripped)."""
        prd = """\
# Project: Backtick Strip Test

## Summary

Tests backtick stripping in verification.

## Goals

- Do things.

## Requirements

- R001: Req.

## Features

### F001: Feature

**Requirements:** R001

## Tasks

### T001: Task With Backtick Verification

**Feature:** F001

**Acceptance criteria:**

- Works correctly.

**Verification:**

- `pytest tests/test_foo.py -v`
- `python -c "import app"`
"""
        result = parse_prd(prd)
        assert not result.errors
        assert len(result.tasks) == 1
        commands = result.tasks[0].verification.commands
        assert len(commands) == 2
        # Backticks must be stripped
        assert "`" not in commands[0], f"Backtick not stripped: {commands[0]}"
        assert "pytest tests/test_foo.py -v" in commands[0]

    def test_task_likely_files_comma_separated(self) -> None:
        """**Likely files:** a.py, b.py, c.py → list of 3 entries."""
        prd = """\
# Project: Likely Files Test

## Summary

Tests comma-separated likely files.

## Goals

- Do tasks.

## Requirements

- R001: Req.

## Features

### F001: Feature

**Requirements:** R001

## Tasks

### T001: Task With Files

**Feature:** F001
**Likely files:** a.py, b.py, c.py

**Acceptance criteria:**

- Works.

**Verification:**

- `pytest tests/ -v`
"""
        result = parse_prd(prd)
        assert not result.errors
        assert len(result.tasks) == 1
        files = result.tasks[0].likely_files
        assert len(files) == 3
        assert "a.py" in files
        assert "b.py" in files
        assert "c.py" in files

    def test_task_dependencies_field_parsed(self) -> None:
        """v1.16.0: **Dependencies:** T001, T002 → Task.dependencies = ['T001', 'T002'].

        Regression for a user-reported bug where the planner missed that
        T002 (chaos tests in 2-process mode) depended on T001 (HttpTransport).
        Before v1.16.0 the parser had no way to recognise an explicit
        **Dependencies:** field — the only path to populated dependencies
        was the file-subset inference, which can't catch semantic
        infrastructure dependencies.
        """
        prd = """\
# Project: Dependencies Test

## Summary

Tests dependency field parsing.

## Goals

- Test deps.

## Requirements

- R001: Req.

## Features

### F001: Feature

**Requirements:** R001

## Tasks

### T001: Implement HttpTransport

**Feature:** F001
**Acceptance criteria:**

- Works.

**Verification:**

- `pytest a`

### T002: Test HttpTransport in 2-process mode

**Feature:** F001
**Dependencies:** T001

**Acceptance criteria:**

- Tests pass.

**Verification:**

- `pytest b`
"""
        result = parse_prd(prd)
        assert not result.errors, f"Unexpected errors: {result.errors}"
        t002 = next(t for t in result.tasks if t.id == "T002")
        assert t002.dependencies == ["T001"], (
            f"Expected T002.dependencies == ['T001'], got: {t002.dependencies}"
        )
        # T001 has no Dependencies field — empty list.
        t001 = next(t for t in result.tasks if t.id == "T001")
        assert t001.dependencies == []

    def test_task_dependencies_multiple_normalized_uppercase(self) -> None:
        """**Dependencies:** t001, T002 → ['T001', 'T002'] (normalised)."""
        prd = """\
# Project: Multi-Dep Test

## Summary

x.

## Goals

- y.

## Requirements

- R001: z.

## Features

### F001: F

**Requirements:** R001

## Tasks

### T001: A

**Feature:** F001
**Acceptance criteria:**

- a

**Verification:**

- `c`

### T002: B

**Feature:** F001
**Acceptance criteria:**

- b

**Verification:**

- `c2`

### T003: C depends on A and B

**Feature:** F001
**Dependencies:** t001, T002

**Acceptance criteria:**

- c

**Verification:**

- `c3`
"""
        result = parse_prd(prd)
        assert not result.errors
        t003 = next(t for t in result.tasks if t.id == "T003")
        assert t003.dependencies == ["T001", "T002"]

    def test_task_dependencies_unknown_id_warns(self) -> None:
        """**Dependencies:** T099 when T099 doesn't exist → ParseError warning,
        dependency still kept on the task."""
        prd = """\
# Project: Unknown Dep Test

## Summary

x.

## Goals

- y.

## Requirements

- R001: z.

## Features

### F001: F

**Requirements:** R001

## Tasks

### T001: Has Bad Dep

**Feature:** F001
**Dependencies:** T099

**Acceptance criteria:**

- a

**Verification:**

- `c`
"""
        result = parse_prd(prd)
        # Dep still kept on the task.
        t001 = result.tasks[0]
        assert "T099" in t001.dependencies, (
            f"Unknown dep should still be kept, got: {t001.dependencies}"
        )
        # And a warning fired.
        dep_errors = [
            e for e in result.errors if "T099" in e.message
        ]
        assert dep_errors, (
            f"Expected ParseError mentioning T099. Got: {result.errors}"
        )

    def test_self_dependency_is_stripped_with_warning(self) -> None:
        """Greptile PR #64 fix: a task that lists itself in **Dependencies:**
        would create a perpetual claim-time warning (T001 can never be
        `done` before T001 is claimed). The parser strips the self-ref
        from the task's dependencies AND emits a ParseError warning
        naming the offending task."""
        prd = _MINIMAL_PRD + """
## Features

### F001: F

**Requirements:** R001

## Tasks

### T001: Self-referential

**Feature:** F001
**Dependencies:** T001

**Acceptance criteria:**

- a

**Verification:**

- `c`
"""
        result = parse_prd(prd)
        t001 = result.tasks[0]
        # Self-ref stripped — the parser does NOT keep "T001" in its own
        # dependencies, unlike the unknown-ID case which keeps the bad
        # value so downstream tooling can see the author's intent.
        assert "T001" not in t001.dependencies, (
            f"Self-dep should be stripped, got: {t001.dependencies}"
        )
        # And a clear warning fires.
        self_dep_errors = [
            e for e in result.errors
            if "T001" in e.message and "itself" in e.message
        ]
        assert self_dep_errors, (
            f"Expected self-dep ParseError. Got: {result.errors}"
        )

    def test_dependency_warning_carries_task_block_line(self) -> None:
        """Greptile PR #64 fix: ParseError for unknown dep ID points at the
        offending ### Txxx: block, NOT at the ## Tasks section header.
        Without per-task block_line tracking, users got pointed at line 1
        of the section regardless of which task held the bad reference.
        """
        prd = """\
# Project: Line Attribution Test

## Summary

x.

## Goals

- y.

## Requirements

- R001: z.

## Features

### F001: F

**Requirements:** R001

## Tasks

### T001: Clean task

**Feature:** F001
**Acceptance criteria:**

- a

**Verification:**

- `c`

### T002: Task with bad dep — should report THIS line

**Feature:** F001
**Dependencies:** T099

**Acceptance criteria:**

- b

**Verification:**

- `c2`
"""
        result = parse_prd(prd)
        # Find the unknown-dep warning.
        bad_dep_errors = [e for e in result.errors if "T099" in e.message]
        assert bad_dep_errors, "Expected unknown-dep warning"
        err = bad_dep_errors[0]
        # The ### T002: heading line. The exact number depends on the
        # prd layout above; what matters is that it is NOT the
        # ## Tasks section header line. Compute a rough lower bound:
        # ## Tasks header is at line 23 in this fixture; ### T002: must
        # be further down. We just assert > 25.
        assert err.line > 25, (
            f"Unknown-dep warning should point at the T002 block (line >25), "
            f"not the ## Tasks section header. Got: line={err.line}"
        )

    def test_task_dependencies_omitted_defaults_empty(self) -> None:
        """Task with no **Dependencies:** field → empty dependencies list."""
        prd = """\
# Project: No Deps Test

## Summary

x.

## Goals

- y.

## Requirements

- R001: z.

## Features

### F001: F

**Requirements:** R001

## Tasks

### T001: No deps

**Feature:** F001
**Acceptance criteria:**

- a

**Verification:**

- `c`
"""
        result = parse_prd(prd)
        assert result.tasks[0].dependencies == []

    def test_task_feature_link_updates_feature_tasks(self) -> None:
        """Task with Feature: F001 adds task ID to Feature.tasks."""
        prd = """\
# Project: Link Test

## Summary

Tests feature-task linking.

## Goals

- Link tasks to features.

## Requirements

- R001: Req.

## Features

### F001: Feature

**Requirements:** R001

## Tasks

### T001: Linked Task

**Feature:** F001

**Acceptance criteria:**

- Works.

**Verification:**

- `pytest tests/ -v`
"""
        result = parse_prd(prd)
        assert not result.errors
        feature = result.features[0]
        assert "T001" in feature.tasks

    def test_tasks_section_with_only_bullets_emits_error(self) -> None:
        """## Tasks section with bullets and no ### Txxx: blocks → ParseError.

        Mirror of the Features regression test: the parser used to silently
        drop bullets-only Tasks sections, leaving the user thinking their work
        was persisted.
        """
        prd = _MINIMAL_PRD + """
## Features

### F001: Single Feature

**Requirements:** R001

## Tasks

- T001: Implement argument parsing.
- T002: Implement core conversion.
- T003: Wire up the CLI entry point.
"""
        result = parse_prd(prd)
        assert result.tasks == [], "No tasks should be returned for malformed section"
        task_errors = [e for e in result.errors if e.section == "tasks"]
        assert task_errors, (
            f"Expected 'tasks' ParseError, got: {result.errors}"
        )
        assert any("### Txxx:" in e.message for e in task_errors), (
            "Error message should point to canonical '### Txxx:' format. "
            f"Got: {[e.message for e in task_errors]}"
        )

    def test_tasks_section_with_only_prose_emits_error(self) -> None:
        """## Tasks section with prose only (no H3 blocks) → ParseError.

        Parity with `test_features_section_with_only_prose_emits_error`.
        """
        prd = _MINIMAL_PRD + """
## Features

### F001: Single Feature

**Requirements:** R001

## Tasks

Tasks will be designed during the planning phase.
"""
        result = parse_prd(prd)
        assert result.tasks == []
        task_errors = [e for e in result.errors if e.section == "tasks"]
        assert task_errors, "Expected a ParseError for prose-only Tasks section"

    def test_empty_tasks_section_emits_no_error(self) -> None:
        """## Tasks section with no body → silent acceptance, no tasks."""
        prd = _MINIMAL_PRD + """
## Features

### F001: Single Feature

**Requirements:** R001

## Tasks

"""
        result = parse_prd(prd)
        assert result.tasks == []
        task_errors = [e for e in result.errors if e.section == "tasks"]
        assert not task_errors, (
            f"Empty Tasks section should not emit errors, got: {task_errors}"
        )

    def test_malformed_task_id_prefix_emits_warning(self) -> None:
        """### T-1 style headings → ParseError warning + auto-assigned ID."""
        prd = _MINIMAL_PRD + """
## Features

### F001: Single Feature

**Requirements:** R001

## Tasks

### T-1: Bootstrap the parser

**Feature:** F001
**Acceptance criteria:**

- Works.

**Verification:**

- `pytest tests/ -v`
"""
        result = parse_prd(prd)
        assert len(result.tasks) == 1
        assert result.tasks[0].id == "T001"
        warnings = [
            e for e in result.errors
            if e.section == "tasks" and "T-1" in e.message
        ]
        assert warnings, (
            f"Expected a warning about malformed 'T-1' prefix. "
            f"Got: {[e.message for e in result.errors]}"
        )


# ---------------------------------------------------------------------------
# Preprocessing
# ---------------------------------------------------------------------------


class TestPreprocessing:
    def test_html_comments_stripped(self) -> None:
        """HTML comments in PRD do not affect parsing."""
        prd = """\
# Project: Comment Test

<!-- This is a comment that should be stripped -->

## Summary

<!-- Another comment -->
A summary with comments.

## Goals

- Goal one.
<!-- Goal two is commented out -->

## Requirements

- R001: First requirement.
"""
        result = parse_prd(prd)
        assert not result.errors, f"Unexpected errors: {result.errors}"
        assert result.prd.summary
        # HTML comment text "This is a comment" should not appear in the summary
        assert "This is a comment" not in result.prd.summary
        assert "Another comment" not in result.prd.summary
        # Requirements should only have R001 (the comment was not parsed as a req)
        assert len(result.requirements) == 1

    def test_trailing_whitespace_tolerated(self) -> None:
        """Extra blank lines and trailing whitespace don't break parsing."""
        prd = """\
# Project: Whitespace Test

## Summary

   A summary with surrounding whitespace.

## Goals


- Goal one.

- Goal two.

## Requirements


- R001: First requirement.
- R002: Second requirement.

"""
        result = parse_prd(prd)
        assert not result.errors, f"Unexpected errors: {result.errors}"
        assert len(result.prd.goals) == 2
        assert len(result.requirements) == 2


# ---------------------------------------------------------------------------
# Reparse semantics
# ---------------------------------------------------------------------------


class TestReparseSemantrics:
    def test_reparse_replaces_requirements_completely(self) -> None:
        """Parse twice with different requirement lists; second parse's result contains only second list."""
        prd_v1 = """\
# Project: Reparse Test

## Summary

First version.

## Goals

- Goal.

## Requirements

- R001: Version 1 requirement A.
- R002: Version 1 requirement B.
- R003: Version 1 requirement C.
"""
        prd_v2 = """\
# Project: Reparse Test

## Summary

Second version.

## Goals

- Goal.

## Requirements

- R001: Version 2 requirement A (different text).
- R002: Version 2 requirement B (different text).
"""
        result_v1 = parse_prd(prd_v1)
        result_v2 = parse_prd(prd_v2)

        assert not result_v1.errors
        assert not result_v2.errors

        # Second result has only 2 requirements
        assert len(result_v2.requirements) == 2

        # First result's extra R003 is not in the second result
        v2_ids = {r.id for r in result_v2.requirements}
        assert "R003" not in v2_ids

        # Text is from second parse
        r001_v2 = next(r for r in result_v2.requirements if r.id == "R001")
        assert "Version 2" in r001_v2.text


# ---------------------------------------------------------------------------
# CL-11 regression: parse_prd must honour an injected Clock for task timestamps
# ---------------------------------------------------------------------------


class TestParsePrdClockInjection:
    """``_parse_tasks`` used to call ``datetime.datetime.now(UTC)`` directly,
    bypassing the project's Clock abstraction. That made parsed-task timestamps
    untestable without ``monkeypatch.setattr``. After CL-11 a Clock can be
    threaded through ``parse_prd(... clock=FrozenClock(...))`` and the
    resulting ``Task.created_at`` / ``Task.updated_at`` reflect the frozen
    instant exactly.
    """

    _PRD_WITH_TASK = """\
# Project: Clock Test

## Summary

Verify CL-11 Clock plumbing.

## Goals

- Inject a clock.

## Requirements

- R001: The parser respects a Clock.

## Features

### F001: Clocked Feature
**Requirements:** R001

A feature.

## Tasks

### T001: A Task
**Feature:** F001
**Priority:** medium

A task body.
"""

    def test_parse_prd_stamps_task_timestamps_from_injected_clock(self) -> None:
        frozen = datetime.datetime(2030, 6, 15, 12, 0, 0, tzinfo=datetime.UTC)
        clock = FrozenClock(frozen)

        result = parse_prd(self._PRD_WITH_TASK, clock=clock)

        assert not result.errors, f"unexpected parse errors: {result.errors}"
        assert len(result.tasks) == 1
        task = result.tasks[0]
        assert task.created_at == frozen, (
            f"CL-11: Task.created_at must use the injected clock, "
            f"got {task.created_at!r} expected {frozen!r}"
        )
        assert task.updated_at == frozen, (
            f"CL-11: Task.updated_at must use the injected clock, "
            f"got {task.updated_at!r} expected {frozen!r}"
        )

    def test_parse_prd_defaults_to_system_clock_when_clock_omitted(self) -> None:
        """Backwards compatibility: callers that omit clock still get a stamp."""
        before = datetime.datetime.now(datetime.UTC)
        result = parse_prd(self._PRD_WITH_TASK)
        after = datetime.datetime.now(datetime.UTC)

        assert not result.errors
        assert len(result.tasks) == 1
        task = result.tasks[0]
        # The default SystemClock stamps within the test's wall-clock window.
        assert before <= task.created_at <= after


# ---------------------------------------------------------------------------
# T015: prd_id is load-bearing — default PRD keeps BARE ids, named PRDs get a
# '<prd_id>:' prefix; bare cross-refs resolve within the same PRD; the Release
# marker round-trips into PRD.target_version / target_tag.
# ---------------------------------------------------------------------------


_FULL_PRD = """\
# Project: Multi-PRD Demo

## Summary

A project spanning features and tasks.

## Goals

- Ship the thing.

## Requirements

- R001: First requirement.

## Features

### F001: Feature One

**Requirements:** R001

## Tasks

### T001: First Task

**Feature:** F001
**Dependencies:** T002

### T002: Second Task

**Feature:** F001
"""


class TestPrdIdLoadBearing:
    """parse_prd(prd_id=...) controls id shape and cross-ref resolution."""

    def test_default_prd_id_yields_bare_ids(self) -> None:
        """Default prd_id keeps ids BARE — byte-identical to the legacy parser."""
        result = parse_prd(_FULL_PRD)
        assert not result.errors, result.errors
        assert result.requirements[0].id == "R001"
        assert result.features[0].id == "F001"
        assert {t.id for t in result.tasks} == {"T001", "T002"}
        # Default PRD stamps the model prd_id as DEFAULT_PRD_ID, never the
        # parse-time "prd" sentinel.
        assert result.requirements[0].prd_id == DEFAULT_PRD_ID
        assert result.features[0].prd_id == DEFAULT_PRD_ID
        assert result.tasks[0].prd_id == DEFAULT_PRD_ID
        assert result.prd.id == DEFAULT_PRD_ID

    def test_named_prd_id_prefixes_every_minted_id(self) -> None:
        """A named prd_id prefixes requirement/feature/task ids with '<id>:'."""
        result = parse_prd(_FULL_PRD, prd_id="v0.2")
        assert not result.errors, result.errors
        assert result.requirements[0].id == "v0.2:R001"
        assert result.features[0].id == "v0.2:F001"
        assert {t.id for t in result.tasks} == {"v0.2:T001", "v0.2:T002"}
        assert result.requirements[0].prd_id == "v0.2"
        assert result.features[0].prd_id == "v0.2"
        assert result.tasks[0].prd_id == "v0.2"
        assert result.prd.id == "v0.2"

    def test_named_prd_resolves_bare_cross_refs_without_warnings(self) -> None:
        """Bare **Feature:**/**Requirements:**/**Dependencies:** refs resolve
        against this PRD's prefixed ids — no unknown-ref warnings."""
        result = parse_prd(_FULL_PRD, prd_id="v0.2")
        assert not result.errors, result.errors
        # Feature's requirement cross-ref is prefixed and resolves.
        assert result.features[0].requirements == ["v0.2:R001"]
        # Task's feature cross-ref resolves.
        t1 = next(t for t in result.tasks if t.id == "v0.2:T001")
        assert t1.feature_id == "v0.2:F001"
        # Task dependency cross-ref resolves to the sibling task's prefixed id.
        assert t1.dependencies == ["v0.2:T002"]
        # Feature.tasks back-link carries the prefixed task ids.
        assert result.features[0].tasks == ["v0.2:T001", "v0.2:T002"]

    def test_author_written_prefixed_ids_parse_without_warnings(self) -> None:
        """Author may write ids already prefixed (v0.2:T001); no double-prefix,
        no warnings, cross-refs still resolve."""
        prd = """\
# Project: Prefixed Authoring

## Summary

Author wrote prefixed ids by hand.

## Goals

- g

## Requirements

- v0.2:R001: First requirement.

## Features

### v0.2:F001: Feature One

**Requirements:** v0.2:R001

## Tasks

### v0.2:T001: First Task

**Feature:** v0.2:F001
"""
        result = parse_prd(prd, prd_id="v0.2")
        assert not result.errors, result.errors
        assert result.requirements[0].id == "v0.2:R001"
        assert result.features[0].id == "v0.2:F001"
        assert result.tasks[0].id == "v0.2:T001"
        # No double-prefixing.
        assert "v0.2:v0.2:" not in result.tasks[0].id
        assert result.features[0].requirements == ["v0.2:R001"]
        assert result.tasks[0].feature_id == "v0.2:F001"

    def test_default_prd_mixed_bare_authoring_unchanged(self) -> None:
        """Sanity: default-PRD parse output is byte-identical regardless of the
        new prd_id plumbing (no prefixes ever appear)."""
        result = parse_prd(_FULL_PRD, prd_id="prd")
        assert not result.errors, result.errors
        assert result.features[0].requirements == ["R001"]
        t1 = next(t for t in result.tasks if t.id == "T001")
        assert t1.feature_id == "F001"
        assert t1.dependencies == ["T002"]


class TestReleaseRoundTrip:
    """A Release marker round-trips into PRD.target_version / target_tag."""

    def test_release_field_line_in_summary(self) -> None:
        """**Release:** v0.2.0 (tag: v0.2) sets both fields; line is kept out
        of the summary prose."""
        prd = """\
# Project: Release Test

## Summary

A release-tagged project.
**Release:** v0.2.0 (tag: v0.2)

## Goals

- g

## Requirements

- R001: req.
"""
        result = parse_prd(prd)
        assert not result.errors, result.errors
        assert result.prd.target_version == "v0.2.0"
        assert result.prd.target_tag == "v0.2"
        # The release marker is not folded into the summary prose.
        assert "Release" not in result.prd.summary
        assert result.prd.summary == "A release-tagged project."

    def test_release_field_version_only(self) -> None:
        """A bare **Release:** value sets target_version; tag stays None."""
        prd = """\
# Project: Release Test

## Summary

Summary.
**Release:** v1.0.0

## Goals

- g

## Requirements

- R001: req.
"""
        result = parse_prd(prd)
        assert not result.errors, result.errors
        assert result.prd.target_version == "v1.0.0"
        assert result.prd.target_tag is None

    def test_release_field_bare_paren_tag(self) -> None:
        """A bare parenthetical (no 'tag:' keyword) sets target_tag too:
        **Release:** v0.2.0 (v0.2) → version=v0.2.0, tag=v0.2."""
        prd = """\
# Project: Release Test

## Summary

Summary.
**Release:** v0.2.0 (v0.2)

## Goals

- g

## Requirements

- R001: req.
"""
        result = parse_prd(prd)
        assert not result.errors, result.errors
        assert result.prd.target_version == "v0.2.0"
        assert result.prd.target_tag == "v0.2"

    def test_release_section(self) -> None:
        """A dedicated ## Release section with **Version:**/**Tag:** fields."""
        prd = """\
# Project: Release Section Test

## Summary

Summary.

## Goals

- g

## Requirements

- R001: req.

## Release

**Version:** v0.3.0
**Tag:** v0.3
"""
        result = parse_prd(prd)
        assert not result.errors, result.errors
        assert result.prd.target_version == "v0.3.0"
        assert result.prd.target_tag == "v0.3"

    def test_release_absent_yields_none(self) -> None:
        """No Release marker => target_version/target_tag both None."""
        result = parse_prd(_MINIMAL_PRD)
        assert not result.errors, result.errors
        assert result.prd.target_version is None
        assert result.prd.target_tag is None
