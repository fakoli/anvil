"""Frozen synthetic labels validate plumbing, not held-out model accuracy.

Cases and expected labels are authored before live evaluation. A live runner
must retain every outcome separately and never rewrite this file to fit it.
"""

import copy
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path

import pytest
from scripts import qualify_jev

from anvil.jev import CAPABILITIES, MAX_INPUT_BYTES, JevConfig, _json_bytes, _questions
from anvil.jev_questions import build_questions, reject_secrets

CASES_PATH = Path(__file__).parent / "fixtures" / "jev" / "cases.json"
CASES = json.loads(CASES_PATH.read_text(encoding="utf-8"))
BROWSER_CASES = [case for case in CASES if case["capability"] == "browser_element_resolution"]
_BROWSER_ARTIFACT = re.compile(
    r"(?:https?://|//|\b(?:url|origin|html|screenshot|cookie|transcript|credential|"
    r"password|token|form|command|epoch|script|dump|path)\b|<[^>]+>)",
    re.IGNORECASE,
)


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def test_corpus_identity_and_coverage():
    assert len(CASES) == 48
    assert len({case["id"] for case in CASES}) == len(CASES)
    assert Counter(case["capability"] for case in CASES if case["group"] == "core") == {
        capability: 4
        for capability in CAPABILITIES
        if capability != "browser_element_resolution"
    }
    assert Counter(case["group"] for case in CASES) == {
        "core": 28, "stale_handoff_audit": 4, "release_note_claim_grounding": 4,
        "missing_regression_proof_experiment": 4, "browser_element_resolution": 8,
    }
    reject_secrets(CASES_PATH.read_text(encoding="utf-8"))


def test_browser_corpus_is_synthetic_projection_data_with_required_edge_coverage():
    assert len(BROWSER_CASES) == 8
    assert {case["expected"]["selection"]["equals"] for case in BROWSER_CASES} >= {
        "alpha",
        "signal",
        "NO_MATCH_IN_CANDIDATES",
        "AMBIGUOUS",
        "NEEDS_VISUAL_EVIDENCE",
    }
    assert any(
        entity["text"] == ""
        for case in BROWSER_CASES
        for entity in case["input"]["entities"]
    )
    assert {case["input"]["coverage"]["state"] for case in BROWSER_CASES} == {
        "complete",
        "partial",
        "unknown",
    }
    null_pairs = next(
        case for case in BROWSER_CASES if case["id"] == "browser_null_predicate_pairs"
    )
    assert set(null_pairs["input"]["entities"][0]["predicate_reasons"].values()) == {
        "not_applicable",
        "unknown",
        "unsupported",
    }
    for case in BROWSER_CASES:
        for text in _strings(case["input"]):
            assert not _BROWSER_ARTIFACT.search(text)


@pytest.mark.parametrize(
    "refusal",
    [
        {"origin": "outside the closed projection"},
        {"target": {"description": "password: synthetic-browser-only", "qualifiers": []}},
        {"entities": "not an entity list"},
    ],
    ids=["extra_field", "recognized_credential", "malformed_entities"],
)
def test_browser_refusal_corpus_fails_before_a_question_is_built(refusal):
    projection = copy.deepcopy(BROWSER_CASES[0]["input"])
    projection.update(refusal)
    with pytest.raises(ValueError):
        build_questions("browser_element_resolution", projection)


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


@pytest.mark.parametrize(
    ("live", "used", "answer", "exit_code"),
    [(True, True, "supports", 0), (True, True, "contradicts", 1),
     (True, False, None, 1), (False, False, None, 0)],
)
def test_qualification_exit_requires_completed_matching_labels(
    tmp_path, monkeypatch, live, used, answer, exit_code,
):
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps([CASES[4]]), encoding="utf-8")
    output = tmp_path / "report.json"
    monkeypatch.chdir(CASES_PATH.parents[3])
    monkeypatch.setattr(sys, "argv", [
        "qualify_jev", "--cases", str(cases), "--output", str(output),
        *(["--live"] if live else []),
    ])
    monkeypatch.setattr(qualify_jev, "evaluate", lambda *args, **kwargs: {
        "used": used, "answers": {"relation": {"choice": answer}} if used else {},
        "elapsed_ms": 1, "usage": {"input_tokens": 1, "output_tokens": 1},
    })

    assert qualify_jev.main() == exit_code
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["completed"] == int(used)
    assert report["cases"][0]["matches_expected"] == (
        used and answer == "supports" if live else None
    )
