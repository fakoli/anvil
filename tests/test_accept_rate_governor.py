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

import pytest

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


def _ev(task_id: str, actor: str, submitted_at=None):  # type: ignore[no-untyped-def]
    # Default submitted_at far in the past so a single submission is always
    # "current" at any in-window decision; rework tests pass explicit times.
    return types.SimpleNamespace(
        task_id=task_id,
        submitted_by=actor,
        submitted_at=submitted_at or (_NOW - datetime.timedelta(days=40)),
    )


def _metrics(
    decisions,
    evidence,
    *,
    needs_review=0,
    floor=0.80,
    cap=10,
    window_days=7.0,
    as_of=_NOW,
):  # type: ignore[no-untyped-def]
    return AcceptRateMetrics(
        _StubBackend(decisions, evidence, needs_review),  # type: ignore[arg-type]
        FrozenClock(as_of),
        window_days=window_days,
        floor=floor,
        needs_review_cap=cap,
        as_of=as_of,
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


def test_accept_rate_does_not_merge_legacy_normalized_spellings() -> None:
    """Replay/read metrics key exact stored bytes, never an NFC alias."""
    decomposed = "cafe\u0301"
    composed = "caf\u00e9"
    metrics = _metrics(
        [("T1", "rejected", _iso(1)), ("T2", "accepted", _iso(1))],
        [_ev("T1", decomposed), _ev("T2", composed)],
    )
    assert metrics.accept_rate(decomposed) == 0.0
    assert metrics.accept_rate(composed) == 1.0


def test_rework_attributes_each_decision_to_the_runner_who_earned_it() -> None:
    """Rework cycle: A submits T (rejected), then B re-submits T (accepted).
    A owns the rejection, B owns the acceptance — NOT both credited to the
    latest submitter (the blind-review bug)."""
    t_a_submit = _NOW - datetime.timedelta(days=3)
    t_a_reject = _NOW - datetime.timedelta(days=2)
    t_b_submit = _NOW - datetime.timedelta(days=1, hours=12)
    t_b_accept = _NOW - datetime.timedelta(days=1)
    decisions = [
        ("T", "rejected", t_a_reject.isoformat()),
        ("T", "accepted", t_b_accept.isoformat()),
    ]
    evidence = [_ev("T", "A", t_a_submit), _ev("T", "B", t_b_submit)]
    m = _metrics(decisions, evidence)
    assert m.accept_rate("A") == 0.0  # A's only reviewed submission was rejected
    assert m.accept_rate("B") == 1.0  # B's only reviewed submission was accepted


def test_each_finalized_quality_attempt_is_one_counting_unit() -> None:
    decisions = [
        ("T", "rejected", _iso(3), "R1"),
        ("T", "rejected", _iso(2), "R2"),
        ("T", "accepted", _iso(1), "R3"),
    ]
    metrics = _metrics(decisions, [_ev("T", "A")])
    assert metrics.acceptance_counts("A") == (1, 3)
    assert metrics.accept_rate("A") == 1 / 3


def test_accept_rate_excludes_decisions_outside_window() -> None:
    decisions = [
        ("T1", "accepted", _iso(1)),   # in window
        ("T2", "rejected", _iso(30)),  # outside the 7-day window
    ]
    evidence = [_ev("T1", "A"), _ev("T2", "A")]
    # Only T1 counts -> 1.0, not 0.5.
    assert _metrics(decisions, evidence).accept_rate("A") == 1.0


def test_window_is_inclusive_excludes_future_and_orders_equal_timestamps() -> None:
    cutoff = _NOW - datetime.timedelta(days=7)
    decisions = [
        ("T-future", "accepted", (_NOW + datetime.timedelta(microseconds=1)).isoformat(), "R9"),
        ("T-now", "rejected", _NOW.isoformat(), "R2"),
        ("T-tie", "accepted", _NOW.isoformat(), "R1"),
        ("T-cutoff", "accepted", cutoff.isoformat(), "R0"),
        (
            "T-outside",
            "rejected",
            (cutoff - datetime.timedelta(microseconds=1)).isoformat(),
            "R8",
        ),
    ]
    evidence = [_ev(task_id, "A") for task_id, *_rest in decisions]
    metrics = _metrics(decisions, evidence)

    assert metrics.acceptance_counts("A") == (2, 3)
    assert metrics.accept_rate("A") == 2 / 3
    assert [item[3] for item in metrics._decisions or []] == ["R0", "R1", "R2"]


def test_same_as_of_is_deterministic_across_input_order() -> None:
    decisions = [
        ("T1", "accepted", _iso(1), "R1"),
        ("T2", "rejected", _iso(1), "R2"),
    ]
    evidence = [_ev("T1", "A"), _ev("T2", "A")]
    forward = _metrics(decisions, evidence).projection("A")
    reverse = _metrics(list(reversed(decisions)), evidence).projection("A")
    assert forward == reverse
    assert forward["as_of"] == "2026-06-21T12:00:00Z"
    assert forward["window_start"] == "2026-06-14T12:00:00Z"


def test_projection_reports_truthful_counts_floor_window_and_recovery() -> None:
    projection = _metrics(
        [("T1", "accepted", _iso(1)), ("T2", "rejected", _iso(1))],
        [_ev("T1", "A"), _ev("T2", "A")],
        floor=0.75,
        cap=4,
        needs_review=2,
        window_days=14,
    ).projection("A")

    assert projection == {
        "as_of": "2026-06-21T12:00:00Z",
        "window_days": 14,
        "window_start": "2026-06-07T12:00:00Z",
        "numerator": 1,
        "denominator": 2,
        "rate": 0.5,
        "floor": 0.75,
        "configured_floor": 0.75,
        "needs_review_depth": 2,
        "needs_review_cap": 4,
        "guidance": projection["guidance"],
    }
    guidance = str(projection["guidance"])
    assert "clearing the review queue alone does not restore" in guidance
    assert "accepted finalized reviews" in guidance
    assert "expiry" in guidance
    assert "configured floor" in guidance
    assert "direct claim of a known task ID" in guidance
    assert "ownership, conflict, PRD, risk, and evidence gates" in guidance


def test_zero_denominator_and_floor_endpoints_use_exact_comparison() -> None:
    assert _metrics([], [], floor=1).projection("new")["rate"] is None
    assert _metrics([], [], floor=1).actor_below_floor("new") is False

    rejected = [("T1", "rejected", _iso(1))]
    evidence = [_ev("T1", "A")]
    assert _metrics(rejected, evidence, floor=0).actor_below_floor("A") is False
    assert _metrics(rejected, evidence, floor=1).actor_below_floor("A") is True

    escalated = [("T9", "rejected", _iso(i)) for i in range(3)]
    escalated_metrics = _metrics(escalated, [_ev("T9", "A")], floor=1)
    assert escalated_metrics.required_floor("T9") == 1.0

    one_of_three = [
        ("T1", "accepted", _iso(1)),
        ("T2", "rejected", _iso(1)),
        ("T3", "rejected", _iso(1)),
    ]
    one_of_three_evidence = [_ev("T1", "A"), _ev("T2", "A"), _ev("T3", "A")]
    assert (
        _metrics(
            one_of_three,
            one_of_three_evidence,
            floor=0.3333333333333333,
        ).actor_below_floor("A")
        is False
    )


@pytest.mark.parametrize("floor", [-0.01, 1.01, float("nan"), float("inf")])
def test_invalid_floor_fails_closed(floor: float) -> None:
    with pytest.raises(ValueError, match="floor must be between 0 and 1"):
        _metrics([], [], floor=floor)


@pytest.mark.parametrize("window", [0, -1, float("nan"), float("inf")])
def test_invalid_window_fails_closed(window: float) -> None:
    with pytest.raises(ValueError, match="window_days must be finite and positive"):
        _metrics([], [], window_days=window)


def test_future_evidence_cannot_backfill_legacy_review_attribution() -> None:
    decision_at = _NOW - datetime.timedelta(seconds=1)
    future_submit = _NOW + datetime.timedelta(seconds=1)
    metrics = _metrics(
        [("T", "rejected", decision_at.isoformat(), "RV-legacy")],
        [_ev("T", "future-actor", future_submit)],
    )

    assert metrics.acceptance_counts("future-actor") == (0, 0)


def test_persisted_attempt_actor_wins_same_timestamp_ties() -> None:
    decisions = [
        ("T", "rejected", _NOW.isoformat(), "RV-1", "EV-a", "actor-b"),
    ]
    evidence = [
        _ev("T", "actor-a", _NOW),
        _ev("T", "actor-b", _NOW),
    ]
    metrics = _metrics(decisions, evidence)

    assert metrics.acceptance_counts("actor-a") == (0, 0)
    assert metrics.acceptance_counts("actor-b") == (0, 1)


def test_non_quality_rejections_do_not_affect_governor_metrics(
    tmp_path: Path,
) -> None:
    """The authoritative review query excludes process/evidence resubmissions."""
    from anvil.state.sqlite import SqliteBackend

    events_path = tmp_path / "events.jsonl"
    events_path.touch()
    backend = SqliteBackend(
        db_path=str(tmp_path / "state.db"),
        events_path=str(events_path),
        clock=FrozenClock(_NOW),
    )
    backend.initialize()
    try:
        conn = sqlite3.connect(str(tmp_path / "state.db"))
        for index, (task_id, decision, category, counts) in enumerate(
            [
                ("T-EVIDENCE", "rejected", "evidence_resubmission", 0),
                ("T-QUALITY", "rejected", "quality", 1),
                ("T-ACCEPT", "accepted", None, 1),
            ],
            start=1,
        ):
            timestamp = (_NOW - datetime.timedelta(minutes=index)).isoformat()
            conn.execute(
                "INSERT INTO reviews "
                "(id, target_kind, target_id, reviewed_by, decision, notes, "
                "rejection_category, counts_toward_accept_rate, created_at) "
                "VALUES (?, 'task', ?, 'reviewer', ?, NULL, ?, ?, ?)",
                (f"RV-{index}", task_id, decision, category, counts, timestamp),
            )
            conn.execute(
                "INSERT INTO evidence "
                "(id, task_id, claim_id, commands_run, output_excerpt, "
                "files_changed, pr_url, commit_sha, screenshots, "
                "known_limitations, submitted_at, submitted_by) "
                "VALUES (?, ?, ?, '[]', NULL, '[]', NULL, NULL, '[]', "
                "NULL, ?, 'worker-a')",
                (f"EV-{index}", task_id, f"C-{index}", timestamp),
            )
        conn.commit()
        conn.close()

        metrics = AcceptRateMetrics(backend, FrozenClock(_NOW))
        assert metrics.accept_rate("worker-a") == 0.5
        assert metrics.rejection_count("T-EVIDENCE") == 0
        assert metrics.rejection_count("T-QUALITY") == 1
    finally:
        backend.close()


# -- review-debt cap ---------------------------------------------------------


def test_review_queue_saturation() -> None:
    assert _metrics([], [], needs_review=10, cap=10).review_queue_saturated() is True
    assert _metrics([], [], needs_review=9, cap=10).review_queue_saturated() is False


def test_withhold_reason_distinguishes_governed_withhold_from_empty() -> None:
    # saturated queue
    assert (
        _metrics([], [], needs_review=10, cap=10).withhold_reason("a")
        == "review_queue_saturated"
    )
    # actor below floor (2 rejected, rate 0.0 < 0.80)
    decisions = [("T1", "rejected", _iso(1)), ("T2", "rejected", _iso(1))]
    evidence = [_ev("T1", "lo"), _ev("T2", "lo")]
    assert _metrics(decisions, evidence).withhold_reason("lo") == "actor_below_floor"
    # nothing wrong -> None (a genuinely empty queue, not a governed withhold)
    assert _metrics([], []).withhold_reason("newcomer") is None


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


def test_direct_known_task_claim_bypasses_only_offer_throttling(tmp_path: Path) -> None:
    from tests.test_claims import (
        _insert_feature_raw,
        _insert_task_raw,
        _make_backend,
        _make_clock,
        _make_manager,
        _setup_prd,
        _setup_project,
    )

    clock = _make_clock()
    backend = _make_backend(tmp_path, clock)
    _setup_project(backend)
    _setup_prd(backend, approve=True)
    conn = sqlite3.connect(str(tmp_path / "state.db"))
    _insert_feature_raw(conn)
    _insert_task_raw(conn, task_id="T001", status="ready")
    _insert_task_raw(conn, task_id="T002", status="ready")
    _insert_task_raw(conn, task_id="T003", status="ready")
    conn.close()
    try:
        other = _make_manager(backend, actor="other", clock=clock)
        other.claim("T001", expected_files=["src/shared.py"])
        manager = _make_manager(backend, actor="low", clock=clock)
        governor = _metrics(
            [("T-old", "rejected", _iso(1))],
            [_ev("T-old", "low")],
        )
        assert governor.actor_below_floor("low") is True
        assert manager.next_claimable(metrics=governor) is None

        result = manager.claim("T003")
        assert result.claim.task_id == "T003"
        assert result.claim.claimed_by == "low"

        from anvil.claims.manager import ClaimError

        with pytest.raises(ClaimError, match="conflict"):
            manager.claim("T002", expected_files=["src/shared.py"])
    finally:
        backend.close()


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
