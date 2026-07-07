"""Tests for anvil.planning.scoring — rule-based six-dimension scoring engine.

Coverage targets:
- score_task returns Score (pure function, no mutation)
- score_all uses model_copy (returns new Task instances)
- Each of the 6 scoring dimensions with at least boundary/representative cases
- score_task explanation field is non-empty
"""

from __future__ import annotations

import datetime

from anvil.planning.scoring import (
    rank_assumptions,
    review_tier,
    score_all,
    score_requirement_assumption,
    score_task,
)
from anvil.state.models import (
    Requirement,
    Score,
    Task,
    TaskPriority,
    TaskStatus,
    Verification,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UTC = datetime.UTC
_NOW = datetime.datetime(2026, 5, 24, 18, 0, 0, tzinfo=_UTC)


def _make_task(
    *,
    task_id: str = "T001",
    feature_id: str = "F001",
    title: str = "Test Task",
    description: str = "A test task description.",
    likely_files: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
    dependencies: list[str] | None = None,
    conflict_groups: list[str] | None = None,
) -> Task:
    """Factory for Task models suitable for scoring tests."""
    return Task(
        id=task_id,
        feature_id=feature_id,
        title=title,
        description=description,
        status=TaskStatus.proposed,
        priority=TaskPriority.medium,
        scores=Score(),
        acceptance_criteria=acceptance_criteria or [],
        verification=Verification(commands=["pytest tests/ -v"]),
        likely_files=likely_files or [],
        dependencies=dependencies or [],
        conflict_groups=conflict_groups or [],
        created_at=_NOW,
        updated_at=_NOW,
    )


# ---------------------------------------------------------------------------
# Pure function contracts
# ---------------------------------------------------------------------------


class TestPureFunctionContracts:
    def test_score_task_pure(self) -> None:
        """score_task returns a Score without mutating the input Task."""
        task = _make_task(likely_files=["src/app.py"])

        result = score_task(task)

        assert isinstance(result, Score)
        # Original task's scores remain unchanged (all None dimensions)
        assert task.scores.complexity is None
        assert task.scores.parallelizability is None

        # Returned Score has all dimensions populated
        assert result.complexity is not None
        assert result.parallelizability is not None
        assert result.context_load is not None
        assert result.blast_radius is not None
        assert result.review_risk is not None
        assert result.agent_suitability is not None

    def test_score_all_returns_new_task_instances(self) -> None:
        """score_all uses model_copy — returns new Task instances, originals unchanged."""
        tasks = [
            _make_task(task_id="T001", likely_files=["src/app.py"]),
            _make_task(task_id="T002", likely_files=["src/utils.py"]),
        ]
        originals = list(tasks)

        scored = score_all(tasks)

        assert len(scored) == 2
        for i, (orig, new_task) in enumerate(zip(originals, scored, strict=True)):
            # Must be different objects (model_copy creates new instance)
            assert new_task is not orig, f"Task {i}: score_all mutated in place"
            # Original task scores remain None
            assert orig.scores.complexity is None
            # New task scores are populated
            assert new_task.scores.complexity is not None

    def test_score_all_empty_list(self) -> None:
        """score_all on empty list returns empty list."""
        assert score_all([]) == []


# ---------------------------------------------------------------------------
# Complexity dimension
# ---------------------------------------------------------------------------


class TestComplexityDimension:
    def test_complexity_zero_files_base_score(self) -> None:
        """Task with 0 likely_files gets base complexity of 2."""
        task = _make_task(likely_files=[], description="A short description.")
        score = score_task(task)
        # 0 files → base 2 (no file-count penalty)
        assert score.complexity == 2

    def test_complexity_scales_with_likely_files_five(self) -> None:
        """Task with 5 likely_files gets complexity of at least 4."""
        task = _make_task(
            likely_files=[f"src/module{i}.py" for i in range(5)],
            description="A description.",
        )
        score = score_task(task)
        assert score.complexity >= 4

    def test_complexity_scales_with_likely_files_ten(self) -> None:
        """Task with 10 likely_files gets complexity of 4 or 5."""
        task = _make_task(
            likely_files=[f"src/module{i}.py" for i in range(10)],
            description="A short description.",
        )
        score = score_task(task)
        assert score.complexity >= 4

    def test_complexity_keywords_boost(self) -> None:
        """Task with 'refactor' in description gets +1 boost over base."""
        task_without_keyword = _make_task(
            likely_files=["src/app.py"],
            description="Add a new feature to handle requests.",
        )
        task_with_keyword = _make_task(
            likely_files=["src/app.py"],
            description="Refactor the request handling module.",
        )
        score_without = score_task(task_without_keyword)
        score_with = score_task(task_with_keyword)
        # 'refactor' should boost complexity by at least 1
        assert score_with.complexity >= score_without.complexity + 1

    def test_complexity_clamped_to_five(self) -> None:
        """Complexity never exceeds 5 even with all penalty flags."""
        task = _make_task(
            likely_files=[f"src/module{i}.py" for i in range(15)],
            description="Refactor migrate architecture redesign " * 10,  # many keywords
        )
        score = score_task(task)
        assert score.complexity <= 5


# ---------------------------------------------------------------------------
# Parallelizability dimension
# ---------------------------------------------------------------------------


class TestParallelizabilityDimension:
    def test_parallelizability_no_deps_high_score(self) -> None:
        """Task with no dependencies gets parallelizability of 4."""
        task = _make_task(dependencies=[], conflict_groups=[])
        score = score_task(task)
        assert score.parallelizability == 4

    def test_parallelizability_low_with_many_deps(self) -> None:
        """Task with 3+ dependencies gets parallelizability of 2."""
        task = _make_task(
            dependencies=["T002", "T003", "T004"],
            conflict_groups=[],
        )
        score = score_task(task)
        assert score.parallelizability == 2

    def test_parallelizability_drops_in_conflict_group(self) -> None:
        """Task in 2+ conflict groups gets parallelizability of 1."""
        task = _make_task(
            dependencies=[],
            conflict_groups=["CG-T001-T002", "CG-T001-T003"],
        )
        score = score_task(task)
        assert score.parallelizability == 1

    def test_parallelizability_medium_with_few_deps(self) -> None:
        """Task with 1-2 dependencies gets parallelizability of 3."""
        task = _make_task(
            dependencies=["T002"],
            conflict_groups=[],
        )
        score = score_task(task)
        assert score.parallelizability == 3


# ---------------------------------------------------------------------------
# Context load dimension
# ---------------------------------------------------------------------------


class TestContextLoadDimension:
    def test_context_load_zero_files_worst_case(self) -> None:
        """Empty likely_files → context_load = 5 (agent must discover)."""
        task = _make_task(likely_files=[])
        score = score_task(task)
        assert score.context_load == 5

    def test_context_load_single_file_low(self) -> None:
        """Task with 1 file gets context_load = 2."""
        task = _make_task(likely_files=["src/app.py"])
        score = score_task(task)
        assert score.context_load == 2

    def test_context_load_multi_dir_high(self) -> None:
        """Files spanning multiple directories → context_load = 4."""
        task = _make_task(
            likely_files=["src/api/routes.py", "tests/test_routes.py", "config/settings.py"]
        )
        score = score_task(task)
        assert score.context_load == 4

    def test_context_load_single_dir_medium(self) -> None:
        """Multiple files in one directory → context_load = 3."""
        task = _make_task(
            likely_files=["src/app/module_a.py", "src/app/module_b.py", "src/app/module_c.py"]
        )
        score = score_task(task)
        assert score.context_load == 3


# ---------------------------------------------------------------------------
# Blast radius dimension
# ---------------------------------------------------------------------------


class TestBlastRadiusDimension:
    def test_blast_radius_high_for_schema_files(self) -> None:
        """likely_files containing 'schema' → blast_radius = 5."""
        task = _make_task(likely_files=["bin/src/anvil/state/schema.py"])
        score = score_task(task)
        assert score.blast_radius == 5

    def test_blast_radius_high_for_migration_files(self) -> None:
        """likely_files containing 'migration' → blast_radius = 5."""
        task = _make_task(likely_files=["db/migration_001.py"])
        score = score_task(task)
        assert score.blast_radius == 5

    def test_blast_radius_high_for_config_files(self) -> None:
        """likely_files containing 'config.' → blast_radius = 5."""
        task = _make_task(likely_files=["src/app/config.py"])
        score = score_task(task)
        assert score.blast_radius == 5

    def test_blast_radius_base_for_plain_file(self) -> None:
        """Task with non-sensitive file gets base blast_radius of 2."""
        task = _make_task(likely_files=["src/utils/helpers.py"])
        score = score_task(task)
        # base 2 + possibly +1 for src/ path
        assert score.blast_radius >= 2
        assert score.blast_radius <= 4  # not 5 unless sensitive


# ---------------------------------------------------------------------------
# Review risk dimension
# ---------------------------------------------------------------------------


class TestReviewRiskDimension:
    def test_review_risk_high_for_security_mentions(self) -> None:
        """Acceptance criteria mentioning 'security' → review_risk = 5."""
        task = _make_task(
            acceptance_criteria=["All security checks pass.", "Authentication is validated."]
        )
        score = score_task(task)
        assert score.review_risk == 5

    def test_review_risk_high_for_auth_mentions(self) -> None:
        """Acceptance criteria with 'auth' → review_risk = 5."""
        task = _make_task(
            acceptance_criteria=["auth token is validated before access"]
        )
        score = score_task(task)
        assert score.review_risk == 5

    def test_review_risk_base_no_sensitive_criteria(self) -> None:
        """Task without security/auth criteria gets base review_risk of 2."""
        task = _make_task(
            acceptance_criteria=["Unit tests pass.", "Integration tests pass."]
        )
        score = score_task(task)
        assert score.review_risk == 2


# ---------------------------------------------------------------------------
# Agent suitability dimension
# ---------------------------------------------------------------------------


class TestAgentSuitabilityDimension:
    def test_agent_suitability_inverse_complexity(self) -> None:
        """agent_suitability = 6 - complexity (uncapped)."""
        # Low complexity (likely 2) → agent suitability 4
        task_simple = _make_task(
            likely_files=["src/utils.py"],
            description="A simple utility function.",
        )
        score = score_task(task_simple)
        assert score.agent_suitability == 6 - score.complexity

    def test_agent_suitability_capped_for_high_blast_radius(self) -> None:
        """When blast_radius >= 4, agent_suitability is capped at 2."""
        task = _make_task(likely_files=["src/schema.py"])  # schema → blast_radius = 5
        score = score_task(task)
        assert score.blast_radius >= 4
        assert score.agent_suitability <= 2

    def test_agent_suitability_clamped_to_one(self) -> None:
        """agent_suitability never goes below 1."""
        # High complexity, high blast radius → should be clamped at 1 or 2
        task = _make_task(
            likely_files=["schema.py"] + [f"src/module{i}.py" for i in range(10)],
            description="Refactor the entire schema migration pipeline.",
        )
        score = score_task(task)
        assert score.agent_suitability >= 1


# ---------------------------------------------------------------------------
# Score explanation
# ---------------------------------------------------------------------------


class TestScoreExplanation:
    def test_explanation_populated(self) -> None:
        """Score.explanation is non-empty after score_task."""
        task = _make_task(likely_files=["src/app.py"])
        score = score_task(task)
        assert score.explanation is not None
        assert len(score.explanation) > 0

    def test_explanation_contains_all_dimension_names(self) -> None:
        """Explanation references all 6 dimension names."""
        task = _make_task(likely_files=["src/app.py", "tests/test_app.py"])
        score = score_task(task)
        assert score.explanation is not None
        for dim_name in [
            "complexity",
            "parallelizability",
            "context_load",
            "blast_radius",
            "review_risk",
            "agent_suitability",
        ]:
            assert dim_name in score.explanation, (
                f"Expected '{dim_name}' in explanation: {score.explanation}"
            )

    def test_all_dimensions_in_range(self) -> None:
        """All score dimensions are in [1, 5]."""
        task = _make_task(
            likely_files=["src/app.py", "tests/test_app.py"],
            acceptance_criteria=["Tests pass."],
            dependencies=["T002"],
        )
        score = score_task(task)
        for dim in [
            score.complexity,
            score.parallelizability,
            score.context_load,
            score.blast_radius,
            score.review_risk,
            score.agent_suitability,
        ]:
            assert dim is not None
            assert 1 <= dim <= 5, f"Dimension {dim} is out of [1,5] range"


# ---------------------------------------------------------------------------
# Expansion queue (v1.21.0) — complexity score → auto-expansion loop
# ---------------------------------------------------------------------------


class TestSuggestedSubtaskCount:
    def test_complexity_four_suggests_three(self) -> None:
        from anvil.planning.scoring import suggested_subtask_count

        assert suggested_subtask_count(4) == 3

    def test_complexity_five_suggests_four(self) -> None:
        from anvil.planning.scoring import suggested_subtask_count

        assert suggested_subtask_count(5) == 4

    def test_low_complexity_clamps_to_expand_engine_minimum(self) -> None:
        """The suggestion never leaves the expand engine's 2-5 envelope."""
        from anvil.planning.scoring import suggested_subtask_count

        assert suggested_subtask_count(1) == 2
        assert suggested_subtask_count(2) == 2
        assert suggested_subtask_count(3) == 2


class TestBuildExpansionQueue:
    def _scored(self, task_id: str, complexity: int, title: str = "Task") -> Task:
        task = _make_task(task_id=task_id, title=title)
        task.scores = Score(complexity=complexity)
        return task

    def test_filters_below_default_threshold(self) -> None:
        """Default threshold 4: complexity 3 stays out, 4 and 5 queue."""
        from anvil.planning.scoring import build_expansion_queue

        tasks = [
            self._scored("T001", 3),
            self._scored("T002", 4),
            self._scored("T003", 5),
        ]
        queue = build_expansion_queue(tasks)
        assert [c.task_id for c in queue] == ["T003", "T002"]

    def test_exactly_at_threshold_is_included(self) -> None:
        from anvil.planning.scoring import build_expansion_queue

        queue = build_expansion_queue([self._scored("T001", 4)], threshold=4)
        assert len(queue) == 1
        assert queue[0].task_id == "T001"
        assert queue[0].complexity == 4
        assert queue[0].suggested_subtasks == 3

    def test_unscored_tasks_are_skipped(self) -> None:
        """Tasks the scoring engine has not assessed never enter the queue."""
        from anvil.planning.scoring import build_expansion_queue

        unscored = _make_task(task_id="T001")  # scores=Score() → complexity None
        queue = build_expansion_queue([unscored, self._scored("T002", 5)])
        assert [c.task_id for c in queue] == ["T002"]

    def test_custom_threshold_widens_the_queue(self) -> None:
        from anvil.planning.scoring import build_expansion_queue

        tasks = [self._scored("T001", 2), self._scored("T002", 3)]
        assert build_expansion_queue(tasks) == []
        widened = build_expansion_queue(tasks, threshold=2)
        assert [c.task_id for c in widened] == ["T002", "T001"]

    def test_sorted_by_complexity_desc_then_task_id_asc(self) -> None:
        """Deterministic ordering: most decomposition-worthy first, id tiebreak."""
        from anvil.planning.scoring import build_expansion_queue

        tasks = [
            self._scored("T009", 4),
            self._scored("T001", 4),
            self._scored("T005", 5),
        ]
        queue = build_expansion_queue(tasks)
        assert [c.task_id for c in queue] == ["T005", "T001", "T009"]

    def test_empty_input_returns_empty_queue(self) -> None:
        from anvil.planning.scoring import build_expansion_queue

        assert build_expansion_queue([]) == []

    def test_queue_carries_title_for_rendering(self) -> None:
        from anvil.planning.scoring import build_expansion_queue

        queue = build_expansion_queue([self._scored("T001", 5, title="Big refactor")])
        assert queue[0].title == "Big refactor"


# ---------------------------------------------------------------------------
# Assumption scoring (SL-6) — rank requirements by blast_radius * uncertainty
# ---------------------------------------------------------------------------


def _req(req_id: str, text: str) -> Requirement:
    """Factory for a PRD Requirement suitable for assumption-scoring tests."""
    return Requirement(id=req_id, prd_section="requirements", text=text)


class TestScoreRequirementAssumption:
    def test_priority_is_blast_times_uncertainty(self) -> None:
        score = score_requirement_assumption(
            _req("R001", "Maybe support every auth provider somehow [NEEDS DECISION]")
        )
        assert score.priority == score.blast_radius * score.uncertainty
        assert 1 <= score.blast_radius <= 5
        assert 1 <= score.uncertainty <= 5

    def test_needs_decision_marker_raises_uncertainty(self) -> None:
        marked = score_requirement_assumption(
            _req("R001", "The system stores user records [NEEDS DECISION]")
        )
        clean = score_requirement_assumption(
            _req("R002", "The system stores user records")
        )
        assert marked.uncertainty > clean.uncertainty
        assert any("NEEDS DECISION" in r or "TBD" in r for r in marked.reasons)

    def test_concrete_testable_language_lowers_uncertainty(self) -> None:
        vague = score_requirement_assumption(
            _req("R001", "The API should probably be fast and seamless")
        )
        concrete = score_requirement_assumption(
            _req(
                "R002",
                "The API must respond within 200 ms for at least 99% of requests",
            )
        )
        assert concrete.uncertainty < vague.uncertainty

    def test_blast_radius_high_for_schema_language(self) -> None:
        score = score_requirement_assumption(
            _req("R001", "Run a database schema migration on startup")
        )
        assert score.blast_radius == 5

    def test_blast_radius_base_for_plain_requirement(self) -> None:
        score = score_requirement_assumption(
            _req("R001", "Display a green checkmark beside completed items")
        )
        assert score.blast_radius == 2

    def test_reasons_never_empty(self) -> None:
        score = score_requirement_assumption(
            _req("R001", "Show the user a confirmation dialog before deleting")
        )
        assert score.reasons  # always at least the baseline reason


class TestRankAssumptions:
    def test_ranks_uncertain_above_concrete(self) -> None:
        """The known-uncertain requirement (NEEDS DECISION, no concreteness)
        ranks above a concrete, testable one of the same blast surface."""
        uncertain = _req(
            "R001", "Migrate the auth schema somehow [NEEDS DECISION], maybe later"
        )
        concrete = _req(
            "R002",
            "Migrate the auth schema in exactly one transaction within 5 seconds",
        )
        ranked = rank_assumptions([concrete, uncertain])
        assert ranked[0].requirement_id == "R001"
        assert ranked[0].priority > ranked[1].priority

    def test_limit_truncates_to_top_n(self) -> None:
        reqs = [
            _req("R001", "All users must always have seamless access [NEEDS DECISION]"),
            _req("R002", "Maybe cache responses, TBD"),
            _req("R003", "Render a list of items"),
        ]
        ranked = rank_assumptions(reqs, limit=2)
        assert len(ranked) == 2

    def test_limit_none_or_zero_returns_all(self) -> None:
        reqs = [_req("R001", "a"), _req("R002", "b"), _req("R003", "c")]
        assert len(rank_assumptions(reqs, limit=None)) == 3
        assert len(rank_assumptions(reqs, limit=0)) == 3

    def test_empty_requirements_yields_empty(self) -> None:
        assert rank_assumptions([]) == []

    def test_deterministic_ordering(self) -> None:
        reqs = [
            _req("R003", "Migrate the database schema [NEEDS DECISION]"),
            _req("R001", "Migrate the database schema [NEEDS DECISION]"),
            _req("R002", "Migrate the database schema [NEEDS DECISION]"),
        ]
        ranked = rank_assumptions(reqs)
        # Equal priority/uncertainty → id ascending tiebreak.
        assert [a.requirement_id for a in ranked] == ["R001", "R002", "R003"]


# ---------------------------------------------------------------------------
# review_tier (retro-opps T001) — derive-only light/standard/max projection
# ---------------------------------------------------------------------------


def _tier_config(**overrides: int):
    """Minimal Config for review_tier tests (defaults: max_min=4, light_risk_max=2)."""
    from anvil.config import Config

    return Config(project_name="Tier Test", project_id="tier-test", **overrides)


def _scored_task(
    *,
    complexity: int | None = 2,
    blast_radius: int | None = 2,
    review_risk: int | None = 2,
    blast_confirmed: bool = False,
    risk_confirmed: bool = False,
) -> Task:
    task = _make_task(likely_files=["src/app.py"])
    return task.model_copy(
        update={
            "scores": Score(
                complexity=complexity,
                parallelizability=3,
                context_load=3,
                blast_radius=blast_radius,
                review_risk=review_risk,
                agent_suitability=4,
                blast_radius_confirmed=blast_confirmed,
                review_risk_confirmed=risk_confirmed,
            )
        }
    )


class TestReviewTier:
    def test_unscored_task_derives_max(self) -> None:
        """Any None risk dim fails safe to max — never a lighter gate."""
        cfg = _tier_config()
        assert review_tier(_make_task(), config=cfg) == "max"
        assert review_tier(_scored_task(review_risk=None), config=cfg) == "max"
        assert review_tier(_scored_task(blast_radius=None), config=cfg) == "max"

    def test_any_dim_none_derives_max_even_with_confirmed_risk(self) -> None:
        """AC verbatim: ANY dim None → max, even a non-risk dim on an
        otherwise confirmed-low-risk task (hand-built Scores fail safe)."""
        task = _scored_task(
            complexity=None, blast_confirmed=True, risk_confirmed=True
        )
        assert review_tier(task, config=_tier_config()) == "max"

    def test_high_risk_derives_max_regardless_of_other_dims(self) -> None:
        cfg = _tier_config()
        assert (
            review_tier(
                _scored_task(
                    review_risk=4, blast_confirmed=True, risk_confirmed=True
                ),
                config=cfg,
            )
            == "max"
        )
        assert review_tier(_scored_task(blast_radius=4), config=cfg) == "max"
        assert review_tier(_scored_task(review_risk=5), config=cfg) == "max"

    def test_confirmed_low_risk_derives_light(self) -> None:
        """Confirmed 2/2/2 passes fast-lane + risk ceiling + both flags."""
        task = _scored_task(blast_confirmed=True, risk_confirmed=True)
        assert review_tier(task, config=_tier_config()) == "light"

    def test_unconfirmed_low_risk_derives_standard_never_light(self) -> None:
        cfg = _tier_config()
        assert review_tier(_scored_task(), config=cfg) == "standard"
        assert (
            review_tier(_scored_task(blast_confirmed=True), config=cfg)
            == "standard"
        )
        assert (
            review_tier(_scored_task(risk_confirmed=True), config=cfg)
            == "standard"
        )

    def test_mid_risk_confirmed_derives_standard(self) -> None:
        """review_risk=3 sits between light ceiling (2) and max floor (4)."""
        task = _scored_task(
            review_risk=3, blast_confirmed=True, risk_confirmed=True
        )
        assert review_tier(task, config=_tier_config()) == "standard"

    def test_fast_lane_failure_blocks_light(self) -> None:
        """complexity=3 exceeds the 2/2 fast-lane gate → standard, not light."""
        task = _scored_task(
            complexity=3, blast_confirmed=True, risk_confirmed=True
        )
        assert review_tier(task, config=_tier_config()) == "standard"

    def test_config_knobs_move_the_boundaries(self) -> None:
        """review_tier_max_min=3 pulls a confirmed risk-3 task up to max."""
        cfg = _tier_config(review_tier_max_min=3)
        task = _scored_task(
            review_risk=3, blast_confirmed=True, risk_confirmed=True
        )
        assert review_tier(task, config=cfg) == "max"

    def test_pure_no_mutation(self) -> None:
        task = _scored_task(blast_confirmed=True, risk_confirmed=True)
        before = task.model_dump()
        review_tier(task, config=_tier_config())
        assert task.model_dump() == before
