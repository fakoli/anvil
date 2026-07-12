"""Integration-focused bundle status rollups."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from typer.testing import CliRunner

from anvil.bundles.review import BundleReviewManager
from anvil.cli import app
from anvil.clock import FrozenClock
from anvil.state.models import (
    BundleCheckpoint,
    BundleClaim,
    BundleReviewVerdict,
    BundleStatus,
    ExecutionBundle,
    ReviewDecision,
    Task,
    TaskPriority,
    TaskStatus,
)
from anvil.state.rollup import compute_bundle_rollup
from tests.test_bundle_execution import _NOW as _EXEC_NOW
from tests.test_bundle_execution import _backend, _implement_bundle

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
        result_times={"B001": _NOW},
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
        result_times={"B001": _NOW},
    )[0].checkpoint_warning is None

    blocked_prefix = [
        tasks[0].model_copy(update={"status": TaskStatus.needs_review}),
        tasks[1].model_copy(update={"status": TaskStatus.done}),
    ]
    assert compute_bundle_rollup(
        [bundle], blocked_prefix, [], reviews, now=_NOW
    )[0].critical_path_stage == 0


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
