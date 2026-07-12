"""Contract tests for the model-neutral coordinator policy benchmark."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from benchmarks.bundle_workflow_fixture import load_and_compare

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "benchmarks" / "fixtures" / "bundle_workflow.json"


def test_fixture_records_required_metrics_for_both_policies() -> None:
    source = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert {row["policy"] for row in source["observations"]} == {
        "task_per_agent",
        "coordinator_first",
    }
    required = {
        "time_to_accepted_commit_ms",
        "coordinator_tokens",
        "delegate_tokens",
        "accepted_tasks",
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


def test_accepted_task_efficiency_uses_all_coordinator_and_delegate_tokens() -> None:
    result = load_and_compare(FIXTURE)
    for summary in result["policies"].values():
        tokens = summary["tokens"]
        assert tokens["total"] == tokens["coordinator"] + tokens["delegates_total"]
        assert summary["accepted_tasks_per_1k_tokens"] == round(
            summary["accepted_tasks"] * 1000 / tokens["total"], 4
        )


def test_comparison_rejects_unpaired_trials() -> None:
    source = json.loads(FIXTURE.read_text(encoding="utf-8"))
    broken = deepcopy(source)
    broken["observations"][-1]["trial"] = "unpaired"
    from benchmarks.bundle_workflow_fixture import compare_fixture

    try:
        compare_fixture(broken)
    except ValueError as exc:
        assert "paired trial ids" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("unpaired benchmark observations were accepted")
