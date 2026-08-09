"""schema_version exposure tests (T007/B11).

The current SCHEMA_VERSION is surfaced to tooling through:
- a public accessor ``schema.get_schema_version()``;
- a backend accessor ``SqliteBackend.get_schema_version()`` (the DB's stamped
  ``PRAGMA user_version``);
- the ``status`` command output (human line + ``--json`` data).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from anvil.cli import app
from anvil.state.schema import SCHEMA_VERSION, get_schema_version

runner = CliRunner()


def _invoke(tmp_path: Path, cmd: list[str]):  # type: ignore[no-untyped-def]
    original_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        return runner.invoke(app, cmd, catch_exceptions=False)
    finally:
        os.chdir(original_cwd)


def _init(tmp_path: Path) -> None:
    res = _invoke(tmp_path, ["init", "--name", "Schema Version Project"])
    assert res.exit_code == 0, res.output


# ---------------------------------------------------------------------------
# Accessors
# ---------------------------------------------------------------------------


def test_get_schema_version_matches_constant() -> None:
    """The public accessor returns the current SCHEMA_VERSION constant."""
    assert get_schema_version() == SCHEMA_VERSION
    assert get_schema_version() == 19


def test_backend_get_schema_version_matches_constant(tmp_path: Path) -> None:
    """A freshly initialized DB stamps user_version == SCHEMA_VERSION."""
    from anvil.clock import SystemClock
    from anvil.state.sqlite import SqliteBackend

    state_dir = tmp_path / ".anvil"
    state_dir.mkdir()
    (state_dir / "events.jsonl").touch()
    backend = SqliteBackend(
        db_path=str(state_dir / "state.db"),
        events_path=str(state_dir / "events.jsonl"),
        clock=SystemClock(),
    )
    backend.initialize()
    try:
        assert backend.get_schema_version() == SCHEMA_VERSION
    finally:
        backend.close()


# ---------------------------------------------------------------------------
# status surfaces schema_version
# ---------------------------------------------------------------------------


def test_status_json_includes_schema_version(tmp_path: Path) -> None:
    """status --json data carries schema_version == SCHEMA_VERSION."""
    _init(tmp_path)
    res = _invoke(tmp_path, ["status", "--json"])
    assert res.exit_code == 0, res.output
    env = json.loads(res.stdout.strip())
    assert env["ok"] is True
    assert env["command"] == "status"
    assert env["data"]["schema_version"] == SCHEMA_VERSION
    # The DB-stamped version is surfaced too and matches on a healthy project.
    assert env["data"]["db_schema_version"] == SCHEMA_VERSION


def test_status_human_includes_schema_version(tmp_path: Path) -> None:
    """Human status output shows the Schema line with the version number."""
    _init(tmp_path)
    res = _invoke(tmp_path, ["status"])
    assert res.exit_code == 0, res.output
    assert "Schema:" in res.output
    assert str(SCHEMA_VERSION) in res.output


# ---------------------------------------------------------------------------
# read_db_schema_version reads the TRUE on-disk version without migrating
# ---------------------------------------------------------------------------


def _stamp_user_version(state_dir: Path, version: int) -> None:
    """Force PRAGMA user_version on the project's state.db (out of band)."""
    import sqlite3

    conn = sqlite3.connect(str(state_dir / "state.db"))
    try:
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
    finally:
        conn.close()


def test_read_db_schema_version_returns_zero_for_missing_db(
    tmp_path: Path,
) -> None:
    """A non-existent db reports user_version 0 (SQLite's default)."""
    from anvil.state.sqlite import read_db_schema_version

    assert read_db_schema_version(str(tmp_path / "nope.db")) == 0


def test_read_db_schema_version_does_not_migrate(tmp_path: Path) -> None:
    """The standalone read reports the TRUE on-disk version, unmigrated."""
    from anvil.state.sqlite import read_db_schema_version

    _init(tmp_path)
    state_dir = tmp_path / ".anvil"
    _stamp_user_version(state_dir, 3)

    # Reads v3 (pre-migration) — NOT the code SCHEMA_VERSION.
    assert read_db_schema_version(str(state_dir / "state.db")) == 3
    # And the read is read-only: the on-disk version is untouched afterward.
    assert read_db_schema_version(str(state_dir / "state.db")) == 3


def _state_manifest(root: Path) -> dict[str, tuple[int, str]]:
    """Return a recursive byte identity without opening SQLite."""
    import hashlib

    result: dict[str, tuple[int, str]] = {}
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        content = path.read_bytes()
        result[path.relative_to(root).as_posix()] = (
            len(content),
            hashlib.sha256(content).hexdigest(),
        )
    return result


