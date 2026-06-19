#!/usr/bin/env python3
"""Critic false-pass harness (roadmap SL-2).

A fault-injection harness: feed a corpus of known-bad diffs to a critic backend
and measure how many bad diffs the critic *waves through* (the false-pass rate).
You cannot improve the critic until you can score it; this puts a number on it.

    cd bin
    uv run python ../benchmarks/critic_falsepass.py                 # mock backend
    uv run python ../benchmarks/critic_falsepass.py --backend mock --json
    uv run python ../benchmarks/critic_falsepass.py --backend api   # real critic (manual)

The harness is backend-agnostic on purpose. A *backend* is just a callable that
takes a corpus case and returns a verdict — `Verdict.PASS` ("the critic approved
this diff") or `Verdict.FAIL` ("the critic rejected it"). Two backends ship:

  * ``mock`` — a deterministic, dependency-free rule-set. It catches some defect
    classes and misses others by design, so its false-pass number is *stable*
    and serves as the committed self-test reference (see
    ``docs/critic-false-pass-baseline.md``). It makes NO API call.
  * ``api`` — the real critic agent (``agents/critic.md``), an LLM that needs
    API access and is non-deterministic, so it cannot run in deterministic CI.
    It is wired here as a documented stub that raises ``NotImplementedError``
    with guidance until the agent harness is connected — it never silently
    fabricates a number.

Definitions (the oracle, pure functions over recorded verdicts):

  false_pass_rate = (# BAD cases the critic PASSED) / (# BAD cases)
  false_fail_rate = (# GOOD cases the critic FAILED) / (# GOOD cases)

A *false pass* is the dangerous one: a known-bad diff the critic approved.

No third-party dependencies; the corpus is plain ``.diff`` + ``.json`` files.
"""
from __future__ import annotations

import argparse
import enum
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

CORPUS_DIR = Path(__file__).resolve().parent / "critic_corpus"

#: The bad defect classes the roadmap (SL-2) names explicitly. The corpus must
#: contain at least one case of each; the integrity test enforces it.
REQUIRED_BAD_CLASSES = frozenset(
    {"off-by-one", "dropped-null-check", "assertion-free-test", "deleted-assertion"}
)


class Verdict(enum.Enum):
    """A critic's call on one diff. PASS == 'approved' (waved through)."""

    PASS = "PASS"
    FAIL = "FAIL"


@dataclass(frozen=True)
class Case:
    """One corpus fixture: a diff + its ground-truth label."""

    id: str
    label: str          # "bad" | "good"
    defect_class: str   # e.g. "off-by-one"; "none" for good controls
    description: str
    diff: str

    @property
    def is_bad(self) -> bool:
        return self.label == "bad"


#: A backend is a callable that returns a Verdict for a case.
Backend = Callable[[Case], Verdict]


# --- corpus loading ---------------------------------------------------------

def load_corpus(corpus_dir: Path = CORPUS_DIR) -> list[Case]:
    """Load every ``<id>.json`` + sibling ``<id>.diff`` pair, sorted by id.

    Each metadata file must carry ``id``, ``label`` ('bad'|'good'),
    ``defect_class`` and ``description``; a sibling ``.diff`` must exist.
    """
    cases: list[Case] = []
    for meta_path in sorted(corpus_dir.glob("*.json")):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        diff_path = meta_path.with_suffix(".diff")
        if not diff_path.is_file():
            raise FileNotFoundError(
                f"corpus case {meta_path.name} has no sibling diff at {diff_path.name}"
            )
        label = meta["label"]
        if label not in {"bad", "good"}:
            raise ValueError(
                f"corpus case {meta['id']!r} has invalid label {label!r} "
                "(must be 'bad' or 'good')"
            )
        cases.append(
            Case(
                id=meta["id"],
                label=label,
                defect_class=meta["defect_class"],
                description=meta["description"],
                diff=diff_path.read_text(encoding="utf-8"),
            )
        )
    if not cases:
        raise FileNotFoundError(f"no corpus cases (*.json) found under {corpus_dir}")
    return cases


# --- backends ---------------------------------------------------------------

