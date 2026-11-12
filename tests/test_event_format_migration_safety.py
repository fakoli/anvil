"""Event-format publication must fence current clients and retain resumable proof."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from anvil.clock import FrozenClock
from anvil.state.backend import SchemaProbeFailed
from anvil.state.schema import SCHEMA_VERSION
from anvil.state.sqlite import SqliteBackend, query_only_transaction
from tests.test_git_events import _T0, _build_local_project, _draft, _run_in


class _WindowsOSView:
    """Scope the platform branch to this module, without changing pathlib's OS."""

    name = "nt"

    def __getattr__(self, name):
        return getattr(os, name)


@pytest.fixture(autouse=True)
def _require_qualified_conversion_platform(request):
    if os.name == "nt" and not request.node.name.startswith((
        "test_windows_", "test_force_init_local_", "test_force_init_pending_local_",
    )):
        pytest.skip("POSIX conversion protocol; Windows refusal has separate coverage")


def _backend(state: Path, storage: str = "local") -> SqliteBackend:
    backend = SqliteBackend(
        db_path=str(state / "state.db"),
        events_path=str(state / "events.jsonl"),
        clock=FrozenClock(_T0),
        events_storage=storage,
    )
    backend.initialize()
    return backend


def _convert(root: Path, *, resume: bool = False):
    return _run_in(
        root,
        ["migrate-events", "--to", "git", "--yes", *(["--resume"] if resume else [])],
    )


def _append_native_sync_metadata(backend, metadata):
    backend.append(
        _draft(
            "sync_mapping.upserted",
            {
                "task_id": "T001",
                "external_system": "review-fixture",
                "external_id": "fixture-1",
                "external_url": None,
                "last_synced_at": _T0.isoformat(),
                "sync_state": "in_sync",
                "conflict_resolution_strategy": "prompt",
                "provider_metadata": metadata,
                "prd_id": "default",
                "entity_kind": "task",
            },
            target_kind="task",
            target_id="T001",
        )
    )


def _files(state: Path) -> dict[str, bytes]:
    return {str(p.relative_to(state)): p.read_bytes() for p in state.rglob("*") if p.is_file()}


def _leave_hot_rollback_journal(state: Path) -> None:
    child = subprocess.run(
        [sys.executable, "-c", """
import os, sqlite3, sys
connection = sqlite3.connect(sys.argv[1])
assert connection.execute('PRAGMA journal_mode=DELETE').fetchone()[0] == 'delete'
connection.execute('PRAGMA synchronous=FULL')
connection.execute('PRAGMA cache_size=1')
connection.execute('BEGIN IMMEDIATE')
connection.execute('UPDATE events SET actor=?', ('uncommitted' * 1024,))
os._exit(0)
""", str(state / "state.db")],
        capture_output=True, timeout=15,
    )
    assert child.returncode == 0, child.stderr
    assert (state / "state.db-journal").stat().st_size > 512


def test_retained_local_backend_refuses_and_fresh_git_backend_can_append(tmp_path):
    _build_local_project(tmp_path)
    state = tmp_path / ".anvil"
    retained = _backend(state)
    database_identity = (state / "state.db").stat().st_ino
    try:
        result = _convert(tmp_path)
        assert result.exit_code == 0, result.output
        assert (state / "state.db").stat().st_ino == database_identity
        before = (state / "events.jsonl").read_bytes()
        with pytest.raises(SchemaProbeFailed, match="event format changed"):
            retained.append(_draft("task.status_changed", {"status": "blocked"}, target_id="T001"))
        assert (state / "events.jsonl").read_bytes() == before
        with pytest.raises(SchemaProbeFailed, match="event format changed"):
            retained.initialize()
        with pytest.raises(SchemaProbeFailed, match="event format changed"):
            retained.replay_from_empty(str(state / "events.jsonl"))
        with pytest.raises(SchemaProbeFailed, match="event format changed"):
            retained.replay_to_event_id(str(state / "events.jsonl"), "E000001")
        assert (state / "state.db").stat().st_ino == database_identity
    finally:
        retained.close()
    fresh = _backend(state, "git")
    try:
        # Append an ordinary native sync event through the configured writer.
        last = json.loads((state / "events.jsonl").read_text().splitlines()[-1])
        fresh.append(
            _draft(
                last["action"],
                {**last["payload_json"], "external_id": "43"},
                target_kind=last["target_kind"],
                target_id=last["target_id"],
            )
        )
    finally:
        fresh.close()
    with query_only_transaction(state / "state.db", state / "events.jsonl") as (
        connection,
        _,
    ):
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 25


