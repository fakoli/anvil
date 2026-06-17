# Migrations

`fakoli-state` ships a small schema (one SQLite DB, one JSONL audit log) and
keeps its migration story minimal: the canonical audit log is `events.jsonl`,
and `backend.replay_from_empty()` rebuilds `state.db` from scratch on any
codebase version. That makes migrations easy to reason about — most schema
changes don't actually need a migration in the SQL sense; we just bump
`SCHEMA_VERSION` and document the diff.

## Version history

| Version | Phase     | Change                                                                                                                                                                  |
|---------|-----------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| v1      | Phase 2-7 | Initial schema (projects, prds, requirements, features, tasks, claims, evidence, decisions, reviews, events, conflict_groups). No `sync_mappings` table.                |
| v2      | Phase 8 prep | `sync_mappings` table added (composite PK only; no UNIQUE on external_id; FK `ON DELETE RESTRICT`).                                                                  |
| v3      | Phase 8 (v1.8.0) | `sync_mappings` adds `UNIQUE(external_system, external_id)`, `external_url` column, `provider_metadata_json` column, FK flipped to `ON DELETE CASCADE`.        |
| v4      | Git-backed events Phase A (v1.22.0) | `events.id` CHECK widened to accept hash-chained ids (`E-<12 hex>`); nullable `events.seq` column added (replay-assigned display order in git mode; NULL in local mode).      |
| v5      | Non-feature task types (T015) | `tasks` adds `task_type TEXT NOT NULL DEFAULT 'feature'` so a brownfield PRD can describe bugfix / refactor / modify work. The DEFAULT backfills every existing row to `feature` (the pre-v5 meaning).      |

## Phase 8 (v1.8.0) — v1 / v2 → v3 auto-upgrade

The schema diff from v1/v2 to v3 is **purely additive**:

- New columns on `sync_mappings`: `external_url`, `provider_metadata_json`.
  Both nullable. Existing rows get NULL; existing code that doesn't read them
  is unaffected.
