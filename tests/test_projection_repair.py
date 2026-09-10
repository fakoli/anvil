"""Regression coverage for local projection repair."""

from __future__ import annotations

import importlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from anvil.cli import app
from anvil.clock import FrozenClock
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
