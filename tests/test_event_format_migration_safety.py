"""Event-format publication must fence current clients and retain resumable proof."""

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

from anvil.clock import FrozenClock
from anvil.state.backend import SchemaProbeFailed
from anvil.state.schema import SCHEMA_VERSION
from anvil.state.sqlite import SqliteBackend, query_only_transaction
from tests.test_git_events import _T0, _build_local_project, _draft, _run_in


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


def _files(state: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(state)): p.read_bytes()
        for p in state.rglob("*")
        if p.is_file()
    }


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
            retained.append(
                _draft("task.status_changed", {"status": "blocked"}, target_id="T001")
            )
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
def test_interrupted_publication_fences_clients_then_resumes(
    tmp_path, monkeypatch, phase
):
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
    "changed", ["log", "config", "backup", "projection", "db_inode", "token_inode"]
)
def test_resume_refuses_changed_authority_and_retains_evidence(
    tmp_path, monkeypatch, changed
):
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
    elif changed in {"db_inode", "token_inode"}:
        path = state / (
            "state.db" if changed == "db_inode" else ".state-operation.lock"
        )
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
        _backend as bundle_backend,
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


@pytest.mark.parametrize(
    "malformed", ["missing_digest", "wrong_identity", "unsafe_path"]
)
def test_malformed_recovery_marker_refuses_before_writing(
    tmp_path, monkeypatch, malformed
):
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
