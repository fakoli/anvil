"""Failed, overlapping and incomplete observations stay distinct from authority."""
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from anvil.attempt_timing import project_attempt_timing
from anvil.attempt_view import _proof
from anvil.state.models import Claim, ClaimAttestationContext
from tests.test_attempt_view import _read, _reclaim, _submit
from tests.test_attempt_view import claimed as claimed
from tests.test_project_snapshot import _state_path
from tests.test_sqlite import _T0, _make_attestable_claim_payload, _make_claim_command_proof
from tests.test_timing_receipts import _receipt


def _facts():
    attr = _receipt()["attribution"]
    start = datetime(2026, 10, 8, 0, 0, tzinfo=UTC)
    claim = Claim(
        id=attr["claim_id"], task_id=attr["task_id"], claimed_by=attr["claimed_by"],
        generation=attr["generation"], created_at=start,
        lease_expires_at=start + timedelta(hours=4), last_heartbeat_at=start,
        attestation_context=ClaimAttestationContext(**{key: attr[key] for key in (
            "repository_id", "claim_start_sha", "prd_id", "prd_revision", "task_revision",
        )}),
    ).model_dump(mode="json")
    return {
        "project_id": attr["project_id"], "claims": [claim], "evidence": [], "reviews": [],
        "events": [{"id": "E1", "timestamp": claim["created_at"], "actor": "author",
                    "action": "claim.created", "target_kind": "claim", "target_id": claim["id"],
                    "payload": deepcopy(claim)}],
    }


def _observe(facts, receipt, *, event_id="E2", timestamp="2026-10-08T01:00:10Z"):
    facts["events"].append({
        "id": event_id, "timestamp": timestamp, "actor": "author", "action": "progress.noted",
        "target_kind": "task", "target_id": "release:T001",
        "payload": {"task_id": "release:T001", "actor": "author", "notes": "",
                    "noted_at": timestamp, "timing": receipt},
    })


def test_overlap_failed_interrupted_and_monotonic_sum_are_independent():
    facts = _facts()
    _observe(facts, _receipt(ended_at="2026-10-08T01:00:04Z", elapsed_us=3_000_000))
    _observe(facts, _receipt(receipt_id="failed", outcome="failed", exit_code=1,
                            started_at="2026-10-08T01:00:02Z", ended_at="2026-10-08T01:00:05Z",
                            elapsed_us=2_000_000, classification="environment"), event_id="E3")
    _observe(facts, _receipt(receipt_id="stopped", outcome="interrupted", exit_code=None,
                            ended_at=None, elapsed_us=500_000), event_id="E4")
    timing = project_attempt_timing(**facts)
    verification = timing["attempts"][0]["verification"]
    assert timing["utc_verification_elapsed_union_us"] == 5_000_000
    assert timing["summed_monotonic_execution_us"] == 5_500_000
    assert verification["counts"] == {"succeeded": 1, "failed": 1, "interrupted": 1}
    assert verification["classified_counts"] == {"source": 0, "environment": 1, "unknown": 2}
    assert verification["incomplete_intervals"] == 1
    assert timing["attempts"][0]["claim_cycle"]["elapsed_us"] is None
    assert timing["attempts"][0]["claim_cycle"]["status"] == "running"
    assert timing["attempts"][0]["authoring_freeze_interval"] == "unknown"
    assert timing["mutation_authority"] is False and timing == project_attempt_timing(**facts)


def test_exact_duplicate_observation_deduplicates_conflict_refuses():
    facts = _facts()
    _observe(facts, _receipt())
    _observe(facts, _receipt(), event_id="E3")
    assert project_attempt_timing(**facts)["attempts"][0]["verification"]["counts"]["succeeded"] == 1
    facts["events"][-1]["payload"]["timing"]["elapsed_us"] += 1
    with pytest.raises(ValueError, match="conflicting repeated"):
        project_attempt_timing(**facts)


@pytest.mark.parametrize("field,value", [
    ("claimed_by", "other"), ("generation", 3), ("task_id", "other:T001"),
    ("prd_revision", 4), ("task_revision", "e" * 64), ("repository_id", "e" * 64),
    ("claim_start_sha", "e" * 40), ("project_id", "other"),
])
def test_mismatched_historical_identity_never_becomes_measured(field, value):
    facts, receipt = _facts(), _receipt()
    receipt["attribution"][field] = value
    _observe(facts, receipt)
    timing = project_attempt_timing(**facts)
    assert timing["unattributed_timing_count"] == 1
    assert timing["summed_monotonic_execution_us"] is None
    assert timing["utc_verification_elapsed_union_us"] is None


