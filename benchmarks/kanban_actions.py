"""Bounded synthetic Kanban throughput; never reads or mutates operator State.

Run from the checkout: uv run --project bin python benchmarks/kanban_actions.py
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_sqlite import (  # noqa: E402
    _make_backend,
    _make_event,
    _make_task_payload,
    _setup_claimable_task,
)

from anvil.clock import SystemClock  # noqa: E402
from anvil.state.schema import get_schema_version  # noqa: E402
from anvil.state.sqlite import SqliteBackend  # noqa: E402
from anvil.task_actions import transition_task  # noqa: E402
from anvil.web import Repository  # noqa: E402
from anvil.web_actions import inspect_repository  # noqa: E402


def _summary(samples: list[float], elapsed: float) -> dict[str, int | float]:
    ordered = sorted(samples)
    return {
        "operations": len(samples),
        "elapsed_seconds": round(elapsed, 6),
        "median_ms": round(statistics.median(samples) * 1000, 3),
        "p95_ms": round(ordered[math.ceil(len(ordered) * 0.95) - 1] * 1000, 3),
        "operations_per_second": round(len(samples) / elapsed, 3),
    }


def _open(state: Path) -> SqliteBackend:
    backend = SqliteBackend(
        db_path=str(state / "state.db"),
        events_path=str(state / "events.jsonl"),
        clock=SystemClock(),
        project_root=state.parent,
    )
    backend.initialize()
    return backend


def _readback(state: Path, task_ids: list[str], initial_events: int) -> dict[str, int]:
    backend = _open(state)
    try:
        assert all(
            backend.get_task(task_id).status.value == "drafted" for task_id in task_ids
        )
    finally:
        backend.close()
    events = [
        json.loads(line) for line in (state / "events.jsonl").read_text().splitlines()
    ]
    added = events[initial_events:]
    assert len(added) == len(task_ids)
    assert all(event["action"] == "task.transitioned" for event in added)
    assert sorted(event["target_id"] for event in added) == sorted(task_ids)
    return {"verified_tasks": len(task_ids), "verified_transition_events": len(added)}


def _native_arm(
    state: Path, task_ids: list[str], workers: int
) -> dict[str, int | float]:
    def run_partition(partition: list[str]) -> list[float]:
        backend = _open(state)
        samples = []
        try:
            for task_id in partition:
                started = time.perf_counter()
                task = backend.get_task(task_id)
                changed = transition_task(
                    backend,
                    task_id,
                    "backlog",
                    "benchmark-human",
                    expected_status=task.status.value,
                    expected_updated_at=task.updated_at,
                )
                assert changed.id == task_id and changed.status.value == "drafted"
                samples.append(time.perf_counter() - started)
        finally:
            backend.close()
        return samples

    started = time.perf_counter()
    if workers == 1:
        samples = run_partition(task_ids)
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            partitions = [task_ids[index::workers] for index in range(workers)]
            samples = [
                duration
                for result in executor.map(run_partition, partitions)
                for duration in result
            ]
    return {**_summary(samples, time.perf_counter() - started), "workers": workers}


def run(task_count: int, operation_count: int, workers: int) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="anvil-kanban-benchmark-") as temporary:
        root = Path(temporary)
        source_state = root / "seed" / ".anvil"
        source_state.mkdir(parents=True)
        fixture_task_ids = [
            "T001",
            *(f"T002.{index}" for index in range(1, task_count)),
        ]
        backend = _make_backend(source_state)
        try:
            _setup_claimable_task(backend)
            for task_id in fixture_task_ids[1:]:
                backend.append(
                    _make_event(
                        "task.created",
                        _make_task_payload(task_id=task_id, status="ready"),
                        target_kind="task",
                        target_id=task_id,
                    )
                )
        finally:
            backend.close()
        initial_events = len((source_state / "events.jsonl").read_text().splitlines())
        repository = Repository(
            id="a" * 24,
            label="Synthetic benchmark",
            root=str(source_state.parent),
            project_id="proj-1",
            reader_label="Synthetic local-layout fixture",
            reader_argv=[
                sys.executable,
                "-I",
                "-m",
                "anvil.cli",
                "project",
                "snapshot",
                "--json",
            ],
            expected_schema_version=get_schema_version(),
        )
        # The explicit temporary checkout uses the supported local layout. No
        # HOME override, native catalog, credentials, services or global State.
        with patch.dict(os.environ, {"ANVIL_STATE_LAYOUT": "local"}):
            snapshots = []
            started = time.perf_counter()
            for _ in range(5):
                sample_started = time.perf_counter()
                board = inspect_repository(repository, "benchmark-human")
                snapshots.append(time.perf_counter() - sample_started)
                assert len(board["snapshot"]["payload"]["tasks"]) == task_count
                assert len(board["workflow"]["actions"]) == task_count
                assert all(
                    "backlog" in {action["id"] for action in actions}
                    for actions in board["workflow"]["actions"].values()
                )
            snapshot_summary = _summary(snapshots, time.perf_counter() - started)
            snapshot_summary["tasks_per_board"] = task_count
            results: dict[str, object] = {
                "snapshot_and_action_availability": snapshot_summary
            }
            task_ids = fixture_task_ids[:operation_count]
            for name, worker_count in (
                ("native_serial", 1),
                ("native_concurrent", workers),
            ):
                state = root / name / ".anvil"
                shutil.copytree(source_state, state)
                measurement = _native_arm(state, task_ids, worker_count)
                measurement.update(_readback(state, task_ids, initial_events))
                results[name] = measurement

            state = root / "cli_serial" / ".anvil"
            shutil.copytree(source_state, state)
            samples = []
            started = time.perf_counter()
            cli_task_ids = task_ids[:10]
            for task_id in cli_task_ids:
                sample_started = time.perf_counter()
                result = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        "-m",
                        "anvil.cli",
                        "task-action",
                        task_id,
                        "backlog",
                        "--actor",
                        "benchmark-human",
                        "--cwd",
                        str(state.parent),
                        "--json",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=True,
                )
                response = json.loads(result.stdout)
                assert response["ok"] and response["data"]["task_id"] == task_id
                assert response["data"]["status"] == "drafted"
                samples.append(time.perf_counter() - sample_started)
            results["cli_serial"] = {
                **_summary(samples, time.perf_counter() - started),
                **_readback(state, cli_task_ids, initial_events),
            }
        return {
            "schema": "anvil.kanban.benchmark.v1",
            "observed_at": datetime.now(UTC).isoformat(),
            "python_version": sys.version.split()[0],
            "state_schema": get_schema_version(),
            "task_count": task_count,
            "operation_count_per_native_arm": operation_count,
            "results": results,
            "validated": True,
            "scope": "Synthetic local SQLite/JSONL state; readiness moves, no evidence approvals.",
            "limits": [
                "Snapshot timing covers native projection and availability, "
                "not DOM rendering or HTTP.",
                "Native arms use one connection per worker; CLI timing includes process startup.",
                "Each transition arm begins from the same copied fixture; setup is excluded.",
                "No latency threshold or multi-user deployment capacity claim.",
            ],
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=int, default=1000)
    parser.add_argument("--operations", type=int, default=100)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    if (
        not 1 <= arguments.operations <= arguments.tasks <= 2000
        or not 1 <= arguments.workers <= 8
    ):
        parser.error("require 1 <= operations <= tasks <= 2000 and 1 <= workers <= 8")
    result = run(arguments.tasks, arguments.operations, arguments.workers)
    text = json.dumps(result, indent=2) + "\n"
    if arguments.output:
        arguments.output.write_text(text, encoding="utf-8")
    print(text, end="")


if __name__ == "__main__":
    main()
