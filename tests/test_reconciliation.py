"""Tests for fakoli_state.sync.reconciliation — Phase 8 Task 5.

Coverage strategy: real local git tmpdirs (no mocks for git), real
SqliteBackend (no in-memory stub), so every test exercises the actual
subprocess + SQLite paths the engine uses in production. The cost is
~50ms per test for git init/commit; the benefit is no false-green from
wrong subprocess assumptions.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from fakoli_state.clock import FrozenClock
from fakoli_state.state.models import EventDraft, SyncMapping
from fakoli_state.state.sqlite import SqliteBackend
from fakoli_state.sync.reconciliation import (
    LOCAL_DRIFT_KINDS,
    Discrepancy,
    DiscrepancyKind,
    FixAction,
    ReconciliationEngine,
    ReconciliationReport,
    Severity,
)

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

_T0 = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)


def _make_clock(dt: datetime = _T0) -> FrozenClock:
    return FrozenClock(dt)


def _make_backend(state_dir: Path, clock: FrozenClock | None = None) -> SqliteBackend:
    if clock is None:
        clock = _make_clock()
    db_path = str(state_dir / "state.db")
    events_path = str(state_dir / "events.jsonl")
    Path(events_path).touch()
    b = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock)
    b.initialize()
    return b


def _git(cwd: Path, *args: str) -> None:
    """Run a git command, raising on non-zero. Stderr captured for debug."""
    r = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=10,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {cwd}: {r.stderr or r.stdout}"
        )


def _init_git_repo(repo: Path) -> None:
    """Initialise a minimal git repo with one commit on ``main``."""
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    # An empty commit so the branch has a HEAD.
    _git(repo, "commit", "--allow-empty", "-q", "-m", "init")


def _make_event(
    action: str,
    payload: dict[str, Any],
    *,
    event_id: str = "unused",
    target_kind: str,
    target_id: str,
    now: datetime = _T0,
) -> EventDraft:
    """Return an EventDraft (SL1-RR-1: id is assigned by backend, event_id ignored)."""
    return EventDraft(
        timestamp=now,
        actor="test",
        action=action,
        target_kind=target_kind,
        target_id=target_id,
        payload_json=payload,
    )


def _make_task_payload(
    *,
    task_id: str = "T001",
    feature_id: str = "F001",
    title: str = "Test Task",
    status: str = "ready",
    now: datetime = _T0,
) -> dict[str, Any]:
    return {
        "id": task_id,
        "feature_id": feature_id,
        "title": title,
        "description": "",
        "status": status,
        "priority": "medium",
        "dependencies": [],
        "conflict_groups": [],
        "scores": {},
        "acceptance_criteria": ["ok"],
        "implementation_notes": [],
        "verification": {
            "commands": ["pytest"],
            "manual_steps": [],
            "required_evidence": [],
        },
        "likely_files": [],
        "parent_task_id": None,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
    }


def _setup_project(b: SqliteBackend, now: datetime = _T0) -> None:
    """Seed project + state.initialized so FK constraints are satisfied."""
    b.append(_make_event(
        "project.created",
        {
            "id": "proj-1",
            "name": "Test",
            "description": "",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        },
        event_id="E000001",
        target_kind="project",
        target_id="proj-1",
        now=now,
    ))
    b.append(_make_event(
        "state.initialized", {},
        event_id="E000002", target_kind="project", target_id="proj-1",
        now=now,
    ))


def _setup_task(
    b: SqliteBackend,
    *,
    task_id: str = "T001",
    feature_id: str = "F001",
    status: str = "ready",
    base_event_id: int = 3,
) -> None:
    """Create a feature + a task in the given status."""
    b.append(_make_event(
        "feature.created",
        {
            "id": feature_id, "title": "F", "description": "",
            "status": "proposed", "requirements": [], "tasks": [],
        },
        event_id=f"E{base_event_id:06d}",
        target_kind="feature", target_id=feature_id,
    ))
    b.append(_make_event(
        "task.created",
        _make_task_payload(task_id=task_id, feature_id=feature_id, status=status),
        event_id=f"E{base_event_id + 1:06d}",
        target_kind="task", target_id=task_id,
    ))


def _create_active_claim(
    b: SqliteBackend,
    *,
    claim_id: str = "C001",
    task_id: str = "T001",
    lease_expires_at: datetime | None = None,
    base_event_id: int = 5,
    now: datetime = _T0,
) -> None:
    """Insert a claim.created event whose task must be in 'ready' state."""
    if lease_expires_at is None:
        lease_expires_at = now + timedelta(hours=1)
    b.append(_make_event(
        "claim.created",
        {
            "id": claim_id,
            "task_id": task_id,
            "claimed_by": "agent-test",
            "claim_type": "task",
            "status": "active",
            "branch": None,
            "worktree_path": None,
            "expected_files": [],
            "created_at": now.isoformat(),
            "lease_expires_at": lease_expires_at.isoformat(),
            "last_heartbeat_at": now.isoformat(),
        },
        event_id=f"E{base_event_id:06d}",
        target_kind="claim", target_id=claim_id,
    ))


# ---------------------------------------------------------------------------
# Empty-project scan
# ---------------------------------------------------------------------------


class TestEmptyProject:
    """A fresh project with nothing in it produces an empty report."""

    def test_empty_project_returns_empty_report(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path)
        b = _make_backend(tmp_path)
        try:
            engine = ReconciliationEngine(
                b, state_dir=tmp_path, clock=_make_clock(),
            )
            report = engine.scan()
            assert report.discrepancies == []
            assert report.summary == {}
            assert report.scanned_at == _T0
        finally:
            b.close()

    def test_no_git_no_packets_dir_still_works(self, tmp_path: Path) -> None:
        """Engine survives when there's no git repo and no packets dir."""
        # No _init_git_repo, no packets dir.
        b = _make_backend(tmp_path)
        try:
            engine = ReconciliationEngine(
                b, state_dir=tmp_path, clock=_make_clock(),
            )
            report = engine.scan()
            assert report.discrepancies == []
        finally:
            b.close()


# ---------------------------------------------------------------------------
# Check 1 — orphan_branch
# ---------------------------------------------------------------------------


