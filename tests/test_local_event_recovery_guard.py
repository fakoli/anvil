"""Pending history restoration must not expose or extend a partial event log."""
from pathlib import Path

import pytest

from anvil.state.backend import SchemaProbeFailed
from anvil.state.sqlite import (
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

