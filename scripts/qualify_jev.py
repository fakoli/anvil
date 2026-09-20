"""Synthetic, explicitly live Jev qualification; no environment-file discovery.

Run with uv run --project bin python scripts/qualify_jev.py --help.
Expected labels never enter a request. All misses and provider failures remain
in the report. This small synthetic corpus is not a production accuracy claim.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path

from anvil.jev import CAPABILITIES, JevConfig, evaluate
from anvil.jev_questions import build_questions


def matches(answers: dict, expected: dict) -> bool:
    for key, expectation in expected.items():
        answer = answers.get(key, {}).get(expectation["field"])
        if "equals" in expectation and answer != expectation["equals"]:
            return False
        if "min" in expectation and (
            not isinstance(answer, (float, int)) or answer < expectation["min"]
        ):
            return False
        if "max" in expectation and (
            not isinstance(answer, (float, int)) or answer > expectation["max"]
        ):
            return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cases", type=Path, default=Path("tests/fixtures/jev/cases.json")
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--live",
        action="store_true",
        help="Send only the selected synthetic corpus to TypeSafe.",
    )
    args = parser.parse_args()
    if args.output.exists():
        parser.error(
            "output exists; retain earlier results and choose a new report path"
        )
    raw = args.cases.read_bytes()
    cases = json.loads(raw)
    requests = [
        (case, *build_questions(case["capability"], case["input"])) for case in cases
    ]
    config = JevConfig(enabled=args.live, capabilities=CAPABILITIES)
    report = {
        "schema": "anvil.jev.qualification.v1",
        "synthetic_only": True,
        "live": args.live,
        "model": config.model,
        "started_at": datetime.now(UTC).isoformat(),
        "corpus_sha256": hashlib.sha256(raw).hexdigest(),
        "cases": [],
    }
    latencies = []
    totals = {"input_tokens": 0, "output_tokens": 0}
    started = time.monotonic()
    for case, state, questions in requests:
        annotation = evaluate(
            config, case["capability"], state, questions, allow_api=args.live
        )
        matched = annotation["used"] and matches(
            annotation["answers"], case["expected"]
        )
        report["cases"].append(
            {
                **case,
                "questions": questions,
                "annotation": annotation,
                "matches_expected": matched if args.live else None,
            }
        )
        if annotation["used"]:
            latencies.append(annotation["elapsed_ms"])
            for key in totals:
                totals[key] += annotation["usage"][key]
    report["summary"] = {
        "case_count": len(cases),
        "completed": len(latencies),
        "matches_expected": sum(
            row["matches_expected"] is True for row in report["cases"]
        ),
        "usage": totals,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "latency_ms": {
            "min": min(latencies),
            "median": statistics.median(latencies),
            "max": max(latencies),
        }
        if latencies
        else {},
        "limitations": (
            "Synthetic illustrative corpus; not held-out production accuracy, "
            "calibration, or a security proof."
        ),
    }
    sources = [
        "bin/src/anvil/jev.py",
        "bin/src/anvil/jev_questions.py",
        "scripts/qualify_jev.py",
    ]
    report["source_sha256"] = {
        name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in sources
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(report, output, indent=2, ensure_ascii=False, allow_nan=False)
        output.write("\n")
    print(json.dumps(report["summary"]))
    return 0 if not args.live or len(latencies) == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
