"""Bounded dispatch advice and exact batch preview authorization."""

from __future__ import annotations

import json
import statistics
import time
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from anvil.bundles.execution import build_bundle_execution_plan, bundle_execution_scope_digest
from anvil.bundles.manager import BundleError, BundleManager
from anvil.cli import app
from anvil.clock import FrozenClock
from anvil.state.backend import EventRejected
from anvil.state.models import Event, TaskStatus
from tests.test_bundle_state import _T0, _backend, _create_bundle, _event, _seed


def _manager(backend, root: Path) -> BundleManager:
    return BundleManager(backend, FrozenClock(_T0), actor="codex-main", project_root=root)


def _fixture(root: Path):
    backend = _backend(root)
    _seed(backend)
    _create_bundle(backend)
    return backend


def _score(backend, task_id="release:T001"):
    backend.append(_event(
        "task.scored", {"task_id": task_id, "scores": {}, "explanation": "Updated priority"},
        target_kind="task", target_id=task_id,
    ))


def test_plan_is_bounded_ordered_read_only_and_skips_proof_verification(tmp_path):
    backend = _fixture(tmp_path)
    try:
        _manager(backend, tmp_path).claim("B001")
        before = (tmp_path / "events.jsonl").read_bytes()
        with patch("anvil.bundles.manager.evaluate_claims", side_effect=AssertionError("hot proof")):
            plan = build_bundle_execution_plan(backend, "B001", max_workers=1, now=_T0)
        assert plan["wave"] == ["release:T002"]
        assert plan["counts"]["runnable"] == 1
        assert plan["members"][1]["blockers"] == ["worker_limit"]
        assert all(member["claim_generation"] == 1 for member in plan["members"])
        assert plan["prd"]["content_event_id"]
        assert "failed" not in plan["counts"]
        assert (tmp_path / "events.jsonl").read_bytes() == before
        expired = build_bundle_execution_plan(backend, "B001", now=_T0 + timedelta(hours=5))
        assert expired["wave"] == []
        assert "coordinator_lease_expired" in expired["blockers"]
        assert backend.get_bundle_claim("B001").status.value == "active"
    finally:
        backend.close()


def test_running_worker_reserves_capacity_but_review_does_not(tmp_path):
    backend = _fixture(tmp_path)
    try:
        _manager(backend, tmp_path).claim("B001")
        backend.append(_event(
            "task.status_changed", {"task_id": "release:T002", "from": "claimed", "to": "in_progress"},
            target_kind="task", target_id="release:T002", actor="codex-main",
        ))
        assert build_bundle_execution_plan(backend, "B001", max_workers=1, now=_T0)["wave"] == []
        backend.append(_event(
            "task.status_changed", {"task_id": "release:T002", "from": "in_progress", "to": "needs_review"},
            target_kind="task", target_id="release:T002", actor="codex-main",
        ))
        plan = build_bundle_execution_plan(backend, "B001", max_workers=1, now=_T0)
        assert plan["wave"] == ["release:T001"]
        assert plan["counts"]["pending_review"] == 1
        assert plan["counts"]["completed"] == 0
    finally:
        backend.close()


@pytest.mark.parametrize("scope", ["paths", "groups", "dependency"])
def test_wave_excludes_conflicts_and_unaccepted_dependencies(tmp_path, scope):
    backend = _fixture(tmp_path)
    try:
        _manager(backend, tmp_path).claim("B001")
        read_task = backend.get_task

        def scoped_task(task_id):
            task = read_task(task_id)
            if scope == "paths":
                return task.model_copy(update={"likely_files": ["./src/File.py" if task_id.endswith("1") else "src/file.py"]})
            if scope == "groups":
                return task.model_copy(update={"conflict_groups": ["shared"]})
            if task_id.endswith("1"):
                return task.model_copy(update={"dependencies": ["release:T002"]})
            return task.model_copy(update={"status": TaskStatus.needs_review})

        with patch.object(backend, "get_task", side_effect=scoped_task):
            plan = build_bundle_execution_plan(backend, "B001", now=_T0)
        assert plan["wave"] == ([] if scope == "dependency" else ["release:T002"])
        assert plan["members"][1]["blockers"]
    finally:
        backend.close()


def test_scope_cas_rejects_changed_member_without_partial_claim(tmp_path):
    backend = _fixture(tmp_path)
    try:
        digest = bundle_execution_scope_digest(backend, "B001")
        _score(backend)
        before = (tmp_path / "events.jsonl").read_bytes()
        with pytest.raises(BundleError, match="bundle_scope_changed"):
            _manager(backend, tmp_path).claim("B001", expected_scope_digest=digest)
        assert (tmp_path / "events.jsonl").read_bytes() == before
        assert backend.get_bundle_claim("B001") is None
        assert backend.list_claims() == []
        digest = bundle_execution_scope_digest(backend, "B001")
        _manager(backend, tmp_path).claim("B001", expected_scope_digest=digest)
        event = json.loads((tmp_path / "events.jsonl").read_text().splitlines()[-1])
        assert event["payload_json"]["expected_scope_digest"] == digest
        backend.replay_from_empty(str(tmp_path / "events.jsonl"))
        assert backend.get_bundle_claim("B001") is not None
    finally:
        backend.close()


