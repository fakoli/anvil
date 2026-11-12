"""Pending history restoration must not expose or extend a partial event log."""
import os
from pathlib import Path

import pytest

from anvil.state.backend import SchemaProbeFailed
from anvil.state.sqlite import (
    _require_no_event_recovery,
    _schema_initialization_lock,
    query_only_transaction,
)
from tests.test_projection_repair import _seed_project, _state


@pytest.mark.parametrize("operation", ["initialize", "read", "append_lock", "query_only", "replay", "bounded_replay"])
def test_pending_recovery_refuses_old_and_new_handles(tmp_path: Path, operation: str) -> None:
    root, backend = _state(tmp_path)
    _seed_project(backend)
    db, log = root / "state.db", root / "events.jsonl"
    before = log.read_bytes()
    marker = root / ".local-event-recovery.json"
    marker.write_text("{}")
    try:
        with pytest.raises(SchemaProbeFailed, match="recovery is pending"):
            if operation == "initialize":
                backend.initialize()
            elif operation == "read":
                backend._require_conn()
            elif operation == "append_lock":
                with backend.claim_operation_lock():
                    pytest.fail("pending repair exposed writer")
            elif operation == "query_only":
                with query_only_transaction(db, log):
                    pytest.fail("pending repair exposed reader")
            elif operation == "replay":
                backend.replay_from_empty(str(log))
            else:
                backend.replay_to_event_id(str(log), "E000001")
        assert log.read_bytes() == before
        assert db.exists()
        # Only the repair entry point opts into owning the original inode lock.
        with _schema_initialization_lock(str(db), str(log), allow_event_recovery=True):
            assert marker.read_text() == "{}"
    finally:
        marker.unlink()
        backend.close()


def test_broken_link_marker_is_not_idle(tmp_path: Path) -> None:
    root, backend = _state(tmp_path)
    marker = root / ".local-event-recovery.json"
    marker.symlink_to(root / "missing")
    try:
        with pytest.raises(SchemaProbeFailed):
            backend.initialize()
    finally:
        marker.unlink()
        backend.close()


@pytest.mark.parametrize("command", ["backup", "restore"])
def test_pending_recovery_blocks_durable_transfer(tmp_path: Path, monkeypatch, command: str) -> None:
    import importlib

    from typer.testing import CliRunner

    from anvil.cli import app

    module = importlib.import_module("anvil.cli.backup")
    root, backend = _state(tmp_path)
    backend.close()
    (root / ".local-event-recovery.json").write_text("{}")
    monkeypatch.setattr(module, "_resolve_state_dir", lambda cwd: root)
    monkeypatch.setattr(
        module, "_load_config_required", lambda *args: pytest.fail("started durable transfer")
    )
    result = CliRunner().invoke(app, [command])
    assert result.exit_code != 0
    assert "schema_probe_failed" in result.output


class _ChangingDatabasePath:
    def __init__(self, *conversions: str | Exception) -> None:
        self.conversions = iter(conversions)
        self.calls = 0

    def __fspath__(self) -> str:
        self.calls += 1
        value = next(self.conversions)
        if isinstance(value, Exception):
            raise value
        return value


