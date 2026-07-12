"""Pure execution-bundle graph eligibility shared by every claim surface."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BundleGraphAnalysis:
    """Critical-path depth plus a deterministic cycle witness, if any."""

    critical_path_depth: int
    dependency_cycle: tuple[str, ...] = ()
    depth_by_task: dict[str, int] | None = None


def analyze_bundle_graph(
    member_ids: list[str], dependencies: dict[str, list[str]]
) -> BundleGraphAnalysis:
    """Analyze only member-to-member edges and fail closed on a cycle."""
    members = set(member_ids)
    memo: dict[str, int] = {}
    visiting: list[str] = []

    def depth(task_id: str) -> int:
        if task_id in memo:
            return memo[task_id]
        if task_id in visiting:
            start = visiting.index(task_id)
            cycle = tuple(visiting[start:] + [task_id])
            raise _CycleFound(cycle)
        visiting.append(task_id)
        value = 1 + max(
            (
                depth(dependency)
                for dependency in dependencies.get(task_id, [])
                if dependency in members
            ),
            default=0,
        )
        visiting.pop()
        memo[task_id] = value
        return value

    try:
        critical_path_depth = max((depth(task_id) for task_id in member_ids), default=0)
    except _CycleFound as exc:
        return BundleGraphAnalysis(
            critical_path_depth=0,
            dependency_cycle=exc.cycle,
            depth_by_task={},
        )
    return BundleGraphAnalysis(
        critical_path_depth=critical_path_depth,
        depth_by_task=dict(memo),
    )


class _CycleFound(Exception):
    def __init__(self, cycle: tuple[str, ...]) -> None:
        self.cycle = cycle
        super().__init__(" -> ".join(cycle))