def test_unrelated_task_does_not_invalidate_bundle_preview(tmp_path):
    backend = _backend(tmp_path)
    try:
        _seed(backend, second_prd=True)
        _create_bundle(backend)
        digest = bundle_execution_scope_digest(backend, "B001")
        unrelated = next(task for task in backend.list_tasks() if task.prd_id != "release")
        _score(backend, unrelated.id)
        assert bundle_execution_scope_digest(backend, "B001") == digest
        _manager(backend, tmp_path).claim("B001", expected_scope_digest=digest)
    finally:
        backend.close()


def test_reducer_rejects_and_replay_skips_stale_scope(tmp_path):
    backend = _fixture(tmp_path)
    try:
        digest = bundle_execution_scope_digest(backend, "B001")
        _score(backend)
        from tests.test_bundle_state import _bundle_claim_payload
        payload = _bundle_claim_payload(backend)
        payload["expected_scope_digest"] = digest
        draft = _event("bundle.claimed", payload, target_kind="bundle", target_id="B001", actor="codex-main")
        with pytest.raises(EventRejected, match="bundle_scope_changed"):
            backend.append(draft)
        lines = (tmp_path / "events.jsonl").read_text().splitlines()
        raw_event = Event(id=f"E{len(lines) + 1:06d}", **draft.model_dump())
        with (tmp_path / "events.jsonl").open("a") as handle:
            handle.write(raw_event.model_dump_json() + "\n")
        backend.replay_from_empty(str(tmp_path / "events.jsonl"))
        assert backend.get_bundle_claim("B001") is None
        assert backend.list_claims() == []
    finally:
        backend.close()


