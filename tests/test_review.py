"""Tests for anvil.review.gates — review gate functions.

Coverage targets (>= 90%):
- evidence_complete() — all decision branches
- Case-insensitive matching
- Multiple required items — some missing, some satisfied
"""

from __future__ import annotations

from datetime import UTC, datetime

from anvil.review.gates import (
    DeferredFinding,
    _contains_test_keyword,
    deferred_findings,
    deferred_findings_for_files,
    evidence_complete,
)
from anvil.state.models import (
    Evidence,
    Review,
    ReviewDecision,
    ReviewTargetKind,
    Score,
    Task,
    TaskPriority,
    TaskStatus,
    Verification,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UTC = UTC
_T0 = datetime(2026, 5, 24, 18, 0, 0, tzinfo=_UTC)


def _make_task(
    *,
    required_evidence: list[str] | None = None,
    task_id: str = "T001",
) -> Task:
    return Task(
        id=task_id,
        feature_id="F001",
        title="Test Task",
        description="A test task.",
        status=TaskStatus.needs_review,
        priority=TaskPriority.medium,
        acceptance_criteria=["Tests pass."],
        implementation_notes=[],
        verification=Verification(
            commands=["pytest tests/ -v"],
            manual_steps=[],
            required_evidence=required_evidence or [],
        ),
        likely_files=[],
        scores=Score(),
        created_at=_T0,
        updated_at=_T0,
    )


def _make_evidence(
    *,
    commands_run: list[str] | None = None,
    files_changed: list[str] | None = None,
    output_excerpt: str | None = None,
    pr_url: str | None = None,
    commit_sha: str | None = None,
    screenshots: list[str] | None = None,
    known_limitations: str | None = None,
) -> Evidence:
    return Evidence(
        id="EV001",
        task_id="T001",
        claim_id="C001",
        commands_run=["pytest tests/ -v"] if commands_run is None else commands_run,
        files_changed=["src/auth.py"] if files_changed is None else files_changed,
        output_excerpt=output_excerpt,
        pr_url=pr_url,
        commit_sha=commit_sha,
        screenshots=[] if screenshots is None else screenshots,
        known_limitations=known_limitations,
        submitted_at=_T0,
        submitted_by="agent-alpha",
    )


# ===========================================================================
# TestEvidenceComplete
# ===========================================================================


class TestPlaceholderEvidence:
    """#108.2 — a required-evidence string carrying an ``<...>`` placeholder is
    matched as a wildcard against the concrete evidence, not literally, so a
    generated ``<date>``-style requirement is satisfiable."""

    _REQ = "evidence captured in `docs/findings/<date>-openclaw-keyless-failover.md`"

    def test_placeholder_matches_concrete_value(self) -> None:
        task = _make_task(required_evidence=[self._REQ])
        evidence = _make_evidence(
            output_excerpt=(
                "gateway ok; evidence captured in "
                "`docs/findings/2026-07-04-openclaw-keyless-failover.md`. done"
            ),
        )
        passed, missing = evidence_complete(task, evidence)
        assert passed is True, missing
        assert missing == []

    def test_placeholder_unsatisfied_when_surrounding_text_absent(self) -> None:
        task = _make_task(required_evidence=[self._REQ])
        evidence = _make_evidence(output_excerpt="totally unrelated output")
        passed, missing = evidence_complete(task, evidence)
        assert passed is False
        assert self._REQ in missing

    def test_no_placeholder_keeps_exact_substring_semantics(self) -> None:
        req = "captured in docs/findings/report.md"
        task = _make_task(required_evidence=[req])
        hit = _make_evidence(output_excerpt="see: captured in docs/findings/report.md here")
        assert evidence_complete(task, hit)[0] is True
        # A different filename must NOT be wildcard-matched (no placeholder).
        miss = _make_evidence(output_excerpt="captured in docs/findings/other.md")
        passed, missing = evidence_complete(task, miss)
        assert passed is False and req in missing

    def test_multiple_placeholders_each_wildcard(self) -> None:
        task = _make_task(required_evidence=["wrote <count> rows to <table>"])
        evidence = _make_evidence(known_limitations="wrote 42 rows to users_v2 ok")
        assert evidence_complete(task, evidence)[0] is True

    def test_placeholder_matched_in_known_limitations(self) -> None:
        task = _make_task(required_evidence=[self._REQ])
        evidence = _make_evidence(
            output_excerpt=None,
            known_limitations=(
                "evidence captured in "
                "`docs/findings/2026-07-04-openclaw-keyless-failover.md`"
            ),
        )
        assert evidence_complete(task, evidence)[0] is True


class TestEvidenceComplete:
    def test_no_required_evidence_passes(self) -> None:
        """task.verification.required_evidence == [] → (True, [])."""
        task = _make_task(required_evidence=[])
        evidence = _make_evidence()
        passed, missing = evidence_complete(task, evidence)
        assert passed is True
        assert missing == []

    def test_test_output_requirement_matched_by_pytest_command(self) -> None:
        """required = ['test output']; commands_run = ['pytest -x'] → passes."""
        task = _make_task(required_evidence=["test output"])
        evidence = _make_evidence(commands_run=["pytest -x"])
        passed, missing = evidence_complete(task, evidence)
        assert passed is True
        assert missing == []

    def test_pr_link_requirement_matched_by_pr_url(self) -> None:
        """required = ['PR link']; pr_url set → passes."""
        task = _make_task(required_evidence=["PR link"])
        evidence = _make_evidence(pr_url="https://github.com/repo/pull/42")
        passed, missing = evidence_complete(task, evidence)
        assert passed is True
        assert missing == []

    def test_pr_link_matched_by_pull_request_keyword(self) -> None:
        """required = ['pull request link']; pr_url set → passes (PR synonym)."""
        task = _make_task(required_evidence=["pull request link"])
        evidence = _make_evidence(pr_url="https://github.com/repo/pull/99")
        passed, missing = evidence_complete(task, evidence)
        assert passed is True
        assert missing == []

    def test_screenshots_requirement_matched_by_screenshots_list(self) -> None:
        """required = ['screenshots']; screenshots non-empty → passes."""
        task = _make_task(required_evidence=["screenshots"])
        evidence = _make_evidence(screenshots=["screenshot1.png"])
        passed, missing = evidence_complete(task, evidence)
        assert passed is True
        assert missing == []

    def test_screenshots_requirement_fails_when_empty_list(self) -> None:
        """required = ['screenshots']; screenshots == [] → fails."""
        task = _make_task(required_evidence=["screenshots"])
        evidence = _make_evidence(screenshots=[])
        passed, missing = evidence_complete(task, evidence)
        assert passed is False
        assert "screenshots" in missing

    def test_files_changed_requirement_matched_when_non_empty(self) -> None:
        """required = ['files changed']; files_changed non-empty → passes."""
        task = _make_task(required_evidence=["files changed"])
        evidence = _make_evidence(files_changed=["src/main.py"])
        passed, missing = evidence_complete(task, evidence)
        assert passed is True
        assert missing == []

    def test_files_changed_requirement_fails_when_empty(self) -> None:
        """required = ['files changed']; files_changed == [] → fails."""
        task = _make_task(required_evidence=["files changed"])
        evidence = _make_evidence(files_changed=[])
        passed, missing = evidence_complete(task, evidence)
        assert passed is False
        assert "files changed" in missing

    def test_generic_requirement_matched_by_output_excerpt(self) -> None:
        """required = ['integration test coverage']; appears in output_excerpt → passes."""
        task = _make_task(required_evidence=["integration test coverage"])
        evidence = _make_evidence(
            output_excerpt="Integration test coverage at 92%. All green."
        )
        passed, missing = evidence_complete(task, evidence)
        assert passed is True
        assert missing == []

    def test_generic_requirement_matched_by_known_limitations(self) -> None:
        """required = ['performance benchmark']; appears in known_limitations → passes."""
        task = _make_task(required_evidence=["performance benchmark"])
        evidence = _make_evidence(
            known_limitations="No performance benchmark run; deferred to next sprint."
        )
        passed, missing = evidence_complete(task, evidence)
        assert passed is True
        assert missing == []

    def test_missing_requirements_returned_in_list(self) -> None:
        """required = ['test output', 'PR link']; commands_run = [] AND pr_url = None
        → (False, ['test output', 'PR link']).
        """
        task = _make_task(required_evidence=["test output", "PR link"])
        evidence = _make_evidence(
            commands_run=["echo hello"],  # not a test runner
            pr_url=None,
            files_changed=["src/foo.py"],
        )
        passed, missing = evidence_complete(task, evidence)
        assert passed is False
        assert "test output" in missing
        assert "PR link" in missing

    def test_partial_match_one_missing(self) -> None:
        """required = ['test output', 'PR link']; only test matched → one missing."""
        task = _make_task(required_evidence=["test output", "PR link"])
        evidence = _make_evidence(
            commands_run=["pytest tests/ -v"],
            pr_url=None,
        )
        passed, missing = evidence_complete(task, evidence)
        assert passed is False
        assert "test output" not in missing
        assert "PR link" in missing

    def test_substring_matching_case_insensitive(self) -> None:
        """required = ['TEST output'] matched by commands_run = ['pytest'] (case-insensitive)."""
        task = _make_task(required_evidence=["TEST output"])
        evidence = _make_evidence(commands_run=["pytest"])
        passed, missing = evidence_complete(task, evidence)
        assert passed is True
        assert missing == []

    def test_cargo_test_matches_test_requirement(self) -> None:
        """'cargo test' in commands_run satisfies a 'test' requirement."""
        task = _make_task(required_evidence=["test output"])
        evidence = _make_evidence(commands_run=["cargo test --workspace"])
        passed, missing = evidence_complete(task, evidence)
        assert passed is True
        assert missing == []

    def test_uv_run_pytest_matches_test_requirement(self) -> None:
        """'uv run pytest' in commands_run satisfies a 'pytest' requirement."""
        task = _make_task(required_evidence=["pytest"])
        evidence = _make_evidence(commands_run=["uv run pytest -q"])
        passed, missing = evidence_complete(task, evidence)
        assert passed is True
        assert missing == []

    def test_pr_requirement_fails_when_pr_url_none(self) -> None:
        """required = ['PR link']; pr_url=None → fails."""
        task = _make_task(required_evidence=["PR link"])
        evidence = _make_evidence(pr_url=None)
        passed, missing = evidence_complete(task, evidence)
        assert passed is False
        assert "PR link" in missing

    def test_multiple_requirements_all_satisfied(self) -> None:
        """All three requirement types satisfied simultaneously."""
        task = _make_task(required_evidence=["test output", "PR link", "files changed"])
        evidence = _make_evidence(
            commands_run=["pytest tests/ -v"],
            pr_url="https://github.com/repo/pull/5",
            files_changed=["src/foo.py"],
        )
        passed, missing = evidence_complete(task, evidence)
        assert passed is True
        assert missing == []

    def test_empty_commands_run_fails_test_requirement(self) -> None:
        """If commands_run is empty, test-related requirements fail."""
        task = _make_task(required_evidence=["test output"])
        evidence = _make_evidence(commands_run=[])
        passed, missing = evidence_complete(task, evidence)
        assert passed is False
        assert "test output" in missing

    def test_generic_requirement_fails_when_no_corpus(self) -> None:
        """Generic requirement fails when output_excerpt and known_limitations are both None.

        Note: the requirement string must NOT contain 'test', 'PR', 'screenshot',
        or 'files changed' to fall through to the generic corpus check path.
        'load benchmark results' has no such keywords.
        """
        task = _make_task(required_evidence=["load benchmark results"])
        evidence = _make_evidence(
            output_excerpt=None,
            known_limitations=None,
        )
        passed, missing = evidence_complete(task, evidence)
        assert passed is False
        assert "load benchmark results" in missing

    def test_returns_tuple_of_bool_and_list(self) -> None:
        """Return type is (bool, list[str]) in both pass and fail cases."""
        task_pass = _make_task(required_evidence=[])
        task_fail = _make_task(required_evidence=["something"])
        ev = _make_evidence()

        result_pass = evidence_complete(task_pass, ev)
        result_fail = evidence_complete(task_fail, ev)

        assert isinstance(result_pass, tuple)
        assert len(result_pass) == 2
        assert isinstance(result_pass[0], bool)
        assert isinstance(result_pass[1], list)

        assert isinstance(result_fail, tuple)
        assert isinstance(result_fail[0], bool)
        assert isinstance(result_fail[1], list)


# ---------------------------------------------------------------------------
# CL-9 regression: collection-only invocations must NOT satisfy "test ran" gate
# ---------------------------------------------------------------------------


class TestContainsTestKeywordCollectionOnly:
    """`pytest --collect-only` exits 0 but runs zero tests; must NOT count."""

    def test_pytest_runs_tests(self) -> None:
        assert _contains_test_keyword("pytest tests/")

    def test_pytest_collect_only_rejected(self) -> None:
        assert not _contains_test_keyword("pytest --collect-only tests/")

    def test_pytest_co_short_form_rejected(self) -> None:
        assert not _contains_test_keyword("pytest --co tests/")

    def test_pytest_collect_only_at_end_rejected(self) -> None:
        assert not _contains_test_keyword("pytest tests/ --collect-only")

    def test_pytest_co_at_end_rejected(self) -> None:
        assert not _contains_test_keyword("pytest tests/ --co")

    def test_uv_run_pytest_collect_only_rejected(self) -> None:
        assert not _contains_test_keyword("uv run pytest --collect-only")

    def test_pytest_color_flag_NOT_rejected(self) -> None:
        """Greptile + critic PR #48 P1: `--co` substring must not match `--color`."""
        assert _contains_test_keyword("pytest --color=no tests/")

    def test_pytest_color_yes_NOT_rejected(self) -> None:
        assert _contains_test_keyword("pytest tests/ --color=yes")

    def test_pytest_cov_NOT_rejected(self) -> None:
        """`--cov` must not be confused with `--co`."""
        assert _contains_test_keyword("pytest --cov=src tests/")

    def test_pytest_continue_on_collection_errors_NOT_rejected(self) -> None:
        assert _contains_test_keyword("pytest --continue-on-collection-errors tests/")

    def test_cargo_test_color_NOT_rejected(self) -> None:
        assert _contains_test_keyword("cargo test --color=auto")


# ===========================================================================
# T017 — Surface deferred / failed-review evidence on file overlap.
#
# A reviewer rejecting (or requesting changes on) a task records a Review row
# whose ``notes`` carry the finding and whose ``target_id`` is the task. The
# files that finding touched are the task's ``likely_files`` plus any submitted
# evidence's ``files_changed``. A later task whose incoming files overlap those
# files should surface the prior unresolved finding in its work packet.
# ===========================================================================


def _make_review(
    *,
    review_id: str,
    target_id: str,
    decision: ReviewDecision,
    notes: str | None = "needs work on the auth path",
    target_kind: ReviewTargetKind = ReviewTargetKind.task,
) -> Review:
    return Review(
        id=review_id,
        target_kind=target_kind,
        target_id=target_id,
        reviewed_by="reviewer-bob",
        decision=decision,
        notes=notes,
        created_at=_T0,
    )


def _make_task_with_files(
    *,
    task_id: str,
    likely_files: list[str],
) -> Task:
    return Task(
        id=task_id,
        feature_id="F001",
        title=f"Task {task_id}",
        description="A task.",
        status=TaskStatus.drafted,
        priority=TaskPriority.medium,
        acceptance_criteria=[],
        implementation_notes=[],
        verification=Verification(),
        likely_files=likely_files,
        scores=Score(),
        created_at=_T0,
        updated_at=_T0,
    )


def _make_evidence_for(
    *,
    evidence_id: str,
    task_id: str,
    files_changed: list[str],
) -> Evidence:
    return Evidence(
        id=evidence_id,
        task_id=task_id,
        claim_id=f"C-{task_id}",
        commands_run=["pytest -q"],
        files_changed=files_changed,
        submitted_at=_T0,
        submitted_by="agent-alpha",
    )


class TestDeferredFindings:
    """Pure derivation of deferred / failed-review findings linked to files."""

    def test_rejected_review_becomes_a_finding_linked_to_task_files(self) -> None:
        task = _make_task_with_files(task_id="T001", likely_files=["src/auth.py"])
        review = _make_review(
            review_id="RV-E5", target_id="T001", decision=ReviewDecision.reject
        )
        findings = deferred_findings([review], [task], [])
        assert len(findings) == 1
        f = findings[0]
        assert isinstance(f, DeferredFinding)
        assert f.review_id == "RV-E5"
        assert f.task_id == "T001"
        assert f.decision == "reject"
        assert f.notes == "needs work on the auth path"
        # File linkage comes from the reviewed task's likely_files.
        assert f.files == ["src/auth.py"]

    def test_needs_changes_review_is_also_a_finding(self) -> None:
        task = _make_task_with_files(task_id="T001", likely_files=["src/db.py"])
        review = _make_review(
            review_id="RV-E7",
            target_id="T001",
            decision=ReviewDecision.needs_changes,
        )
        findings = deferred_findings([review], [task], [])
        assert [f.decision for f in findings] == ["needs_changes"]

    def test_approved_review_is_not_a_finding(self) -> None:
        task = _make_task_with_files(task_id="T001", likely_files=["src/auth.py"])
        review = _make_review(
            review_id="RV-E9", target_id="T001", decision=ReviewDecision.approve
        )
        assert deferred_findings([review], [task], []) == []

    def test_finding_files_union_task_likely_and_evidence_changed(self) -> None:
        task = _make_task_with_files(task_id="T001", likely_files=["src/auth.py"])
        ev = _make_evidence_for(
            evidence_id="EV1",
            task_id="T001",
            files_changed=["src/auth.py", "src/session.py"],
        )
        review = _make_review(
            review_id="RV-E5", target_id="T001", decision=ReviewDecision.reject
        )
        findings = deferred_findings([review], [task], [ev])
        # Union of likely_files and evidence files_changed, sorted + de-duped.
        assert findings[0].files == ["src/auth.py", "src/session.py"]

    def test_non_task_review_target_ignored(self) -> None:
        review = _make_review(
            review_id="RV-E2",
            target_id="proj-1",
            decision=ReviewDecision.reject,
            target_kind=ReviewTargetKind.prd,
        )
        assert deferred_findings([review], [], []) == []


class TestDeferredFindingsForFiles:
    """Filtering deferred findings by an incoming claim/task's expected files."""

    def test_deferred_overlap_surfaces_prior_finding(self) -> None:
        # A prior finding deferred on src/auth.py ...
        prior_task = _make_task_with_files(
            task_id="T001", likely_files=["src/auth.py"]
        )
        review = _make_review(
            review_id="RV-E5", target_id="T001", decision=ReviewDecision.reject
        )
        # ... and a later task that intends to touch the SAME file.
        overlapping = deferred_findings_for_files(
            [review], [prior_task], [], expected_files=["src/auth.py"]
        )
        assert len(overlapping) == 1
        assert overlapping[0].review_id == "RV-E5"
        assert overlapping[0].overlapping_files == ["src/auth.py"]

    def test_deferred_overlap_excludes_non_overlapping_finding(self) -> None:
        prior_task = _make_task_with_files(
            task_id="T001", likely_files=["src/auth.py"]
        )
        review = _make_review(
            review_id="RV-E5", target_id="T001", decision=ReviewDecision.reject
        )
        # A later task touching a DIFFERENT file → no surfaced findings.
        assert (
            deferred_findings_for_files(
                [review], [prior_task], [], expected_files=["src/unrelated.py"]
            )
            == []
        )

    def test_empty_expected_files_yields_no_findings(self) -> None:
        prior_task = _make_task_with_files(
            task_id="T001", likely_files=["src/auth.py"]
        )
        review = _make_review(
            review_id="RV-E5", target_id="T001", decision=ReviewDecision.reject
        )
        assert (
            deferred_findings_for_files(
                [review], [prior_task], [], expected_files=[]
            )
            == []
        )

    def test_overlap_via_evidence_files_changed(self) -> None:
        # The reviewed task declared no likely_files, but the agent's evidence
        # touched src/session.py; a later task on src/session.py must still see
        # the prior finding.
        prior_task = _make_task_with_files(task_id="T001", likely_files=[])
        ev = _make_evidence_for(
            evidence_id="EV1",
            task_id="T001",
            files_changed=["src/session.py"],
        )
        review = _make_review(
            review_id="RV-E5", target_id="T001", decision=ReviewDecision.reject
        )
        overlapping = deferred_findings_for_files(
            [review], [prior_task], [ev], expected_files=["src/session.py"]
        )
        assert len(overlapping) == 1
        assert overlapping[0].overlapping_files == ["src/session.py"]


class TestWorkPacketSurfacesDeferredFindings:
    """The rendered work packet (markdown + json) surfaces overlapping findings."""

    def test_packet_markdown_and_json_include_deferred_finding(self) -> None:
        from anvil.context.packets import render_packet

        prior_task = _make_task_with_files(
            task_id="T001", likely_files=["src/auth.py"]
        )
        review = _make_review(
            review_id="RV-E5",
            target_id="T001",
            decision=ReviewDecision.reject,
            notes="auth path leaks the token on error",
        )
        # The new task we are rendering a packet for touches the same file.
        new_task = _make_task_with_files(
            task_id="T002", likely_files=["src/auth.py"]
        )

        findings = deferred_findings_for_files(
            [review], [prior_task, new_task], [], expected_files=new_task.likely_files
        )
        assert findings, "precondition: overlap must produce a finding"

        packet = render_packet(new_task, deferred_findings=findings)

        # Markdown surfaces the finding section and the note.
        assert "Prior unresolved review findings" in packet.markdown
        assert "RV-E5" in packet.markdown
        assert "auth path leaks the token on error" in packet.markdown
        assert "src/auth.py" in packet.markdown

        # JSON carries a structured, machine-readable array.
        df = packet.json_data["deferred_findings"]
        assert len(df) == 1
        assert df[0]["review_id"] == "RV-E5"
        assert df[0]["task_id"] == "T001"
        assert df[0]["decision"] == "reject"
        assert df[0]["overlapping_files"] == ["src/auth.py"]

    def test_packet_without_findings_omits_section_backcompat(self) -> None:
        from anvil.context.packets import render_packet

        new_task = _make_task_with_files(
            task_id="T002", likely_files=["src/auth.py"]
        )
        # Default (no deferred_findings passed) → no section, empty json array.
        packet = render_packet(new_task)
        assert "Prior unresolved review findings" not in packet.markdown
        assert packet.json_data["deferred_findings"] == []


# ===========================================================================
# T017 end-to-end (CLI): deferring a finding on file X then rendering a later
# task touching file X surfaces the finding in the actual `packet` command.
# Mirrors tests/test_strict_evidence.py: Typer CliRunner + chdir(tmp_path).
# ===========================================================================

# Two tasks that BOTH touch src/app/converter.py. T002 depends on T001 so the
# deterministic --no-llm planner keeps them ordered, but the file overlap is
# what drives the surfaced finding.
_OVERLAP_PRD = """\
# Project: Deferred Overlap Test Project

## Summary

A project exercising deferred-finding surfacing on file overlap.

## Goals

- Convert files correctly.

## Requirements

- R001: Accept file input.

## Features

### F001: File Conversion

Convert input files to output format.

**Requirements:** R001

## Tasks

### T001: Implement converter core

**Feature:** F001
**Priority:** high
**Likely files:** src/app/converter.py

**Acceptance criteria:**

- Conversion succeeds for valid input.

**Verification:**

- `pytest tests/test_converter.py -v`

### T002: Harden converter error handling

**Feature:** F001
**Priority:** high
**Likely files:** src/app/converter.py
**Dependencies:** T001

**Acceptance criteria:**

- Errors are reported, not swallowed.

**Verification:**

- `pytest tests/test_converter.py -v`
"""


class TestDeferredOverlapEndToEnd:
    def test_deferred_overlap_finding_appears_in_later_task_packet(
        self, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        import json as _json
        import os
        import sqlite3 as _sqlite3

        from typer.testing import CliRunner

        from anvil.cli import app

        runner = CliRunner()

        def _invoke(cmd: list[str]):  # type: ignore[no-untyped-def]
            original_cwd = os.getcwd()
            os.chdir(tmp_path)
            try:
                return runner.invoke(app, cmd, catch_exceptions=False)
            finally:
                os.chdir(original_cwd)

        # 1. Plan the two-task project.
        assert _invoke(["init", "--name", "Deferred Overlap Test Project"]).exit_code == 0
        (tmp_path / ".anvil" / "prd.md").write_text(
            _OVERLAP_PRD, encoding="utf-8"
        )
        assert _invoke(["prd", "parse"]).exit_code == 0
        assert _invoke(["prd", "review"]).exit_code == 0
        assert _invoke(["prd", "review", "--approve"]).exit_code == 0
        assert _invoke(["plan", "--no-llm"]).exit_code == 0
        assert _invoke(["score"]).exit_code == 0
        assert _invoke(["review", "tasks"]).exit_code == 0

        # 2. Claim T001, submit evidence (→ needs_review), then REJECT with a
        #    finding note. The reject is the "deferred / failed review".
        assert _invoke(["claim", "T001", "--actor", "agent-test"]).exit_code == 0
        sub = _invoke(
            [
                "submit",
                "T001",
                "--commands",
                "pytest tests/ -v",
                "--files-changed",
                "src/app/converter.py",
                "--actor",
                "agent-test",
            ]
        )
        assert sub.exit_code == 0, sub.output

        rej = _invoke(
            [
                "apply",
                "T001",
                "--reject",
                "--reason",
                "converter swallows malformed-input errors silently",
            ]
        )
        assert rej.exit_code == 0, rej.output

        # The finding is persisted as a queryable review row on src/app/converter.py.
        db_path = tmp_path / ".anvil" / "state.db"
        conn = _sqlite3.connect(str(db_path))
        try:
            review_row = conn.execute(
                "SELECT decision, notes FROM reviews "
                "WHERE target_kind='task' AND target_id='T001'"
            ).fetchone()
        finally:
            conn.close()
        assert review_row is not None
        assert review_row[0] == "rejected"
        assert "swallows malformed-input errors" in review_row[1]

        # 3. Render the packet for T002 — which touches the SAME file. The prior
        #    deferred finding must surface, in both markdown and json.
        md_res = _invoke(["packet", "T002", "--format", "md"])
        assert md_res.exit_code == 0, md_res.output
        md = (tmp_path / ".anvil" / "packets" / "T002.md").read_text(
            encoding="utf-8"
        )
        assert "Prior unresolved review findings" in md
        assert "converter swallows malformed-input errors silently" in md
        assert "src/app/converter.py" in md

        json_res = _invoke(["packet", "T002", "--format", "json"])
        assert json_res.exit_code == 0, json_res.output
        data = _json.loads(
            (tmp_path / ".anvil" / "packets" / "T002.json").read_text(
                encoding="utf-8"
            )
        )
        findings = data["deferred_findings"]
        assert len(findings) == 1
        assert findings[0]["task_id"] == "T001"
        # The reviews table stores the raw task.applied outcome "rejected"; when
        # read back via list_reviews() it surfaces as the canonical
        # ReviewDecision "needs_changes" (a rejected task auto-reopens for
        # rework — see _row_to_review / _TASK_OUTCOME_TO_REVIEW_DECISION). Either
        # way the finding is a deferred/failed review and must surface.
        assert findings[0]["decision"] in ("reject", "needs_changes")
        assert findings[0]["overlapping_files"] == ["src/app/converter.py"]
        assert "swallows malformed-input errors" in findings[0]["notes"]
