"""Scoring-evaluation harness for six-axis task scorers.

The harness is deliberately pure: a caller supplies a scorer function and a
fixed corpus of completed tasks with actual-proxy outcomes. The result reports
per-axis error, rank correlation, a risk-direction check, and one weighted
aggregate that future scoring changes must beat before they ship.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from math import sqrt
from typing import Literal, TypeAlias

from anvil.planning.scoring import score_task
from anvil.state.models import Score, Task, TaskPriority, TaskStatus, Verification

ScoreAxis: TypeAlias = Literal[
    "complexity",
    "parallelizability",
    "context_load",
    "blast_radius",
    "review_risk",
    "agent_suitability",
]

SCORE_AXES: tuple[ScoreAxis, ...] = (
    "complexity",
    "parallelizability",
    "context_load",
    "blast_radius",
    "review_risk",
    "agent_suitability",
)

RISK_AXES: tuple[ScoreAxis, ...] = ("blast_radius", "review_risk")
HIGH_RISK_THRESHOLD = 4

DEFAULT_AXIS_WEIGHTS: Mapping[ScoreAxis, float] = {
    "complexity": 1.5,
    "parallelizability": 1.0,
    "context_load": 1.0,
    "blast_radius": 2.0,
    "review_risk": 2.0,
    "agent_suitability": 1.0,
}

Scorer: TypeAlias = Callable[[Task], Score]
AxisScores: TypeAlias = Mapping[ScoreAxis, int]


@dataclass(frozen=True)
class ActualProxyOutcome:
    """Observed outcome proxies converted onto the 1-5 scoring scale."""

    scores: AxisScores
    evidence: str


@dataclass(frozen=True)
class ScoringCorpusCase:
    """One held-out task with frozen predicted scores and actual outcomes."""

    id: str
    task: Task
    predicted_scores: AxisScores
    actual_outcome: ActualProxyOutcome
    regression_label: str | None = None


@dataclass(frozen=True)
class ScoringCaseResult:
    """One scorer's prediction against one corpus case."""

    case_id: str
    predicted_scores: AxisScores
    actual_scores: AxisScores
    absolute_error: AxisScores


@dataclass(frozen=True)
class AxisMetric:
    """Per-axis scorer quality."""

    axis: ScoreAxis
    mae: float
    spearman: float
    weight: float


