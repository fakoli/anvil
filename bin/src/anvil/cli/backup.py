"""backup / restore commands — push and pull events.jsonl to S3.

Design: local SQLite stays the working store; events.jsonl is the source of
truth. "S3 durable storage" = push/pull events.jsonl (and optionally state.db)
to/from an S3 bucket. restore = download events.jsonl then replay.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from anvil.cli._helpers import _resolve_state_dir
from anvil.state.sqlite import _require_no_event_recovery, _schema_initialization_lock

if TYPE_CHECKING:
    from anvil.config import Config
    from anvil.state.durable import DurableStore


def backup(
    include_db: bool = typer.Option(  # noqa: B008
        False,
        "--include-db",
        help="Also upload state.db as a warm snapshot.",
    ),
    cwd: Path | None = typer.Option(None, "--cwd", hidden=True),  # noqa: B008
) -> None:
    """Push events.jsonl (and optionally state.db) to the configured S3 bucket."""
    from anvil.state.durable import S3Error

    state_dir = _resolve_state_dir(cwd)
    _require_no_event_recovery(state_dir / "state.db")
    config = _load_config_required(state_dir)
    store = _make_store(config)
    try:
        if not (state_dir / "events.jsonl").is_file():
            raise FileNotFoundError("Backup event log is unavailable.")
        if include_db and not (state_dir / "state.db").is_file():
            raise FileNotFoundError("Backup database is unavailable.")
        with tempfile.TemporaryDirectory(prefix="anvil-durable-backup-") as temporary:
            staged = Path(temporary)
            # Capture the log and committed DB/WAL contents under the same
            # native custody window. Upload only closed private files after
            # releasing custody; remote latency must not block local writers.
            with _schema_initialization_lock(state_dir / "state.db", state_dir / "events.jsonl"):
                _require_no_event_recovery(state_dir / "state.db")
                shutil.copyfile(state_dir / "events.jsonl", staged / "events.jsonl")
                if include_db:
                    from anvil.cli.migrate import _backup_database

                    _backup_database(state_dir / "state.db", staged / "state.db")
            events_uri = store.push(str(staged / "events.jsonl"), "events.jsonl")
            typer.echo(f"Pushed events.jsonl → {events_uri}")
            if include_db:
                db_uri = store.push(str(staged / "state.db"), "state.db")
                typer.echo(f"Pushed state.db    → {db_uri}")
    except S3Error as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None


def restore(
    yes: bool = typer.Option(  # noqa: B008
        False,
        "--yes",
        "-y",
        help="Skip the confirmation prompt.",
    ),
    cwd: Path | None = typer.Option(None, "--cwd", hidden=True),  # noqa: B008
) -> None:
    """Pull events.jsonl from S3 and replay into state.db (destructive)."""
    state_dir = _resolve_state_dir(cwd)
    _require_no_event_recovery(state_dir / "state.db")
    _require_safe_restore_paths(state_dir)
    config = _load_config_required(state_dir)
    events_storage = config.events_storage
    if not yes:
        typer.confirm(
            "This will overwrite state.db by replaying the remote events.jsonl. Continue?",
            abort=True,
        )
    from anvil.state.durable import S3Error

    store = _make_store(config)
    # The log will be renamed into place after projection publication. Allocate
    # its private stage on the State filesystem before changing either live file.
    with tempfile.NamedTemporaryFile(
        delete=False, prefix=".restore-", suffix=".jsonl", dir=state_dir,
    ) as tmp:
        tmp_events = tmp.name
    try:
        with tempfile.TemporaryDirectory(prefix="anvil-restore-") as temporary:
            scratch_db = str(Path(temporary) / "state.db")
            store.pull("events.jsonl", tmp_events)
            # Remote latency and replay do not hold up live writers. Bind replay to
            # the selected format, then refuse if conversion won before publication.
            _replay_into(tmp_events, scratch_db, state_dir, events_storage=events_storage)
            _publish_restore(state_dir, Path(tmp_events), Path(scratch_db), events_storage)
    except S3Error as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    finally:
        Path(tmp_events).unlink(missing_ok=True)
    typer.echo("Restored: events.jsonl pulled and state.db rebuilt from replay.")


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _require_safe_restore_paths(state_dir: Path) -> None:
    from anvil.cli.local_event_repair import _regular, _safe_sidecars
    from anvil.cli.repair import _safe_state_directory
    from anvil.state.sqlite import _STATE_OPERATION_TOKEN

    _safe_state_directory(state_dir)
    for name in ("state.db", "events.jsonl", "config.yaml", _STATE_OPERATION_TOKEN):
        _regular(state_dir / name, required=name == "config.yaml")
    _safe_sidecars(state_dir / "state.db")


def _publish_restore(
    state_dir: Path, staged_events: Path, staged_database: Path, events_storage: str,
) -> None:
    from anvil.cli.repair import (
        _checkpoint_and_fsync_live_projection,
        _fsync_directory,
        _publish_staged_projection,
    )
    from anvil.state.backend import SchemaProbeFailed
    from anvil.state.sqlite import _require_event_storage_fence, _state_operation_lock

    database = str(state_dir / "state.db")
    events = str(state_dir / "events.jsonl")
    _require_no_event_recovery(database)
    _require_safe_restore_paths(state_dir)
    # Replacing the authority log needs permanent custody even for local State.
    # A legacy waiter that missed creation refuses under its old event lock.
    with _state_operation_lock(database, create=True, contention_error_type=SchemaProbeFailed):
        _require_no_event_recovery(database)
        _require_safe_restore_paths(state_dir)
        _require_event_storage_fence(database, events_storage)
        if _load_config_required(state_dir).events_storage != events_storage:
            raise SchemaProbeFailed(
                "State event format changed during restore; retry from the current State."
            )
        if os.path.lexists(events):
            # Drain pre-token writers, then close the replaceable log handle
            # before publication (including on Windows). Keep the token owned.
            with _schema_initialization_lock(database, events, create_missing_log=False):
                _require_safe_restore_paths(state_dir)
        # Keep retained SQLite connections on the live inode. This preserves
        # restore's existing crash semantics; the two files are not one atomic
        # transaction and no new recovery protocol is introduced here.
        with closing(sqlite3.connect(database)) as connection:
            _publish_staged_projection(staged_database, connection)
            _checkpoint_and_fsync_live_projection(connection, Path(database))
        staged_events.replace(events)
        _fsync_directory(state_dir)


def _load_config_required(state_dir: Path) -> Config:
    """Load config.yaml and abort loudly if durable_store is not set to 's3'."""
    import yaml

    from anvil.config import load_merged_config

    config_path = state_dir / "config.yaml"
    if not config_path.exists():
        typer.echo(
            "Error: no config.yaml found. Run `anvil init` first.",
            err=True,
        )
        raise typer.Exit(code=1)
    try:
        config = load_merged_config(config_path)
    except (FileNotFoundError, OSError, ValueError, yaml.YAMLError) as exc:
        typer.echo(f"Error: config.yaml load failed: {exc}", err=True)
        raise typer.Exit(code=1) from None
    if config.durable_store == "none":
        typer.echo(
            "Error: durable_store is not configured. "
            "Set durable_store: s3 and s3_bucket in .anvil/config.yaml.",
            err=True,
        )
        raise typer.Exit(code=1)
    return config


def _make_store(config: Config) -> DurableStore:
    """Return a DurableStore for the given config, or exit 1 on unknown store.

    # ponytail: single dispatch — add elif "gcs" / elif "azure" when those
    # DurableStore impls land.
    """
    from anvil.state.durable import S3DurableStore

    if config.durable_store == "s3":
        return S3DurableStore(
            bucket=config.s3_bucket or "",  # validated non-None by load_config
            prefix=config.s3_prefix,
            region=config.s3_region,
            profile=config.s3_profile,
        )
    typer.echo(
        f"Error: unknown durable_store value {config.durable_store!r}.",
        err=True,
    )
    raise typer.Exit(code=1)


def _replay_into(
    events_path: str, db_path: str, state_dir: Path, *, events_storage: str | None = None,
) -> None:
    """Replay events_path into a scratch db at db_path.

    Thin wrapper around SqliteBackend.replay_from_empty — no logic duplicated.
    """
    from anvil.clock import SystemClock
    from anvil.config import read_events_storage
    from anvil.state.sqlite import SqliteBackend

    # Ensure the scratch db parent exists (tmp dirs already do, but be explicit).
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmpdir:
        scratch_events = str(Path(tmpdir) / "scratch_events.jsonl")
        backend = SqliteBackend(
            db_path=db_path,
            events_path=scratch_events,
            clock=SystemClock(),
            events_storage=(
                read_events_storage(state_dir / "config.yaml")
                if events_storage is None else events_storage
            ),
        )
        # Replay initializes the scratch projection after removing its placeholder.
        backend.replay_from_empty(events_path)
        backend.close()