def test_clock_skew_retains_actual_monotonic_and_explicit_observation_boundary():
    facts = _facts()
    _observe(facts, _receipt(ended_at="2026-10-08T00:59:59Z"))
    timing = project_attempt_timing(**facts, observation_at="2026-10-08T02:00:00Z")
    assert timing["utc_verification_elapsed_union_us"] is None
    assert timing["summed_monotonic_execution_us"] == 1_500_000
    assert timing["attempts"][0]["verification"]["clock_skew_intervals"] == 1
    assert timing["attempts"][0]["claim_cycle"]["elapsed_us"] == 7_200_000_000
    with pytest.raises(ValueError):
        project_attempt_timing(**facts, observation_at="2026-10-08T02:00:00")


@pytest.mark.parametrize("change", ["before_creation", "after_lease", "late_after_release", "outer_actor"])
def test_impossible_custody_boundaries_are_unattributed(change):
    facts, receipt = _facts(), _receipt()
    timestamp = "2026-10-08T01:00:10Z"
    if change == "before_creation":
        receipt["started_at"] = "2026-10-07T23:59:00Z"
    elif change == "after_lease":
        timestamp = "2026-10-08T04:00:01Z"
    elif change == "late_after_release":
        facts["events"].append({"id": "ER", "timestamp": timestamp, "actor": "author",
                                "action": "claim.released", "target_kind": "claim",
                                "target_id": facts["claims"][0]["id"],
                                "payload": {"claim_id": facts["claims"][0]["id"]}})
    _observe(facts, receipt, timestamp=timestamp)
    if change == "outer_actor":
        facts["events"][-1]["actor"] = "other"
    assert project_attempt_timing(**facts)["unattributed_timing_count"] == 1


def test_native_rejected_rework_read_preserves_every_generation_and_bytes(claimed):
    _submit(claimed, "EV1")
    _reclaim(claimed)
    _submit(claimed, "EV2", claim_id="C002")
    root = _state_path(claimed)
    before = (root / "events.jsonl").read_bytes()
    view = _read(claimed)
    assert view == _read(claimed)
    timing = view["timing"]
    assert [attempt["generation"] for attempt in timing["attempts"]] == [1, 2]
    assert [attempt["handoffs"][0]["evidence_id"] for attempt in timing["attempts"]] == ["EV1", "EV2"]
    assert timing["attempts"][0]["handoffs"][0]["reviews"][0]["decision"] == "rejected"
    assert timing["attempts"][0]["verification"]["proof_interval_samples"] == 0
    assert timing["summed_monotonic_execution_us"] is None
    assert timing["attempts"][0]["release_event_id"] is not None
    assert (root / "events.jsonl").read_bytes() == before
    assert claimed.get_task("T001").status.value == "needs_review"


def test_native_success_proof_interval_is_utc_not_monotonic_or_capture_time():
    facts = _facts()
    claim = Claim.model_validate(_make_attestable_claim_payload()).model_dump(mode="json")
    facts["project_id"], facts["claims"] = "proj-1", [claim]
    facts["events"] = [{"id": "EC", "timestamp": claim["created_at"], "actor": "agent-alpha",
                        "action": "claim.created", "target_kind": "claim", "target_id": "C001",
                        "payload": deepcopy(claim)}]
    facts["evidence"] = [{
        "id": "EV", "claim_id": "C001",
        "submitted_at": (_T0 + timedelta(minutes=20)).isoformat(),
        "proofs": [_proof(_make_claim_command_proof()), {"kind": "hook_command"}],
    }]
    timing = project_attempt_timing(**facts)
    verify = timing["attempts"][0]["verification"]
    assert verify["proof_interval_samples"] == 1 and verify["capture_only_samples"] == 1
    assert verify["utc_elapsed_union_us"] == verify["summed_proof_utc_interval_us"] == 60_000_000
    assert verify["summed_monotonic_execution_us"] is None
    assert timing["attempts"][0]["handoffs"][0]["verification_to_submission"]["elapsed_us"] == 540_000_000
    facts["evidence"][0]["proofs"][0]["evidence_core"]["generation"] = 2
    unknown = project_attempt_timing(**facts)
    assert unknown["unattributed_timing_count"] == 1
    assert unknown["attempts"][0]["handoffs"][0]["verification_to_submission"]["status"] == "unknown"