def test_cli_execution_plan_does_not_initialize_or_reap_state(tmp_path, monkeypatch):
    backend = _fixture(tmp_path)
    backend.close()
    monkeypatch.setattr("anvil.cli.bundle._resolve_state_dir", lambda _cwd: tmp_path)
    monkeypatch.setattr("anvil.cli.bundle._open_backend", lambda *a, **k: pytest.fail("initializer used"))
    before = (tmp_path / "events.jsonl").read_bytes()
    result = CliRunner().invoke(app, ["bundle", "execution-plan", "B001", "--max-workers", "2", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["bundle_id"] == "B001"
    assert data["max_workers"] == 2
    assert data["wave"] == []
    assert len(data["scope_digest"]) == 64
    assert (tmp_path / "events.jsonl").read_bytes() == before


@pytest.mark.parametrize("member_count", [12, 100, 500])
def test_bounded_large_cohort_plan_and_claim(tmp_path, member_count):
    """Exercise the native cohort bound; timings are observations, not an SLO."""
    from tests.test_bundle_state import _bundle_payload

    backend = _backend(tmp_path)
    try:
        _seed(backend)
        template = backend.get_task("release:T001").model_dump(mode="json")
        task_ids = [f"release:T{index:03d}" for index in range(1, member_count + 1)]
        for task_id in task_ids[2:]:
            backend.append(_event("task.created", {
                **template, "id": task_id, "title": task_id, "prd_id": "release",
            }, target_kind="task", target_id=task_id))
        payload = _bundle_payload(task_ids=task_ids)
        payload["throughput_budget"] = {"max_tasks": member_count, "max_serial_stages": 1}
        backend.append(_event("bundle.created", payload, target_kind="bundle", target_id="B001"))
        digest = bundle_execution_scope_digest(backend, "B001")
        started = time.perf_counter_ns()
        _manager(backend, tmp_path).claim("B001", expected_scope_digest=digest)
        claim_ms = (time.perf_counter_ns() - started) / 1_000_000
        timings = []
        for _ in range(25):
            started = time.perf_counter_ns()
            plan = build_bundle_execution_plan(backend, "B001", max_workers=8, now=_T0)
            timings.append((time.perf_counter_ns() - started) / 1_000_000)
        assert plan["wave"] == task_ids[:8]
        assert plan["counts"]["total"] == member_count
        assert len(backend.list_claims()) == member_count
        print(json.dumps({
            "bundle_members": member_count, "workers": 8, "plan_samples": len(timings),
            "plan_median_ms": round(statistics.median(timings), 3),
            "plan_p95_ms": round(sorted(timings)[23], 3),
            "atomic_claim_single_sample_ms": round(claim_ms, 3),
            "scope": "isolated State, in-process; host load uncontrolled; CLI startup excluded",
        }, sort_keys=True))
    finally:
        backend.close()


def test_scoped_internal_dependency_claim_rejected_by_manager_live_and_replay(tmp_path):
    from tests.test_bundle_execution import (
        _bundle_claim_payload,
    )
    from tests.test_bundle_execution import (
        _event as bundle_event,
    )
    from tests.test_bundle_execution import (
        _manager as bundle_manager,
    )
    from tests.test_bundle_execution import (
        _seed as seed_bundle,
    )

    backend = _backend(tmp_path)
    try:
        seed_bundle(backend, internal_dependency=True)
        digest = bundle_execution_scope_digest(backend, "B001")
        before = (tmp_path / "events.jsonl").read_bytes()
        with pytest.raises(BundleError, match="bundle_internal_dependencies"):
            bundle_manager(backend, tmp_path).claim("B001", expected_scope_digest=digest)
        payload = _bundle_claim_payload(backend, "BC-INTERNAL")
        payload["expected_scope_digest"] = digest
        draft = bundle_event("bundle.claimed", "bundle", "B001", payload, actor="coordinator")
        with pytest.raises(EventRejected, match="bundle_internal_dependencies"):
            backend.append(draft)
        assert (tmp_path / "events.jsonl").read_bytes() == before
        assert backend.get_bundle_claim("B001") is None
        assert backend.list_claims() == []
        raw_event = Event(id=f"E{len(before.splitlines()) + 1:06d}", **draft.model_dump())
        with (tmp_path / "events.jsonl").open("a") as handle:
            handle.write(raw_event.model_dump_json() + "\n")
        backend.replay_from_empty(str(tmp_path / "events.jsonl"))
        assert backend.get_bundle_claim("B001") is None
        assert backend.list_claims() == []
        bundle_manager(backend, tmp_path).claim("B001")
        plan = build_bundle_execution_plan(backend, "B001", now=_T0)
        assert not plan["fanout_eligible"]
        assert "internal_dependencies_require_sequential_bundle_flow" in plan["blockers"]
        assert plan["wave"] == []
    finally:
        backend.close()


def test_scope_digest_binds_project_identity_and_refuses_missing_identity(tmp_path):
    from anvil.bundles.execution import BundleExecutionPlanError
    from tests.test_bundle_state import _bundle_claim_payload

    backend = _fixture(tmp_path)
    try:
        digest = bundle_execution_scope_digest(backend, "B001")
        project = backend.get_project()
        before = (tmp_path / "events.jsonl").read_bytes()
        with patch.object(backend, "get_project", return_value=project.model_copy(update={"id": "replacement"})):
            replacement_digest = bundle_execution_scope_digest(backend, "B001")
            assert replacement_digest != digest
            with pytest.raises(BundleError, match="bundle_scope_changed"):
                _manager(backend, tmp_path).claim("B001", expected_scope_digest=digest)
        payload = _bundle_claim_payload(backend)
        payload["expected_scope_digest"] = replacement_digest
        draft = _event("bundle.claimed", payload, target_kind="bundle", target_id="B001", actor="codex-main")
        with pytest.raises(EventRejected, match="bundle_scope_changed"):
            backend.append(draft)
        with patch.object(backend, "get_project", return_value=None):
            with pytest.raises(BundleExecutionPlanError, match="identity"):
                bundle_execution_scope_digest(backend, "B001")
        assert (tmp_path / "events.jsonl").read_bytes() == before
        assert backend.list_claims() == []
        raw_event = Event(id=f"E{len(before.splitlines()) + 1:06d}", **draft.model_dump())
        with (tmp_path / "events.jsonl").open("a") as handle:
            handle.write(raw_event.model_dump_json() + "\n")
        backend.replay_from_empty(str(tmp_path / "events.jsonl"))
        assert backend.get_bundle_claim("B001") is None
        assert backend.list_claims() == []
    finally:
        backend.close()


@pytest.mark.parametrize("paths", [["src/moved.py"], ["../unsafe.py"]])
def test_running_member_retains_frozen_claim_paths_after_metadata_upsert(tmp_path, paths):
    from tests.test_bundle_execution import (
        _event as bundle_event,
    )
    from tests.test_bundle_execution import (
        _manager as bundle_manager,
    )
    from tests.test_bundle_execution import (
        _seed as seed_bundle,
    )

    backend = _backend(tmp_path)
    try:
        seed_bundle(backend)
        bundle_manager(backend, tmp_path).claim(
            "B001", expected_scope_digest=bundle_execution_scope_digest(backend, "B001"),
        )
        backend.append(bundle_event("task.status_changed", "task", "release:T001", {
            "task_id": "release:T001", "from": "claimed", "to": "in_progress",
        }, actor="coordinator"))
        task = backend.get_task("release:T001")
        backend.append(bundle_event("task.created", "task", task.id, {
            **task.model_dump(mode="json"), "prd_id": task.prd_id,
            "status": "in_progress", "likely_files": paths,
        }))
        plan = build_bundle_execution_plan(backend, "B001", now=_T0)
        assert plan["wave"] == []
        if paths == ["../unsafe.py"]:
            assert "running_member_requires_attention" in plan["blockers"]
        else:
            assert "conflicts_with_running_or_selected_member" in plan["members"][1]["blockers"]
    finally:
        backend.close()
