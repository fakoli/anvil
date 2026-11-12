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

import hashlib
import json
import os
import shutil
import sqlite3
import time
import uuid
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

import typer

from anvil.cli._helpers import (
    BoundedSchemaProbe,
    _open_backend,
    _preflight_schema_compatibility,
    _require_state_dir,
    _resolve_state_dir,
)
from anvil.cli._json import JSON_OPTION, emit_success, fail
from anvil.state.backend import SchemaProbeFailed
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
  - KEEP machine-local `.anvil/.state-operation.lock`,
    `.anvil/.event-format-migration.json`, `.anvil/.event-format-migration-*/`
    and `.anvil/*.bak` out of Git. Recovery copies include private configuration;
    retain them locally and never delete the token or marker to bypass recovery.
  - Consider ignoring `.anvil/id_mapping.json` if you do not want the
    event identifier mapping in the repo."""

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


def _require_existing_state_files(state_dir: Path) -> None:
    if not (state_dir / "state.db").is_file() or not (state_dir / "events.jsonl").is_file():
        raise SchemaProbeFailed("Migration requires an existing database and event log.")


class _EventConversionRefused(RuntimeError):
    """A closed, recoverable conversion refusal suitable for operator output."""


def migrate_events(
    to: str = typer.Option(  # noqa: B008
        ...,
        "--to",
        help="Target storage mode. Only 'git' is supported (no downgrade path).",
    ),
    yes: bool = typer.Option(  # noqa: B008
        False,
        "--yes",
        help="Apply the migration. Without this flag this is a dry run.",
    ),
    resume: bool = typer.Option(  # noqa: B008
        False,
        "--resume",
        help="Resume an interrupted conversion; requires --yes.",
    ),
) -> None:
    """Convert idle local event history with durable backups and native recovery.

    Stop older engine versions before applying. Current engines share a permanent
    token; retained local writers refuse after conversion. Interrupted publication
    fences normal clients until --resume validates and completes the same work.
    Apply/resume is POSIX-only; Windows refuses before accessing State because
    private artifact ACL enforcement is not yet qualified.
    """
    from anvil.config import load_config
    from anvil.state.sqlite import (
        _EVENT_FORMAT_MIGRATION_MARKER,
        SqliteBackend,
        _schema_initialization_lock,
        _state_operation_lock,
    )

    if to != "git":
        typer.echo(f"Error: unsupported --to value {to!r}. Only 'git' is supported.", err=True)
        raise typer.Exit(1)
    if resume and not yes:
        typer.echo("Error: --resume requires --yes.", err=True)
        raise typer.Exit(1)
    if os.name == "nt" and yes:
        typer.echo(
            "Error: event-format conversion apply/resume is not qualified on Windows: "
            "private artifact ACLs are not enforced. No State files were changed. "
            "Dry-run inspection and schema-only `anvil migrate state` remain available.",
            err=True,
        )
        raise typer.Exit(1)
    state_dir = _resolve_state_dir(None)
    _require_state_dir(state_dir)
    probe = _preflight_schema_compatibility(state_dir)
    try:
        _require_event_conversion_paths(state_dir)
        config = load_config(state_dir / "config.yaml")
        pending = os.path.lexists(state_dir / _EVENT_FORMAT_MIGRATION_MARKER)
        if resume and not pending:
            raise _EventConversionRefused("No event-format migration is pending.")
        if pending and not resume:
            raise _EventConversionRefused(
                "Event-format migration is pending; "
                "use anvil migrate-events --to git --resume --yes."
            )
        db_path = str(state_dir / "state.db")
        events_path = str(state_dir / "events.jsonl")
        # Dry runs and schema-only upgrades never introduce the permanent token.
        with _state_operation_lock(
            db_path,
            create=yes and not resume and config.events_storage != "git",
        ) as token:
            with _schema_initialization_lock(
                db_path,
                events_path,
                create_missing_log=False,
                allow_event_conversion=resume,
            ):
                _require_event_conversion_paths(state_dir)
                SqliteBackend.validate_schema_compatibility(probe(Path(db_path)))
                if resume:
                    marker = _read_event_conversion_marker(state_dir)
                else:
                    config = load_config(state_dir / "config.yaml")
                    if config.events_storage == "git":
                        typer.echo("events_storage is already 'git' — nothing to migrate.")
                        return
                    active = _active_claim_ids_without_migrating(state_dir, Path(db_path))
                    if active:
                        raise _EventConversionRefused(
                            "Event conversion refused: active claim custody (task or bundle): "
                            + ", ".join(active)
                        )
                    rewritten, mapping, dropped = _converted_event_history(Path(events_path))
                    typer.echo(f"Events to rewrite : {len(mapping)}")
                    if dropped:
                        typer.echo("Note: dropped one torn trailing line (crash mid-append).")
                    typer.echo(_GITIGNORE_GUIDANCE)
                    if not yes:
                        typer.echo("Dry run — nothing written. Re-run with --yes to apply.")
                        return
                    marker = _stage_event_conversion(state_dir, rewritten, mapping)
                    _event_conversion_hook("after_marker")
            # Close the replaceable log handle before rename, including Windows.
            # Stable ownership spans admission, backup, publication and recovery.
            if token is None:
                raise _EventConversionRefused(
                    "Event conversion requires its permanent coordination token."
                )
            _publish_event_conversion(state_dir, marker, token)
        typer.echo("Migrated events to git-backed storage; retained backups and id_mapping.json.")
        typer.echo("Commit .anvil/events.jsonl and .anvil/.gitattributes.")
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        probe.close()


_EVENT_CONVERSION_PREFIX = ".event-format-migration-"
_EVENT_CONVERSION_FILES = ("events.jsonl", "config.yaml", "id_mapping.json", ".gitattributes")
_EVENT_CONVERSION_PHASE_HOOK: Callable[[str], None] | None = None


def _require_event_conversion_paths(state_dir: Path) -> None:
    """Admit safe existing paths before introducing any coordination artifact."""
    from anvil.cli.local_event_repair import _regular, _safe_sidecars
    from anvil.cli.repair import _safe_state_directory
    from anvil.state.sqlite import _EVENT_FORMAT_MIGRATION_MARKER, _STATE_OPERATION_TOKEN

    _safe_state_directory(state_dir)
    _require_existing_state_files(state_dir)
    required = {"state.db", "events.jsonl", "config.yaml"}
    optional = {
        "id_mapping.json",
        ".gitattributes",
        _EVENT_FORMAT_MIGRATION_MARKER,
        _STATE_OPERATION_TOKEN,
        f"events.jsonl{_BACKUP_SUFFIX}",
    }
    for name in required | optional:
        _regular(state_dir / name, required=name in required)
    _safe_sidecars(state_dir / "state.db")


def _event_conversion_hook(phase: str) -> None:
    """Fault-injection seam; never configured by normal execution."""
    if _EVENT_CONVERSION_PHASE_HOOK is not None:
        _EVENT_CONVERSION_PHASE_HOOK(phase)


def _converted_event_history(events_path: Path) -> tuple[bytes, dict[str, str], bool]:
    from anvil.state.hashing import hash_event_id
    from anvil.state.models import Event

    lines = events_path.read_text(encoding="utf-8").splitlines()
    rewritten: list[str] = []
    mapping: dict[str, str] = {}
    parent: str | None = None
    dropped = False
    for index, raw in enumerate(lines):
        if not raw.strip():
            continue
        try:
            event = Event.model_validate_json(raw)
        except ValueError:
            if index == len(lines) - 1:
                dropped = True
                break
            raise _EventConversionRefused(f"Event log line {index + 1} is corrupt.") from None
        if event.id in mapping or event.parent_event_id is not None or event.lamport is not None:
            raise _EventConversionRefused(
                "Conversion requires one local event history with unique ids."
            )
        identity = hash_event_id(
            parent_event_id=parent,
            action=event.action,
            target_kind=event.target_kind,
            target_id=event.target_id,
            payload=event.payload_json,
            actor=event.actor,
            ts=event.timestamp.isoformat(),
        )
        mapping[event.id] = identity
        rewritten.append(
            Event(
                **{
                    **event.model_dump(),
                    "id": identity,
                    "parent_event_id": parent,
                    "lamport": len(rewritten) + 1,
                }
            ).model_dump_json()
            + "\n"
        )
        parent = identity
    return "".join(rewritten).encode(), mapping, dropped


def _conversion_digest(path: Path) -> str:
    from anvil.cli.local_event_repair import _regular

    _regular(path)
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _conversion_projection_digest(path: Path) -> str:
    # Same logical-row oracle as native local-event repair, including schema and
    # version. WAL layout changes do not change the recorded recovery identity.
    from anvil.cli.local_event_repair import _readonly_connection, _table_rows
    from anvil.state.hashing import canonical_json_bytes
    from anvil.state.sqlite import _stable_database_copy

    with _stable_database_copy(path) as snapshot:
        with closing(_readonly_connection(snapshot)) as connection:
            return hashlib.sha256(canonical_json_bytes(_table_rows(connection))).hexdigest()


def _stage_event_conversion(
    state_dir: Path, rewritten: bytes, mapping: dict[str, str]
) -> dict[str, Any]:
    from anvil.cli.backup import _replay_into
    from anvil.cli.local_event_repair import _identity, _regular, _safe_sidecars, _write_file
    from anvil.cli.repair import _fsync_directory, _fsync_file, _safe_state_directory
    from anvil.state.sqlite import _EVENT_FORMAT_MIGRATION_MARKER, _STATE_OPERATION_TOKEN

    _safe_state_directory(state_dir)
    _safe_sidecars(state_dir / "state.db")
    backup = state_dir / f"events.jsonl{_BACKUP_SUFFIX}"
    if os.path.lexists(backup):
        raise _EventConversionRefused(
            "Event backup already exists; retain it and reconcile the earlier attempt."
        )
    recovery = state_dir / f"{_EVENT_CONVERSION_PREFIX}{uuid.uuid4().hex}"
    recovery.mkdir(mode=0o700)
    before, after = recovery / "before", recovery / "after"
    before.mkdir(mode=0o700)
    after.mkdir(mode=0o700)
    originals: dict[str, str | None] = {}
    for name in _EVENT_CONVERSION_FILES:
        source = state_dir / name
        _regular(source, required=name in {"events.jsonl", "config.yaml"})
        originals[name] = _conversion_digest(source) if source.exists() else None
        if source.exists():
            _write_file(before / name, source.read_bytes())
    _write_file(after / "events.jsonl", rewritten)
    _write_file(after / "config.yaml", (before / "config.yaml").read_bytes())
    _set_events_storage_git(after / "config.yaml")
    _write_file(
        after / "id_mapping.json", (json.dumps(mapping, indent=2, sort_keys=True) + "\n").encode()
    )
    if (before / ".gitattributes").exists():
        _write_file(after / ".gitattributes", (before / ".gitattributes").read_bytes())
    _ensure_gitattributes_line(after / ".gitattributes")
    _backup_database(state_dir / "state.db", before / "state.db")
    # Replay only into private files, before establishing the pending marker.
    _replay_into(str(after / "events.jsonl"), str(after / "state.db"), after)
    for directory in (before, after):
        for artifact in directory.iterdir():
            artifact.chmod(0o600)
            _fsync_file(artifact)
        _fsync_directory(directory)
    marker = {
        "schema_version": 1,
        "recovery_dir": recovery.name,
        "state_identity": {"dev": state_dir.stat().st_dev, "ino": state_dir.stat().st_ino},
        "db_identity": _identity(state_dir / "state.db"),
        "token_identity": _identity(state_dir / _STATE_OPERATION_TOKEN),
        "before": originals,
        "after": {name: _conversion_digest(after / name) for name in _EVENT_CONVERSION_FILES},
        "databases": {
            side: _conversion_digest(recovery / side / "state.db") for side in ("before", "after")
        },
        "projections": {
            side: _conversion_projection_digest(recovery / side / "state.db")
            for side in ("before", "after")
        },
    }
    # This copy is outside the private staging directory. Restrict permissions
    # at creation, independently of umask, and refuse any existing backup.
    descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write((before / "events.jsonl").read_bytes())
        stream.flush()
        os.fsync(stream.fileno())
    _fsync_file(state_dir / _STATE_OPERATION_TOKEN)
    _fsync_directory(recovery)
    temporary = recovery / "marker.json"
    _write_file(temporary, (json.dumps(marker, sort_keys=True) + "\n").encode())
    temporary.chmod(0o600)
    _fsync_file(temporary)
    os.replace(temporary, state_dir / _EVENT_FORMAT_MIGRATION_MARKER)
    _fsync_directory(state_dir)
    return marker


def _read_event_conversion_marker(state_dir: Path) -> dict[str, Any]:
    from anvil.cli.local_event_repair import _bounded_bytes, _directory
    from anvil.state.sqlite import _EVENT_FORMAT_MIGRATION_MARKER

    value = json.loads(_bounded_bytes(state_dir / _EVENT_FORMAT_MIGRATION_MARKER, limit=65536))
    fields = {
        "schema_version",
        "recovery_dir",
        "state_identity",
        "db_identity",
        "token_identity",
        "before",
        "after",
        "databases",
        "projections",
    }
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
    ):
        raise _EventConversionRefused("Pending event conversion marker is invalid.")
    name = value["recovery_dir"]
    if (
        not isinstance(name, str)
        or not name.startswith(_EVENT_CONVERSION_PREFIX)
        or "/" in name
        or "\\" in name
    ):
        raise _EventConversionRefused("Pending event conversion paths are invalid.")
    for field, names in (
        ("before", _EVENT_CONVERSION_FILES),
        ("after", _EVENT_CONVERSION_FILES),
        ("databases", ("before", "after")),
        ("projections", ("before", "after")),
    ):
        if not isinstance(value[field], dict) or set(value[field]) != set(names):
            raise _EventConversionRefused("Pending event conversion manifest is invalid.")
    for field in ("state_identity", "db_identity", "token_identity"):
        identity = value[field]
        if (
            not isinstance(identity, dict)
            or set(identity) != {"dev", "ino"}
            or any(type(item) is not int or item < 0 for item in identity.values())
        ):
            raise _EventConversionRefused("Pending conversion identity is invalid.")
    for field in ("before", "after", "databases", "projections"):
        for artifact, digest in value[field].items():
            if (
                field == "before"
                and artifact in {"id_mapping.json", ".gitattributes"}
                and digest is None
            ):
                continue
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise _EventConversionRefused("Pending conversion digest is invalid.")
    _directory(state_dir / name)
    return value


def _publish_event_conversion(state_dir: Path, marker: dict[str, Any], token: Any) -> None:
    from anvil.cli.local_event_repair import (
        _directory,
        _identity,
        _regular,
        _safe_sidecars,
        _write_file,
    )
    from anvil.cli.repair import (
        _checkpoint_and_fsync_live_projection,
        _fsync_directory,
        _fsync_file,
        _publish_staged_projection,
    )
    from anvil.project_snapshot import validate_converged_event_log
    from anvil.state.sqlite import _EVENT_FORMAT_MIGRATION_MARKER, _STATE_OPERATION_TOKEN

    directory = _directory(state_dir)
    if marker["state_identity"] != {"dev": directory.st_dev, "ino": directory.st_ino}:
        raise _EventConversionRefused("Conversion State identity changed.")
    if marker["db_identity"] != _identity(state_dir / "state.db") or marker[
        "token_identity"
    ] != _identity(state_dir / _STATE_OPERATION_TOKEN):
        raise _EventConversionRefused("Conversion database or token identity changed.")
    recovery = state_dir / marker["recovery_dir"]
    _directory(recovery)
    # Validate every staged artifact and live file before any resumed mutation.
    for side in ("before", "after"):
        directory = recovery / side
        _directory(directory)
        for name, digest in marker[side].items():
            if digest is not None and _conversion_digest(directory / name) != digest:
                raise _EventConversionRefused("Conversion artifact digest changed.")
        if _conversion_digest(directory / "state.db") != marker["databases"][side]:
            raise _EventConversionRefused("Conversion database backup digest changed.")
        if _conversion_projection_digest(directory / "state.db") != marker["projections"][side]:
            raise _EventConversionRefused("Conversion projection backup changed.")
    if (
        _conversion_digest(state_dir / f"events.jsonl{_BACKUP_SUFFIX}")
        != marker["before"]["events.jsonl"]
    ):
        raise _EventConversionRefused("Conversion event backup changed.")
    for name in _EVENT_CONVERSION_FILES:
        path = state_dir / name
        _regular(path, required=marker["before"][name] is not None)
        observed = _conversion_digest(path) if path.exists() else None
        if observed not in (marker["before"][name], marker["after"][name]):
            raise _EventConversionRefused(
                "Live conversion file changed outside the recorded publication."
            )
    _safe_sidecars(state_dir / "state.db")
    if _conversion_projection_digest(state_dir / "state.db") not in marker["projections"].values():
        raise _EventConversionRefused(
            "Live conversion projection changed outside the recorded publication."
        )
    for name in _EVENT_CONVERSION_FILES:
        target = state_dir / name
        if target.exists() and _conversion_digest(target) == marker["after"][name]:
            target.chmod(0o600)
            _fsync_file(target)
            continue
        temporary = recovery / f"publish-{uuid.uuid4().hex}"
        _write_file(temporary, (recovery / "after" / name).read_bytes())
        # Staging's private parent protects the initial write. Restrict the file
        # before it becomes visible at the public State pathname (also on resume).
        temporary.chmod(0o600)
        _fsync_file(temporary)
        os.replace(temporary, target)
        _fsync_directory(state_dir)
        _event_conversion_hook(f"after_{name}")
    with closing(sqlite3.connect(state_dir / "state.db")) as connection:
        _publish_staged_projection(recovery / "after" / "state.db", connection)
        _checkpoint_and_fsync_live_projection(connection, state_dir / "state.db")
        connection.row_factory = sqlite3.Row
        with (state_dir / "events.jsonl").open("rb") as events:
            validate_converged_event_log(connection, events)
    _event_conversion_hook("after_database")
    token.seek(0)
    token.write(b"git\n")
    token.truncate()
    token.flush()
    os.fsync(token.fileno())
    _event_conversion_hook("after_fence")
    marker_path = state_dir / _EVENT_FORMAT_MIGRATION_MARKER
    _regular(marker_path)
    marker_path.unlink()
    _fsync_directory(state_dir)


@migrate_app.command("state", help=_MIGRATE_STATE_HELP)
def migrate_state(
    yes: bool = typer.Option(  # noqa: B008
        False,
        "--yes",
        help="Apply the migration. Without this flag this is a dry run.",
    ),
    json_output: bool = JSON_OPTION,
) -> None:
    """Preview or apply an idle-State upgrade under the engine's writer lock."""
    from anvil.cli._helpers import BoundedSchemaProbe
    from anvil.state.sqlite import SqliteBackend, _schema_initialization_lock

    state_dir = _resolve_state_dir(None)
    _require_state_dir(state_dir, command="migrate state", json_output=json_output)
    db_path = state_dir / "state.db"
    events_path = state_dir / "events.jsonl"
    schema_probe = BoundedSchemaProbe()
    try:
        from_version = schema_probe(db_path)
        SqliteBackend.validate_schema_compatibility(from_version)
        # A migration must not initialize a competing project or create a missing
        # authority log. The stable log is also the cross-process writer token.
        _require_existing_state_files(state_dir)
        with _schema_initialization_lock(
            str(db_path), str(events_path), create_missing_log=False,
        ):
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
    _require_existing_state_files(state_dir)
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
