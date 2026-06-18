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
    from anvil.config import read_events_storage

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
