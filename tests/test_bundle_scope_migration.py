"""Schema 25 stores scope on winning claims without reclassifying old claims."""

from __future__ import annotations

import sqlite3

import pytest

from anvil.bundles.execution import bundle_execution_scope_digest
from anvil.bundles.manager import BundleManager
from anvil.clock import FrozenClock
from anvil.state.backend import SchemaMismatch
from anvil.state.sqlite import SqliteBackend
from tests.test_bundle_state import _T0, _backend, _create_bundle, _seed


def _seed_claim(root, *, scoped=False):
    backend = _backend(root)
    _seed(backend)
    _create_bundle(backend)
    digest = bundle_execution_scope_digest(backend, "B001") if scoped else None
    result = BundleManager(
        backend, FrozenClock(_T0), actor="codex-main", project_root=root,
    ).claim("B001", expected_scope_digest=digest)
    return backend, result.claim, digest


def _reopen(root):
    backend = SqliteBackend(
        db_path=str(root / "state.db"), events_path=str(root / "events.jsonl"),
        clock=FrozenClock(_T0),
    )
    backend.initialize()
    return backend


def test_new_database_persists_winning_claim_scope_and_replays_it(tmp_path):
    backend, claimed, digest = _seed_claim(tmp_path, scoped=True)
    try:
        assert claimed.expected_scope_digest == digest
        connection = backend._require_conn()
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 26
        column = next(
            row for row in connection.execute("PRAGMA table_info(bundle_claims)")
            if row[1] == "expected_scope_digest"
        )
        assert column[2] == "TEXT" and column[3] == 0
        assert connection.execute(
            "SELECT expected_scope_digest FROM bundle_claims WHERE id = ?", (claimed.id,),
        ).fetchone()[0] == digest
        backend.replay_from_empty(str(tmp_path / "events.jsonl"))
        assert backend.get_bundle_claim("B001").expected_scope_digest == digest
    finally:
        backend.close()


@pytest.mark.parametrize("old_version", [22, 23, 24, 25])
def test_supported_upgrade_preserves_legacy_claims_and_authoritative_log(tmp_path, old_version):
    backend, claimed, _ = _seed_claim(tmp_path)
    BundleManager(backend, FrozenClock(_T0), actor="codex-main", project_root=tmp_path).release(
        "B001", reason="Release before fixture schema migration",
    )
    claimed = backend.get_bundle_claim("B001")
    original_claim = claimed.model_dump(mode="json", exclude={"expected_scope_digest"})
    backend.close()
    with sqlite3.connect(tmp_path / "state.db") as connection:
        if old_version < 25:
            connection.execute("ALTER TABLE bundle_claims DROP COLUMN expected_scope_digest")
        if old_version <= 23:
            connection.execute("ALTER TABLE tasks DROP COLUMN closure")
        connection.execute(f"PRAGMA user_version = {old_version}")
        original_members = connection.execute("SELECT * FROM claims ORDER BY id").fetchall()
        original_events = connection.execute("SELECT * FROM events ORDER BY rowid").fetchall()
        columns = {row[1] for row in connection.execute("PRAGMA table_info(bundle_claims)")}
        assert ("expected_scope_digest" in columns) is (old_version == 25)
    original_log = (tmp_path / "events.jsonl").read_bytes()

    backend = _reopen(tmp_path)
    try:
        migrated = backend.get_bundle_claim("B001")
        assert migrated is not None
        assert migrated.expected_scope_digest is None
        assert migrated.model_dump(mode="json", exclude={"expected_scope_digest"}) == original_claim
        connection = backend._require_conn()
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 26
        assert "closure" in {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
        assert [tuple(row) for row in connection.execute("SELECT * FROM claims ORDER BY id")] == original_members
        assert [tuple(row) for row in connection.execute("SELECT * FROM events ORDER BY rowid")] == original_events
        assert (tmp_path / "events.jsonl").read_bytes() == original_log
    finally:
        backend.close()
    # Reopening an upgraded database is idempotent and does not synthesize a grant.
    backend = _reopen(tmp_path)
    try:
        assert backend.get_bundle_claim("B001").expected_scope_digest is None
        assert (tmp_path / "events.jsonl").read_bytes() == original_log
    finally:
        backend.close()


def test_future_schema_refuses_before_ddl_or_log_changes(tmp_path):
    backend, _, _ = _seed_claim(tmp_path)
    backend.close()
    with sqlite3.connect(tmp_path / "state.db") as connection:
        connection.execute("ALTER TABLE bundle_claims DROP COLUMN expected_scope_digest")
        connection.execute("ALTER TABLE tasks DROP COLUMN closure")
        connection.execute("PRAGMA user_version = 27")
    before_database = (tmp_path / "state.db").read_bytes()
    before_log = (tmp_path / "events.jsonl").read_bytes()
    backend = SqliteBackend(
        db_path=str(tmp_path / "state.db"), events_path=str(tmp_path / "events.jsonl"),
        clock=FrozenClock(_T0),
    )
    try:
        with pytest.raises(SchemaMismatch) as error:
            backend.initialize()
        assert error.value.actual == 27
        assert error.value.expected == 26
        assert (tmp_path / "state.db").read_bytes() == before_database
        assert (tmp_path / "events.jsonl").read_bytes() == before_log
    finally:
        backend.close()
