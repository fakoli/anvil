"""Byte-level schema reads executed only inside the file-inspection worker.

Every backend open starts one worker, so this module is that worker's entire
import cost. Import only the standard library and ``anvil.state.backend``
here: importing ``anvil.state.sqlite`` (Pydantic models, planning, signing)
made each worker start cost several hundred milliseconds.
"""
from __future__ import annotations

import os
import sqlite3
import struct
import tempfile
import threading
from pathlib import Path
from typing import NamedTuple

from anvil.state.backend import SchemaProbeFailed


def _read_schema_from_rollback_copy(db_path: Path) -> int:
    """Let SQLite recover a disposable rollback-journal snapshot."""
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("PRAGMA user_version").fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def _read_schema_from_rollback_snapshot(db_path: Path) -> int:
    """Read schema through a disposable copy when a rollback journal exists.

    Hot-journal recovery needs SQLite's transaction semantics.  It runs only
    against a byte-stable temporary copy, never the live database.
    """
    try:
        journal_path = Path(f"{db_path}-journal")
        before_db = db_path.read_bytes()
        before_journal = journal_path.read_bytes()
        with tempfile.TemporaryDirectory(prefix="anvil-schema-probe-") as temp_dir:
            snapshot_db = Path(temp_dir) / "state.db"
            snapshot_db.write_bytes(before_db)
            Path(f"{snapshot_db}-journal").write_bytes(before_journal)
            if (
                not journal_path.exists()
                or db_path.read_bytes() != before_db
                or journal_path.read_bytes() != before_journal
            ):
                raise SchemaProbeFailed(
                    "Database changed during the side-effect-free schema probe; retry when idle."
                )
            return _read_schema_from_rollback_copy(snapshot_db)
    except SchemaProbeFailed:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise SchemaProbeFailed(
            "Database schema could not be inspected without mutating live state."
        ) from exc


def _wal_checksum(
    content: bytes, *, byteorder: str, seed: tuple[int, int] = (0, 0)
) -> tuple[int, int]:
    """Implement SQLite's cumulative WAL checksum over an 8-byte-aligned buffer."""
    if len(content) % 8:
        raise SchemaProbeFailed("WAL checksum input is not 8-byte aligned.")
    s0, s1 = seed
    byte_format = {"little": "<II", "big": ">II"}[byteorder]
    for first, second in struct.iter_unpack(byte_format, content):
        s0 = (s0 + first + s1) & 0xFFFFFFFF
        s1 = (s1 + second + s0) & 0xFFFFFFFF
    return s0, s1


def _schema_version_from_database_bytes(database_header: bytes) -> int:
    if len(database_header) == 0:
        return 0
    if len(database_header) < 100 or database_header[:16] != b"SQLite format 3\x00":
        raise SchemaProbeFailed("Database header is incomplete or invalid.")
    return int.from_bytes(database_header[60:64], "big")


class _VerifiedWalPrefix(NamedTuple):
    content: bytes
    checksum: tuple[int, int]
    latest_page_one: bytes | None
    committed_page_one: bytes | None


_WAL_SCHEMA_PREFIX_CACHE_MAX_BYTES = 16 * 1024 * 1024
_WAL_SCHEMA_PREFIX_CACHE: _VerifiedWalPrefix | None = None
_WAL_SCHEMA_PREFIX_CACHE_LOCK = threading.Lock()


