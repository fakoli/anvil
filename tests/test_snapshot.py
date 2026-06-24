"""Tests for anvil.state.snapshot.serialize_state.

serialize_state is the canonical-state snapshot consumed by the SL-1
replay-equivalence test. These tests prove two properties:

1. **Determinism** — ``json.dumps(serialize_state(b), sort_keys=True)`` is
   byte-identical across repeated calls on an unchanged backend.
2. **Totality of claim/review state** — the snapshot reflects released and
   stale claims and review rows, proving it reads ``list_claims`` /
   ``list_reviews`` (ALL rows) and NOT the active-only variants.

The backend is populated through the real event pipeline (append(EventDraft)),
so the snapshot is exercised against genuine SQLite-backed state rather than
hand-built models. SL1-RR-1: apply_event is retired; append() is the sole write.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from anvil.clock import FrozenClock
from anvil.state.models import EventDraft
from anvil.state.snapshot import serialize_state
from anvil.state.sqlite import SqliteBackend

_T0 = datetime(2026, 5, 24, 18, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Backend-construction helpers (mirrors test_sqlite / test_claims conventions)
# ---------------------------------------------------------------------------


def _make_backend(state_dir: Path) -> SqliteBackend:
    db_path = str(state_dir / "state.db")
    events_path = str(state_dir / "events.jsonl")
    Path(events_path).touch()
    b = SqliteBackend(db_path=db_path, events_path=events_path, clock=FrozenClock(_T0))
    b.initialize()
    return b


def _event(
    action: str,
    payload: dict[str, Any],
    *,
    event_id: str = "unused",
    target_kind: str = "task",
    target_id: str = "T001",
) -> EventDraft:
    """Return an EventDraft (SL1-RR-1: id is assigned by backend, event_id ignored)."""
    return EventDraft(
        timestamp=_T0,
        actor="test",
        action=action,
        target_kind=target_kind,
        target_id=target_id,
        payload_json=payload,
    )


def _claim_payload(*, claim_id: str, task_id: str) -> dict[str, Any]:
    return {
        "id": claim_id,
        "task_id": task_id,
        "claimed_by": "agent-alpha",
        "claim_type": "task",
        "status": "active",
        "branch": None,
        "worktree_path": None,
        "expected_files": [],
        "created_at": _T0.isoformat(),
        "lease_expires_at": (_T0 + timedelta(hours=1)).isoformat(),
        "last_heartbeat_at": _T0.isoformat(),
        "released_at": None,
        "release_reason": None,
    }


def _task_payload(*, task_id: str) -> dict[str, Any]:
    return {
        "id": task_id,
        "feature_id": "F001",
        "title": f"Task {task_id}",
        "description": "desc",
        "status": "proposed",
        "priority": "medium",
        "dependencies": [],
        "conflict_groups": [],
        "scores": {},
        "acceptance_criteria": [],
        "implementation_notes": [],
        "verification": {},
        "likely_files": [],
        "parent_task_id": None,
        "created_at": _T0.isoformat(),
        "updated_at": _T0.isoformat(),
    }


def _build_populated_backend(state_dir: Path) -> SqliteBackend:
    """Build a backend containing every canonical collection.

    Produces: project, PRD, a feature, two ready tasks, a released claim
    (auto-released via evidence.submitted), a stale claim (via claim.stale),
    a review row (via task.applied accepted), and an evidence row.
    """
    b = _make_backend(state_dir)

    eid = iter(f"E{n:06d}" for n in range(1, 1000))

    # Project + state init.
    b.append(_event(
        "project.created",
        {
            "id": "proj-1",
            "name": "Test Project",
            "description": "",
            "created_at": _T0.isoformat(),
            "updated_at": _T0.isoformat(),
        },
        event_id=next(eid), target_kind="project", target_id="proj-1",
    ))
    b.append(_event(
        "state.initialized", {},
        event_id=next(eid), target_kind="project", target_id="proj-1",
    ))

    # PRD (parsed + reviewed).
    prd_payload = {
        "project_id": "proj-1",
        "status": "draft",
        "summary": "A test PRD.",
        "goals": ["Goal one."],
        "non_goals": [],
        "requirements": [
            {"id": "R001", "prd_section": "requirements", "text": "Req 1.",
             "source_paragraph": None, "derived": False}
        ],
        "acceptance_criteria": ["AC one."],
        "risks": [],
        "open_questions": [],
    }
    b.append(_event(
        "prd.parsed", prd_payload,
        event_id=next(eid), target_kind="prd", target_id="proj-1",
    ))
    b.append(_event(
        "prd.reviewed", {"project_id": "proj-1", "reviewer": "alice"},
        event_id=next(eid), target_kind="prd", target_id="proj-1",
    ))

    # Feature.
    b.append(_event(
        "feature.created",
        {
            "id": "F001",
            "title": "Feature F001",
            "description": "A feature.",
            "status": "proposed",
            "requirements": [],
            "tasks": [],
        },
        event_id=next(eid), target_kind="feature", target_id="F001",
    ))

    # Two tasks, each promoted proposed → drafted → reviewed → ready.
    for task_id in ("T001", "T002"):
        b.append(_event(
            "task.created", _task_payload(task_id=task_id),
            event_id=next(eid), target_id=task_id,
        ))
        for from_s, to_s in (
            ("proposed", "drafted"),
            ("drafted", "reviewed"),
            ("reviewed", "ready"),
        ):
            b.append(_event(
                "task.status_changed",
                {"task_id": task_id, "from": from_s, "to": to_s},
                event_id=next(eid), target_id=task_id,
            ))

    # T001: claim → evidence.submitted (auto-releases claim, inserts evidence) →
    # task.applied accepted (inserts a review row, task → done).
    b.append(_event(
        "claim.created", _claim_payload(claim_id="C001", task_id="T001"),
        event_id=next(eid), target_kind="claim", target_id="C001",
    ))
    b.append(_event(
        "evidence.submitted",
        {
            "task_id": "T001",
            "claim_id": "C001",
            "evidence_id": "EV001",
            "submitted_by": "agent-alpha",
            "commands_run": ["pytest tests/ -v"],
            "files_changed": ["src/auth.py"],
            "output_excerpt": "5 passed",
            "pr_url": None,
            "commit_sha": None,
            "screenshots": [],
            "known_limitations": None,
        },
        event_id=next(eid), target_id="T001",
    ))
    applied_event_id = next(eid)
    b.append(_event(
        "task.applied",
        {"task_id": "T001", "reviewer": "alice", "decision": "accepted", "notes": None},
        event_id=applied_event_id, target_id="T001",
    ))

    # T002: claim → claim.stale (stale claim, task returns to ready).
    b.append(_event(
        "claim.created", _claim_payload(claim_id="C002", task_id="T002"),
        event_id=next(eid), target_kind="claim", target_id="C002",
    ))
    b.append(_event(
        "claim.stale",
        {
            "claim_id": "C002",
            "task_id": "T002",
            "expired_at": (_T0 - timedelta(hours=1)).isoformat(),
            "detected_at": _T0.isoformat(),
            "reason": "lease_expired",
            "actor": "system",
        },
        event_id=next(eid), target_kind="claim", target_id="C002",
    ))

    return b


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_serialize_state_is_json_serialisable_and_total(tmp_path: Path) -> None:
    """Snapshot covers every canonical collection and is JSON-serialisable."""
    b = _build_populated_backend(tmp_path)
    try:
        snap = serialize_state(b)

        # Top-level shape is exactly the documented contract.
        assert set(snap.keys()) == {
            "project", "prds", "features", "tasks",
            "claims", "reviews", "evidence", "requirements", "sync_mappings",
        }

        # Project singleton present.
        assert snap["project"] is not None
        assert snap["project"]["id"] == "proj-1"
        # T024: the default PRD is the sole 'prds' entry, carrying its identity
        # (id / revision) stamps alongside the mutable scalars.
        assert len(snap["prds"]) == 1
        assert snap["prds"][0]["id"] == "default"
        assert snap["prds"][0]["revision"] == 1
        assert snap["prds"][0]["status"] == "reviewed"

        # Collections populated.
        assert [f["id"] for f in snap["features"]] == ["F001"]
        assert {t["id"] for t in snap["tasks"]} == {"T001", "T002"}
        assert len(snap["evidence"]) == 1
        assert snap["evidence"][0]["id"] == "EV001"

        # Requirements are present from the parsed PRD.
        assert len(snap["requirements"]) >= 1

        # The whole structure is JSON-serialisable (no datetimes/enums leak).
        json.dumps(snap, sort_keys=True)
    finally:
        b.close()


def test_serialize_state_reflects_non_active_claims_and_reviews(
    tmp_path: Path,
) -> None:
    """Snapshot reflects released + stale claims and review rows.

    Proves serialize_state uses list_claims / list_reviews (ALL rows), not the
    active-only variants — a released claim and a stale claim must both appear.
    """
    b = _build_populated_backend(tmp_path)
    try:
        snap = serialize_state(b)

        claims_by_id = {c["id"]: c for c in snap["claims"]}
        # Both terminal-state claims present and in their non-active state.
        assert claims_by_id["C001"]["status"] == "released"
        assert claims_by_id["C002"]["status"] == "stale"
        # Sanity: none of them is 'active' (so active-only reads would drop them).
        assert all(c["status"] != "active" for c in snap["claims"])

        # The task.applied review row is present.
        assert len(snap["reviews"]) == 1
        review = snap["reviews"][0]
        assert review["target_id"] == "T001"
        assert review["decision"] == "approve"  # accepted → approve
    finally:
        b.close()


def test_serialize_state_is_byte_stable_across_repeated_calls(
    tmp_path: Path,
) -> None:
    """Two calls on the same unchanged backend produce byte-identical JSON."""
    b = _build_populated_backend(tmp_path)
    try:
        first = json.dumps(serialize_state(b), sort_keys=True)
        second = json.dumps(serialize_state(b), sort_keys=True)
        assert first == second
    finally:
        b.close()


def test_serialize_state_empty_backend(tmp_path: Path) -> None:
    """An uninitialised-content backend yields a well-formed, empty snapshot."""
    b = _make_backend(tmp_path)
    try:
        snap = serialize_state(b)
        assert snap["project"] is None
        # T024: no PRDs on an empty backend — 'prds' is the empty list, not None.
        assert snap["prds"] == []
        assert snap["features"] == []
        assert snap["tasks"] == []
        assert snap["claims"] == []
        assert snap["reviews"] == []
        assert snap["evidence"] == []
        assert snap["requirements"] == []
        assert snap["sync_mappings"] == []
        # Still deterministic.
        assert json.dumps(serialize_state(b), sort_keys=True) == json.dumps(
            snap, sort_keys=True
        )
    finally:
        b.close()


def test_serialize_state_requirements_reflect_parsed_prd(tmp_path: Path) -> None:
    """Snapshot requirements collection reflects the parsed PRD's requirement bodies.

    Proves that serialize_state captures text and section — not just IDs —
    so a replay divergence in requirement bodies would be detected by a
    byte-compare of two serialize_state snapshots.
    """
    b = _build_populated_backend(tmp_path)
    try:
        snap = serialize_state(b)

        # The populated backend parses a PRD with one requirement (R001).
        reqs = snap["requirements"]
        assert len(reqs) >= 1, "requirements collection must be non-empty after prd.parsed"

        # Find R001 by id (the _build_populated_backend fixture only adds R001).
        req_by_id = {r["id"]: r for r in reqs}
        assert "R001" in req_by_id, f"R001 not found in requirements snapshot: {list(req_by_id)}"
        r001 = req_by_id["R001"]

        # Verify both body fields are present — not just the id.
        assert r001["text"] == "Req 1.", (
            f"R001 text mismatch: expected 'Req 1.', got {r001['text']!r}"
        )
        assert r001["prd_section"] == "requirements", (
            f"R001 prd_section mismatch: expected 'requirements', got {r001['prd_section']!r}"
        )

        # Verify bool field round-trips correctly.
        assert r001["derived"] is False

        # The requirements list must be sorted by id.
        ids = [r["id"] for r in reqs]
        assert ids == sorted(ids), f"requirements not sorted by id: {ids}"
    finally:
        b.close()


def test_serialize_state_sorts_prd_kind_sync_mappings_with_null_task_id() -> None:
    """The sync_mappings sort tolerates a prd-kind mapping's null task_id.

    T028: a prd-kind (milestone) mapping carries ``task_id=None``. The snapshot
    sorts mappings by ``(task_id, external_system)``; comparing ``None`` against
    a ``str`` task_id raises ``TypeError`` and would abort every snapshot. This
    drives serialize_state with a stub backend whose ``list_sync_mappings``
    yields a prd-kind row alongside a task-kind one, asserting the sort does not
    raise and the null-task_id mapping orders ahead of the task-kind one.
    """
    from anvil.state.models import SyncMapping

    prd_mapping = SyncMapping(
        task_id=None,
        prd_id="default",
        entity_kind="prd",
        external_system="github_issues",
        external_id="milestone-1",
        last_synced_at=_T0,
    )
    task_mapping = SyncMapping(
        task_id="T001",
        external_system="github_issues",
        external_id="42",
        last_synced_at=_T0,
    )

    class _StubBackend:
        """Minimal read-only backend exercising the sync_mappings sort path."""

        def get_project(self) -> None:
            return None

        def get_prd(self) -> None:
            return None

        def list_prds(self) -> list[Any]:
            return []

        def list_features(self) -> list[Any]:
            return []

        def list_tasks(self) -> list[Any]:
            return []

        def list_claims(self) -> list[Any]:
            return []

        def list_reviews(self) -> list[Any]:
            return []

        def list_evidence(self) -> list[Any]:
            return []

        def list_requirements(self, **_kwargs: Any) -> list[Any]:
            return []

        def list_sync_mappings(self) -> list[SyncMapping]:
            # Return the task-kind row first to prove the sort (not input order)
            # is what places the null-task_id prd-kind row ahead of it.
            return [task_mapping, prd_mapping]

    snap = serialize_state(_StubBackend())  # type: ignore[arg-type]

    # The sort completed without TypeError, and the null-task_id (prd-kind)
    # mapping sorts ahead of the task-kind one ("" < "T001").
    assert [m["task_id"] for m in snap["sync_mappings"]] == [None, "T001"]
    # Still fully JSON-serialisable.
    json.dumps(snap, sort_keys=True)


# ===========================================================================
# T009/F006: `anvil migrate state` — promote the in-init schema
# migration to an explicit, backed-up, dry-run-by-default command.
#
# These tests live alongside serialize_state because the round-trip proof is a
# serialize_state byte-compare: a fixture db pinned at an OLDER schema version
# migrates to the current version with every row preserved (snapshot equal),
# replay still reproduces that snapshot, and re-running migrate is a no-op.
# ===========================================================================

import os  # noqa: E402
import sqlite3  # noqa: E402

from typer.testing import CliRunner  # noqa: E402

from anvil.cli import app  # noqa: E402
from anvil.state.schema import SCHEMA_VERSION  # noqa: E402
from anvil.state.sqlite import read_db_schema_version  # noqa: E402

_migrate_runner = CliRunner()


def _run_in(project_dir: Path, args: list[str]):  # type: ignore[no-untyped-def]
    """Invoke the CLI with cwd switched to *project_dir* (commands use Path.cwd)."""
    original = os.getcwd()
    os.chdir(project_dir)
    try:
        return _migrate_runner.invoke(app, args, catch_exceptions=False)
    finally:
        os.chdir(original)


def _downgrade_db_to_v3(db_path: Path) -> None:
    """Synthesize a genuine v3 (pre-git-events) state.db from a v4 db.

    The only schema delta v3→v4 in LOCAL mode is the nullable ``events.seq``
    column (the widened id CHECK only lives in the v4 DDL and is deliberately
    not retrofitted). So a faithful v3 fixture is the current db with ``seq``
    dropped and ``user_version`` stamped back to 3 — exactly the on-disk shape
    the v3→v4 forward branch expects. SQLite 3.35+ supports DROP COLUMN.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("ALTER TABLE events DROP COLUMN seq")
        conn.execute("PRAGMA user_version = 3")
        conn.commit()
    finally:
        conn.close()


