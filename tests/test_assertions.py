"""Tests for the artifact predicate engine (evidence-contracts:T003).

Every operator has a passing and failing case; malformed inputs are failed
assertions with reasons, never exceptions; phase predicates recreate the
voice-incident stage logic; the engine reads only the declared artifacts.
"""

from __future__ import annotations

import json
from pathlib import Path

from anvil.review.assertions import evaluate_assertions
from anvil.state.models import ArtifactAssertion, Predicate

# The voice-incident artifact shapes (issue #153).
_MEASURED = {
    "candidate_identity": {"candidate_id": "gemma4-12b-it"},
    "status": "measured",
    "stage_timings_ms": {"ttfa_ms": 611.29, "llm_ms": 356.82},
    "errors": [],
    "rows": [{"stage": "llm", "score": 3}, {"stage": "tts", "score": 5}],
}
_FAILED_STT = {
    "candidate_identity": {"candidate_id": "gemma4-12b-it"},
    "status": "failed_unavailable",
    "stage_timings_ms": {"ttfa_ms": None, "llm_ms": None},
    "errors": [{"stage": "stt", "kind": "connection_refused"}],
}


def _write(tmp_path: Path, name: str, doc: object) -> str:
    (tmp_path / name).write_text(json.dumps(doc), encoding="utf-8")
    return name


def _one(tmp_path: Path, doc: object, *preds: Predicate, **kwargs: object):
    name = _write(tmp_path, "artifact.json", doc)
    assertion = ArtifactAssertion(
        artifact=name, assertions=list(preds), **kwargs
    )
    (result,) = evaluate_assertions([assertion], tmp_path)
    return result


class TestOperators:
    def test_exists_pass_and_fail(self, tmp_path: Path) -> None:
        assert _one(tmp_path, _MEASURED, Predicate(path="status", op="exists")).passed
        r = _one(tmp_path, _MEASURED, Predicate(path="nope.deep", op="exists"))
        assert not r.passed and "not found" in r.failures[0]

    def test_not_null_pass_and_fail(self, tmp_path: Path) -> None:
        assert _one(
            tmp_path, _MEASURED,
            Predicate(path="stage_timings_ms.llm_ms", op="not_null"),
        ).passed
        r = _one(
            tmp_path, _FAILED_STT,
            Predicate(path="stage_timings_ms.llm_ms", op="not_null"),
        )
        assert not r.passed and "null" in r.failures[0]

    def test_equals_pass_and_fail_with_observed(self, tmp_path: Path) -> None:
        assert _one(
            tmp_path, _MEASURED, Predicate(path="status", op="equals", value="measured")
        ).passed
        r = _one(
            tmp_path, _FAILED_STT,
            Predicate(path="status", op="equals", value="measured"),
        )
        assert not r.passed
        assert "failed_unavailable" in r.failures[0]  # observed value named
        assert r.observed["status"] == "failed_unavailable"

    def test_not_equals_pass_fail_and_vacuous(self, tmp_path: Path) -> None:
        assert _one(
            tmp_path, _MEASURED,
            Predicate(path="status", op="not_equals", value="failed_unavailable"),
        ).passed
        assert not _one(
            tmp_path, _FAILED_STT,
            Predicate(path="status", op="not_equals", value="failed_unavailable"),
        ).passed
        # Unresolved path proves the absence.
        assert _one(
            tmp_path, _MEASURED,
            Predicate(path="missing.path", op="not_equals", value="x"),
        ).passed

    def test_contains_and_not_contains_wildcard(self, tmp_path: Path) -> None:
        assert _one(
            tmp_path, _FAILED_STT,
            Predicate(path="errors[*].stage", op="contains", value="stt"),
        ).passed
        assert not _one(
            tmp_path, _MEASURED,
            Predicate(path="errors[*].stage", op="contains", value="stt"),
        ).passed
        # THE incident predicate: no stt failure recorded → pass.
        assert _one(
            tmp_path, _MEASURED,
            Predicate(path="errors[*].stage", op="not_contains", value="stt"),
        ).passed
        r = _one(
            tmp_path, _FAILED_STT,
            Predicate(path="errors[*].stage", op="not_contains", value="stt"),
        )
        assert not r.passed and "contains 'stt'" in r.failures[0]

    def test_not_contains_vacuous_on_missing_path(self, tmp_path: Path) -> None:
        assert _one(
            tmp_path, {"no": "errors"},
            Predicate(path="errors[*].stage", op="not_contains", value="stt"),
        ).passed

    def test_numeric_comparisons(self, tmp_path: Path) -> None:
        assert _one(
            tmp_path, _MEASURED,
            Predicate(path="stage_timings_ms.llm_ms", op="lt", value=1000),
        ).passed
        assert not _one(
            tmp_path, _MEASURED,
            Predicate(path="stage_timings_ms.llm_ms", op="gt", value=1000),
        ).passed
        assert _one(
            tmp_path, _MEASURED,
            Predicate(path="stage_timings_ms.ttfa_ms", op="gte", value=611.29),
        ).passed
        assert not _one(
            tmp_path, _MEASURED,
            Predicate(path="stage_timings_ms.ttfa_ms", op="lte", value=100),
        ).passed
        # Wildcard numeric: every row score >= 3.
        assert _one(
            tmp_path, _MEASURED,
            Predicate(path="rows[*].score", op="gte", value=3),
        ).passed

    def test_numeric_on_null_is_failure_not_crash(self, tmp_path: Path) -> None:
        r = _one(
            tmp_path, _FAILED_STT,
            Predicate(path="stage_timings_ms.llm_ms", op="gt", value=0),
        )
        assert not r.passed

    def test_len_ops(self, tmp_path: Path) -> None:
        assert _one(
            tmp_path, _MEASURED, Predicate(path="rows", op="len_eq", value=2)
        ).passed
        assert not _one(
            tmp_path, _MEASURED, Predicate(path="rows", op="len_eq", value=3)
        ).passed
        assert _one(
            tmp_path, _MEASURED, Predicate(path="rows", op="len_gte", value=1)
        ).passed
        r = _one(
            tmp_path, _MEASURED,
            Predicate(path="stage_timings_ms.llm_ms", op="len_gte", value=1),
        )
        assert not r.passed and "no length" in r.failures[0]


