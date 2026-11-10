"""Targeted history lookups stay indexed without changing authoritative events."""

from __future__ import annotations

import sqlite3

import pytest

import anvil.state.sqlite as sqlite_backend
from anvil.state.schema import SCHEMA_VERSION
from anvil.state.snapshot import serialize_state
from tests.test_bundle_state import _backend, _seed


@pytest.mark.parametrize("old_version", [22, 24, SCHEMA_VERSION])
def test_existing_state_adds_history_index_and_preserves_replay(
    tmp_path, monkeypatch, old_version,
):
    # Build the predecessor through the engine, without this physical index.
    with monkeypatch.context() as predecessor:
        predecessor.setattr(sqlite_backend, "DDL", "\n".join(
            line for line in sqlite_backend.DDL.splitlines()
            if "idx_events_target_action" not in line
        ))
        backend = _backend(tmp_path)
        try:
            _seed(backend)
            expected_state = serialize_state(backend)
            expected_events = [tuple(row) for row in backend._require_conn().execute(
                "SELECT * FROM events ORDER BY rowid"
            )]
        finally:
            backend.close()
    # Model the supported predecessor layouts; only this disposable fixture is changed.
    with sqlite3.connect(tmp_path / "state.db") as connection:
        if old_version < 25:
            connection.execute("ALTER TABLE bundle_claims DROP COLUMN expected_scope_digest")
        if old_version == 22:
            connection.execute("ALTER TABLE tasks DROP COLUMN closure")
        connection.execute(f"PRAGMA user_version = {old_version}")
        assert not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'idx_events_target_action'"
        ).fetchone()
    expected_log = (tmp_path / "events.jsonl").read_bytes()

    backend = _backend(tmp_path)
    try:
        connection = backend._require_conn()
        assert [row[2] for row in connection.execute(
            "PRAGMA index_info(idx_events_target_action)"
        )] == ["target_id", "action"]
        queries = []
        connection.set_trace_callback(queries.append)
        try:
            backend._check_invalidation_claim_actor(connection, "release:T001", "worker")
        finally:
            connection.set_trace_callback(None)
        assert len(queries) == 1
        details = [row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + queries[0])]
        assert sum("idx_events_target_action" in detail for detail in details) == 2
        assert not any("SCAN events" in detail for detail in details)
        assert serialize_state(backend) == expected_state
        assert [tuple(row) for row in connection.execute(
            "SELECT * FROM events ORDER BY rowid"
        )] == expected_events
        assert (tmp_path / "events.jsonl").read_bytes() == expected_log

        backend.replay_from_empty(str(tmp_path / "events.jsonl"))
        assert serialize_state(backend) == expected_state
        assert (tmp_path / "events.jsonl").read_bytes() == expected_log
        assert backend._require_conn().execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'idx_events_target_action'"
        ).fetchone()
    finally:
        backend.close()
