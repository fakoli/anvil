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
import shutil
from pathlib import Path

import typer

from anvil.cli._helpers import (
    _require_state_dir,
    _resolve_state_dir,
)
from anvil.cli._json import JSON_OPTION, emit_success, fail

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

    # ------------------------------------------------------------------
    # Preconditions: valid config, not already migrated, no active claims.
    # load_config (not the narrow reader) on purpose — migration rewrites
    # the project's source of truth, so a fully valid config is a fair gate.
    # ------------------------------------------------------------------
    from anvil.config import load_config

    try:
        config = load_config(config_path)
    except (OSError, ValueError) as exc:
        typer.echo(f"Error: cannot load {config_path}: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if config.events_storage == "git":
        typer.echo("events_storage is already 'git' — nothing to migrate.")
        raise typer.Exit(code=0)

    from anvil.cli._helpers import _open_backend

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


@migrate_app.command("state")
def migrate_state(
    yes: bool = typer.Option(  # noqa: B008
        False,
        "--yes",
        help="Apply the migration. Without this flag the command is a dry run.",
    ),
    json_output: bool = JSON_OPTION,
) -> None:
    """Upgrade .anvil/state.db to the current engine schema version.

    T009/F006. The ordered, idempotent forward migration branches (0/1→8, 2→8,
    3→8, 4→8, 5→8, 6→8, 7→8) ALREADY live inside
    ``SqliteBackend._check_schema_version`` and run automatically on
    ``initialize()``. This command promotes that in-init
    migration to an explicit, backed-up, dry-run-by-default operation — it does
    NOT add a new migration framework.

    Flow:

    1. Read the TRUE on-disk ``schema_version`` via ``read_db_schema_version``
       (the T007 accessor) — without migrating.
    2. If already at ``SCHEMA_VERSION``, report a no-op and exit 0.
    3. Refuse while any claim is active (same guard as ``migrate-events``): a
       mid-flight agent reads/writes the projection, and a schema change under
       it would corrupt its next write.
    4. Dry-run by default — report the from→to versions and exit. With ``--yes``,
       copy ``state.db`` to ``state.db.pre-schema-migration.bak`` and then run
       the existing engine migration by calling ``initialize()`` (which captures
       the pre-DDL version and runs the forward branches up to ``SCHEMA_VERSION``).
    """
    from anvil.state.backend import SchemaMismatch
    from anvil.state.schema import SCHEMA_VERSION
    from anvil.state.sqlite import read_db_schema_version

    command = "migrate state"

    state_dir = _resolve_state_dir(None)
    _require_state_dir(state_dir, command=command, json_output=json_output)

    db_path = state_dir / "state.db"

    # 1. TRUE on-disk version, read WITHOUT migrating (T007 accessor). A missing
    #    db reports 0 — there is nothing to migrate, and initialize() would
    #    create a fresh db already stamped at SCHEMA_VERSION.
    from_version = read_db_schema_version(db_path)

    # 2. Already current → idempotent no-op (covers "re-running migrate is a
    #    no-op" once the db has been brought up to SCHEMA_VERSION).
    if from_version == SCHEMA_VERSION:
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
    #     Surface it cleanly rather than letting initialize() raise mid-apply.
    if from_version > SCHEMA_VERSION:
        msg = (
            f"state.db schema version {from_version} is NEWER than this "
            f"engine's version {SCHEMA_VERSION}. Upgrade anvil — this "
            "build cannot migrate a forward-versioned database backward."
        )
        if json_output:
            fail(command, msg, code="schema_too_new")
        typer.echo(f"Error: {msg}", err=True)
        raise typer.Exit(code=1)

    # 3. Active-claim guard (same contract as migrate-events). Open the backend,
    #    but DO NOT let initialize() migrate yet on a non-dry run path — we open
    #    via the standalone read above, so opening the backend to list claims
    #    would already run the migration. Avoid that by querying claims through
    #    a short-lived read that does not migrate: open in dry-run order.
    #
    #    We must list active claims, which requires an initialized backend, and
    #    initialize() runs the migration. To keep the dry run side-effect-free,
    #    list claims against a SCRATCH copy of the db so the real db is never
    #    mutated until --yes.
    active_ids = _active_claim_ids_without_migrating(state_dir, db_path)
    if active_ids:
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

    # 4a. Dry run (default): report and exit, mutating nothing.
    if not yes:
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

    # 4b. Apply (--yes): refuse to clobber a leftover backup, copy state.db, then
    #     run the existing engine migration by initializing the backend (which
    #     captures the pre-DDL version and runs the forward branches).
    if backup_path.exists():
        msg = (
            f"backup {backup_path} already exists (previous migration "
            "attempt?). Move it aside before re-running."
        )
        if json_output:
            fail(command, msg, code="backup_exists")
        typer.echo(f"Error: {msg}", err=True)
        raise typer.Exit(code=1)

    # Copy the db AND its WAL/SHM sidecars so the backup is a faithful restore
    # point: a WAL-mode db with an uncheckpointed -wal carries committed rows
    # not yet folded into the main file. copy2 preserves mtime; missing sidecars
    # are skipped (a checkpointed db has none).
    shutil.copy2(db_path, backup_path)
    for sidecar_suffix in ("-wal", "-shm"):
        sidecar = db_path.with_name(db_path.name + sidecar_suffix)
        if sidecar.exists():
            shutil.copy2(
                sidecar, backup_path.with_name(backup_path.name + sidecar_suffix)
            )

    from anvil.cli._helpers import _open_backend

    try:
        backend = _open_backend(state_dir)
    except SchemaMismatch as exc:
        # No forward branch matched (handled the > case above, but a gap inside
        # the supported range still raises). Leave the backup in place and the
        # db untouched beyond whatever initialize did before raising.
        msg = (
            f"cannot migrate state.db from version {from_version} to "
            f"{SCHEMA_VERSION}: {exc}. The pre-migration backup is at "
            f"{backup_path}."
        )
        if json_output:
            fail(command, msg, code="schema_mismatch")
        typer.echo(f"Error: {msg}", err=True)
        raise typer.Exit(code=1) from None

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
            },
        )
        return
    typer.echo(f"Migrated state.db v{from_version} -> v{to_version}.")
    typer.echo(f"Backup written to {backup_path}.")
    typer.echo(
        "Re-running `anvil migrate state` is now a no-op. Delete the "
        "backup once you have verified the migration."
    )


