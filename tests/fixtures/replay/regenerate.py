#!/usr/bin/env python3
"""Golden-fixture generator for the SL-1 replay-equivalence test.

This script builds a realistic, *deterministic* anvil event log by
driving the real ``SqliteBackend.append`` pipeline with a ``FrozenClock``,
then commits three artifacts under
``tests/fixtures/replay/sample-project/``:

* ``events.jsonl``       — the deterministic audit log the scenario produced
                            (canonical events only — no tombstones).
* ``audit.jsonl``        — the sibling audit log (rejections + idempotent no-ops).
* ``expected-state.json`` — ``serialize_state`` of that backend, pretty-printed
                            with ``sort_keys=True`` and a trailing newline.

All three artifacts are committed. Regeneration is a **deliberate human step**
— it is never run automatically by the test suite. Re-run it only when the
fixture *legitimately* changes (e.g. a new canonical collection is added to
``serialize_state`` or the scenario is intentionally extended):

    uv run --project plugins/anvil/bin \
        python plugins/anvil/tests/fixtures/replay/regenerate.py

After regenerating, eyeball the diff (``git diff`` on the three artifacts),
run the equivalence test, and commit the artifacts together with the code
change that motivated them.

Determinism contract
--------------------
Every timestamp in the produced ``events.jsonl`` comes from the ``FrozenClock``
(advanced explicitly between steps) or from explicit ISO strings in the event
payloads — never from wall-clock time. The scenario uses fixed IDs throughout.
As a result, running this script twice produces byte-identical artifacts.

Scenario exercised
-------------------
A small but realistic project lifecycle:

1. ``project.created`` + ``state.initialized``.
2. ``prd.parsed`` (one requirement) + ``prd.reviewed``.
3. One ``feature.created``.
4. Three tasks created and each promoted proposed -> drafted -> reviewed ->
   ready (a non-trivial multi-task log).
5. T001: ``claim.created`` with a lease, a heartbeat ``claim.renewed`` after the
   clock advances, then ``evidence.submitted`` (auto-releases the claim) and a
   ``task.applied`` acceptance (inserts a review row, task -> done).
6. T002: ``claim.created`` followed by ``claim.stale`` (a stale-claim reap;
   task returns to ready).
7. A ``sync_mapping.upserted`` mirroring T001 into github_issues.
8. Two audit-only entries proving rejections and idempotent no-ops land in
   ``audit.jsonl`` and NOT in ``events.jsonl``:
     * an ``idempotent_no_op`` produced by a real duplicate
       ``evidence.submitted`` for C001 under a different evidence_id (the
       ``_check_evidence_submitted`` raises ``IdempotentNoOp``; ``append``
       catches it, writes an ``idempotent_no_op`` line to ``audit.jsonl``,
       and returns None — nothing touches events.jsonl), and
     * a ``rejection`` produced by an ``evidence.submitted`` draft with empty
       ``commands_run`` (the ``_check_evidence_submitted`` raises
       ``EventRejected``; ``append`` catches it, writes a ``rejection`` line to
       ``audit.jsonl``, re-raises — again nothing touches events.jsonl).

SL1-RR-1 guarantee: every line in ``events.jsonl`` is a real canonical event.
``replay_from_empty`` applies every line with no skip-list; the log is
failure-free by construction.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from anvil.clock import FrozenClock
from anvil.state.backend import EventRejected
from anvil.state.models import EventDraft
from anvil.state.snapshot import serialize_state
from anvil.state.sqlite import SqliteBackend

# Fixed scenario epoch — matches the suite-wide FrozenClock anchor.
_T0 = datetime(2026, 5, 24, 18, 0, 0, tzinfo=UTC)

_FIXTURE_DIR = Path(__file__).parent / "sample-project"
_EVENTS_OUT = _FIXTURE_DIR / "events.jsonl"
_AUDIT_OUT = _FIXTURE_DIR / "audit.jsonl"
_EXPECTED_OUT = _FIXTURE_DIR / "expected-state.json"


def _draft(
    action: str,
    payload: dict[str, Any],
    *,
    clock: FrozenClock,
    target_kind: str = "task",
    target_id: str = "T001",
) -> EventDraft:
    """Build an EventDraft timestamped at the clock's current time."""
    return EventDraft(
        timestamp=clock.now(),
        actor="test",
        action=action,
        target_kind=target_kind,
        target_id=target_id,
        payload_json=payload,
    )


