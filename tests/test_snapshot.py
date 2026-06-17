"""Tests for fakoli_state.state.snapshot.serialize_state.

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

from fakoli_state.clock import FrozenClock
from fakoli_state.state.models import EventDraft
from fakoli_state.state.snapshot import serialize_state
from fakoli_state.state.sqlite import SqliteBackend

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
            "project", "prd", "features", "tasks",
            "claims", "reviews", "evidence", "requirements", "sync_mappings",
        }

        # Singletons present.
        assert snap["project"] is not None
        assert snap["project"]["id"] == "proj-1"
        assert snap["prd"] is not None
        assert snap["prd"]["status"] == "reviewed"

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
        assert snap["prd"] is None
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