def _schema_version_from_wal(database_header: bytes, wal_content: bytes) -> int:
    """Read committed ``user_version``, reusing only byte-identical verified frames.

    One bounded immutable prefix avoids checksumming the same WAL again under
    the event lock after the initial outside-lock probe. Every call still reads
    the current bytes and compares the entire prefix, including header/salts.
    No file identity, timestamp, size, path, or digest grants cache reuse.
    """
    global _WAL_SCHEMA_PREFIX_CACHE
    with _WAL_SCHEMA_PREFIX_CACHE_LOCK:
        verified_prefix = _WAL_SCHEMA_PREFIX_CACHE
        if (
            verified_prefix is not None
            and not wal_content.startswith(verified_prefix.content)
        ):
            _WAL_SCHEMA_PREFIX_CACHE = None
            verified_prefix = None
    main_version = _schema_version_from_database_bytes(database_header)
    if not wal_content:
        return main_version
    if len(wal_content) < 32:
        raise SchemaProbeFailed("WAL header is incomplete.")

    magic, wal_version, page_size_raw, _, salt1, salt2, checksum1, checksum2 = (
        struct.unpack(">8I", wal_content[:32])
    )
    if magic not in {0x377F0682, 0x377F0683} or wal_version != 3007000:
        raise SchemaProbeFailed("WAL header is invalid or unsupported.")
    page_size = 65536 if page_size_raw == 1 else page_size_raw
    if page_size < 512 or page_size > 65536 or page_size & (page_size - 1):
        raise SchemaProbeFailed("WAL page size is invalid.")

    checksum_byteorder = "little" if magic == 0x377F0682 else "big"
    running = _wal_checksum(wal_content[:24], byteorder=checksum_byteorder)
    if running != (checksum1, checksum2):
        raise SchemaProbeFailed("WAL header checksum is invalid.")

    frame_size = 24 + page_size
    latest_page_one: bytes | None = None
    committed_page_one: bytes | None = None
    offset = 32
    if verified_prefix is not None:
        running = verified_prefix.checksum
        latest_page_one = verified_prefix.latest_page_one
        committed_page_one = verified_prefix.committed_page_one
        offset = len(verified_prefix.content)
    while offset + frame_size <= len(wal_content):
        header = wal_content[offset : offset + 24]
        page = wal_content[offset + 24 : offset + frame_size]
        page_number, database_size, frame_salt1, frame_salt2, stored1, stored2 = (
            struct.unpack(">6I", header)
        )
        if (frame_salt1, frame_salt2) != (salt1, salt2):
            break
        frame_checksum = _wal_checksum(
            header[:8] + page,
            byteorder=checksum_byteorder,
            seed=running,
        )
        if frame_checksum != (stored1, stored2):
            break
        running = frame_checksum
        if page_number == 1:
            latest_page_one = page
        if database_size:
            committed_page_one = latest_page_one
        offset += frame_size

    # Ignore an invalid/incomplete tail, exactly as SQLite does. Only complete,
    # checksum-verified frames enter the cache; the tail must be checked again.
    with _WAL_SCHEMA_PREFIX_CACHE_LOCK:
        _WAL_SCHEMA_PREFIX_CACHE = (
            _VerifiedWalPrefix(
                wal_content[:offset], running, latest_page_one, committed_page_one,
            )
            if offset <= _WAL_SCHEMA_PREFIX_CACHE_MAX_BYTES else None
        )
    if committed_page_one is None:
        return main_version
    return _schema_version_from_database_bytes(committed_page_one[:100])


def _read_stable_database_and_wal(db_path: Path) -> tuple[bytes, bytes]:
    """Worker-only raw page/WAL read, also usable on closed private test copies.

    WAL appends are sequential and frame-checksummed. A concurrent writer may
    extend the file after this read; that does not invalidate the complete,
    committed prefix already captured. The parser ignores an incomplete or
    checksum-invalid trailing frame exactly as SQLite recovery does. Recheck
    page 1 because a concurrent checkpoint may move WAL content into the DB.
    """
    wal_path = Path(f"{db_path}-wal")
    with db_path.open("rb") as database:
        database_header = database.read(100)
    wal_content = wal_path.read_bytes() if wal_path.exists() else b""
    with db_path.open("rb") as database:
        if database.read(100) != database_header:
            raise SchemaProbeFailed("Database header changed during schema inspection.")
    return database_header, wal_content


def _read_db_schema_version_in_worker(db_path: str | os.PathLike[str]) -> int:
    """Worker-only on-disk ``PRAGMA user_version`` inspection without migration.

    T007/B11 (MUST-FIX 2a): the TRUE pre-migration schema version stamped on a
    database. Unlike ``SqliteBackend.get_schema_version()`` — which reads the
    PRAGMA *after* ``initialize()`` has already migrated/re-stamped it to
    ``SCHEMA_VERSION`` — this is a standalone byte-level read that never opens
    the live database or runs DDL or migrations. That makes genuine drift
    (db_version < code_version, pre-migration) observable by the ``status``
    command instead of being masked by the open-time migration.

    Returns 0 when the db file does not exist (a brand-new db reports
    ``user_version == 0``), matching SQLite's own default for an unstamped db.
    """
    path = Path(db_path)
    if not path.exists():
        return 0

    # Read only page 1 and the WAL bytes. This follows SQLite's documented WAL
    # frame/checksum format without opening the live database, so no SHM, WAL,
    # journal, lock, or directory entry can be created or modified.
    try:
        if Path(f"{path}-journal").exists():
            return _read_schema_from_rollback_snapshot(path)
        last_error: SchemaProbeFailed | OSError | None = None
        for _ in range(3):
            try:
                database_header, wal_content = _read_stable_database_and_wal(path)
                return _schema_version_from_wal(database_header, wal_content)
            except (SchemaProbeFailed, OSError) as exc:
                # Last-close checkpoint can remove WAL after its existence check.
                # Retry the whole pair: the earlier DB header may also be stale.
                last_error = exc
        assert last_error is not None
        raise last_error
    except SchemaProbeFailed:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise SchemaProbeFailed(
            "Database schema could not be inspected without mutating live state."
        ) from exc
