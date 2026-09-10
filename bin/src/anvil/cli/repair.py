"""Local projection recovery backed by immutable Anvil event history."""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import stat
import tempfile
import time
from pathlib import Path

import typer

from anvil.cli._helpers import _open_backend, _require_state_dir, _resolve_state_dir

repair_app = typer.Typer(help="Repair durable local projections from event history.")


def _is_link_or_reparse(metadata: os.stat_result) -> bool:
    """Return whether metadata names a link-like filesystem object."""
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag)


def _regular_path(path: Path, *, required: bool) -> None:
    """Refuse links and special files at a repair boundary."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if required:
            raise RuntimeError(f"missing required state artifact: {path.name}") from None
        return
    if _is_link_or_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"unsafe state artifact: {path.name}")


def _safe_state_directory(path: Path) -> None:
    """Require the state root itself is a real directory, never a reparse point."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        raise RuntimeError("missing required state directory") from None
    if _is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError("unsafe state directory")


def _event_source_identity(path: Path) -> tuple[int, str]:
    """Return the immutable log's end offset and digest without retaining content."""
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        return stream.tell(), digest


def _fsync_file(path: Path) -> None:
    with path.open("r+b") as stream:
        os.fsync(stream.fileno())


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _new_sibling(state_dir: Path, *, suffix: str) -> Path:
    descriptor, raw_path = tempfile.mkstemp(
        prefix=".state.db.repair-", suffix=suffix, dir=state_dir
    )
    os.close(descriptor)
    path = Path(raw_path)
    _regular_path(path, required=True)
    return path


def _remove_staging_artifacts(staging_path: Path) -> None:
    """Remove only the exact temporary database and SQLite sidecars we created."""
    for path in (
        staging_path,
        Path(f"{staging_path}-journal"),
        Path(f"{staging_path}-wal"),
        Path(f"{staging_path}-shm"),
    ):
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISDIR(metadata.st_mode) and not _is_link_or_reparse(metadata):
            raise RuntimeError(f"unsafe staging artifact: {path.name}")
        path.unlink()


def _backup_sqlite(connection: sqlite3.Connection, backup_path: Path) -> None:
    """Create a consistent SQLite online backup at a safe, retained path."""
    destination = sqlite3.connect(str(backup_path))
    try:
        _bounded_backup(connection, destination, operation="creating online backup")
        destination.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        destination.close()
    _fsync_file(backup_path)
    _fsync_directory(backup_path.parent)


def _bounded_backup(
    source: sqlite3.Connection, destination: sqlite3.Connection, *, operation: str
) -> None:
    """Copy SQLite pages without allowing a blocked repair to run indefinitely."""
    deadline = time.monotonic() + 5.0

    def bounded_progress(_: int, __: int, ___: int) -> None:
        if time.monotonic() > deadline:
            raise RuntimeError(f"timed out {operation}")

    source.backup(destination, pages=128, progress=bounded_progress, sleep=0.01)


def _publish_staged_projection(
    staging_path: Path, destination: sqlite3.Connection
) -> None:
    """Copy a verified projection into the existing live SQLite inode.

    The backup API updates the destination database transactionally without a
    pathname replacement, so connections opened before repair continue to use
    the repaired database rather than writing to an unlinked prior inode.
    """
    source = sqlite3.connect(str(staging_path))
    try:
        _bounded_backup(source, destination, operation="publishing repaired projection")
    finally:
        source.close()


def _verify_staged_projection(staging_path: Path, state_dir: Path) -> None:
    """Apply the existing replay-equivalence oracle before publishing."""
    from anvil.cli.doctor import _ERROR, _check_replay
    from anvil.clock import SystemClock
    from anvil.config import read_events_storage
    from anvil.state.sqlite import SqliteBackend

    # Git-backed projections converge against their configured log at open.
    # Copy the immutable source into an isolated log first: an empty temporary
    # log would make Git-mode convergence rebuild the staged projection, while
    # using the live log would contend with the operation lock held by repair.
    with tempfile.TemporaryDirectory(prefix="anvil-repair-verify-") as temp_dir:
        verify_events = Path(temp_dir) / "verify-events.jsonl"
        shutil.copyfile(state_dir / "events.jsonl", verify_events)
        _fsync_file(verify_events)
        staged = SqliteBackend(
            db_path=str(staging_path),
            events_path=str(verify_events),
            clock=SystemClock(),
            events_storage=read_events_storage(state_dir / "config.yaml"),
        )
        staged.initialize()
        try:
            finding = _check_replay(staged, state_dir)
        finally:
            staged.close()
    if finding.severity == _ERROR:
        raise RuntimeError("staged projection did not pass replay verification")


@repair_app.command("projection")
def repair_projection(
    yes: bool = typer.Option(False, "--yes", "-y", help="Apply the verified repair."),
    cwd: Path | None = typer.Option(None, "--cwd", hidden=True),  # noqa: B008
) -> None:
    """Rebuild state.db from local events.jsonl and retain an online backup.

    The immutable event log remains untouched. Anvil holds its normal event-log
    lock across the online backup, scratch replay, event digest recheck, and
    in-place projection publication, so ordinary CLI writers cannot append a fact
    between proof and publication.
    """
    if not yes:
        typer.confirm(
            "This rebuilds state.db in place from a verified local replay and "
            "retains a backup. Continue?",
            abort=True,
        )
    state_dir = _resolve_state_dir(cwd)
    _require_state_dir(state_dir, command="repair projection", json_output=False)
    state_db = state_dir / "state.db"
    events_path = state_dir / "events.jsonl"
    try:
        _safe_state_directory(state_dir)
        _regular_path(state_db, required=True)
        _regular_path(events_path, required=True)
    except RuntimeError as exc:
        typer.echo(f"Error: projection repair failed: {exc}", err=True)
        raise typer.Exit(code=1) from None

    backup_path: Path | None = None
    staging_path: Path | None = None
    backend = _open_backend(state_dir)
    try:
        with backend.claim_operation_lock():
            _regular_path(state_db, required=True)
            _regular_path(events_path, required=True)
            source_identity = _event_source_identity(events_path)
            backup_path = _new_sibling(state_dir, suffix=".backup")
            staging_path = _new_sibling(state_dir, suffix=".staging")
            _backup_sqlite(backend._require_conn(), backup_path)  # noqa: SLF001
            from anvil.cli.backup import _replay_into

            _replay_into(str(events_path), str(staging_path), state_dir)
            _fsync_file(staging_path)
            _verify_staged_projection(staging_path, state_dir)
            if source_identity != _event_source_identity(events_path):
                raise RuntimeError("events.jsonl changed while projection repair ran")
            _regular_path(staging_path, required=True)
            _publish_staged_projection(staging_path, backend._require_conn())  # noqa: SLF001
            _fsync_file(state_db)
            from anvil.cli.doctor import _ERROR, _check_replay

            finding = _check_replay(backend, state_dir)
            if finding.severity == _ERROR:
                raise RuntimeError("published projection did not pass replay verification")
    except Exception as exc:  # noqa: BLE001 - report one bounded operator failure.
        typer.echo(f"Error: projection repair failed: {exc}", err=True)
        raise typer.Exit(code=1) from None
    finally:
        backend.close()
        if staging_path is not None:
            _remove_staging_artifacts(staging_path)

    assert backup_path is not None
    typer.echo("Projection repaired from local event history.")
    typer.echo(f"Retained online backup: {backup_path}")
