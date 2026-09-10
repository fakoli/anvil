"""Regression coverage for local projection repair."""

from __future__ import annotations

import hashlib
import importlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from anvil.cli import app
from anvil.clock import FrozenClock
from anvil.planning.prd_persistence import material_content_sha256, source_binding
from anvil.state.models import EventDraft
from anvil.state.sqlite import SqliteBackend

runner = CliRunner()
_T0 = datetime(2026, 5, 24, 18, 0, 0, tzinfo=UTC)


def _state(tmp_path: Path) -> tuple[Path, SqliteBackend]:
    state_dir = tmp_path / ".anvil"
    state_dir.mkdir()
    events_path = state_dir / "events.jsonl"
    events_path.touch()
    backend = SqliteBackend(
        db_path=str(state_dir / "state.db"),
        events_path=str(events_path),
        clock=FrozenClock(_T0),
    )
    backend.initialize()
    return state_dir, backend


def _git_state(tmp_path: Path) -> tuple[Path, SqliteBackend]:
    state_dir = tmp_path / ".anvil"
    state_dir.mkdir()
    events_path = state_dir / "events.jsonl"
    events_path.touch()
    (state_dir / "config.yaml").write_text("events_storage: git\n", encoding="utf-8")
    backend = SqliteBackend(
        db_path=str(state_dir / "state.db"),
        events_path=str(events_path),
        clock=FrozenClock(_T0),
        events_storage="git",
    )
    backend.initialize()
    return state_dir, backend


def _seed_project(backend: SqliteBackend) -> None:
    backend.append(
        EventDraft(
            timestamp=_T0,
            actor="test",
            action="project.created",
            target_kind="project",
            target_id="proj-1",
            payload_json={
                "id": "proj-1",
                "name": "repair test",
                "description": "",
                "created_at": _T0.isoformat(),
                "updated_at": _T0.isoformat(),
            },
        )
    )


def _seed_owner_only_prd(
    backend: SqliteBackend, *, owner: str, prd_id: str = "default"
) -> None:
    """Create modern PRD evidence without the historical project seed event."""
    source_bytes = b"# Project: Repair\n\n## Summary\nS.\n\n## Goals\n- G.\n\n## Requirements\n"
    source = SimpleNamespace(
        source_bytes=source_bytes,
        markdown=source_bytes.decode("utf-8"),
        source_sha256=hashlib.sha256(source_bytes).hexdigest(),
        source_size_bytes=len(source_bytes),
        source_encoding="utf-8",
    )
    backend.append(
        EventDraft(
            timestamp=_T0,
            actor="test",
            action="prd.parsed",
            target_kind="prd",
            target_id=owner,
            payload_json={
                "project_id": owner,
                "prd_id": prd_id,
                "expected_absent": True,
                "is_default": prd_id == "default",
                "title": "Repair",
                "status": "draft",
                "summary": "S.",
                "goals": ["G."],
                "non_goals": [],
                "requirements": [],
                "acceptance_criteria": [],
                "risks": [],
                "open_questions": [],
                "assumptions": [],
                "material_sha256": material_content_sha256(source, "Repair"),
                **source_binding(source, 1),
            },
        )
    )


def _write_recovery_config(state_dir: Path, *, git_backed: bool = False) -> None:
    storage = "events_storage: git\n" if git_backed else ""
    (state_dir / "config.yaml").write_text(
        f"project_name: repair-test\nproject_id: unrelated-config-id\n{storage}",
        encoding="utf-8",
    )


def test_projection_repair_replays_locally_retains_backup_and_preserves_events(
    tmp_path: Path,
) -> None:
    state_dir, backend = _state(tmp_path)
    events_path = state_dir / "events.jsonl"
    inode_before = (state_dir / "state.db").stat().st_ino
    try:
        before_events = events_path.read_bytes()
    finally:
        backend.close()

    result = runner.invoke(
        app, ["repair", "projection", "--yes", "--cwd", str(tmp_path)]
    )

    assert result.exit_code == 0, result.output
    assert "Projection repaired from local event history." in result.output
    assert events_path.read_bytes() == before_events
    assert (state_dir / "state.db").stat().st_ino == inode_before
    backups = list(state_dir.glob(".state.db.repair-*.backup"))
    assert len(backups) == 1
    assert backups[0].is_file()

    repaired = SqliteBackend(
        db_path=str(state_dir / "state.db"),
        events_path=str(events_path),
        clock=FrozenClock(_T0),
    )
    repaired.initialize()
    repaired.close()