def _copy_live_wal_fixture(
    tmp_path: Path, *, version: int, include_shm: bool
) -> Path:
    """Copy an uncheckpointed WAL database while its writer remains open."""
    import shutil
    import sqlite3

    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    seed_db = seed_dir / "state.db"
    conn = sqlite3.connect(seed_db)
    try:
        assert conn.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        conn.execute("PRAGMA wal_autocheckpoint = 0")
        conn.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
        conn.execute("INSERT INTO sentinel VALUES ('uncheckpointed')")
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        shutil.copyfile(seed_db, state_dir / "state.db")
        shutil.copyfile(Path(f"{seed_db}-wal"), state_dir / "state.db-wal")
        if include_shm:
            shutil.copyfile(Path(f"{seed_db}-shm"), state_dir / "state.db-shm")
        (state_dir / "events.jsonl").write_text("sentinel-event\n", encoding="utf-8")
        return state_dir
    finally:
        conn.close()


def _create_hot_rollback_journal(tmp_path: Path, *, version: int) -> Path:
    """Crash a subprocess mid-transaction, leaving a genuine hot journal."""
    import sqlite3
    import subprocess
    import sys

    state_dir = tmp_path / "hot-journal-state"
    state_dir.mkdir()
    db_path = state_dir / "state.db"
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA journal_mode = DELETE").fetchone()[0] == "delete"
        conn.execute("CREATE TABLE payload (value BLOB NOT NULL)")
        conn.execute("INSERT INTO payload VALUES (zeroblob(1048576))")
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()

    script = (
        "import os, sqlite3, sys; "
        "c=sqlite3.connect(sys.argv[1]); "
        "c.execute('PRAGMA journal_mode=DELETE'); "
        "c.execute('PRAGMA synchronous=FULL'); "
        "c.execute('PRAGMA cache_size=1'); "
        "c.execute('BEGIN IMMEDIATE'); "
        "c.execute(\"UPDATE payload SET value=randomblob(1048576)\"); "
        "os._exit(0)"
    )
    completed = subprocess.run([sys.executable, "-c", script, str(db_path)], check=False)
    assert completed.returncode == 0
    journal_path = Path(f"{db_path}-journal")
    assert journal_path.exists()
    assert journal_path.stat().st_size > 512
    (state_dir / "events.jsonl").write_text("sentinel-event\n", encoding="utf-8")
    return state_dir


def test_future_delete_schema_refuses_before_mutation(tmp_path: Path) -> None:
    """A future rollback-mode database is byte-identical after initialize refusal."""
    import sqlite3

    from anvil.clock import SystemClock
    from anvil.state.backend import SchemaMismatch
    from anvil.state.sqlite import SqliteBackend

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "state.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    (state_dir / "events.jsonl").write_text("sentinel-event\n", encoding="utf-8")
    before = _state_manifest(state_dir)

    backend = SqliteBackend(
        db_path=str(db_path),
        events_path=str(state_dir / "events.jsonl"),
        clock=SystemClock(),
    )
    with pytest.raises(SchemaMismatch) as raised:
        backend.initialize()

    assert raised.value.actual == SCHEMA_VERSION + 1
    assert raised.value.expected == SCHEMA_VERSION
    assert raised.value.direction == "newer"
    assert "delete state.db to start fresh" not in str(raised.value).lower()
    assert "do not delete state" in str(raised.value).lower()
    assert backend._conn is None  # noqa: SLF001
    assert _state_manifest(state_dir) == before


def test_future_hot_rollback_journal_refuses_without_live_recovery(tmp_path: Path) -> None:
    """Hot-journal recovery happens only on a disposable byte-stable copy."""
    from anvil.clock import SystemClock
    from anvil.state.backend import SchemaMismatch
    from anvil.state.sqlite import SqliteBackend, read_db_schema_version

    state_dir = _create_hot_rollback_journal(
        tmp_path,
        version=SCHEMA_VERSION + 1,
    )
    before = _state_manifest(state_dir)
    assert read_db_schema_version(state_dir / "state.db") == SCHEMA_VERSION + 1
    assert _state_manifest(state_dir) == before

    backend = SqliteBackend(
        db_path=str(state_dir / "state.db"),
        events_path=str(state_dir / "events.jsonl"),
        clock=SystemClock(),
    )
    with pytest.raises(SchemaMismatch, match="newer"):
        backend.initialize()

    assert backend._conn is None  # noqa: SLF001
    assert _state_manifest(state_dir) == before


