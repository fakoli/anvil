"""The four benchmark scenarios. Each is a task set + actor count + failure-injection
knobs. The SAME scenario runs through both coordination arms.
"""
from __future__ import annotations

from dataclasses import dataclass

from .engine import TaskSpec


@dataclass(frozen=True)
class Scenario:
    key: str
    title: str
    description: str
    tasks: list[TaskSpec]
    actors: int
    headline: tuple[str, ...]          # metric keys to feature in the table
    lease_minutes: float = 60.0
    gamed_fraction: float = 0.0        # share of completions submitted without real verification
    crash_actor: bool = False          # a "dead" actor reserves crash_task then vanishes
    crash_task: str = "T001"           # which task the dead actor locks (crash scenario)


def _overlapping() -> Scenario:
    # Pairs of tasks share a file: (T001,T002)->s1, (T003,T004)->s2, ...
    tasks = []
    for k in range(1, 7):  # 6 pairs -> 12 tasks, 6 shared files
        shared = f"workspace/shared_{k}.txt"
        for j in (0, 1):
            tid = f"T{2*k-1+j:03d}"
            tasks.append(TaskSpec(
                tid, f"Touch shared bucket {k}",
                files=(shared, f"workspace/own_{tid}.txt"),
                priority="high" if j == 0 else "medium",
            ))
    return Scenario(
        key="overlapping_files",
        title="Overlapping files",
        description="12 tasks in 6 pairs; each pair mutates one shared file. "
                    "Proves exclusive leasing serializes writes to a shared file.",
        tasks=tasks, actors=8,
        headline=("collisions", "duplicate_completions", "final_state_valid"),
    )


def _dependency() -> Scenario:
    # Three independent chains of 3: T..a <- T..b <- T..c (c depends on b depends on a).
    tasks = []
    n = 1
    for _chain in range(3):
        prev = None
        for _step in range(3):
            tid = f"T{n:03d}"; n += 1
            deps = (prev,) if prev else ()
            tasks.append(TaskSpec(
                tid, f"Chain step {tid}",
                files=(f"workspace/{tid}.txt",),
                deps=deps, priority="high",
            ))
            prev = tid
    return Scenario(
        key="dependency_ordering",
        title="Dependency ordering",
        description="3 chains of 3 dependent tasks. Proves the readiness gate stops a "
                    "task starting before its dependency is done.",
        tasks=tasks, actors=6,
        headline=("ordering_violations", "final_state_valid"),
    )


def _crash() -> Scenario:
    tasks = [
        TaskSpec(f"T{n:03d}", f"Unit {n}", files=(f"workspace/u{n}.txt",),
                 priority="high")
        for n in range(1, 6)
    ]
    return Scenario(
        key="crash_recovery",
        title="Crash / stale-lease recovery",
        description="A dead actor holds an exclusive lock on T001 then vanishes. Proves "
                    "the lease expires and the abandoned task is reclaimed and completed "
                    "with no duplicate work — exclusive locking that self-heals. (The "
                    "lease is fast-forwarded to exercise the reaper without a real wait; "
                    "see README engine findings.)",
        tasks=tasks, actors=4, lease_minutes=1.0, crash_actor=True, crash_task="T001",
        headline=("duplicate_completions", "recovered_after_crash", "completed_all"),
    )


def _gaming() -> Scenario:
    tasks = [
        TaskSpec(f"T{n:03d}", f"Feature {n}", files=(f"workspace/f{n}.txt",),
                 priority="medium", verification=("pytest tests/test_f.py -q",))
        for n in range(1, 9)
    ]
    return Scenario(
        key="evidence_gaming",
        title="Evidence gaming",
        description="Half of all completions are submitted without real verification. "
                    "Proves the evidence record + gate flag unverified work that a "
                    "markdown checkbox cannot.",
        tasks=tasks, actors=6, gamed_fraction=0.5,
        headline=("gamed_detected_pct", "evidence_records", "final_state_valid"),
    )


def all_scenarios() -> dict[str, Scenario]:
    return {s.key: s for s in (_overlapping(), _dependency(), _crash(), _gaming())}
