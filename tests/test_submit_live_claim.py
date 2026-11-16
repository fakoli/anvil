"""Evidence may only land on a claim that still holds its task.

The CLI and MCP surfaces look the active claim up before they take the append
lock, so a release, stale reap, or lease expiry can land in between. The engine
re-judges the claim under the lock for the first evidence it records.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any

import pytest

from anvil.cli._helpers import _open_backend
from anvil.state.backend import EventRejected
from anvil.state.models import EventDraft
from anvil.state.sqlite import SqliteBackend
from tests.test_stale_task_graph import _invoke, _ok, _planned_project


def _claimed_project(tmp_path: Path) -> str:
    _planned_project(tmp_path)
    claimed = _ok(tmp_path, ["claim", "T001", "--actor", "tester", "--json"])
    return str(json.loads(claimed.stdout.strip())["data"]["claim"]["id"])


def _evidence_draft(claim_id: str, evidence_id: str = "EVRACE01") -> EventDraft:
    return EventDraft(
        timestamp=datetime.datetime.now(datetime.UTC),
        actor="tester",
        action="evidence.submitted",
        target_kind="task",
        target_id="T001",
        payload_json={
            "task_id": "T001",
            "claim_id": claim_id,
            "submitted_by": "tester",
            "evidence_id": evidence_id,
            "commands_run": ["pytest tests/test_converter.py -v"],
            "files_changed": ["src/app/converter.py"],
        },
    )


def _release_draft(claim_id: str) -> EventDraft:
    return EventDraft(
        timestamp=datetime.datetime.now(datetime.UTC),
        actor="tester",
        action="claim.released",
        target_kind="claim",
        target_id=claim_id,
        payload_json={
            "claim_id": claim_id,
            "released_by": "tester",
            "release_reason": "revoked",
            "force": False,
        },
    )


def _open(tmp_path: Path) -> SqliteBackend:
    return _open_backend(tmp_path / ".anvil", project_root=tmp_path)


def test_engine_refuses_first_evidence_on_a_released_claim(tmp_path: Path) -> None:
    claim_id = _claimed_project(tmp_path)
    backend = _open(tmp_path)
    try:
        backend.append(_release_draft(claim_id))
        with pytest.raises(EventRejected, match="no longer active"):
            backend.append(_evidence_draft(claim_id))
        assert backend.get_latest_evidence("T001") is None
    finally:
        backend.close()


def test_engine_refuses_first_evidence_after_the_lease_expired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claim_id = _claimed_project(tmp_path)
    backend = _open(tmp_path)
    try:
        claim = backend.get_claim(claim_id)
        assert claim is not None
        after_expiry = claim.lease_expires_at + datetime.timedelta(seconds=1)
        monkeypatch.setattr(backend._clock, "now", lambda: after_expiry)
        with pytest.raises(EventRejected, match="expired"):
            backend.append(_evidence_draft(claim_id))
        assert backend.get_latest_evidence("T001") is None
    finally:
        backend.close()


def test_exact_resubmission_stays_an_idempotent_replay(tmp_path: Path) -> None:
    claim_id = _claimed_project(tmp_path)
    backend = _open(tmp_path)
    try:
        backend.append(_evidence_draft(claim_id))
        # The first submission auto-released the claim; the same evidence id
        # again is a replay of recorded work, not new evidence on a dead claim.
        backend.append(_evidence_draft(claim_id))
        evidence = backend.get_latest_evidence("T001")
        assert evidence is not None and evidence.id == "EVRACE01"
    finally:
        backend.close()


def test_cli_submit_refuses_a_claim_revoked_after_its_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claim_id = _claimed_project(tmp_path)
    monkeypatch.setattr("anvil.cli.packet_apply._reap_stale_claims", lambda backend: None)
    original = SqliteBackend.list_active_claims
    revoked: list[bool] = []

    def lookup_then_revoke(self: SqliteBackend) -> Any:
        claims = original(self)
        if not revoked:
            revoked.append(True)
            self.append(_release_draft(claim_id))
        return claims

    monkeypatch.setattr(SqliteBackend, "list_active_claims", lookup_then_revoke)

    result = _invoke(
        tmp_path,
        [
            "submit", "T001", "--actor", "tester", "--json",
            "--commands", "pytest tests/test_converter.py -v",
            "--files-changed", "src/app/converter.py",
        ],
    )

    assert revoked, "the lookup never ran"
    assert result.exit_code == 1, result.output
    envelope = json.loads(result.stdout.strip())
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "event_rejected"
    assert "no longer active" in envelope["error"]["message"]
    monkeypatch.undo()
    backend = _open(tmp_path)
    try:
        assert backend.get_latest_evidence("T001") is None
        task = backend.get_task("T001")
        assert task is not None and task.status.value != "needs_review"
    finally:
        backend.close()


def test_mcp_submit_refuses_a_claim_revoked_after_its_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    import anvil.mcp_server as mcp_server

    claim_id = _claimed_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mcp_server, "_reap_stale", lambda backend: None)
    original = mcp_server._find_active_claim_for_task

    def lookup_then_revoke(backend: Any, task_id: str) -> Any:
        claim = original(backend, task_id)
        backend.append(_release_draft(claim_id))
        return claim

    monkeypatch.setattr(mcp_server, "_find_active_claim_for_task", lookup_then_revoke)

    async def submit() -> Any:
        async with Client(mcp_server.mcp) as client:
            return await client.call_tool(
                "submit_completion_evidence",
                {
                    "task_id": "T001",
                    "actor": "tester",
                    "commands_run": ["pytest tests/test_converter.py -v"],
                    "files_changed": ["src/app/converter.py"],
                },
            )

    with pytest.raises(ToolError, match="no longer active"):
        asyncio.run(submit())
    monkeypatch.undo()
    backend = _open(tmp_path)
    try:
        assert backend.get_latest_evidence("T001") is None
    finally:
        backend.close()
