"""Bounded snapshot capture history scaling in temporary synthetic State.

Run: uv run --project bin --locked python benchmarks/snapshot_capture.py
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_sqlite import (  # noqa: E402
    _make_backend,
    _make_event,
    _make_task_payload,
    _setup_claimable_task,
)

import anvil.state.sqlite as sqlite_backend  # noqa: E402
from anvil.clock import SystemClock  # noqa: E402
from anvil.project_snapshot import captured_snapshot_transaction  # noqa: E402
from anvil.state.models import EventDraft  # noqa: E402


def measure_capture(state: Path, expected_events: int) -> dict[str, object]:
    capture_milliseconds = []
    event_lock_milliseconds = []
    acquire = sqlite_backend._append_lock_acquire_nb
    release = sqlite_backend._append_lock_release
    acquired_at = 0.0

    def timed_acquire(stream):
        nonlocal acquired_at
        acquire(stream)
        acquired_at = time.perf_counter()

    def timed_release(stream):
        release(stream)
        event_lock_milliseconds.append((time.perf_counter() - acquired_at) * 1000)

    for _ in range(5):
        # Model a fresh web subprocess: the first schema probe is cold, and
        # its exact verified bytes can be reused by the under-lock confirmation.
        sqlite_backend._WAL_SCHEMA_PREFIX_CACHE = None
        with (
            patch.object(sqlite_backend, "_append_lock_acquire_nb", timed_acquire),
            patch.object(sqlite_backend, "_append_lock_release", timed_release),
        ):
            started = time.perf_counter()
            with captured_snapshot_transaction(state) as (connection, log):
                captured_at = time.perf_counter()
                captured_events = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
                assert captured_events == expected_events
                assert os.fstat(log.fileno()).st_size == (state / "events.jsonl").stat().st_size
            capture_milliseconds.append((captured_at - started) * 1000)
    wal_path = state / "state.db-wal"
    return {
        "tasks": 1000,
        "events": expected_events,
        "database_bytes": (state / "state.db").stat().st_size,
        "wal_bytes": wal_path.stat().st_size if wal_path.exists() else 0,
        "log_bytes": (state / "events.jsonl").stat().st_size,
        "capture_ms": capture_milliseconds,
        "capture_median_ms": statistics.median(capture_milliseconds),
        "capture_max_ms": max(capture_milliseconds),
        "event_lock_ms": event_lock_milliseconds,
        "event_lock_median_ms": statistics.median(event_lock_milliseconds),
        "event_lock_max_ms": max(event_lock_milliseconds),
    }


def main() -> None:
    results = []
    with tempfile.TemporaryDirectory(prefix="anvil-capture-history-") as directory:
        state = Path(directory) / ".anvil"
        state.mkdir()
        backend = _make_backend(state)
        try:
            _setup_claimable_task(backend)
            for index in range(1, 1000):
                task_id = f"T002.{index}"
                backend.append(_make_event(
                    "task.created", _make_task_payload(task_id=task_id, status="ready"),
                    target_kind="task", target_id=task_id,
                ))
            event_count = backend._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            for target_events in (event_count, 10_000, 100_000):
                while event_count < target_events:
                    now = SystemClock().now()
                    backend.append(EventDraft(
                        timestamp=now, actor="history-probe", action="progress.noted",
                        target_kind="task", target_id="T001",
                        payload_json={
                            "task_id": "T001", "notes": "synthetic historical progress",
                            "noted_at": now.isoformat(), "actor": "history-probe",
                        },
                    ))
                    event_count += 1
                results.append(measure_capture(state, event_count))
        finally:
            backend.close()
    print(json.dumps({
        "scope": "Capture only, 1000 tasks, at most 100000 synthetic events; "
        "no proof verification, "
        "full snapshot serialization, competing writers, containers, or operator State. "
        "Cold prefix cache per sample; actual event lock acquisition through release is measured. "
        "Five samples describe bounded latency, not an end-to-end p95 SLO.",
        "results": results,
    }, indent=2))


if __name__ == "__main__":
    main()
