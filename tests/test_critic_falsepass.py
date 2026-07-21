"""Critic false-pass harness self-test (roadmap SL-2).

SL-2 ships a fault-injection harness that feeds a corpus of known-bad diffs to
the critic and measures the false-pass rate (how many bad diffs it waves
through). The real critic is an LLM agent (``agents/critic.md``): API-dependent
and non-deterministic, so it CANNOT run in deterministic CI. This test therefore
exercises the harness with the **deterministic mock backend only** — no network,
no LLM — and asserts three things:

  1. the false-pass / false-fail arithmetic is correct on the fixed mock result;
  2. the corpus has integrity (every case labelled + classed; all four named bad
     classes present; good controls present);
  3. the ``api`` backend fails LOUDLY (``NotImplementedError``) rather than
     silently fabricating a verdict.

Layout note: this file lives at ``<repo-root>/tests/test_critic_falsepass.py``
and the harness at ``<repo-root>/benchmarks/critic_falsepass.py``. We add
``benchmarks/`` to ``sys.path`` and import the module directly — mirroring how
``benchmarks/run_benchmark.py`` bootstraps its own package. Explicit test paths
from ``bin/`` discover the repository-root ``pytest.ini``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import the harness from benchmarks/ (mirrors run_benchmark.py's bootstrap).
# ---------------------------------------------------------------------------

_BENCHMARKS = Path(__file__).resolve().parents[1] / "benchmarks"
if str(_BENCHMARKS) not in sys.path:
    sys.path.insert(0, str(_BENCHMARKS))

import critic_falsepass as cf  # noqa: E402

# ---------------------------------------------------------------------------
# Corpus integrity
# ---------------------------------------------------------------------------


def test_corpus_loads_and_every_case_is_well_formed() -> None:
    """Every case carries a valid label, a defect_class, a description, a diff."""
    cases = cf.load_corpus()
    assert cases, "corpus is empty"
    for c in cases:
        assert c.label in {"bad", "good"}, f"{c.id} has bad label {c.label!r}"
        assert c.defect_class, f"{c.id} missing defect_class"
        assert c.description.strip(), f"{c.id} missing description"
        assert c.diff.strip(), f"{c.id} has an empty diff"


def test_corpus_contains_every_named_bad_class() -> None:
    """The four bad classes the roadmap (SL-2) names must all be represented."""
    cases = cf.load_corpus()
    present = {c.defect_class for c in cases if c.is_bad}
    missing = cf.REQUIRED_BAD_CLASSES - present
    assert not missing, f"corpus is missing required bad classes: {sorted(missing)}"


def test_corpus_has_good_controls() -> None:
    """The design requires two good controls so the false-fail rate is meaningful."""
    cases = cf.load_corpus()
    goods = [c for c in cases if not c.is_bad]
    assert len(goods) >= 2, "corpus needs >=2 good controls to detect over-rejection"
    assert all(c.defect_class == "none" for c in goods), (
        "good controls should have defect_class 'none'"
    )


# ---------------------------------------------------------------------------
# The oracle / false-pass math on the deterministic mock backend
# ---------------------------------------------------------------------------


def test_mock_backend_false_pass_math_is_correct() -> None:
    """The mock catches 3 of 4 bad classes and misses off-by-one => 1/4 = 0.25.

    This is the committed self-test reference recorded in
    docs/critic-false-pass-baseline.md. It locks the harness arithmetic without
    any API call.
    """
    cases = cf.load_corpus()
    report = cf.run(cf.mock_backend, "mock", cases)

    # Headline: exactly one false pass (off_by_one) out of four bad cases.
    assert report.n_bad == 4
    assert report.n_good == 2
    assert report.false_passes == 1
    assert report.false_pass_rate == 0.25
    # No good control is wrongly rejected.
    assert report.false_fails == 0
    assert report.false_fail_rate == 0.0


def test_mock_backend_waves_through_off_by_one_only() -> None:
    """Pin exactly which bad case the mock misses: off-by-one (the blind spot)."""
    cases = cf.load_corpus()
    report = cf.run(cf.mock_backend, "mock", cases)
    waved_through = {r["id"] for r in report.results if r["false_pass"]}
    assert waved_through == {"off_by_one"}


def test_mock_backend_catches_the_text_visible_classes() -> None:
    """The mock must FAIL the three classes a textual rule can see."""
    cases = cf.load_corpus()
    report = cf.run(cf.mock_backend, "mock", cases)
    verdict_by_id = {r["id"]: r["verdict"] for r in report.results}
    for case_id in ("deleted_assertion", "dropped_null_check", "assertion_free_test"):
        assert verdict_by_id[case_id] == "FAIL", f"mock should catch {case_id}"


def test_run_report_to_dict_is_json_friendly_and_consistent() -> None:
    """The JSON shape matches the headline counts (the --json contract)."""
    import json

    cases = cf.load_corpus()
    report = cf.run(cf.mock_backend, "mock", cases)
    payload = json.loads(json.dumps(report.to_dict()))  # round-trip must not raise
    assert payload["backend"] == "mock"
    assert payload["n_cases"] == report.n_bad + report.n_good
    assert payload["false_pass_rate"] == 0.25
    assert len(payload["results"]) == report.n_bad + report.n_good


# ---------------------------------------------------------------------------
# The api backend must fail loudly, never fabricate.
# ---------------------------------------------------------------------------


def test_api_backend_raises_rather_than_fabricating() -> None:
    """The real-critic backend must raise NotImplementedError until wired up.

    Critical: it must NEVER silently return a verdict, which would fabricate a
    false-pass number. The guidance must point at the doc + the API-key need.
    """
    cases = cf.load_corpus()
    with pytest.raises(NotImplementedError) as excinfo:
        cf.api_backend(cases[0])
    msg = str(excinfo.value).lower()
    assert "api" in msg
    assert "baseline" in msg or "fabricate" in msg


def test_cli_main_runs_mock_backend(capsys: pytest.CaptureFixture[str]) -> None:
    """The CLI entrypoint runs the mock backend and prints the headline rate."""
    rc = cf.main(["--backend", "mock"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "false_pass_rate" in out
    assert "0.25" in out
