"""CLI ``--json`` output tests (backlog T006/B10).

Verifies the machine-readable JSON envelope across every command that grew a
``--json`` flag. Each test asserts:

* stdout is exactly one line of valid JSON (pipeable into ``json.load``);
* the envelope carries the canonical keys (``ok`` / ``command`` / ``data``
  on success, ``ok`` / ``command`` / ``error`` on failure);
* exit codes are correct (0 on success, non-zero on failure);
* human (non-json) output is unchanged when ``--json`` is absent.

Pattern mirrors ``tests/test_cli.py`` (Typer ``CliRunner`` + ``os.chdir`` into
a per-test ``tmp_path``). Commands that build a fully-planned project use the
deterministic ``--no-llm`` path so no LLM provider is required.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from fakoli_state.cli import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_FULL_PRD = """\
# Project: JSON Output Test Project

## Summary

A full project for JSON output testing.

## Goals

- Convert files correctly.
- Handle errors gracefully.

## Requirements

- R001: Accept file input.
- R002: Produce file output.
- R003: Handle errors.

## Acceptance Criteria

- Converts files correctly.

## Features

### F001: File Conversion

Convert input files to output format.

**Requirements:** R001, R002

### F002: Error Handling

Handle errors gracefully.

**Requirements:** R003

## Tasks

### T001: Implement converter

**Feature:** F001
**Priority:** high
**Likely files:** src/app/converter.py

**Acceptance criteria:**

- Conversion succeeds for valid input.

**Verification:**

- `pytest tests/test_converter.py -v`

### T002: Implement error handler

**Feature:** F002
**Priority:** medium
**Likely files:** src/app/errors.py

**Acceptance criteria:**

- Errors are reported with context.

**Verification:**