def test_projection_repair_replays_git_backed_events(tmp_path: Path) -> None:
    state_dir, backend = _git_state(tmp_path)
    _seed_project(backend)
    events_path = state_dir / "events.jsonl"
    before_events = events_path.read_bytes()
    backend.close()

    result = runner.invoke(
        app, ["repair", "projection", "--yes", "--cwd", str(tmp_path)]
    )

    assert result.exit_code == 0, result.output
    assert events_path.read_bytes() == before_events
    repaired = SqliteBackend(
        db_path=str(state_dir / "state.db"),
        events_path=str(events_path),
        clock=FrozenClock(_T0),
        events_storage="git",
    )
    repaired.initialize()
    try:
        assert repaired.get_project().id == "proj-1"
    finally:
        repaired.close()


def test_projection_repair_refuses_symlinked_live_database(tmp_path: Path) -> None:
    state_dir, backend = _state(tmp_path)
    events_path = state_dir / "events.jsonl"
    backend.close()
    target = state_dir / "other.db"
    target.write_bytes((state_dir / "state.db").read_bytes())
    (state_dir / "state.db").unlink()
    (state_dir / "state.db").symlink_to(target.name)
    before_events = events_path.read_bytes()

    result = runner.invoke(
        app, ["repair", "projection", "--yes", "--cwd", str(tmp_path)]
    )

    assert result.exit_code == 1
    assert "unsafe state artifact: state.db" in result.output
    assert events_path.read_bytes() == before_events


def test_projection_repair_refuses_reparse_point_live_database(
    tmp_path: Path, monkeypatch
) -> None:
    state_dir, backend = _state(tmp_path)
    backend.close()
    state_db = state_dir / "state.db"
    real_lstat = Path.lstat

    def reparse_lstat(path: Path):  # type: ignore[no-untyped-def]
        result = real_lstat(path)
        if path == state_db:
            return SimpleNamespace(
                st_mode=result.st_mode,
                st_file_attributes=0x400,
            )
        return result

    monkeypatch.setattr(Path, "lstat", reparse_lstat)

    result = runner.invoke(
        app, ["repair", "projection", "--yes", "--cwd", str(tmp_path)]
    )

    assert result.exit_code == 1
    assert "unsafe state artifact: state.db" in result.output


def test_projection_repair_refuses_event_source_change_before_replace(
    tmp_path: Path, monkeypatch
) -> None:
    state_dir, backend = _state(tmp_path)
    backend.close()
    before_db = (state_dir / "state.db").read_bytes()

    backup_module = importlib.import_module("anvil.cli.backup")
    real_replay = backup_module._replay_into

    def mutate_source(events_path_arg: str, db_path: str, state_dir_arg: Path) -> None:
        real_replay(events_path_arg, db_path, state_dir_arg)
        with Path(events_path_arg).open("ab") as stream:
            stream.write(b"\n")

    monkeypatch.setattr(backup_module, "_replay_into", mutate_source)

    result = runner.invoke(
        app, ["repair", "projection", "--yes", "--cwd", str(tmp_path)]
    )

    assert result.exit_code == 1
    assert "events.jsonl changed while projection repair ran" in result.output
    assert (state_dir / "state.db").read_bytes() == before_db
    assert len(list(state_dir.glob(".state.db.repair-*.backup"))) == 1


def test_projection_repair_verifies_staging_before_publishing(
    tmp_path: Path, monkeypatch
) -> None:
    state_dir, backend = _state(tmp_path)
    backend.close()
    before_db = (state_dir / "state.db").read_bytes()

    repair_module = importlib.import_module("anvil.cli.repair")

    def reject_staging(staging_path: Path, state_dir_arg: Path) -> None:
        _ = (staging_path, state_dir_arg)
        raise RuntimeError("synthetic staging refusal")

    monkeypatch.setattr(repair_module, "_verify_staged_projection", reject_staging)

    result = runner.invoke(
        app, ["repair", "projection", "--yes", "--cwd", str(tmp_path)]
    )

    assert result.exit_code == 1
    assert "synthetic staging refusal" in result.output
    assert (state_dir / "state.db").read_bytes() == before_db