class TestMalformation:
    def test_missing_artifact_is_failed_with_reason(self, tmp_path: Path) -> None:
        assertion = ArtifactAssertion(
            artifact="never/written.json",
            assertions=[Predicate(path="x", op="exists")],
        )
        (result,) = evaluate_assertions([assertion], tmp_path)
        assert not result.passed
        assert "does not exist" in result.failures[0]

    def test_malformed_json_is_failed_with_reason(self, tmp_path: Path) -> None:
        (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
        assertion = ArtifactAssertion(
            artifact="bad.json", assertions=[Predicate(path="x", op="exists")]
        )
        (result,) = evaluate_assertions([assertion], tmp_path)
        assert not result.passed
        assert "not valid JSON" in result.failures[0]

    def test_wildcard_on_non_list_is_unresolved(self, tmp_path: Path) -> None:
        r = _one(
            tmp_path, _MEASURED,
            Predicate(path="status[*].x", op="exists"),
        )
        assert not r.passed and "not found" in r.failures[0]

    def test_engine_reads_only_declared_artifacts(self, tmp_path: Path) -> None:
        """Purity: no writes anywhere, no reads beyond the declared files."""
        name = _write(tmp_path, "artifact.json", _MEASURED)
        before = sorted(p.name for p in tmp_path.iterdir())
        evaluate_assertions(
            [ArtifactAssertion(artifact=name, assertions=[Predicate(path="status", op="exists")])],
            tmp_path,
        )
        assert sorted(p.name for p in tmp_path.iterdir()) == before


class TestPhasePredicates:
    _STAGES = {"stage_order": ["stt", "llm", "tts"], "stage_path": "errors[*].stage"}

    def test_must_not_fail_before_fails_on_early_stage(self, tmp_path: Path) -> None:
        """The voice incident: STT failure before the LLM stage."""
        r = _one(tmp_path, _FAILED_STT, must_not_fail_before="llm", **self._STAGES)
        assert not r.passed
        assert "failed at stage 'stt'" in r.failures[0]

    def test_must_not_fail_before_passes_clean_and_late_failures(
        self, tmp_path: Path
    ) -> None:
        assert _one(
            tmp_path, _MEASURED, must_not_fail_before="llm", **self._STAGES
        ).passed
        late = dict(_FAILED_STT, errors=[{"stage": "tts", "kind": "x"}])
        assert _one(
            tmp_path, late, must_not_fail_before="llm", **self._STAGES
        ).passed

    def test_must_reach_is_stricter_than_not_fail_before(self, tmp_path: Path) -> None:
        """A failure AT the target stage passes must_not_fail_before but
        fails must_reach (entered, never completed)."""
        at_llm = dict(_FAILED_STT, errors=[{"stage": "llm", "kind": "oom"}])
        assert _one(
            tmp_path, at_llm, must_not_fail_before="llm", **self._STAGES
        ).passed
        r = _one(tmp_path, at_llm, must_reach="llm", **self._STAGES)
        assert not r.passed and "never completed 'llm'" in r.failures[0]

    def test_phase_predicates_without_stage_config_are_loud(
        self, tmp_path: Path
    ) -> None:
        r = _one(tmp_path, _MEASURED, must_reach="llm")
        assert not r.passed
        assert "require both stage_order and stage_path" in r.failures[0]

    def test_unknown_target_stage_is_loud(self, tmp_path: Path) -> None:
        r = _one(tmp_path, _MEASURED, must_reach="deploy", **self._STAGES)
        assert not r.passed and "not in stage_order" in r.failures[0]


class TestClaimBindingCarrythrough:
    def test_result_carries_claim_and_artifact(self, tmp_path: Path) -> None:
        name = _write(tmp_path, "artifact.json", _MEASURED)
        assertion = ArtifactAssertion(
            artifact=name,
            claim="candidate_benchmark_completed",
            assertions=[Predicate(path="status", op="equals", value="measured")],
        )
        (result,) = evaluate_assertions([assertion], tmp_path)
        assert result.passed
        assert result.claim == "candidate_benchmark_completed"
        assert result.artifact == name