def _task_payload(*, task_id: str, clock: FrozenClock) -> dict[str, Any]:
    now = clock.now().isoformat()
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
        "created_at": now,
        "updated_at": now,
    }


def _claim_payload(
    *, claim_id: str, task_id: str, clock: FrozenClock
) -> dict[str, Any]:
    now = clock.now()
    return {
        "id": claim_id,
        "task_id": task_id,
        "claimed_by": "agent-alpha",
        "claim_type": "task",
        "status": "active",
        "branch": None,
        "worktree_path": None,
        "expected_files": [],
        "created_at": now.isoformat(),
        "lease_expires_at": (now + timedelta(hours=1)).isoformat(),
        "last_heartbeat_at": now.isoformat(),
        "released_at": None,
        "release_reason": None,
    }


def build_scenario(state_dir: Path) -> SqliteBackend:
    """Build the canonical scenario through append(EventDraft); return the backend.

    The clock is advanced between steps so lease / heartbeat / renew / stale
    timestamps are realistic and distinct. All event ids are assigned by the
    backend's log-authority counter — no hardcoded ids.
    """
    clock = FrozenClock(_T0)
    db_path = str(state_dir / "state.db")
    events_path = str(state_dir / "events.jsonl")
    Path(events_path).touch()
    b = SqliteBackend(db_path=db_path, events_path=events_path, clock=clock)
    b.initialize()

    # 1. Project + state init.
    b.append(_draft(
        "project.created",
        {
            "id": "proj-1",
            "name": "Sample Project",
            "description": "A realistic replay-equivalence fixture.",
            "created_at": _T0.isoformat(),
            "updated_at": _T0.isoformat(),
        },
        clock=clock,
        target_kind="project", target_id="proj-1",
    ))
    b.append(_draft(
        "state.initialized", {},
        clock=clock,
        target_kind="project", target_id="proj-1",
    ))

    # 2. PRD parsed (one requirement) + reviewed.
    b.append(_draft(
        "prd.parsed",
        {
            "project_id": "proj-1",
            "status": "draft",
            "summary": "Build the thing.",
            "goals": ["Ship SL-1."],
            "non_goals": [],
            "requirements": [
                {"id": "R001", "prd_section": "requirements",
                 "text": "Replay must reproduce canonical state.",
                 "source_paragraph": None, "derived": False},
            ],
            "acceptance_criteria": ["Golden snapshot matches."],
            "risks": [],
            "open_questions": [],
        },
        clock=clock,
        target_kind="prd", target_id="proj-1",
    ))
    b.append(_draft(
        "prd.reviewed", {"project_id": "proj-1", "reviewer": "alice"},
        clock=clock,
        target_kind="prd", target_id="proj-1",
    ))

    # 3. One feature.
    b.append(_draft(
        "feature.created",
        {
            "id": "F001",
            "title": "Feature F001",
            "description": "The only feature.",
            "status": "proposed",
            "requirements": [],
            "tasks": [],
        },
        clock=clock,
        target_kind="feature", target_id="F001",
    ))

    # 4. Three tasks, each promoted proposed -> drafted -> reviewed -> ready.
    for task_id in ("T001", "T002", "T003"):
        b.append(_draft(
            "task.created", _task_payload(task_id=task_id, clock=clock),
            clock=clock, target_id=task_id,
        ))
        for from_s, to_s in (
            ("proposed", "drafted"),
            ("drafted", "reviewed"),
            ("reviewed", "ready"),
        ):
            b.append(_draft(
                "task.status_changed",
                {"task_id": task_id, "from": from_s, "to": to_s},
                clock=clock, target_id=task_id,
            ))

    # 5. T001 — claim with a lease, a heartbeat/renew after the clock advances,
    # then evidence.submitted (auto-releases the claim) + task.applied accepted.
    b.append(_draft(
        "claim.created", _claim_payload(claim_id="C001", task_id="T001", clock=clock),
        clock=clock, target_kind="claim", target_id="C001",
    ))

    clock.advance(minutes=20)  # work happens; heartbeat renews the lease.
    renew_now = clock.now()
    b.append(_draft(
        "claim.renewed",
        {
            "claim_id": "C001",
            "lease_expires_at": (renew_now + timedelta(hours=1)).isoformat(),
            "last_heartbeat_at": renew_now.isoformat(),
            "renewed_by": "agent-alpha",
        },
        clock=clock, target_kind="claim", target_id="C001",
    ))

    clock.advance(minutes=10)
    b.append(_draft(
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
        clock=clock, target_id="T001",
    ))
    b.append(_draft(
        "task.applied",
        {"task_id": "T001", "reviewer": "alice", "decision": "accepted", "notes": None},
        clock=clock, target_id="T001",
    ))

    # 6. T002 — claim then claim.stale (a stale-claim reap; task -> ready).
    clock.advance(minutes=5)
    b.append(_draft(
        "claim.created", _claim_payload(claim_id="C002", task_id="T002", clock=clock),
        clock=clock, target_kind="claim", target_id="C002",
    ))
    clock.advance(hours=2)  # lease (1h) is now expired.
    stale_now = clock.now()
    b.append(_draft(
        "claim.stale",
        {
            "claim_id": "C002",
            "task_id": "T002",
            "expired_at": (stale_now - timedelta(hours=1)).isoformat(),
            "detected_at": stale_now.isoformat(),
            "reason": "lease_expired",
            "actor": "system",
        },
        clock=clock, target_kind="claim", target_id="C002",
    ))

    # 7. sync_mapping.upserted — mirror T001 into github_issues.
    b.append(_draft(
        "sync_mapping.upserted",
        {
            "task_id": "T001",
            "external_system": "github_issues",
            "external_id": "42",
            "external_url": "https://github.com/acme/repo/issues/42",
            "last_synced_at": clock.now().isoformat(),
            "sync_state": "in_sync",
            "conflict_resolution_strategy": "prompt",
            "provider_metadata": {"labels": ["sl-1"]},
        },
        clock=clock,
        target_kind="sync_mapping", target_id="T001",
    ))

    # 8a. idempotent_no_op — a real duplicate evidence.submitted for C001 under
    # a DIFFERENT evidence_id. _check_evidence_submitted raises IdempotentNoOp;
    # append() catches it, writes an idempotent_no_op line to audit.jsonl, and
    # returns None. Nothing is written to events.jsonl.
    result = b.append(_draft(
        "evidence.submitted",
        {
            "task_id": "T001",
            "claim_id": "C001",
            "evidence_id": "EV-DUP",   # different id for same claim -> no-op.
            "submitted_by": "agent-alpha",
            "commands_run": ["pytest -q"],
            "files_changed": ["src/auth.py"],
            "output_excerpt": "ok",
            "pr_url": None,
            "commit_sha": None,
            "screenshots": [],
            "known_limitations": None,
        },
        clock=clock, target_id="T001",
    ))
    assert result is None, (
        "Expected idempotent no-op (None) for duplicate evidence submission; "
        f"got {result!r}. Check _check_evidence_submitted."
    )

    # 8b. rejection — evidence.submitted with empty commands_run. The
    # _check_evidence_submitted raises EventRejected; append() writes a
    # rejection line to audit.jsonl and re-raises. Nothing touches events.jsonl.
    try:
        b.append(_draft(
            "evidence.submitted",
            {
                "task_id": "T003",
                "claim_id": "C001",  # already-released claim, wrong task — but
                # the empty commands_run guard fires first.
                "evidence_id": "EV-BAD",
                "submitted_by": "agent-alpha",
                "commands_run": [],   # <- empty: triggers EventRejected
                "files_changed": ["src/x.py"],
                "output_excerpt": None,
                "pr_url": None,
                "commit_sha": None,
                "screenshots": [],
                "known_limitations": None,
            },
            clock=clock, target_id="T003",
        ))
    except EventRejected:
        pass  # expected — rejection written to audit.jsonl automatically

    return b


def main() -> None:
    _FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        state_dir = Path(tmp)
        b = build_scenario(state_dir)
        try:
            snap = serialize_state(b)
        finally:
            b.close()

        # Copy the deterministic events.jsonl into the fixture dir.
        shutil.copyfile(state_dir / "events.jsonl", _EVENTS_OUT)

        # Copy the audit.jsonl sibling into the fixture dir.
        audit_src = state_dir / "audit.jsonl"
        if audit_src.exists():
            shutil.copyfile(audit_src, _AUDIT_OUT)
        else:
            _AUDIT_OUT.write_text("", encoding="utf-8")

        # Write the golden snapshot: pretty-printed, sort_keys, trailing newline.
        _EXPECTED_OUT.write_text(
            json.dumps(snap, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )

    print(f"wrote {_EVENTS_OUT}")
    print(f"wrote {_AUDIT_OUT}")
    print(f"wrote {_EXPECTED_OUT}")


if __name__ == "__main__":
    main()
