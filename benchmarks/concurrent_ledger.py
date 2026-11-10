"""Disposable process contention benchmark; setup and process startup excluded.

uv run --project bin python benchmarks/concurrent_ledger.py --output /tmp/ledger.json
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import multiprocessing
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks import kanban_actions as fixture  # noqa: E402

from anvil.task_actions import transition_task  # noqa: E402


def _writer(state, task_ids, ready, start, results):
    backend = None
    try:
        backend = fixture._open(Path(state))
        ready.put(True)
        if not start.wait(30):
            raise TimeoutError("Writer start timed out")
        started = time.perf_counter()
        cpu_started = time.process_time()
        samples = []
        for task_id in task_ids:
            before = time.perf_counter()
            task = backend.get_task(task_id)
            changed = transition_task(
                backend,
                task_id,
                "backlog",
                "benchmark-human",
                expected_status=task.status.value,
                expected_updated_at=task.updated_at,
            )
            assert changed.status.value == "drafted"
            samples.append(time.perf_counter() - before)
        results.put({
            "samples": samples,
            "elapsed": time.perf_counter() - started,
            "cpu_seconds": time.process_time() - cpu_started,
        })
    except BaseException as error:
        results.put({"error": repr(error)})
    finally:
        if backend:
            backend.close()


def _reader(state, task_count, start, results):
    import anvil.project_snapshot as snapshot_module
    import anvil.web_actions as actions_module
    from anvil.state.sqlite import query_only_transaction

    @contextlib.contextmanager
    def observed(*args, **kwargs):
        with query_only_transaction(*args, **kwargs) as captured:
            # Force a real collision: eight writers arrive while capture owns
            # the ledger lock. Validation then continues in another process.
            start.set()
            yield captured

    repository = fixture.Repository(
        id="a" * 24,
        label="Disposable contention fixture",
        root=str(Path(state).parent),
        project_id="proj-1",
        reader_label="Native isolated fixture",
        reader_argv=[sys.executable, "-I", "-m", "anvil.cli", "project", "snapshot", "--json"],
        expected_schema_version=fixture.get_schema_version(),
    )
    try:
        began = time.perf_counter()
        with (
            patch.object(snapshot_module, "query_only_transaction", observed),
            patch.object(actions_module, "query_only_transaction", observed),
        ):
            result = actions_module.inspect_repository(repository, "benchmark-human")
        assert len(result["snapshot"]["payload"]["tasks"]) == task_count
        results.put({"reader_ms": (time.perf_counter() - began) * 1000})
    except BaseException as error:
        start.set()
        results.put({"error": repr(error)})


def run(tasks, operations, workers, repetitions):
    context = multiprocessing.get_context("spawn")
    with tempfile.TemporaryDirectory(prefix="anvil-ledger-latency-") as temporary:
        root = Path(temporary)
        seed = root / "seed" / ".anvil"
        seed.mkdir(parents=True)
        task_ids = ["T001", *(f"T002.{index}" for index in range(1, tasks))]
        backend = fixture._make_backend(seed)
        try:
            fixture._setup_claimable_task(backend)
            for task_id in task_ids[1:]:
                backend.append(
                    fixture._make_event(
                        "task.created",
                        fixture._make_task_payload(task_id=task_id, status="ready"),
                        target_kind="task",
                        target_id=task_id,
                    )
                )
        finally:
            backend.close()
        initial_events = len((seed / "events.jsonl").read_text().splitlines())
        measurements = []
        for overlap in (False, True):
            for repetition in range(repetitions):
                state = root / f"arm-{overlap}-{repetition}" / ".anvil"
                shutil.copytree(seed, state)
                ready, results = context.Queue(), context.Queue()
                start = context.Event()
                processes = [
                    context.Process(
                        target=_writer,
                        args=(
                            str(state),
                            task_ids[index:operations:workers],
                            ready,
                            start,
                            results,
                        ),
                    )
                    for index in range(workers)
                ]
                try:
                    for process in processes:
                        process.start()
                    for _ in processes:
                        assert ready.get(timeout=60) is True
                    if overlap:
                        reader = context.Process(
                            target=_reader,
                            args=(
                                str(state),
                                tasks,
                                start,
                                results,
                            ),
                        )
                        reader.start()
                        processes.append(reader)
                    else:
                        start.set()
                    outcomes = [results.get(timeout=60) for _ in processes]
                    assert not any("error" in item for item in outcomes), outcomes
                    for process in processes:
                        process.join(timeout=10)
                        assert process.exitcode == 0, process.exitcode
                    writers = [item for item in outcomes if "samples" in item]
                    samples = [sample for item in writers for sample in item["samples"]]
                    measurement = fixture._summary(
                        samples, max(item["elapsed"] for item in writers)
                    )
                    measurement.update(
                        mode="overlapping_board_reader" if overlap else "writers_only",
                        repetition=repetition,
                        workers=workers,
                        p99_ms=round(sorted(samples)[math.ceil(len(samples) * 0.99) - 1] * 1000, 3),
                        max_ms=round(max(samples) * 1000, 3),
                        p95_within_15_ms=measurement["p95_ms"] < 15,
                        worker_cpu_seconds=round(
                            sum(item.get("cpu_seconds", 0) for item in writers), 6
                        ),
                        reader_ms=next(
                            (
                                round(item["reader_ms"], 3)
                                for item in outcomes
                                if "reader_ms" in item
                            ),
                            None,
                        ),
                    )
                    measurement.update(
                        fixture._readback(state, task_ids[:operations], initial_events)
                    )
                    measurements.append(measurement)
                finally:
                    for process in processes:
                        if process.is_alive():
                            process.terminate()
                        process.join(timeout=10)
                    ready.close()
                    results.close()
        return {
            "schema": "anvil.ledger-contention-benchmark.v1",
            "task_count": tasks,
            "initial_events": initial_events,
            "event_bytes": (seed / "events.jsonl").stat().st_size,
            "database_bytes": (seed / "state.db").stat().st_size,
            "measurements": measurements,
            "validated": True,
            "limits": [
                "Synthetic ready-to-backlog transitions, independent OS processes and connections.",
                "No claims, Git preparation, proof verification, inference, containers "
                "or HTTP timing.",
                "Setup and process startup excluded; one synchronized reader collision per arm.",
                "p95 target 15ms applies only to this declared ledger workload; "
                "no fleet capacity claim.",
            ],
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=int, default=1000)
    parser.add_argument("--operations", type=int, default=1000)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    if not 1 <= arguments.operations <= arguments.tasks <= 2000:
        parser.error("require 1 <= operations <= tasks <= 2000")
    if (
        not 1 <= arguments.workers <= min(64, arguments.operations)
        or not 1 <= arguments.repetitions <= 10
    ):
        parser.error("require 1 <= workers <= min(64, operations) and 1 <= repetitions <= 10")
    # Only child benchmark processes inherit this explicit disposable layout.
    os.environ.pop("ANVIL_ROOT", None)
    os.environ.pop("ANVIL_PRD", None)
    os.environ["ANVIL_STATE_LAYOUT"] = "local"
    result = run(arguments.tasks, arguments.operations, arguments.workers, arguments.repetitions)
    rendered = json.dumps(result, indent=2) + "\n"
    if arguments.output:
        arguments.output.write_text(rendered)
    print(rendered, end="")


if __name__ == "__main__":
    main()