@pytest.mark.parametrize(
    "phase",
    [
        "after_marker",
        "after_events.jsonl",
        "after_config.yaml",
        "after_id_mapping.json",
        "after_.gitattributes",
        "after_database",
        "after_fence",
    ],
)
def test_interrupted_publication_fences_clients_then_resumes(tmp_path, monkeypatch, phase):
    import anvil.cli.migrate as migration

    _build_local_project(tmp_path)
    state = tmp_path / ".anvil"
    original = (state / "events.jsonl").read_bytes()
    retained = _backend(state)

    def interrupt(observed):
        if observed == phase:
            raise RuntimeError("injected publication failure")

    monkeypatch.setattr(migration, "_EVENT_CONVERSION_PHASE_HOOK", interrupt)
    failed = _convert(tmp_path)
    assert failed.exit_code == 1, failed.output
    marker_path = state / ".event-format-migration.json"
    marker = json.loads(marker_path.read_text())
    assert (state / "events.jsonl.pre-git-migration.bak").read_bytes() == original
    assert (state / marker["recovery_dir"] / "before" / "state.db").is_file()
    with pytest.raises(SchemaProbeFailed, match="migration is pending"):
        with retained.claim_operation_lock():
            pytest.fail("retained writer entered pending conversion")
    with pytest.raises(SchemaProbeFailed, match="migration is pending"):
        with query_only_transaction(state / "state.db", state / "events.jsonl"):
            pytest.fail("reader observed mixed conversion")
    retained.close()
    retry_without_resume = _convert(tmp_path)
    assert retry_without_resume.exit_code == 1
    assert "--resume" in retry_without_resume.output
    monkeypatch.setattr(migration, "_EVENT_CONVERSION_PHASE_HOOK", None)
    result = _convert(tmp_path, resume=True)
    assert result.exit_code == 0, result.output
    assert not marker_path.exists()
    backend = _backend(state, "git")
    backend.close()
    assert len((state / "events.jsonl").read_text().splitlines()) == 24
    assert (state / ".state-operation.lock").read_bytes() == b"git\n"


@pytest.mark.parametrize(
    "changed",
    [
        "log",
        "config",
        "backup",
        "projection",
        "db_inode",
        "token_inode",
        "missing_token",
    ],
)
def test_resume_refuses_changed_authority_and_retains_evidence(tmp_path, monkeypatch, changed):
    import anvil.cli.migrate as migration

    _build_local_project(tmp_path)
    state = tmp_path / ".anvil"

    def interrupt(phase):
        if phase == "after_marker":
            raise RuntimeError("injected interruption")

    monkeypatch.setattr(migration, "_EVENT_CONVERSION_PHASE_HOOK", interrupt)
    assert _convert(tmp_path).exit_code == 1
    monkeypatch.setattr(migration, "_EVENT_CONVERSION_PHASE_HOOK", None)
    marker = json.loads((state / ".event-format-migration.json").read_text())
    if changed == "projection":
        with closing(sqlite3.connect(state / "state.db")) as conn:
            conn.execute("UPDATE tasks SET title='unrecorded mutation'")
            conn.commit()
    elif changed == "missing_token":
        (state / ".state-operation.lock").unlink()
    elif changed in {"db_inode", "token_inode"}:
        path = state / ("state.db" if changed == "db_inode" else ".state-operation.lock")
        replacement = state / "replacement"
        shutil.copyfile(path, replacement)
        os.replace(replacement, path)
    else:
        path = {
            "log": state / "events.jsonl",
            "config": state / "config.yaml",
            "backup": state / marker["recovery_dir"] / "after" / "events.jsonl",
        }[changed]
        with path.open("ab") as stream:
            stream.write(b"\n# unrelated write\n")
    before = _files(state)
    result = _convert(tmp_path, resume=True)
    assert result.exit_code == 1, result.output
    assert _files(state) == before
    assert (state / ".event-format-migration.json").exists()


@pytest.mark.parametrize("projection_is_active", [False, True])
def test_bundle_custody_on_either_side_of_crash_refuses(
    tmp_path, monkeypatch, projection_is_active
):
    from anvil.bundles.manager import BundleManager
    from anvil.roots.registry import RootSetRegistry
    from tests.test_bundle_state import (
        _T0 as bundle_time,
    )
    from tests.test_bundle_state import (
        _backend as bundle_backend,
    )
    from tests.test_bundle_state import (
        _create_bundle,
        _seed,
    )

    state = tmp_path / ".anvil"
    state.mkdir()
    backend = bundle_backend(state)
    _seed(backend)
    _create_bundle(backend)
    monkeypatch.setattr(
        "anvil.roots.registry.RootSetRegistry",
        lambda: RootSetRegistry(tmp_path / "roots"),
    )
    manager = BundleManager(
        backend, FrozenClock(bundle_time), actor="codex-main", project_root=tmp_path
    )
    if projection_is_active:
        manager.claim("B001")
    saved = tmp_path / "saved.db"
    with closing(sqlite3.connect(saved)) as destination:
        backend._require_conn().backup(destination)
        destination.execute("PRAGMA journal_mode=DELETE")
    if projection_is_active:
        manager.release("B001", reason="release log ahead of projection")
    else:
        manager.claim("B001")
    backend.close()
    shutil.copyfile(saved, state / "state.db")
    (state / "config.yaml").write_text(
        "project_name: Bundles\nproject_id: proj\nevents_storage: local\n"
    )
    log_before = (state / "events.jsonl").read_bytes()
    database_before = (state / "state.db").read_bytes()
    result = _convert(tmp_path)
    assert result.exit_code == 1, result.output
    assert "active claim" in result.output
    assert (state / "events.jsonl").read_bytes() == log_before
    assert (state / "state.db").read_bytes() == database_before
    assert not (state / ".event-format-migration.json").exists()


