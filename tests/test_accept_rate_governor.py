"""B49 — accept-rate governor + review-debt cap.

The metric logic is unit-tested against a tiny stub backend; the gating wiring
(``next_claimable`` returns nothing when the governor trips) is checked against a
real backend.
"""

from __future__ import annotations

import datetime
import sqlite3
import types
from pathlib import Path

from anvil.claims.metrics import AcceptRateMetrics
from anvil.clock import FrozenClock

_NOW = datetime.datetime(2026, 6, 21, 12, 0, 0, tzinfo=datetime.UTC)


def _iso(days_ago: float) -> str:
    return (_NOW - datetime.timedelta(days=days_ago)).isoformat()


class _StubBackend:
    """Exposes only what AcceptRateMetrics reads."""

    def __init__(self, decisions, evidence, needs_review=0):  # type: ignore[no-untyped-def]
        self._decisions = decisions  # list[(task_id, decision, iso)]
        self._evidence = evidence  # list[ns(task_id, submitted_by)]
        self._needs_review = needs_review

    def list_task_review_decisions(self):  # type: ignore[no-untyped-def]
        return list(self._decisions)

    def list_evidence(self):  # type: ignore[no-untyped-def]
        return list(self._evidence)

    def list_tasks(self, status=None, **_kw):  # type: ignore[no-untyped-def]
        from anvil.state.models import TaskStatus

        if status == TaskStatus.needs_review:
            return [object()] * self._needs_review
        return []


def _ev(task_id: str, actor: str):  # type: ignore[no-untyped-def]
    return types.SimpleNamespace(task_id=task_id, submitted_by=actor)


def _metrics(decisions, evidence, *, needs_review=0, floor=0.80, cap=10):  # type: ignore[no-untyped-def]
    return AcceptRateMetrics(
        _StubBackend(decisions, evidence, needs_review),  # type: ignore[arg-type]
        FrozenClock(_NOW),
        window_days=7.0,
        floor=floor,
        needs_review_cap=cap,
    )


# -- accept rate (per work-actor) -------------------------------------------


def test_accept_rate_is_per_work_actor() -> None:
    # A: 3 accepted + 1 rejected = 0.75; B: 1 accepted = 1.0; C: no history.
    decisions = [
        ("T1", "accepted", _iso(1)),
        ("T2", "accepted", _iso(1)),
        ("T3", "accepted", _iso(1)),
        ("T4", "rejected", _iso(1)),
        ("T5", "accepted", _iso(1)),
    ]
    evidence = [
        _ev("T1", "A"), _ev("T2", "A"), _ev("T3", "A"), _ev("T4", "A"),
        _ev("T5", "B"),
    ]
    m = _metrics(decisions, evidence)
    assert m.accept_rate("A") == 0.75
    assert m.accept_rate("B") == 1.0
    assert m.accept_rate("C") is None  # no reviewed history


def test_accept_rate_excludes_decisions_outside_window() -> None:
    decisions = [
        ("T1", "accepted", _iso(1)),   # in window
        ("T2", "rejected", _iso(30)),  # outside the 7-day window
    ]
    evidence = [_ev("T1", "A"), _ev("T2", "A")]
    # Only T1 counts -> 1.0, not 0.5.
    assert _metrics(decisions, evidence).accept_rate("A") == 1.0


# -- review-debt cap ---------------------------------------------------------


def test_review_queue_saturation() -> None:
    assert _metrics([], [], needs_review=10, cap=10).review_queue_saturated() is True
    assert _metrics([], [], needs_review=9, cap=10).review_queue_saturated() is False


# -- floor + escalation ------------------------------------------------------


def test_actor_below_floor_only_with_history() -> None:
    decisions = [("T1", "rejected", _iso(1)), ("T2", "rejected", _iso(1))]
    evidence = [_ev("T1", "A"), _ev("T2", "A")]
    m = _metrics(decisions, evidence, floor=0.80)
    assert m.actor_below_floor("A") is True  # rate 0.0 < 0.80
    assert m.actor_below_floor("newcomer") is False  # no history -> benefit of doubt


def test_task_escalates_after_threshold_rejections() -> None:
    # T9 rejected 3 times -> escalated floor 0.95.
    decisions = [("T9", "rejected", _iso(i)) for i in range(3)]
    evidence = [_ev("T9", "X")]
    m = _metrics(decisions, evidence)
    assert m.rejection_count("T9") == 3
    assert m.required_floor("T9") == 0.95
    # A new actor (no history) is blocked from an escalated task.
    assert m.task_blocked_for_actor("T9", "newcomer") is True


def test_escalated_task_allows_only_proven_high_actor() -> None:
    # "pro" has 20 acceptances (rate 1.0); T9 has been rejected 3x (escalated).
    decisions = [(f"P{i}", "accepted", _iso(0)) for i in range(20)] + [
        ("T9", "rejected", _iso(0)) for _ in range(3)
    ]
    evidence = [_ev(f"P{i}", "pro") for i in range(20)]
    m = _metrics(decisions, evidence)
    assert m.accept_rate("pro") == 1.0
    assert m.task_blocked_for_actor("T9", "pro") is False  # 1.0 >= escalation 0.95


def test_base_floor_blocks_only_proven_low_actor() -> None:
    decisions = [("Tlow", "rejected", _iso(0)), ("Tlow2", "rejected", _iso(0))]
    evidence = [_ev("Tlow", "lo"), _ev("Tlow2", "lo")]
    m = _metrics(decisions, evidence)
    assert m.task_blocked_for_actor("Tbase", "lo") is True  # rate 0.0 < 0.80
    assert m.task_blocked_for_actor("Tbase", "newcomer") is False  # no history


# -- integration: next_claimable trips the governor --------------------------


def test_next_claimable_returns_none_when_review_queue_saturated(
    tmp_path: Path,
) -> None:
    from tests.test_claims import (  # reuse the claims harness
        _insert_feature_raw,
        _insert_task_raw,
        _make_backend,
        _make_clock,
        _make_manager,
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
    conn.close()
    try:
        mgr = _make_manager(b, clock=clock)
        # Without the governor, the ready task is returned.
        assert mgr.next_claimable() is not None
        # cap=0 forces review_queue_saturated() True -> no new work offered.
        saturated = AcceptRateMetrics(b, clock, needs_review_cap=0)
        assert mgr.next_claimable(metrics=saturated) is None
    finally:
        b.close()


def test_config_loads_governor_knobs(tmp_path: Path) -> None:
    from anvil.config import load_config

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "project_name: t\nproject_id: t\n"
        "accept_rate_floor: 0.5\nneeds_review_cap: 3\naccept_rate_window_days: 14\n",
        encoding="utf-8",
    )
    loaded = load_config(cfg)
    assert loaded.accept_rate_floor == 0.5
    assert loaded.needs_review_cap == 3
    assert loaded.accept_rate_window_days == 14.0
