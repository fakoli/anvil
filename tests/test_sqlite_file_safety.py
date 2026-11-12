"""Raw file inspection must preserve a retained SQLite owner's POSIX locks.

The operation runs in the retaining process. All raw database observations run
in independent children: an observer's own open/close would otherwise destroy
the very locks this regression is checking.
"""

from __future__ import annotations

import errno
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from anvil.clock import FrozenClock
from anvil.state.models import EventDraft
from anvil.state.schema import SCHEMA_VERSION
from anvil.state.sqlite import (
    SqliteBackend,
    _schema_initialization_lock,
    _stable_database_copy,
    query_only_transaction,
    read_db_schema_version,
)

_LOCK_OBSERVER = """
import fcntl
import json
import sys

with open(sys.argv[1], "r+b") as database:
    try:
        fcntl.lockf(database, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        result = {"exclusive_lock_obtained": False, "errno": error.errno}
    else:
        result = {"exclusive_lock_obtained": True}
print(json.dumps(result))
"""

_SQLITE_OBSERVER = """
import json
import sqlite3
import sys
from contextlib import closing

with closing(sqlite3.connect(sys.argv[1], timeout=5)) as connection:
    result = {
        "rows": connection.execute(
            "SELECT value FROM retained_owner_probe ORDER BY rowid"
        ).fetchall(),
        "integrity": connection.execute("PRAGMA integrity_check").fetchall(),
        "schema_version": connection.execute("PRAGMA user_version").fetchone()[0],
        "sqlite_version": sqlite3.sqlite_version,
    }
print(json.dumps(result))
"""

_NOW = datetime(2026, 11, 12, tzinfo=UTC)


@pytest.fixture
def inspection_processes(monkeypatch: pytest.MonkeyPatch) -> list[subprocess.Popen]:
    """Observe real owned workers without replacing their transport or result."""
    processes: list[subprocess.Popen] = []
    original_popen = subprocess.Popen

    def track_worker(*arguments: Any, **options: Any) -> subprocess.Popen:
        process = original_popen(*arguments, **options)
        command = process.args
        if isinstance(command, (list, tuple)) and any(
            module in command
            for module in (
                "anvil.cli._schema_probe_worker",
                "anvil.state._file_inspection_worker",
            )
        ):
            processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", track_worker)
    return processes


def _native_backend(root: Path) -> SqliteBackend:
    events = root / "events.jsonl"
    events.touch()
    return SqliteBackend(
        db_path=str(root / "state.db"),
        events_path=str(events),
        clock=FrozenClock(_NOW),
    )


def _observe_in_child(script: str, database: Path) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, "-c", script, str(database)],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return json.loads(result.stdout)


def _require_retained_lock(database: Path) -> None:
    observation = _observe_in_child(_LOCK_OBSERVER, database)
    assert observation.get("exclusive_lock_obtained") is False, (
        f"Retained SQLite lock was lost: {observation}"
    )
    assert observation["errno"] in {errno.EACCES, errno.EAGAIN}


def _sidecar_identities(database: Path) -> dict[str, tuple[int, int]]:
    # stat is safe in the retaining process; never read/hash live DB bytes here.
    identities = {}
    for suffix in ("-wal", "-shm"):
        metadata = Path(f"{database}{suffix}").stat()
        identities[suffix] = (metadata.st_dev, metadata.st_ino)
    return identities


