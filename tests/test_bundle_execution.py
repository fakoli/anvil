"""Coordinator-first execution flow for milestone bundles (issue #171)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from anvil.bundles.manager import BundleError, BundleManager
from anvil.cli import app
from anvil.clock import FrozenClock
from anvil.state.models import BundleStatus, EventDraft
from anvil.state.sqlite import SqliteBackend

_NOW = datetime(2026, 7, 11, 18, 0, tzinfo=UTC)


def _event(action: str, target_kind: str, target_id: str, payload: dict) -> EventDraft:
    return EventDraft(
        timestamp=_NOW,
        actor="seed",
        action=action,
        target_kind=target_kind,
        target_id=target_id,
        payload_json=payload,
    )


def _backend(root: Path) -> SqliteBackend:
    events = root / "events.jsonl"
    events.touch()
    backend = SqliteBackend(
        db_path=str(root / "state.db"),
        events_path=str(events),
        clock=FrozenClock(_NOW),
    )
    backend.initialize()
    return backend


def _seed(
    backend: SqliteBackend,
    *,
    internal_dependency: bool = False,
    external_dependency_status: str | None = None,
) -> str:
    backend.append(
        _event(
            "project.created",
            "project",
            "proj",
            {
                "id": "proj",
                "name": "Bundle execution",
                "description": "",
                "created_at": _NOW.isoformat(),
                "updated_at": _NOW.isoformat(),
            },
        )
    )
    backend.append(
        _event(
            "prd.parsed",
            "prd",
            "release",
            {
                "project_id": "proj",
                "prd_id": "release",
                "title": "Release",
                "is_default": False,
                "status": "approved",
                "summary": "",
                "goals": [],
                "non_goals": [],
                "requirements": [
                    {
                        "id": "release:R001",
                        "prd_id": "release",
                        "prd_section": "Requirements",
                        "text": "Ship coherently.",
                        "source_paragraph": None,
                        "derived": False,
                    }
                ],
                "acceptance_criteria": [],
                "risks": [],
                "open_questions": [],
            },
        )
    )
    task_ids = ["release:T001", "release:T002"]
    if external_dependency_status is not None:
        task_ids.append("release:T000")
    backend.append(
        _event(
            "feature.created",
            "feature",
            "release:F001",
            {
                "id": "release:F001",
                "prd_id": "release",
                "title": "Execution",
                "description": "",
                "status": "ready",
                "requirements": ["release:R001"],
                "tasks": task_ids,
            },
        )
    )
    for index, task_id in enumerate(task_ids):
        dependencies: list[str] = []
        status = "ready"
        if task_id == "release:T002" and internal_dependency:
            dependencies = ["release:T001"]
        if task_id == "release:T002" and external_dependency_status is not None:
            dependencies = ["release:T000"]
        if task_id == "release:T000":
            status = external_dependency_status or "ready"
        backend.append(
            _event(
                "task.created",
                "task",
                task_id,
                {
                    "id": task_id,
                    "feature_id": "release:F001",
                    "prd_id": "release",
                    "title": task_id,
                    "description": f"Member {index}",
                    "status": status,
                    "priority": "high",
                    "task_type": "feature",
                    "dependencies": dependencies,
                    "conflict_groups": ["shared"] if task_id.endswith("T002") else [],
                    "scores": {},
                    "acceptance_criteria": [f"{task_id} accepted"],
                    "implementation_notes": [],
                    "verification": {"commands": [f"verify-{task_id}"]},
                    "likely_files": [f"src/{task_id[-1]}.py", "src/shared.py"],
                    "parent_task_id": None,
                    "created_at": _NOW.isoformat(),
                    "updated_at": _NOW.isoformat(),
                },
            )
        )
    created = backend.append(
        _event(
            "bundle.created",
            "bundle",
            "B001",
            {
                "id": "B001",
                "prd_id": "release",
                "task_ids": ["release:T001", "release:T002"],
                "coordinator": "coordinator",
                "status": "planned",
                "review_policy": {},
                "throughput_budget": {},
                "delegated_agents": [
                    {
                        "id": "optional-worker",
                        "task_ids": ["release:T002"],
                        "status": "missing",
                        "observed_at": _NOW.isoformat(),
                    }
                ],
                "created_at": _NOW.isoformat(),
                "updated_at": _NOW.isoformat(),
            },
        )
    )
    assert created is not None
    return created.id


def _manager(backend: SqliteBackend, root: Path) -> BundleManager:
    return BundleManager(
        backend,
        FrozenClock(_NOW),
        actor="coordinator",
        project_root=root,
    )


def test_claim_creates_one_public_claim_and_ordered_member_authorizations(
    tmp_path: Path,
) -> None:
    backend = _backend(tmp_path)
    try:
        _seed(backend, internal_dependency=True)
        result = _manager(backend, tmp_path).claim(
            "B001", branch="agent/bundle-b001", worktree_path="C:/wt/b001"
        )
        assert result.bundle.status is BundleStatus.active
        assert result.bundle.branch == "agent/bundle-b001"
        assert result.claim.bundle_id == "B001"
        assert list(result.claim.member_claim_ids) == ["release:T001", "release:T002"]
        assert len(backend.list_bundle_claims()) == 1
        child_claims = backend.list_active_claims()
        assert {claim.bundle_claim_id for claim in child_claims} == {result.claim.id}
        assert {claim.task_id for claim in child_claims} == {
            "release:T001",
            "release:T002",
        }
        assert all(
            backend.get_task(task_id).status.value == "claimed"  # type: ignore[union-attr]
            for task_id in result.bundle.task_ids
        )
        packet = _manager(backend, tmp_path).packet("B001")
        assert [member["task"]["id"] for member in packet.json_data["members"]] == [
            "release:T001",
            "release:T002",
        ]
        assert "release:R001: Ship coherently." in packet.markdown
        assert packet.json_data["aggregate"]["shared_file_overlaps"] == [
            {
                "path": "src/shared.py",
                "tasks": ["release:T001", "release:T002"],
            }
        ]
        assert "verify-release:T001" in packet.markdown
        assert result.claim.member_claim_ids["release:T002"] in packet.markdown
    finally:
        backend.close()


def test_external_dependency_refuses_atomically_but_done_dependency_allows(
    tmp_path: Path,
) -> None:
    blocked_root = tmp_path / "blocked"
    allowed_root = tmp_path / "allowed"
    blocked_root.mkdir()
    allowed_root.mkdir()
    blocked = _backend(blocked_root)
    try:
        _seed(blocked, external_dependency_status="ready")
        before = (blocked_root / "events.jsonl").read_text(encoding="utf-8")
        with pytest.raises(BundleError, match="external dependencies are not done"):
            _manager(blocked, blocked_root).claim("B001")
        assert blocked.get_bundle_claim("B001") is None
        assert blocked.get_bundle("B001").status is BundleStatus.planned  # type: ignore[union-attr]
        assert (blocked_root / "events.jsonl").read_text(encoding="utf-8") == before
    finally:
        blocked.close()
    allowed = _backend(allowed_root)
    try:
        _seed(allowed, external_dependency_status="done")
        assert _manager(allowed, allowed_root).claim("B001").bundle.status is BundleStatus.active
    finally:
        allowed.close()


def test_partial_evidence_reports_exact_unproven_members_and_agents_do_not_gate(
    tmp_path: Path,
) -> None:
    backend = _backend(tmp_path)
    try:
        _seed(backend)
        manager = _manager(backend, tmp_path)
        result = manager.claim("B001")
        manager.note_progress(
            "B001", phase="build", member_task_ids=["release:T001"]
        )
        first_claim = result.claim.member_claim_ids["release:T001"]
        backend.append(
            _event(
                "evidence.submitted",
                "task",
                "release:T001",
                {
                    "task_id": "release:T001",
                    "claim_id": first_claim,
                    "submitted_by": "coordinator",
                    "evidence_id": "EV-T001",
                    "commands_run": ["verify-release:T001"],
                    "files_changed": ["src/1.py"],
                },
            )
        )
        partial = manager.mark_implemented("B001")
        assert not partial.can_mark_implemented
        assert list(partial.unproven_members) == ["release:T002"]
        assert backend.get_bundle("B001").status is BundleStatus.active  # type: ignore[union-attr]

        second_claim = result.claim.member_claim_ids["release:T002"]
        backend.append(
            _event(
                "evidence.submitted",
                "task",
                "release:T002",
                {
                    "task_id": "release:T002",
                    "claim_id": second_claim,
                    "submitted_by": "coordinator",
                    "evidence_id": "EV-T002",
                    "commands_run": ["verify-release:T002"],
                    "files_changed": ["src/2.py"],
                },
            )
        )
        complete = manager.mark_implemented("B001")
        assert complete.can_mark_implemented
        assert complete.unproven_members == {}
        assert (
            backend.get_bundle("B001").status
            is BundleStatus.implemented_unreviewed  # type: ignore[union-attr]
        )
        assert all(
            backend.get_task(task_id).status.value == "needs_review"  # type: ignore[union-attr]
            for task_id in ("release:T001", "release:T002")
        )
    finally:
        backend.close()


def test_union_file_conflict_refuses_whole_bundle(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    try:
        _seed(backend)
        backend.append(
            _event(
                "task.created",
                "task",
                "release:T999",
                {
                    "id": "release:T999",
                    "feature_id": "release:F001",
                    "prd_id": "release",
                    "title": "External work",
                    "description": "",
                    "status": "ready",
                    "priority": "high",
                    "task_type": "feature",
                    "dependencies": [],
                    "conflict_groups": [],
                    "scores": {},
                    "acceptance_criteria": [],
                    "implementation_notes": [],
                    "verification": {"commands": ["verify"]},
                    "likely_files": ["src/external.py"],
                    "parent_task_id": None,
                    "created_at": _NOW.isoformat(),
                    "updated_at": _NOW.isoformat(),
                },
            )
        )
        backend.append(
            _event(
                "claim.created",
                "claim",
                "C-EXTERNAL",
                {
                    "id": "C-EXTERNAL",
                    "task_id": "release:T999",
                    "claimed_by": "other",
                    "claim_type": "task",
                    "status": "active",
                    "expected_files": ["src/shared.py"],
                    "created_at": _NOW.isoformat(),
                    "lease_expires_at": (_NOW + timedelta(hours=1)).isoformat(),
                    "last_heartbeat_at": _NOW.isoformat(),
                },
            )
        )
        with pytest.raises(BundleError, match="conflicts with active claims"):
            _manager(backend, tmp_path).claim("B001")
        assert backend.get_bundle_claim("B001") is None
        assert all(
            backend.get_task(task_id).status.value == "ready"  # type: ignore[union-attr]
            for task_id in ("release:T001", "release:T002")
        )
    finally:
        backend.close()


def test_bundle_claim_replays_byte_equivalently(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    replay_root = tmp_path / "replay"
    source_root.mkdir()
    replay_root.mkdir()
    source = _backend(source_root)
    replay = _backend(replay_root)
    try:
        _seed(source, internal_dependency=True)
        _manager(source, source_root).claim("B001", branch="agent/b001")
        replay.replay_from_empty(str(source_root / "events.jsonl"))
        assert replay.get_bundle("B001") == source.get_bundle("B001")
        assert replay.get_bundle_claim("B001") == source.get_bundle_claim("B001")
        assert replay.list_claims() == source.list_claims()
    finally:
        source.close()
        replay.close()


def test_bundle_claim_packet_and_progress_cli_share_coordinator_flow(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / ".anvil"
    state_dir.mkdir()
    backend = _backend(state_dir)
    try:
        _seed(backend)
    finally:
        backend.close()
    runner = CliRunner()
    env = {"ANVIL_STATE_LAYOUT": "local"}
    claimed = runner.invoke(
        app,
        [
            "claim",
            "B001",
            "--bundle",
            "--actor",
            "coordinator",
            "--branch",
            "agent/b001",
            "--cwd",
            str(tmp_path),
        ],
        env=env,
    )
    assert claimed.exit_code == 0, claimed.output
    assert "coordinator claim" in claimed.output

    packet = runner.invoke(
        app,
        ["packet", "B001", "--bundle", "--cwd", str(tmp_path)],
        env=env,
    )
    assert packet.exit_code == 0, packet.output
    assert "Ordered member work" in packet.output
    assert (state_dir / "packets" / "B001.md").is_file()

    progress = runner.invoke(
        app,
        [
            "progress",
            "B001",
            "build",
            "--bundle",
            "--actor",
            "coordinator",
            "--cwd",
            str(tmp_path),
        ],
        env=env,
    )
    assert progress.exit_code == 0, progress.output
    assert "Progress recorded for bundle" in progress.output