def _active_claim_ids_without_migrating(state_dir: Path, db_path: Path) -> list[str]:
    """Return active claim ids, listing them WITHOUT migrating the live state.db.

    The active-claim guard must run on the DRY-RUN path too, but opening the
    real backend calls ``initialize()`` — which would migrate the live db before
    the user passed ``--yes``. To keep the dry run side-effect-free we replay the
    event log into a SCRATCH db (built from the current schema DDL) and list
    active claims there. The scratch db reflects the same canonical claim state
    as the live db (both derive from ``events.jsonl``) without touching it.

    If there is no event log yet (a brand-new project), there are no claims.
    """
    events_path = state_dir / "events.jsonl"
    if not events_path.exists():
        return []

    import tempfile

    from anvil.clock import SystemClock
    from anvil.config import read_events_storage
    from anvil.state.sqlite import SqliteBackend

    with tempfile.TemporaryDirectory() as tmpdir:
        scratch_db = str(Path(tmpdir) / "scratch.db")
        backend = SqliteBackend(
            db_path=scratch_db,
            events_path=str(events_path),
            clock=SystemClock(),
            events_storage=read_events_storage(state_dir / "config.yaml"),
        )
        backend.initialize()
        try:
            backend.replay_from_empty(str(events_path))
            return [c.id for c in backend.list_active_claims()]
        finally:
            backend.close()


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
