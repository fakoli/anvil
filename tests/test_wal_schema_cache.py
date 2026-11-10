"""WAL probe reuse is bound to verified bytes, never filesystem metadata."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

import anvil.state.sqlite as sqlite_backend
from anvil.state.backend import SchemaProbeFailed


@pytest.fixture
def live_wal(tmp_path, monkeypatch):
    monkeypatch.setattr(sqlite_backend, "_WAL_SCHEMA_PREFIX_CACHE", None)
    database = tmp_path / "state.db"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA wal_autocheckpoint = 0")
    connection.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
    connection.execute("INSERT INTO sentinel VALUES ('first')")
    connection.execute("PRAGMA user_version = 7")
    connection.commit()
    try:
        yield database, connection
    finally:
        connection.close()


def _observe_checksum_bytes(monkeypatch):
    checksummed = []
    original = sqlite_backend._wal_checksum

    def observe(content, **kwargs):
        checksummed.append(len(content))
        return original(content, **kwargs)

    monkeypatch.setattr(sqlite_backend, "_wal_checksum", observe)
    return checksummed


def test_exact_prefix_reuses_checksums_and_validates_new_commits(live_wal, monkeypatch):
    database, connection = live_wal
    checksummed = _observe_checksum_bytes(monkeypatch)
    assert sqlite_backend.read_db_schema_version(database) == 7
    cold_checksum_bytes = sum(checksummed)
    assert cold_checksum_bytes > 24
    checksummed.clear()
    assert sqlite_backend.read_db_schema_version(database) == 7
    assert checksummed == [24]
    checksummed.clear()

    connection.execute("PRAGMA user_version = 8")
    connection.commit()
    assert sqlite_backend.read_db_schema_version(database) == 8
    assert 24 < sum(checksummed) < cold_checksum_bytes
    checksummed.clear()
    assert sqlite_backend.read_db_schema_version(database) == 8
    assert checksummed == [24]


@pytest.mark.parametrize("mutation", ["rewrite", "truncate", "header", "salts"])
def test_changed_bytes_invalidate_prefix_even_with_restored_mtime(
    live_wal, monkeypatch, mutation,
):
    database, _connection = live_wal
    wal = Path(f"{database}-wal")
    assert sqlite_backend.read_db_schema_version(database) == 7
    initial = wal.stat()
    content = bytearray(wal.read_bytes())
    if mutation == "rewrite":
        content[32 + 24 + 60] ^= 1
    elif mutation == "truncate":
        del content[32:]
    elif mutation == "header":
        content[0] ^= 1
    else:
        content[16] ^= 1
    wal.write_bytes(content)
    os.utime(wal, ns=(initial.st_atime_ns, initial.st_mtime_ns))
    if mutation in {"header", "salts"}:
        with pytest.raises(SchemaProbeFailed):
            sqlite_backend.read_db_schema_version(database)
        assert sqlite_backend._WAL_SCHEMA_PREFIX_CACHE is None
    else:
        checksummed = _observe_checksum_bytes(monkeypatch)
        cached_result = sqlite_backend.read_db_schema_version(database)
        assert cached_result == 0
        if mutation == "rewrite":
            assert sum(checksummed) > 24
        monkeypatch.setattr(sqlite_backend, "_WAL_SCHEMA_PREFIX_CACHE", None)
        assert sqlite_backend.read_db_schema_version(database) == cached_result


@pytest.mark.parametrize("invalid_tail", ["incomplete", "checksum", "salt"])
def test_invalid_tail_does_not_poison_checksum_seed(live_wal, monkeypatch, invalid_tail):
    database, connection = live_wal
    header, original_wal = sqlite_backend._read_stable_database_and_wal(database)
    assert sqlite_backend._schema_version_from_wal(header, original_wal) == 7
    connection.execute("PRAGMA user_version = 8")
    connection.commit()
    _header, extended_wal = sqlite_backend._read_stable_database_and_wal(database)
    assert extended_wal.startswith(original_wal)
    damaged = bytearray(extended_wal)
    if invalid_tail == "incomplete":
        del damaged[-1:]
    elif invalid_tail == "checksum":
        damaged[-1] ^= 1
    else:
        damaged[len(original_wal) + 8] ^= 1
    assert sqlite_backend._schema_version_from_wal(header, bytes(damaged)) == 7
    assert sqlite_backend._WAL_SCHEMA_PREFIX_CACHE.content == original_wal
    # Completing or repairing the tail validates from the last good checksum.
    assert sqlite_backend._schema_version_from_wal(header, extended_wal) == 8
    monkeypatch.setattr(sqlite_backend, "_WAL_SCHEMA_PREFIX_CACHE", None)
    assert sqlite_backend._schema_version_from_wal(header, extended_wal) == 8


def test_cache_ceiling_does_not_skip_validation(live_wal, monkeypatch):
    database, _connection = live_wal
    monkeypatch.setattr(sqlite_backend, "_WAL_SCHEMA_PREFIX_CACHE_MAX_BYTES", 32)
    checksummed = _observe_checksum_bytes(monkeypatch)
    assert sqlite_backend.read_db_schema_version(database) == 7
    assert sqlite_backend._WAL_SCHEMA_PREFIX_CACHE is None
    first_bytes = sum(checksummed)
    checksummed.clear()
    assert sqlite_backend.read_db_schema_version(database) == 7
    assert sum(checksummed) == first_bytes


def test_main_header_is_not_cached_when_wal_has_no_page_one(live_wal):
    database, connection = live_wal
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.execute("UPDATE sentinel SET value = 'later'")
    connection.commit()
    header, content = sqlite_backend._read_stable_database_and_wal(database)
    assert sqlite_backend._schema_version_from_wal(header, content) == 7
    assert sqlite_backend._WAL_SCHEMA_PREFIX_CACHE.committed_page_one is None
    replacement_header = bytearray(header)
    replacement_header[60:64] = (9).to_bytes(4, "big")
    assert sqlite_backend._schema_version_from_wal(bytes(replacement_header), content) == 9