@pytest.mark.parametrize("include_shm", [False, True])
def test_future_wal_schema_refuses_without_touching_live_sidecars(
    tmp_path: Path, include_shm: bool
) -> None:
    """Uncheckpointed WAL user_version is read from a disposable stable copy."""
    from anvil.clock import SystemClock
    from anvil.state.backend import SchemaMismatch
    from anvil.state.sqlite import SqliteBackend, read_db_schema_version

    state_dir = _copy_live_wal_fixture(
        tmp_path,
        version=SCHEMA_VERSION + 1,
        include_shm=include_shm,
    )
    before = _state_manifest(state_dir)
    assert read_db_schema_version(state_dir / "state.db") == SCHEMA_VERSION + 1

    backend = SqliteBackend(
        db_path=str(state_dir / "state.db"),
        events_path=str(state_dir / "events.jsonl"),
        clock=SystemClock(),
    )
    with pytest.raises(SchemaMismatch, match="newer"):
        backend.initialize()

    assert backend._conn is None  # noqa: SLF001
    assert _state_manifest(state_dir) == before


def test_future_wal_appearing_between_probes_never_reaches_live_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The serialized confirmation catches a WAL that races the first probe."""
    import sqlite3

    import anvil.state.sqlite as sqlite_backend
    from anvil.clock import SystemClock
    from anvil.state.backend import SchemaProbeFailed
    from anvil.state.sqlite import SqliteBackend

    state_dir = tmp_path / "racing-wal"
    state_dir.mkdir()
    db_path = state_dir / "state.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("CREATE TABLE sentinel (value INTEGER NOT NULL)")
        conn.execute("INSERT INTO sentinel VALUES (1)")
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    (state_dir / "events.jsonl").write_text("sentinel-event\n", encoding="utf-8")

    original_probe = sqlite_backend.read_db_schema_version
    calls = 0
    writer: sqlite3.Connection | None = None
    raced_manifest: dict[str, tuple[int, str]] = {}

    def race_once(path: str | os.PathLike[str]) -> int:
        nonlocal calls, writer, raced_manifest
        calls += 1
        if calls == 1:
            writer = sqlite3.connect(db_path)
            writer.execute("PRAGMA journal_mode = WAL")
            writer.execute("PRAGMA wal_autocheckpoint = 0")
            if hasattr(sqlite3, "SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE"):
                writer.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
            writer.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
            writer.execute("UPDATE sentinel SET value = 2")
            writer.commit()
            raced_manifest = _state_manifest(state_dir)
            return SCHEMA_VERSION
        return original_probe(path)

    monkeypatch.setattr(sqlite_backend, "read_db_schema_version", race_once)
    backend = SqliteBackend(
        db_path=str(db_path),
        events_path=str(state_dir / "events.jsonl"),
        clock=SystemClock(),
    )
    try:
        with pytest.raises(SchemaProbeFailed, match="changed during"):
            backend.initialize()
        assert calls == 2
        assert backend._conn is None  # noqa: SLF001
        assert _state_manifest(state_dir) == raced_manifest
    finally:
        if writer is not None:
            writer.close()


def test_unreadable_wal_probe_fails_closed_without_live_fallback(
    tmp_path: Path,
) -> None:
    """A corrupt WAL fails closed without retrying through live SQLite state."""
    from anvil.clock import SystemClock
    from anvil.state.backend import SchemaProbeFailed
    from anvil.state.sqlite import SqliteBackend

    state_dir = _copy_live_wal_fixture(
        tmp_path,
        version=SCHEMA_VERSION,
        include_shm=False,
    )
    wal_path = state_dir / "state.db-wal"
    wal = bytearray(wal_path.read_bytes())
    wal[24] ^= 0xFF
    wal_path.write_bytes(wal)
    before = _state_manifest(state_dir)
    backend = SqliteBackend(
        db_path=str(state_dir / "state.db"),
        events_path=str(state_dir / "events.jsonl"),
        clock=SystemClock(),
    )
    with pytest.raises(SchemaProbeFailed, match="checksum"):
        backend.initialize()

    assert backend._conn is None  # noqa: SLF001
    assert _state_manifest(state_dir) == before


def test_schema_initialize_lock_is_reentrant_across_nested_paths(
    tmp_path: Path,
) -> None:
    """A nested A-to-B-to-A replay reuses A's existing OS-lock ownership."""
    from anvil.state.sqlite import _schema_initialization_lock

    a_db = str(tmp_path / "a.db")
    a_events = str(tmp_path / "a.events.jsonl")
    b_db = str(tmp_path / "b.db")
    b_events = str(tmp_path / "b.events.jsonl")
    with _schema_initialization_lock(a_db, a_events):
        with _schema_initialization_lock(b_db, b_events):
            with _schema_initialization_lock(a_db, a_events):
                pass


