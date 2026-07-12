"""Contract and adversarial tests for the model-neutral policy benchmark."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from benchmarks.bundle_workflow_fixture import compare_fixture, load_and_compare

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "benchmarks" / "fixtures" / "bundle_workflow.json"


def _source() -> dict:  # type: ignore[type-arg]
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_fixture_records_required_raw_metrics_for_paired_policies() -> None:
    source = _source()
    assert {row["policy"] for row in source["observations"]} == {
        "task_per_agent",
        "coordinator_first",
    }
    required = {
        "trial",
        "seed",
        "execution_profile_id",
        "run_id",
        "provenance",
        "run_started_ms",
        "accepted_commit_ms",
        "accepted_commit_sha",
        "accepted_task_ids",
        "coordinator_tokens",
        "delegate_tokens",
        "review_findings",
        "rereviews",
        "wait_ms",
        "human_interventions",
    }
    assert all(required <= set(row) for row in source["observations"])


def test_comparison_is_deterministic_descriptive_and_model_neutral() -> None:
    first = load_and_compare(FIXTURE)
    second = load_and_compare(FIXTURE)
    assert first == second
    assert first["descriptive_only"] is True
    assert set(first["policies"]) == {"task_per_agent", "coordinator_first"}
    assert "winner" not in json.dumps(first).lower()
    source_text = FIXTURE.read_text(encoding="utf-8").lower()
    for vendor_or_model in ("anthropic", "openai", "claude", "codex", "gemini"):
        assert vendor_or_model not in source_text


def test_accepted_task_efficiency_uses_all_tokens_and_proven_acceptance() -> None:
    result = load_and_compare(FIXTURE)
    for summary in result["policies"].values():
        tokens = summary["tokens"]
        assert summary["all_trials_complete"] is True
        assert tokens["total"] == tokens["coordinator"] + tokens["delegates_total"]
        assert summary["accepted_tasks_per_1k_tokens"] == round(
            summary["accepted_tasks"] * 1000 / tokens["total"], 4
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("coordinator_tokens", -1, "nonnegative"),
        ("coordinator_tokens", True, "integer"),
        ("wait_ms", 1.5, "integer"),
        ("review_findings", "2", "integer"),
        ("rereviews", -2, "nonnegative"),
        ("human_interventions", -1, "nonnegative"),
    ],
)
def test_comparison_rejects_impossible_metric_values(
    field: str, value: object, message: str
) -> None:
    broken = _source()
    broken["observations"][0][field] = value
    with pytest.raises(ValueError, match=message):
        compare_fixture(broken)


def test_incomplete_arm_suppresses_time_and_efficiency_deltas() -> None:
    source = _source()
    for row in source["observations"]:
        if row["policy"] == "coordinator_first" and row["trial"] == "trial-29":
            row["accepted_task_ids"] = ["T001", "T002"]
            row["accepted_commit_sha"] = None
            row["accepted_commit_ms"] = None
    result = compare_fixture(source)
    candidate = result["policies"]["coordinator_first"]
    assert candidate["all_trials_complete"] is False
    assert candidate["time_to_accepted_commit_ms_mean"] is None
    assert candidate["accepted_tasks_per_1k_tokens"] is None
    deltas = result["coordinator_first_minus_task_per_agent"]
    assert deltas["time_to_accepted_commit_ms_mean"] is None
    assert deltas["accepted_tasks_per_1k_tokens"] is None


def test_zero_token_acceptance_suppresses_efficiency_and_negative_delegate_fails() -> None:
    zero = _source()
    for row in zero["observations"]:
        if row["policy"] == "coordinator_first":
            row["coordinator_tokens"] = 0
            row["delegate_tokens"] = {}
    result = compare_fixture(zero)
    assert result["policies"]["coordinator_first"]["accepted_tasks_per_1k_tokens"] is None
    assert result["coordinator_first_minus_task_per_agent"]["accepted_tasks_per_1k_tokens"] is None

    negative = _source()
    negative["observations"][0]["delegate_tokens"]["worker_a"] = -1
    with pytest.raises(ValueError, match="nonnegative"):
        compare_fixture(negative)


@pytest.mark.parametrize("field", ["seed", "execution_profile_id"])
def test_comparison_rejects_mismatched_pair_identity(field: str) -> None:
    broken = _source()
    candidate = next(
        row
        for row in broken["observations"]
        if row["policy"] == "coordinator_first" and row["trial"] == "trial-17"
    )
    candidate[field] = 999 if field == "seed" else "different-profile"
    with pytest.raises(ValueError, match=f"mismatched {field}"):
        compare_fixture(broken)


def test_comparison_rejects_unpaired_or_duplicate_trials() -> None:
    unpaired = _source()
    unpaired["observations"][-1]["trial"] = "unpaired"
    with pytest.raises(ValueError, match="paired trial ids"):
        compare_fixture(unpaired)

    duplicate = _source()
    duplicate_row = deepcopy(duplicate["observations"][0])
    duplicate_row["run_id"] = "duplicate-baseline"
    duplicate["observations"].append(duplicate_row)
    with pytest.raises(ValueError, match="each trial id only once"):
        compare_fixture(duplicate)

    duplicate_run = _source()
    duplicate_run["observations"][1]["run_id"] = duplicate_run["observations"][0]["run_id"]
    with pytest.raises(ValueError, match="run_id values must be unique"):
        compare_fixture(duplicate_run)


def test_comparison_rejects_unproven_or_invalid_acceptance() -> None:
    bad_order = _source()
    bad_order["observations"][0]["accepted_task_ids"] = ["T002", "T001", "T003", "T004"]
    with pytest.raises(ValueError, match="preserve workload order"):
        compare_fixture(bad_order)

    missing_commit = _source()
    missing_commit["observations"][0]["accepted_commit_sha"] = None
    with pytest.raises(ValueError, match="accepted_commit_sha"):
        compare_fixture(missing_commit)

    early_commit = _source()
    early_commit["observations"][0]["accepted_commit_ms"] = 100
    with pytest.raises(ValueError, match="after run_started_ms"):
        compare_fixture(early_commit)


def test_comparison_rejects_malformed_container_and_workload_identity() -> None:
    with pytest.raises(ValueError, match="fixture must be an object"):
        compare_fixture([])

    broken = _source()
    broken["observations"][0] = "not-an-object"
    with pytest.raises(ValueError, match="observation must be an object"):
        compare_fixture(broken)

    duplicate_tasks = _source()
    duplicate_tasks["workload"]["task_ids"][-1] = "T001"
    with pytest.raises(ValueError, match="task_ids must be unique"):
        compare_fixture(duplicate_tasks)
