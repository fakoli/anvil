"""replay command — rebuild canonical state from an events log."""

from __future__ import annotations

import tempfile
from pathlib import Path

import typer

from fakoli_state.cli._helpers import _STATE_DIR_NAME, _resolve_base_dir


def replay(
    from_events: Path = typer.Option(  # noqa: B008
        ...,
        "--from-events",
        help="Path to the source events.jsonl file to replay from.",
    ),
    into: Path = typer.Option(  # noqa: B008
        ...,
        "--into",
        help="Path for the scratch SQLite database to build. Must not be the live state.db.",
    ),
) -> None:
    """Reconstruct canonical state into a scratch database from an events log.

    Reads every event from --from-events and replays them into the SQLite
    database at --into, which is deleted and rebuilt from scratch.

    The command refuses to target the project's live state.db to prevent
    accidental data loss (replay deletes its target first).
    """
    from fakoli_state.clock import SystemClock
    from fakoli_state.config import read_events_storage
    from fakoli_state.state.sqlite import SqliteBackend

    # Resolve both paths to absolute form for comparison.
    from_events_abs = from_events.resolve()
    into_abs = into.resolve()

    # Guard: refuse to target the live state.db for the active project. Resolve
    # the project root through the env-aware resolver (FAKOLI_STATE_ROOT > cwd)
    # so the guard protects the SAME db the rest of the CLI reads/writes — not
    # just the one under cwd (SHOULD-FIX consistency).
    live_state_db = _resolve_base_dir(None) / _STATE_DIR_NAME / "state.db"
    if into_abs == live_state_db:
        typer.echo(
            f"Error: --into targets the live state database at {live_state_db}. "
            "Replay deletes its target. Use a scratch path outside .fakoli-state/.",
            err=True,
        )
        raise typer.Exit(code=1)

    # Guard: --from-events must exist and be readable.
    if not from_events_abs.exists():
        typer.echo(
            f"Error: --from-events file not found: {from_events_abs}",
            err=True,
        )
        raise typer.Exit(code=1)
    try:
        with from_events_abs.open(encoding="utf-8"):
            pass
    except OSError as exc:
        typer.echo(
            f"Error: cannot read --from-events file {from_events_abs}: {exc}",
            err=True,
        )
        raise typer.Exit(code=1) from None

    # Ensure the parent directory of --into exists.
    into_abs.parent.mkdir(parents=True, exist_ok=True)

    # Build a scratch backend pointing at --into.
    # The events_path for the scratch backend lives inside a temporary
    # directory so no stray sibling file is created next to --into.
    # replay_from_empty reads events from from_events_abs directly and
    # only writes to SQLite; the scratch events file is never used for
    # replay output.
    with tempfile.TemporaryDirectory() as _tmpdir:
        scratch_events = str(Path(_tmpdir) / "scratch_events.jsonl")
        backend = SqliteBackend(
            db_path=str(into_abs),
            events_path=scratch_events,
            clock=SystemClock(),
            # v1.22.0: a git-backed log needs the order-tolerant replay. Anchor
            # the storage-mode lookup to the directory the events file actually
            # lives in — NOT the CWD. A git-backed log replayed from a scratch
            # dir, a subdirectory, or an in-place backup would otherwise fall
            # back to "local" mode, which does not deduplicate; a union-merged
            # log can then double-write each duplicated event. The config beside
            # the events file is the one that declared the log git-backed.
            events_storage=read_events_storage(
                from_events_abs.parent / "config.yaml"
            ),
        )
        backend.initialize()

        # Delegate entirely to the existing engine — no replay logic here.
        backend.replay_from_empty(str(from_events_abs))

    typer.echo(f"Replayed events from {from_events_abs}")
    typer.echo(f"Canonical state written to {into_abs}")
