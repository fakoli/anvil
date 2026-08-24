# Migrations

> **Audience:** users upgrading anvil across versions and hitting a schema or workspace-layout change.

`anvil` ships a small schema (one SQLite DB, one JSONL audit log) and
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
| v6      | Typed proofs (SL-3 / B48) | `evidence` adds `proofs TEXT NOT NULL DEFAULT '[]'` — a JSON array of typed `ProofArtifact`s (CommandProof / DiffProof / LinkProof / AssertionProof). The DEFAULT backfills every existing row to "no typed proofs" (the pre-v6 meaning). Additive: legacy string evidence fields stay.      |
| v7      | Multi-PRD persistence (v0.3 / T002) | `prds` becomes a multi-row table keyed on a single-column `id` PRIMARY KEY (not composite) and gains `title`/`target_version`/`target_tag`/`is_default`/`created_at`/`updated_at`, with a partial unique index enforcing at most one default PRD; `requirements`/`features`/`tasks` each gain a `prd_id` partition column (DEFAULT `'default'`); `requirements` also gains nullable `revision_introduced`/`revision_superseded` lineage columns; `sync_mappings` gains `prd_id`/`entity_kind` columns. The v6→v7 migration rebuilds `prds` (SQLite can't ALTER a PRIMARY KEY) and ALTER-backfills every other column via its DEFAULT, so existing rows adopt the default PRD with zero data loss.      |
| v8      | Multi-PRD revisions (v0.3 / T023) | `prds` adds `revision INTEGER NOT NULL DEFAULT 1` — the per-PRD monotonic revision counter bumped by `prd.revised`. Purely additive: the DEFAULT backfills every pre-existing v7 PRD row to revision 1. A separate version from v7 (not folded in) because v7 already shipped without it; a DB already stamped v7 must re-enter the migration ladder via the v8 bump to grow the column.      |
| v9      | Evidence contracts (issue #153) | `tasks` adds `claims`; `evidence` adds `category`. Additive defaults preserve pre-contract behavior. |
| v10     | Distinct-actor concurrency guard | `claims` adds nullable `session_id`; historical claims remain session-unknown. |
| v11     | Execution bundles (issue #171) | Adds `execution_bundles`, FK-protected position-ordered `execution_bundle_members`, and internal `claim_replay_lineages` fencing for divergent legacy claim IDs. Bundle policy and optional agent observations are JSON columns; existing task/claim/evidence rows are unchanged. |
| v12     | Bundle coordinator claims (issue #171) | Adds one public `bundle_claims` lease per execution bundle and nullable `claims.bundle_claim_id` links for atomic internal member evidence authorizations. Existing task claims remain unlinked and unchanged. |
| v13     | Bundle review dispositions (issue #171) | Binds every adversarial verdict to the exact `implemented_unreviewed` transition that opened its review cycle, preventing an older quorum from satisfying later rework. |
| v14     | Bundle delivery lineage (issue #171) | Adds a named `superseded_by` bundle reference while retaining checkpoint and reconciliation history. |
| v15     | Bundle result projection (issue #171) | Adds authoritative `last_result_at` timing for applied reviewed/integrated/merged/completed transitions. |
| v16     | Behavior-first PRD readiness | `prds` adds `assumptions TEXT NOT NULL DEFAULT '[]'`, storing typed, stable PRD assumptions alongside the canonical PRD state. The additive default preserves the prior meaning for every existing PRD: no recorded assumptions. |
| v17     | Claim-bound progress attestations | `claims` adds monotonic `generation` and nullable immutable `attestation_context`; `claim_progress_attestations` records one pending, consumed, invalidated, or quarantined progress fact per claim generation. Legacy claims receive deterministic generations and remain unattestable when their context is NULL. |
| v18     | Revision-bound PRD source provenance | `prds` adds exact source bytes plus digest, size, UTF-8 encoding, source revision, and explicit availability fields. Existing rows retain their lifecycle and ownership state but migrate as `legacy_unbound` with no fabricated source or digest. |
| v19     | Task-rejection provenance | `reviews` adds immutable engine-derived category, reason, attempt/claim identity, evidence digest, typed findings, matched process predicate, and accept-rate accounting. Historical task rejections migrate conservatively as counting `quality`; no missing identity is fabricated. |
| v20     | Transactional claim Git bindings | `claims` and `bundle_claims` add nullable `git_metadata` containing the validated selected base, exact claim-start identity, branch, repository root, and shared/isolated target. Historical claims remain unbound. |
| v21     | Revision-bound PRD lifecycle | `prds` adds canonical material/content identity and the exact source/material/content lineage reviewed or approved. Pre-v21 review and approval state is preserved in the audit log but demoted to unbound `draft`; Anvil never fabricates proof that historical content was reviewed. |

Canonical PRD titles require no new schema migration: `prds.title` has existed
since v7. Historical rows and legacy events that have no title remain `""`;
Anvil never guesses from a PRD id or source file during migration or listing.
An explicit `prd parse` reparse records the first canonical title as an audited
`prd.revised` event.

## Execution bundles — v0-v10 → v11 auto-upgrade

The v11 migration is additive. It creates three new tables and three indexes; it
does not rewrite existing entities:

- `execution_bundles` stores coordinator-owned lifecycle, review policy,
  throughput budget, optional agent observations, and delivery metadata.
- `execution_bundle_members` stores ordered task membership with `RESTRICT`
  foreign keys so task or bundle history cannot be deleted underneath an audit
  record.
- `claim_replay_lineages` fingerprints immutable claim-creation facts and
  fail-closes descendants when merged legacy events reuse one claim ID for
  divergent creations that cannot identify a unique lineage.

Current DDL runs before the ordered migration ladder, and the v10→v11 ladder
step repeats the table/index creation idempotently. The schema version is
stamped 11 only after the complete transaction succeeds. A failure rolls back
without changing `PRAGMA user_version`; reopening retries safely. Replaying an
older `events.jsonl` produces no bundle rows and preserves the legacy snapshot
shape.

## Bundle coordinator claims — v11 → v12 auto-upgrade

The v12 migration creates `bundle_claims` and adds nullable
`claims.bundle_claim_id`. A bundle claim is the single public coordinator lease;
the linked task claims are internal authorizations that preserve the existing
task-scoped evidence and disposition contract. The nullable link leaves every
pre-v12 task claim byte-compatible in legacy snapshots.

Existing tasks do not need conversion before bundle adoption. Bundle creation adds ordered
membership without rewriting task IDs, dependencies, claims, or evidence. Historical
evidence remains auditable, while a newly claimed bundle requires fresh evidence bound to
its new member authorizations. Follow the guarded adoption sequence in
[Coordinating a milestone bundle](how-to/coordinating-a-bundle.md#adopting-existing-tasks-without-losing-history).
When a bundle reaches `replan_required`, a replacement generation may retain the same task
IDs; supersession preserves old evidence, reopens shared review-state members, and gives
the replacement fresh claim/evidence lineage.

## Bundle review dispositions — v12 → v13 auto-upgrade

The v13 migration adds `execution_bundles.review_disposition_event_id` and
rebuilds `bundle_review_verdicts` with an explicit disposition-event lineage.
Historical verdicts are retained as `legacy-unbound`; they remain auditable but
cannot silently satisfy a newly opened needs-review disposition.

## Bundle delivery lineage — v13 → v14 auto-upgrade

The v14 migration adds nullable `execution_bundles.superseded_by`. Existing
bundles remain unsuperseded; new supersession events preserve both source and
replacement bundle rows and all task evidence.

## Bundle result projection — v14 → v15 auto-upgrade

The v15 migration adds nullable `execution_bundles.last_result_at`. New applied
reviewed, integrated, merged, and completed transitions update it atomically
with bundle status. For an existing bundle already in one of those states, the
migration uses the projection's `updated_at` as a conservative baseline; it
does not derive timing from raw audit-log events that may have been replay
no-ops. Future result transitions replace that baseline with their exact time.

## Behavior-first PRD readiness — v15 → v16 auto-upgrade

The v16 migration adds `prds.assumptions TEXT NOT NULL DEFAULT '[]'`. It is an
additive JSON projection of the typed assumptions captured in newer
`prd.parsed` and `prd.revised` events. Older event payloads omit this optional
field and are interpreted as `[]`, so replay preserves their historical
meaning without rewriting the audit log.

## Claim-bound progress attestations — v16 → v17 auto-upgrade

The v17 migration adds `claims.generation`, nullable
`claims.attestation_context`, and the `claim_progress_attestations` projection.
Existing claims are assigned deterministic per-task generations ordered by
creation time and claim ID. Their context remains NULL, so their historical
renewal behavior is preserved and external attestations cannot be attached
retroactively. New Git-backed standalone claims capture immutable repository,
task, PRD, and expected-path context for one-use progress attestations.

## Revision-bound PRD source provenance — v17 → v18 auto-upgrade

The v18 migration adds nullable exact-source columns to `prds` together with
explicit provenance and content-availability flags. Every pre-v18 PRD is marked
`legacy_unbound`: its source bytes, digest, size, encoding, and source revision
remain NULL. Anvil does not guess provenance from the current filesystem or
fabricate a digest for historical state.

PRD lifecycle, revision, feature ownership, task state, claims, and evidence
are otherwise preserved. New provenance-bearing `prd.parsed` and `prd.revised`
events update the complete source tuple in the same projection transaction as
the corresponding PRD revision. Because exact PRD source bytes can be stored in
`state.db`, treat database backups as containing the authored PRD content.

## Engine-derived task-rejection provenance — v18 → v19 auto-upgrade

The v19 migration adds rejection-category, reason-code, claim/review-attempt
identity, evidence-digest, typed-finding, matched-process-predicate, and
accept-rate-accounting columns to `reviews`. New rejected `task.applied`
events carry the exact immutable provenance object derived from persisted
evidence and claim state; live append and replay independently recompute it.

Historical task rejections had no typed provenance. They migrate and replay as
counting `quality` rejections with reason `unspecified_quality`; claim,
attempt, evidence-digest, and process-predicate fields remain NULL rather than
being inferred. Historical accepted and non-task reviews retain their prior
shape.

New accepted `task.applied` events also bind the exact evidence attempt that
was reviewed. Live append validates that binding under the event lock, and
replay recomputes it before projecting the accepted review. Historical
accepted events that predate the v1 payload discriminator remain deliberately
unbound (`review_attempt_id` and submitter stay NULL); migration and replay do
not guess an identity for them.

## Transactional claim Git bindings — v19 → v20 auto-upgrade

The v20 migration adds nullable `git_metadata` to task and bundle claim
projections. New Git-backed claims persist the already validated selected base,
exact claim-start ref and commit, branch, canonical repository root, and shared
or isolated target so state and filesystem preparation can be rolled back as
one operation. Existing claims retain `NULL`: migration does not inspect a
working tree or invent historical Git identity. The additive columns and schema
stamp commit in one SQLite transaction and safely retry after interruption.

## Revision-bound PRD lifecycle — v20 → v21 auto-upgrade

The v21 migration adds `material_sha256`, `content_event_id`, the four complete
`lifecycle_*` binding fields, and `review_event_id` to each PRD projection.
Where exact v18 source provenance and a matching historical content event are
available, migration computes the canonical material digest and retains that
content identity. It never reads a current source file to fill history.

Pre-v21 `prd.reviewed` and `prd.approved` events did not carry the complete
source/material/content binding. They remain immutable in `events.jsonl`, but
their projection is demoted to `draft`, reviewer metadata is cleared, and the
legacy PRD review projection is removed. This conservative migration requires
operators to run `anvil prd parse`, `anvil prd review`, and
`anvil prd review --approve` once against the current canonical source before
new task or bundle claims. Existing active claims remain valid and auditable.

New lifecycle events bind the exact persisted revision, source digest,
canonical material digest, and content event. A revision that changes only the
H1 project title may retain that binding because title is replaced by a fixed
sentinel for the material digest; every other material change returns the PRD
to `draft`. Live append and replay both validate the complete lineage.

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
$ sqlite3 .anvil/state.db "PRAGMA user_version;"
3
```

If the version is still 1, 2, or 3 after running any `anvil` command, the
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
git-mode entry path (`anvil migrate-events --to git`) rebuilds the
projection from scratch, recreating `events` from the v4 DDL.

```bash
$ sqlite3 .anvil/state.db "PRAGMA user_version;"
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
$ sqlite3 .anvil/state.db "PRAGMA user_version;"
5
```

## Typed proofs (SL-3 / B48) — v0–v5 → v6 auto-upgrade

The v6 diff is **purely additive**: `evidence` gains a `proofs` column
(`ALTER TABLE evidence ADD COLUMN proofs TEXT NOT NULL DEFAULT '[]'`,
duplicate-column tolerant so a crashed upgrade can re-run). The `DEFAULT '[]'`
backfills every pre-v6 row to "no typed proofs," which is the correct pre-SL-3
meaning, so no data is rewritten. The column stores a JSON array of typed
`ProofArtifact`s — `CommandProof` (command + real `exit_code` + `output_sha256`),
`DiffProof`, `LinkProof`, `AssertionProof` — which the review gate evaluates
against a task's `Verification.required_proofs`. The legacy free-text
`required_evidence` / string evidence fields are untouched (the change is
additive, not a rename), so old `events.jsonl` logs replay unchanged.

```bash
$ sqlite3 .anvil/state.db "PRAGMA user_version;"
6
```

## Explicit migration: `anvil migrate state`

The auto-upgrade described above runs **silently inside `initialize()`** when a
normal state command first initializes the backend after an engine upgrade —
convenient, but invisible and un-backed-up. Project-free commands such as
`anvil --version`, the non-opening `anvil status --path-only` probe, and the
default dry-run of `anvil migrate state` deliberately do not initialize and
migrate the backend. For operators who want the migration to be deliberate,
`anvil migrate state` promotes that same in-init migration to an
explicit, backed-up, dry-run-by-default command. It does **not** introduce a new
migration framework — it runs the ordered, idempotent `_MIGRATIONS` chain from
every supported historical version through the current `SCHEMA_VERSION`
(currently v21, including the final v20→v21 step) that already lives in
`SqliteBackend._check_schema_version`.

```bash
# Inspect what would happen (dry run — mutates nothing):
$ anvil migrate state
Schema migration  : v3 -> v21
Will back up      : /repo/.anvil/state.db
            to    : /repo/.anvil/state.db.pre-schema-migration.bak

Dry run — nothing written. Re-run with --yes to apply.

# Apply it:
$ anvil migrate state --yes
Migrated state.db v3 -> v21.
Backup written to /repo/.anvil/state.db.pre-schema-migration.bak.
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

## `anvil migrate-workspace` — legacy in-repo state → HOME workspace

Distinct from `anvil migrate state` above: this command moves *where*
`.anvil/` lives, not the schema inside it. Before the HOME-workspace default
(B44), `anvil` kept state in-repo at `<repo>/.anvil` (or `<repo>/bin/.anvil`
for the anvil-on-anvil dogfooding case). `anvil migrate-workspace` copies that
legacy directory into the canonical HOME workspace
(`~/.anvil/workspaces/<key>/.anvil`) so a project that predates the HOME
default resolves its history under the new layout.

Behaviour:

- **Dry-run by default** — reports source → target and a file count, writing
  nothing; re-run with `--yes` to apply.
- **No-clobber** — if a HOME workspace already exists for the project, it is
  authoritative and the command skips entirely (never overwrites it).
- **Copy, never move** — the legacy directory is left in place as a fallback.
- **Atomic** — copies into a temp sibling, then does an atomic replace into
  the target, so an interrupted copy never leaves a half-populated workspace.
- **Whole-tree** — copies all of `.anvil/` (`state.db` plus `-wal`/`-shm`
  sidecars, `events.jsonl`, config, prd, `packets/`, `.evidence-buffer/`).

```bash
# Inspect what would happen (dry run — mutates nothing):
$ anvil migrate-workspace
Would copy 12 file(s) from /repo/.anvil → ~/.anvil/workspaces/<key>/.anvil.
Dry run — nothing written. Re-run with --yes to apply.

# Apply it:
$ anvil migrate-workspace --yes
Migrated 12 file(s) from /repo/.anvil → ~/.anvil/workspaces/<key>/.anvil.
The legacy directory was left in place as a fallback.
```

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

The `events.jsonl` replay verifier is always available. It deliberately builds
a scratch database and refuses to overwrite live state. Resolve the source log
from the current state layout and choose an inspected, non-live destination:

```bash
anvil_state_dir="$(anvil status --path-only)"
anvil replay \
  --from-events "$anvil_state_dir/events.jsonl" \
  --into "/safe/scratch/rebuilt-state.db"
```

Compare the scratch projection before any separately authorized replacement of
live state; replay itself never performs that replacement.