- New `UNIQUE(external_system, external_id)` constraint. Pre-Phase-8 (v1)
  databases have **no** rows in `sync_mappings` (the table doesn't exist).
  v2 databases have the table but cannot contain a row in violation of the
  new UNIQUE because the upsert handler in v2 already keyed on
  `(task_id, external_system)`, and the only way to land two rows with the
  same `(external_system, external_id)` would have been to deliberately
  cross-claim a single external record across two tasks — a state the v2
  handler emitted no event for and the v2 CLI offered no command for.
- FK direction flip: `ON DELETE RESTRICT` → `ON DELETE CASCADE`. Affects
  what happens on `DELETE FROM tasks WHERE id=?`; no Phase 2-7 codepath
  issues such a DELETE. Pure schema-shape change.

Because every diff is additive and no live rows can violate the new
constraints, `SqliteBackend._check_schema_version()` auto-upgrades v1 and v2
databases to v3 on first open: the DDL (which uses
`CREATE TABLE IF NOT EXISTS`) is re-applied, then `PRAGMA user_version` is
bumped. No data is rewritten and no offline migration is required.

If you need to verify the upgrade manually:

```bash
$ sqlite3 .fakoli-state/state.db "PRAGMA user_version;"
4
```

If the version is still 1, 2, or 3 after running any `fakoli-state` command, the
upgrade did not fire — `initialize()` was never invoked. Most likely a
process-supervision oddity; open a bug.

## Git-backed events Phase A (v1.22.0) — v0–v3 → v4 auto-upgrade

The v4 diff is **purely additive for local mode**: `events` gains a nullable
`seq` column (`ALTER TABLE events ADD COLUMN seq INTEGER`, duplicate-column
tolerant so a crashed upgrade can re-run). Existing rows keep `seq` NULL —
in local mode the monotonic `E{N}` id IS the display order.

The widened `events.id` CHECK (`E[0-9]*` OR `E-*`) only exists in the v4
DDL; SQLite cannot ALTER a CHECK, so pre-v4 tables keep the strict pattern.
That is deliberate and harmless: local mode never writes a hash id, and the
git-mode entry path (`fakoli-state migrate-events --to git`) rebuilds the
projection from scratch, recreating `events` from the v4 DDL.

```bash
$ sqlite3 .fakoli-state/state.db "PRAGMA user_version;"
4
```

## Non-feature task types (T015) — v0–v4 → v5 auto-upgrade

The v5 diff is **purely additive**: `tasks` gains a `task_type` column
(`ALTER TABLE tasks ADD COLUMN task_type TEXT NOT NULL DEFAULT 'feature'`,
duplicate-column tolerant so a crashed upgrade can re-run). The `DEFAULT
'feature'` backfills every pre-v5 row to the value that matches its original
meaning, so no data is rewritten and the loop behaves identically for tasks
that predate the column. New PRDs can declare `**Type:** bugfix` (or
`refactor` / `modify`) per task; everything else defaults to `feature`.

```bash
$ sqlite3 .fakoli-state/state.db "PRAGMA user_version;"
5
```

## Explicit migration: `fakoli-state migrate state`

The auto-upgrade described above runs **silently inside `initialize()`** on the
first `fakoli-state` command after an engine upgrade — convenient, but invisible
and un-backed-up. For operators who want the migration to be deliberate,
`fakoli-state migrate state` promotes that same in-init migration to an
explicit, backed-up, dry-run-by-default command. It does **not** introduce a new
migration framework — it runs the exact ordered, idempotent forward branches
(`0/1→5`, `2→5`, `3→5`, `4→5`) that already live in
`SqliteBackend._check_schema_version`.

```bash
# Inspect what would happen (dry run — mutates nothing):
$ fakoli-state migrate state
Schema migration  : v3 -> v4
Will back up      : /repo/.fakoli-state/state.db
            to    : /repo/.fakoli-state/state.db.pre-schema-migration.bak

Dry run — nothing written. Re-run with --yes to apply.

# Apply it:
$ fakoli-state migrate state --yes
Migrated state.db v3 -> v4.
Backup written to /repo/.fakoli-state/state.db.pre-schema-migration.bak.
```

Behaviour:

- **Detects the TRUE on-disk version** via the `read_db_schema_version`
  accessor (read-only; never migrates as a side effect of detection).
- **Dry-run by default** — reports `from → to` and exits without touching the
  db. `--yes` applies.
- **Backs up `state.db`** (and any `-wal`/`-shm` sidecars) to
  `state.db.pre-schema-migration.bak` before mutating, and refuses to clobber
  an existing backup from a prior attempt.
- **Refuses while any claim is active** — same guard as `migrate-events`. A
  mid-flight agent reads/appends to the projection; migrating it out from under
  the agent corrupts its next write.
- **Idempotent** — once the db is at the current `SCHEMA_VERSION`, re-running is
  a reported no-op that mutates nothing.
- **`--json`** emits the standard envelope
  (`{"ok": true, "command": "migrate state", "data": {"from_version", "to_version", "applied", "migrated", "backup"}}`).

The `replay` escape hatch below remains available; `migrate state` is the
in-place, row-preserving path, while `replay` rebuilds `state.db` from the audit
log.

## When you need a real migration

Any non-additive schema change (renaming a column, dropping a table, changing
a column's type, adding a NOT NULL with no default) requires:

1. Bump `SCHEMA_VERSION` (one step at a time — v3 → v4, never v3 → v5).
2. Add an `_upgrade_vN_to_vN_plus_one()` helper in `state/sqlite.py` that
   runs the data migration in a single BEGIN IMMEDIATE.
3. Call the helper from `_check_schema_version()` when `on_disk == N` and
   `SCHEMA_VERSION == N + 1`.
4. Add a test that creates a v(N) db (raw SQL), opens it with the v(N+1)
   backend, and asserts the upgrade ran.
5. Re-document in this file.

The "replay events.jsonl" escape hatch is always available for users who
prefer to rebuild from the audit log:

```bash
$ rm .fakoli-state/state.db .fakoli-state/state.db-wal .fakoli-state/state.db-shm
$ fakoli-state replay   # rebuilds state.db from events.jsonl
```
