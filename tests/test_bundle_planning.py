"""Deterministic bundle planning and bounded adversarial-review policy."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from anvil.cli import app
from anvil.planning.inference import build_bundle_plan
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
