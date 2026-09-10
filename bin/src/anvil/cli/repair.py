"""Local projection recovery backed by immutable Anvil event history."""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer

from anvil.cli._helpers import _open_backend, _require_state_dir, _resolve_state_dir

repair_app = typer.Typer(help="Repair durable local projections from event history.")


@dataclass(frozen=True, slots=True)
class _ProjectRecoveryDecision:
    """One fail-closed owner consensus drawn from immutable PRD events."""

    project_id: str
    content_event_count: int


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


def _project_recovery_decision(events_path: Path) -> _ProjectRecoveryDecision:
    """Return a sole historical PRD owner, or refuse without changing state."""
    from pydantic import ValidationError

    from anvil.state.models import Event

    owners: set[str] = set()
    content_event_count = 0
    try:
        with events_path.open("rb") as stream:
            for raw in stream:
                if not raw.strip():
                    continue
                event = Event.model_validate_json(raw)
                if event.action == "project.created":
                    raise RuntimeError(
                        "event history already contains project.created"
                    )
                if event.action not in {"prd.parsed", "prd.revised"}:
                    continue
                payload: dict[str, Any] = event.payload_json
                owner = payload.get("project_id")
                if (
                    not isinstance(owner, str)
                    or not owner.strip()
                    or owner != owner.strip()
                ):
                    raise RuntimeError(
                        "PRD content event has a missing or malformed project owner"
                    )
                if event.target_kind != "prd":
                    raise RuntimeError("PRD content event has an invalid target kind")
                if event.action == "prd.parsed" and event.target_id != owner:
                    raise RuntimeError("PRD parse event does not target its project owner")
                if event.action == "prd.revised":
                    prd_id = payload.get("prd_id", "default")
                    if not isinstance(prd_id, str) or not prd_id.strip():
                        raise RuntimeError("PRD revision event has a malformed PRD id")
                    if event.target_id != prd_id:
                        raise RuntimeError("PRD revision event does not target its PRD")
                owners.add(owner)
                content_event_count += 1
    except (OSError, UnicodeDecodeError, ValidationError, ValueError) as exc:
        raise RuntimeError("cannot validate immutable event history") from exc

    if not owners:
        raise RuntimeError("event history has no PRD content owner")
    if len(owners) != 1:
        raise RuntimeError("PRD content events do not agree on one project owner")
    return _ProjectRecoveryDecision(
        project_id=next(iter(owners)), content_event_count=content_event_count
    )


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


def _remove_staging_artifacts(staging_path: Path) -> tuple[str, ...]:
    """Remove regular staging artifacts without touching substituted paths."""
    skipped: list[str] = []
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
        if _is_link_or_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
            skipped.append(path.name)
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except (IsADirectoryError, PermissionError):
            # A replacement race must never cause a destructive cleanup attempt.
            skipped.append(path.name)
    return tuple(skipped)


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


def _checkpoint_and_fsync_live_projection(
    connection: sqlite3.Connection, state_db: Path
) -> None:
    """Durably flush the in-place publication without disturbing peer handles."""
    # PASSIVE never waits for readers that opened before repair. A committed WAL
    # remains durable after its own fsync even when those readers defer checkpoint.
    connection.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
    _fsync_file(state_db)
    live_wal = Path(f"{state_db}-wal")
    try:
        _regular_path(live_wal, required=False)
    except RuntimeError:
        raise RuntimeError("unsafe live SQLite WAL") from None
    if live_wal.exists():
        _fsync_file(live_wal)
    _fsync_directory(state_db.parent)


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
    skipped_staging_artifacts: tuple[str, ...] = ()
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
            _checkpoint_and_fsync_live_projection(backend._require_conn(), state_db)  # noqa: SLF001
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
            skipped_staging_artifacts = _remove_staging_artifacts(staging_path)

    if skipped_staging_artifacts:
        names = ", ".join(skipped_staging_artifacts)
        typer.echo(
            f"Error: projection repair left unsafe staging artifact(s): {names}",
            err=True,
        )
        raise typer.Exit(code=1)

    assert backup_path is not None
    typer.echo("Projection repaired from local event history.")
    typer.echo(f"Retained online backup: {backup_path}")