def test_dry_run_and_future_schema_create_no_coordination_artifact(tmp_path):
    _build_local_project(tmp_path)
    state = tmp_path / ".anvil"
    before = _files(state)
    result = _run_in(tmp_path, ["migrate-events", "--to", "git"])
    assert result.exit_code == 0, result.output
    assert _files(state) == before
    with closing(sqlite3.connect(state / "state.db")) as conn:
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    before = _files(state)
    result = _convert(tmp_path)
    assert result.exit_code == 1
    assert "schema_mismatch" in result.output and "is newer" in result.output
    assert _files(state) == before


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX old-inode waiter; Windows lock path has separate coverage",
)
def test_waiter_that_opened_original_log_cannot_enter_after_conversion(tmp_path):
    """A real process opens the old inode before the stable token exists."""
    import fcntl

    _build_local_project(tmp_path)
    state = tmp_path / ".anvil"
    code = r"""
import sys
from pathlib import Path
import anvil.state.sqlite as engine
state = Path(sys.argv[1])
original = engine._append_lock_acquire_nb
announced = False
def acquire(handle):
    global announced
    if not announced:
        print("opened", flush=True)
        announced = True
        sys.stdin.readline()
    return original(handle)
engine._append_lock_acquire_nb = acquire
try:
    with engine.query_only_transaction(state / "state.db", state / "events.jsonl"):
        print("UNSAFE", flush=True)
except Exception as exc:
    print(type(exc).__name__, flush=True)
"""
    with (state / "events.jsonl").open("rb") as old:
        fcntl.flock(old, fcntl.LOCK_EX)
        process = subprocess.Popen(
            [sys.executable, "-c", code, str(state)],
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert process.stdout.readline().strip() == "opened"
            fcntl.flock(old, fcntl.LOCK_UN)
            converted = _convert(tmp_path)
            assert converted.exit_code == 0, converted.output
            stdout, stderr = process.communicate("continue\n", timeout=10)
            assert process.returncode == 0, stderr
            assert "UNSAFE" not in stdout
            assert "StateLocked" in stdout or "SchemaProbeFailed" in stdout
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)


@pytest.mark.parametrize("phase", ["after_events.jsonl", "after_database"])
def test_process_death_keeps_native_resume_available(tmp_path, phase):
    _build_local_project(tmp_path)
    code = r"""
import os, sys
import anvil.cli.migrate as migration
os.environ["ANVIL_ROOT"] = sys.argv[1]
os.environ["ANVIL_STATE_LAYOUT"] = "local"
def crash(phase):
    if phase == sys.argv[2]:
        os._exit(81)
migration._EVENT_CONVERSION_PHASE_HOOK = crash
migration.migrate_events(to="git", yes=True, resume=False)
"""
    process = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), phase],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert process.returncode == 81, process.stderr
    assert (tmp_path / ".anvil" / ".event-format-migration.json").is_file()
    result = _convert(tmp_path, resume=True)
    assert result.exit_code == 0, result.output
    backend = _backend(tmp_path / ".anvil", "git")
    backend.close()


