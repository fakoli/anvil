"""The oracle: compute metrics from the instrumentation log + recorded completions +
canonical task status. Pure functions over recorded facts — no opinions.
"""
from __future__ import annotations

import math
from collections import defaultdict


def _has_overlap(intervals: list[tuple[str, float, float]]) -> bool:
    """True if two intervals from DIFFERENT actors overlap in time."""
    ordered = sorted(intervals, key=lambda iv: iv[1])
    for i in range(len(ordered)):
        ai, _s_i, e_i = ordered[i]
        for j in range(i + 1, len(ordered)):
            aj, s_j, _e_j = ordered[j]
            if s_j >= e_i:
                break  # sorted by start; no later interval can overlap interval i
            if aj != ai:
                return True
    return False


def compute(scenario, rows, completions, statuses, coord_name, crash_recovered=None):
    """Return a flat metric dict for one trial.

    rows: list[(ts, actor, task, kind, target)] from the WorkLog
    completions: list[{task, actor, gamed, completed, evidence_valid, failure}]
        (evidence_valid None means no evidence concept)
    statuses: task_id -> canonical status (fakoli) or {} (markdown)
    """
    # write rows carry an interval "t0:t1" in the extra column
    writes = []  # (actor, task, target, t0, t1)
    for _ts, a, t, k, tgt, extra in rows:
        if k != "write":
            continue
        try:
            t0, t1 = (float(x) for x in extra.split(":"))
        except (ValueError, AttributeError):
            t0 = t1 = _ts
        writes.append((a, t, tgt, t0, t1))
    dones = [(ts, a, t) for ts, a, t, k, _tgt, _e in rows if k == "done"]

    # collisions: a file with two writes from DIFFERENT actors whose intervals OVERLAP
    # in time (a real read-modify-write race). Sequential writes by different actors
    # (correctly serialized by a lease) are not collisions.
    by_file = defaultdict(list)
    for actor, _t, tgt, t0, t1 in writes:
        by_file[tgt].append((actor, t0, t1))
    collisions = 0
    for intervals in by_file.values():
        if _has_overlap(intervals):
            collisions += 1

    # duplicate completions: a task whose work was performed by >1 distinct actor
    by_task_actors = defaultdict(set)
    for _ts, actor, task in dones:
        by_task_actors[task].add(actor)
    duplicate_completions = sum(1 for a in by_task_actors.values() if len(a) > 1)

    # ordering violations: a dependent task whose first write preceded a dep's completion
    done_time, first_write = {}, {}
    for ts, _a, task in dones:
        done_time[task] = max(done_time.get(task, 0.0), ts)
    for _a, task, _tgt, t0, _t1 in writes:
        first_write[task] = min(first_write.get(task, math.inf), t0)
    ordering_violations = 0
    for t in scenario.tasks:
        if t.deps and t.id in first_write:
            if any(dep in done_time and first_write[t.id] < done_time[dep]
                   for dep in t.deps):
                ordering_violations += 1

    # completion
    total = len(scenario.tasks)
    expected_task_ids = {task.id for task in scenario.tasks}
    if coord_name == "anvil":
        observed_successful_task_ids = {
            completion["task"]
            for completion in completions
            if (
                completion["completed"] is True
                and completion["evidence_valid"] is not None
                and completion["failure"] is None
            )
        }
        completion_observations_valid = int(
            observed_successful_task_ids == expected_task_ids
        )
        completed_task_ids = {
            task_id
            for task_id, status in statuses.items()
            if status in {"done", "accepted"}
        }
        completed_all = int(
            set(statuses) == expected_task_ids
            and completed_task_ids == expected_task_ids
            and completion_observations_valid == 1
        )
    else:
        completed = len({t for _ts, _a, t in dones})
        completed_all = int(completed == total)
        completion_observations_valid = 1

    # evidence
    evidence_records = sum(1 for c in completions if c["evidence_valid"] is not None)
    gamed = [c for c in completions if c["gamed"]]
    detected = sum(1 for c in gamed if c["evidence_valid"] is False)
    gamed_detected_pct = round(100.0 * detected / len(gamed), 1) if gamed else 0.0
    invalid_honest_evidence = sum(
        1
        for completion in completions
        if (
            not completion["gamed"]
            and completion["completed"]
            and completion["evidence_valid"] is not True
        )
    )
    completion_failure_details = [
        c["failure"].diagnostic for c in completions if c["failure"] is not None
    ]

    final_state_valid = int(
        collisions == 0 and duplicate_completions == 0
        and ordering_violations == 0 and completed_all == 1
    )

    m = {
        "collisions": collisions,
        "duplicate_completions": duplicate_completions,
        "ordering_violations": ordering_violations,
        "completed_all": completed_all,
        "completion_observations_valid": completion_observations_valid,
        "evidence_records": evidence_records,
        "gamed_detected_pct": gamed_detected_pct,
        "invalid_honest_evidence": invalid_honest_evidence,
        "final_state_valid": final_state_valid,
        "completion_failures": len(completion_failure_details),
        # Detail is deliberately excluded from aggregate arithmetic below. It remains
        # attached to the individual trial, which is the unit the validity gate checks.
        "_completion_failure_details": completion_failure_details,
    }
    if crash_recovered is not None:
        m["recovered_after_crash"] = int(crash_recovered)
    return m


# Lower-is-better for these; higher-is-better for the rest.
LOWER_BETTER = {
    "collisions", "duplicate_completions", "ordering_violations",
}
PCT = {"gamed_detected_pct"}
BOOLISH = {
    "completed_all",
    "completion_observations_valid",
    "final_state_valid",
    "recovered_after_crash",
}


def aggregate(trial_metrics: list[dict]) -> dict:
    """Mean each metric across trials (keeps the headline number stable under the
    inherent nondeterminism of real concurrency)."""
    keys = (
        {key for metrics in trial_metrics for key in metrics if not key.startswith("_")}
        if trial_metrics else set()
    )
    out = {}
    for k in keys:
        vals = [m[k] for m in trial_metrics if k in m]
        out[k] = round(sum(vals) / len(vals), 2) if vals else 0.0
    return out