class TestOrphanBranch:
    """``agent/t*-*`` branches whose task id is not in the SQLite store."""

    def test_orphan_branch_detected_when_present(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path)
        # Create an agent branch that does NOT correspond to any task.
        _git(tmp_path, "branch", "agent/t099-orphaned")
        b = _make_backend(tmp_path)
        try:
            engine = ReconciliationEngine(b, state_dir=tmp_path, clock=_make_clock())
            report = engine.scan()
            kinds = [d.kind for d in report.discrepancies]
            assert DiscrepancyKind.orphan_branch in kinds
            d = next(d for d in report.discrepancies
                     if d.kind == DiscrepancyKind.orphan_branch)
            assert d.target_id == "agent/t099-orphaned"
            assert d.severity == Severity.warning
            assert d.suggested_fix == "git branch -D agent/t099-orphaned"
            assert d.payload["task_id"] == "T099"
            assert d.target_kind == "branch"
        finally:
            b.close()

    def test_orphan_branch_not_detected_when_task_exists(
        self, tmp_path: Path,
    ) -> None:
        _init_git_repo(tmp_path)
        _git(tmp_path, "branch", "agent/t001-my-task")
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            _setup_task(b, task_id="T001")
            engine = ReconciliationEngine(b, state_dir=tmp_path, clock=_make_clock())
            report = engine.scan()
            orphans = [d for d in report.discrepancies
                       if d.kind == DiscrepancyKind.orphan_branch]
            assert orphans == []
        finally:
            b.close()

    def test_non_agent_branches_ignored(self, tmp_path: Path) -> None:
        """``main``, ``feature/foo``, ``release/v1`` are never orphan_branch."""
        _init_git_repo(tmp_path)
        _git(tmp_path, "branch", "feature/random")
        _git(tmp_path, "branch", "release/v1")
        b = _make_backend(tmp_path)
        try:
            engine = ReconciliationEngine(b, state_dir=tmp_path, clock=_make_clock())
            report = engine.scan()
            assert [d for d in report.discrepancies
                    if d.kind == DiscrepancyKind.orphan_branch] == []
        finally:
            b.close()

    def test_orphan_branch_fix_remediates(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path)
        _git(tmp_path, "branch", "agent/t099-bye")
        b = _make_backend(tmp_path)
        try:
            engine = ReconciliationEngine(b, state_dir=tmp_path, clock=_make_clock())
            report = engine.scan()
            assert any(d.kind == DiscrepancyKind.orphan_branch
                       for d in report.discrepancies)
            actions = engine.fix(report, dry_run=False)
            applied = [a for a in actions
                       if a.kind == DiscrepancyKind.orphan_branch]
            assert applied and all(a.result == "applied" for a in applied)
            # Second scan returns no orphan_branch.
            report2 = engine.scan()
            assert [d for d in report2.discrepancies
                    if d.kind == DiscrepancyKind.orphan_branch] == []
        finally:
            b.close()


# ---------------------------------------------------------------------------
# Check 2 — orphan_packet
# ---------------------------------------------------------------------------


class TestOrphanPacket:
    """``.fakoli-state/packets/*.md`` for task ids not in SQLite."""

    def _packet_dir(self, root: Path) -> Path:
        d = root / ".fakoli-state" / "packets"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def test_orphan_packet_detected_when_present(self, tmp_path: Path) -> None:
        pdir = self._packet_dir(tmp_path)
        (pdir / "T099.md").write_text("# Orphan packet\n")
        b = _make_backend(tmp_path)
        try:
            engine = ReconciliationEngine(b, state_dir=tmp_path, clock=_make_clock())
            report = engine.scan()
            orphans = [d for d in report.discrepancies
                       if d.kind == DiscrepancyKind.orphan_packet]
            assert len(orphans) == 1
            assert orphans[0].severity == Severity.info
            assert "T099" in orphans[0].payload["task_id"]
            assert orphans[0].suggested_fix.startswith("rm ")
        finally:
            b.close()

    def test_orphan_packet_not_detected_when_task_exists(
        self, tmp_path: Path,
    ) -> None:
        pdir = self._packet_dir(tmp_path)
        (pdir / "T001.md").write_text("# Hello\n")
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            _setup_task(b, task_id="T001")
            engine = ReconciliationEngine(b, state_dir=tmp_path, clock=_make_clock())
            report = engine.scan()
            assert [d for d in report.discrepancies
                    if d.kind == DiscrepancyKind.orphan_packet] == []
        finally:
            b.close()

    def test_non_md_files_ignored(self, tmp_path: Path) -> None:
        """README.txt and .DS_Store in packets/ are never orphans."""
        pdir = self._packet_dir(tmp_path)
        (pdir / "README.txt").write_text("notes\n")
        (pdir / ".DS_Store").write_bytes(b"\x00")
        b = _make_backend(tmp_path)
        try:
            engine = ReconciliationEngine(b, state_dir=tmp_path, clock=_make_clock())
            report = engine.scan()
            assert [d for d in report.discrepancies
                    if d.kind == DiscrepancyKind.orphan_packet] == []
        finally:
            b.close()

    def test_orphan_packet_fix_remediates(self, tmp_path: Path) -> None:
        pdir = self._packet_dir(tmp_path)
        path = pdir / "T099.md"
        path.write_text("# bye\n")
        b = _make_backend(tmp_path)
        try:
            engine = ReconciliationEngine(b, state_dir=tmp_path, clock=_make_clock())
            report = engine.scan()
            actions = engine.fix(report, dry_run=False)
            assert any(a.result == "applied" for a in actions)
            assert not path.exists()
            assert [d for d in engine.scan().discrepancies
                    if d.kind == DiscrepancyKind.orphan_packet] == []
        finally:
            b.close()

    def test_orphan_packet_detected_when_state_dir_is_dot_fakoli(
        self, tmp_path: Path,
    ) -> None:
        """MUST-FIX 2: when ``state_dir`` IS the ``.fakoli-state/`` dir (the
        CLI's convention), packets at ``.fakoli-state/packets/*.md`` must still
        resolve. The old hardcoded ``state_dir / ".fakoli-state" / "packets"``
        looked at ``.fakoli-state/.fakoli-state/packets`` and never fired."""
        state_dir = tmp_path / ".fakoli-state"
        pdir = state_dir / "packets"
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "T099.md").write_text("# Orphan packet\n")
        b = _make_backend(state_dir)
        try:
            engine = ReconciliationEngine(
                b, state_dir=state_dir, clock=_make_clock(),
            )
            report = engine.scan()
            orphans = [d for d in report.discrepancies
                       if d.kind == DiscrepancyKind.orphan_packet]
            assert len(orphans) == 1
            assert orphans[0].payload["task_id"] == "T099"
        finally:
            b.close()