@contextmanager
def _retained_database(root: Path) -> Iterator[tuple[Path, Path, sqlite3.Connection]]:
    database = root / "state.db"
    events = root / "events.jsonl"
    events.touch()
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        connection.execute("CREATE TABLE retained_owner_probe (value TEXT NOT NULL)")
        connection.execute("INSERT INTO retained_owner_probe VALUES ('before-inspection')")
        connection.commit()
        yield database, events, connection


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX advisory-lock semantics")
@pytest.mark.parametrize(
    "operation",
    ["public-schema", "query-only", "stable-copy", "live-fsync"],
)
def test_live_file_operation_preserves_retained_lock_and_later_commit(
    tmp_path: Path, operation: str,
) -> None:
    """An external last-close must not strand the retained owner's next commit."""
    with _retained_database(tmp_path) as (database, events, retained):
        _require_retained_lock(database)
        original_sidecars = _sidecar_identities(database)

        if operation == "public-schema":
            assert read_db_schema_version(database) == SCHEMA_VERSION
        elif operation == "query-only":
            with query_only_transaction(database, events) as (reader, _):
                rows = reader.execute("SELECT value FROM retained_owner_probe").fetchall()
                assert [tuple(row) for row in rows] == [("before-inspection",)]
        elif operation == "stable-copy":
            # Stable capture requires the caller's native event/operation lock.
            with _schema_initialization_lock(str(database), str(events)):
                with _stable_database_copy(database) as snapshot:
                    observed_snapshot = _observe_in_child(_SQLITE_OBSERVER, snapshot)
                    assert observed_snapshot["rows"] == [["before-inspection"]]
                    assert observed_snapshot["integrity"] == [["ok"]]
        else:
            from anvil.cli.repair import _checkpoint_and_fsync_live_projection

            _checkpoint_and_fsync_live_projection(retained, database)

        # Modern SQLite may conceal the later data loss. Lock preservation is
        # mandatory independently of whether that runtime keeps WAL files alive.
        _require_retained_lock(database)
        external_before_commit = _observe_in_child(_SQLITE_OBSERVER, database)
        assert external_before_commit["rows"] == [["before-inspection"]]
        assert external_before_commit["integrity"] == [["ok"]]
        assert _sidecar_identities(database) == original_sidecars

        retained.execute("INSERT INTO retained_owner_probe VALUES ('after-external-close')")
        retained.commit()
        expected_rows = [["before-inspection"], ["after-external-close"]]
        # Keep retained open: closing it before this observation can mask a
        # commit to unlinked WAL files on affected SQLite versions.
        fresh = _observe_in_child(_SQLITE_OBSERVER, database)
        assert fresh["rows"] == expected_rows
        assert fresh["integrity"] == [["ok"]]
        assert fresh["schema_version"] == SCHEMA_VERSION
        assert [list(row) for row in retained.execute(
            "SELECT value FROM retained_owner_probe ORDER BY rowid"
        )] == expected_rows
        _require_retained_lock(database)
        assert _sidecar_identities(database) == original_sidecars

    after_owner_close = _observe_in_child(_SQLITE_OBSERVER, database)
    assert after_owner_close["rows"] == expected_rows
    assert after_owner_close["integrity"] == [["ok"]]


def test_latest_evidence_distinguishes_missing_row_from_database_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _native_backend(tmp_path)
    try:
        backend.initialize()
        assert backend.get_latest_evidence("missing-task") is None

        def failed_query(_connection: sqlite3.Connection, _task_id: str) -> None:
            raise sqlite3.OperationalError("fixture evidence query failed")

        monkeypatch.setattr(SqliteBackend, "_latest_evidence_row", staticmethod(failed_query))
        with pytest.raises(sqlite3.OperationalError, match="fixture evidence query failed"):
            backend.get_latest_evidence("missing-task")
    finally:
        backend.close()


def test_existing_database_initialization_reuses_one_owned_probe_child(
    tmp_path: Path, inspection_processes: list[subprocess.Popen],
) -> None:
    backend = _native_backend(tmp_path)
    try:
        backend.initialize()
        backend.close()
        inspection_processes.clear()

        # The file already exists; repeated compatibility probes belong to one
        # initialization budget, rather than a fresh interpreter for each probe.
        backend.initialize()
        assert len(inspection_processes) <= 1
        assert all(process.poll() is not None for process in inspection_processes)
    finally:
        backend.close()


def test_query_transaction_reuses_one_owned_probe_child(
    tmp_path: Path, inspection_processes: list[subprocess.Popen],
) -> None:
    with _retained_database(tmp_path) as (database, events, _):
        with query_only_transaction(database, events) as (connection, _):
            assert connection.execute("SELECT COUNT(*) FROM retained_owner_probe").fetchone()[0] == 1
        assert len(inspection_processes) <= 1
        assert all(process.poll() is not None for process in inspection_processes)


def test_initialized_append_starts_no_inspection_child(
    tmp_path: Path, inspection_processes: list[subprocess.Popen],
) -> None:
    backend = _native_backend(tmp_path)
    try:
        backend.initialize()
        inspection_processes.clear()
        event = backend.append(EventDraft(
            timestamp=_NOW,
            actor="file-safety-test",
            action="project.created",
            target_kind="project",
            target_id="file-safety-project",
            payload_json={
                "id": "file-safety-project",
                "name": "File safety fixture",
                "description": "",
                "created_at": _NOW.isoformat(),
                "updated_at": _NOW.isoformat(),
            },
        ))
        assert event is not None
        assert backend.get_project().id == "file-safety-project"
        assert inspection_processes == []
    finally:
        backend.close()