@repair_app.command("project")
def repair_project(
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Register the reviewed recovery decision.",
    ),
    cwd: Path | None = typer.Option(None, "--cwd", hidden=True),  # noqa: B008
) -> None:
    """Restore a missing project row from unanimous immutable PRD ownership.

    Historical state created before ``project.created`` became an event can
    replay faithfully with PRDs but no project row. This command never derives
    ownership from config: it requires every PRD content event to name exactly
    one owner, then appends one ordinary current ``project.created`` fact using
    that owner. By default it reports the decision without changing state.
    """
    from anvil.clock import SystemClock
    from anvil.config import load_config
    from anvil.state.models import EventDraft

    state_dir = _resolve_state_dir(cwd)
    _require_state_dir(state_dir, command="repair project", json_output=False)
    state_db = state_dir / "state.db"
    events_path = state_dir / "events.jsonl"
    config_path = state_dir / "config.yaml"
    try:
        _safe_state_directory(state_dir)
        _regular_path(state_db, required=True)
        _regular_path(events_path, required=True)
        _regular_path(config_path, required=True)
        config = load_config(config_path)
        if not config.project_name.strip():
            raise RuntimeError("configured project name is missing")
    except (RuntimeError, OSError, ValueError) as exc:
        typer.echo(f"Error: project repair failed: {exc}", err=True)
        raise typer.Exit(code=1) from None

    backup_path: Path | None = None
    backend = _open_backend(state_dir)
    try:
        with backend.claim_operation_lock():
            _regular_path(state_db, required=True)
            _regular_path(events_path, required=True)
            if backend.get_project() is not None:
                raise RuntimeError("a project is already projected")
            source_identity = _event_source_identity(events_path)
            decision = _project_recovery_decision(events_path)
            from anvil.cli.doctor import _ERROR, _check_replay

            preflight = _check_replay(backend, state_dir)
            if preflight.severity == _ERROR:
                raise RuntimeError(
                    "existing projection did not pass replay verification"
                )
            if not yes:
                typer.echo(
                    "Project recovery is ready: "
                    f"{decision.content_event_count} PRD content event(s) agree "
                    f"on owner {decision.project_id!r}. Re-run with --yes to "
                    "append project.created."
                )
                return
            if source_identity != _event_source_identity(events_path):
                raise RuntimeError("events.jsonl changed while project repair ran")
            backup_path = _new_sibling(state_dir, suffix=".backup")
            _backup_sqlite(backend._require_conn(), backup_path)  # noqa: SLF001
            now = SystemClock().now()
            backend.append(
                EventDraft(
                    timestamp=now,
                    actor="anvil-cli",
                    action="project.created",
                    target_kind="project",
                    target_id=decision.project_id,
                    payload_json={
                        "id": decision.project_id,
                        "name": config.project_name,
                        "description": "",
                        "created_at": now.isoformat(),
                        "updated_at": now.isoformat(),
                    },
                )
            )
            _checkpoint_and_fsync_live_projection(
                backend._require_conn(), state_db  # noqa: SLF001
            )
            finding = _check_replay(backend, state_dir)
            if finding.severity == _ERROR:
                raise RuntimeError(
                    "registered project did not pass replay verification"
                )
    except Exception as exc:  # noqa: BLE001 - report one bounded operator failure.
        typer.echo(f"Error: project repair failed: {exc}", err=True)
        raise typer.Exit(code=1) from None
    finally:
        backend.close()

    assert backup_path is not None
    typer.echo("Project registration repaired from immutable PRD ownership.")
    typer.echo(f"Retained online backup: {backup_path}")