def test_projection_repair_publishes_into_peer_opened_before_repair(
    tmp_path: Path,
) -> None:
    state_dir, backend = _state(tmp_path)
    _seed_project(backend)
    backend.close()
    state_db = state_dir / "state.db"
    peer = sqlite3.connect(state_db)
    peer.execute("DELETE FROM projects")
    peer.commit()

    result = runner.invoke(
        app, ["repair", "projection", "--yes", "--cwd", str(tmp_path)]
    )

    assert result.exit_code == 0, result.output
    assert peer.execute("SELECT id FROM projects").fetchone() == ("proj-1",)
    peer.execute("CREATE TABLE peer_repair_probe (id INTEGER)")
    peer.commit()
    peer.close()
    fresh = sqlite3.connect(state_db)
    try:
        assert fresh.execute(
            "SELECT name FROM sqlite_master WHERE name = 'peer_repair_probe'"
        ).fetchone() == ("peer_repair_probe",)
    finally:
        fresh.close()


def test_projection_repair_keeps_final_verification_under_operation_lock(
    tmp_path: Path, monkeypatch
) -> None:
    state_dir, backend = _state(tmp_path)
    backend.close()
    doctor_module = importlib.import_module("anvil.cli.doctor")
    real_check_replay = doctor_module._check_replay
    verified_under_lock = False

    def observe_final_verification(
        checked_backend: SqliteBackend, checked_state_dir: Path
    ):
        nonlocal verified_under_lock
        if Path(checked_backend._db_path).name == "state.db":  # noqa: SLF001
            verified_under_lock = True
            assert checked_backend._append_lock_depth == 1  # noqa: SLF001
        return real_check_replay(checked_backend, checked_state_dir)

    monkeypatch.setattr(doctor_module, "_check_replay", observe_final_verification)

    result = runner.invoke(
        app, ["repair", "projection", "--yes", "--cwd", str(tmp_path)]
    )

    assert result.exit_code == 0, result.output
    assert verified_under_lock


def test_projection_repair_removes_staging_sqlite_sidecars(
    tmp_path: Path, monkeypatch
) -> None:
    state_dir, backend = _state(tmp_path)
    backend.close()
    repair_module = importlib.import_module("anvil.cli.repair")
    real_publish = repair_module._publish_staged_projection

    def publish_then_leave_sidecars(
        staging_path: Path, destination: sqlite3.Connection
    ) -> None:
        real_publish(staging_path, destination)
        Path(f"{staging_path}-wal").write_bytes(b"temporary")
        Path(f"{staging_path}-shm").write_bytes(b"temporary")
        Path(f"{staging_path}-journal").write_bytes(b"temporary")

    monkeypatch.setattr(repair_module, "_publish_staged_projection", publish_then_leave_sidecars)

    result = runner.invoke(
        app, ["repair", "projection", "--yes", "--cwd", str(tmp_path)]
    )

    assert result.exit_code == 0, result.output
    assert not list(state_dir.glob(".state.db.repair-*.staging*"))


def test_projection_repair_cleanup_never_unlinks_an_unsafe_staging_directory(
    tmp_path: Path,
) -> None:
    repair_module = importlib.import_module("anvil.cli.repair")
    staging_path = tmp_path / ".state.db.repair-test.staging"
    staging_path.write_bytes(b"temporary")
    unsafe_sidecar = Path(f"{staging_path}-wal")
    unsafe_sidecar.mkdir()

    skipped = repair_module._remove_staging_artifacts(staging_path)

    assert skipped == (unsafe_sidecar.name,)
    assert not staging_path.exists()
    assert unsafe_sidecar.is_dir()


def test_projection_repair_cleanup_skips_regular_mode_reparse_sidecar(
    tmp_path: Path, monkeypatch
) -> None:
    repair_module = importlib.import_module("anvil.cli.repair")
    staging_path = tmp_path / ".state.db.repair-test.staging"
    staging_path.write_bytes(b"temporary")
    reparse_sidecar = Path(f"{staging_path}-wal")
    reparse_sidecar.write_bytes(b"temporary")
    real_lstat = Path.lstat

    def reparse_lstat(path: Path):  # type: ignore[no-untyped-def]
        result = real_lstat(path)
        if path == reparse_sidecar:
            return SimpleNamespace(
                st_mode=result.st_mode,
                st_file_attributes=0x400,
            )
        return result

    monkeypatch.setattr(Path, "lstat", reparse_lstat)

    skipped = repair_module._remove_staging_artifacts(staging_path)

    assert skipped == (reparse_sidecar.name,)
    assert not staging_path.exists()
    assert reparse_sidecar.exists()


