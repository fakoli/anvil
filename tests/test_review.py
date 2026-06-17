"""Tests for fakoli_state.review.gates — review gate functions.

Coverage targets (>= 90%):
- evidence_complete() — all decision branches
- Case-insensitive matching
- Multiple required items — some missing, some satisfied
"""

from __future__ import annotations

from datetime import UTC, datetime

from fakoli_state.review.gates import _contains_test_keyword, evidence_complete
from fakoli_state.state.models import (
    Evidence,
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
