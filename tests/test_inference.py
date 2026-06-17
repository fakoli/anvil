"""Tests for fakoli_state.planning.inference — dependency and conflict-group inference.

All tests follow the pure-function contract:
- Input tasks are never mutated.
- Output tasks are new instances via model_copy.
"""

from __future__ import annotations

import datetime

from fakoli_state.planning.inference import (
    InferenceResult,
    infer_all,
    infer_conflict_groups,
    infer_dependencies,
)
from fakoli_state.state.models import Score, Task, TaskPriority, TaskStatus, Verification

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UTC = datetime.UTC
_NOW = datetime.datetime(2026, 5, 24, 18, 0, 0, tzinfo=_UTC)


def _make_task(
    task_id: str,
    likely_files: list[str],
    *,
    dependencies: list[str] | None = None,
    conflict_groups: list[str] | None = None,
) -> Task:
    return Task(
        id=task_id,
        feature_id="F001",
        title=f"Task {task_id}",
        description="A task for inference testing.",
        status=TaskStatus.proposed,
        priority=TaskPriority.medium,
        scores=Score(),
        acceptance_criteria=["Tests pass."],
        verification=Verification(commands=["pytest tests/ -v"]),
        likely_files=likely_files,
        dependencies=dependencies or [],
        conflict_groups=conflict_groups or [],
        created_at=_NOW,
        updated_at=_NOW,
    )


# ---------------------------------------------------------------------------
# infer_dependencies
# ---------------------------------------------------------------------------


class TestInferDependencies:
    def test_no_dependencies_when_no_overlap(self) -> None:
        """Tasks with completely disjoint likely_files get no deps inferred."""
        tasks = [
            _make_task("T001", ["src/api.py", "src/routes.py"]),
            _make_task("T002", ["tests/test_api.py", "tests/conftest.py"]),
        ]
        result = infer_dependencies(tasks)
        assert len(result) == 2
        # Neither task should have new dependencies
        t001 = next(t for t in result if t.id == "T001")
        t002 = next(t for t in result if t.id == "T002")
        assert t001.dependencies == []
        assert t002.dependencies == []

    def test_subset_creates_dependency_edge(self) -> None:
        """A.files ⊂ B.files → A depends on B."""
        # T001 has a strict subset of T002's files
        tasks = [
            _make_task("T001", ["src/api.py"]),  # subset of T002's files
            _make_task("T002", ["src/api.py", "src/utils.py", "src/models.py"]),
        ]
        result = infer_dependencies(tasks)
        t001 = next(t for t in result if t.id == "T001")
        # T001 should depend on T002 (T001 specialises T002)
        assert "T002" in t001.dependencies

    def test_superset_gets_no_extra_dependency(self) -> None:
        """B.files ⊃ A.files → B does NOT depend on A (A depends on B)."""
        tasks = [
            _make_task("T001", ["src/api.py"]),
            _make_task("T002", ["src/api.py", "src/utils.py"]),
        ]
        result = infer_dependencies(tasks)
        t002 = next(t for t in result if t.id == "T002")
        # T002 is the broader task, should NOT depend on T001
        assert "T001" not in t002.dependencies

    def test_empty_files_not_a_subset(self) -> None:
        """Task with empty likely_files does not create dependency edges."""
        tasks = [
            _make_task("T001", []),  # empty
            _make_task("T002", ["src/api.py", "src/utils.py"]),
        ]
        result = infer_dependencies(tasks)
        t001 = next(t for t in result if t.id == "T001")
        # empty set is mathematically a subset of everything, but parser skips it
        assert t001.dependencies == []

    def test_input_tasks_not_mutated(self) -> None:
        """infer_dependencies does not mutate input tasks."""
        original_task = _make_task("T001", ["src/api.py"])
        tasks = [
            original_task,
            _make_task("T002", ["src/api.py", "src/utils.py"]),
        ]
        _ = infer_dependencies(tasks)
        # Original task object unchanged
        assert original_task.dependencies == []

    def test_empty_list_returns_empty_list(self) -> None:
        """infer_dependencies on empty list returns empty list."""
        assert infer_dependencies([]) == []

    def test_single_task_returns_unchanged(self) -> None:
        """Single task has nothing to be a subset of — returns unchanged."""
        tasks = [_make_task("T001", ["src/api.py"])]
        result = infer_dependencies(tasks)
        assert len(result) == 1
        assert result[0].dependencies == []


# ---------------------------------------------------------------------------
# infer_conflict_groups
# ---------------------------------------------------------------------------