def _build_v3_project(project_dir: Path) -> tuple[str, Path]:
    """Build a populated LOCAL-mode project, then pin its state.db at v3.

    Returns ``(pre_migration_snapshot_json, state_dir)``. The snapshot is taken
    on the v4-populated backend BEFORE the downgrade, so a post-migration
    snapshot equal to it proves every row survived the round-trip.
    """
    state_dir = project_dir / ".anvil"
    state_dir.mkdir(parents=True)
    # A config.yaml so read_events_storage / load paths behave like a real
    # project (local mode is the default; no events_storage key needed).
    (state_dir / "config.yaml").write_text(
        "project_name: 'Migrate Me'\nproject_id: 'proj-1'\n",
        encoding="utf-8",
    )

    b = _build_populated_backend(state_dir)
    try:
        pre_snapshot = json.dumps(serialize_state(b), sort_keys=True)
    finally:
        b.close()

    db_path = state_dir / "state.db"
    # Checkpoint+close already happened on b.close(); fold any -wal into the
    # main file so DROP COLUMN sees a single consistent db, then downgrade.
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    _downgrade_db_to_v3(db_path)
    assert read_db_schema_version(db_path) == 3

    return pre_snapshot, state_dir


def _snapshot_db(db_path: Path, events_path: Path) -> str:
    """Open *db_path* read-as-is and return its serialize_state JSON.

    Opens with a SystemClock; serialize_state is clock-independent (it reads
    stored rows), so the snapshot is comparable to the FrozenClock-built one.
    """
    from anvil.clock import SystemClock

    b = SqliteBackend(
        db_path=str(db_path),
        events_path=str(events_path),
        clock=SystemClock(),
        events_storage="local",
    )
    b.initialize()
    try:
        return json.dumps(serialize_state(b), sort_keys=True)
    finally:
        b.close()


