"""Frozen snapshot pairs release live writers before expensive validation."""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

import pytest

import anvil.project_snapshot as snapshot_module
from anvil.project_snapshot import (
    ProjectSnapshotError,
    captured_snapshot_transaction,
    read_project_snapshot,
    snapshot_from_transaction,
)
from anvil.read_contracts import ReadErrorCode, snapshot_response_canonical_bytes
from anvil.state.sqlite import SqliteBackend, query_only_transaction
from tests.test_project_snapshot import _event, _seed, _state_path


def test_capture_matches_locked_snapshot_and_releases_writer(backend, frozen_clock):
    _seed(backend)
    root = _state_path(backend)
    with query_only_transaction(root / "state.db", root / "events.jsonl") as pair:
        original = snapshot_from_transaction(*pair)
    finished = threading.Event()
    failures = []

    def append_during_capture():
        writer = SqliteBackend(
            db_path=str(root / "state.db"), events_path=str(root / "events.jsonl"),
            clock=frozen_clock,
        )
        try:
            # Initialization also takes the global initialization lock. This
            # proves both locks are released before the captured view is used.
            writer.initialize()
            writer.append(_event("progress.noted", {
                "task_id": "T001", "notes": "after capture", "actor": "snapshot-test",
                "noted_at": frozen_clock.now().isoformat(),
            }, kind="task", target="T001"))
        except Exception as error:
            failures.append(error)
        finally:
            writer.close()
            finished.set()

    with captured_snapshot_transaction(root) as (connection, log):
        capture_directory = Path(log.name).parent
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        assert not log.writable()
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("DELETE FROM tasks")
        writer = threading.Thread(target=append_during_capture)
        writer.start()
        try:
            assert finished.wait(3), "snapshot retained a live writer/initialization lock"
            assert failures == []
            captured = snapshot_from_transaction(connection, log)
            assert snapshot_response_canonical_bytes(captured) == snapshot_response_canonical_bytes(original)
        finally:
            writer.join(timeout=3)
    assert not capture_directory.exists()
    assert read_project_snapshot(root).event_cursor.event_count == original.event_cursor.event_count + 1


@pytest.mark.parametrize("mutation", ["same_size_rewrite", "replace", "append"])
def test_capture_refuses_log_mutation_even_with_restored_mtime(backend, monkeypatch, mutation):
    _seed(backend)
    root = _state_path(backend)
    live_path = root / "events.jsonl"
    initial_stat = live_path.stat()
    initial_bytes = live_path.read_bytes()
    original_verify = snapshot_module._verify_event_identity
    changed = False
    capture_directories = []
    original_temporary_directory = snapshot_module.tempfile.TemporaryDirectory

    def capture_directory(*args, **kwargs):
        directory = original_temporary_directory(*args, **kwargs)
        capture_directories.append(Path(directory.name))
        return directory

    def change_after_initial_identity(log, identity):
        nonlocal changed
        original_verify(log, identity)
        if Path(log.name) == live_path and not changed:
            changed = True
            if mutation == "same_size_rewrite":
                changed_bytes = initial_bytes.replace(b'snapshot-test', b'snapshot-best')
                assert len(changed_bytes) == len(initial_bytes) and changed_bytes != initial_bytes
                live_path.write_bytes(changed_bytes)
            elif mutation == "replace":
                replacement = root / "replacement.jsonl"
                replacement.write_bytes(initial_bytes)
                replacement.replace(live_path)
            else:
                with live_path.open("ab") as stream:
                    stream.write(initial_bytes)
            os.utime(live_path, ns=(initial_stat.st_atime_ns, initial_stat.st_mtime_ns))

    monkeypatch.setattr(snapshot_module, "_verify_event_identity", change_after_initial_identity)
    monkeypatch.setattr(snapshot_module.tempfile, "TemporaryDirectory", capture_directory)
    with pytest.raises(ProjectSnapshotError) as refusal:
        read_project_snapshot(root)
    assert refusal.value.error.code is ReadErrorCode.projection_not_converged
    assert capture_directories and all(not path.exists() for path in capture_directories)
