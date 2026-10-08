"""Timing is exact-owner audit data, with no lifecycle or replay authority."""
from copy import deepcopy
from datetime import timedelta

import pytest

from anvil.state.backend import EventRejected
from tests.test_sqlite import (
    _T0,
    _make_attestable_claim_payload,
    _make_event,
    _setup_claimable_task,
)
from tests.test_timing_receipts import _receipt


def _seed(backend, frozen_clock, *, without_context=False):
    _setup_claimable_task(backend)
    claim = _make_attestable_claim_payload()
    stored = deepcopy(claim)
    if without_context:
        stored.pop("attestation_context")
    backend.append(_make_event("claim.created", stored, target_kind="claim", target_id="C001"))
    frozen_clock.advance(minutes=20)
    context = claim["attestation_context"]
    timing = _receipt(
        attribution={
            **{key: context[key] for key in (
                "repository_id", "claim_start_sha", "prd_id", "prd_revision", "task_revision",
            )},
            "project_id": "proj-1", "claim_id": "C001", "generation": 1,
            "claimed_by": "agent-alpha", "task_id": "T001",
        },
        started_at=(_T0 + timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        ended_at=(_T0 + timedelta(minutes=2)).isoformat().replace("+00:00", "Z"),
    )
    return {
        "task_id": "T001", "actor": "agent-alpha", "notes": "observed",
        "noted_at": frozen_clock.now().isoformat(), "timing": timing,
    }


def _note(backend, payload, *, now=None, pre_log_check=None):
    return backend.append(
        _make_event("progress.noted", payload, target_kind="task", target_id="T001",
                    now=now or _T0 + timedelta(minutes=20)),
        pre_log_check=pre_log_check,
    )


def _snapshot(backend, state_dir):
    return (
        (state_dir / "events.jsonl").read_bytes(),
        backend.get_task("T001"), backend.get_claim("C001"),
    )


@pytest.mark.parametrize("kind", ["failed", "interrupted", "skew", "succeeded"])
def test_live_timing_is_audit_only_and_replays_without_live_guard(
    backend, frozen_clock, state_dir, monkeypatch, kind,
):
    payload = _seed(backend, frozen_clock)
    if kind == "failed":
        payload["timing"].update(outcome="failed", exit_code=1, classification="environment")
    elif kind == "interrupted":
        payload["timing"].update(outcome="interrupted", exit_code=None, ended_at=None)
    elif kind == "skew":
        payload["timing"]["ended_at"] = _T0.isoformat().replace("+00:00", "Z")
    before = _snapshot(backend, state_dir)
    first = _note(backend, payload)
    _note(backend, payload)  # Duplication cannot renew or become qualifying progress.
    assert first.payload_json["timing"] == payload["timing"]
    assert _snapshot(backend, state_dir)[1:] == before[1:]
    log = (state_dir / "events.jsonl").read_bytes()
    frozen_clock.advance(hours=2)
    monkeypatch.setattr(backend, "_check_timing_noted", lambda *args: pytest.fail("live guard in replay"))
    backend.replay_from_empty(str(state_dir / "events.jsonl"))
    assert _snapshot(backend, state_dir)[1:] == before[1:]
    assert (state_dir / "events.jsonl").read_bytes() == log


@pytest.mark.parametrize("field,value", [
    ("project_id", "other"), ("claim_id", "missing"), ("generation", 2),
    ("claimed_by", "other"), ("task_id", "other:T001"), ("prd_id", "other"),
    ("prd_revision", 2), ("task_revision", "d" * 64),
    ("repository_id", "d" * 64), ("claim_start_sha", "d" * 40),
])
def test_exact_frozen_identity_refuses_before_event_or_domain_write(
    backend, frozen_clock, state_dir, field, value,
):
    payload = _seed(backend, frozen_clock)
    payload["timing"]["attribution"][field] = value
    before = _snapshot(backend, state_dir)
    with pytest.raises(EventRejected, match="timing ownership"):
        _note(backend, payload)
    assert _snapshot(backend, state_dir) == before


@pytest.mark.parametrize("change", ["before_start", "before_end", "future_start", "future_end", "noted", "naive", "expired", "callback_expired"])
def test_lifetime_and_final_callback_boundary_refuse(backend, frozen_clock, state_dir, change):
    payload = _seed(backend, frozen_clock)
    callback = None
    if change in ("before_start", "before_end"):
        field = "started_at" if change == "before_start" else "ended_at"
        payload["timing"][field] = (_T0 - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    elif change in ("future_start", "future_end"):
        field = "started_at" if change == "future_start" else "ended_at"
        payload["timing"][field] = (_T0 + timedelta(minutes=21)).isoformat().replace("+00:00", "Z")
    elif change == "noted":
        payload["noted_at"] = _T0.isoformat()
    elif change == "naive":
        payload["noted_at"] = (_T0 + timedelta(minutes=20)).replace(tzinfo=None).isoformat()
    elif change == "expired":
        frozen_clock.advance(hours=1)
    else:
        def callback():
            frozen_clock.advance(hours=1)
    before = _snapshot(backend, state_dir)
    with pytest.raises(EventRejected, match="timing ownership"):
        _note(backend, payload, pre_log_check=callback)
    assert _snapshot(backend, state_dir) == before


def test_plain_unclaimed_notes_keep_absent_timing_bytes(backend, state_dir):
    _setup_claimable_task(backend)
    payload = {"task_id": "T001", "actor": "author", "notes": "old", "noted_at": _T0.isoformat()}
    event = _note(backend, deepcopy(payload), now=_T0)
    assert event.payload_json == payload and "timing" not in event.payload_json
    before = (state_dir / "events.jsonl").read_bytes()
    backend.replay_from_empty(str(state_dir / "events.jsonl"))
    assert (state_dir / "events.jsonl").read_bytes() == before


@pytest.mark.parametrize("change", ["missing_context", "released", "malformed"])
def test_no_live_ordinary_custody_no_timing_write(backend, frozen_clock, state_dir, change):
    payload = _seed(backend, frozen_clock, without_context=change == "missing_context")
    if change == "released":
        backend.append(_make_event(
            "claim.released",
            {"claim_id": "C001", "released_by": "agent-alpha", "release_reason": "stopped"},
            target_kind="claim", target_id="C001", now=frozen_clock.now(),
        ))
    elif change == "malformed":
        payload["timing"]["elapsed_us"] = True
    before = _snapshot(backend, state_dir)
    with pytest.raises(EventRejected):
        _note(backend, payload)
    assert _snapshot(backend, state_dir) == before