class TestMigrateState:
    def test_migrate_state_dry_run_is_default_and_writes_nothing(
        self, tmp_path: Path
    ) -> None:
        """Default invocation reports v3->v4 and mutates nothing on disk."""
        _pre, state_dir = _build_v3_project(tmp_path)
        db_path = state_dir / "state.db"
        db_before = db_path.read_bytes()

        result = _run_in(tmp_path, ["migrate", "state"])

        assert result.exit_code == 0, result.output
        assert "Dry run" in result.output
        assert f"v3 -> v{SCHEMA_VERSION}" in result.output
        # No mutation: version still 3, bytes unchanged, no backup written.
        assert read_db_schema_version(db_path) == 3
        assert db_path.read_bytes() == db_before
        assert not (state_dir / "state.db.pre-schema-migration.bak").exists()

    def test_migrate_state_yes_upgrades_and_preserves_all_rows(
        self, tmp_path: Path
    ) -> None:
        """--yes brings v3 db to current version, backs it up, preserves rows."""
        pre_snapshot, state_dir = _build_v3_project(tmp_path)
        db_path = state_dir / "state.db"
        events_path = state_dir / "events.jsonl"

        result = _run_in(tmp_path, ["migrate", "state", "--yes"])

        assert result.exit_code == 0, result.output
        # On-disk version is now the engine version.
        assert read_db_schema_version(db_path) == SCHEMA_VERSION
        # Backup of the pre-migration db exists.
        backup = state_dir / "state.db.pre-schema-migration.bak"
        assert backup.exists()
        assert read_db_schema_version(backup) == 3
        # ALL rows preserved: the migrated snapshot equals the pre-migration one.
        post_snapshot = _snapshot_db(db_path, events_path)
        assert post_snapshot == pre_snapshot

    def test_migrate_state_replay_still_passes_after_migration(
        self, tmp_path: Path
    ) -> None:
        """Replaying the log into a fresh db reproduces the migrated state."""
        pre_snapshot, state_dir = _build_v3_project(tmp_path)
        events_path = state_dir / "events.jsonl"

        result = _run_in(tmp_path, ["migrate", "state", "--yes"])
        assert result.exit_code == 0, result.output

        # Replay the (untouched) event log into a scratch db and snapshot it.
        scratch_db = tmp_path / "scratch.db"
        from anvil.clock import SystemClock

        b = SqliteBackend(
            db_path=str(scratch_db),
            events_path=str(events_path),
            clock=SystemClock(),
            events_storage="local",
        )
        b.initialize()
        try:
            b.replay_from_empty(str(events_path))
            replayed = json.dumps(serialize_state(b), sort_keys=True)
        finally:
            b.close()
        assert replayed == pre_snapshot

    def test_migrate_state_rerun_is_a_no_op(self, tmp_path: Path) -> None:
        """Re-running migrate on an already-current db changes nothing."""
        _pre, state_dir = _build_v3_project(tmp_path)
        db_path = state_dir / "state.db"

        first = _run_in(tmp_path, ["migrate", "state", "--yes"])
        assert first.exit_code == 0, first.output
        assert read_db_schema_version(db_path) == SCHEMA_VERSION
        db_after_first = db_path.read_bytes()

        second = _run_in(tmp_path, ["migrate", "state"])
        assert second.exit_code == 0, second.output
        assert "already at schema version" in second.output
        assert read_db_schema_version(db_path) == SCHEMA_VERSION
        # The no-op path must not touch the db.
        assert db_path.read_bytes() == db_after_first

        # And a no-op --yes run must not create a second backup either.
        third = _run_in(tmp_path, ["migrate", "state", "--yes"])
        assert third.exit_code == 0, third.output

    def test_migrate_state_json_envelope(self, tmp_path: Path) -> None:
        """--json emits the success envelope with from/to versions."""
        _pre, _state_dir = _build_v3_project(tmp_path)

        dry = _run_in(tmp_path, ["migrate", "state", "--json"])
        assert dry.exit_code == 0, dry.output
        env = json.loads(dry.stdout.strip())
        assert env["ok"] is True
        assert env["command"] == "migrate state"
        assert env["data"]["from_version"] == 3
        assert env["data"]["to_version"] == SCHEMA_VERSION
        assert env["data"]["applied"] is False

        applied = _run_in(tmp_path, ["migrate", "state", "--yes", "--json"])
        assert applied.exit_code == 0, applied.output
        env2 = json.loads(applied.stdout.strip())
        assert env2["ok"] is True
        assert env2["data"]["applied"] is True
        assert env2["data"]["from_version"] == 3
        assert env2["data"]["to_version"] == SCHEMA_VERSION

    def test_migrate_state_refuses_while_claims_active(
        self, tmp_path: Path
    ) -> None:
        """An active claim blocks the migration (same guard as migrate-events)."""
        state_dir = tmp_path / ".anvil"
        state_dir.mkdir(parents=True)
        (state_dir / "config.yaml").write_text(
            "project_name: 'Busy'\nproject_id: 'proj-1'\n", encoding="utf-8"
        )
        b = _make_backend(state_dir)
        try:
            # Minimal feature+task so the claim FK is satisfiable, then an
            # ACTIVE claim with no release.
            b.append(_event(
                "feature.created",
                {
                    "id": "F001",
                    "title": "F",
                    "description": "",
                    "status": "proposed",
                    "requirements": [],
                    "tasks": ["T001"],
                },
                target_kind="feature", target_id="F001",
            ))
            b.append(_event(
                "task.created", _task_payload(task_id="T001"),
                target_id="T001",
            ))
            for from_s, to_s in (
                ("proposed", "drafted"),
                ("drafted", "reviewed"),
                ("reviewed", "ready"),
            ):
                b.append(_event(
                    "task.status_changed",
                    {"task_id": "T001", "from": from_s, "to": to_s},
                    target_id="T001",
                ))
            b.append(_event(
                "claim.created", _claim_payload(claim_id="C001", task_id="T001"),
                target_kind="claim", target_id="C001",
            ))
        finally:
            b.close()

        db_path = state_dir / "state.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        _downgrade_db_to_v3(db_path)

        result = _run_in(tmp_path, ["migrate", "state", "--yes"])

        assert result.exit_code == 1, result.output
        assert "active claim" in result.output.lower()
        # Refused before mutating: still v3, no backup.
        assert read_db_schema_version(db_path) == 3
        assert not (state_dir / "state.db.pre-schema-migration.bak").exists()
