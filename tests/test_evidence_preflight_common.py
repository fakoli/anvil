"""Advisory coverage keeps native proof limits and never changes State."""
import json
from datetime import UTC, datetime

import pytest

from anvil.attempt_view import read_evidence_preflight
from anvil.cli._helpers import _open_backend
from anvil.project_snapshot import ProjectSnapshotError
from tests.test_evidence_preflight import _capture, _claim


@pytest.mark.parametrize("count,failed", [(0, False), (1, False), (1, True), (16, True), (17, True)])
def test_complete_preflight_keeps_failed_captures_and_refuses_overflow(tmp_path, monkeypatch, count, failed):
    task_id, claim_id = _claim(tmp_path)
    monkeypatch.setenv("ANVIL_CLAIM_ID", claim_id)
    for _ in range(count):
        _capture(tmp_path, failed=failed)
    state = tmp_path / ".anvil"
    buffer = state / ".evidence-buffer" / f"{claim_id}.json"
    log = (state / "events.jsonl").read_bytes()
    original_buffer = buffer.read_bytes() if buffer.exists() else None
    backend = _open_backend(state, project_root=tmp_path)
    before_claim = backend.get_claim(claim_id)
    try:
        if count == 17:
            with pytest.raises(ProjectSnapshotError):
                read_evidence_preflight(state, task_id)
        else:
            result = read_evidence_preflight(state, task_id,
                observation_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"))
            assert result["schema_id"] == "anvil.state.evidence-preflight.v1"
            assert result["advisory_only"] and not result["mutation_authority"]
            assert result["hard_proof_limits"] == {"max_items": 16, "max_bytes": 1_048_576}
            assert result["buffer"]["valid_proof_count"] == count
            assert result["buffer"]["inspected_records"] == count
            assert result["required_command_proofs"]
            assert all(item["satisfied"] for item in result["required_command_proofs"]) == bool(count and not failed)
            assert str(tmp_path) not in json.dumps(result)
        assert backend.get_claim(claim_id) == before_claim
        assert (state / "events.jsonl").read_bytes() == log
        assert (buffer.read_bytes() if buffer.exists() else None) == original_buffer
    finally:
        backend.close()


def test_preflight_without_observation_does_not_invent_lease_eligibility(tmp_path, monkeypatch):
    task, claim = _claim(tmp_path)
    monkeypatch.setenv("ANVIL_CLAIM_ID", claim)
    _capture(tmp_path)
    result = read_evidence_preflight(tmp_path / ".anvil", task)
    assert result["observation_at"] is None
    assert "lease_time_unobserved" in result["problems"]
    assert result["mutation_authority"] is False