def test_claim_started_during_conversion_cannot_escape_admission(tmp_path, monkeypatch):
    import anvil.cli.migrate as migration
    from tests.test_git_events import _seed_ready_task

    state = tmp_path / ".anvil"
    state.mkdir()
    (state / "config.yaml").write_text("project_name: Race\nproject_id: proj-1\n")
    initial = _backend(state)
    _seed_ready_task(initial)
    initial.close()
    code = r"""
import sys
from pathlib import Path
from anvil.clock import FrozenClock
from anvil.state.sqlite import SqliteBackend
from tests.test_git_events import _T0, _draft, _claim_payload
state = Path(sys.argv[1])
backend = SqliteBackend(db_path=str(state / "state.db"), events_path=str(state / "events.jsonl"),
                       clock=FrozenClock(_T0), events_storage="local")
backend.initialize()
print("ready", flush=True)
sys.stdin.readline()
print("claiming", flush=True)
try:
    with backend.claim_operation_lock():
        backend.append(_draft("claim.created", _claim_payload("C-racing"),
                              target_kind="claim", target_id="C-racing"))
    print("UNSAFE", flush=True)
except Exception as exc:
    print(type(exc).__name__, flush=True)
finally:
    backend.close()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(state)],
        text=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert process.stdout.readline().strip() == "ready"

        def start_claim(phase):
            if phase == "after_marker":
                process.stdin.write("claim\n")
                process.stdin.flush()
                assert process.stdout.readline().strip() == "claiming"

        monkeypatch.setattr(migration, "_EVENT_CONVERSION_PHASE_HOOK", start_claim)
        result = _convert(tmp_path)
        assert result.exit_code == 0, result.output
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
        assert "SchemaProbeFailed" in stdout and "UNSAFE" not in stdout
        assert "C-racing" not in (state / "events.jsonl").read_text()
        fresh = _backend(state, "git")
        try:
            assert fresh.list_active_claims() == []
        finally:
            fresh.close()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


@pytest.mark.parametrize("malformed", ["missing_digest", "wrong_identity", "unsafe_path"])
def test_malformed_recovery_marker_refuses_before_writing(tmp_path, monkeypatch, malformed):
    import anvil.cli.migrate as migration

    _build_local_project(tmp_path)
    state = tmp_path / ".anvil"

    def interrupt(phase):
        if phase == "after_marker":
            raise RuntimeError("injected interruption")

    monkeypatch.setattr(migration, "_EVENT_CONVERSION_PHASE_HOOK", interrupt)
    assert _convert(tmp_path).exit_code == 1
    marker_path = state / ".event-format-migration.json"
    marker = json.loads(marker_path.read_text())
    if malformed == "missing_digest":
        marker["after"]["events.jsonl"] = None
    elif malformed == "wrong_identity":
        marker["db_identity"]["ino"] = True
    else:
        marker["recovery_dir"] = ".event-format-migration-/../elsewhere"
    marker_path.write_text(json.dumps(marker))
    before = _files(state)
    result = _convert(tmp_path, resume=True)
    assert result.exit_code == 1, result.output
    assert "invalid" in result.output
    assert _files(state) == before


def test_bundle_only_custody_refuses_even_without_active_child_rows(tmp_path):
    from tests.test_bundle_scope_migration import _seed_claim

    state = tmp_path / ".anvil"
    state.mkdir()
    backend, _, _ = _seed_claim(state)
    backend.close()
    with closing(sqlite3.connect(state / "state.db")) as connection:
        connection.execute("UPDATE claims SET status='released'")
        connection.commit()
    (state / "config.yaml").write_text("project_name: Bundles\nproject_id: proj\n")
    result = _convert(tmp_path)
    assert result.exit_code == 1, result.output
    assert "active claim" in result.output
    assert not (state / ".event-format-migration.json").exists()


@pytest.mark.parametrize("unsafe", ["state_directory", "config", "events", "database", "sidecar"])
def test_unsafe_conversion_paths_refuse_before_coordination_creation(tmp_path, unsafe):
    _build_local_project(tmp_path)
    state = tmp_path / ".anvil"
    external = tmp_path / "retained"
    if unsafe == "state_directory":
        state.rename(external)
        state.symlink_to(external, target_is_directory=True)
        target = external
    else:
        target = state
        name = {
            "config": "config.yaml",
            "events": "events.jsonl",
            "database": "state.db",
            "sidecar": "state.db-journal",
        }[unsafe]
        artifact = state / name
        if artifact.exists():
            artifact.rename(external)
        else:
            external.write_bytes(b"unsafe sidecar")
        artifact.symlink_to(external)
    before = _files(target)
    result = _convert(tmp_path)
    assert result.exit_code == 1, result.output
    assert _files(target) == before
    assert not (target / ".state-operation.lock").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX file mode privacy; Windows uses owner ACLs")
@pytest.mark.parametrize("interrupted", [False, True])
def test_conversion_and_resume_do_not_widen_private_file_modes(tmp_path, monkeypatch, interrupted):
    import stat

    import anvil.cli.migrate as migration

    _build_local_project(tmp_path)
    state = tmp_path / ".anvil"
    for name in ("events.jsonl", "config.yaml"):
        (state / name).chmod(0o600)
    previous_umask = os.umask(0o022)
    try:
        if interrupted:

            def interrupt(phase):
                if phase == "after_config.yaml":
                    raise RuntimeError("injected interrupted publication")

            monkeypatch.setattr(migration, "_EVENT_CONVERSION_PHASE_HOOK", interrupt)
            assert _convert(tmp_path).exit_code == 1
            assert stat.S_IMODE((state / ".event-format-migration.json").stat().st_mode) == 0o600
            monkeypatch.setattr(migration, "_EVENT_CONVERSION_PHASE_HOOK", None)
        result = _convert(tmp_path, resume=interrupted)
        assert result.exit_code == 0, result.output
    finally:
        os.umask(previous_umask)
    for name in (
        "events.jsonl",
        "config.yaml",
        "events.jsonl.pre-git-migration.bak",
        "id_mapping.json",
        ".gitattributes",
    ):
        assert stat.S_IMODE((state / name).stat().st_mode) == 0o600
    for recovery in state.glob(".event-format-migration-*"):
        if recovery.is_dir():
            assert stat.S_IMODE(recovery.stat().st_mode) == 0o700
            for artifact in recovery.rglob("*"):
                if artifact.is_file():
                    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600


@pytest.mark.parametrize("resume", [False, True])
def test_windows_conversion_refuses_before_state_access_or_artifact_creation(
    tmp_path, monkeypatch, resume
):
    import anvil.cli.migrate as migration

    _build_local_project(tmp_path)
    state = tmp_path / ".anvil"
    before = _files(state)

    monkeypatch.setattr(migration, "os", _WindowsOSView())

    def forbidden_state_access(*args, **kwargs):
        pytest.fail("Windows conversion must refuse before resolving State")

    monkeypatch.setattr(migration, "_resolve_state_dir", forbidden_state_access)
    result = _convert(tmp_path, resume=resume)
    assert result.exit_code == 1, result.output
    assert "not qualified on Windows" in result.output
    assert "ACL" in result.output
    assert _files(state) == before


def test_windows_guard_preserves_dry_run_and_schema_only_commands(tmp_path, monkeypatch):
    import anvil.cli.migrate as migration

    _build_local_project(tmp_path)
    state = tmp_path / ".anvil"
    before = _files(state)

    monkeypatch.setattr(migration, "os", _WindowsOSView())
    preview = _run_in(tmp_path, ["migrate-events", "--to", "git"])
    assert preview.exit_code == 0 and "Dry run" in preview.output
    assert _files(state) == before
    schema = _run_in(tmp_path, ["migrate", "state", "--yes", "--json"])
    assert schema.exit_code == 0, schema.output
    assert _files(state) == before


def test_projection_repair_after_conversion_reuses_same_state_token(tmp_path):
    _build_local_project(tmp_path)
    state = tmp_path / ".anvil"
    converted = _convert(tmp_path)
    assert converted.exit_code == 0, converted.output
    before_log = (state / "events.jsonl").read_bytes()
    token_identity = (state / ".state-operation.lock").stat().st_ino
    database_identity = (state / "state.db").stat().st_ino
    repaired = _run_in(tmp_path, ["repair", "projection", "--yes"])
    assert repaired.exit_code == 0, repaired.output
    assert (state / "events.jsonl").read_bytes() == before_log
    assert (state / "state.db").stat().st_ino == database_identity
    assert (state / ".state-operation.lock").stat().st_ino == token_identity
    reopened = _backend(state, "git")
    reopened.close()


def test_scratch_placeholder_replay_is_supported_after_conversion(tmp_path):
    _build_local_project(tmp_path)
    state = tmp_path / ".anvil"
    assert _convert(tmp_path).exit_code == 0
    destination = tmp_path / "existing-scratch.db"
    destination.write_bytes(b"replaceable scratch placeholder")
    result = _run_in(
        tmp_path,
        ["replay", "--from-events", str(state / "events.jsonl"), "--into", str(destination)],
    )
    assert result.exit_code == 0, result.output
    with closing(sqlite3.connect(destination)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 24


def test_nested_legacy_writer_refuses_token_appearance_without_lock_inversion(tmp_path):
    from anvil.state.backend import StateLocked

    _build_local_project(tmp_path)
    state = tmp_path / ".anvil"
    backend = _backend(state)
    try:
        with backend.claim_operation_lock():
            # A converter can introduce its stable token while waiting for this
            # old event lock. Nested work must refuse, never invert lock order.
            (state / ".state-operation.lock").touch()
            with pytest.raises(StateLocked, match="coordination changed"):
                with backend.claim_operation_lock():
                    pytest.fail("nested legacy writer bypassed stable admission")
    finally:
        backend.close()


@pytest.mark.parametrize("interrupted", [False, True])
def test_native_float_metadata_survives_conversion_and_resume(tmp_path, monkeypatch, interrupted):
    import anvil.cli.migrate as migration

    _build_local_project(tmp_path)
    state = tmp_path / ".anvil"
    backend = _backend(state)
    try:
        _append_native_sync_metadata(backend, {"confidence": 0.75})
    finally:
        backend.close()
    if interrupted:

        def interrupt(phase):
            if phase == "after_events.jsonl":
                raise RuntimeError("injected interrupted publication")

        monkeypatch.setattr(migration, "_EVENT_CONVERSION_PHASE_HOOK", interrupt)
        assert _convert(tmp_path).exit_code == 1
        monkeypatch.setattr(migration, "_EVENT_CONVERSION_PHASE_HOOK", None)
    result = _convert(tmp_path, resume=interrupted)
    assert result.exit_code == 0, result.output
    assert not (state / ".event-format-migration.json").exists()
    _backend(state, "git").close()
    with query_only_transaction(state / "state.db", state / "events.jsonl") as (connection, _):
        metadata = connection.execute(
            "SELECT provider_metadata_json FROM sync_mappings WHERE external_system=?",
            ("review-fixture",),
        ).fetchone()[0]
        assert json.loads(metadata) == {"confidence": 0.75}
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 25


@pytest.mark.parametrize("changed", ["projection", "events"])
def test_publication_refuses_drift_from_replayed_artifacts(tmp_path, monkeypatch, changed):
    import anvil.cli.repair as repair

    _build_local_project(tmp_path)
    state = tmp_path / ".anvil"
    publish = repair._publish_staged_projection

    def publish_with_drift(staged_database, connection):
        publish(staged_database, connection)
        if changed == "projection":
            connection.execute("UPDATE tasks SET title='unrecorded publication drift'")
            connection.commit()
        else:
            with (state / "events.jsonl").open("ab") as events:
                events.write(b"\n")

    monkeypatch.setattr(repair, "_publish_staged_projection", publish_with_drift)
    result = _convert(tmp_path)
    assert result.exit_code == 1, result.output
    assert "does not match its replayed artifacts" in result.output
    assert (state / ".event-format-migration.json").is_file()
    assert (state / ".state-operation.lock").read_bytes() == b""
    monkeypatch.setattr(repair, "_publish_staged_projection", publish)
    before = _files(state)
    resumed = _convert(tmp_path, resume=True)
    assert resumed.exit_code == 1, resumed.output
    assert _files(state) == before


@pytest.mark.parametrize("operation", ["initialize", "query", "status", "convert", "append"])
def test_stable_token_contention_uses_callers_error_contract(tmp_path, monkeypatch, operation):
    import fcntl

    import anvil.state.sqlite as sqlite_module
    from anvil.state.backend import StateLocked

    _build_local_project(tmp_path)
    state = tmp_path / ".anvil"
    assert _convert(tmp_path).exit_code == 0
    backend = _backend(state, "git")
    before_events = (state / "events.jsonl").read_bytes()
    monkeypatch.setattr(sqlite_module, "_FLOCK_TIMEOUT_S", 0.01)
    try:
        with (state / ".state-operation.lock").open("r+b") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if operation == "initialize":
                with pytest.raises(SchemaProbeFailed, match="contention deadline"):
                    backend.initialize()
            elif operation == "query":
                with pytest.raises(SchemaProbeFailed, match="contention deadline"):
                    with query_only_transaction(state / "state.db", state / "events.jsonl"):
                        pytest.fail("query entered held State")
            elif operation == "append":
                with pytest.raises(StateLocked, match="contention deadline"):
                    backend.append(_draft("task.status_changed", {}, target_id="T001"))
            else:
                result = (
                    _run_in(tmp_path, ["status", "--json"])
                    if operation == "status" else _convert(tmp_path)
                )
                assert result.exit_code == 1, result.output
                assert isinstance(result.exception, SystemExit)
                assert "schema_probe_failed" in result.output
                if operation == "status":
                    assert json.loads(result.stdout)["error"]["code"] == "schema_probe_failed"
    finally:
        backend.close()
    assert (state / "events.jsonl").read_bytes() == before_events


@pytest.mark.parametrize("event_count,metadata_bytes", [(36, 512 * 1024), (1, 17 * 1024 * 1024)])
def test_native_large_rows_and_history_convert_without_public_response_limits(
    tmp_path, event_count, metadata_bytes,
):
    from anvil.cli.local_event_repair import _table_rows
    from anvil.state.hashing import CanonicalJsonRefusal, canonical_json_bytes

    _build_local_project(tmp_path)
    state = tmp_path / ".anvil"
    backend = _backend(state)
    try:
        for index in range(event_count):
            _append_native_sync_metadata(backend, {"value": "x" * metadata_bytes, "index": index})
    finally:
        backend.close()
    # Default local repair/public canonicalization stays bounded. Only native
    # conversion's private comparison selects the complete-row encoder.
    with closing(sqlite3.connect(state / "state.db")) as connection:
        with pytest.raises(CanonicalJsonRefusal, match="byte_limit_exceeded"):
            canonical_json_bytes(_table_rows(connection))
    result = _convert(tmp_path)
    assert result.exit_code == 0, result.output
    assert not (state / ".event-format-migration.json").exists()
    _backend(state, "git").close()
    with query_only_transaction(state / "state.db", state / "events.jsonl") as (connection, _):
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 24 + event_count
        metadata = json.loads(connection.execute(
            "SELECT provider_metadata_json FROM sync_mappings WHERE external_system=?",
            ("review-fixture",),
        ).fetchone()[0])
        assert len(metadata["value"]) == metadata_bytes
        assert metadata["index"] == event_count - 1


def test_private_projection_digest_preserves_existing_canonical_preimages(tmp_path):
    from anvil.cli.local_event_repair import _table_rows
    from anvil.cli.migrate import _conversion_projection_digest
    from anvil.state.hashing import canonical_json_bytes

    database = tmp_path / "projection.db"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE native_rows (payload BLOB, note TEXT, number INTEGER)")
        connection.executemany(
            "INSERT INTO native_rows VALUES (?, ?, ?)",
            [(b"\x00\xff", '雪\n"\\\x00', -(2**63)), (None, "é", 2**63 - 1)],
        )
        connection.execute("PRAGMA user_version=26")
        connection.commit()
        expected = hashlib.sha256(canonical_json_bytes(_table_rows(connection))).hexdigest()
    before = database.read_bytes()
    assert _conversion_projection_digest(database) == expected
    assert database.read_bytes() == before


def test_private_projection_digest_has_no_public_aggregate_node_limit(tmp_path, monkeypatch):
    import anvil.cli.local_event_repair as repair
    from anvil.cli.migrate import _conversion_projection_digest
    from anvil.state.hashing import CanonicalJsonRefusal, canonical_json_bytes

    database = tmp_path / "projection.db"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE native_rows (value INTEGER)")
        connection.commit()
    # Encoder-boundary fixture, not a task/concurrency or scale qualification.
    rows = {"native_rows": ["{}"] * 100_001}
    with pytest.raises(CanonicalJsonRefusal, match="node_limit_exceeded"):
        canonical_json_bytes(rows)
    monkeypatch.setattr(repair, "_table_rows", lambda connection, **kwargs: rows)
    expected = hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()
    assert _conversion_projection_digest(database) == expected


@pytest.mark.parametrize("phase", ["after_events.jsonl", "after_database"])
def test_hot_journal_recovers_only_private_admission_copy_then_resumes(tmp_path, monkeypatch, phase):
    import anvil.cli.migrate as migration

    _build_local_project(tmp_path)
    state = tmp_path / ".anvil"

    def interrupt(observed):
        if observed == phase:
            raise RuntimeError("injected conversion interruption")

    monkeypatch.setattr(migration, "_EVENT_CONVERSION_PHASE_HOOK", interrupt)
    assert _convert(tmp_path).exit_code == 1
    monkeypatch.setattr(migration, "_EVENT_CONVERSION_PHASE_HOOK", None)
    marker = json.loads((state / ".event-format-migration.json").read_text())
    _leave_hot_rollback_journal(state)
    before = _files(state)
    recovered_digest = migration._conversion_projection_digest(state / "state.db")
    side = "after" if phase == "after_database" else "before"
    assert recovered_digest == marker["projections"][side]
    assert _files(state) == before
    result = _convert(tmp_path, resume=True)
    assert result.exit_code == 0, result.output
    assert not (state / ".event-format-migration.json").exists()
    assert migration._conversion_projection_digest(state / "state.db") == marker["projections"]["after"]
    _backend(state, "git").close()


def test_lock_acquisition_error_contract_does_not_translate_body_exceptions(tmp_path):
    from anvil.state.backend import StateLocked
    from anvil.state.sqlite import _state_operation_lock

    with pytest.raises(StateLocked, match="body failure"):
        with _state_operation_lock(
            tmp_path / "state.db", create=True, contention_error_type=SchemaProbeFailed,
        ):
            raise StateLocked("body failure")


@pytest.mark.parametrize("operation", ["initialize", "query", "status", "preview", "append"])
def test_token_appearing_during_legacy_admission_uses_callers_error_contract(
    tmp_path, monkeypatch, operation,
):
    import anvil.state.sqlite as sqlite_module
    from anvil.state.backend import StateLocked

    _build_local_project(tmp_path)
    state = tmp_path / ".anvil"
    backend = _backend(state)
    acquire = sqlite_module._append_lock_acquire_nb

    def acquire_and_introduce_token(handle):
        acquire(handle)
        (state / ".state-operation.lock").touch()

    monkeypatch.setattr(sqlite_module, "_append_lock_acquire_nb", acquire_and_introduce_token)
    before_events = (state / "events.jsonl").read_bytes()
    try:
        if operation == "initialize":
            with pytest.raises(SchemaProbeFailed, match="coordination changed"):
                backend.initialize()
        elif operation == "query":
            with pytest.raises(SchemaProbeFailed, match="coordination changed"):
                with query_only_transaction(state / "state.db", state / "events.jsonl"):
                    pytest.fail("query entered unfenced State")
        elif operation == "append":
            with pytest.raises(StateLocked, match="coordination changed"):
                backend.append(_draft("task.status_changed", {}, target_id="T001"))
        else:
            result = _run_in(
                tmp_path,
                ["status", "--json"] if operation == "status" else ["migrate-events", "--to", "git"],
            )
            assert result.exit_code == 1, result.output
            assert isinstance(result.exception, SystemExit)
            assert "schema_probe_failed" in result.output
            if operation == "status":
                assert json.loads(result.stdout)["error"]["code"] == "schema_probe_failed"
    finally:
        backend.close()
    assert (state / "events.jsonl").read_bytes() == before_events


@pytest.mark.parametrize("option", [[], ["--with-sample"], ["--from-repo"]])
@pytest.mark.parametrize("phase", [
    None, "after_marker", "after_events.jsonl", "after_config.yaml",
    "after_id_mapping.json", "after_.gitattributes", "after_database", "after_fence",
])
def test_force_init_preserves_converted_and_pending_state(tmp_path, monkeypatch, phase, option):
    import anvil.cli.migrate as migration

    _build_local_project(tmp_path)
    state = tmp_path / ".anvil"
    if phase is not None:
        def interrupt(observed):
            if observed == phase:
                raise RuntimeError("injected interruption before force-init")
        monkeypatch.setattr(migration, "_EVENT_CONVERSION_PHASE_HOOK", interrupt)
    converted = _convert(tmp_path)
    assert converted.exit_code == (0 if phase is None else 1), converted.output
    monkeypatch.setattr(migration, "_EVENT_CONVERSION_PHASE_HOOK", None)
    before = _files(state)
    identities = {name: (state / name).stat().st_ino for name in before}
    child = subprocess.run(
        [sys.executable, "-m", "anvil.cli", "init", "--force", *option],
        cwd=tmp_path, env={**os.environ, "ANVIL_ROOT": str(tmp_path)},
        capture_output=True, text=True, timeout=20,
    )
    assert child.returncode == 1, child.stdout + child.stderr
    assert "force initialization refused" in child.stderr
    assert "Traceback" not in child.stderr
    assert _files(state) == before
    assert {name: (state / name).stat().st_ino for name in before} == identities
    if phase is not None:
        assert "--resume" in child.stderr
        resumed = _convert(tmp_path, resume=True)
        assert resumed.exit_code == 0, resumed.output
    _backend(state, "git").close()


@pytest.mark.parametrize("option", [[], ["--with-sample"], ["--from-repo"]])
def test_force_init_local_reset_keeps_token_and_discards_old_journal(tmp_path, option):
    _build_local_project(tmp_path)
    state = tmp_path / ".anvil"
    _leave_hot_rollback_journal(state)
    result = _run_in(tmp_path, ["init", "--force", "--name", "Replacement", *option])
    assert result.exit_code == 0, result.output
    assert not (state / "state.db-journal").exists()
    assert (state / ".state-operation.lock").read_bytes() == b""
    inode = (state / ".state-operation.lock").stat().st_ino
    assert _run_in(tmp_path, ["status", "--json"]).exit_code == 0
    again = _run_in(tmp_path, ["init", "--force", "--name", "Second"])
    assert again.exit_code == 0, again.output
    assert (state / ".state-operation.lock").stat().st_ino == inode
    events = [json.loads(line) for line in (state / "events.jsonl").read_text().splitlines()]
    assert [event["action"] for event in events] == ["project.created", "state.initialized"]
    assert events[0]["payload_json"]["name"] == "Second"


def test_force_init_local_fresh_initialization_creates_no_token(tmp_path):
    result = _run_in(tmp_path, ["init", "--force"])
    assert result.exit_code == 0, result.output
    assert not (tmp_path / ".anvil" / ".state-operation.lock").exists()


def test_force_init_pending_local_recovery_refuses_before_token_creation(tmp_path):
    _build_local_project(tmp_path)
    state = tmp_path / ".anvil"
    (state / ".local-event-recovery.json").write_text("{}")
    before = _files(state)
    result = _run_in(tmp_path, ["init", "--force"])
    assert result.exit_code == 1, result.output
    assert "repair local-event --resume" in result.output
    assert _files(state) == before


@pytest.mark.parametrize("unsafe", ["state", "config.yaml", "state.db-journal", "state.db"])
def test_force_init_unsafe_paths_refuse_before_token_creation(tmp_path, unsafe):
    _build_local_project(tmp_path)
    state = tmp_path / ".anvil"
    if unsafe == "state":
        real_state = tmp_path / "real-state"
        state.rename(real_state)
        state.symlink_to(real_state, target_is_directory=True)
    else:
        destination = tmp_path / "outside"
        if unsafe == "state.db":
            os.link(state / unsafe, destination)
        else:
            destination.write_text("protected outside bytes")
            (state / unsafe).unlink(missing_ok=True)
            (state / unsafe).symlink_to(destination)
    before = _files(state)
    result = _run_in(tmp_path, ["init", "--force"])
    assert result.exit_code == 1, result.output
    assert "unsafe" in result.output
    assert _files(state) == before
    assert not (state / ".state-operation.lock").exists()


def _read_child_line(process):
    import select

    assert select.select([process.stdout], [], [], 10)[0], "child readiness timed out"
    return process.stdout.readline().strip()


@pytest.mark.parametrize("winner", ["conversion", "force_init"])
def test_force_init_and_conversion_share_admission_boundary(tmp_path, monkeypatch, winner):
    import anvil.state.sqlite as engine

    _build_local_project(tmp_path)
    state = tmp_path / ".anvil"
    code = r'''
import os, sys
from contextlib import contextmanager
import anvil.cli.init_status as initialization
import anvil.state.sqlite as engine
os.environ['ANVIL_ROOT'] = sys.argv[1]
if sys.argv[2] == 'conversion':
    original_lock = engine._state_operation_lock
    first = True
    @contextmanager
    def paused_lock(*args, **kwargs):
        global first
        if first:
            first = False
            print('preflight', flush=True)
            sys.stdin.readline()
        with original_lock(*args, **kwargs) as token:
            yield token
    engine._state_operation_lock = paused_lock
else:
    original_initialize = initialization._initialize_project_state
    def paused_initialize(*args, **kwargs):
        print('admitted', flush=True)
        sys.stdin.readline()
        return original_initialize(*args, **kwargs)
    initialization._initialize_project_state = paused_initialize
from anvil.cli import app
app(['init', '--force', '--name', 'Replacement'])
'''
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path), winner],
        text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        assert _read_child_line(process) == ("preflight" if winner == "conversion" else "admitted")
        if winner == "conversion":
            converted = _convert(tmp_path)
            assert converted.exit_code == 0, converted.output
            before = _files(state)
            stdout, stderr = process.communicate("continue\n", timeout=20)
            assert process.returncode == 1, stdout + stderr
            assert "force initialization refused" in stderr
            assert _files(state) == before
        else:
            before = _files(state)
            monkeypatch.setattr(engine, "_FLOCK_TIMEOUT_S", 0.05)
            blocked = _convert(tmp_path)
            assert blocked.exit_code == 1, blocked.output
            assert "schema_probe_failed" in blocked.output
            assert _files(state) == before
            stdout, stderr = process.communicate("continue\n", timeout=20)
            assert process.returncode == 0, stdout + stderr
            converted = _convert(tmp_path)
            assert converted.exit_code == 0, converted.output
        _backend(state, "git").close()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


def test_force_init_drains_writer_that_entered_before_token_creation(tmp_path):
    _build_local_project(tmp_path)
    state = tmp_path / ".anvil"
    holder_code = r'''
import sys
from pathlib import Path
from anvil.clock import SystemClock
from anvil.state.sqlite import SqliteBackend
state = Path(sys.argv[1])
backend = SqliteBackend(db_path=str(state/'state.db'), events_path=str(state/'events.jsonl'), clock=SystemClock())
backend.initialize()
try:
    with backend.claim_operation_lock():
        print('held', flush=True)
        sys.stdin.readline()
finally:
    backend.close()
'''
    reset_code = r'''
import os, sys
import anvil.state.sqlite as engine
os.environ['ANVIL_ROOT'] = sys.argv[1]
original = engine._append_lock_acquire_nb
announced = False
def observe(handle):
    global announced
    if not announced and str(handle.name).endswith('events.jsonl'):
        print('draining', flush=True)
        announced = True
    return original(handle)
engine._append_lock_acquire_nb = observe
from anvil.cli import app
app(['init', '--force'])
'''
    processes = []
    try:
        holder = subprocess.Popen(
            [sys.executable, "-c", holder_code, str(state)], text=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        processes.append(holder)
        assert _read_child_line(holder) == "held"
        before = _files(state)
        reset = subprocess.Popen(
            [sys.executable, "-c", reset_code, str(tmp_path)], text=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        processes.append(reset)
        assert _read_child_line(reset) == "draining"
        assert reset.poll() is None
        after = _files(state)
        assert {name: after[name] for name in before} == before
        assert set(after) - set(before) == {".state-operation.lock"}
        holder.communicate("release\n", timeout=10)
        assert holder.returncode == 0
        stdout, stderr = reset.communicate(timeout=20)
        assert reset.returncode == 0, stdout + stderr
        assert len((state / "events.jsonl").read_text().splitlines()) == 2
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
