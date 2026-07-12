"""Deterministic bundle planning and bounded adversarial-review policy."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from anvil.cli import app
from anvil.planning.inference import BundlePlanningError, build_bundle_plan
from anvil.review.gates import evaluate_bundle_reviews
from anvil.state.models import (
    BundleReviewPolicy,
    BundleReviewVerdict,
    ReviewDecision,
    Score,
    Task,
    TaskPriority,
    TaskStatus,
    Verification,
)

_NOW = datetime(2026, 7, 11, 18, 0, tzinfo=UTC)


def _task(
    task_id: str,
    files: list[str],
    *,
    dependencies: list[str] | None = None,
    blast: int | None = None,
) -> Task:
    return Task(
        id=task_id,
        feature_id="F001",
        title=task_id,
        description="Bundle planning fixture.",
        status=TaskStatus.proposed,
        priority=TaskPriority.high,
        scores=Score(blast_radius=blast),
        acceptance_criteria=["Done."],
        verification=Verification(commands=["pytest -q"]),
        likely_files=files,
        dependencies=dependencies or [],
        created_at=_NOW,
        updated_at=_NOW,
    )


def _verdict(
    index: int,
    *,
    reviewer: str,
    angle: str,
    decision: ReviewDecision = ReviewDecision.approve,
    review_round: int = 1,
) -> BundleReviewVerdict:
    return BundleReviewVerdict(
        id=f"BR{index:03d}",
        bundle_id="B001",
        creation_event_id="E000001",
        disposition_event_id="E000002",
        review_round=review_round,
        angle=angle,
        reviewed_by=reviewer,
        decision=decision,
        notes="blocking finding" if decision is not ReviewDecision.approve else None,
        created_at=_NOW,
    )


def test_bundle_plan_is_stable_and_reports_costs_risks_and_limits() -> None:
    tasks = [
        _task("T001", ["src/state/schema.py"], blast=5),
        _task(
            "T002",
            ["src/state/schema.py", "src/cli/plan.py"],
            dependencies=["T001"],
        ),
        _task("T003", ["docs/guide.md"]),
    ]
    first = build_bundle_plan(tasks, max_tasks=2, max_serial_stages=1)
    second = build_bundle_plan(list(reversed(tasks)), max_tasks=2, max_serial_stages=1)
    assert first.to_dict() == second.to_dict()
    assert first.task_count == 3
    assert first.serial_depth == 2
    assert first.overlap_pair_count == 1
    assert [item.task_ids for item in first.proposed_bundles] == [
        ("T001", "T002"),
        ("T003",),
    ]
    assert first.expected_review_count >= 6
    assert first.expected_checkpoints == 2
    assert "topology" in first.high_risk_policies
    assert first.limit_breaches == (
        "task_count 3 exceeds limit 2",
        "serial_depth 2 exceeds limit 1",
    )


def test_bundle_plan_rejects_malformed_graphs_and_limits() -> None:
    with pytest.raises(BundlePlanningError, match="duplicate task ids"):
        build_bundle_plan([_task("T001", []), _task("T001", [])])
    with pytest.raises(BundlePlanningError, match="missing dependency nodes"):
        build_bundle_plan([_task("T001", [], dependencies=["T404"])])
    with pytest.raises(BundlePlanningError, match="dependency cycle"):
        build_bundle_plan(
            [
                _task("T001", [], dependencies=["T002"]),
                _task("T002", [], dependencies=["T001"]),
            ]
        )
    for limit in (0, -1, 501, True):
        with pytest.raises(BundlePlanningError, match="range 1-500"):
            build_bundle_plan([_task("T001", [])], max_tasks=limit)  # type: ignore[arg-type]


def test_bundle_plan_normalizes_equivalent_project_paths_deterministically() -> None:
    tasks = [
        _task("T001", ["src/x.py"]),
        _task("T002", ["./src\\x.py", "src/y.py"]),
    ]
    first = build_bundle_plan(tasks)
    second = build_bundle_plan(list(reversed(tasks)))
    assert first.to_dict() == second.to_dict()
    assert first.overlap_pair_count == 1
    assert first.overlap_files == ("src/x.py",)
    assert first.proposed_bundles[0].task_ids == ("T001", "T002")
    assert first.serial_depth == 2

    for unsafe in (
        "../outside.py",
        "/absolute.py",
        "C:/absolute.py",
        "C:drive-relative.py",
        "src/file.py:stream",
    ):
        with pytest.raises(BundlePlanningError, match="project"):
            build_bundle_plan([_task("T001", [unsafe])])


def test_review_gate_requires_three_distinct_non_author_angles() -> None:
    policy = BundleReviewPolicy()
    verdicts = [
        _verdict(1, reviewer="reviewer-a", angle="correctness"),
        _verdict(2, reviewer="reviewer-b", angle="security"),
        _verdict(3, reviewer="reviewer-c", angle="integration"),
    ]
    assert evaluate_bundle_reviews(policy, verdicts, coordinator="author").passed

    self_review = verdicts[:2] + [
        _verdict(4, reviewer="author", angle="integration")
    ]
    blocked = evaluate_bundle_reviews(policy, self_review, coordinator="author")
    assert not blocked.passed
    assert blocked.invalid_reviewers == ["author"]
    disabled_flag = policy.model_copy(
        update={"independent_reviewer_required": False}
    )
    still_blocked = evaluate_bundle_reviews(
        disabled_flag, self_review, coordinator="author"
    )
    assert not still_blocked.passed
    assert still_blocked.invalid_reviewers == ["author"]


def test_blocker_requires_remediation_then_replan_when_rereview_is_exhausted() -> None:
    policy = BundleReviewPolicy(max_rereviews=1)
    first_round = [
        _verdict(1, reviewer="a", angle="correctness"),
        _verdict(2, reviewer="b", angle="security", decision=ReviewDecision.reject),
        _verdict(3, reviewer="c", angle="integration"),
    ]
    initial = evaluate_bundle_reviews(policy, first_round, coordinator="author")
    assert not initial.passed
    assert not initial.replan_required

    second_round = first_round + [
        _verdict(4, reviewer="d", angle="correctness", review_round=2),
        _verdict(
            5,
            reviewer="e",
            angle="security",
            decision=ReviewDecision.needs_changes,
            review_round=2,
        ),
        _verdict(6, reviewer="f", angle="integration", review_round=2),
    ]
    exhausted = evaluate_bundle_reviews(policy, second_round, coordinator="author")
    assert not exhausted.passed
    assert exhausted.replan_required
    assert exhausted.rereviews_used == 1


def test_plan_bundle_limits_fail_closed_and_acknowledgement_is_audited(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    original = os.getcwd()
    os.chdir(tmp_path)
    try:
        initialized = runner.invoke(app, ["init", "--name", "Bundle Plan"])
        assert initialized.exit_code == 0, initialized.output
        prd = tmp_path / ".anvil" / "prd.md"
        prd.write_text(
            """# Project: Bundle Plan

