"""Observation bounds and failed runs never become successful evidence."""
import json
from collections import UserDict
from types import MappingProxyType

import pytest
from pydantic import ValidationError

from anvil.state.hashing import canonical_json_bytes
from anvil.state.models import HookCommandAttribution
from anvil.timing_receipts import MAX_TIMING_RECEIPT_BYTES, CommandTimingReceipt


def _receipt(**overrides):
    return {
        "schema_version": 1, "receipt_id": "real-run-1",
        "attribution": {
            "project_id": "project", "task_id": "release:T001", "claim_id": "C00000001",
            "generation": 2, "claimed_by": "author", "task_revision": "a" * 64,
            "prd_id": "release", "prd_revision": 3,
            "repository_id": "b" * 64, "claim_start_sha": "c" * 40,
        },
        "command_sha256": "d" * 64, "started_at": "2026-10-08T01:00:00Z",
        "ended_at": "2026-10-08T01:00:02Z", "elapsed_us": 1_500_000,
        "outcome": "succeeded", "exit_code": 0, **overrides,
    }


@pytest.mark.parametrize("outcome,code", [("succeeded", 0), ("failed", 1), ("failed", -9), ("interrupted", 130)])
def test_completed_observations_round_trip_without_inventing_wall_elapsed(outcome, code):
    observed = CommandTimingReceipt(**_receipt(outcome=outcome, exit_code=code))
    assert observed.utc_interval_status == "complete"
    assert observed.elapsed_us == 1_500_000
    assert CommandTimingReceipt.model_validate_json(observed.model_dump_json()) == observed
    assert len(observed.semantic_digest()) == 64
    assert observed.semantic_digest() != CommandTimingReceipt(**_receipt(receipt_id="run-2", outcome=outcome, exit_code=code)).semantic_digest()
    assert "kind" not in observed.model_dump() and "output" not in observed.model_dump_json()


@pytest.mark.parametrize("elapsed", [None, 0, 100])
def test_interruption_retains_missing_end_and_partial_measurement(elapsed):
    observed = CommandTimingReceipt(**_receipt(outcome="interrupted", ended_at=None, exit_code=None, elapsed_us=elapsed))
    assert observed.utc_interval_status == "partial" and observed.elapsed_us == elapsed


@pytest.mark.parametrize("start,end", [
    ("2026-10-08T01:00:00Z", "2026-10-08T00:59:59Z"),
    ("2026-10-08T01:00:00.100000Z", "2026-10-08T01:00:00Z"),
])
def test_clock_skew_retains_monotonic_measurement(start, end):
    observed = CommandTimingReceipt(**_receipt(started_at=start, ended_at=end))
    assert observed.utc_interval_status == "clock_skew"
    assert observed.elapsed_us == 1_500_000 and observed.ended_at == end


@pytest.mark.parametrize("changes", [
    {"schema_version": True}, {"schema_version": 1.0}, {"schema_version": 2},
    {"elapsed_us": True}, {"elapsed_us": 1.0}, {"elapsed_us": -1}, {"elapsed_us": 2**63},
    {"exit_code": False}, {"exit_code": 0.0}, {"exit_code": 2**63},
    {"ended_at": None}, {"elapsed_us": None}, {"exit_code": None},
    {"outcome": "failed", "exit_code": 0}, {"outcome": "succeeded", "exit_code": 1},
    {"outcome": "interrupted", "exit_code": 0},
    {"started_at": "2026-10-08T01:00:00"}, {"started_at": "2026-10-08T01:00:00+00:00"},
    {"started_at": "2026-10-08T02:00:00+01:00"}, {"started_at": "garbage"},
    {"command_sha256": "bad"}, {"receipt_id": "../run"}, {"receipt_id": "x" * 129},
    {"output": "private"}, {"classification": "guessed"},
])
def test_malformed_observations_refuse(changes):
    with pytest.raises(ValidationError):
        CommandTimingReceipt(**_receipt(**changes))


@pytest.mark.parametrize("field,value", [("schema_version", True), ("generation", True), ("prd_revision", 0), ("task_revision", "bad")])
def test_native_attribution_is_revalidated(field, value):
    data = _receipt()
    data["attribution"][field] = value
    with pytest.raises(ValidationError):
        CommandTimingReceipt(**data)
    data["attribution"] = HookCommandAttribution.model_construct(**data["attribution"])
    with pytest.raises(ValidationError):
        CommandTimingReceipt(**data)


@pytest.mark.parametrize("mapping", [dict, UserDict, MappingProxyType])
@pytest.mark.parametrize("version", [True, 1.0, 1])
def test_all_attribution_mappings_preserve_strict_schema(mapping, version):
    data = _receipt()
    data["attribution"] = mapping({**data["attribution"], "schema_version": version})
    if type(version) is int:
        assert CommandTimingReceipt(**data).attribution.schema_version == 1
    else:
        with pytest.raises(ValidationError):
            CommandTimingReceipt(**data)


def test_canonical_byte_cap_counts_utf8_and_exact_boundary():
    base = CommandTimingReceipt(**_receipt()).model_dump(mode="json")
    base["attribution"]["claimed_by"] = ""
    overhead = len(canonical_json_bytes(base))
    size = MAX_TIMING_RECEIPT_BYTES - overhead
    base["attribution"]["claimed_by"] = "😀" * (size // 4) + "a" * (size % 4)
    exact = CommandTimingReceipt(**base)
    assert len(canonical_json_bytes(exact.model_dump(mode="json"))) == MAX_TIMING_RECEIPT_BYTES
    base["attribution"]["claimed_by"] += "a"
    with pytest.raises(ValueError, match="byte_limit_exceeded"):
        CommandTimingReceipt(**base)
    hostile = _receipt()
    hostile["attribution"]["claimed_by"] = "\ud800"
    with pytest.raises((ValueError, UnicodeError)):
        CommandTimingReceipt(**hostile)
    assert json.loads(exact.model_dump_json())["elapsed_us"] == 1_500_000
