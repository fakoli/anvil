"""Deterministic, model-neutral coordinator policy comparison fixture."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

DEFAULT_FIXTURE = Path(__file__).with_name("fixtures") / "bundle_workflow.json"
POLICIES = ("task_per_agent", "coordinator_first")


def _mean(values: list[int]) -> float:
    return round(sum(values) / len(values), 4)


def summarize_policy(observations: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate recorded trials without ranking or choosing a winner."""
    if not observations:
        raise ValueError("each policy requires at least one observation")
    coordinator_tokens = sum(int(row["coordinator_tokens"]) for row in observations)
    delegate_tokens: dict[str, int] = defaultdict(int)
    for row in observations:
        for actor, tokens in row["delegate_tokens"].items():
            delegate_tokens[str(actor)] += int(tokens)
    delegate_total = sum(delegate_tokens.values())
    accepted = sum(int(row["accepted_tasks"]) for row in observations)
    total_tokens = coordinator_tokens + delegate_total
    if total_tokens <= 0:
        raise ValueError("token total must be positive")
    return {
        "trials": len(observations),
        "accepted_tasks": accepted,
        "time_to_accepted_commit_ms_mean": _mean(
            [int(row["time_to_accepted_commit_ms"]) for row in observations]
        ),
        "tokens": {
            "coordinator": coordinator_tokens,
            "delegates": dict(sorted(delegate_tokens.items())),
            "delegates_total": delegate_total,
            "total": total_tokens,
        },
        "accepted_tasks_per_1k_tokens": round(accepted * 1000 / total_tokens, 4),
        "review_findings_mean": _mean(
            [int(row["review_findings"]) for row in observations]
        ),
        "rereviews_mean": _mean([int(row["rereviews"]) for row in observations]),
        "wait_ms_mean": _mean([int(row["wait_ms"]) for row in observations]),
        "human_interventions_mean": _mean(
            [int(row["human_interventions"]) for row in observations]
        ),
    }


def compare_fixture(document: dict[str, Any]) -> dict[str, Any]:
    """Return descriptive summaries and signed deltas for the two policies."""
    if document.get("schema_version") != 1:
        raise ValueError("unsupported fixture schema_version")
    rows = document.get("observations")
    if not isinstance(rows, list):
        raise ValueError("observations must be a list")
    unknown = {row.get("policy") for row in rows} - set(POLICIES)
    if unknown:
        raise ValueError(f"unknown policies: {sorted(unknown)}")
    grouped = {
        policy: [row for row in rows if row.get("policy") == policy]
        for policy in POLICIES
    }
    trial_sets = [{str(row["trial"]) for row in grouped[policy]} for policy in POLICIES]
    if trial_sets[0] != trial_sets[1]:
        raise ValueError("both policies must record the same paired trial ids")
    has_duplicate_trial = any(
        len(grouped[policy]) != len(trial_sets[index])
        for index, policy in enumerate(POLICIES)
    )
    if has_duplicate_trial:
        raise ValueError("each policy may record each trial id only once")
    target = len(document["workload"]["task_ids"])
    if any(int(row["accepted_tasks"]) > target for row in rows):
        raise ValueError("accepted_tasks exceeds the shared workload target")
    summaries = {policy: summarize_policy(grouped[policy]) for policy in POLICIES}
    baseline = summaries[POLICIES[0]]
    candidate = summaries[POLICIES[1]]
    delta_fields = (
        "time_to_accepted_commit_ms_mean",
        "accepted_tasks_per_1k_tokens",
        "review_findings_mean",
        "rereviews_mean",
        "wait_ms_mean",
        "human_interventions_mean",
    )
    return {
        "schema_version": 1,
        "fixture_id": document["fixture_id"],
        "descriptive_only": True,
        "policies": summaries,
        "coordinator_first_minus_task_per_agent": {
            field: round(float(candidate[field]) - float(baseline[field]), 4)
            for field in delta_fields
        },
    }


def load_and_compare(path: Path = DEFAULT_FIXTURE) -> dict[str, Any]:
    return compare_fixture(json.loads(path.read_text(encoding="utf-8")))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    args = parser.parse_args()
    print(json.dumps(load_and_compare(args.fixture), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
