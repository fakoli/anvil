"""migrate-events command — rewrite the local event log for git-backed storage.

Phase A of the git-backed-events spec (docs/specs/2026-06-10-git-backed-events.md):
turn a machine-scoped ``events.jsonl`` (sequence ids, strict replay) into a
repo-scoped, merge-friendly log (hash-chained ids + Lamport counter,
order-tolerant replay), preserving event order and emitting an old→new id
mapping for every rewritten event.

Dry-run by default; ``--yes`` applies. Refuses while claims are active: a
mid-flight agent is about to append events referencing the old chain, and the
rewrite would yank the log out from under its next write.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import typer

from fakoli_state.cli._helpers import (
    _require_state_dir,
    _resolve_state_dir,
)

# The union merge driver is the whole point of the git layout: concurrent
# appends on two branches union into one file, and the order-tolerant replay
# (dedupe + HLC sort) absorbs whatever line order the merge produced.
_GITATTRIBUTES_LINE = "events.jsonl merge=union"
_ID_MAPPING_FILENAME = "id_mapping.json"
_BACKUP_SUFFIX = ".pre-git-migration.bak"

_GITIGNORE_GUIDANCE = """\
.gitignore guidance (apply manually in your project root):
  - REMOVE any ignore rule for `.fakoli-state/events.jsonl` — the log is now
    repo state and must be COMMITTED, together with `.fakoli-state/.gitattributes`.
  - KEEP ignoring `.fakoli-state/state.db*` (disposable projection, rebuilt by
    replay) and `.fakoli-state/audit.jsonl` (machine-local audit trail).
  - Consider ignoring `.fakoli-state/*.bak` and `.fakoli-state/id_mapping.json`
    if you do not want migration artifacts in the repo."""


def migrate_events(
    to: str = typer.Option(  # noqa: B008
        ...,
        "--to",
        help="Target storage mode. Only 'git' is supported (no downgrade path).",
    ),
    yes: bool = typer.Option(  # noqa: B008
        False,
        "--yes",
        help="Apply the migration. Without this flag the command is a dry run.",
    ),
) -> None:
    """Migrate the event log to git-backed storage (events_storage: git).

    Rewrites every line of events.jsonl with a hash-chained id
    ("E-" + sha256(parent ‖ canonical_json(payload) ‖ actor ‖ ts)[:12]) and a
    Lamport counter, preserving the original order; emits id_mapping.json
    (old id → new id); writes .fakoli-state/.gitattributes with
    `events.jsonl merge=union`; sets events_storage: git in config.yaml; and
    rebuilds the SQLite projection from the rewritten log.

    Dry-run by default — re-run with --yes to apply. Refuses while any claim
    is active.
    """
    from fakoli_state.state.hashing import hash_event_id
    from fakoli_state.state.models import Event

    if to != "git":
        typer.echo(
            f"Error: unsupported --to value {to!r}. Only 'git' is supported — "
            "git-mode logs are not downgraded back to sequence ids "
            "(local mode cannot represent the hash chain).",
            err=True,
        )
        raise typer.Exit(code=1)

    state_dir = _resolve_state_dir(None)
    _require_state_dir(state_dir)

    config_path = state_dir / "config.yaml"
    events_path = state_dir / "events.jsonl"

    # ------------------------------------------------------------------
    # Preconditions: valid config, not already migrated, no active claims.
    # load_config (not the narrow reader) on purpose — migration rewrites
    # the project's source of truth, so a fully valid config is a fair gate.
    # ------------------------------------------------------------------
    from fakoli_state.config import load_config

    try:
        config = load_config(config_path)
    except (OSError, ValueError) as exc:
        typer.echo(f"Error: cannot load {config_path}: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if config.events_storage == "git":
        typer.echo("events_storage is already 'git' — nothing to migrate.")
        raise typer.Exit(code=0)

    from fakoli_state.cli._helpers import _open_backend

    backend = _open_backend(state_dir)
    try:
        active = backend.list_active_claims()
    finally:
        # Close BEFORE touching the log: the backend holds the projection
        # open, and the apply path below rewrites the file the backend's
        # append path flocks.
        backend.close()
    if active:
        ids = ", ".join(sorted(c.id for c in active))
        typer.echo(
            f"Error: {len(active)} active claim(s) ({ids}). Release or finish "
            "them first — migration rewrites the log a mid-flight agent is "
            "about to append to.",
            err=True,
        )
        raise typer.Exit(code=1)

    # ------------------------------------------------------------------
    # Read + rewrite (in memory): hash-chain the ids preserving file order.
    # Lamport is 1..N — the pre-migration log is a single linear history, so
    # file order IS causal order and replay's (lamport, ts, id) sort
    # reproduces it exactly.
    # ------------------------------------------------------------------
    old_lines: list[str] = []
    if events_path.exists():
        old_lines = events_path.read_text(encoding="utf-8").splitlines()

    new_lines: list[str] = []
    id_mapping: dict[str, str] = {}
    parent: str | None = None
    dropped_torn_line = False
    for i, raw_line in enumerate(old_lines):
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            event = Event.model_validate(json.loads(stripped))
        except Exception as exc:  # json + envelope validation alike
            if i == len(old_lines) - 1:
                # Torn trailing line (crash mid-append) — unreplayable in
                # both modes; drop it rather than fossilize garbage into the
                # committed log.
                dropped_torn_line = True
                break
            typer.echo(
                f"Error: events.jsonl line {i + 1} is malformed and not the "
                f"trailing line — refusing to migrate a corrupt log: {exc}",
                err=True,
            )
            raise typer.Exit(code=1) from None
        new_id = hash_event_id(
            parent_event_id=parent,
            action=event.action,
            target_kind=event.target_kind,
            target_id=event.target_id,
            payload=event.payload_json,
            actor=event.actor,
            ts=event.timestamp.isoformat(),
        )
        id_mapping[event.id] = new_id
        migrated = Event(
            **{
                **event.model_dump(),
                "id": new_id,
                "parent_event_id": parent,
                "lamport": len(new_lines) + 1,
            }
        )
        new_lines.append(migrated.model_dump_json())
        parent = new_id

    # ------------------------------------------------------------------
    # Report (both modes) / apply (--yes only).
    # ------------------------------------------------------------------
    mapping_path = state_dir / _ID_MAPPING_FILENAME
    gitattributes_path = state_dir / ".gitattributes"
    backup_path = state_dir / f"events.jsonl{_BACKUP_SUFFIX}"

    typer.echo(f"Events to rewrite : {len(new_lines)}")
    if dropped_torn_line:
        typer.echo("Note: dropped one torn trailing line (crash mid-append).")
    if id_mapping:
        first_old, first_new = next(iter(id_mapping.items()))
        typer.echo(f"Id mapping sample : {first_old} -> {first_new}")
    typer.echo(f"Will write        : {events_path}")
    typer.echo(f"                    {mapping_path}")
    typer.echo(f"                    {gitattributes_path} ({_GITATTRIBUTES_LINE})")
    typer.echo(f"Backup            : {backup_path}")
    typer.echo("Config change     : events_storage: git")
    typer.echo(_GITIGNORE_GUIDANCE)

    if not yes:
        typer.echo("\nDry run — nothing written. Re-run with --yes to apply.")
        raise typer.Exit(code=0)

    if backup_path.exists():
        typer.echo(
            f"Error: backup {backup_path} already exists (previous migration "
            "attempt?). Move it aside before re-running.",
            err=True,
        )
        raise typer.Exit(code=1)

    # Apply order: backup → log (atomic rename) → mapping → .gitattributes →
    # config flip → projection rebuild. The config flip comes AFTER the log
    # rewrite so a crash in between leaves a local-mode config pointing at a
    # restorable backup, never a git-mode config over a sequence-id log.
    if events_path.exists():
        shutil.copy2(events_path, backup_path)
    tmp_path = events_path.with_suffix(".jsonl.tmp")
    tmp_path.write_text(
        "".join(line + "\n" for line in new_lines),
        encoding="utf-8",
    )
    tmp_path.replace(events_path)

    mapping_path.write_text(
        json.dumps(id_mapping, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    _ensure_gitattributes_line(gitattributes_path)
    _set_events_storage_git(config_path)

    # Rebuild the projection from the rewritten log. The old state.db rows
    # still carry E{N} ids; opening in git mode detects the id-set divergence
    # and runs the order-tolerant replay — which also validates the rewritten
    # log end-to-end before the user commits it.
    from fakoli_state.clock import SystemClock
    from fakoli_state.state.sqlite import SqliteBackend

    rebuilt = SqliteBackend(
        db_path=str(state_dir / "state.db"),
        events_path=str(events_path),
        clock=SystemClock(),
        events_storage="git",
    )
    rebuilt.initialize()
    rebuilt.close()

    typer.echo(f"\nMigrated {len(new_lines)} events to git-backed storage.")
    typer.echo(f"Id mapping written to {mapping_path}.")
    typer.echo("Commit .fakoli-state/events.jsonl and .fakoli-state/.gitattributes.")


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _ensure_gitattributes_line(path: Path) -> None:
    """Write/append the merge=union line to .fakoli-state/.gitattributes.

    Idempotent: an existing file keeps its content; the line is appended only
    when missing, so re-running after a partial apply never duplicates it.
    """
    if path.exists():
        content = path.read_text(encoding="utf-8")
        if _GITATTRIBUTES_LINE in content.splitlines():
            return
        suffix = "" if content.endswith("\n") or not content else "\n"
        path.write_text(content + suffix + _GITATTRIBUTES_LINE + "\n", encoding="utf-8")
        return
    path.write_text(_GITATTRIBUTES_LINE + "\n", encoding="utf-8")


def _set_events_storage_git(config_path: Path) -> None:
    """Set ``events_storage: git`` in config.yaml, preserving comments/layout.

    ``yaml.safe_dump`` would re-serialize the whole file and destroy the
    commented template, so this is a line-level edit: replace an existing
    top-level ``events_storage:`` line in place (matched on the raw line so
    commented-out or indented occurrences are left alone), else append a
    marked block at the end.
    """
    # Read as bytes and decode: Path.read_text() applies universal-newline
    # translation (\r\n -> \n), which would hide CRLF endings before we can
    # detect them. Decoding raw bytes preserves them, so a Windows CRLF config
    # is rewritten with CRLF and not silently flattened to LF (git-diff noise).
    text = config_path.read_bytes().decode("utf-8")
    sep = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("events_storage:"):
            lines[i] = "events_storage: git"
            config_path.write_text(sep.join(lines) + sep, encoding="utf-8")
            return
    block_lines = [
        "",
        "# Set by `fakoli-state migrate-events --to git` (v1.22.0) — hash-chained",
        "# event ids, merge=union log. See docs/specs/2026-06-10-git-backed-events.md.",
        "events_storage: git",
    ]
    config_path.write_text(
        sep.join(lines) + sep + sep.join(block_lines) + sep, encoding="utf-8"
    )