@pytest.mark.parametrize("offset_hours", [-5, 2])
def test_native_aware_offsets_preserve_claim_and_release_intervals(
    backend, tmp_path, monkeypatch, offset_hours,
):
    from datetime import timezone

    from anvil.roots import registry
    from tests.test_sqlite import _make_claim_payload, _make_event, _setup_claimable_task

    original = registry.RootSetRegistry
    monkeypatch.setattr(registry, "RootSetRegistry", lambda: original(tmp_path / "owner"))
    _setup_claimable_task(backend)
    offset = _T0.astimezone(timezone(timedelta(hours=offset_hours)))
    backend.append(_make_event("claim.created", _make_claim_payload(now=offset),
                               target_kind="claim", target_id="C001", now=offset))
    backend.append(_make_event(
        "claim.released",
        {"claim_id": "C001", "released_by": "agent-alpha", "release_reason": "stopped"},
        target_kind="claim", target_id="C001", now=offset + timedelta(minutes=2),
    ))
    root = _state_path(backend)
    before = (root / "events.jsonl").read_bytes()
    view = _read(backend)
    assert view == _read(backend)
    assert view["timing"]["attempts"][0]["claim_cycle"]["elapsed_us"] == 120_000_000
    assert view["custody"]["runner_stop"] == "unknown"
    assert (root / "events.jsonl").read_bytes() == before


@pytest.mark.parametrize("terminal", ["release", "force", "stale"])
def test_native_bundle_member_creation_renewal_release_is_readable(tmp_path, monkeypatch, terminal):
    from anvil.roots import registry
    from tests.test_bundle_execution import _backend, _manager, _seed

    original = registry.RootSetRegistry
    monkeypatch.setattr(registry, "RootSetRegistry", lambda: original(tmp_path / "owner"))
    backend = _backend(tmp_path)
    try:
        _seed(backend)
        manager = _manager(backend, tmp_path)
        manager.claim("B001")
        initial = _read(backend, "release:T001")
        assert initial["task"]["status"] == "claimed"
        assert initial["timing"]["attempt_count"] == 1
        assert initial["timing"]["attempts"][0]["claim_cycle"]["status"] == "running"
        manager._clock.advance(seconds=1)
        manager.renew("B001")
        assert _read(backend, "release:T001")["timing"]["attempt_count"] == 1
        if terminal == "release":
            manager.release("B001", reason="owned runner stopped")
        elif terminal == "force":
            from anvil.bundles.manager import BundleManager

            operator = BundleManager(backend, manager._clock, actor="operator", project_root=tmp_path)
            operator.release("B001", force=True, reason="explicit operator recovery")
            assert backend.list_bundle_claims()[0].status.value == "force_released"
        else:
            from anvil.claims.stale import detect_and_release_stale

            manager._clock.advance(hours=5)
            detect_and_release_stale(backend, manager._clock, actor="observer")
        before = (tmp_path / "events.jsonl").read_bytes()
        final = _read(backend, "release:T001")
        assert final == _read(backend, "release:T001")
        assert final["timing"]["attempts"][0]["release_event_id"] is not None
        assert final["timing"]["attempts"][0]["claim_cycle"]["elapsed_us"] == (
            1_000_000 if terminal in {"release", "force"} else 18_001_000_000
        )
        assert final["custody"]["runner_stop"] == "unknown"
        other = _read(backend, "release:T002")
        assert other == _read(backend, "release:T002")
        assert other["timing"]["attempts"][0]["claim_cycle"]["elapsed_us"] == (
            1_000_000 if terminal in {"release", "force"} else 18_001_000_000
        )
        assert other["custody"]["runner_stop"] == "unknown"
        assert (tmp_path / "events.jsonl").read_bytes() == before
    finally:
        backend.close()


def test_missing_standalone_creation_still_refuses():
    facts = _facts()
    facts["events"] = []
    with pytest.raises(ValueError, match="claim creation"):
        project_attempt_timing(**facts)
