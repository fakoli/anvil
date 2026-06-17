#!/usr/bin/env python3
"""Entrypoint for the fakoli-state coordination benchmark.

    python run_benchmark.py                 # all scenarios, 3 trials -> RESULTS.md
    python run_benchmark.py --quick         # fast smoke (1 trial, fewer actors)
    python run_benchmark.py --scenarios overlapping_files,evidence_gaming
    python run_benchmark.py --live          # (phase-2 stub) real subagents

No third-party dependencies; drives the real fakoli-state CLI via its synced venv.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness.runner import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
