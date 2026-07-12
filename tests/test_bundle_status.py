"""Integration-focused bundle status rollups."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from anvil.bundles.review import BundleReviewManager
from anvil.cli import app
from anvil.clock import FrozenClock
from anvil.state.models import (
    BundleCheckpoint,
    BundleClaim,
    BundleReviewVerdict,
    BundleStatus,
    BundleThroughputBudget,
    Claim,
    EventDraft,
    ExecutionBundle,
    ReviewDecision,
    Task,
    TaskPriority,
    TaskStatus,
)
from anvil.state.rollup import compute_bundle_rollup
from tests.test_bundle_execution import _NOW as _EXEC_NOW
from tests.test_bundle_execution import _backend, _implement_bundle, _seed

_NOW = datetime(2026, 7, 12, 1, 0, tzinfo=UTC)


def test_rollup_surfaces_critical_path_review_checkpoint_and_warning() -> None:
    tasks = [
        Task(
            id="T001",
            feature_id="F001",
            title="First",
            description="",
            status=TaskStatus.done,
            priority=TaskPriority.high,
            created_at=_NOW,
            updated_at=_NOW,
        ),
        Task(
            id="T002",
            feature_id="F001",
            title="Second",
            description="",
            status=TaskStatus.needs_review,
            priority=TaskPriority.high,
            dependencies=["T001"],
            created_at=_NOW,
            updated_at=_NOW,
        ),
    ]
    bundle = ExecutionBundle(
        id="B001",
        creation_event_id="E001",
        review_disposition_event_id="E010",
        prd_id="default",
        task_ids=["T001", "T002"],
        coordinator="coordinator",
        status=BundleStatus.reviewed_unintegrated,
        last_result_at=_NOW,
        created_at=_NOW,
        updated_at=_NOW,
    )
    reviews = [
        BundleReviewVerdict(
            id=f"BR{index}",
            bundle_id="B001",
            creation_event_id="E001",
            disposition_event_id="E010",
            review_round=1,
            angle=angle,
            reviewed_by=f"reviewer-{index}",
            decision=ReviewDecision.approve,
            created_at=_NOW,
        )
        for index, angle in enumerate(("correctness", "security", "integration"), 1)
    ]
    entry = compute_bundle_rollup(
        [bundle],
        tasks,
        [],
        reviews,
        now=_NOW + timedelta(minutes=2),
    )[0]
    assert entry.critical_path_stage == 1
    assert entry.critical_path_depth == 2
    assert entry.review_usage == {"round": 1, "reviews": 3, "rereviews": 0}
    assert entry.elapsed_since_result_seconds == 120
    assert entry.checkpoint_warning is not None

    checkpointed = bundle.model_copy(
        update={
            "checkpoint": BundleCheckpoint(
                commit_sha="abc123",
                recorded_at=_NOW,
                recorded_by="coordinator",
            )
        }
    )
    assert compute_bundle_rollup(
        [checkpointed],
        tasks,
        [],
        reviews,
        now=_NOW,
    )[0].checkpoint_warning is None

    blocked_prefix = [
        tasks[0].model_copy(update={"status": TaskStatus.needs_review}),
        tasks[1].model_copy(update={"status": TaskStatus.done}),
    ]
    assert compute_bundle_rollup(
        [bundle], blocked_prefix, [], reviews, now=_NOW
    )[0].critical_path_stage == 0


def test_rollup_cycle_is_not_dependency_closed() -> None:
    tasks = [
        Task(
            id=task_id,
            feature_id="F001",
            title=task_id,
            description="",
            status=TaskStatus.done,
            priority=TaskPriority.high,
            dependencies=[dependency],
            created_at=_NOW,
            updated_at=_NOW,
        )
        for task_id, dependency in (("T001", "T002"), ("T002", "T001"))
    ]
    bundle = ExecutionBundle(
        id="B001",
        creation_event_id="E001",
        prd_id="default",
        task_ids=["T001", "T002"],
        coordinator="coordinator",
        created_at=_NOW,
        updated_at=_NOW,
    )

    entry = compute_bundle_rollup([bundle], tasks, [], [], now=_NOW)[0]

    assert entry.critical_path_stage == 0
    assert entry.critical_path_depth == 0
    assert not entry.claimable
    assert "dependency_cycle" in {refusal["code"] for refusal in entry.refusals}


def test_rollup_prefers_active_coordinator_claim_generation() -> None:
    bundle = ExecutionBundle(
        id="B001",
        creation_event_id="E001",
        prd_id="default",
        task_ids=["T001"],
        coordinator="coordinator",
        created_at=_NOW,
        updated_at=_NOW,
    )
    claims = [
        BundleClaim(
            id="BCA",
            bundle_id="B001",
            claimed_by="coordinator",
            status="active",
            member_claim_ids={"T001": "C1"},
            created_at=_NOW,
            lease_expires_at=_NOW + timedelta(hours=1),
            last_heartbeat_at=_NOW,
        ),
        BundleClaim(
            id="BCZ",
            bundle_id="B001",
            claimed_by="coordinator",
            status="released",
            member_claim_ids={"T001": "C0"},
            created_at=_NOW - timedelta(hours=1),
            lease_expires_at=_NOW,
            last_heartbeat_at=_NOW - timedelta(hours=1),
            released_at=_NOW,
        ),
    ]
    entry = compute_bundle_rollup([bundle], [], claims, [], now=_NOW)[0]
    assert entry.coordinator_claim["id"] == "BCA"  # type: ignore[index]


def test_rollup_explains_all_bundle_refusal_classes() -> None:
    tasks = [
        Task(
            id="T001",
            feature_id="F001",
            title="Member one",
            description="",
            status=TaskStatus.ready,
            priority=TaskPriority.high,
            dependencies=["EXT"],
            likely_files=["src/shared.py"],
            created_at=_NOW,
            updated_at=_NOW,
        ),
        Task(
            id="T002",
            feature_id="F001",
            title="Member two",
            description="",
            status=TaskStatus.ready,
            priority=TaskPriority.high,
            dependencies=["T001"],
            created_at=_NOW,
            updated_at=_NOW,
        ),
        Task(
            id="EXT",
            feature_id="F001",
            title="External blocker",
            description="",
            status=TaskStatus.claimed,
            priority=TaskPriority.high,
            created_at=_NOW,
            updated_at=_NOW,
        ),
    ]
    bundle = ExecutionBundle(
        id="B001",
        creation_event_id="E001",
        prd_id="default",
        task_ids=["T001", "T002"],
        coordinator="coordinator",
        throughput_budget=BundleThroughputBudget(max_tasks=2, max_serial_stages=1),
        created_at=_NOW,
        updated_at=_NOW,
    )
    active_claim = Claim(
        id="C001",
        task_id="EXT",
        claimed_by="worker",
        expected_files=["src/shared.py"],
        created_at=_NOW,
        lease_expires_at=_NOW + timedelta(hours=1),
        last_heartbeat_at=_NOW,
    )

    entry = compute_bundle_rollup(
        [bundle], tasks, [], [], [active_claim], now=_NOW, actor="coordinator"
    )[0]
    assert {refusal["code"] for refusal in entry.refusals} >= {
        "throughput_serial_stages",
        "dependencies",
        "conflicts",
    }
    assert all(refusal["remediation"] for refusal in entry.refusals)
    assert not entry.claimable

    replan = compute_bundle_rollup(
        [bundle.model_copy(update={"status": BundleStatus.replan_required})],
        tasks,
        [],
        [],
        now=_NOW,
    )[0]
    assert "replan_required" in {refusal["code"] for refusal in replan.refusals}
    exhausted_bundle = bundle.model_copy(
        update={
            "status": BundleStatus.replan_required,
            "review_disposition_event_id": "E010",
        }
    )
    exhausted_reviews = [
        BundleReviewVerdict(
            id=f"BR{index}",
            bundle_id="B001",
            creation_event_id="E001",
            disposition_event_id="E010",
            review_round=2,
            angle=angle,
            reviewed_by=f"reviewer-{index}",
            decision=(
                ReviewDecision.reject
                if angle == "security"
                else ReviewDecision.approve
            ),
            created_at=_NOW,
        )
        for index, angle in enumerate(("correctness", "security", "integration"), 1)
    ]
    exhausted = compute_bundle_rollup(
        [exhausted_bundle], tasks, [], exhausted_reviews, now=_NOW
    )[0]
    assert "review_budget_exhausted" in {
        refusal["code"] for refusal in exhausted.refusals
    }
    wrong_angles = [
        review.model_copy(update={"angle": angle})
        for review, angle in zip(
            exhausted_reviews, ("foo", "bar", "baz"), strict=True
        )
    ]
    generic = compute_bundle_rollup(
        [exhausted_bundle], tasks, [], wrong_angles, now=_NOW
    )[0]
    assert "replan_required" in {refusal["code"] for refusal in generic.refusals}
    assert "review_budget_exhausted" not in {
        refusal["code"] for refusal in generic.refusals
    }
    superseded = compute_bundle_rollup(
        [
            bundle.model_copy(
                update={"status": BundleStatus.superseded, "superseded_by": "B002"}
            )
        ],
        tasks,
        [],
        [],
        now=_NOW,
    )[0]
    assert superseded.refusals[-1]["code"] == "superseded"
    assert "B002" in superseded.refusals[-1]["remediation"]


def test_next_bundle_recommends_claimable_bundle(tmp_path) -> None:
    state_dir = tmp_path / ".anvil"
    state_dir.mkdir()
    backend = _backend(state_dir)
    try:
        _seed(backend)
    finally:
        backend.close()

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "next",
            "--bundle",
            "--actor",
            "coordinator",
            "--json",
            "--cwd",
            str(tmp_path),
        ],
        env={"ANVIL_STATE_LAYOUT": "local"},
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["bundle"]["bundle_id"] == "B001"
    assert data["bundle"]["claimable"] is True
    assert data["bundle_refusals"] == []


@pytest.mark.parametrize(
    "filter_args",
    (["--type", "bugfix"], ["--max-blast", "1"], ["--max-review-risk", "1"]),
)
def test_next_bundle_rejects_task_only_filters(tmp_path, filter_args) -> None:
    state_dir = tmp_path / ".anvil"
    state_dir.mkdir()
    backend = _backend(state_dir)
    try:
        _seed(backend)
    finally:
        backend.close()

    result = CliRunner().invoke(
        app,
        ["next", "--bundle", "--json", "--cwd", str(tmp_path), *filter_args],
        env={"ANVIL_STATE_LAYOUT": "local"},
    )

    assert result.exit_code != 0
    error = json.loads(result.output)["error"]
    assert error["code"] == "invalid_bundle_filter"


def test_scoped_status_keeps_cross_prd_bundle_conflicts(tmp_path) -> None:
    from tests.test_bundle_state import (
        _T0,
        _claim_payload,
        _create_bundle,
    )
    from tests.test_bundle_state import (
        _backend as state_backend,
    )
    from tests.test_bundle_state import (
        _seed as state_seed,
    )

    state_dir = tmp_path / ".anvil"
    state_dir.mkdir()
    backend = state_backend(state_dir)
    try:
        state_seed(backend, second_prd=True)
        _create_bundle(backend)
        conn = backend._require_conn()
        conn.execute(
            "UPDATE tasks SET conflict_groups = '[\"shared\"]' "
            "WHERE id IN ('release:T001', 'other:T001')"
        )
        conn.commit()
        backend.append(
            EventDraft(
                timestamp=_T0,
                actor="worker",
                action="claim.created",
                target_kind="claim",
                target_id="C-OTHER",
                payload_json=_claim_payload("C-OTHER", "other:T001"),
            )
        )
    finally:
        backend.close()

    result = CliRunner().invoke(
        app,
        ["status", "--prd", "release", "--json", "--cwd", str(tmp_path)],
        env={"ANVIL_STATE_LAYOUT": "local"},
    )

    assert result.exit_code == 0, result.output
    bundle = json.loads(result.output)["data"]["bundles"][0]
    assert "conflicts" in {refusal["code"] for refusal in bundle["refusals"]}


def test_status_human_and_json_include_bundle_integration_rollup(tmp_path) -> None:
    state_dir = tmp_path / ".anvil"
    state_dir.mkdir()
    backend = _backend(state_dir)
    try:
        _implement_bundle(backend, tmp_path)
        for reviewer, angle in (
            ("reviewer-a", "correctness"),
            ("reviewer-b", "security"),
            ("reviewer-c", "integration"),
        ):
            BundleReviewManager(
                backend, FrozenClock(_EXEC_NOW), actor=reviewer
            ).record(
                "B001",
                review_round=1,
                angle=angle,
                decision=ReviewDecision.approve,
            )
        BundleReviewManager(
            backend, FrozenClock(_EXEC_NOW), actor="coordinator"
        ).finalize("B001")
    finally:
        backend.close()
    runner = CliRunner()
    env = {"ANVIL_STATE_LAYOUT": "local"}
    json_result = runner.invoke(
        app, ["status", "--json", "--cwd", str(tmp_path)], env=env
    )
    assert json_result.exit_code == 0, json_result.output
    bundles = json.loads(json_result.output)["data"]["bundles"]
    assert bundles[0]["bundle_id"] == "B001"
    assert bundles[0]["status"] == "reviewed_unintegrated"
    assert bundles[0]["critical_path_depth"] == 1
    human = runner.invoke(app, ["status", "--cwd", str(tmp_path)], env=env)
    assert human.exit_code == 0, human.output
    assert "Bundle B001 (reviewed_unintegrated)" in human.output
    assert "critical-path" in human.output
    assert "optional-worker" in human.output
    assert "Last result:" in human.output
    assert "elapsed=" in human.output
