"""B32 / T008 — evidence gate accepts empty files_changed when commands run.

A verification-only / check step runs commands but changes no files. The gate
now accepts that (commands_run is the mandatory proof); both empty is still
rejected.
"""

from __future__ import annotations

import pytest

from anvil.claims.manager import ClaimManager
from anvil.state.backend import EventRejected
from anvil.state.models import EventDraft, TaskStatus
from anvil.workflows.tasks import create_workflow_task, submit_workflow_evidence


def _claim(backend, clock, tid):  # type: ignore[no-untyped-def]
    return ClaimManager(backend, clock, actor="r").claim(tid).claim


def test_empty_files_changed_accepted_with_commands(approved_backend, frozen_clock):  # type: ignore[no-untyped-def]
    tid = create_workflow_task(
        approved_backend, title="t", description="d", actor="r", clock=frozen_clock
    )
    claim = _claim(approved_backend, frozen_clock, tid)
    # files_changed omitted (None -> []) but commands present -> accepted (B32).
    submit_workflow_evidence(
        approved_backend, task_id=tid, claim_id=claim.id, actor="r",
        clock=frozen_clock, commands=["uv run pytest -q"], files_changed=None,
    )
    assert approved_backend.get_task(tid).status == TaskStatus.needs_review


def test_both_empty_still_rejected(approved_backend, frozen_clock):  # type: ignore[no-untyped-def]
    tid = create_workflow_task(
        approved_backend, title="t", description="d", actor="r", clock=frozen_clock
    )
    claim = _claim(approved_backend, frozen_clock, tid)
    # Raw append with empty commands_run AND files_changed -> still rejected.
    draft = EventDraft(
        timestamp=frozen_clock.now(), actor="r", action="evidence.submitted",
        target_kind="task", target_id=tid,
        payload_json={
            "task_id": tid, "claim_id": claim.id, "submitted_by": "r",
            "evidence_id": "EVZZZ", "commands_run": [], "files_changed": [],
            "output_excerpt": None, "pr_url": None, "commit_sha": None,
            "screenshots": [], "known_limitations": None,
        },
    )
    with pytest.raises(EventRejected):
        approved_backend.append(draft)
