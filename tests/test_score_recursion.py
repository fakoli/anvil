"""Tests for score recursion + parent roll-up (v1.23.0).

Closes the claude-task-master D5 lessons:
  - parent roll-up: an expanded parent is a container, excluded from the
    actionable expansion queue (TM #250)
  - recursive expand-to-threshold: a depth-capped, cycle-guarded frontier
  - partial-score merge: re-scoring a subset must not wipe other tasks'
    scores (TM #1644) — proven against the event-sourced backend
"""

from __future__ import annotations

import datetime

from fakoli_state.planning.scoring import (
    DEFAULT_RECURSION_DEPTH_CAP,
    _depth_of,
    build_expansion_queue,
    build_recursive_expansion_queue,
    is_expanded,
)
from fakoli_state.state.models import (
    Score,
    Task,
    TaskPriority,
    TaskStatus,
    Verification,
)

_NOW = datetime.datetime(2026, 5, 24, 18, 0, 0, tzinfo=datetime.UTC)


def _task(task_id: str, complexity: int | None, parent: str | None = None) -> Task:
    return Task(
        id=task_id,
        feature_id="F001",
        title=f"Task {task_id}",
        description="d",
        status=TaskStatus.proposed,
        priority=TaskPriority.medium,
        scores=Score(complexity=complexity),
        acceptance_criteria=[],
        verification=Verification(commands=["pytest"]),
        likely_files=[],
        dependencies=[],
        conflict_groups=[],
        parent_task_id=parent,
        created_at=_NOW,
        updated_at=_NOW,
    )


class TestParentRollup:
    def test_is_expanded_true_when_children_exist(self):
        tasks = [_task("P", 5), _task("C1", 2, "P")]
        assert is_expanded(tasks[0], tasks) is True
        assert is_expanded(tasks[1], tasks) is False

    def test_expanded_parent_excluded_from_queue(self):
        """TM #250: an over-threshold parent that was split is not re-queued."""
        tasks = [_task("P", 5), _task("C1", 4, "P"), _task("C2", 2, "P")]
        queue_ids = [c.task_id for c in build_expansion_queue(tasks)]
        assert "P" not in queue_ids  # container, rolled up
        assert queue_ids == ["C1"]  # only the over-threshold leaf

    def test_flat_taskset_unchanged(self):
        """Backward compat: with no parent links nothing is excluded."""
        tasks = [_task("T1", 5), _task("T2", 4), _task("T3", 3)]
        assert [c.task_id for c in build_expansion_queue(tasks)] == ["T1", "T2"]


class TestRecursiveFrontier:
    def test_frontier_reports_depth_and_excludes_containers(self):
        # P(5) → C1(4 leaf), C2(2 leaf); plus a top-level T9(5).
        tasks = [_task("P", 5), _task("C1", 4, "P"), _task("C2", 2, "P"), _task("T9", 5)]
        frontier = build_recursive_expansion_queue(tasks)
        # P excluded (container); C2 below threshold; T9 depth 0, C1 depth 1.
        assert [(c.task_id, c.depth) for c in frontier] == [("T9", 0), ("C1", 1)]

    def test_depth_cap_drops_too_deep_leaves(self):
        # A lineage deeper than the cap: root → a → b → c → d (all over threshold).
        chain = [_task("root", 5)]
        prev = "root"
        for name in ("a", "b", "c", "d"):
            chain.append(_task(name, 5, prev))
            prev = name
        frontier = build_recursive_expansion_queue(chain, depth_cap=2)
        # 'd' is the only leaf; walking its parents reaches depth 4. With cap 2,
        # _depth_of's runaway guard (`depth > depth_cap + 1` → 4 > 3) returns
        # None, so 'd' is dropped by the `depth is None` branch — i.e. anything
        # past the cap is excluded from the auto-queue.
        assert frontier == []

    def test_leaf_at_exactly_cap_is_kept(self):
        chain = [_task("root", 5), _task("a", 5, "root"), _task("b", 5, "a")]
        frontier = build_recursive_expansion_queue(chain, depth_cap=2)
        assert [c.task_id for c in frontier] == ["b"]
        assert frontier[0].depth == 2

    def test_default_depth_cap_is_three(self):
        assert DEFAULT_RECURSION_DEPTH_CAP == 3

    def test_terminates_on_cyclic_parents_via_public_api(self):
        """Public-API proof of termination: a parent cycle must not hang.

        A ↔ B both point at each other, so both are containers and roll up — the
        observable result is an empty frontier and, critically, the call
        returns at all. (The cycle guard inside ``_depth_of`` is not reachable
        through this path precisely because the container check excludes both
        nodes first; ``TestDepthGuards`` covers that internal guard directly.)
        """
        cyclic = [_task("A", 5, "B"), _task("B", 5, "A")]
        assert build_recursive_expansion_queue(cyclic) == []


class TestDepthGuards:
    """Direct unit tests for the _depth_of termination guards.

    These exercise _depth_of by name on purpose: the cycle/runaway guards are
    defense-in-depth that the public queue builders cannot reach (a node in a
    cycle always has a child, so the container check excludes it before
    _depth_of is called). A direct test is the only way to verify the
    termination invariant the guards provide.
    """

    def test_top_level_task_depth_zero(self):
        t = _task("T", 5)
        assert _depth_of(t, {"T": t}, 3) == 0

    def test_cycle_returns_none_not_infinite_loop(self):
        # A ↔ B mutual parent cycle must not hang.
        a = _task("A", 5, "B")
        b = _task("B", 5, "A")
        by_id = {"A": a, "B": b}
        assert _depth_of(a, by_id, 3) is None
        assert _depth_of(b, by_id, 3) is None

    def test_dangling_parent_terminates_at_current_depth(self):
        # child points at a parent not present → chain rooted where data stops.
        child = _task("C", 5, "MISSING")
        assert _depth_of(child, {"C": child}, 3) == 0
