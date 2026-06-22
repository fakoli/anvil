"""Tests for the scoring-eval corpus, metrics, and baseline gate."""

from __future__ import annotations

import json
from math import inf, nan

import pytest

from anvil.planning.scoring import score_task
from anvil.planning.scoring_eval import (
    DEFAULT_CORPUS,
    FROZEN_RULE_BASELINE,
    SCORE_AXES,
    assert_not_worse_than_baseline,
    evaluate_frozen_baseline,
    evaluate_predictions,
    evaluate_scorer,
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
        assert case.actual_outcome.source.strip()
        assert case.source_task_id is not None
        assert case.source_task_id.strip()
        for axis in SCORE_AXES:
            assert 1 <= case.predicted_scores[axis] <= 5
            assert 1 <= case.actual_outcome.scores[axis] <= 5


def test_corpus_contains_known_scoring_regression_cases() -> None:
    cases = {case.regression_label: case for case in DEFAULT_CORPUS}
    isolated_parser = cases["known-miss: isolated parser scored high blast"]
    hidden_dependency = cases["known-miss: hidden dependency scored low complexity"]

    assert isolated_parser.predicted_scores["blast_radius"] == 5
    assert isolated_parser.actual_outcome.scores["blast_radius"] <= 2
    assert hidden_dependency.predicted_scores["complexity"] == 2
    assert hidden_dependency.actual_outcome.scores["complexity"] >= 4


def test_frozen_baseline_uses_stored_predictions_not_live_scorer() -> None:
    """The frozen baseline remains historical when the live scorer changes."""
    report = evaluate_frozen_baseline()
    by_id = {case.id: case for case in DEFAULT_CORPUS}

    for result in report.case_results:
        assert result.predicted_scores == by_id[result.case_id].predicted_scores


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
    json.dumps(payload, allow_nan=False)


def test_current_rule_scorer_does_not_regress_frozen_baseline() -> None:
    report = evaluate_scorer(score_task, scorer_name="current-rule")

    assert_not_worse_than_baseline(report)


def test_baseline_gate_rejects_worse_weighted_mae() -> None:
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


def test_baseline_gate_rejects_lost_high_risk_signal() -> None:
    predictions = []
    for case in DEFAULT_CORPUS:
        predicted = dict(case.actual_outcome.scores)
        for axis in ("blast_radius", "review_risk"):
            if predicted[axis] >= 4:
                predicted[axis] = 1
        predictions.append((case, predicted))
    report = evaluate_predictions(predictions, scorer_name="risk-blind")

    assert report.weighted_mae < FROZEN_RULE_BASELINE.weighted_mae
    assert report.high_risk_recall == 0.0
    with pytest.raises(AssertionError, match="high_risk_recall"):
        assert_not_worse_than_baseline(report)


def test_evaluate_scorer_protects_corpus_tasks_from_mutating_scorers() -> None:
    before = list(DEFAULT_CORPUS[0].task.likely_files)

    def mutating_scorer(task: Task) -> Score:
        task.likely_files.append("surprise.py")
        return score_task(task)

    evaluate_scorer(mutating_scorer, corpus=[DEFAULT_CORPUS[0]], scorer_name="mutator")

    assert DEFAULT_CORPUS[0].task.likely_files == before


def test_evaluate_predictions_rejects_invalid_axis_scores() -> None:
    invalid = dict(DEFAULT_CORPUS[0].predicted_scores)
    invalid["complexity"] = 99

    with pytest.raises(ValueError, match="must be in"):
        evaluate_predictions([(DEFAULT_CORPUS[0], invalid)], scorer_name="invalid")


def test_axis_weights_must_be_finite() -> None:
    bad_nan = dict.fromkeys(SCORE_AXES, 1.0)
    bad_nan["complexity"] = nan
    bad_inf = dict.fromkeys(SCORE_AXES, 1.0)
    bad_inf["review_risk"] = inf

    with pytest.raises(ValueError, match="finite"):
        evaluate_frozen_baseline(axis_weights=bad_nan)
    with pytest.raises(ValueError, match="finite"):
        evaluate_frozen_baseline(axis_weights=bad_inf)


def test_spearman_rank_correlation_handles_ties_and_inverse_order() -> None:
    assert spearman_rank_correlation([1, 2, 3], [1, 2, 3]) == 1.0
    assert spearman_rank_correlation([1, 2, 3], [3, 2, 1]) == -1.0
    assert spearman_rank_correlation([2, 2, 4], [1, 1, 5]) == 1.0
    assert spearman_rank_correlation([1, 1, 1], [5, 5, 5]) == 0.0
    assert spearman_rank_correlation([1, 1, 1], [1, 1, 1]) == 1.0
