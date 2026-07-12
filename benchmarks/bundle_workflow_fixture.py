"""Fail-closed, model-neutral coordinator policy comparison fixture."""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

DEFAULT_FIXTURE = Path(__file__).with_name("fixtures") / "bundle_workflow.json"
POLICIES = ("task_per_agent", "coordinator_first")
COUNT_FIELDS = (
    "coordinator_tokens",
    "review_findings",
    "rereviews",
    "wait_ms",
    "human_interventions",
)


def _integer(value: Any, field: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    if value < (1 if positive else 0):
        qualifier = "positive" if positive else "nonnegative"
        raise ValueError(f"{field} must be {qualifier}")
    return value


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _sha(value: Any, field: str, length: int) -> str:
    text = _string(value, field)
    if not re.fullmatch(rf"[0-9a-f]{{{length}}}", text):
        raise ValueError(f"{field} must be {length} lowercase hex characters")
    return text


def _mean(values: list[int]) -> float:
    return round(sum(values) / len(values), 4)


def _validate_workload(document: dict[str, Any]) -> tuple[list[str], int]:
    workload = document.get("workload")
    if not isinstance(workload, dict):
        raise ValueError("workload must be an object")
    _sha(workload.get("task_graph_sha256"), "workload.task_graph_sha256", 64)
    _sha(workload.get("initial_commit"), "workload.initial_commit", 40)
    commands = workload.get("acceptance_commands")
    if not isinstance(commands, list) or not commands:
        raise ValueError("workload.acceptance_commands must be a non-empty list")
    for command in commands:
        _string(command, "workload.acceptance_commands[]")
    task_ids = workload.get("task_ids")
    if not isinstance(task_ids, list) or not task_ids:
        raise ValueError("workload.task_ids must be a non-empty list")
    normalized = [_string(task_id, "workload.task_ids[]") for task_id in task_ids]
    if len(set(normalized)) != len(normalized):
        raise ValueError("workload.task_ids must be unique")
    target = _integer(
        workload.get("required_accepted_tasks"),
        "workload.required_accepted_tasks",
        positive=True,
    )
    if target != len(normalized):
        raise ValueError("required_accepted_tasks must equal the workload task count")
    return normalized, target


def _validate_observation(row: Any, *, task_ids: list[str], target: int) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ValueError("each observation must be an object")
    policy = _string(row.get("policy"), "observation.policy")
    if policy not in POLICIES:
        raise ValueError(f"unknown policy: {policy}")
    _string(row.get("trial"), "observation.trial")
    _integer(row.get("seed"), "observation.seed")
    _string(row.get("execution_profile_id"), "observation.execution_profile_id")
    _string(row.get("run_id"), "observation.run_id")
    provenance = row.get("provenance")
    if not isinstance(provenance, dict):
        raise ValueError("observation.provenance must be an object")
    _string(provenance.get("kind"), "observation.provenance.kind")
    _string(provenance.get("reference"), "observation.provenance.reference")

    for field in COUNT_FIELDS:
        _integer(row.get(field), f"observation.{field}")
    delegates = row.get("delegate_tokens")
    if not isinstance(delegates, dict):
        raise ValueError("observation.delegate_tokens must be an object")
    for actor, tokens in delegates.items():
        _string(actor, "observation.delegate_tokens actor")
        _integer(tokens, f"observation.delegate_tokens.{actor}")

    started = _integer(row.get("run_started_ms"), "observation.run_started_ms")
    accepted_ids = row.get("accepted_task_ids")
    if not isinstance(accepted_ids, list):
        raise ValueError("observation.accepted_task_ids must be a list")
    accepted = [_string(task_id, "observation.accepted_task_ids[]") for task_id in accepted_ids]
    if len(set(accepted)) != len(accepted):
        raise ValueError("observation.accepted_task_ids must be unique")
    if not set(accepted) <= set(task_ids):
        raise ValueError("observation accepted unknown task IDs")

    commit = row.get("accepted_commit_sha")
    accepted_at = row.get("accepted_commit_ms")
    complete = len(accepted) == target
    if complete:
        if accepted != task_ids:
            raise ValueError("complete accepted_task_ids must preserve workload order")
        _sha(commit, "observation.accepted_commit_sha", 40)
        completed = _integer(accepted_at, "observation.accepted_commit_ms", positive=True)
        if completed <= started:
            raise ValueError("accepted_commit_ms must be after run_started_ms")
    elif commit is not None or accepted_at is not None:
        raise ValueError("incomplete observations cannot claim an accepted commit")
    return row


def summarize_policy(observations: list[dict[str, Any]], *, target: int) -> dict[str, Any]:
    """Aggregate validated trials without ranking or choosing a winner."""
    if not observations:
        raise ValueError("each policy requires at least one observation")
    coordinator_tokens = sum(row["coordinator_tokens"] for row in observations)
    delegate_tokens: dict[str, int] = defaultdict(int)
    for row in observations:
        for actor, tokens in row["delegate_tokens"].items():
            delegate_tokens[actor] += tokens
    delegate_total = sum(delegate_tokens.values())
    total_tokens = coordinator_tokens + delegate_total
    accepted = sum(len(row["accepted_task_ids"]) for row in observations)
    all_complete = all(len(row["accepted_task_ids"]) == target for row in observations)
    times = (
        [row["accepted_commit_ms"] - row["run_started_ms"] for row in observations]
        if all_complete
        else []
    )
    comparable = all_complete and total_tokens > 0
    return {
        "trials": len(observations),
        "all_trials_complete": all_complete,
        "accepted_tasks": accepted,
        "time_to_accepted_commit_ms_mean": _mean(times) if all_complete else None,
        "tokens": {
            "coordinator": coordinator_tokens,
            "delegates": dict(sorted(delegate_tokens.items())),
            "delegates_total": delegate_total,
            "total": total_tokens,
        },
        "accepted_tasks_per_1k_tokens": (
            round(accepted * 1000 / total_tokens, 4) if comparable else None
        ),
        "review_findings_mean": _mean([row["review_findings"] for row in observations]),
        "rereviews_mean": _mean([row["rereviews"] for row in observations]),
        "wait_ms_mean": _mean([row["wait_ms"] for row in observations]),
        "human_interventions_mean": _mean([row["human_interventions"] for row in observations]),
    }


def compare_fixture(document: Any) -> dict[str, Any]:
    """Return descriptive summaries and signed deltas for rigorously paired runs."""
    if not isinstance(document, dict):
        raise ValueError("fixture must be an object")
    if document.get("schema_version") != 1:
        raise ValueError("unsupported fixture schema_version")
    task_ids, target = _validate_workload(document)
    rows = document.get("observations")
    if not isinstance(rows, list):
        raise ValueError("observations must be a list")
    validated = [_validate_observation(row, task_ids=task_ids, target=target) for row in rows]
    run_ids = [row["run_id"] for row in validated]
    if len(set(run_ids)) != len(run_ids):
        raise ValueError("observation.run_id values must be unique")
    grouped = {policy: [row for row in validated if row["policy"] == policy] for policy in POLICIES}
    pair_maps = {policy: {row["trial"]: row for row in grouped[policy]} for policy in POLICIES}
    if any(len(pair_maps[policy]) != len(grouped[policy]) for policy in POLICIES):
        raise ValueError("each policy may record each trial id only once")
    if set(pair_maps[POLICIES[0]]) != set(pair_maps[POLICIES[1]]):
        raise ValueError("both policies must record the same paired trial ids")
    for trial in pair_maps[POLICIES[0]]:
        baseline = pair_maps[POLICIES[0]][trial]
        candidate = pair_maps[POLICIES[1]][trial]
        for field in ("seed", "execution_profile_id"):
            if baseline[field] != candidate[field]:
                raise ValueError(f"paired trial {trial!r} has mismatched {field}")

    summaries = {policy: summarize_policy(grouped[policy], target=target) for policy in POLICIES}
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
    deltas = {}
    for field in delta_fields:
        left, right = baseline[field], candidate[field]
        deltas[field] = (
            round(float(right) - float(left), 4) if left is not None and right is not None else None
        )
    return {
        "schema_version": 1,
        "fixture_id": _string(document.get("fixture_id"), "fixture_id"),
        "descriptive_only": True,
        "policies": summaries,
        "coordinator_first_minus_task_per_agent": deltas,
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