@dataclass(frozen=True)
class ScoringEvaluationReport:
    """Aggregate scorer report over a corpus."""

    scorer_name: str
    case_results: tuple[ScoringCaseResult, ...]
    per_axis: Mapping[ScoreAxis, AxisMetric]
    weighted_mae: float
    high_risk_recall: float

    @property
    def n_cases(self) -> int:
        return len(self.case_results)

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-friendly payload for CLI/CI renderers."""
        return {
            "scorer": self.scorer_name,
            "n_cases": self.n_cases,
            "weighted_mae": self.weighted_mae,
            "high_risk_recall": self.high_risk_recall,
            "per_axis": {
                axis: {
                    "mae": metric.mae,
                    "spearman": metric.spearman,
                    "weight": metric.weight,
                }
                for axis, metric in self.per_axis.items()
            },
            "cases": [
                {
                    "id": result.case_id,
                    "predicted": _axis_scores_payload(result.predicted_scores),
                    "actual": _axis_scores_payload(result.actual_scores),
                    "absolute_error": _axis_scores_payload(result.absolute_error),
                }
                for result in self.case_results
            ],
        }


@dataclass(frozen=True)
class FrozenBaseline:
    """The committed bar a candidate scorer must not regress."""

    name: str
    weighted_mae: float
    per_axis_mae: Mapping[ScoreAxis, float]
    high_risk_recall: float


def _utc_now_fixture() -> datetime.datetime:
    return datetime.datetime(2026, 6, 19, 17, 0, 0, tzinfo=datetime.UTC)


def _task_fixture(
    task_id: str,
    title: str,
    description: str,
    likely_files: Sequence[str],
    acceptance_criteria: Sequence[str],
) -> Task:
    now = _utc_now_fixture()
    return Task(
        id=task_id,
        feature_id="F005",
        title=title,
        description=description,
        status=TaskStatus.done,
        priority=TaskPriority.medium,
        scores=Score(),
        acceptance_criteria=list(acceptance_criteria),
        verification=Verification(commands=["pytest"]),
        likely_files=list(likely_files),
        dependencies=[],
        conflict_groups=[],
        created_at=now,
        updated_at=now,
    )


def _scores(**kwargs: int) -> AxisScores:
    scores: dict[ScoreAxis, int] = {}
    for axis in SCORE_AXES:
        value = kwargs.get(axis)
        if value is None:
            raise ValueError(f"missing score for axis {axis!r}")
        if value < 1 or value > 5:
            raise ValueError(f"score for axis {axis!r} must be in [1, 5]")
        scores[axis] = value
    return scores


def _axis_scores_payload(scores: AxisScores) -> dict[str, int]:
    return {axis: scores[axis] for axis in SCORE_AXES}


DEFAULT_CORPUS: tuple[ScoringCorpusCase, ...] = (
    ScoringCorpusCase(
        id="isolated-parser-schema-low-blast",
        task=_task_fixture(
            "C001",
            "Isolated parser schema file",
            "Adjust the isolated PRD parser grammar table for one edge case.",
            ["bin/src/anvil/workflows/schema.py"],
            ["parser accepts the edge case"],
        ),
        predicted_scores=_scores(
            complexity=2,
            parallelizability=4,
            context_load=2,
            blast_radius=5,
            review_risk=3,
            agent_suitability=2,
        ),
        actual_outcome=ActualProxyOutcome(
            scores=_scores(
                complexity=2,
                parallelizability=4,
                context_load=2,
                blast_radius=2,
                review_risk=2,
                agent_suitability=4,
            ),
            evidence=(
                "Observed parser-only change stayed isolated; no schema/data "
                "migration behavior changed."
            ),
        ),
        regression_label="known-miss: isolated parser scored high blast",
    ),
    ScoringCorpusCase(
        id="hidden-dependency-complexity",
        task=_task_fixture(
            "C002",
            "Hidden dependency scoring refactor",
            "Tune the task score mapping for one route.",
            ["bin/src/anvil/planning/scoring.py"],
            ["scoring output updates"],
        ),
        predicted_scores=_scores(
            complexity=2,
            parallelizability=4,
            context_load=2,
            blast_radius=3,
            review_risk=2,
            agent_suitability=4,
        ),
        actual_outcome=ActualProxyOutcome(
            scores=_scores(
                complexity=4,
                parallelizability=3,
                context_load=4,
                blast_radius=3,
                review_risk=3,
                agent_suitability=2,
            ),
            evidence=(
                "Implementation needed hidden dependency updates and review "
                "iteration beyond the single-file estimate."
            ),
        ),
        regression_label="known-miss: hidden dependency scored low complexity",
    ),
    ScoringCorpusCase(
        id="security-permission-gate",
        task=_task_fixture(
            "C003",
            "Security permission gate",
            "Add security permission checks before applying evidence.",
            ["bin/src/anvil/review/gates.py"],
            ["security checks pass", "permission denied paths tested"],
        ),
        predicted_scores=_scores(
            complexity=2,
            parallelizability=4,
            context_load=2,
            blast_radius=3,
            review_risk=5,
            agent_suitability=4,
        ),
        actual_outcome=ActualProxyOutcome(
            scores=_scores(
                complexity=3,
                parallelizability=3,
                context_load=3,
                blast_radius=4,
                review_risk=5,
                agent_suitability=2,
            ),
            evidence="Review confirmed high-risk permission behavior despite narrow files.",
        ),
    ),
    ScoringCorpusCase(
        id="cli-output-polish",
        task=_task_fixture(
            "C004",
            "CLI output polish",
            "Modify CLI rendering for the score table.",
            ["bin/src/anvil/cli/plan.py"],
            ["CLI output includes the new column"],
        ),
        predicted_scores=_scores(
            complexity=2,
            parallelizability=4,
            context_load=2,
            blast_radius=3,
            review_risk=2,
            agent_suitability=4,
        ),
        actual_outcome=ActualProxyOutcome(
            scores=_scores(
                complexity=2,
                parallelizability=3,
                context_load=2,
                blast_radius=3,
                review_risk=2,
                agent_suitability=4,
            ),
            evidence="CLI-only rendering work stayed small but serialized with CLI tests.",
        ),
    ),
    ScoringCorpusCase(
        id="docs-only-guidance-update",
        task=_task_fixture(
            "C005",
            "Docs-only guidance update",
            "Update documentation for authoring PRDs.",
            ["docs/how-to/authoring-a-prd.md"],
            ["docs mention the new guidance"],
        ),
        predicted_scores=_scores(
            complexity=2,
            parallelizability=4,
            context_load=2,
            blast_radius=2,
            review_risk=2,
            agent_suitability=4,
        ),
        actual_outcome=ActualProxyOutcome(
            scores=_scores(
                complexity=1,
                parallelizability=5,
                context_load=1,
                blast_radius=1,
                review_risk=1,
                agent_suitability=5,
            ),
            evidence="Docs-only edit had no code blast radius and no review findings.",
        ),
    ),
)

FROZEN_RULE_BASELINE = FrozenBaseline(
    name="rule-scorer-2026-06-19",
    weighted_mae=0.8706,
    per_axis_mae={
        "complexity": 0.8,
        "parallelizability": 0.8,
        "context_load": 0.8,
        "blast_radius": 1.0,
        "review_risk": 0.6,
        "agent_suitability": 1.4,
    },
    high_risk_recall=0.5,
)


def score_to_axis_scores(score: Score) -> AxisScores:
    """Extract a complete axis-score mapping from a ``Score`` model."""
    values: dict[ScoreAxis, int] = {}
    for axis in SCORE_AXES:
        value = getattr(score, axis)
        if value is None:
            raise ValueError(f"scorer did not produce {axis!r}")
        if value < 1 or value > 5:
            raise ValueError(f"score for axis {axis!r} must be in [1, 5]")
        values[axis] = value
    return values


def _validate_axis_weights(axis_weights: Mapping[ScoreAxis, float]) -> None:
    missing = [axis for axis in SCORE_AXES if axis not in axis_weights]
    if missing:
        raise ValueError(f"axis weights missing: {', '.join(missing)}")
    non_positive = [axis for axis, weight in axis_weights.items() if weight <= 0]
    if non_positive:
        raise ValueError(f"axis weights must be positive: {', '.join(non_positive)}")


def _average_ranks(values: Sequence[float]) -> list[float]:
    sorted_pairs = sorted((value, index) for index, value in enumerate(values))
    ranks = [0.0] * len(values)
    i = 0
    while i < len(sorted_pairs):
        j = i + 1
        while j < len(sorted_pairs) and sorted_pairs[j][0] == sorted_pairs[i][0]:
            j += 1
        average_rank = (i + 1 + j) / 2
        for _, original_index in sorted_pairs[i:j]:
            ranks[original_index] = average_rank
        i = j
    return ranks


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float:
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    x_deltas = [x - mean_x for x in xs]
    y_deltas = [y - mean_y for y in ys]
    numerator = sum(x * y for x, y in zip(x_deltas, y_deltas, strict=True))
    x_norm = sqrt(sum(x * x for x in x_deltas))
    y_norm = sqrt(sum(y * y for y in y_deltas))
    denominator = x_norm * y_norm
    if denominator == 0:
        return 1.0 if list(xs) == list(ys) else 0.0
    return numerator / denominator


def spearman_rank_correlation(predicted: Sequence[int], actual: Sequence[int]) -> float:
    """Return Spearman rank correlation with average ranks for ties."""
    if len(predicted) != len(actual):
        raise ValueError("predicted and actual must have the same length")
    if len(predicted) < 2:
        return 1.0
    return round(_pearson(_average_ranks(predicted), _average_ranks(actual)), 4)


def evaluate_predictions(
    predictions: Sequence[tuple[ScoringCorpusCase, AxisScores]],
    *,
    scorer_name: str,
    axis_weights: Mapping[ScoreAxis, float] = DEFAULT_AXIS_WEIGHTS,
) -> ScoringEvaluationReport:
    """Evaluate already-materialized predictions against actual outcomes."""
    if not predictions:
        raise ValueError("scoring evaluation corpus is empty")
    _validate_axis_weights(axis_weights)

    case_results: list[ScoringCaseResult] = []
    for case, predicted in predictions:
        actual = case.actual_outcome.scores
        absolute_error = {
            axis: abs(predicted[axis] - actual[axis])
            for axis in SCORE_AXES
        }
        case_results.append(ScoringCaseResult(
            case_id=case.id,
            predicted_scores=dict(predicted),
            actual_scores=dict(actual),
            absolute_error=absolute_error,
        ))

    per_axis: dict[ScoreAxis, AxisMetric] = {}
    for axis in SCORE_AXES:
        predicted_values = [result.predicted_scores[axis] for result in case_results]
        actual_values = [result.actual_scores[axis] for result in case_results]
        errors = [result.absolute_error[axis] for result in case_results]
        per_axis[axis] = AxisMetric(
            axis=axis,
            mae=round(sum(errors) / len(errors), 4),
            spearman=spearman_rank_correlation(predicted_values, actual_values),
            weight=axis_weights[axis],
        )

    total_weight = sum(axis_weights[axis] for axis in SCORE_AXES)
    weighted_mae = round(
        sum(axis_weights[axis] * per_axis[axis].mae for axis in SCORE_AXES)
        / total_weight,
        4,
    )

    actual_high = 0
    predicted_high = 0
    for result in case_results:
        for axis in RISK_AXES:
            if result.actual_scores[axis] >= HIGH_RISK_THRESHOLD:
                actual_high += 1
                predicted_high += int(result.predicted_scores[axis] >= HIGH_RISK_THRESHOLD)
    high_risk_recall = round(predicted_high / actual_high, 4) if actual_high else 1.0

    return ScoringEvaluationReport(
        scorer_name=scorer_name,
        case_results=tuple(case_results),
        per_axis=per_axis,
        weighted_mae=weighted_mae,
        high_risk_recall=high_risk_recall,
    )


def evaluate_scorer(
    scorer: Scorer,
    *,
    corpus: Sequence[ScoringCorpusCase] = DEFAULT_CORPUS,
    scorer_name: str = "candidate",
    axis_weights: Mapping[ScoreAxis, float] = DEFAULT_AXIS_WEIGHTS,
) -> ScoringEvaluationReport:
    """Run ``scorer`` over the corpus and report prediction quality."""
    predictions = [
        (case, score_to_axis_scores(scorer(case.task)))
        for case in corpus
    ]
    return evaluate_predictions(
        predictions,
        scorer_name=scorer_name,
        axis_weights=axis_weights,
    )


def evaluate_frozen_baseline(
    *,
    corpus: Sequence[ScoringCorpusCase] = DEFAULT_CORPUS,
    axis_weights: Mapping[ScoreAxis, float] = DEFAULT_AXIS_WEIGHTS,
) -> ScoringEvaluationReport:
    """Evaluate the committed rule-scorer predictions stored in the corpus."""
    return evaluate_predictions(
        [(case, case.predicted_scores) for case in corpus],
        scorer_name=FROZEN_RULE_BASELINE.name,
        axis_weights=axis_weights,
    )


def is_worse_than_baseline(
    report: ScoringEvaluationReport,
    *,
    baseline: FrozenBaseline = FROZEN_RULE_BASELINE,
    tolerance: float = 0.0,
) -> bool:
    """Return True when ``report`` misses the frozen weighted-MAE bar."""
    return report.weighted_mae > baseline.weighted_mae + tolerance


def assert_not_worse_than_baseline(
    report: ScoringEvaluationReport,
    *,
    baseline: FrozenBaseline = FROZEN_RULE_BASELINE,
    tolerance: float = 0.0,
) -> None:
    """Raise if a candidate scorer regresses the frozen baseline."""
    if is_worse_than_baseline(report, baseline=baseline, tolerance=tolerance):
        raise AssertionError(
            f"{report.scorer_name} weighted_mae={report.weighted_mae} is worse "
            f"than {baseline.name} weighted_mae={baseline.weighted_mae}"
        )


__all__ = [
    "ActualProxyOutcome",
    "AxisMetric",
    "DEFAULT_AXIS_WEIGHTS",
    "DEFAULT_CORPUS",
    "FROZEN_RULE_BASELINE",
    "FrozenBaseline",
    "HIGH_RISK_THRESHOLD",
    "RISK_AXES",
    "SCORE_AXES",
    "Scorer",
    "ScoringCaseResult",
    "ScoringCorpusCase",
    "ScoringEvaluationReport",
    "assert_not_worse_than_baseline",
    "evaluate_frozen_baseline",
    "evaluate_predictions",
    "evaluate_scorer",
    "is_worse_than_baseline",
    "score_task",
    "score_to_axis_scores",
    "spearman_rank_correlation",
]