# ---------------------------------------------------------------------------
# Check 3 — orphan_worktree
# ---------------------------------------------------------------------------


class TestOrphanWorktree:
    """Worktrees pointing at ``agent/t*-*`` branches whose task/claim is gone."""

    def _add_worktree(self, repo: Path, branch: str, name: str) -> Path:
        """Create ``branch`` and add a worktree at ``repo.parent/name``."""
        _git(repo, "branch", branch)
        wt_path = repo.parent / name
        _git(repo, "worktree", "add", str(wt_path), branch)
        return wt_path

    def test_orphan_worktree_detected_when_present(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_git_repo(repo)
        self._add_worktree(repo, "agent/t099-bye", "wt-t099")
        b = _make_backend(repo)
        try:
            engine = ReconciliationEngine(b, state_dir=repo, clock=_make_clock())
            report = engine.scan()
            orphans = [d for d in report.discrepancies
                       if d.kind == DiscrepancyKind.orphan_worktree]
            assert orphans, "expected orphan_worktree to be detected"
            d = orphans[0]
            assert d.severity == Severity.warning
            assert d.suggested_fix.startswith("git worktree remove --force ")
            assert d.payload["task_id"] == "T099"
        finally:
            b.close()

    def test_worktree_with_active_claim_not_flagged(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_git_repo(repo)
        self._add_worktree(repo, "agent/t001-real", "wt-t001")
        b = _make_backend(repo)
        try:
            _setup_project(b)
            _setup_task(b, task_id="T001")
            _create_active_claim(b, claim_id="C001", task_id="T001")
            engine = ReconciliationEngine(b, state_dir=repo, clock=_make_clock())
            report = engine.scan()
            assert [d for d in report.discrepancies
                    if d.kind == DiscrepancyKind.orphan_worktree] == []
        finally:
            b.close()

    def test_main_worktree_never_flagged(self, tmp_path: Path) -> None:
        """The main worktree on ``main`` is not an agent branch — never flagged."""
        _init_git_repo(tmp_path)
        b = _make_backend(tmp_path)
        try:
            engine = ReconciliationEngine(b, state_dir=tmp_path, clock=_make_clock())
            report = engine.scan()
            assert [d for d in report.discrepancies
                    if d.kind == DiscrepancyKind.orphan_worktree] == []
        finally:
            b.close()

    def test_orphan_worktree_fix_remediates(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_git_repo(repo)
        wt = self._add_worktree(repo, "agent/t099-bye", "wt-t099")
        b = _make_backend(repo)
        try:
            engine = ReconciliationEngine(b, state_dir=repo, clock=_make_clock())
            report = engine.scan()
            assert any(d.kind == DiscrepancyKind.orphan_worktree
                       for d in report.discrepancies)
            actions = engine.fix(report, dry_run=False)
            applied = [a for a in actions
                       if a.kind == DiscrepancyKind.orphan_worktree]
            assert applied and all(a.result == "applied" for a in applied), \
                f"got: {[(a.result, a.error) for a in actions]}"
            assert not wt.exists()
        finally:
            b.close()


# ---------------------------------------------------------------------------
# Check 4 — stale_claim
# ---------------------------------------------------------------------------


class TestStaleClaim:
    """Active claims whose ``lease_expires_at`` is in the past."""

    def test_stale_claim_detected_when_present(self, tmp_path: Path) -> None:
        clock = _make_clock()
        b = _make_backend(tmp_path, clock)
        try:
            _setup_project(b)
            _setup_task(b, task_id="T001")
            # Lease in the past relative to the current clock.
            _create_active_claim(
                b, claim_id="C001", task_id="T001",
                lease_expires_at=_T0 - timedelta(hours=2),
            )
            engine = ReconciliationEngine(b, state_dir=tmp_path, clock=clock)
            report = engine.scan()
            stales = [d for d in report.discrepancies
                      if d.kind == DiscrepancyKind.stale_claim]
            assert len(stales) == 1
            d = stales[0]
            assert d.severity == Severity.error
            assert d.target_id == "C001"
            assert d.target_kind == "claim"
            assert "--force" in d.suggested_fix
            assert "T001" in d.suggested_fix
        finally:
            b.close()

    def test_stale_claim_not_detected_when_lease_in_future(
        self, tmp_path: Path,
    ) -> None:
        clock = _make_clock()
        b = _make_backend(tmp_path, clock)
        try:
            _setup_project(b)
            _setup_task(b, task_id="T001")
            _create_active_claim(
                b, claim_id="C001", task_id="T001",
                lease_expires_at=_T0 + timedelta(hours=1),
            )
            engine = ReconciliationEngine(b, state_dir=tmp_path, clock=clock)
            report = engine.scan()
            assert [d for d in report.discrepancies
                    if d.kind == DiscrepancyKind.stale_claim] == []
        finally:
            b.close()

    def test_released_claim_not_flagged_even_if_lease_past(
        self, tmp_path: Path,
    ) -> None:
        """list_active_claims() excludes released claims — no false flag."""
        clock = _make_clock()
        b = _make_backend(tmp_path, clock)
        try:
            _setup_project(b)
            _setup_task(b, task_id="T001")
            _create_active_claim(
                b, claim_id="C001", task_id="T001",
                lease_expires_at=_T0 - timedelta(hours=1),
            )
            # Release the claim — now active_claims is empty.
            b.append(_make_event(
                "claim.released",
                {
                    "claim_id": "C001",
                    "released_by": "test",
                    "release_reason": "manual",
                    "force": False,
                },
                event_id="E000006", target_kind="claim", target_id="C001",
            ))
            engine = ReconciliationEngine(b, state_dir=tmp_path, clock=clock)
            report = engine.scan()
            assert [d for d in report.discrepancies
                    if d.kind == DiscrepancyKind.stale_claim] == []
        finally:
            b.close()

    def test_stale_claim_fix_remediates(self, tmp_path: Path) -> None:
        clock = _make_clock()
        b = _make_backend(tmp_path, clock)
        try:
            _setup_project(b)
            _setup_task(b, task_id="T001")
            _create_active_claim(
                b, claim_id="C001", task_id="T001",
                lease_expires_at=_T0 - timedelta(hours=2),
            )
            engine = ReconciliationEngine(b, state_dir=tmp_path, clock=clock)
            report = engine.scan()
            actions = engine.fix(report, dry_run=False)
            applied = [a for a in actions
                       if a.kind == DiscrepancyKind.stale_claim]
            assert applied and all(a.result == "applied" for a in applied), \
                f"got: {[(a.result, a.error) for a in actions]}"
            # Active claims list is now empty.
            assert b.list_active_claims() == []
            # Re-scan: no stale_claim.
            report2 = engine.scan()
            assert [d for d in report2.discrepancies
                    if d.kind == DiscrepancyKind.stale_claim] == []
        finally:
            b.close()


# ---------------------------------------------------------------------------
# Check 5 — missing_expected_file (intent vs filesystem)
# ---------------------------------------------------------------------------


class TestMissingExpectedFile:
    """Terminal tasks whose plan-declared ``likely_files`` are absent on disk."""

    def _setup_terminal_task_with_files(
        self,
        b: SqliteBackend,
        *,
        task_id: str = "T001",
        likely_files: list[str],
        terminal: str = "done",
    ) -> None:
        """Seed a task with ``likely_files`` and walk it to a terminal status."""
        _setup_project(b)
        # task.created carries likely_files; reuse the payload helper + patch.
        payload = _make_task_payload(task_id=task_id, status="proposed")
        payload["likely_files"] = likely_files
        b.append(_make_event(
            "feature.created",
            {
                "id": "F001", "title": "F", "description": "",
                "status": "proposed", "requirements": [], "tasks": [],
            },
            target_kind="feature", target_id="F001",
        ))
        b.append(_make_event(
            "task.created", payload,
            target_kind="task", target_id=task_id,
        ))
        chain = [
            ("proposed", "drafted"), ("drafted", "reviewed"),
            ("reviewed", "ready"), ("ready", "claimed"),
            ("claimed", "in_progress"), ("in_progress", "needs_review"),
            ("needs_review", "accepted"), ("accepted", "done"),
        ]
        for frm, to in chain:
            b.append(_make_event(
                "task.status_changed",
                {"task_id": task_id, "from": frm, "to": to},
                target_kind="task", target_id=task_id,
            ))
            if to == terminal:
                break

    def test_missing_file_detected_for_done_task(self, tmp_path: Path) -> None:
        b = _make_backend(tmp_path)
        try:
            self._setup_terminal_task_with_files(
                b, likely_files=["src/widget.py"],
            )
            engine = ReconciliationEngine(b, state_dir=tmp_path, clock=_make_clock())
            report = engine.scan()
            missing = [d for d in report.discrepancies
                       if d.kind == DiscrepancyKind.missing_expected_file]
            assert len(missing) == 1
            d = missing[0]
            assert d.severity == Severity.warning
            assert d.target_kind == "task"
            assert d.payload["task_id"] == "T001"
            assert d.payload["expected_file"] == "src/widget.py"
        finally:
            b.close()

    def test_present_file_not_flagged(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "widget.py").write_text("ok\n")
        b = _make_backend(tmp_path)
        try:
            self._setup_terminal_task_with_files(
                b, likely_files=["src/widget.py"],
            )
            engine = ReconciliationEngine(b, state_dir=tmp_path, clock=_make_clock())
            report = engine.scan()
            assert [d for d in report.discrepancies
                    if d.kind == DiscrepancyKind.missing_expected_file] == []
        finally:
            b.close()

    def test_non_terminal_task_missing_file_not_flagged(
        self, tmp_path: Path,
    ) -> None:
        """A 'ready' task whose files don't exist yet is normal mid-flight."""
        b = _make_backend(tmp_path)
        try:
            self._setup_terminal_task_with_files(
                b, likely_files=["src/widget.py"], terminal="ready",
            )
            engine = ReconciliationEngine(b, state_dir=tmp_path, clock=_make_clock())
            report = engine.scan()
            assert [d for d in report.discrepancies
                    if d.kind == DiscrepancyKind.missing_expected_file] == []
        finally:
            b.close()

    def test_state_dir_named_fakoli_state_resolves_to_project_root(
        self, tmp_path: Path,
    ) -> None:
        """When state_dir IS the .fakoli-state/ dir (the CLI's view),
        likely_files resolve against its PARENT (the project root)."""
        project_root = tmp_path
        state_dir = project_root / ".fakoli-state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (project_root / "src").mkdir()
        (project_root / "src" / "widget.py").write_text("ok\n")
        b = _make_backend(state_dir)
        try:
            self._setup_terminal_task_with_files(
                b, likely_files=["src/widget.py"],
            )
            # Pass the .fakoli-state/ dir, mirroring the CLI's _resolve_state_dir.
            engine = ReconciliationEngine(
                b, state_dir=state_dir, clock=_make_clock(),
            )
            report = engine.scan()
            # File exists at project_root/src/widget.py -> no drift.
            assert [d for d in report.discrepancies
                    if d.kind == DiscrepancyKind.missing_expected_file] == []
        finally:
            b.close()

    def test_missing_expected_file_fix_raises_not_implemented(
        self, tmp_path: Path,
    ) -> None:
        """No safe auto-fix exists; fix() reports result='failed' for it."""
        b = _make_backend(tmp_path)
        try:
            self._setup_terminal_task_with_files(
                b, likely_files=["src/widget.py"],
            )
            engine = ReconciliationEngine(b, state_dir=tmp_path, clock=_make_clock())
            report = engine.scan()
            actions = engine.fix(report, dry_run=False)
            mfa = [a for a in actions
                   if a.kind == DiscrepancyKind.missing_expected_file]
            assert mfa and mfa[0].result == "failed"
            assert "cannot auto-fix" in (mfa[0].error or "")
        finally:
            b.close()

    # -- MUST-FIX 1: dot-rooted paths must not be corrupted by lstrip("./") --

    @pytest.mark.parametrize(
        "rel",
        [
            ".env",
            ".github/workflows/ci.yml",
            "src/x.py",
            "./.env",  # leading ./ prefix on a dotfile must still resolve to .env
            "./src/x.py",
        ],
    )
    def test_dot_rooted_and_normal_paths_detected_when_absent(
        self, tmp_path: Path, rel: str,
    ) -> None:
        """An absent .env / .github/... / src/x.py each surfaces as missing.

        Regression for ``str.lstrip("./")`` stripping the {'.','/'} CHARACTER
        SET (``.env`` -> ``env``), which silently checked the wrong path and
        failed to detect missing dotfiles.
        """
        b = _make_backend(tmp_path)
        try:
            self._setup_terminal_task_with_files(b, likely_files=[rel])
            engine = ReconciliationEngine(b, state_dir=tmp_path, clock=_make_clock())
            report = engine.scan()
            missing = [d for d in report.discrepancies
                       if d.kind == DiscrepancyKind.missing_expected_file]
            assert len(missing) == 1, report.discrepancies
            d = missing[0]
            # payload echoes the original (uncorrupted) declaration ...
            assert d.payload["expected_file"] == rel
            # ... and the resolved path keeps the dotfile's leading dot and is
            # NOT pointed at a sibling like ``env`` / ``github``.
            resolved = Path(d.payload["resolved_path"])
            expected_leaf = rel.removeprefix("./")
            assert str(resolved).endswith(expected_leaf), resolved
            assert "/env" not in str(resolved)
            assert "/github/" not in str(resolved)
        finally:
            b.close()

    @pytest.mark.parametrize(
        "rel",
        [".env", ".github/workflows/ci.yml", "src/x.py"],
    )
    def test_dot_rooted_and_normal_paths_not_flagged_when_present(
        self, tmp_path: Path, rel: str,
    ) -> None:
        """The SAME paths, present on disk at their TRUE location, are clean."""
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("ok\n")
        b = _make_backend(tmp_path)
        try:
            self._setup_terminal_task_with_files(b, likely_files=[rel])
            engine = ReconciliationEngine(b, state_dir=tmp_path, clock=_make_clock())
            report = engine.scan()
            assert [d for d in report.discrepancies
                    if d.kind == DiscrepancyKind.missing_expected_file] == []
        finally:
            b.close()

    def test_parent_escape_is_clamped_into_project_root(
        self, tmp_path: Path,
    ) -> None:
        """A ``../escape.py`` likely_file must not probe outside the root.

        Defence in depth: even though the planner should never emit ``..``,
        the resolved path must stay in-tree (clamped under the project root),
        never silently checking an arbitrary filesystem location.
        """
        b = _make_backend(tmp_path)
        try:
            self._setup_terminal_task_with_files(b, likely_files=["../escape.py"])
            engine = ReconciliationEngine(b, state_dir=tmp_path, clock=_make_clock())
            report = engine.scan()
            missing = [d for d in report.discrepancies
                       if d.kind == DiscrepancyKind.missing_expected_file]
            assert len(missing) == 1
            resolved = Path(missing[0].payload["resolved_path"]).resolve()
            root = tmp_path.resolve()
            # Resolved path stays within the project root (no parent escape).
            assert root == resolved or root in resolved.parents, resolved
        finally:
            b.close()


# ---------------------------------------------------------------------------
# Check 6 — missing_sync_mapping
# ---------------------------------------------------------------------------


class TestMissingSyncMapping:
    """Done tasks without a SyncMapping when at least one provider configured."""

    def _make_done_task(self, b: SqliteBackend, task_id: str = "T001") -> None:
        """Walk the full status chain proposed → ... → done for ``task_id``.

        The only realistic shortcut: emit task.status_changed events. We
        bypass the claim flow because that requires a 'ready' precursor;
        what reconciliation cares about is just the final status.
        """
        _setup_project(b)
        _setup_task(b, task_id=task_id, status="proposed")
        # Walk through the legal transitions to land on 'done'.
        chain = [
            ("proposed", "drafted"),
            ("drafted", "reviewed"),
            ("reviewed", "ready"),
            ("ready", "claimed"),
            ("claimed", "in_progress"),
            ("in_progress", "needs_review"),
            ("needs_review", "accepted"),
            ("accepted", "done"),
        ]
        next_eid = 5
        for frm, to in chain:
            b.append(_make_event(
                "task.status_changed",
                {"task_id": task_id, "from": frm, "to": to},
                event_id=f"E{next_eid:06d}",
                target_kind="task", target_id=task_id,
            ))
            next_eid += 1

    def test_missing_sync_mapping_detected_when_done_task_no_mapping(
        self, tmp_path: Path,
    ) -> None:
        b = _make_backend(tmp_path)
        try:
            self._make_done_task(b, task_id="T001")
            engine = ReconciliationEngine(
                b, state_dir=tmp_path, clock=_make_clock(),
                configured_providers=["github_issues"],
            )
            report = engine.scan()
            missing = [d for d in report.discrepancies
                       if d.kind == DiscrepancyKind.missing_sync_mapping]
            assert len(missing) == 1
            assert missing[0].target_id == "T001"
            assert missing[0].severity == Severity.warning
            assert "github_issues" in missing[0].suggested_fix
        finally:
            b.close()

    def test_missing_sync_mapping_not_detected_when_no_provider_configured(
        self, tmp_path: Path,
    ) -> None:
        b = _make_backend(tmp_path)
        try:
            self._make_done_task(b, task_id="T001")
            engine = ReconciliationEngine(
                b, state_dir=tmp_path, clock=_make_clock(),
                configured_providers=[],  # explicitly empty
            )
            report = engine.scan()
            assert [d for d in report.discrepancies
                    if d.kind == DiscrepancyKind.missing_sync_mapping] == []
        finally:
            b.close()

    def test_missing_sync_mapping_not_detected_for_non_done_task(
        self, tmp_path: Path,
    ) -> None:
        b = _make_backend(tmp_path)
        try:
            _setup_project(b)
            _setup_task(b, task_id="T001", status="ready")
            engine = ReconciliationEngine(
                b, state_dir=tmp_path, clock=_make_clock(),
                configured_providers=["github_issues"],
            )
            report = engine.scan()
            assert [d for d in report.discrepancies
                    if d.kind == DiscrepancyKind.missing_sync_mapping] == []
        finally:
            b.close()

    def test_missing_sync_mapping_not_detected_when_mapping_exists(
        self, tmp_path: Path,
    ) -> None:
        b = _make_backend(tmp_path)
        try:
            self._make_done_task(b, task_id="T001")
            mapping = SyncMapping(
                task_id="T001",
                external_system="github_issues",
                external_id="gh-1",
                last_synced_at=_T0,
            )
            b.apply_sync_mapping(mapping)
            engine = ReconciliationEngine(
                b, state_dir=tmp_path, clock=_make_clock(),
                configured_providers=["github_issues"],
            )
            report = engine.scan()
            assert [d for d in report.discrepancies
                    if d.kind == DiscrepancyKind.missing_sync_mapping] == []
        finally:
            b.close()


# ---------------------------------------------------------------------------
# Check 6 — drift_sync_state
# ---------------------------------------------------------------------------


class TestDriftSyncState:
    """SyncMappings in conflict OR stale beyond the drift threshold."""

    def test_drift_detected_when_state_conflict(self, tmp_path: Path) -> None:
        clock = _make_clock()
        b = _make_backend(tmp_path, clock)
        try:
            _setup_project(b)
            _setup_task(b, task_id="T001")
            mapping = SyncMapping(
                task_id="T001",
                external_system="github_issues",
                external_id="gh-1",
                last_synced_at=_T0,
                sync_state="conflict",
            )
            b.apply_sync_mapping(mapping)
            engine = ReconciliationEngine(b, state_dir=tmp_path, clock=clock)
            report = engine.scan()
            drifts = [d for d in report.discrepancies
                      if d.kind == DiscrepancyKind.drift_sync_state]
            assert len(drifts) == 1
            assert drifts[0].payload["reason"] == "conflict"
            assert drifts[0].target_id == "T001"
            assert drifts[0].severity == Severity.warning
        finally:
            b.close()

    def test_drift_detected_when_stale_beyond_threshold(
        self, tmp_path: Path,
    ) -> None:
        # Mapping says last_synced_at = T0; clock says T0 + 8 days.
        synced_at = _T0
        now = _T0 + timedelta(days=8)
        clock = _make_clock(now)
        b = _make_backend(tmp_path, clock)
        try:
            _setup_project(b)
            _setup_task(b, task_id="T001")
            mapping = SyncMapping(
                task_id="T001",
                external_system="github_issues",
                external_id="gh-1",
                last_synced_at=synced_at,
            )
            b.apply_sync_mapping(mapping)
            engine = ReconciliationEngine(
                b, state_dir=tmp_path, clock=clock,
                drift_threshold_days=7,
            )
            report = engine.scan()
            drifts = [d for d in report.discrepancies
                      if d.kind == DiscrepancyKind.drift_sync_state]
            assert len(drifts) == 1
            assert drifts[0].payload["reason"] == "stale"
        finally:
            b.close()

    def test_drift_not_detected_when_in_sync_and_recent(
        self, tmp_path: Path,
    ) -> None:
        clock = _make_clock()
        b = _make_backend(tmp_path, clock)
        try:
            _setup_project(b)
            _setup_task(b, task_id="T001")
            mapping = SyncMapping(
                task_id="T001",
                external_system="github_issues",
                external_id="gh-1",
                last_synced_at=_T0,
            )
            b.apply_sync_mapping(mapping)
            engine = ReconciliationEngine(b, state_dir=tmp_path, clock=clock)
            report = engine.scan()
            assert [d for d in report.discrepancies
                    if d.kind == DiscrepancyKind.drift_sync_state] == []
        finally:
            b.close()

    def test_drift_fix_raises_not_implemented(self, tmp_path: Path) -> None:
        """drift_sync_state cannot be auto-fixed by reconciliation —
        the fix returns result='failed' for it with NotImplementedError."""
        clock = _make_clock()
        b = _make_backend(tmp_path, clock)
        try:
            _setup_project(b)
            _setup_task(b, task_id="T001")
            mapping = SyncMapping(
                task_id="T001",
                external_system="github_issues",
                external_id="gh-1",
                last_synced_at=_T0,
                sync_state="conflict",
            )
            b.apply_sync_mapping(mapping)
            engine = ReconciliationEngine(b, state_dir=tmp_path, clock=clock)
            report = engine.scan()
            actions = engine.fix(report, dry_run=False)
            drift_actions = [a for a in actions
                             if a.kind == DiscrepancyKind.drift_sync_state]
            assert drift_actions and drift_actions[0].result == "failed"
            # Operator-facing message: tells the user what to run, not which
            # internal wave/phase owns the feature.
            err = drift_actions[0].error or ""
            assert "cannot auto-fix" in err
            assert "drift_sync_state" in err
            assert "suggested command" in err
        finally:
            b.close()


# ---------------------------------------------------------------------------
# ReconciliationReport — Pydantic round-trip + summary invariant
# ---------------------------------------------------------------------------


class TestReconciliationReport:
    """Pydantic round-trip + summary counts match discrepancy list."""

    def test_report_roundtrip_json(self) -> None:
        d = Discrepancy(
            kind=DiscrepancyKind.orphan_branch,
            severity=Severity.warning,
            target_id="agent/t001-foo",
            target_kind="branch",
            description="bye",
            suggested_fix="git branch -D agent/t001-foo",
            payload={"task_id": "T001"},
        )
        report = ReconciliationReport(
            scanned_at=_T0,
            discrepancies=[d],
            summary={"orphan_branch": 1},
        )
        as_json = report.model_dump_json()
        rebuilt = ReconciliationReport.model_validate_json(as_json)
        assert rebuilt.scanned_at == _T0
        assert rebuilt.discrepancies[0].target_id == "agent/t001-foo"
        assert rebuilt.summary == {"orphan_branch": 1}

    def test_summary_counts_match_discrepancy_histogram(
        self, tmp_path: Path,
    ) -> None:
        _init_git_repo(tmp_path)
        _git(tmp_path, "branch", "agent/t101-a")
        _git(tmp_path, "branch", "agent/t102-b")
        pdir = tmp_path / ".fakoli-state" / "packets"
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "T201.md").write_text("x")
        b = _make_backend(tmp_path)
        try:
            engine = ReconciliationEngine(b, state_dir=tmp_path, clock=_make_clock())
            report = engine.scan()
            # Both branches + the packet should surface.
            assert report.summary.get("orphan_branch") == 2
            assert report.summary.get("orphan_packet") == 1
            # The invariant validator should pass.
            report.validate_summary()
        finally:
            b.close()

    def test_summary_validate_raises_on_mismatch(self) -> None:
        d = Discrepancy(
            kind=DiscrepancyKind.orphan_branch,
            severity=Severity.warning,
            target_id="agent/t001-foo",
            target_kind="branch",
            description="bye",
            suggested_fix="git branch -D agent/t001-foo",
        )
        report = ReconciliationReport(
            scanned_at=_T0,
            discrepancies=[d],
            summary={"orphan_branch": 99},  # wrong on purpose
        )
        with pytest.raises(ValueError, match="does not match"):
            report.validate_summary()

    def test_discrepancy_extra_forbid(self) -> None:
        """Discrepancy rejects unknown fields (extra='forbid')."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            Discrepancy(
                kind=DiscrepancyKind.orphan_branch,
                severity=Severity.warning,
                target_id="x",
                target_kind="branch",
                description="bye",
                suggested_fix="rm -rf /",
                surprise="boom",  # type: ignore[call-arg]
            )

    def test_fix_action_extra_forbid(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            FixAction(
                kind=DiscrepancyKind.orphan_branch,
                target_id="x",
                command="git branch -D x",
                result="applied",
                surprise="boom",  # type: ignore[call-arg]
            )

    def test_discrepancies_sorted_deterministically(
        self, tmp_path: Path,
    ) -> None:
        _init_git_repo(tmp_path)
        # Create branches out of alphabetical order.
        _git(tmp_path, "branch", "agent/t202-z")
        _git(tmp_path, "branch", "agent/t101-a")
        b = _make_backend(tmp_path)
        try:
            engine = ReconciliationEngine(b, state_dir=tmp_path, clock=_make_clock())
            report = engine.scan()
            branches = [d.target_id for d in report.discrepancies
                        if d.kind == DiscrepancyKind.orphan_branch]
            assert branches == sorted(branches)
        finally:
            b.close()


# ---------------------------------------------------------------------------
# Dry-run semantics
# ---------------------------------------------------------------------------


class TestDryRun:
    """``fix(dry_run=True)`` returns the action list without mutating."""

    def test_dry_run_skips_all_actions(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path)
        _git(tmp_path, "branch", "agent/t099-bye")
        pdir = tmp_path / ".fakoli-state" / "packets"
        pdir.mkdir(parents=True, exist_ok=True)
        packet = pdir / "T201.md"
        packet.write_text("x")
        b = _make_backend(tmp_path)
        try:
            engine = ReconciliationEngine(b, state_dir=tmp_path, clock=_make_clock())
            report = engine.scan()
            actions = engine.fix(report, dry_run=True)

            # Every action is skipped, none applied.
            assert actions, "dry-run still returns one action per discrepancy"
            assert all(a.result == "skipped" for a in actions)
            assert all(a.error is None for a in actions)

            # Nothing happened on disk: branch and packet still there.
            assert packet.exists()
            r = subprocess.run(
                ["git", "rev-parse", "--verify", "agent/t099-bye"],
                cwd=str(tmp_path),
                capture_output=True,
                timeout=5,
            )
            assert r.returncode == 0
        finally:
            b.close()

    def test_dry_run_returns_same_action_kinds_as_real_run(
        self, tmp_path: Path,
    ) -> None:
        _init_git_repo(tmp_path)
        _git(tmp_path, "branch", "agent/t099-bye")
        b = _make_backend(tmp_path)
        try:
            engine = ReconciliationEngine(b, state_dir=tmp_path, clock=_make_clock())
            report = engine.scan()
            dry = engine.fix(report, dry_run=True)
            # Re-run as real — the kinds + targets must match the dry run.
            real = engine.fix(report, dry_run=False)
            assert [(a.kind, a.target_id) for a in dry] == \
                   [(a.kind, a.target_id) for a in real]
        finally:
            b.close()


# ---------------------------------------------------------------------------
# Best-effort fix loop — one bad action does not break the others
# ---------------------------------------------------------------------------


class TestBestEffortFixLoop:
    """A single fix failure must not skip subsequent discrepancies."""

    def test_one_failed_fix_does_not_abort_others(
        self, tmp_path: Path,
    ) -> None:
        # Set up two orphan branches; manually delete one before fix()
        # runs so the first git-branch-D fails (branch missing), but the
        # second still succeeds.
        _init_git_repo(tmp_path)
        _git(tmp_path, "branch", "agent/t101-a")
        _git(tmp_path, "branch", "agent/t102-b")
        b = _make_backend(tmp_path)
        try:
            engine = ReconciliationEngine(b, state_dir=tmp_path, clock=_make_clock())
            report = engine.scan()
            # Delete agent/t101-a out-of-band so the engine's fix fails.
            _git(tmp_path, "branch", "-D", "agent/t101-a")
            actions = engine.fix(report, dry_run=False)
            results = {a.target_id: a.result for a in actions
                       if a.kind == DiscrepancyKind.orphan_branch}
            # The deleted branch fails, the other succeeds.
            assert results.get("agent/t101-a") == "failed"
            assert results.get("agent/t102-b") == "applied"
        finally:
            b.close()


# ---------------------------------------------------------------------------
# DiscrepancyKind / Severity StrEnum coverage
# ---------------------------------------------------------------------------


class TestEnumValues:
    """The public StrEnums are exactly the six kinds and three severities."""

    def test_discrepancy_kind_values(self) -> None:
        assert {k.value for k in DiscrepancyKind} == {
            "orphan_branch",
            "orphan_packet",
            "orphan_worktree",
            "stale_claim",
            "missing_expected_file",
            "missing_sync_mapping",
            "drift_sync_state",
        }

    def test_severity_values(self) -> None:
        assert {s.value for s in Severity} == {"info", "warning", "error"}


class TestLocalDriftKindsContract:
    """Pin the `drift` contract: which DiscrepancyKinds are LOCAL drift.

    LOCAL_DRIFT_KINDS is the exact set the read-only `fakoli-state drift` view
    reports — local intent/state/fs/git drift that needs no external sync
    provider. The two provider-dependent kinds (``missing_sync_mapping``,
    ``drift_sync_state``) are deliberately EXCLUDED. Changing this set changes
    the user-facing drift contract, so it is pinned here.
    """

    def test_local_drift_kinds_membership(self) -> None:
        assert LOCAL_DRIFT_KINDS == frozenset({
            DiscrepancyKind.orphan_branch,
            DiscrepancyKind.orphan_packet,
            DiscrepancyKind.orphan_worktree,
            DiscrepancyKind.stale_claim,
            DiscrepancyKind.missing_expected_file,
        })

    def test_orphan_packet_is_in_local_drift(self) -> None:
        """MUST-FIX 2: orphan_packet is advertised AND reachable — keep it in."""
        assert DiscrepancyKind.orphan_packet in LOCAL_DRIFT_KINDS

    def test_provider_kinds_excluded_from_local_drift(self) -> None:
        assert DiscrepancyKind.missing_sync_mapping not in LOCAL_DRIFT_KINDS
        assert DiscrepancyKind.drift_sync_state not in LOCAL_DRIFT_KINDS


# ---------------------------------------------------------------------------
# Wave 3 P2-5 — suggested_fix strings must parse against the real CLI
# ---------------------------------------------------------------------------


class TestSuggestedFixMatchesCliSyntax:
    """The CLI hints the reconciliation engine emits must actually be
    parseable by ``fakoli-state sync``. Old strings used positional task
    ids (``--push T001``) which Typer rejects with "Got unexpected extra
    argument" — making the hint useless."""

    def test_missing_sync_mapping_fix_string_is_parseable(
        self, tmp_path: Path,
    ) -> None:
        """The hint for missing_sync_mapping must use `--task <id>`, not
        positional id, and `sync provider <id>` not `sync <id>`."""
        from typer.testing import CliRunner

        from fakoli_state.cli import app

        b = _make_backend(tmp_path)
        try:
            # Walk T001 to done.
            _setup_project(b)
            _setup_task(b, task_id="T001", status="proposed")
            chain = [
                ("proposed", "drafted"), ("drafted", "reviewed"),
                ("reviewed", "ready"), ("ready", "claimed"),
                ("claimed", "in_progress"), ("in_progress", "needs_review"),
                ("needs_review", "accepted"), ("accepted", "done"),
            ]
            next_eid = 5
            for frm, to in chain:
                b.append(_make_event(
                    "task.status_changed",
                    {"task_id": "T001", "from": frm, "to": to},
                    event_id=f"E{next_eid:06d}",
                    target_kind="task", target_id="T001",
                ))
                next_eid += 1

            engine = ReconciliationEngine(
                b, state_dir=tmp_path, clock=_make_clock(),
                configured_providers=["github_issues"],
            )
            report = engine.scan()
            missing = [d for d in report.discrepancies
                       if d.kind == DiscrepancyKind.missing_sync_mapping]
            assert len(missing) == 1
            hint = missing[0].suggested_fix

            # The hint must contain --task (not positional id).
            assert "--task" in hint, f"hint missing --task: {hint!r}"
            assert "T001" in hint
            # Must use the `provider` subcommand for non-aliased providers.
            assert "sync provider" in hint, (
                f"hint must say `sync provider <id>`, not `sync <id>`: {hint!r}"
            )

            # Smoke test: split the hint and feed the CLI runner. We expect
            # NOT to see "Got unexpected extra argument" / "no such option";
            # the actual exit code can be non-zero for unrelated reasons
            # (uninitialised tmpdir, etc.), but the parser must succeed.
            parts = hint.split()
            assert parts[0] == "fakoli-state"
            runner = CliRunner()
            r = runner.invoke(app, parts[1:], catch_exceptions=False)
            # Parser-success contract: no "unexpected extra argument" /
            # "no such option" in stderr+stdout combined.
            combined = (r.output or "")
            assert "unexpected extra argument" not in combined.lower(), (
                f"hint failed to parse: {combined}"
            )
            assert "no such option" not in combined.lower(), (
                f"hint failed to parse: {combined}"
            )
        finally:
            b.close()

    def test_drift_sync_state_fix_string_is_parseable(
        self, tmp_path: Path,
    ) -> None:
        """Same contract for the drift_sync_state hint."""
        from typer.testing import CliRunner

        from fakoli_state.cli import app

        clock = _make_clock()
        b = _make_backend(tmp_path, clock)
        try:
            _setup_project(b)
            _setup_task(b, task_id="T001")
            mapping = SyncMapping(
                task_id="T001",
                external_system="github_issues",
                external_id="gh-1",
                last_synced_at=_T0,
                sync_state="conflict",
            )
            b.apply_sync_mapping(mapping)
            engine = ReconciliationEngine(b, state_dir=tmp_path, clock=clock)
            report = engine.scan()
            drifts = [d for d in report.discrepancies
                      if d.kind == DiscrepancyKind.drift_sync_state]
            assert len(drifts) == 1
            hint = drifts[0].suggested_fix

            assert "--task" in hint, f"hint missing --task: {hint!r}"
            assert "T001" in hint
            assert "sync provider" in hint, (
                f"hint must say `sync provider <id>`, not `sync <id>`: {hint!r}"
            )

            parts = hint.split()
            assert parts[0] == "fakoli-state"
            runner = CliRunner()
            r = runner.invoke(app, parts[1:], catch_exceptions=False)
            combined = (r.output or "")
            assert "unexpected extra argument" not in combined.lower(), (
                f"hint failed to parse: {combined}"
            )
            assert "no such option" not in combined.lower(), (
                f"hint failed to parse: {combined}"
            )
        finally:
            b.close()