def test_pathlike_second_conversion_keeps_its_own_marker_directory(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (second / ".event-format-migration.json").write_text("{}")
    database = _ChangingDatabasePath(str(first / "state.db"), str(second / "state.db"))

    with pytest.raises(SchemaProbeFailed) as refusal:
        _require_no_event_recovery(database)

    assert str(refusal.value) == (
        "Event-format migration is pending; use anvil migrate-events --to git --resume --yes."
    )
    assert database.calls == 2


@pytest.mark.parametrize("short_circuit", ["local_marker", "allow_conversion", "neither"])
def test_pathlike_second_conversion_remains_lazy(tmp_path: Path, short_circuit: str) -> None:
    conversion_error = OSError("second path conversion refused")
    database = _ChangingDatabasePath(str(tmp_path / "state.db"), conversion_error)
    if short_circuit == "local_marker":
        (tmp_path / ".local-event-recovery.json").write_text("{}")
        with pytest.raises(SchemaProbeFailed) as refusal:
            _require_no_event_recovery(database)
        assert str(refusal.value) == (
            "Local event recovery is pending; use anvil repair local-event --resume."
        )
    elif short_circuit == "allow_conversion":
        (tmp_path / ".event-format-migration.json").write_text("{}")
        _require_no_event_recovery(database, allow_event_conversion=True)
    else:
        with pytest.raises(OSError) as refusal:
            _require_no_event_recovery(database)
        assert refusal.value is conversion_error
    assert database.calls == (2 if short_circuit == "neither" else 1)


@pytest.mark.parametrize("path_kind", ["str", "path", "str_subclass", "path_subclass"])
@pytest.mark.parametrize("relative", [False, True])
def test_marker_arriving_between_checks_is_observed(
    tmp_path: Path, monkeypatch, path_kind: str, relative: bool,
) -> None:
    class DatabaseString(str):
        pass

    class DatabasePath(type(Path())):
        pass

    monkeypatch.chdir(tmp_path)
    database = Path("state.db") if relative else tmp_path / "state.db"
    if path_kind == "path_subclass":
        database = DatabasePath(database)
    elif path_kind != "path":
        database = str(database) if path_kind == "str" else DatabaseString(database)
    original_lexists = os.path.lexists
    inspected = []

    def inspect_and_start_conversion(marker):
        inspected.append(Path(marker))
        exists = original_lexists(marker)
        if Path(marker).name == ".local-event-recovery.json":
            (tmp_path / ".event-format-migration.json").write_text("{}")
        return exists

    monkeypatch.setattr(os.path, "lexists", inspect_and_start_conversion)
    with pytest.raises(SchemaProbeFailed, match="Event-format migration is pending"):
        _require_no_event_recovery(database)
    parent = Path(".") if relative else tmp_path
    assert inspected == [
        parent / ".local-event-recovery.json", parent / ".event-format-migration.json",
    ]


def test_str_subclass_keeps_native_pathlib_conversion_semantics(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()

    class ChangingDatabaseString(str):
        calls = 0

        def __str__(self):
            self.calls += 1
            return str((first if self.calls == 1 else second) / "state.db")

    # pathlib's str-subclass normalization differs between Python 3.11 and 3.14.
    # Preserve each runtime's actual conversions, including their side effects.
    reference = ChangingDatabaseString(str(first / "state.db"))
    Path(reference).parent
    conversion_parent = Path(reference).parent
    (conversion_parent / ".event-format-migration.json").write_text("{}")
    database = ChangingDatabaseString(str(first / "state.db"))
    with pytest.raises(SchemaProbeFailed, match="Event-format migration is pending"):
        _require_no_event_recovery(database)
    assert database.calls == reference.calls


@pytest.mark.parametrize("marker_name", [".local-event-recovery.json", ".event-format-migration.json"])
def test_marker_changes_remain_visible_to_existing_handle(tmp_path: Path, marker_name: str) -> None:
    root, backend = _state(tmp_path)
    _seed_project(backend)
    marker = root / marker_name
    database = str(root / "state.db")
    try:
        backend._require_conn()
        _require_no_event_recovery(database)
        before = {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()}
        for _ in range(2):
            marker.write_text("{}")
            with pytest.raises(SchemaProbeFailed):
                backend._require_conn()
            with pytest.raises(SchemaProbeFailed):
                _require_no_event_recovery(database)
            assert {name: (root / name).read_bytes() for name in before} == before
            marker.unlink()
            backend._require_conn()
            _require_no_event_recovery(database)
    finally:
        marker.unlink(missing_ok=True)
        backend.close()


@pytest.mark.parametrize("allow_conversion", [False, True])
def test_local_recovery_refusal_precedes_conversion_marker(
    tmp_path: Path, allow_conversion: bool,
) -> None:
    for name in [".local-event-recovery.json", ".event-format-migration.json"]:
        (tmp_path / name).write_text("{}")
    with pytest.raises(SchemaProbeFailed) as refusal:
        _require_no_event_recovery(str(tmp_path / "state.db"), allow_event_conversion=allow_conversion)
    assert str(refusal.value) == (
        "Local event recovery is pending; use anvil repair local-event --resume."
    )


def test_broken_conversion_marker_remains_pending(tmp_path: Path) -> None:
    marker = tmp_path / ".event-format-migration.json"
    marker.symlink_to(tmp_path / "missing")
    with pytest.raises(SchemaProbeFailed, match="Event-format migration is pending"):
        _require_no_event_recovery(str(tmp_path / "state.db"))
    _require_no_event_recovery(str(tmp_path / "state.db"), allow_event_conversion=True)