def _added_lines(diff: str) -> list[str]:
    return [ln[1:] for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++")]


def _removed_lines(diff: str) -> list[str]:
    return [ln[1:] for ln in diff.splitlines() if ln.startswith("-") and not ln.startswith("---")]


def _touches_test_file(diff: str) -> bool:
    return bool(re.search(r"^\+\+\+ .*test", diff, re.MULTILINE | re.IGNORECASE))


def mock_backend(case: Case) -> Verdict:
    """A deterministic, dependency-free stand-in for the critic.

    It is a *cheap linter*, not a reasoner: it catches defect classes a simple
    textual rule can see, and is structurally blind to ones requiring semantic
    reasoning (e.g. arithmetic off-by-one). The blind spots are deliberate so
    the resulting false-pass number is fixed and reproducible — it is the
    committed self-test reference, NOT a claim about the real LLM critic.

    Rules (any one fires => FAIL / rejected):
      * removes an ``assert`` line                     -> deleted-assertion
      * adds a ``def test_...`` with no ``assert``      -> assertion-free-test
      * removes a null/None guard line                  -> dropped-null-check

    Everything else => PASS. Notably it has NO arithmetic rule, so an
    off-by-one (``range(len(x) + 1)``) is waved through — a false pass.
    """
    added = _added_lines(case.diff)
    removed = _removed_lines(case.diff)

    # deleted-assertion: an assert was removed.
    if any(re.match(r"\s*assert\b", ln) for ln in removed):
        return Verdict.FAIL

    # dropped-null-check: a None/null guard was removed.
    if any(re.search(r"\bis\s+None\b|==\s*None\b|!=\s*None\b", ln) for ln in removed):
        return Verdict.FAIL

    # assertion-free-test: a new test function with no assert anywhere in the additions.
    if _touches_test_file(case.diff) and any(re.match(r"\s*def test_", ln) for ln in added):
        if not any(re.search(r"\bassert\b", ln) for ln in added):
            return Verdict.FAIL

    return Verdict.PASS


def api_backend(case: Case) -> Verdict:
    """Invoke the REAL critic agent (``agents/critic.md``) on the case.

    The critic is an LLM agent: non-deterministic and API-dependent, so it
    cannot run in deterministic pytest CI. This stub is the documented seam
    where the agent harness is wired in. Until then it raises loudly rather
    than fabricating a verdict — populating the real baseline is a manual,
    API-having step.

    To implement: render ``case.diff`` (plus a minimal acceptance-criteria
    stub) into the critic agent prompt, run it via the Claude Agent SDK with a
    valid ``ANTHROPIC_API_KEY``, parse the agent's '## Verdict' section, and map
    PASS -> Verdict.PASS / (SHOULD FIX | MUST FIX) -> Verdict.FAIL.
    """
    raise NotImplementedError(
        "The `api` backend is not wired up. The real critic is an LLM agent "
        "(agents/critic.md): it needs ANTHROPIC_API_KEY and is non-deterministic, "
        "so it runs manually, not in CI. Implement the Agent-SDK call in "
        "api_backend() to populate the real baseline in "
        "docs/critic-false-pass-baseline.md; do not fabricate a number."
    )


BACKENDS: dict[str, Backend] = {"mock": mock_backend, "api": api_backend}


# --- the oracle -------------------------------------------------------------

@dataclass(frozen=True)
class Report:
    """Aggregate result of running one backend over the whole corpus."""

    backend: str
    results: list[dict]   # one row per case: id, label, defect_class, verdict, false_*
    n_bad: int
    n_good: int
    false_passes: int     # bad cases the critic PASSED (waved through)
    false_fails: int      # good cases the critic FAILED (rejected)

    @property
    def false_pass_rate(self) -> float:
        return round(self.false_passes / self.n_bad, 4) if self.n_bad else 0.0

    @property
    def false_fail_rate(self) -> float:
        return round(self.false_fails / self.n_good, 4) if self.n_good else 0.0

    def to_dict(self) -> dict:
        return {
            "backend": self.backend,
            "n_cases": self.n_bad + self.n_good,
            "n_bad": self.n_bad,
            "n_good": self.n_good,
            "false_passes": self.false_passes,
            "false_pass_rate": self.false_pass_rate,
            "false_fails": self.false_fails,
            "false_fail_rate": self.false_fail_rate,
            "results": self.results,
        }


def run(backend: Backend, backend_name: str, cases: list[Case]) -> Report:
    """Run ``backend`` over every case and compute the false-pass/fail rates."""
    results: list[dict] = []
    false_passes = false_fails = n_bad = n_good = 0
    for case in cases:
        verdict = backend(case)
        false_pass = case.is_bad and verdict is Verdict.PASS
        false_fail = (not case.is_bad) and verdict is Verdict.FAIL
        if case.is_bad:
            n_bad += 1
            false_passes += int(false_pass)
        else:
            n_good += 1
            false_fails += int(false_fail)
        results.append({
            "id": case.id,
            "label": case.label,
            "defect_class": case.defect_class,
            "verdict": verdict.value,
            "false_pass": false_pass,
            "false_fail": false_fail,
        })
    return Report(
        backend=backend_name,
        results=results,
        n_bad=n_bad,
        n_good=n_good,
        false_passes=false_passes,
        false_fails=false_fails,
    )


# --- rendering / CLI --------------------------------------------------------

def render_text(report: Report) -> str:
    lines = [
        f"Critic false-pass harness — backend: {report.backend}",
        f"  corpus: {report.n_bad} bad, {report.n_good} good ({report.n_bad + report.n_good} total)",
        "",
        f"  {'case':<22} {'label':<6} {'defect_class':<20} {'verdict':<7} flag",
        f"  {'-' * 22} {'-' * 6} {'-' * 20} {'-' * 7} ----",
    ]
    for r in report.results:
        flag = "FALSE-PASS" if r["false_pass"] else ("false-fail" if r["false_fail"] else "")
        lines.append(
            f"  {r['id']:<22} {r['label']:<6} {r['defect_class']:<20} "
            f"{r['verdict']:<7} {flag}"
        )
    lines += [
        "",
        f"  false_pass_rate = {report.false_passes}/{report.n_bad} = {report.false_pass_rate}"
        "   (bad diffs waved through — the headline number)",
        f"  false_fail_rate = {report.false_fails}/{report.n_good} = {report.false_fail_rate}"
        "   (good diffs wrongly rejected)",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Critic false-pass harness (SL-2)")
    ap.add_argument("--backend", choices=sorted(BACKENDS), default="mock",
                    help="critic backend: 'mock' (deterministic) or 'api' (real LLM critic)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = ap.parse_args(argv)

    cases = load_corpus()
    report = run(BACKENDS[args.backend], args.backend, cases)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