class TestInferConflictGroups:
    def test_partial_overlap_creates_conflict_group(self) -> None:
        """A ∩ B nonempty but neither subset → both in a ConflictGroup."""
        tasks = [
            _make_task("T001", ["src/api.py", "src/models.py"]),
            _make_task("T002", ["src/api.py", "src/routes.py"]),
        ]
        _, groups = infer_conflict_groups(tasks)
        assert len(groups) == 1
        cg = groups[0]
        assert "T001" in cg.task_ids
        assert "T002" in cg.task_ids

    def test_no_conflict_group_for_disjoint_tasks(self) -> None:
        """Tasks with no file overlap produce no conflict groups."""
        tasks = [
            _make_task("T001", ["src/api.py"]),
            _make_task("T002", ["tests/test_utils.py"]),
        ]
        _, groups = infer_conflict_groups(tasks)
        assert groups == []

    def test_conflict_group_naming_deterministic(self) -> None:
        """Sorted task IDs in group name → same group regardless of input order."""
        tasks_ab = [
            _make_task("T001", ["src/api.py", "src/models.py"]),
            _make_task("T002", ["src/api.py", "src/routes.py"]),
        ]
        tasks_ba = [
            _make_task("T002", ["src/api.py", "src/routes.py"]),
            _make_task("T001", ["src/api.py", "src/models.py"]),
        ]
        _, groups_ab = infer_conflict_groups(tasks_ab)
        _, groups_ba = infer_conflict_groups(tasks_ba)
        assert len(groups_ab) == 1
        assert len(groups_ba) == 1
        # Both should have the same group ID (sorted IDs)
        assert groups_ab[0].id == groups_ba[0].id
        # ID follows "CG-T001-T002" pattern (sorted)
        assert groups_ab[0].id == "CG-T001-T002"

    def test_strict_subset_not_a_conflict(self) -> None:
        """A ⊂ B → dependency edge, not a conflict group."""
        tasks = [
            _make_task("T001", ["src/api.py"]),  # strict subset of T002
            _make_task("T002", ["src/api.py", "src/utils.py"]),
        ]
        _, groups = infer_conflict_groups(tasks)
        # Strict subset → dependency, not conflict
        assert groups == []

    def test_empty_file_task_not_in_conflict(self) -> None:
        """Task with empty likely_files is never placed in a conflict group."""
        tasks = [
            _make_task("T001", []),  # empty
            _make_task("T002", ["src/api.py"]),
        ]
        _, groups = infer_conflict_groups(tasks)
        assert groups == []

    def test_conflict_group_task_ids_in_tasks_field(self) -> None:
        """Tasks in a conflict group have the group ID in their conflict_groups field."""
        tasks = [
            _make_task("T001", ["src/api.py", "src/models.py"]),
            _make_task("T002", ["src/api.py", "src/routes.py"]),
        ]
        result_tasks, groups = infer_conflict_groups(tasks)
        assert len(groups) == 1
        cg_id = groups[0].id
        t001 = next(t for t in result_tasks if t.id == "T001")
        t002 = next(t for t in result_tasks if t.id == "T002")
        assert cg_id in t001.conflict_groups
        assert cg_id in t002.conflict_groups

    def test_input_tasks_not_mutated_conflict(self) -> None:
        """infer_conflict_groups does not mutate input tasks."""
        original = _make_task("T001", ["src/api.py", "src/models.py"])
        tasks = [original, _make_task("T002", ["src/api.py", "src/routes.py"])]
        _ = infer_conflict_groups(tasks)
        assert original.conflict_groups == []

    def test_empty_list_returns_empty_tuple(self) -> None:
        """infer_conflict_groups on empty list returns ([], [])."""
        result_tasks, groups = infer_conflict_groups([])
        assert result_tasks == []
        assert groups == []


# ---------------------------------------------------------------------------
# infer_all
# ---------------------------------------------------------------------------


class TestInferAll:
    def test_infer_all_composes_correctly(self) -> None:
        """infer_all: dependencies first, conflicts second, no double-flagging.

        Setup:
        - T001: files [A, B]
        - T002: files [A, B, C]  (T001 ⊂ T002 → T001 depends on T002; no conflict)
        - T003: files [A, D]     (overlaps T001 and T002 partially → conflicts)
        """
        tasks = [
            _make_task("T001", ["a.py", "b.py"]),
            _make_task("T002", ["a.py", "b.py", "c.py"]),
            _make_task("T003", ["a.py", "d.py"]),
        ]
        result = infer_all(tasks)
        assert isinstance(result, InferenceResult)
        assert len(result.tasks) == 3

        t001 = next(t for t in result.tasks if t.id == "T001")

        # T001 ⊂ T002 → T001 depends on T002
        assert "T002" in t001.dependencies

        # T003 partially overlaps T001 → conflict
        # (T003 has [a.py, d.py]; T001 has [a.py, b.py]; partial overlap)
        # T003 partially overlaps T002 → conflict
        # (T003 has [a.py, d.py]; T002 has [a.py, b.py, c.py]; partial overlap)
        assert len(result.conflict_groups) >= 1

        # T001 and T002 should NOT be in the same conflict group (they are subset/superset)
        t001_t002_pair = {"T001", "T002"}
        for cg in result.conflict_groups:
            assert set(cg.task_ids) != t001_t002_pair, (
                "T001 and T002 should not be in a conflict group (they have a subset relationship)"
            )

    def test_infer_all_returns_inference_result(self) -> None:
        """infer_all returns InferenceResult with tasks and conflict_groups fields."""
        tasks = [
            _make_task("T001", ["src/api.py"]),
            _make_task("T002", ["src/api.py", "src/utils.py"]),
        ]
        result = infer_all(tasks)
        assert hasattr(result, "tasks")
        assert hasattr(result, "conflict_groups")

    def test_infer_all_empty_list(self) -> None:
        """infer_all on empty list returns empty InferenceResult."""
        result = infer_all([])
        assert result.tasks == []
        assert result.conflict_groups == []

    def test_infer_all_preserves_task_count(self) -> None:
        """infer_all always returns the same number of tasks as input."""
        tasks = [
            _make_task("T001", ["a.py"]),
            _make_task("T002", ["b.py"]),
            _make_task("T003", ["a.py", "b.py"]),
        ]
        result = infer_all(tasks)
        assert len(result.tasks) == 3

    def test_infer_all_no_side_effects(self) -> None:
        """infer_all does not mutate the original task list."""
        original_t1 = _make_task("T001", ["a.py"])
        tasks = [original_t1, _make_task("T002", ["a.py", "b.py"])]
        _ = infer_all(tasks)
        # Original task remains unchanged
        assert original_t1.dependencies == []
        assert original_t1.conflict_groups == []