def test_projection_repair_checkpoints_and_fsyncs_live_database_and_wal(
    tmp_path: Path, monkeypatch
) -> None:
    repair_module = importlib.import_module("anvil.cli.repair")
    state_db = tmp_path / "state.db"
    connection = sqlite3.connect(state_db)
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("CREATE TABLE probe (id INTEGER)")
    connection.commit()
    fsynced: list[Path] = []
    monkeypatch.setattr(repair_module, "_fsync_file", fsynced.append)
    monkeypatch.setattr(repair_module, "_fsync_directory", lambda _: None)

    try:
        repair_module._checkpoint_and_fsync_live_projection(connection, state_db)
    finally:
        connection.close()

    assert state_db in fsynced
    assert Path(f"{state_db}-wal") in fsynced


def test_project_repair_reports_consensus_without_mutating_by_default(
    tmp_path: Path,
) -> None:
    state_dir, backend = _state(tmp_path)
    _seed_owner_only_prd(backend, owner="historical-owner")
    _write_recovery_config(state_dir)
    events_path = state_dir / "events.jsonl"
    before = events_path.read_bytes()
    backend.close()

    result = runner.invoke(app, ["repair", "project", "--cwd", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "Project recovery is ready" in result.output
    assert events_path.read_bytes() == before
    assert not list(state_dir.glob(".state.db.repair-*.backup"))


def test_project_repair_registers_consensus_owner_and_replays(
    tmp_path: Path,
) -> None:
    state_dir, backend = _state(tmp_path)
    _seed_owner_only_prd(backend, owner="historical-owner")
    _write_recovery_config(state_dir)
    events_path = state_dir / "events.jsonl"
    backend.close()

    result = runner.invoke(
        app, ["repair", "project", "--yes", "--cwd", str(tmp_path)]
    )

    assert result.exit_code == 0, result.output
    assert "Project registration repaired" in result.output
    assert len(list(state_dir.glob(".state.db.repair-*.backup"))) == 1
    actions = [
        json.loads(line)["action"]
        for line in events_path.read_text(encoding="utf-8").splitlines()
    ]
    assert actions == ["prd.parsed", "project.created"]
    repaired = SqliteBackend(
        db_path=str(state_dir / "state.db"),
        events_path=str(events_path),
        clock=FrozenClock(_T0),
    )
    repaired.initialize()
    try:
        assert repaired.get_project().id == "historical-owner"
        from anvil.cli._helpers import _get_project_id

        assert _get_project_id(repaired) == "historical-owner"
    finally:
        repaired.close()
    snapshot = runner.invoke(
        app, ["project", "snapshot", "--json", "--cwd", str(tmp_path)]
    )
    assert snapshot.exit_code == 0, snapshot.output


def test_project_repair_registers_consensus_owner_for_git_events(
    tmp_path: Path,
) -> None:
    state_dir, backend = _git_state(tmp_path)
    _seed_owner_only_prd(backend, owner="historical-owner")
    _write_recovery_config(state_dir, git_backed=True)
    backend.close()

    result = runner.invoke(
        app, ["repair", "project", "--yes", "--cwd", str(tmp_path)]
    )

    assert result.exit_code == 0, result.output
    repaired = SqliteBackend(
        db_path=str(state_dir / "state.db"),
        events_path=str(state_dir / "events.jsonl"),
        clock=FrozenClock(_T0),
        events_storage="git",
    )
    repaired.initialize()
    try:
        assert repaired.get_project().id == "historical-owner"
    finally:
        repaired.close()


def test_project_repair_refuses_zero_or_multiple_owners_without_mutating(
    tmp_path: Path,
) -> None:
    state_dir, backend = _state(tmp_path)
    _write_recovery_config(state_dir)
    events_path = state_dir / "events.jsonl"
    backend.close()
    before_empty = events_path.read_bytes()

    empty = runner.invoke(
        app, ["repair", "project", "--yes", "--cwd", str(tmp_path)]
    )

    assert empty.exit_code == 1
    assert "no PRD content owner" in empty.output
    assert events_path.read_bytes() == before_empty

    backend = SqliteBackend(
        db_path=str(state_dir / "state.db"),
        events_path=str(events_path),
        clock=FrozenClock(_T0),
    )
    backend.initialize()
    _seed_owner_only_prd(backend, owner="owner-a")
    _seed_owner_only_prd(backend, owner="owner-b", prd_id="other")
    backend.close()
    before_multiple = events_path.read_bytes()

    multiple = runner.invoke(
        app, ["repair", "project", "--yes", "--cwd", str(tmp_path)]
    )

    assert multiple.exit_code == 1
    assert "do not agree on one project owner" in multiple.output
    assert events_path.read_bytes() == before_multiple


def test_project_repair_refuses_existing_project_event_without_mutating(
    tmp_path: Path,
) -> None:
    state_dir, backend = _state(tmp_path)
    _seed_project(backend)
    _write_recovery_config(state_dir)
    events_path = state_dir / "events.jsonl"
    before = events_path.read_bytes()
    backend.close()

    result = runner.invoke(
        app, ["repair", "project", "--yes", "--cwd", str(tmp_path)]
    )

    assert result.exit_code == 1
    assert "a project is already projected" in result.output
    assert events_path.read_bytes() == before


def test_project_repair_refuses_project_history_when_project_row_is_missing(
    tmp_path: Path,
) -> None:
    state_dir, backend = _state(tmp_path)
    _seed_project(backend)
    _write_recovery_config(state_dir)
    events_path = state_dir / "events.jsonl"
    backend._require_conn().execute("DELETE FROM projects")  # noqa: SLF001
    backend._require_conn().commit()  # noqa: SLF001
    before = events_path.read_bytes()
    backend.close()

    result = runner.invoke(
        app, ["repair", "project", "--yes", "--cwd", str(tmp_path)]
    )

    assert result.exit_code == 1
    assert "already contains project.created" in result.output
    assert events_path.read_bytes() == before


def test_project_repair_preflights_replay_before_backup_or_append(
    tmp_path: Path, monkeypatch
) -> None:
    state_dir, backend = _state(tmp_path)
    _seed_owner_only_prd(backend, owner="historical-owner")
    _write_recovery_config(state_dir)
    events_path = state_dir / "events.jsonl"
    before = events_path.read_bytes()
    backend.close()
    doctor_module = importlib.import_module("anvil.cli.doctor")

    monkeypatch.setattr(
        doctor_module,
        "_check_replay",
        lambda *_: SimpleNamespace(severity=doctor_module._ERROR),
    )

    result = runner.invoke(
        app, ["repair", "project", "--yes", "--cwd", str(tmp_path)]
    )

    assert result.exit_code == 1
    assert "existing projection did not pass replay verification" in result.output
    assert events_path.read_bytes() == before
    assert not list(state_dir.glob(".state.db.repair-*.backup"))


def test_project_recovery_decision_refuses_malformed_owner_or_target(
    tmp_path: Path,
) -> None:
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        '{"id":"E000001","timestamp":"2026-05-24T18:00:00+00:00",'
        '"actor":"test","action":"prd.parsed","target_kind":"prd",'
        '"target_id":" owner ","payload_json":{"project_id":" owner "}}\n',
        encoding="utf-8",
    )
    repair_module = importlib.import_module("anvil.cli.repair")

    try:
        repair_module._project_recovery_decision(events_path)
    except RuntimeError as exc:
        assert "missing or malformed project owner" in str(exc)
    else:
        raise AssertionError("malformed owner was accepted")

    events_path.write_text(
        '{"id":"E000001","timestamp":"2026-05-24T18:00:00+00:00",'
        '"actor":"test","action":"prd.parsed","target_kind":"prd",'
        '"target_id":"different-owner",'
        '"payload_json":{"project_id":"owner"}}\n',
        encoding="utf-8",
    )
    try:
        repair_module._project_recovery_decision(events_path)
    except RuntimeError as exc:
        assert "does not target its project owner" in str(exc)
    else:
        raise AssertionError("mismatched parse target was accepted")
