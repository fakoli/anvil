"""migrate commands — schema and event-log migrations.

Two distinct, independent migrations live here:

* ``migrate-events`` (Phase A of the git-backed-events spec,
  docs/specs/2026-06-10-git-backed-events.md): rewrite a machine-scoped
  ``events.jsonl`` (sequence ids, strict replay) into a repo-scoped,
  merge-friendly log (hash-chained ids + Lamport counter, order-tolerant
  replay), preserving event order and emitting an old→new id mapping.

* ``migrate state`` (T009/F006): promote the in-init ``state.db`` schema
  migration — the ordered, idempotent forward branches that already run
  automatically inside ``SqliteBackend.initialize()`` — to an explicit,
  backed-up, dry-run-by-default command. It detects the on-disk
  ``schema_version`` (via the T007 ``read_db_schema_version`` accessor),
  runs the existing engine migration up to the code's ``SCHEMA_VERSION``,
  and backs up ``state.db`` before mutating. This is NOT a migration
  framework — it surfaces the migration the engine already performs.

Both are dry-run by default (``--yes`` applies) and both refuse while claims
are active: a mid-flight agent is about to append events / read the
projection, and rewriting it out from under the agent corrupts its next write.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import typer

from anvil.cli._helpers import (
    BoundedSchemaProbe,
    _open_backend,
    _preflight_schema_compatibility,
    _require_state_dir,
    _resolve_state_dir,
)
from anvil.cli._json import JSON_OPTION, emit_success, fail
from anvil.state.schema import SCHEMA_VERSION

# The union merge driver is the whole point of the git layout: concurrent
# appends on two branches union into one file, and the order-tolerant replay
# (dedupe + HLC sort) absorbs whatever line order the merge produced.
_GITATTRIBUTES_LINE = "events.jsonl merge=union"
_ID_MAPPING_FILENAME = "id_mapping.json"
_BACKUP_SUFFIX = ".pre-git-migration.bak"

_GITIGNORE_GUIDANCE = """\
.gitignore guidance (apply manually in your project root):
  - REMOVE any ignore rule for `.anvil/events.jsonl` — the log is now
    repo state and must be COMMITTED, together with `.anvil/.gitattributes`.
  - KEEP ignoring `.anvil/state.db*` (disposable projection, rebuilt by
    replay) and `.anvil/audit.jsonl` (machine-local audit trail).
  - Consider ignoring `.anvil/*.bak` and `.anvil/id_mapping.json`
    if you do not want migration artifacts in the repo."""

# Backup suffix for the state.db schema migration (T009). Distinct from
# ``_BACKUP_SUFFIX`` above (the events.jsonl backup) so a schema migration and
# an events migration never clobber each other's backup.
_DB_BACKUP_SUFFIX = ".pre-schema-migration.bak"

# ---------------------------------------------------------------------------
# ``migrate`` sub-app: groups the schema migration under `migrate state`.
# `migrate-events` stays a top-level command (registered in cli/__init__) for
# backward compatibility; the schema migration is a NEW command and lives here.
# ---------------------------------------------------------------------------

migrate_app = typer.Typer(
    name="migrate",
    help=(
        "Schema/state migrations. `migrate state` upgrades .anvil/state.db "
        "to the current engine schema version (dry-run by default; --yes to apply)."
    ),
    no_args_is_help=True,
)

_MIGRATE_STATE_HELP = (
    "Upgrade .anvil/state.db through every supported forward migration to "
    f"the current engine schema (v{SCHEMA_VERSION}). Dry-run by default; "
    "use --yes to apply."
)


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
    (old id → new id); writes .anvil/.gitattributes with
    `events.jsonl merge=union`; sets events_storage: git in config.yaml; and
    rebuilds the SQLite projection from the rewritten log.

    Dry-run by default — re-run with --yes to apply. Refuses while any claim
    is active.
    """
    from anvil.state.hashing import hash_event_id
    from anvil.state.models import Event

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

    # Schema incompatibility is the primary safety boundary for every
    # backend-opening command. Check it before YAML parsing can warn with an
    # absolute path, raise a parser traceback, or otherwise pollute output.
    schema_probe = _preflight_schema_compatibility(state_dir)

    # ------------------------------------------------------------------
    # Preconditions: valid config, not already migrated, no active claims.
    # load_config (not the narrow reader) on purpose — migration rewrites
    # the project's source of truth, so a fully valid config is a fair gate.
    # ------------------------------------------------------------------
    from anvil.config import load_config

    try:
        config = load_config(config_path)
    except (OSError, ValueError) as exc:
        schema_probe.close()
        typer.echo(f"Error: cannot load {config_path}: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except BaseException:
        schema_probe.close()
        raise

    if config.events_storage == "git":
        schema_probe.close()
        typer.echo("events_storage is already 'git' — nothing to migrate.")
        raise typer.Exit(code=0)

    backend = _open_backend(state_dir, schema_probe=schema_probe)
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
    from anvil.clock import SystemClock
    from anvil.state.sqlite import SqliteBackend

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
    typer.echo("Commit .anvil/events.jsonl and .anvil/.gitattributes.")


@migrate_app.command("state", help=_MIGRATE_STATE_HELP)
def migrate_state(
    yes: bool = typer.Option(  # noqa: B008
        False, "--yes", help="Apply the migration. Without this flag this is a dry run.",
    ),
    json_output: bool = JSON_OPTION,
) -> None:
    """Preview or apply an idle-State upgrade under the engine's writer lock."""
    from anvil.cli._helpers import BoundedSchemaProbe
    from anvil.state.backend import SchemaProbeFailed
    from anvil.state.sqlite import SqliteBackend, _schema_initialization_lock

    state_dir = _resolve_state_dir(None)
    _require_state_dir(state_dir, command="migrate state", json_output=json_output)
    db_path = state_dir / "state.db"
    events_path = state_dir / "events.jsonl"
    schema_probe = BoundedSchemaProbe()
    try:
        from_version = schema_probe(db_path)
        SqliteBackend.validate_schema_compatibility(from_version)
        if from_version == SCHEMA_VERSION:
            _migrate_state_locked(state_dir, schema_probe, yes=yes, json_output=json_output)
            return
        # A migration must not initialize a competing project or create a missing
        # authority log. The stable log is also the cross-process writer token.
        if not db_path.is_file() or not events_path.is_file():
            raise SchemaProbeFailed("Schema migration requires an existing database and event log.")
        with _schema_initialization_lock(str(db_path), str(events_path)):
            _migrate_state_locked(state_dir, schema_probe, yes=yes, json_output=json_output)
    finally:
        schema_probe.close()


def _migrate_state_locked(
    state_dir: Path, schema_probe: BoundedSchemaProbe, *, yes: bool, json_output: bool
) -> None:
    """Own preflight, backup and apply as one operation against an idle log."""
    from anvil.state.backend import SchemaMismatch
    from anvil.state.sqlite import SqliteBackend

    command = "migrate state"
    db_path = state_dir / "state.db"
    # Re-probe under the lock: a pre-lock observation cannot authorize writes.
    from_version = schema_probe(db_path)
    SqliteBackend.validate_schema_compatibility(from_version)

    # 2. Already current → idempotent no-op (covers "re-running migrate is a
    #    no-op" once the db has been brought up to SCHEMA_VERSION).
    if from_version == SCHEMA_VERSION:
        schema_probe.close()
        if json_output:
            emit_success(
                command,
                {
                    "migrated": False,
                    "from_version": from_version,
                    "to_version": SCHEMA_VERSION,
                    "applied": False,
                    "backup": None,
                },
            )
            return
        typer.echo(
            f"state.db is already at schema version {SCHEMA_VERSION} — "
            "nothing to migrate."
        )
        return

    # 2b. An on-disk version the engine has no forward branch for (e.g. a db
    #     newer than this build, or an unknown stamp) cannot be migrated up.
    #     Route it through the same closed root diagnostic as every command.
    if from_version > SCHEMA_VERSION:
        schema_probe.close()
        raise SchemaMismatch(
            actual=from_version,
            expected=SCHEMA_VERSION,
            direction="newer",
        )

    # Replay a private authority-log copy; opening live State here would migrate
    # before the active task and bundle custody check had completed.
    try:
        active_ids = _active_claim_ids_without_migrating(state_dir, db_path)
    except BaseException:
        schema_probe.close()
        raise
    if active_ids:
        schema_probe.close()
        ids = ", ".join(sorted(active_ids))
        msg = (
            f"{len(active_ids)} active claim(s) ({ids}). Release or finish them "
            "first — a schema migration rewrites the projection a mid-flight "
            "agent is about to read and append to."
        )
        if json_output:
            fail(command, msg, code="active_claims")
        typer.echo(f"Error: {msg}", err=True)
        raise typer.Exit(code=1)

    backup_path = db_path.with_name(db_path.name + _DB_BACKUP_SUFFIX)

    # Retain one SQLite-consistent DB plus the exact authority log/config.
    # All copies share the event lock with admission and migration. No WAL
    # sidecars are required to restore the standalone backup database.
    backup_artifacts = (
        backup_path,
        backup_path.with_name(backup_path.name + ".events.jsonl"),
        backup_path.with_name(backup_path.name + ".config.yaml"),
    )
    # Old migration attempts may have retained sidecars under the former format.
    conflicting_artifacts = (*backup_artifacts, *(
        backup_path.with_name(backup_path.name + suffix) for suffix in ("-wal", "-shm")
    ))
    existing_artifact = next((path for path in conflicting_artifacts if path.exists()), None)
    if existing_artifact is not None:
        msg = ("A schema-migration backup artifact already exists; "
               "retain and reconcile it before retrying.")
        if json_output:
            fail(command, msg, code="backup_exists")
        typer.echo(f"Error: {msg}", err=True)
        raise typer.Exit(code=1)

    # 4a. Dry run (default): report and exit, mutating nothing.
    if not yes:
        schema_probe.close()
        if json_output:
            emit_success(
                command,
                {
                    "migrated": False,
                    "from_version": from_version,
                    "to_version": SCHEMA_VERSION,
                    "applied": False,
                    "backup": str(backup_path),
                },
            )
            return
        typer.echo(f"Schema migration  : v{from_version} -> v{SCHEMA_VERSION}")
        typer.echo(f"Will back up      : {db_path}")
        typer.echo(f"            to    : {backup_path}")
        typer.echo("\nDry run — nothing written. Re-run with --yes to apply.")
        return

    created_artifacts: list[Path] = []
    try:
        # Exclusive creation means cleanup can only remove files this attempt owns.
        with os.fdopen(os.open(backup_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"):
            pass
        created_artifacts.append(backup_path)
        _backup_database(db_path, backup_path)
        for original, destination in (
            (state_dir / "events.jsonl", backup_artifacts[1]),
            (state_dir / "config.yaml", backup_artifacts[2]),
        ):
            if original.exists():
                with os.fdopen(os.open(
                    destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
                ), "wb") as output:
                    created_artifacts.append(destination)
                    with original.open("rb") as source:
                        shutil.copyfileobj(source, output)
        from anvil.cli.repair import _fsync_directory, _fsync_file

        for artifact in created_artifacts:
            _fsync_file(artifact)
        _fsync_directory(state_dir)
    except BaseException:
        for artifact in created_artifacts:
            artifact.unlink(missing_ok=True)
        raise

    try:
        backend = _open_backend(state_dir, schema_probe=schema_probe)
    except SchemaMismatch:
        # Compatibility refusal happens before live mutation. Remove only the
        # backup artifacts created by this attempt, then let the root boundary
        # render the closed path-free diagnostic.
        for artifact in created_artifacts:
            artifact.unlink(missing_ok=True)
        raise

    try:
        to_version = backend.get_schema_version()
    finally:
        backend.close()

    if json_output:
        emit_success(
            command,
            {
                "migrated": True,
                "from_version": from_version,
                "to_version": to_version,
                "applied": True,
                "backup": str(backup_path),
                "backup_artifacts": [str(path) for path in created_artifacts],
            },
        )
        return
    typer.echo(f"Migrated state.db v{from_version} -> v{to_version}.")
    typer.echo(f"Backup written to {backup_path}.")
    typer.echo(
        "Re-running `anvil migrate state` is now a no-op. Delete the "
        "backup once you have verified the migration."
    )


def _backup_database(db_path: Path, backup_path: Path) -> None:
    """Create a bounded SQLite snapshot, including committed WAL contents."""
    deadline = time.monotonic() + 10.0

    def check_deadline(_status: int, _remaining: int, _total: int) -> None:
        if time.monotonic() >= deadline:
            raise TimeoutError("Timed out creating the schema-migration database backup.")

    from anvil.state.sqlite import _stable_database_copy

    with _stable_database_copy(db_path) as scratch_db:
        with closing(sqlite3.connect(scratch_db)) as source:
            with closing(sqlite3.connect(backup_path)) as destination:
                source.backup(destination, pages=256, progress=check_deadline, sleep=0.01)
                destination.execute("PRAGMA journal_mode=DELETE")
                if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Schema-migration backup failed its integrity check.")


def _active_claim_ids_without_migrating(state_dir: Path, db_path: Path) -> list[str]:
    """Share normal-open's canonical custody check without opening live State."""
    from anvil.config import read_events_storage
    from anvil.state.sqlite import _schema_upgrade_active_claim_ids

    return _schema_upgrade_active_claim_ids(
        str(db_path),
        str(state_dir / "events.jsonl"),
        read_events_storage(state_dir / "config.yaml"),
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _ensure_gitattributes_line(path: Path) -> None:
    """Write/append the merge=union line to .anvil/.gitattributes.

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
        "# Set by `anvil migrate-events --to git` (v1.22.0) — hash-chained",
        "# event ids, merge=union log. See docs/specs/2026-06-10-git-backed-events.md.",
        "events_storage: git",
    ]
    config_path.write_text(
        sep.join(lines) + sep + sep.join(block_lines) + sep, encoding="utf-8"
    )