- `pytest tests/test_errors.py -v`
"""


def _invoke(tmp_path: Path, cmd: list[str]):  # type: ignore[no-untyped-def]
    """Run a CLI command with cwd set to tmp_path."""
    original_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        return runner.invoke(app, cmd, catch_exceptions=False)
    finally:
        os.chdir(original_cwd)


def _parse_envelope(result: Any) -> dict[str, Any]:
    """Assert stdout is a single valid-JSON line and return the parsed dict."""
    text = result.stdout.strip()
    # Must be parseable as a whole — no human lines mixed in.
    envelope = json.loads(text)
    assert isinstance(envelope, dict)
    return envelope


def _assert_success(envelope: dict[str, Any], command: str) -> dict[str, Any]:
    assert envelope["ok"] is True
    assert envelope["command"] == command
    assert "data" in envelope
    assert "error" not in envelope
    return envelope["data"]


def _assert_error(envelope: dict[str, Any], command: str) -> dict[str, Any]:
    assert envelope["ok"] is False
    assert envelope["command"] == command
    assert "error" in envelope
    assert "code" in envelope["error"]
    assert "message" in envelope["error"]
    assert "data" not in envelope
    return envelope["error"]


def _init(tmp_path: Path) -> None:
    res = _invoke(tmp_path, ["init", "--name", "JSON Output Test Project"])
    assert res.exit_code == 0, res.output


def _planned_project(tmp_path: Path) -> None:
    """init -> PRD -> review -> approve -> plan --no-llm -> score -> review tasks."""
    _init(tmp_path)
    (tmp_path / ".fakoli-state" / "prd.md").write_text(_FULL_PRD, encoding="utf-8")
    assert _invoke(tmp_path, ["prd", "parse"]).exit_code == 0
    assert _invoke(tmp_path, ["prd", "review"]).exit_code == 0
    assert _invoke(tmp_path, ["prd", "review", "--approve"]).exit_code == 0
    assert _invoke(tmp_path, ["plan", "--no-llm"]).exit_code == 0
    assert _invoke(tmp_path, ["score"]).exit_code == 0
    assert _invoke(tmp_path, ["review", "tasks"]).exit_code == 0


# ---------------------------------------------------------------------------
# Read / query commands
# ---------------------------------------------------------------------------


class TestStatusJson:
    def test_status_json_success(self, tmp_path: Path) -> None:
        _planned_project(tmp_path)
        res = _invoke(tmp_path, ["status", "--json"])
        assert res.exit_code == 0
        data = _assert_success(_parse_envelope(res), "status")
        assert data["prd_status"] == "approved"
        assert data["tasks"]["total"] == 2
        assert data["project"]["name"] == "JSON Output Test Project"
        assert data["active_claims"] == 0

    def test_status_json_not_initialized_is_error(self, tmp_path: Path) -> None:
        res = _invoke(tmp_path, ["status", "--json"])
        assert res.exit_code != 0
        err = _assert_error(_parse_envelope(res), "status")
        assert err["code"] == "not_initialized"

    def test_status_human_output_unchanged(self, tmp_path: Path) -> None:
        _planned_project(tmp_path)
        res = _invoke(tmp_path, ["status"])
        assert res.exit_code == 0
        # Human output is NOT JSON and carries the multi-line summary.
        assert "PRD:" in res.output
        assert "Tasks:" in res.output


class TestListJson:
    def test_list_json_success(self, tmp_path: Path) -> None:
        _planned_project(tmp_path)
        res = _invoke(tmp_path, ["list", "--json"])
        assert res.exit_code == 0
        data = _assert_success(_parse_envelope(res), "list")
        assert data["count"] == 2
        assert len(data["tasks"]) == 2
        ids = {t["id"] for t in data["tasks"]}
        assert ids == {"T001", "T002"}
        # Serialized via model_dump — enums become plain strings.
        assert all(isinstance(t["status"], str) for t in data["tasks"])

    def test_list_json_status_filter(self, tmp_path: Path) -> None:
        _planned_project(tmp_path)
        res = _invoke(tmp_path, ["list", "--status", "ready", "--json"])
        assert res.exit_code == 0
        data = _assert_success(_parse_envelope(res), "list")
        assert data["filters"]["status"] == "ready"
        assert all(t["status"] == "ready" for t in data["tasks"])

    def test_list_json_not_initialized_is_error(self, tmp_path: Path) -> None:
        res = _invoke(tmp_path, ["list", "--json"])
        assert res.exit_code != 0
        err = _assert_error(_parse_envelope(res), "list")
        assert err["code"] == "not_initialized"


class TestShowJson:
    def test_show_json_success(self, tmp_path: Path) -> None:
        _planned_project(tmp_path)
        res = _invoke(tmp_path, ["show", "T001", "--json"])
        assert res.exit_code == 0
        data = _assert_success(_parse_envelope(res), "show")
        assert data["task"]["id"] == "T001"
        assert "scores" in data["task"]
        assert isinstance(data["active_claims"], list)
        assert isinstance(data["recent_events"], list)

    def test_show_json_missing_task_is_error(self, tmp_path: Path) -> None:
        _planned_project(tmp_path)
        res = _invoke(tmp_path, ["show", "T999", "--json"])
        assert res.exit_code != 0
        err = _assert_error(_parse_envelope(res), "show")
        assert err["code"] == "not_found"


class TestNextJson:
    def test_next_json_returns_task(self, tmp_path: Path) -> None:
        _planned_project(tmp_path)
        res = _invoke(tmp_path, ["next", "--json"])
        assert res.exit_code == 0
        data = _assert_success(_parse_envelope(res), "next")
        assert data["task"] is not None
        assert data["task"]["id"] in {"T001", "T002"}

    def test_next_json_empty_queue_is_null_not_error(self, tmp_path: Path) -> None:
        # A freshly-initialized project with no ready tasks → task is null,
        # exit 0 (an empty queue is not an error).
        _init(tmp_path)
        res = _invoke(tmp_path, ["next", "--json"])
        assert res.exit_code == 0
        data = _assert_success(_parse_envelope(res), "next")
        assert data["task"] is None


class TestFindDecisionsJson:
    def test_find_decisions_json_success(self, tmp_path: Path) -> None:
        _planned_project(tmp_path)
        res = _invoke(tmp_path, ["prd", "find-decisions", "--json"])
        assert res.exit_code == 0
        data = _assert_success(_parse_envelope(res), "prd find-decisions")
        assert "decisions" in data
        assert "count" in data
        assert "counts_by_kind" in data

    def test_find_decisions_json_missing_prd_is_error(self, tmp_path: Path) -> None:
        _init(tmp_path)  # initialized but no prd.md authored
        res = _invoke(tmp_path, ["prd", "find-decisions", "--json"])
        assert res.exit_code != 0
        err = _assert_error(_parse_envelope(res), "prd find-decisions")
        assert err["code"] == "not_found"


# ---------------------------------------------------------------------------
# Mutation commands
# ---------------------------------------------------------------------------


class TestPlanJson:
    def test_plan_json_success(self, tmp_path: Path) -> None:
        _init(tmp_path)
        (tmp_path / ".fakoli-state" / "prd.md").write_text(_FULL_PRD, encoding="utf-8")
        assert _invoke(tmp_path, ["prd", "parse"]).exit_code == 0
        assert _invoke(tmp_path, ["prd", "review"]).exit_code == 0
        assert _invoke(tmp_path, ["prd", "review", "--approve"]).exit_code == 0
        res = _invoke(tmp_path, ["plan", "--no-llm", "--json"])
        assert res.exit_code == 0
        data = _assert_success(_parse_envelope(res), "plan")
        assert data["features"] == 2
        assert data["tasks"] == 2
        assert isinstance(data["warnings"], list)


class TestScoreJson:
    def test_score_json_success(self, tmp_path: Path) -> None:
        _init(tmp_path)
        (tmp_path / ".fakoli-state" / "prd.md").write_text(_FULL_PRD, encoding="utf-8")
        assert _invoke(tmp_path, ["prd", "parse"]).exit_code == 0
        assert _invoke(tmp_path, ["prd", "review"]).exit_code == 0
        assert _invoke(tmp_path, ["prd", "review", "--approve"]).exit_code == 0
        assert _invoke(tmp_path, ["plan", "--no-llm"]).exit_code == 0
        res = _invoke(tmp_path, ["score", "--json"])
        assert res.exit_code == 0
        data = _assert_success(_parse_envelope(res), "score")
        assert data["count"] == 2
        assert len(data["scored"]) == 2
        for entry in data["scored"]:
            assert "task_id" in entry
            assert entry["scores"]["complexity"] is not None

    def test_score_json_missing_task_is_error(self, tmp_path: Path) -> None:
        _planned_project(tmp_path)
        res = _invoke(tmp_path, ["score", "T999", "--json"])
        assert res.exit_code != 0
        err = _assert_error(_parse_envelope(res), "score")
        assert err["code"] == "not_found"


class TestReviewTasksJson:
    def test_review_tasks_json_success(self, tmp_path: Path) -> None:
        _init(tmp_path)
        (tmp_path / ".fakoli-state" / "prd.md").write_text(_FULL_PRD, encoding="utf-8")
        assert _invoke(tmp_path, ["prd", "parse"]).exit_code == 0
        assert _invoke(tmp_path, ["prd", "review"]).exit_code == 0
        assert _invoke(tmp_path, ["prd", "review", "--approve"]).exit_code == 0
        assert _invoke(tmp_path, ["plan", "--no-llm"]).exit_code == 0
        assert _invoke(tmp_path, ["score"]).exit_code == 0
        res = _invoke(tmp_path, ["review", "tasks", "--json"])
        assert res.exit_code == 0
        data = _assert_success(_parse_envelope(res), "review tasks")
        assert "promoted_to_reviewed" in data
        assert "promoted_to_ready" in data
        assert "blocked" in data


class TestClaimReleaseRenewJson:
    def test_claim_then_renew_then_release_json(self, tmp_path: Path) -> None:
        _planned_project(tmp_path)
        # claim
        res = _invoke(tmp_path, ["claim", "T001", "--actor", "tester", "--json"])
        assert res.exit_code == 0, res.output
        # JSON mode must keep stderr silent — warnings are folded into the
        # envelope's ``warnings`` list, not printed (here: the non-git-repo
        # branch warning, since tmp_path is not a git repo).
        assert res.stderr == ""
        data = _assert_success(_parse_envelope(res), "claim")
        claim_id = data["claim"]["id"]
        assert data["claim"]["task_id"] == "T001"
        assert isinstance(data["warnings"], list)
        assert any("git" in w.lower() for w in data["warnings"])

        # renew
        res = _invoke(tmp_path, ["renew", claim_id, "--actor", "tester", "--json"])
        assert res.exit_code == 0, res.output
        data = _assert_success(_parse_envelope(res), "renew")
        assert data["claim"]["id"] == claim_id

        # release
        res = _invoke(
            tmp_path,
            ["release", claim_id, "--actor", "tester", "--reason", "done", "--json"],
        )
        assert res.exit_code == 0, res.output
        data = _assert_success(_parse_envelope(res), "release")
        assert data["claim_id"] == claim_id
        assert data["released"] is True

    def test_claim_missing_task_is_error(self, tmp_path: Path) -> None:
        _planned_project(tmp_path)
        res = _invoke(tmp_path, ["claim", "T999", "--json"])
        assert res.exit_code != 0
        err = _assert_error(_parse_envelope(res), "claim")
        assert err["code"] == "not_found"

    def test_renew_unknown_claim_is_error(self, tmp_path: Path) -> None:
        _planned_project(tmp_path)
        res = _invoke(tmp_path, ["renew", "C-DOESNOTEXIST", "--json"])
        assert res.exit_code != 0
        err = _assert_error(_parse_envelope(res), "renew")
        assert err["code"] == "claim_error"


# The unified ``apply --json`` data key set — IDENTICAL across review-only and
# decision (--approve/--reject) modes so a consumer reads the outcome the same
# way regardless of how apply was invoked.
_APPLY_KEYS = {
    "task_id",
    "status",
    "decision",
    "reviewer",
    "reason",
    "has_evidence",
    "evidence_gate",
    "task",
    "next_ready",  # T014
}


class TestSubmitApplyJson:
    def test_submit_then_apply_approve_json(self, tmp_path: Path) -> None:
        _planned_project(tmp_path)
        claim_res = _invoke(tmp_path, ["claim", "T001", "--actor", "tester", "--json"])
        assert claim_res.exit_code == 0, claim_res.output

        # submit evidence
        res = _invoke(
            tmp_path,
            [
                "submit",
                "T001",
                "--commands",
                "pytest tests/test_converter.py -v",
                "--files-changed",
                "src/app/converter.py",
                "--actor",
                "tester",
                "--json",
            ],
        )
        assert res.exit_code == 0, res.output
        data = _assert_success(_parse_envelope(res), "submit")
        assert data["claim_id"]
        assert data["evidence_id"].startswith("EV")
        assert data["task"]["status"] == "needs_review"

        # apply --approve
        res = _invoke(
            tmp_path,
            ["apply", "T001", "--approve", "--reviewer", "human", "--json"],
        )
        assert res.exit_code == 0, res.output
        data = _assert_success(_parse_envelope(res), "apply")
        # Decision mode carries the unified key set.
        assert set(data.keys()) == _APPLY_KEYS
        assert data["task_id"] == "T001"
        assert data["decision"] == "accepted"
        assert data["reviewer"] == "human"
        assert data["status"] == "done"
        assert data["task"]["status"] == "done"
        assert data["has_evidence"] is True

    def test_apply_review_only_mode_json(self, tmp_path: Path) -> None:
        _planned_project(tmp_path)
        assert _invoke(
            tmp_path, ["claim", "T001", "--actor", "tester"]
        ).exit_code == 0
        assert _invoke(
            tmp_path,
            [
                "submit",
                "T001",
                "--commands",
                "pytest -v",
                "--files-changed",
                "src/app/converter.py",
                "--actor",
                "tester",
            ],
        ).exit_code == 0
        # No --approve / --reject → review-only summary, exit 0.
        res = _invoke(tmp_path, ["apply", "T001", "--json"])
        assert res.exit_code == 0, res.output
        data = _assert_success(_parse_envelope(res), "apply")
        # Review-only mode carries the SAME unified key set as decision mode.
        assert set(data.keys()) == _APPLY_KEYS
        assert data["task_id"] == "T001"
        assert data["status"] == "needs_review"
        assert data["decision"] is None
        assert data["reviewer"] is None
        assert data["reason"] is None
        assert data["has_evidence"] is True
        assert data["task"]["id"] == "T001"
        assert data["task"]["status"] == "needs_review"

    def test_submit_json_names_next_ready_task(self, tmp_path: Path) -> None:
        """T014: submit --json carries a next_ready descriptor naming the
        next claimable task (T002 here), or null when none is available."""
        _planned_project(tmp_path)
        assert _invoke(
            tmp_path, ["claim", "T001", "--actor", "tester"]
        ).exit_code == 0
        res = _invoke(
            tmp_path,
            [
                "submit",
                "T001",
                "--commands",
                "pytest -v",
                "--files-changed",
                "src/app/converter.py",
                "--actor",
                "tester",
                "--json",
            ],
        )
        assert res.exit_code == 0, res.output
        data = _assert_success(_parse_envelope(res), "submit")
        assert "next_ready" in data
        assert data["next_ready"] is not None
        assert data["next_ready"]["id"] == "T002"
        assert set(data["next_ready"].keys()) == {"id", "title", "priority"}

    def test_submit_json_next_ready_null_when_only_candidate_taken(
        self, tmp_path: Path
    ) -> None:
        """T014: next_ready is null when the only other task is already
        claimed (and its file locked) by another agent."""
        _planned_project(tmp_path)
        # Another agent claims T002; the claim's expected_files are derived
        # from T002's likely_files (src/app/errors.py), locking that file.
        assert _invoke(
            tmp_path,
            ["claim", "T002", "--actor", "other"],
        ).exit_code == 0
        # Now claim + submit T001 as tester.
        assert _invoke(
            tmp_path, ["claim", "T001", "--actor", "tester"]
        ).exit_code == 0
        res = _invoke(
            tmp_path,
            [
                "submit",
                "T001",
                "--commands",
                "pytest -v",
                "--files-changed",
                "src/app/converter.py",
                "--actor",
                "tester",
                "--json",
            ],
        )
        assert res.exit_code == 0, res.output
        data = _assert_success(_parse_envelope(res), "submit")
        # T002 is the only other ready task, but it is claimed AND its file is
        # locked by 'other' → no safe next_ready remains.
        assert data["next_ready"] is None

    def test_submit_without_claim_is_error(self, tmp_path: Path) -> None:
        _planned_project(tmp_path)
        res = _invoke(
            tmp_path,
            [
                "submit",
                "T001",
                "--commands",
                "pytest -v",
                "--files-changed",
                "src/app/converter.py",
                "--json",
            ],
        )
        assert res.exit_code != 0
        err = _assert_error(_parse_envelope(res), "submit")
        assert err["code"] == "no_active_claim"

    def test_apply_wrong_status_is_error(self, tmp_path: Path) -> None:
        _planned_project(tmp_path)
        # T001 is 'ready', not 'needs_review'.
        res = _invoke(tmp_path, ["apply", "T001", "--approve", "--json"])
        assert res.exit_code != 0
        err = _assert_error(_parse_envelope(res), "apply")
        assert err["code"] == "invalid_status"