## Summary
Bundle planning fixture.

## Goals
- Produce a bounded execution wave.

## Requirements
- R001: Plan bundles.

## Features
### F001: Planner
**Requirements:** R001

## Tasks
### T001: First
**Feature:** F001
**Likely files:** src/shared.py
**Acceptance criteria:**
- First works.
**Verification:**
- `pytest -q`

### T002: Second
**Feature:** F001
**Likely files:** src/shared.py
**Acceptance criteria:**
- Second works.
**Verification:**
- `pytest -q`
""",
            encoding="utf-8",
        )
        config = tmp_path / ".anvil" / "config.yaml"
        config.write_text(
            config.read_text(encoding="utf-8")
            + "\nbundle_max_tasks: 1\nbundle_max_serial_stages: 6\n",
            encoding="utf-8",
        )
        parsed = runner.invoke(app, ["prd", "parse"])
        assert parsed.exit_code == 0, parsed.output

        refused = runner.invoke(app, ["plan", "--bundles", "--json"])
        assert refused.exit_code == 1
        refusal = json.loads(refused.output)
        assert refusal["error"]["code"] == "bundle_limits_exceeded"
        assert refusal["error"]["bundle_plan"]["task_count"] == 2

        config.write_text(
            config.read_text(encoding="utf-8")
            + "\nbundle_max_serial_stages: 0\n",
            encoding="utf-8",
        )
        malformed = runner.invoke(app, ["plan", "--bundles", "--json"])
        assert malformed.exit_code == 1
        assert json.loads(malformed.output)["error"]["code"] == "invalid_bundle_config"
        config.write_text(
            config.read_text(encoding="utf-8")
            + "\nbundle_max_serial_stages: 6\n",
            encoding="utf-8",
        )

        accepted = runner.invoke(
            app,
            [
                "plan",
                "--bundles",
                "--acknowledge-bundle-limits",
                "--json",
            ],
        )
        assert accepted.exit_code == 0, accepted.output
        data = json.loads(accepted.output)["data"]
        assert data["bundle_limits_acknowledged"] is True
        assert data["bundle_plan"]["proposed_bundles"][0]["id"] == "BP001"
        events = (tmp_path / ".anvil" / "events.jsonl").read_text(encoding="utf-8")
        assert '"action":"bundle.plan_acknowledged"' in events
    finally:
        os.chdir(original)


def test_plan_bundle_cycle_returns_stable_json_error_without_writes(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    original = os.getcwd()
    os.chdir(tmp_path)
    try:
        assert runner.invoke(app, ["init", "--name", "Cycle Plan"]).exit_code == 0
        (tmp_path / ".anvil" / "prd.md").write_text(
            """# Project: Cycle Plan

## Summary
Cycle fixture.
## Goals
- Refuse unsafe graphs.
## Requirements
- R001: Detect cycles.
## Features
### F001: Planner
**Requirements:** R001
## Tasks
### T001: First
**Feature:** F001
**Dependencies:** T002
**Likely files:** src/a.py
**Acceptance criteria:**
- First.
**Verification:**
- `pytest -q`
### T002: Second
**Feature:** F001
**Dependencies:** T001
**Likely files:** src/b.py
**Acceptance criteria:**
- Second.
**Verification:**
- `pytest -q`
""",
            encoding="utf-8",
        )
        assert runner.invoke(app, ["prd", "parse"]).exit_code == 0
        before = (tmp_path / ".anvil" / "events.jsonl").read_bytes()
        result = runner.invoke(app, ["plan", "--bundles", "--json"])
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["error"]["code"] == "invalid_bundle_graph"
        assert "T001 -> T002 -> T001" in payload["error"]["message"]
        assert (tmp_path / ".anvil" / "events.jsonl").read_bytes() == before
    finally:
        os.chdir(original)
