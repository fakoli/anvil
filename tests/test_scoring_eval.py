"""Tests for the scoring-eval corpus, metrics, and baseline gate."""

from __future__ import annotations

import json

import pytest

from anvil.planning.scoring import score_task
from anvil.planning.scoring_eval import (
    DEFAULT_CORPUS,
    FROZEN_RULE_BASELINE,
    SCORE_AXES,
    assert_not_worse_than_baseline,
    evaluate_frozen_baseline,
    evaluate_scorer,
    score_to_axis_scores,
    spearman_rank_correlation,
)
from anvil.state.models import Score, Task


def test_corpus_captures_tasks_predictions_and_actual_proxy_outcomes() -> None:
    """Each corpus row has the task, frozen prediction, and revealed outcome."""
    assert DEFAULT_CORPUS
    for case in DEFAULT_CORPUS:
        assert isinstance(case.task, Task)
        assert set(case.predicted_scores) == set(SCORE_AXES)
        assert set(case.actual_outcome.scores) == set(SCORE_AXES)
        assert case.actual_outcome.evidence.strip()
        for axis in SCORE_AXES:
            assert 1 <= case.predicted_scores[axis] <= 5
            assert 1 <= case.actual_outcome.scores[axis] <= 5


def test_corpus_contains_known_scoring_regression_cases() -> None:
    labels = {case.regression_label for case in DEFAULT_CORPUS}
    assert "known-miss: isolated parser scored high blast" in labels
    assert "known-miss: hidden dependency scored low complexity" in labels


def test_frozen_predictions_match_current_rule_scorer_until_it_changes() -> None:
    """The stored rule-baseline predictions are intentionally explicit."""
    for case in DEFAULT_CORPUS:
        assert score_to_axis_scores(score_task(case.task)) == case.predicted_scores


def test_evaluate_frozen_baseline_reports_weighted_and_per_axis_metrics() -> None:
    report = evaluate_frozen_baseline()

    assert report.scorer_name == FROZEN_RULE_BASELINE.name
    assert report.n_cases == len(DEFAULT_CORPUS)
    assert report.weighted_mae == FROZEN_RULE_BASELINE.weighted_mae
    assert report.high_risk_recall == FROZEN_RULE_BASELINE.high_risk_recall
    assert report.per_axis["blast_radius"].mae == 1.0
    assert report.per_axis["review_risk"].mae == 0.6
    assert report.per_axis["blast_radius"].weight > report.per_axis["context_load"].weight
    assert -1.0 <= report.per_axis["complexity"].spearman <= 1.0


def test_report_payload_is_json_friendly() -> None:
    payload = evaluate_frozen_baseline().to_dict()

    assert payload["weighted_mae"] == FROZEN_RULE_BASELINE.weighted_mae
    json.dumps(payload)


def test_current_rule_scorer_does_not_regress_frozen_baseline() -> None:
    report = evaluate_scorer(score_task, scorer_name="current-rule")

    assert_not_worse_than_baseline(report)


def test_baseline_gate_rejects_worse_scorer() -> None:
    def all_low_scorer(_: Task) -> Score:
        return Score(
            complexity=1,
            parallelizability=1,
            context_load=1,
            blast_radius=1,
            review_risk=1,
            agent_suitability=1,
        )

    report = evaluate_scorer(all_low_scorer, scorer_name="all-low")

    with pytest.raises(AssertionError, match="worse than"):
        assert_not_worse_than_baseline(report)


def test_spearman_rank_correlation_handles_ties_and_inverse_order() -> None:
    assert spearman_rank_correlation([1, 2, 3], [1, 2, 3]) == 1.0
    assert spearman_rank_correlation([1, 2, 3], [3, 2, 1]) == -1.0
    assert spearman_rank_correlation([2, 2, 4], [1, 1, 5]) == 1.0
