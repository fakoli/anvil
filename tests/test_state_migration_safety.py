"""Migration admission and recovery are shared by CLI and normal engine open."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest
from typer.testing import CliRunner

from anvil.cli import app
from anvil.clock import FrozenClock
from anvil.state.backend import SchemaMismatch, SchemaProbeFailed
from anvil.state.snapshot import serialize_state
from anvil.state.sqlite import SqliteBackend
from tests.test_bundle_scope_migration import _seed_claim
from tests.test_bundle_state import _T0, _backend, _create_bundle, _event, _seed


def _files(root: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()}


def _older_state(root: Path) -> Path:
    state = root / ".anvil"
    state.mkdir()
    backend = _backend(state)
    _seed(backend)
    backend.close()
    with closing(sqlite3.connect(state / "state.db")) as connection:
        connection.execute("PRAGMA user_version = 25")
        connection.commit()
    (state / "config.yaml").write_text("events_storage: local\n", encoding="utf-8")
    return state


def _invoke(root: Path, monkeypatch, arguments: list[str]):
    monkeypatch.chdir(root)
    monkeypatch.setenv("ANVIL_ROOT", str(root))
    monkeypatch.setenv("ANVIL_STATE_LAYOUT", "local")
    return CliRunner().invoke(app, arguments)


@pytest.mark.parametrize("only_bundle_custody", [False, True])
def test_normal_open_refuses_active_custody_without_live_mutation(tmp_path, only_bundle_custody):
    backend, _, _ = _seed_claim(tmp_path)
    backend.close()
    with closing(sqlite3.connect(tmp_path / "state.db")) as connection:
        connection.execute("PRAGMA user_version = 25")
        connection.commit()
        if only_bundle_custody:
            # A malformed legacy projection must not hide the public bundle lease.
            connection.execute("UPDATE claims SET status = 'released'")
            connection.commit()
    before = _files(tmp_path)
    with pytest.raises(SchemaProbeFailed, match="Active task or bundle custody"):
        _backend(tmp_path)
    assert _files(tmp_path) == before


@pytest.mark.parametrize("projection_is_active", [False, True])
def test_migration_refuses_custody_when_log_and_projection_disagree(
    tmp_path, monkeypatch, projection_is_active,
):
    """Either side of a claim/release crash blocks preview and every apply path."""
    from anvil.bundles.manager import BundleManager
    from anvil.roots.registry import RootSetRegistry

    state = tmp_path / ".anvil"
    state.mkdir()
    backend = _backend(state)
    _seed(backend)
    _create_bundle(backend)
    monkeypatch.setattr(
        "anvil.roots.registry.RootSetRegistry",
        lambda: RootSetRegistry(tmp_path / "native-home"),
    )
    manager = BundleManager(
        backend, FrozenClock(_T0), actor="codex-main", project_root=tmp_path,
    )
    if projection_is_active:
        manager.claim("B001")
    saved_projection = state / "before-crash.db"
    with closing(sqlite3.connect(saved_projection)) as saved:
        backend._require_conn().backup(saved)
        saved.execute("PRAGMA journal_mode=DELETE")
        saved.execute("PRAGMA user_version=25")
    if projection_is_active:
        manager.release("B001", reason="Release logged before projection crash")
    else:
        manager.claim("B001")
    backend.close()
    shutil.copyfile(saved_projection, state / "state.db")
    for suffix in ("-wal", "-shm"):
        (state / f"state.db{suffix}").unlink(missing_ok=True)
    (state / "config.yaml").write_text("events_storage: local\n", encoding="utf-8")
    before = _files(state)
    with pytest.raises(SchemaProbeFailed, match="Active task or bundle custody"):
        _backend(state)
    assert _files(state) == before
    for options in (["--json"], ["--yes", "--json"]):
        result = _invoke(tmp_path, monkeypatch, ["migrate", "state", *options])
        assert result.exit_code == 1, result.output
        assert json.loads(result.stdout)["error"]["code"] == "active_claims"
        assert _files(state) == before


@pytest.mark.parametrize("missing_projection", [False, True])
def test_empty_projection_refuses_custody_but_missing_projection_retains_bootstrap(
    tmp_path, monkeypatch, missing_projection,
):
    state = tmp_path / ".anvil"
    state.mkdir()
    backend, _, _ = _seed_claim(state)
    original_projection = serialize_state(backend)
    backend.close()
    if missing_projection:
        (state / "state.db").unlink()
    else:
        (state / "state.db").write_bytes(b"")
    for suffix in ("-wal", "-shm"):
        (state / f"state.db{suffix}").unlink(missing_ok=True)
    (state / "config.yaml").write_text("events_storage: local\n", encoding="utf-8")
    before = _files(state)
    for options in (["--json"], ["--yes", "--json"]):
        result = _invoke(tmp_path, monkeypatch, ["migrate", "state", *options])
        assert result.exit_code == 1, result.output
        if not missing_projection:
            assert json.loads(result.stdout)["error"]["code"] == "active_claims"
        assert _files(state) == before
    if missing_projection:
        # A fresh clone can reconstruct active shared claims from its authority;
        # no prior projection schema exists to upgrade.
        rebuilt = _backend(state)
        try:
            assert serialize_state(rebuilt) == original_projection
            assert (state / "events.jsonl").read_bytes() == before["events.jsonl"]
        finally:
            rebuilt.close()
    else:
        with pytest.raises(SchemaProbeFailed, match="Active task or bundle custody"):
            _backend(state)
        assert _files(state) == before



@pytest.mark.parametrize("empty_projection", [False, True])
def test_normal_open_refuses_missing_authority_without_creating_log(tmp_path, empty_projection):
    state = _older_state(tmp_path)
    if empty_projection:
        (state / "state.db").write_bytes(b"")
    (state / "events.jsonl").unlink()
    before = _files(state)
    backend = SqliteBackend(
        db_path=str(state / "state.db"), events_path=str(state / "events.jsonl"),
        clock=FrozenClock(_T0),
    )
    with pytest.raises(SchemaProbeFailed, match="authority log is unavailable"):
        backend.initialize()
    assert _files(state) == before


def test_cli_status_and_migration_refuse_active_custody(tmp_path, monkeypatch):
    state = tmp_path / ".anvil"
    state.mkdir()
    backend, _, _ = _seed_claim(state)
    backend.close()
    with closing(sqlite3.connect(state / "state.db")) as connection:
        connection.execute("PRAGMA user_version = 25")
        connection.commit()
    (state / "config.yaml").write_text("events_storage: local\n", encoding="utf-8")
    before = _files(state)
    for arguments, error in (
        (["status", "--json"], "schema_probe_failed"),
        (["migrate", "state", "--yes", "--json"], "active_claims"),
    ):
        result = _invoke(tmp_path, monkeypatch, arguments)
        assert result.exit_code == 1, result.output
        assert json.loads(result.output)["error"]["code"] == error
        assert _files(state) == before


def test_active_custody_refusal_does_not_create_missing_event_log(tmp_path):
    backend, _, _ = _seed_claim(tmp_path)
    backend.close()
    with closing(sqlite3.connect(tmp_path / "state.db")) as connection:
        connection.execute("PRAGMA user_version = 25")
    (tmp_path / "events.jsonl").unlink()
    before = _files(tmp_path)
    backend = SqliteBackend(
        db_path=str(tmp_path / "state.db"), events_path=str(tmp_path / "events.jsonl"),
        clock=FrozenClock(_T0),
    )
    with pytest.raises(SchemaProbeFailed, match="authority log is unavailable"):
        backend.initialize()
    assert _files(tmp_path) == before


def test_preview_refuses_preexisting_backup_without_clobbering(tmp_path, monkeypatch):
    state = _older_state(tmp_path)
    (state / "state.db.pre-schema-migration.bak.events.jsonl").write_bytes(b"retained backup")
    before = _files(state)
    result = _invoke(tmp_path, monkeypatch, ["migrate", "state", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.output)["error"]["code"] == "backup_exists"
    assert _files(state) == before


def test_preview_apply_and_repeat_keep_exact_log_and_standalone_backup(tmp_path, monkeypatch):
    state = _older_state(tmp_path)
    before = _files(state)
    preview = _invoke(tmp_path, monkeypatch, ["migrate", "state", "--json"])
    assert preview.exit_code == 0, preview.output
    assert json.loads(preview.output)["data"]["applied"] is False
    assert _files(state) == before
    result = _invoke(tmp_path, monkeypatch, ["migrate", "state", "--yes", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert (data["from_version"], data["to_version"], data["applied"]) == (25, 26, True)
    assert (state / "events.jsonl").read_bytes() == before["events.jsonl"]
    assert Path(data["backup"] + ".events.jsonl").read_bytes() == before["events.jsonl"]
    assert Path(data["backup"] + ".config.yaml").read_bytes() == before["config.yaml"]
    with closing(sqlite3.connect(data["backup"])) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 25
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    if os.name != "nt":
        assert all(Path(path).stat().st_mode & 0o077 == 0 for path in data["backup_artifacts"])
    repeat = _invoke(tmp_path, monkeypatch, ["migrate", "state", "--yes", "--json"])
    assert repeat.exit_code == 0, repeat.output
    assert json.loads(repeat.output)["data"]["migrated"] is False


def test_database_backup_contains_committed_wal_without_touching_source(tmp_path):
    from anvil.cli.migrate import _backup_database

    database = tmp_path / "source.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE marker (value TEXT)")
        connection.execute("INSERT INTO marker VALUES ('committed in WAL')")
        connection.commit()
        before = _files(tmp_path)
        destination = tmp_path / "backup.db"
        _backup_database(database, destination)
        assert {name: (tmp_path / name).read_bytes() for name in before} == before
        with closing(sqlite3.connect(destination)) as restored:
            assert restored.execute("SELECT value FROM marker").fetchone()[0] == "committed in WAL"
    finally:
        connection.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX subprocess contention probe")
def test_migration_holds_event_lock_through_backup(tmp_path, monkeypatch):
    import anvil.cli.migrate as migration

    state = _older_state(tmp_path)
    original = migration._backup_database
    probes = []

    def backup_under_contention(database, destination):
        script = (
            "import fcntl,sys\n"
            "with open(sys.argv[1], 'rb') as log:\n"
            " try: fcntl.flock(log, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            " except BlockingIOError: sys.exit(0)\n"
            "sys.exit(1)\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script, str(state / "events.jsonl")],
            capture_output=True, timeout=5,
        )
        probes.append(result.returncode)
        assert result.returncode == 0
        original(database, destination)

    monkeypatch.setattr(migration, "_backup_database", backup_under_contention)
    result = _invoke(tmp_path, monkeypatch, ["migrate", "state", "--yes", "--json"])
    assert result.exit_code == 0, result.output
    assert probes == [0]


def test_open_writer_refuses_changed_schema_before_event_append(tmp_path):
    backend = _backend(tmp_path)
    try:
        with closing(sqlite3.connect(tmp_path / "state.db")) as connection:
            connection.execute("PRAGMA user_version = 27")
        before = (tmp_path / "events.jsonl").read_bytes()
        with pytest.raises(SchemaMismatch):
            backend.append(_event("state.initialized", {}, target_kind="project", target_id="proj"))
        assert (tmp_path / "events.jsonl").read_bytes() == before
    finally:
        backend.close()
