"""B50 — the bake-off metrics snapshot tool.

Verifies the live, in-engine snapshot (review debt + accept-rate + packet sizing)
that the two-week bake-off logs daily. The harness lives at
``benchmarks/bakeoff_snapshot.py``; mirror test_critic_falsepass's bootstrap to
import it.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_BENCHMARKS = Path(__file__).resolve().parents[1] / "benchmarks"
if str(_BENCHMARKS) not in sys.path:
    sys.path.insert(0, str(_BENCHMARKS))

import bakeoff_snapshot as bo  # noqa: E402


def test_snapshot_reports_review_debt_and_packet_sizing(tmp_path: Path) -> None:
    from tests.test_claims import (
        _insert_feature_raw,
        _insert_task_raw,
        _make_backend,
        _make_clock,
        _setup_prd,
        _setup_project,
    )

    clock = _make_clock()
    b = _make_backend(tmp_path, clock)
    _setup_project(b)
    _setup_prd(b)
    conn = sqlite3.connect(str(tmp_path / "state.db"))
    _insert_feature_raw(conn)
    _insert_task_raw(conn, task_id="T001", status="ready")
    _insert_task_raw(conn, task_id="T002", status="needs_review")
    conn.close()
    b.close()  # snapshot opens its own backend

    snap = bo.snapshot(tmp_path)
    assert snap["needs_review_depth"] == 1
    assert snap["task_status_counts"]["ready"] == 1
    assert snap["task_status_counts"]["needs_review"] == 1
    assert snap["packet_sizing"]["total_tasks"] == 2
    assert snap["packet_sizing"]["as_routed_savings_pct"] >= 0.0
    assert "accept_rate_by_runner" in snap


def test_snapshot_main_usage_without_args() -> None:
    assert bo._main([]) == 2  # prints usage, non-zero exit
