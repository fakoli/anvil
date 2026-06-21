"""backup / restore commands — push and pull events.jsonl to S3.

Design: local SQLite stays the working store; events.jsonl is the source of
truth. "S3 durable storage" = push/pull events.jsonl (and optionally state.db)
to/from an S3 bucket. restore = download events.jsonl then replay.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from anvil.cli._helpers import _resolve_state_dir

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
    config = _load_config_required(state_dir)
    store = _make_store(config)
    try:
        events_uri = store.push(str(state_dir / "events.jsonl"), "events.jsonl")
        typer.echo(f"Pushed events.jsonl → {events_uri}")
        if include_db:
            db_uri = store.push(str(state_dir / "state.db"), "state.db")
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
    config = _load_config_required(state_dir)
    if not yes:
        typer.confirm(
            "This will overwrite state.db by replaying the remote events.jsonl. Continue?",
            abort=True,
        )
    from anvil.state.durable import S3Error

    store = _make_store(config)
    # Download into a temp file; only replace live events.jsonl on success.
    with tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl") as tmp:
        tmp_events = tmp.name
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp2:
        scratch_db = tmp2.name
    try:
        store.pull("events.jsonl", tmp_events)
        # Replay into a scratch db first, then atomically replace state.db.
        _replay_into(tmp_events, scratch_db, state_dir)
        Path(scratch_db).replace(state_dir / "state.db")
        Path(tmp_events).replace(state_dir / "events.jsonl")
    except S3Error as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    finally:
        Path(tmp_events).unlink(missing_ok=True)
        Path(scratch_db).unlink(missing_ok=True)
    typer.echo("Restored: events.jsonl pulled and state.db rebuilt from replay.")


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


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


def _replay_into(events_path: str, db_path: str, state_dir: Path) -> None:
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
            events_storage=read_events_storage(state_dir / "config.yaml"),
        )
        backend.initialize()
        backend.replay_from_empty(events_path)
        backend.close()
