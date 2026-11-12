"""Tests for anvil backup/restore — S3 durable storage.

Uses a FakeDurableStore (in-memory) for all unit tests; no network, no moto.
A live S3 test is guarded by ANVIL_TEST_S3_BUCKET.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from anvil.cli import app
from anvil.clock import FrozenClock
from anvil.state.snapshot import serialize_state
from anvil.state.sqlite import SqliteBackend

runner = CliRunner()
_T0 = datetime(2026, 5, 24, 18, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fake store — in-memory stand-in; satisfies DurableStore structurally
# ---------------------------------------------------------------------------


class FakeDurableStore:
    """In-memory stand-in. Satisfies DurableStore structurally."""

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    def push(self, local_path: str, remote_key: str) -> str:
        self._blobs[remote_key] = Path(local_path).read_bytes()
        return f"fake://{remote_key}"

    def pull(self, remote_key: str, local_path: str) -> None:
        Path(local_path).write_bytes(self._blobs[remote_key])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_backend(tmp_path: Path, *, db_name: str = "state.db") -> SqliteBackend:
    db_path = str(tmp_path / db_name)
    events_path = str(tmp_path / "events.jsonl")
    Path(events_path).touch()
    b = SqliteBackend(
        db_path=db_path,
        events_path=events_path,
        clock=FrozenClock(_T0),
    )
    b.initialize()
    return b


def _seed_backend(backend: SqliteBackend) -> None:
    """Ensure events.jsonl has real events (initialize() already writes one)."""
    # initialize() writes a state.initialized event — that's enough for the
    # round-trip test to exercise a non-empty log.
    pass


def _write_s3_config(state_dir: Path) -> None:
    """Write a minimal config.yaml with durable_store: s3 into state_dir."""
    (state_dir / "config.yaml").write_text(
        "project_name: 'backup-test'\n"
        "project_id: 'proj-001'\n"
        "durable_store: s3\n"
        "s3_bucket: test-bucket\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# 1. Push/pull round-trip — byte identical
# ---------------------------------------------------------------------------


def test_push_round_trips_events_jsonl(tmp_path: Path) -> None:
    """push then pull produces byte-identical events.jsonl."""
    original = tmp_path / "events.jsonl"
    original.write_text('{"type":"test","ts":"2026-01-01"}\n', encoding="utf-8")

    store = FakeDurableStore()
    store.push(str(original), "events.jsonl")

    dest = tmp_path / "restored.jsonl"
    store.pull("events.jsonl", str(dest))

    assert dest.read_bytes() == original.read_bytes()


# ---------------------------------------------------------------------------
# 2. Restore rebuilds state.db
# ---------------------------------------------------------------------------


def test_restore_rebuilds_state_db(tmp_path: Path) -> None:
    """After push+restore, serialize_state matches original."""
    # Build a real backend with one event.
    backend = _make_backend(tmp_path)
    _seed_backend(backend)
    original_state = serialize_state(backend)
    events_path = backend._events_path  # noqa: SLF001
    db_path = backend._db_path  # noqa: SLF001
    backend.close()

    # Push events.jsonl to fake store.
    store = FakeDurableStore()
    store.push(events_path, "events.jsonl")

    # Wipe state.db to simulate a fresh machine.
    Path(db_path).unlink()

    # Pull events.jsonl back, replay into a scratch db, then atomically replace.
    import tempfile

    from anvil.cli.backup import _replay_into

    with tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl") as tmp_f:
        tmp_events = tmp_f.name
    store.pull("events.jsonl", tmp_events)

    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp_db:
        scratch_db = tmp_db.name

    _replay_into(tmp_events, scratch_db, tmp_path)
    Path(scratch_db).replace(tmp_path / "state.db")
    Path(tmp_events).replace(Path(events_path))

    # Re-open and compare.
    restored = SqliteBackend(
        db_path=db_path,
        events_path=events_path,
        clock=FrozenClock(_T0),
    )
    restored.initialize()
    restored_state = serialize_state(restored)
    restored.close()

    assert json.dumps(original_state, sort_keys=True) == json.dumps(
        restored_state, sort_keys=True
    )


# ---------------------------------------------------------------------------
# 3. CLI backup with missing config exits non-zero
# ---------------------------------------------------------------------------


def test_backup_cli_missing_config_exits_nonzero(tmp_path: Path) -> None:
    """anvil backup with no config.yaml exits with code 1."""
    # Create an empty .anvil dir but no config.yaml.
    state_dir = tmp_path / ".anvil"
    state_dir.mkdir()

    result = runner.invoke(app, ["backup", "--cwd", str(tmp_path)])
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# 4. Restore CLI aborts on 'n' confirm
# ---------------------------------------------------------------------------


def test_restore_cli_confirm_abort(tmp_path: Path) -> None:
    """anvil restore without --yes aborts (non-zero) when user says 'n'."""
    state_dir = tmp_path / ".anvil"
    state_dir.mkdir()
    _write_s3_config(state_dir)
    # events.jsonl must exist for the prompt to be reached, but we abort before S3.
    (state_dir / "events.jsonl").touch()

    result = runner.invoke(
        app, ["restore", "--cwd", str(tmp_path)], input="n\n"
    )
    # Aborted confirm → non-zero exit; state.db is untouched (didn't exist).
    assert result.exit_code != 0
    assert not (state_dir / "state.db").exists()


# ---------------------------------------------------------------------------
# 5. Live S3 test (guarded by env var)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("ANVIL_TEST_S3_BUCKET"),
    reason="ANVIL_TEST_S3_BUCKET not set",
)
def test_s3_push_pull_live(tmp_path: Path) -> None:
    """Live S3 round-trip: push a tiny file, pull it back, assert content, cleanup."""
    pytest.importorskip("boto3")
    from anvil.state.durable import S3DurableStore

    bucket = os.environ["ANVIL_TEST_S3_BUCKET"]
    prefix = "anvil-test-ci"
    remote_key = f"test-{os.getpid()}.txt"

    test_file = tmp_path / "test.txt"
    test_file.write_bytes(b"anvil-s3-test")

    store = S3DurableStore(bucket=bucket, prefix=prefix)
    try:
        uri = store.push(str(test_file), remote_key)
        assert uri == f"s3://{bucket}/{prefix}/{remote_key}"

        dest = tmp_path / "pulled.txt"
        store.pull(remote_key, str(dest))
        assert dest.read_bytes() == b"anvil-s3-test"
    finally:
        # Cleanup — best effort.
        try:
            store._s3.delete_object(  # noqa: SLF001
                Bucket=bucket, Key=f"{prefix}/{remote_key}"
            )
        except Exception:  # noqa: BLE001
            pass


def test_warm_backup_uploads_closed_wal_snapshot_and_matching_log_after_custody(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib
    import sqlite3

    from anvil.state import sqlite as sqlite_module
    from tests.test_sqlite_file_safety import _require_retained_lock, _retained_database

    backup_module = importlib.import_module("anvil.cli.backup")
    state = tmp_path / ".anvil"
    state.mkdir()
    _write_s3_config(state)
    uploaded_paths = []
    with _retained_database(state) as (database, events, retained):
        events.write_bytes(b"captured-event-log\n")

        class ObservingStore(FakeDurableStore):
            def push(self, local_path: str, remote_key: str) -> str:
                path = Path(local_path)
                uploaded_paths.append(path)
                assert path.parent != state
                assert getattr(sqlite_module._SCHEMA_INITIALIZATION_LOCAL, "stack", []) == []
                if os.name == "posix":
                    _require_retained_lock(database)
                # The upload begins after capture and custody release. Later
                # live commits cannot change either staged artifact.
                if remote_key == "events.jsonl":
                    retained.execute("INSERT INTO retained_owner_probe VALUES ('during-upload')")
                    retained.commit()
                    events.write_bytes(b"later-event-log\n")
                else:
                    assert not Path(f"{path}-wal").exists()
                    assert not Path(f"{path}-journal").exists()
                    snapshot = sqlite3.connect(path)
                    try:
                        assert snapshot.execute(
                            "SELECT value FROM retained_owner_probe"
                        ).fetchall() == [("before-inspection",)]
                        assert snapshot.execute("PRAGMA integrity_check").fetchone() == ("ok",)
                    finally:
                        snapshot.close()
                return super().push(local_path, remote_key)

        store = ObservingStore()
        monkeypatch.setattr(backup_module, "_make_store", lambda _: store)
        result = runner.invoke(app, ["backup", "--include-db", "--cwd", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert store._blobs["events.jsonl"] == b"captured-event-log\n"
        assert retained.execute("SELECT COUNT(*) FROM retained_owner_probe").fetchone() == (2,)
        if os.name == "posix":
            _require_retained_lock(database)
    assert len(uploaded_paths) == 2
    assert all(not path.exists() for path in uploaded_paths)


def _restore_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import importlib

    from tests.test_git_events import _build_local_project

    _build_local_project(tmp_path)
    state = tmp_path / ".anvil"
    with (state / "config.yaml").open("a") as stream:
        stream.write("durable_store: s3\ns3_bucket: test-bucket\n")
    module = importlib.import_module("anvil.cli.backup")
    store = FakeDurableStore()
    monkeypatch.setattr(module, "_make_store", lambda _: store)
    return state, module, store


@pytest.mark.parametrize("storage", ["local", "git"])
def test_restore_preserves_live_inode_and_retained_native_connection(
    tmp_path, monkeypatch, storage,
):
    import sqlite3

    from anvil.cli.doctor import _OK, _check_replay
    from anvil.state import sqlite as sqlite_module
    from tests.test_event_format_migration_safety import _append_native_sync_metadata
    from tests.test_git_events import _run_in

    if storage == "git" and os.name == "nt":
        pytest.skip("Windows conversion is intentionally refused")
    state, module, store = _restore_fixture(tmp_path, monkeypatch)
    if storage == "git":
        converted = _run_in(tmp_path, ["migrate-events", "--to", "git", "--yes"])
        assert converted.exit_code == 0, converted.output
    store.push(str(state / "events.jsonl"), "events.jsonl")
    original_inode = (state / "state.db").stat().st_ino
    retained = SqliteBackend(
        db_path=str(state / "state.db"), events_path=str(state / "events.jsonl"),
        clock=FrozenClock(_T0), events_storage=storage,
    )
    retained.initialize()
    original_pull = store.pull

    def pull_outside_custody(key, destination):
        assert not getattr(sqlite_module._STATE_OPERATION_LOCAL, "owned", {})
        assert not getattr(sqlite_module._SCHEMA_INITIALIZATION_LOCAL, "stack", [])
        original_pull(key, destination)

    monkeypatch.setattr(store, "pull", pull_outside_custody)
    try:
        _append_native_sync_metadata(retained, {"after_backup": True})
        assert len((state / "events.jsonl").read_text().splitlines()) == 25
        restored = runner.invoke(app, ["restore", "--yes", "--cwd", str(tmp_path)])
        assert restored.exit_code == 0, (restored.output, restored.exception)
        assert (state / "state.db").stat().st_ino == original_inode
        assert len((state / "events.jsonl").read_text().splitlines()) == 24
        _append_native_sync_metadata(retained, {"after_restore": True})
        with sqlite3.connect(state / "state.db") as fresh:
            assert fresh.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 25
        events = [json.loads(line) for line in (state / "events.jsonl").read_text().splitlines()]
        assert len(events) == 25
        if storage == "local":
            assert [event["id"] for event in events] == [f"E{index:06d}" for index in range(1, 26)]
        snapshot = runner.invoke(app, ["project", "snapshot", "--json", "--cwd", str(tmp_path)])
        assert snapshot.exit_code == 0, snapshot.output
        replay = _check_replay(retained, state)
        assert replay.severity == _OK, replay.to_json()
    finally:
        retained.close()


@pytest.mark.parametrize("marker", [".event-format-migration.json", ".local-event-recovery.json"])
def test_restore_refuses_pending_recovery_before_download(tmp_path, monkeypatch, marker):
    from tests.test_cli import _recursive_file_manifest

    state, _, store = _restore_fixture(tmp_path, monkeypatch)
    (state / marker).write_text("pending recovery")
    before = _recursive_file_manifest(state)
    monkeypatch.setattr(store, "pull", lambda *_: pytest.fail("download entered pending State"))
    restored = runner.invoke(app, ["restore", "--yes", "--cwd", str(tmp_path)])
    assert restored.exit_code != 0
    assert _recursive_file_manifest(state) == before
    assert not (state / ".state-operation.lock").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX conversion; Windows apply refuses")
@pytest.mark.parametrize("phase", ["after_marker", "after_database", "complete"])
def test_restore_refuses_conversion_during_download_and_preserves_recovery(
    tmp_path, monkeypatch, phase,
):
    import anvil.cli.migrate as migration
    from tests.test_cli import _recursive_file_manifest
    from tests.test_git_events import _run_in

    state, _, store = _restore_fixture(tmp_path, monkeypatch)
    original_log = (state / "events.jsonl").read_bytes()
    observed = {}

    def interrupt(selected):
        if selected == phase:
            raise RuntimeError("ordinary publication fault fixture")

    def pull_while_conversion_runs(key, destination):
        assert key == "events.jsonl"
        monkeypatch.setattr(migration, "_EVENT_CONVERSION_PHASE_HOOK", interrupt)
        converted = _run_in(tmp_path, ["migrate-events", "--to", "git", "--yes"])
        assert converted.exit_code == (0 if phase == "complete" else 1), converted.output
        monkeypatch.setattr(migration, "_EVENT_CONVERSION_PHASE_HOOK", None)
        observed["before_restore_publication"] = _recursive_file_manifest(state)
        # The owned download stage is removed on refusal, not live State.
        observed["before_restore_publication"].pop(Path(destination).name)
        observed["inode"] = (state / "state.db").stat().st_ino
        Path(destination).write_bytes(original_log)

    monkeypatch.setattr(store, "pull", pull_while_conversion_runs)
    restored = runner.invoke(app, ["restore", "--yes", "--cwd", str(tmp_path)])
    assert restored.exit_code != 0
    assert _recursive_file_manifest(state) == observed["before_restore_publication"]
    assert (state / "state.db").stat().st_ino == observed["inode"]
    if phase != "complete":
        resumed = _run_in(tmp_path, ["migrate-events", "--to", "git", "--resume", "--yes"])
        assert resumed.exit_code == 0, resumed.output
    assert not (state / ".event-format-migration.json").exists()


def test_restore_waits_for_permanent_custody_before_publication(tmp_path, monkeypatch):
    import threading

    from anvil.state import sqlite as sqlite_module
    from tests.test_cli import _recursive_file_manifest

    state, _, store = _restore_fixture(tmp_path, monkeypatch)
    store.push(str(state / "events.jsonl"), "events.jsonl")
    ready, release = threading.Event(), threading.Event()
    failures = []

    def own_token():
        try:
            with sqlite_module._state_operation_lock(state / "state.db", create=True):
                ready.set()
                assert release.wait(10)
        except BaseException as exc:
            failures.append(exc)

    holder = threading.Thread(target=own_token)
    holder.start()
    try:
        assert ready.wait(5)
        before = _recursive_file_manifest(state)
        inode = (state / "state.db").stat().st_ino
        monkeypatch.setattr(sqlite_module, "_FLOCK_TIMEOUT_S", 0.05)
        restored = runner.invoke(app, ["restore", "--yes", "--cwd", str(tmp_path)])
        assert restored.exit_code != 0
        assert _recursive_file_manifest(state) == before
        assert (state / "state.db").stat().st_ino == inode
    finally:
        release.set()
        holder.join(timeout=5)
    assert not holder.is_alive() and failures == []


@pytest.mark.skipif(os.name != "posix", reason="forked legacy-waiter process fixture")
def test_restore_fences_a_waiter_that_opened_the_previous_log(tmp_path, monkeypatch):
    import multiprocessing

    from anvil.state import sqlite as sqlite_module
    state, _, store = _restore_fixture(tmp_path, monkeypatch)
    store.push(str(state / "events.jsonl"), "events.jsonl")
    process_context = multiprocessing.get_context("fork")
    observer, worker = process_context.Pipe()

    def wait_on_previous_log():
        original_acquire = sqlite_module._append_lock_acquire_nb

        def delay_old_waiter(handle):
            worker.send("opened")
            assert worker.poll(10) and worker.recv() == "resume"
            return original_acquire(handle)

        sqlite_module._append_lock_acquire_nb = delay_old_waiter
        try:
            with sqlite_module._schema_initialization_lock(
                str(state / "state.db"), str(state / "events.jsonl"),
            ):
                worker.send("entered")
        except BaseException as exc:
            worker.send(type(exc).__name__)
        finally:
            worker.close()

    waiter = process_context.Process(target=wait_on_previous_log)
    waiter.start()
    try:
        assert observer.poll(5) and observer.recv() == "opened"
        restored = runner.invoke(app, ["restore", "--yes", "--cwd", str(tmp_path)])
        assert restored.exit_code == 0, (restored.output, restored.exception)
        observer.send("resume")
        assert observer.poll(5) and observer.recv() == "SchemaProbeFailed"
        waiter.join(timeout=5)
        assert not waiter.is_alive() and waiter.exitcode == 0
    finally:
        if waiter.is_alive():
            waiter.terminate()
            waiter.join(timeout=5)
        observer.close()
        worker.close()


def test_restore_can_recreate_an_absent_projection(tmp_path, monkeypatch):
    import sqlite3

    state, _, store = _restore_fixture(tmp_path, monkeypatch)
    store.push(str(state / "events.jsonl"), "events.jsonl")
    (state / "state.db").unlink()
    restored = runner.invoke(app, ["restore", "--yes", "--cwd", str(tmp_path)])
    assert restored.exit_code == 0, (restored.output, restored.exception)
    with sqlite3.connect(state / "state.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 24


@pytest.mark.parametrize("different_filesystem", [False, True])
def test_restore_stages_log_on_state_filesystem_before_publication(
    tmp_path, monkeypatch, different_filesystem,
):
    import sqlite3
    import stat
    import tempfile

    from tests.test_event_format_migration_safety import _append_native_sync_metadata

    state, _, store = _restore_fixture(tmp_path, monkeypatch)
    store.push(str(state / "events.jsonl"), "events.jsonl")
    temporary_parent = tmp_path
    if different_filesystem:
        for candidate in (Path("/dev/shm"), Path(tempfile.gettempdir())):
            if (candidate.is_dir() and os.access(candidate, os.W_OK)
                    and candidate.stat().st_dev != state.stat().st_dev):
                temporary_parent = candidate
                break
        else:
            pytest.skip("No writable second temporary filesystem available")
    original_inode = (state / "state.db").stat().st_ino
    original_pull = store.pull
    downloaded = []

    def observe_download(key, destination):
        path = Path(destination)
        downloaded.append(path)
        assert path.parent == state
        assert path.stat().st_dev == state.stat().st_dev
        if os.name == "posix":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        original_pull(key, destination)

    monkeypatch.setattr(store, "pull", observe_download)
    retained = SqliteBackend(
        db_path=str(state / "state.db"), events_path=str(state / "events.jsonl"),
        clock=FrozenClock(_T0),
    )
    retained.initialize()
    try:
        _append_native_sync_metadata(retained, {"after_backup": True})
        assert len((state / "events.jsonl").read_text().splitlines()) == 25
        with tempfile.TemporaryDirectory(dir=temporary_parent) as temporary:
            monkeypatch.setattr(tempfile, "tempdir", temporary)
            if different_filesystem:
                assert Path(temporary).stat().st_dev != state.stat().st_dev
            restored = runner.invoke(app, ["restore", "--yes", "--cwd", str(tmp_path)])
            assert restored.exit_code == 0, (restored.output, restored.exception)
            assert list(Path(temporary).iterdir()) == []
        assert (state / "state.db").stat().st_ino == original_inode
        assert (state / "events.jsonl").read_bytes() == store._blobs["events.jsonl"]
        assert retained._conn is not None
        assert retained._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 24
        _append_native_sync_metadata(retained, {"after_restore": True})
        with sqlite3.connect(state / "state.db") as fresh:
            assert fresh.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 25
        events = [json.loads(line) for line in (state / "events.jsonl").read_text().splitlines()]
        assert [event["id"] for event in events] == [f"E{index:06d}" for index in range(1, 26)]
        assert len(downloaded) == 1 and not downloaded[0].exists()
    finally:
        retained.close()


@pytest.mark.parametrize("allocation", ["NamedTemporaryFile", "TemporaryDirectory"])
def test_restore_staging_allocation_failure_leaves_state_unchanged(
    tmp_path, monkeypatch, allocation,
):
    from tests.test_cli import _recursive_file_manifest

    state, module, store = _restore_fixture(tmp_path, monkeypatch)
    before = _recursive_file_manifest(state)

    def refuse_allocation(*args, **kwargs):
        raise OSError("staging unavailable")

    monkeypatch.setattr(module.tempfile, allocation, refuse_allocation)
    monkeypatch.setattr(store, "pull", lambda *_: pytest.fail("download before staging"))
    restored = runner.invoke(app, ["restore", "--yes", "--cwd", str(tmp_path)])
    assert restored.exit_code != 0
    assert isinstance(restored.exception, OSError)
    assert _recursive_file_manifest(state) == before
