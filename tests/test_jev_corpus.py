"""Frozen synthetic labels validate plumbing, not held-out model accuracy.

Cases and expected labels are authored before live evaluation. A live runner
must retain every outcome separately and never rewrite this file to fit it.
"""

import copy
import json
import math
from collections import Counter
from pathlib import Path

import pytest

from anvil.jev import CAPABILITIES, MAX_INPUT_BYTES, JevConfig, _json_bytes, _questions
from anvil.jev_questions import build_questions, reject_secrets

CASES_PATH = Path(__file__).parent / "fixtures" / "jev" / "cases.json"
CASES = json.loads(CASES_PATH.read_text(encoding="utf-8"))


def test_corpus_identity_and_coverage():
    assert len(CASES) == 40
    assert len({case["id"] for case in CASES}) == len(CASES)
    assert Counter(case["capability"] for case in CASES if case["group"] == "core") == dict.fromkeys(
        CAPABILITIES, 4,
    )
    assert Counter(case["group"] for case in CASES) == {
        "core": 28, "stale_handoff_audit": 4, "release_note_claim_grounding": 4,
        "missing_regression_proof_experiment": 4,
    }
    reject_secrets(CASES_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_case_has_bounded_deterministic_request_and_valid_independent_labels(case):
    assert set(case) == {"id", "group", "capability", "input", "expected", "label_rationale"}
    assert isinstance(case["id"], str) and case["id"].isidentifier()
    assert isinstance(case["label_rationale"], str) and case["label_rationale"].strip()
    original = copy.deepcopy(case)
    state, questions = build_questions(case["capability"], case["input"])
    assert (state, questions) == build_questions(case["capability"], case["input"])
    assert case == original and state == case["input"]
    assert _questions(questions) == questions
    assert len(_json_bytes({"model": JevConfig().model, "state": state, "questions": questions})) <= MAX_INPUT_BYTES
    assert case["expected"].keys() == questions.keys()
    for name, expectation in case["expected"].items():
        question = questions[name]
        assert expectation["field"] == question["type"]
        if question["type"] == "choice":
            assert set(expectation) == {"field", "equals"}
            assert expectation["equals"] in question["criteria"]
        else:
            assert set(expectation) == {"field", "min", "max"}
            low, high = expectation["min"], expectation["max"]
            assert type(low) in (int, float) and type(high) in (int, float)
            assert math.isfinite(low) and math.isfinite(high)
            ceiling = len(question["criteria"]) - 1 if question["type"] == "score" else 1
            assert 0 <= low <= high <= ceiling
    assert "expected" not in state and "label_rationale" not in state