def test_schema_initialize_lock_keeps_one_cross_process_token(
    tmp_path: Path,
) -> None:
    """Creating state.db mid-initialize cannot move a contender to another lock."""
    import subprocess
    import sys
    import time

    db_path = tmp_path / "state.db"
    events_path = tmp_path / "events.jsonl"
    locked = tmp_path / "locked"
    release = tmp_path / "release"
    acquired = tmp_path / "acquired"
    first_script = f"""
import time
from pathlib import Path
from anvil.state.sqlite import _schema_initialization_lock
with _schema_initialization_lock({str(db_path)!r}, {str(events_path)!r}):
    Path({str(db_path)!r}).touch()
    Path({str(locked)!r}).touch()
    deadline = time.monotonic() + 10
    while not Path({str(release)!r}).exists():
        if time.monotonic() > deadline:
            raise SystemExit('release timeout')
        time.sleep(0.01)
"""
    second_script = f"""
from pathlib import Path
from anvil.state.sqlite import _schema_initialization_lock
with _schema_initialization_lock({str(db_path)!r}, {str(events_path)!r}):
    Path({str(acquired)!r}).touch()
"""
    first = subprocess.Popen(
        [sys.executable, "-c", first_script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    second: subprocess.Popen[bytes] | None = None
    try:
        deadline = time.monotonic() + 10
        while not locked.exists():
            if first.poll() is not None:
                _, stderr = first.communicate()
                pytest.fail(f"first initializer exited early: {stderr.decode()}")
            if time.monotonic() > deadline:
                pytest.fail("first initializer did not acquire schema lock")
            time.sleep(0.01)

        second = subprocess.Popen(
            [sys.executable, "-c", second_script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        time.sleep(0.25)
        assert not acquired.exists()
        release.touch()
        first.communicate(timeout=10)
        second.communicate(timeout=10)
        assert first.returncode == 0
        assert second.returncode == 0
        assert acquired.exists()
    finally:
        release.touch(exist_ok=True)
        for process in (first, second):
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate()


# ---------------------------------------------------------------------------
# MUST-FIX 2: status reports an un-migratable schema cleanly (no traceback)
# ---------------------------------------------------------------------------


def test_status_unknown_schema_version_clean_error_human(tmp_path: Path) -> None:
    """user_version=99 -> status exits 1 with a clean 'Error:' line, no traceback."""
    _init(tmp_path)
    _stamp_user_version(tmp_path / ".anvil", 99)

    # catch_exceptions defaults True here so an UNCAUGHT exception would surface
    # as res.exception (a traceback) rather than a clean exit — we assert none.
    res = _invoke(tmp_path, ["status"])
    assert res.exit_code == 1, res.output
    assert res.exception is None or isinstance(res.exception, SystemExit), (
        f"status raised a traceback: {res.exception!r}"
    )
    combined = res.output + (getattr(res, "stderr", "") or "")
    assert "Error:" in combined
    assert "99" in combined


def test_status_unknown_schema_version_clean_error_json(tmp_path: Path) -> None:
    """user_version=99 -> status --json returns a schema_mismatch envelope, exit 1."""
    _init(tmp_path)
    _stamp_user_version(tmp_path / ".anvil", 99)

    res = _invoke(tmp_path, ["status", "--json"])
    assert res.exit_code == 1, res.output
    assert res.exception is None or isinstance(res.exception, SystemExit), (
        f"status --json raised a traceback: {res.exception!r}"
    )
    env = json.loads(res.stdout.strip())
    assert env["ok"] is False
    assert env["command"] == "status"
    assert env["error"]["code"] == "schema_mismatch"
    assert env["error"]["message"]


def test_status_hook_format_unknown_schema_version_exits_zero(
    tmp_path: Path,
) -> None:
    """Hook safety: a bad schema must not fail the SessionStart hook."""
    _init(tmp_path)
    _stamp_user_version(tmp_path / ".anvil", 99)

    res = _invoke(tmp_path, ["status", "--hook-format"])
    assert res.exit_code == 0, res.output
    line = res.output.strip()
    assert line.startswith("schema_mismatch ")
    assert f"supported-schema:{SCHEMA_VERSION}" in line
    assert "database-schema:99" in line
    assert "prd-status:" not in line


@pytest.mark.parametrize(
    ("arguments", "expected_code", "expected_output"),
    [
        (["status", "--json"], 1, "schema_probe_failed"),
        (["status", "--hook-format"], 0, "schema_probe_failed"),
    ],
)
def test_status_translates_schema_probe_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
    expected_code: int,
    expected_output: str,
) -> None:
    """Probe refusal remains typed for JSON and non-blocking for SessionStart."""
    from anvil.state.backend import SchemaProbeFailed

    _init(tmp_path)

    def fail_probe(
        _probe: object, _path: str | os.PathLike[str]
    ) -> int:
        raise SchemaProbeFailed("Database schema probe refused safely.")

    monkeypatch.setattr("anvil.cli._helpers.BoundedSchemaProbe.__call__", fail_probe)
    result = _invoke(tmp_path, arguments)

    assert result.exit_code == expected_code, result.output
    if "--json" in arguments:
        envelope = json.loads(result.stdout)
        assert envelope["error"]["code"] == expected_output
    else:
        assert result.output.strip().startswith(expected_output + " ")
