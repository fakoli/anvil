"""B51 — packet-quality measurement harness.

Quantifies the token reduction of right-sized (lightweight) vs full work packets
so packet quality is a tracked, measured workstream rather than an assertion.
Runnable as a pytest test and as a standalone script
(`python -m anvil.context.packet_metrics <state_dir>`).
"""

from __future__ import annotations

import datetime

from anvil.context.packet_metrics import measure_backlog, measure_task
from anvil.state.models import Score, Task

_NOW = datetime.datetime(2026, 6, 21, 12, 0, 0, tzinfo=datetime.UTC)


def _task(task_id: str, complexity: int | None, blast: int | None) -> Task:
    return Task(
        id=task_id,
        feature_id="F1",
        title=f"Task {task_id}",
        description="Do the thing carefully and well. " * 12,
        scores=Score(complexity=complexity, blast_radius=blast),
        acceptance_criteria=[f"criterion {i}" for i in range(5)],
        implementation_notes=[f"constraint note {i}" for i in range(4)],
        likely_files=[f"src/mod_{i}.py" for i in range(3)],
        created_at=_NOW,
        updated_at=_NOW,
    )


def test_lightweight_packet_is_smaller_than_full() -> None:
    sizing = measure_task(_task("T1", complexity=1, blast=1))
    assert sizing.lightweight_tokens < sizing.full_tokens
    assert sizing.reduction_tokens > 0
    assert 0 < sizing.reduction_pct <= 100


def test_routed_variant_follows_score() -> None:
    # low complexity + low blast routes lightweight; high routes full.
    assert measure_task(_task("T1", 1, 1)).routed_variant == "lightweight"
    assert measure_task(_task("T2", 5, 5)).routed_variant == "full"
    # unscored routes full (is_lightweight is conservative when a dim is None).
    assert measure_task(_task("T3", None, None)).routed_variant == "full"


def test_measure_backlog_aggregates() -> None:
    tasks = [_task("T1", 1, 1), _task("T2", 5, 5), _task("T3", 2, 2)]
    report = measure_backlog(tasks)
    assert report["total_tasks"] == 3
    assert report["routed_lightweight"] >= 1
    # routing already saves tokens vs sending everything full.
    assert report["as_routed_total_tokens"] <= report["all_full_total_tokens"]
    assert report["as_routed_savings_pct"] >= 0.0
    assert len(report["per_task"]) == 3
    assert {"task_id", "routed_variant", "reduction_pct"} <= set(report["per_task"][0])


def test_measure_backlog_empty_is_safe() -> None:
    report = measure_backlog([])
    assert report["total_tasks"] == 0
    assert report["as_routed_savings_pct"] == 0.0  # no div-by-zero


def test_standalone_main_usage_without_args() -> None:
    from anvil.context.packet_metrics import _main

    assert _main([]) == 2  # prints usage, non-zero exit
