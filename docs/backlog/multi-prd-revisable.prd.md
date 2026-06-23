# Project: Multi-PRD, Revisable, Partitioned in One state.db (anvil v0.3)

## Summary

Evolve anvil from a single-PRD-per-project model to **many PRDs coexisting in one `state.db`, each independently
revisable, where a PRD is a release/milestone-scoped plan**. PRDs are partitioned by an explicit `prd_id` on every
Requirement / Feature / Task; the claim gate keys on the task's owning PRD (so an approved release is claimable while
a draft release is not); and — critically — claims and conflict groups stay **global**, so two tasks in different PRDs
that touch the same file still conflict. Revision becomes event-sourced (amend-aware supersede, not destructive
replace), leveraging anvil's existing append-only log + replay. One workspace, one `events.jsonl`, one replay per repo
is preserved throughout. The whole change is gated behind a single v6→v7 migration that backfills a `'default'` PRD
owning all existing rows with zero data loss, so every phase stays backward-compatible.

## Goals

- A `state.db` holds multiple PRDs, each with a stable id, status, and a release target (version/tag).
- Every Requirement/Feature/Task carries an explicit `prd_id`; the claim gate checks the task's OWNING PRD's status.
- Cross-PRD coordination is preserved: conflict groups, active-claim exclusion, and the stale reaper span ALL PRDs.
- Re-parse becomes event-sourced revision (non-destructive supersede + per-PRD revision counter + replay-to-revision).
- An existing single-PRD `state.db` migrates to v7 in one atomic transaction with zero data loss and unchanged replay output.
- CLI/MCP gain an optional `--prd`/`prd_id` selector that defaults to the single/default PRD, so existing usage is byte-identical.
- `anvil status` rolls up per-PRD plus a project total; a PRD maps cleanly to a release/milestone for sync.

## Non-Goals

- Multiple workspaces per repo or multiple Project rows — multi-PRD lives in ONE state.db / one event log / one replay.
- Network-touching GitHub milestone creation (ensure_release_group Protocol + client) — deferred; only the release/sync DATA plumbing lands here.
- Linear/Jira release-group mapping — deferred behind the same capability flag.
- Changing the existing six-dimension scoring model or the task lifecycle state machine.
- Per-PRD separate event logs or per-PRD replay — the audit log stays unified.

## Requirements

- R001: A PRD has a stable identity (`PRD.id`) and release fields (`target_version`, `target_tag`); the `prds` table holds many rows keyed `(id)` with exactly one `is_default` per project.
- R002: Requirement, Feature, and Task each carry an explicit `prd_id` partition column (denormalized onto Task; invariant `Task.prd_id == owning Feature.prd_id` enforced at write time).
- R003: A single v6→v7 migration backfills a `'default'` PRD that owns every existing row with zero data loss; the `SCHEMA_VERSION == 6` literal gate is de-literalized into an ordered migration ladder.
- R004: Replay-from-empty of a pre-v7 log reconstructs the `'default'`-owned state byte-identically; a directly-built multi-PRD DB replays byte-identically (replay-equivalence oracle).
- R005: The backend exposes `get_prd(prd_id)`, `list_prds()`, `default_prd_id()`, and `prd_id` filters on `list_tasks/list_features/list_requirements`; the 12 legacy no-arg `get_prd()` sites resolve to the default PRD.
- R006: The claim gate resolves and checks the task's owning PRD (`get_prd_for_task`); an approved PRD is claimable while a draft PRD is not; the duplicated MCP gate collapses onto ClaimManager.
- R007: Conflict groups, active-claim exclusion, and the stale reaper span ALL PRDs — pinned by regression tests that must land BEFORE any `--prd` narrowing.
- R008: The parser is `prd_id`-load-bearing: the default PRD emits BARE ids (`T001`), named PRDs emit PREFIXED ids (`v0.2:T001`); `prd parse --prd <id>` reads `.anvil/prds/<id>.md` and touches only that PRD's rows.
- R009: `plan --prd` prunes only that PRD's orphans while conflict-group inference reads ALL PRDs' tasks.
- R010: A shared `resolve_prd_id` helper (explicit `--prd` > `ANVIL_PRD` > single|default|ambiguity-error) threads through CLI + MCP with identical resolution; read-only rollups default to all PRDs and never raise ambiguity.
- R011: Re-parse of an existing PRD emits `prd.revised` (non-destructive supersede via revision-lineage columns + a per-PRD revision counter); `serialize_state` enumerates all PRDs deterministically; `replay_to_event_id` reconstructs as-of a revision.
- R012: Release/sync data plumbing: `SyncMapping` gains `prd_id`/`entity_kind`; push stamps the owning `prd_id`; `--prd` scopes push; reconciliation attributes discrepancies to a PRD. Milestone wiring is tracked but deferred.
- R013: Skills, docs, and positioning reframe the PRD as a release-scoped, separately-gated, revisable plan; the v7 schema bump completes the version-lockstep + packaging-manifest + user-facing version-doc refresh.

## Acceptance Criteria

- Opening any v6 DB with v7 code yields one `is_default` PRD `'default'` owning every row; row counts unchanged; migration idempotent and crash-safe.
- A task in an approved PRD is claimable while a sibling in a draft PRD raises `gate_name='prd_status_gate'`.
- A PRD-A task and a PRD-B task sharing a `likely_file` land in ONE conflict group; `next` (no `--prd`) never returns a task colliding with an active cross-PRD claim.
- `anvil prd parse --prd v0.2` reads `.anvil/prds/v0.2.md`, mints `v0.2:`-prefixed ids, and leaves other PRDs untouched; default parse output is byte-identical to today.
- A directly-built 2-PRD multi-revision DB and its `replay_from_empty` rebuild are byte-identical under `serialize_state`.
- Single-PRD, no-`--prd` output for status/plan/next/review is byte-identical to the pre-change baseline (modulo an additive `prds[]`).

## Risks

- SQLite cannot ALTER a PRIMARY KEY, so the `prds` rebuild (CREATE/INSERT-SELECT/DROP/RENAME) must be atomic inside a SAVEPOINT and crash-idempotent.
- A `--prd` filter implemented as `list_tasks(prd_id=)` for the exclusion sets would silently break cross-PRD conflict detection (the moat) — guarded by Phase 3 regression tests landing first.
- Prefixed TaskIDs (`v0.2:T001`) could break any `^T\d+` matcher in claims/skills/drift; keeping the default PRD's ids BARE limits blast radius to newly-named PRDs.
- A legacy no-arg `get_prd()` call site left unaudited silently operates on the default PRD once multiple exist — a correctness trap (Phase 5 audits all 12).
- The v7 bump is publishable: the full version-lockstep + packaging manifests + user-facing version docs must land before publish or `test_version_sync.py`/`test_install_manifests.py` fail.

## Decisions (resolved 2026-06-22)

- PRD-table key & FK: one Project per repo -> single-column `id` PK on `prds`; `prd_id` on Requirement/Feature/Task is a plain indexed column (no hard FK), with the `Task.prd_id == Feature.prd_id` invariant enforced in the write handler + the cross-PRD moat tests. (Phase 0/1; overrides the tentative composite PK.)
- `anvil next` with no `--prd` scans ALL approved PRDs (cross-PRD ready queue), still excluding cross-PRD conflicts + active claims. (Phase 5.)
- Named-PRD ids render verbatim/prefixed (`v0.2:T001`) everywhere; the default PRD keeps bare ids (`T001`). (Phase 4/5.)
- The default PRD's source stays at `.anvil/prd.md`; named PRDs at `.anvil/prds/<id>.md`; no shim, no file migration. (Phase 4.)
- Revise->status: `prd.revised` demotes an approved PRD to `draft` (re-review) IFF it supersedes/removes an existing requirement; pure-additive revisions keep the current status. Deterministic via the `requirements_superseded` set in the diff payload. (Phase 6.)
- `prd.revised` payload carries a full self-describing diff (added / superseded / unchanged requirement sets). (Phase 6.)
- replay-to-revision reconstructs PRD + requirements as-of a revision only (features/tasks are plan-generated, not revision-versioned). (Phase 6.)
- `is_default` is sticky: the migrated/first PRD stays default; `parse` never re-picks it (a dedicated re-default command is deferred). (Phase 5.)
- Sync is independent of the per-PRD status gate by default; a config knob `sync.gate_by_prd_status` (default off) restricts pushing to reviewed/approved PRDs. (Phase 7.)
- `target_tag` is unique per project (1:1 PRD<->release/milestone), enforced by a validation check. (Phase 0/7.)
- The deferred PRD->release sync method is named `ensure_release_group` (provider-neutral: GitHub Milestone / Linear Cycle / Jira FixVersion), not `ensure_release_group`. (Phase 7.)

## Features

### F001: Phase 0 — Model + schema + payload foundation (no behavior change)

Land the pure data-layer foundation — PRDID alias, prd_id partition fields, PRD identity+release fields, v7 DDL carrying EVERY column any later facet needs (partition prd_id, nullable revision lineage, sync_mappings prd_id/entity_kind), and payloads with the shared DEFAULT_PRD_ID='default' replay default — without changing any runtime behavior.

**Requirements:** R001, R002

### F002: Phase 1 — v6->v7 migration + default-PRD backfill + de-literalized ladder + multi-PRD replay oracle

Migrate any existing v6 DB to v7 in one atomic transaction that mints the single 'default' PRD owning all current rows (zero data loss), de-literalize the SCHEMA_VERSION==6 gate, and stand up the multi-PRD replay-equivalence oracle. After this phase every DB has a PRD and every later phase is backward-compatible by construction.

_Depends on features: F001._

**Requirements:** R003, R004

### F003: Phase 2 — Backend partition API (get_prd(prd_id), list_prds, filters, scoped write handlers)

Add the backend read/write seam every surface consumes: get_prd(prd_id)/list_prds()/default_prd_id(), list_tasks/features/requirements prd_id filters, prd_id-scoped write handlers with the Task.prd_id==Feature.prd_id invariant, and per-PRD-scoped destructive re-parse (DELETE FROM requirements WHERE prd_id=?). Zero user-visible change.

_Depends on features: F002._

**Requirements:** R005

### F004: Phase 3 — Per-PRD claim gate + cross-PRD coordination moat (pinned)

Make the claim gate resolve the task's OWNING PRD and check that PRD's status (approved PRD-A claimable while draft PRD-B is not), collapse the duplicated MCP gate onto ClaimManager, and PIN the cross-PRD coordination guarantee (conflict groups and active claims span all PRDs) with regression tests — before any --prd narrowing can hide a cross-PRD conflict.

_Depends on features: F003._

**Requirements:** R006, R007

### F005: Phase 4 — Parser prd_id load-bearing + per-PRD plan/prune (first phase that can mint a 2nd PRD)

Make parse_prd's prd_id load-bearing (bare ids for default, prefixed for named PRDs), add per-PRD source files (.anvil/prds/<id>.md), scope plan/prune to a single PRD while keeping conflict-group inference cross-PRD, and round-trip the Release field. This is the first phase where a second PRD can exist — deliberately after the gate and moat are proven.

_Depends on features: F004._

**Requirements:** R008, R009

### F006: Phase 5 — CLI/MCP --prd surface, resolve_prd_id, ambiguity handling, per-PRD status rollup

Thread the optional --prd selector (CLI flag with ANVIL_PRD envvar, MCP prd_id arg) through every PRD-touching command via one shared resolve_prd_id helper, add the per-PRD status rollup to `anvil status`/get_project_status, and surface the ambiguity error — making multi-PRD usable end-to-end with disambiguation in place.

_Depends on features: F005._

**Requirements:** R010

### F007: Phase 6 — Event-sourced revision (prd.revised, non-destructive supersede, replay-to-revision)

Replace destructive per-PRD re-parse with an amend-aware prd.revised event that SUPERSEDES (not deletes) prior requirement rows via the lineage columns the v7 migration pre-added, add a per-PRD revision counter derived from the log, enumerate all PRDs deterministically in serialize_state, and add replay-to-revision. This isolates the re-parse semantics change in its own phase with its own multi-revision replay coverage.

_Depends on features: F006._

**Requirements:** R011, R004

### F008: Phase 7 — Release/sync data plumbing (PRD release fields -> SyncMapping prd_id, per-PRD push)

Wire the release/milestone concept through sync as DATA only: PRD release fields (already in the model since Phase 0) flow to SyncMapping.prd_id/entity_kind, per-PRD push scoping in the CLI, and reconciliation attribution — with the network-touching GitHub milestone creation explicitly DEFERRED behind a capability flag.

_Depends on features: F006._

**Requirements:** R012

### F009: Phase 8 — Skills + docs + positioning reframe (PRD = release-scoped revisable plan)

Sweep skills and docs LAST, once the CLI surface they reference exists: reframe the PRD mental model (singleton -> release-scoped, separately-gated, revisable tranche), add per-PRD select/create + per-PRD gates + amend-aware revision to skills, update prd-template.md for .anvil/prds/<id>.md + Release field, sweep how-to docs for the path change, and complete the version-doc + release hygiene.

_Depends on features: F006, F007, F008._

**Requirements:** R013

## Tasks

### T001: Add PRDID + DEFAULT_PRD_ID, PRD identity/release fields, and prd_id partition field to models.py

**Feature:** F001
**Priority:** high
**Likely files:** bin/src/anvil/state/models.py, tests/test_models.py

Adding a REQUIRED prd_id to Task/Feature/Requirement could break existing constructors in tests/fixtures; mitigate by defaulting prd_id to DEFAULT_PRD_ID so old construction still validates.

**Acceptance criteria:**

- models.py defines PRDID: TypeAlias = str (near models.py:87) added to __all__, and DEFAULT_PRD_ID = 'default' module constant.
- PRD (models.py:442) gains id: PRDID, title: str='', target_version: str|None=None, target_tag: str|None=None, is_default: bool=False, created_at/updated_at: datetime with UTC validators.
- Requirement (models.py:468), Feature (models.py:480), Task (models.py:493) each gain prd_id: PRDID.
- Constructing each model with prd_id and a PRD with id validates.

**Verification:**

- `cd bin && uv run python -c "from anvil.state.models import PRD, Task, Feature, Requirement, PRDID, DEFAULT_PRD_ID; import datetime as d; t=d.datetime.now(d.timezone.utc); p=PRD(id=DEFAULT_PRD_ID, created_at=t, updated_at=t, target_version='v0.2.0'); print(p.id, p.target_version)"`
- `cd bin && uv run pytest -q ../tests/test_models.py`

### T002: Extend schema.py to SCHEMA_VERSION 7 with multi-row prds, prd_id columns, nullable revision lineage, and sync_mappings partition columns

**Feature:** F001
**Priority:** high
**Likely files:** bin/src/anvil/state/schema.py, tests/test_schema_version.py
**Dependencies:** T001

Bundling all future columns now (revision lineage, sync columns) avoids a second migration but means the DDL ships columns unused until later phases; acceptable and documented.

**Acceptance criteria:**

- SCHEMA_VERSION == 7 and PRAGMA user_version line == 7.
- prds DDL: (id) PK + title/target_version/target_tag/is_default/created_at/updated_at; ux_prds_default partial unique index.
- requirements/features/tasks have prd_id; requirements also have nullable revision_introduced/revision_superseded.
- sync_mappings have prd_id TEXT and entity_kind TEXT NOT NULL DEFAULT 'task'.
- Indexes idx_requirements_prd, idx_features_prd, idx_tasks_prd_status(prd_id,status), idx_requirements_prd_live present; v7 entry added to schema docstring history.

**Verification:**

- `cd bin && uv run python -c "from anvil.state.schema import SCHEMA_VERSION, DDL; assert SCHEMA_VERSION==7; assert 'is_default' in DDL and 'idx_tasks_prd_status' in DDL and 'entity_kind' in DDL and 'revision_superseded' in DDL; print('ok')"`
- `cd bin && uv run pytest -q ../tests/test_schema_version.py ../tests/test_version_sync.py`

### T003: Thread prd_id + release fields through payloads with DEFAULT_PRD_ID replay defaults

**Feature:** F001
**Priority:** high
**Likely files:** bin/src/anvil/state/payloads.py, tests/test_payloads.py
**Dependencies:** T001

Adding prd_id to NEW prd.parsed/task.created event JSON changes line bytes vs old goldens; any test pinning a NEW parse's exact bytes must be regenerated. Old logs stay byte-stable because the field defaults and is omitted on read.

**Acceptance criteria:**

- PrdParsedPayload/PrdReviewedPayload/PrdApprovedPayload/FeatureCreatedPayload/TaskCreatedPayload accept prd_id defaulting to DEFAULT_PRD_ID ('default'); PRD payloads also accept title/target_version/target_tag/is_default.
- A payload omitting prd_id validates with prd_id=='default' (replay equivalence for pre-v7 events).
- extra='forbid' retained on all payloads; new fields exported where applicable.

**Verification:**

- `cd bin && uv run python -c "from anvil.state.payloads import TaskCreatedPayload, PrdParsedPayload; assert TaskCreatedPayload(id='T001',feature_id='F001',title='x').prd_id=='default'; print('ok')"`
- `cd bin && uv run pytest -q ../tests/ -k payload`

### T004: Write the single v6->v7 in-place migration with 'default' PRD backfill (prds rebuild + entity/sync ALTERs)

**Feature:** F002
**Priority:** critical
**Likely files:** bin/src/anvil/state/sqlite.py, tests/test_sqlite.py
**Dependencies:** T002

SQLite cannot ALTER a PK so the prds rebuild must be inside the SAVEPOINT atomic with the additive entity/sync ALTERs; a torn rebuild must be idempotent. The 'default' literal MUST equal DEFAULT_PRD_ID or replay forks.

**Acceptance criteria:**

- prds is rebuilt via SAVEPOINT (CREATE prds_new (id) PK + new cols / INSERT-SELECT existing row as id='default' is_default=1 created_at=COALESCE(last_reviewed_at,project.created_at) / DROP / RENAME).
- ALTER requirements/features/tasks ADD COLUMN prd_id TEXT NOT NULL DEFAULT 'default' backfills every existing row in one statement; requirements also gain nullable revision_introduced/revision_superseded.
- ALTER sync_mappings ADD prd_id TEXT + entity_kind TEXT NOT NULL DEFAULT 'task'; prd_id backfilled by joining mapping.task_id -> task.prd_id (=='default').
- New indexes + ux_prds_default created IF NOT EXISTS; PRAGMA user_version becomes 7; row counts for tasks/features/requirements/sync_mappings identical before/after.

**Verification:**

- `cd bin && uv run pytest -q ../tests/test_sqlite.py -k 'migrat or v6 or v7'`
- `cd bin && uv run pytest -q -k 'migration and (sync or prd or default)'`

### T005: De-literalize the SCHEMA_VERSION migration gate into an ordered _MIGRATIONS ladder

**Feature:** F002
**Priority:** critical
**Likely files:** bin/src/anvil/state/sqlite.py, tests/test_sqlite.py
**Dependencies:** T004

Refactoring the migration dispatch risks regressing an old upgrade path; pin every existing vN->latest path with a test before refactoring.

**Acceptance criteria:**

- sqlite.py no longer contains a literal 'SCHEMA_VERSION == 6' comparison (the chain at sqlite.py:1277-1344 is replaced).
- _check_schema_version iterates an ordered _MIGRATIONS table while on_disk < SCHEMA_VERSION; all existing v0/1/2/3/4/5/6 -> latest paths still pass.
- Adding a hypothetical v8 step requires only appending one tuple (documented in a code comment).

**Verification:**

- `cd bin && grep -n 'SCHEMA_VERSION == 6' src/anvil/state/sqlite.py; test $? -eq 1`
- `cd bin && uv run pytest -q ../tests/test_sqlite.py`

### T006: Multi-PRD replay-equivalence oracle (SL-1) covering single-PRD pre-v7 logs and a directly-built 2-PRD DB

**Feature:** F002
**Priority:** critical
**Likely files:** tests/test_replay.py, bin/src/anvil/state/snapshot.py
**Dependencies:** T003, T004

serialize_state currently emits a singleton 'prd'; the 2-PRD byte-compare needs the multi-PRD serialization (Phase 6 task) — for THIS phase scope the oracle to single-PRD equivalence and add the 2-PRD assertion once serialize_state enumerates all prds. Sequence the 2-PRD half after the serialize_state change or land a minimal sorted-prds serialization here.

**Acceptance criteria:**

- A pre-v7 events.jsonl (no prd_id keys) replayed from empty on the v7 schema reconstructs every row under prd_id='default' identical to the migrated DB.
- A directly-built 2-PRD fixture: json.dumps(serialize_state(b), sort_keys=True) is byte-identical between the direct backend and a replay_from_empty backend.
- Test asserts row-count and the DEFAULT_PRD_ID constant is the single source used by both migration and payload defaults.

**Verification:**

- `cd bin && uv run pytest -q ../tests/ -k 'replay and equival'`
- `cd bin && uv run pytest -q ../tests/ -k 'replay and (multi_prd or default_prd)'`

### T007: Migration crash/idempotency + row-count invariant test

**Feature:** F002
**Priority:** critical
**Likely files:** tests/test_sqlite.py
**Dependencies:** T004

Simulating a mid-SAVEPOINT crash deterministically in SQLite is fiddly; may approximate by running individual migration steps and asserting IF NOT EXISTS / duplicate-column tolerance.

**Acceptance criteria:**

- A test runs the v7 migration, simulates a crash mid-migration (e.g. partial prds rebuild), re-opens, and asserts the migration completes cleanly.
- Row counts for tasks/features/requirements/sync_mappings are preserved across migrate; default PRD owns every row.
- Re-running migration on an already-v7 DB is a no-op.

**Verification:**

- `cd bin && uv run pytest -q ../tests/test_sqlite.py -k 'idempot or crash or rowcount'`

### T008: Add get_prd(prd_id)/list_prds()/default_prd_id() backend API + Protocol + row mappers

**Feature:** F003
**Priority:** high
**Likely files:** bin/src/anvil/state/sqlite.py, bin/src/anvil/state/backend.py, tests/test_sqlite.py
**Dependencies:** T004

get_prd() no-arg must keep returning the single existing PRD (now via is_default) for the 12 call sites; a missed ORDER BY / is_default filter returns an arbitrary PRD once multiple exist.

**Acceptance criteria:**

- backend.get_prd(prd_id: str|None=None): None resolves is_default=1, else WHERE project_id=? AND id=?; ORDER BY id where multiple could match.
- list_prds() returns all PRDs ordered by id; default_prd_id() returns the default id or None.
- backend.py Protocol declares the new signatures; _row_to_prd maps id/title/target_version/target_tag/is_default/created_at/updated_at.
- The 12 legacy no-arg get_prd() call sites still resolve unchanged for a single-PRD DB.

**Verification:**

- `cd bin && uv run python -c "import inspect; from anvil.state.sqlite import SqliteBackend as B; assert 'prd_id' in inspect.signature(B.get_prd).parameters; assert hasattr(B,'list_prds') and hasattr(B,'default_prd_id'); print('ok')"`
- `cd bin && uv run pytest -q ../tests/test_sqlite.py -k prd`

### T009: Add prd_id filters to list_tasks/list_features/list_requirements

**Feature:** F003
**Priority:** high
**Likely files:** bin/src/anvil/state/sqlite.py, bin/src/anvil/state/backend.py
**Dependencies:** T008

Low; additive optional kwarg. Ensure None means all-PRDs everywhere to avoid silent narrowing.

**Acceptance criteria:**

- list_tasks(prd_id=None) returns all PRDs' tasks (unchanged); list_tasks(prd_id=<id>) adds 'WHERE prd_id = ?'.
- Same for list_features and list_requirements.
- Existing call sites passing no prd_id behave byte-identically.

**Verification:**

- `cd bin && uv run python -c "import inspect; from anvil.state.sqlite import SqliteBackend as B; assert 'prd_id' in inspect.signature(B.list_tasks).parameters and 'prd_id' in inspect.signature(B.list_features).parameters; print('ok')"`
- `cd bin && uv run pytest -q ../tests/test_sqlite.py -k 'list and prd'`

### T010: Thread prd_id through write handlers + per-PRD scoped re-parse + denormalization invariant

**Feature:** F003
**Priority:** high
**Likely files:** bin/src/anvil/state/sqlite.py, tests/test_sqlite.py
**Dependencies:** T003, T009

Scoping DELETE FROM requirements to WHERE prd_id=? changes destructive-reparse semantics; the later revision phase depends on this seam. A single-PRD re-parse must still clear exactly its own ('default') requirements.

**Acceptance criteria:**

- _write_prd_parsed keys prds on (id), injects prd_id into requirement rows, and scopes DELETE FROM requirements to WHERE prd_id=? (sqlite.py:2159).
- _write_feature_created and _insert_task_row persist prd_id and assert Task.prd_id == owning Feature.prd_id.
- prd.reviewed/prd.approved UPDATEs scope to project_id AND id.
- Replaying a pre-v7 log reconstructs every row under prd_id='default' identical to a v6 run.

**Verification:**

- `cd bin && uv run pytest -q ../tests/test_sqlite.py -k 'replay or prd or task_created or feature_created'`
- `cd bin && grep -n 'DELETE FROM requirements WHERE' src/anvil/state/sqlite.py`

### T011: Per-PRD claim gate: resolve and check the task's owning PRD via task.prd_id

**Feature:** F004
**Priority:** critical
**Likely files:** bin/src/anvil/claims/manager.py, bin/src/anvil/state/sqlite.py, tests/test_claims.py
**Dependencies:** T010

If task.prd_id is somehow absent (mis-migrated), get_prd_for_task must fall back to default PRD to preserve single-PRD behavior rather than raising.

**Acceptance criteria:**

- backend.get_prd_for_task(task) resolves via task.prd_id -> get_prd(prd_id), falling back to the default PRD when prd_id is absent (reads task.prd_id directly, no Feature join).
- ClaimManager.claim() (manager.py:437) gates on get_prd_for_task(task) instead of get_prd().
- transitions._can_claim_task / task_ready_to_claimed signatures unchanged (still pure (task, prd)).
- A task in an approved PRD is claimable while a sibling in a draft PRD raises ClaimError gate_name prd_status_gate.

**Verification:**

- `cd bin && uv run pytest -q ../tests/test_claims.py ../tests/test_transitions.py`
- `cd bin && uv run pytest -q -k 'per_prd_gate or owning_prd'`

### T012: Collapse the duplicated MCP claim gate onto ClaimManager

**Feature:** F004
**Priority:** critical
**Likely files:** bin/src/anvil/mcp_server.py, tests/test_mcp_server.py
**Dependencies:** T011

Removing the early inline gate changes the error surface ordering; tests asserting the old pre-ClaimManager message must be updated in the same change.

**Acceptance criteria:**

- The inline prd-draft refusal block in mcp_server.claim_task (mcp_server.py:752-760) is removed.
- claim_task still raises ToolError when the task's OWNING PRD is draft (now via ClaimManager.claim -> ClaimError -> ToolError).
- Error message/code for the draft refusal is preserved or updated in lockstep with its test.

**Verification:**

- `cd bin && uv run pytest -q -k 'mcp and claim'`
- `cd bin && uv run pytest -q ../tests/test_mcp_server.py`

### T013: Pin cross-PRD coordination: conflict groups, active claims, and stale reaper span all PRDs

**Feature:** F004
**Priority:** critical
**Likely files:** tests/test_inference.py, tests/test_claims.py, bin/src/anvil/planning/inference.py
**Dependencies:** T011

This is the moat. The danger is a future --prd filter implemented as list_tasks(prd_id=) for the exclusion sets, hiding cross-PRD conflicts. These tests are the guardrail; they must land BEFORE the --prd filter.

**Acceptance criteria:**

- Test: infer_conflict_groups over the union of PRD-A and PRD-B tasks with one shared likely_file emits a single CG-... group containing both task ids.
- Test: with an active claim on a PRD-A task, next_claimable() (no --prd) does NOT return the colliding PRD-B task.
- Test: check_conflicts flags a PRD-B claim whose expected_files overlap an active PRD-A claim.
- Test: detect_and_release_stale reaps an expired claim regardless of which PRD owns its task.
- ConflictGroup carries NO prd_id and inference does NOT filter by prd_id.

**Verification:**

- `cd bin && uv run pytest -q ../tests/test_inference.py ../tests/test_claims.py`
- `cd bin && uv run pytest -q -k cross_prd`

### T014: v6->v7 gate-equivalence test: migrated single-PRD DB keeps identical claimability

**Feature:** F004
**Priority:** critical
**Likely files:** tests/test_claims.py, tests/test_replay.py
**Dependencies:** T011

Low; verification-only, but pins the backward-compat contract for the gate.

**Acceptance criteria:**

- Test: load a v6 single-PRD fixture, migrate to v7, assert get_prd_for_task(t)==default PRD for every pre-existing task.
- Test: a task claimable pre-migration is still claimable post-migration and vice-versa.
- Test: replay_from_empty over the original events.jsonl on v7 reconstructs the same task statuses and claims.

**Verification:**

- `cd bin && uv run pytest -q -k 'migration and (gate or claim)'`
- `cd bin && uv run pytest -q ../tests/test_replay.py`

### T015: Make parse_prd prd_id load-bearing with PRD-prefixed auto ids (bare for default) + Release field round-trip

**Feature:** F005
**Priority:** high
**Likely files:** bin/src/anvil/planning/template.py, tests/test_template.py, docs/prd-template.md
**Dependencies:** T010

Prefixed TaskID shape ('v0.2:T001') could break any '^T\d+' matcher in claims/skills/drift; keeping the default PRD's ids BARE limits blast radius to newly-named PRDs.

**Acceptance criteria:**

- parse_prd(markdown, prd_id='v0.2') yields ids prefixed 'v0.2:' (e.g. v0.2:T001); default prd_id yields BARE ids byte-identical to today; noqa: ARG001 removed from template.py:1071.
- Author-written prefixed ids parse without warnings; bare cross-refs resolve within the same PRD via a _normalize_id helper.
- A **Release:** line (or ## Release) round-trips into PRD.target_version/target_tag; absent => None.

**Verification:**

- `cd bin && uv run pytest -q ../tests/ -k template`
- `cd bin && uv run python -c "from anvil.planning.template import parse_prd; r=parse_prd('# Project: X\n## Summary\ns\n## Goals\n- g\n## Requirements\n- foo\n', prd_id='v0.2'); print(r.requirements[0].id)" | grep 'v0.2:R001'`

### T016: Per-PRD source files and --prd on prd parse

**Feature:** F005
**Priority:** high
**Likely files:** bin/src/anvil/cli/prd.py, bin/src/anvil/cli/_helpers.py, tests/test_cli_prd.py
**Dependencies:** T015

This is the first surface that can mint a 2nd PRD; it must land in the SAME phase as resolve_prd_id ambiguity handling (Phase 5) OR guard so a 2nd PRD cannot be created before disambiguation exists. Mitigate by sequencing the user-facing parse --prd command behind Phase 5's resolver, or accept that until Phase 5 only explicit --prd works (no bare-command ambiguity yet).

**Acceptance criteria:**

- prd_source_path(state_dir, prd_id) returns state_dir/'prds'/f'{prd_id}.md' for named PRDs and state_dir/'prd.md' for 'default'.
- `anvil prd parse --prd v0.2` reads .anvil/prds/v0.2.md and emits prd.parsed carrying prd_id='v0.2'; no flag reads .anvil/prd.md unchanged.
- Parsing one PRD leaves other PRDs' requirement rows untouched.
- Missing .anvil/prds/<id>.md exits 1 with an actionable message.

**Verification:**

- `cd bin && uv run pytest -q ../tests/ -k 'prd and parse'`
- `cd bin && uv run pytest -q ../tests/ -k 'prd_parsed and prd_id'`

### T017: Scope plan + orphan-prune to a single PRD while keeping conflict inference cross-PRD

**Feature:** F005
**Priority:** high
**Likely files:** bin/src/anvil/cli/plan.py, bin/src/anvil/planning/_plan_helpers.py, bin/src/anvil/planning/inference.py, tests/test_plan.py
**Dependencies:** T009, T016

plan needs TWO distinct lists: the --prd subset for prune/deps/draft, the UNION for conflict groups. Mixing scopes either fragments cross-PRD conflict detection (north-star violation) or prunes another PRD's tasks (data loss). classify_orphans signature stays pure; scoping is entirely at the call site.

**Acceptance criteria:**

- `anvil plan --prd v0.2` only emits task.deleted/feature.deleted for v0.2 orphans (caller passes list_tasks(prd_id=...)/list_features(prd_id=...) to classify_orphans); tasks in other PRDs never pruned.
- feature.created/task.created events carry prd_id; dependency inference + proposed->drafted promotion run over the --prd subset.
- Conflict-group computation reads backend.list_tasks() (ALL PRDs), not the subset — a PRD-A task and PRD-B task sharing a likely_file land in one CG-* group.
- plan with no --prd targets 'default' and matches pre-v7 behavior.

**Verification:**

- `cd bin && uv run pytest -q ../tests/ -k 'plan and (prune or orphan or prd)'`
- `cd bin && uv run pytest -q ../tests/ -k 'conflict and prd'`

### T018: Add shared resolve_prd_id helper + PRD_OPTION (CLI + MCP parity)

**Feature:** F006
**Priority:** high
**Likely files:** bin/src/anvil/cli/_helpers.py, bin/src/anvil/mcp_server.py, tests/test_cli_helpers.py
**Dependencies:** T008

MCP args have no Typer envvar binding; ANVIL_PRD must be read explicitly in _resolve_prd_id or CLI/MCP diverge. Cover with the parity test.

**Acceptance criteria:**

- cli/_helpers.py exports PRD_OPTION (typer.Option None, '--prd', envvar='ANVIL_PRD') and resolve_prd_id(backend, explicit) implementing explicit > ANVIL_PRD > (single|'default'|ambiguity-error).
- mcp_server.py exposes _resolve_prd_id(backend, prd_id) honoring ANVIL_PRD and raising ToolError on ambiguity/missing.
- Both resolvers return identical ids for identical DB+env inputs (parametrized parity test).
- Read-only rollup commands (status, get_project_status) never resolve a single PRD and never raise ambiguity.

**Verification:**

- `cd bin && uv run pytest -q -k 'resolve_prd_id or anvil_prd_env'`
- `cd bin && uv run python -c "from anvil.cli._helpers import resolve_prd_id, PRD_OPTION; print('ok')"`

### T019: Thread --prd/prd_id through prd parse/review, plan, score, list, show, packet, next (CLI + MCP)

**Feature:** F006
**Priority:** high
**Likely files:** bin/src/anvil/cli/prd.py, bin/src/anvil/cli/plan.py, bin/src/anvil/cli/claim.py, bin/src/anvil/mcp_server.py, bin/src/anvil/claims/manager.py
**Dependencies:** T013, T017, T018

The --prd filter on next MUST build exclusion sets from ALL PRDs then narrow; implementing as list_tasks(prd_id=) for exclusions silently breaks the moat. The Phase 3 regression test guards this. claim/release/renew do NOT get --prd (task id already names its PRD).

**Acceptance criteria:**

- prd parse/review, plan, score, list, show, packet, next gain --prd and pass the resolved prd_id to parse_prd/get_prd/list_tasks/next_claimable; the literal prd_id='prd' at prd.py and plan.py is replaced by the resolved id.
- MCP parse_prd/plan_tasks/review_prd/get_next_task/init_project gain optional prd_id; init_project returns prd_id (defaults 'default').
- next_claimable(prd_id=)/next_ready_excluding_active_files(prd_id=) build exclusion sets from ALL PRDs first, then narrow final candidates — `next --prd v0.1` skips a v0.1 task colliding with an active v0.2 claim.
- Single-PRD no-flag runs produce identical output to pre-change.

**Verification:**

- `cd bin && uv run pytest -q -k 'next and prd'; cd bin && uv run pytest -q ../tests/test_claims.py ../tests/test_mcp_server.py`
- `cd bin && uv run anvil next --help | grep -- --prd`

### T020: Per-PRD rollup for anvil status and get_project_status/get_project_summary

**Feature:** F006
**Priority:** high
**Likely files:** bin/src/anvil/cli/init_status.py, bin/src/anvil/mcp_server.py, tests/test_status.py
**Dependencies:** T008

--hook-format must keep ONE line for the SessionStart hook parser; pin prd-status to a single documented PRD. extra=forbid MCP clients asserting exact field sets see an additive prds[] (compliant clients fine).

**Acceptance criteria:**

- `anvil status` prints one block per PRD (id, status, counts, ready, active claims) plus a PROJECT TOTAL; single-PRD DB shows one block whose numbers equal existing totals.
- status --json adds data['prds'] while retaining flat project-total fields; --hook-format line shape unchanged (prd-status pinned to default/most-mature PRD).
- get_project_status/get_project_summary add prds: list[PrdStatusEntry] (additive, extra=forbid retained), flat fields kept as project total.

**Verification:**

- `cd bin && uv run pytest -q -k 'status_rollup or get_project_status or get_project_summary'`
- `cd bin && uv run anvil status --json | python -c "import sys,json; d=json.load(sys.stdin); assert 'prds' in d['data']; print('ok')"`

### T021: Backward-compat surface tests + get_prd() no-arg call-site audit

**Feature:** F006
**Priority:** high
**Likely files:** tests/test_cli.py, bin/src/anvil/mcp_server.py, bin/src/anvil/state/snapshot.py
**Dependencies:** T019, T020

A call site left on bare get_prd() silently operates only on the default PRD once multiple exist — a correctness trap. The audit must classify every one.

**Acceptance criteria:**

- A v7 single-PRD ('default') DB yields identical output for status/prd review/plan/next with no --prd vs the pre-change baseline (modulo additive prds[]).
- Ambiguity error fires only when >1 PRD and no --prd/ANVIL_PRD; the message lists available ids and the --prd syntax; ANVIL_PRD honored equally by CLI and MCP.
- All 12 legacy get_prd() call sites audited: each either passes an explicit prd_id or is documented as default-only-correct (mcp_server.py:561,752,1583,1796,2052; cli/plan.py:1229; cli/scan.py:174; cli/init_status.py:431; cli/prd.py:174,560; state/snapshot.py:84; claims/manager.py:437).

**Verification:**

- `cd bin && uv run pytest -q -k 'single_prd_backcompat or prd_ambiguity or anvil_prd_env'`
- `cd bin && grep -rn 'get_prd()' src/anvil/ | wc -l`

### T022: Add PRD.revision + Requirement lineage model fields + PrdRevisedPayload

**Feature:** F007
**Priority:** medium
**Likely files:** bin/src/anvil/state/models.py, bin/src/anvil/state/payloads.py, tests/test_payloads.py
**Dependencies:** T019

Lineage columns already exist in the v7 DDL (Phase 0); this task only adds the model/payload surface, so no second migration is needed.

**Acceptance criteria:**

- PRD gains revision: int=1; Requirement gains revision_introduced: int=1 and revision_superseded: int|None=None (back-compat defaults).
- New PrdRevisedPayload (extra='forbid') with project_id/prd_id/revision/scalar PRD fields and requirements_added/superseded/unchanged; exported in payloads.__all__.
- Constructing PRD()/Requirement() with no new fields still validates.

**Verification:**

- `cd bin && uv run pytest -q ../tests/ -k 'payload or model'`
- `cd bin && uv run python -c "from anvil.state.payloads import PrdRevisedPayload; PrdRevisedPayload(project_id='p',prd_id='default',revision=2)"`

### T023: prd.revised handler: amend-aware non-destructive supersede + per-PRD revision counter

**Feature:** F007
**Priority:** medium
**Likely files:** bin/src/anvil/state/sqlite.py, tests/test_sqlite.py
**Dependencies:** T022

Under order-tolerant git replay two concurrent prd.revised for one prd_id could both claim revision N; replay is checks-free so a deterministic last-writer rule (by lamport,ts,id) must govern supersede marking to avoid double-marking.

**Acceptance criteria:**

- Register 'prd.revised': ActionSpec(PrdRevisedPayload, _check_prd_revised, _write_prd_revised) alongside prd.parsed.
- _check_prd_revised rejects revision != current+1; _write_prd_revised UPDATEs prds scalars, bumps revision, sets revision_superseded=new rev on superseded rows, inserts added rows with revision_introduced=new rev — NEVER DELETE FROM requirements.
- Live set for a PRD = rows WHERE prd_id=? AND revision_superseded IS NULL; get_prd uses ORDER BY id.
- Replaying [parsed, revised] reconstructs the live set == direct-run with superseded rows present and correct lineage.

**Verification:**

- `cd bin && uv run pytest -q ../tests/ -k 'prd and (revis or parse or review or approve)'`
- `cd bin && uv run pytest -q ../tests/ -k replay`

### T024: serialize_state: enumerate all prds + partitioned requirement rows deterministically

**Feature:** F007
**Priority:** medium
**Likely files:** bin/src/anvil/state/snapshot.py, tests/test_replay.py
**Dependencies:** T023

A missed sort key lets replay silently diverge on multi-PRD/multi-revision DBs. This also completes the 2-PRD half of the Phase 1 replay oracle. Changing the serialize_state key from 'prd' to 'prds' may break any consumer reading the singleton key — audit consumers.

**Acceptance criteria:**

- serialize_state emits a 'prds' list sorted by id (each carrying id/revision/status) replacing the singleton 'prd' key, and 'requirements' sorted by (prd_id, revision_introduced, id) including superseded rows.
- list_requirements returns all lineage-bearing rows.
- json.dumps(serialize_state(b), sort_keys=True) byte-identical between directly-built and replayed backend for a 2-PRD multi-revision fixture.
- Existing single-PRD fixtures still serialize (default PRD is the sole 'prds' entry).

**Verification:**

- `cd bin && uv run pytest -q ../tests/ -k 'serialize or snapshot or equivalence'`
- `cd bin && uv run pytest -q ../tests/ -k 'replay and equival'`

### T025: replay-to-revision: bounded replay_to_event_id + CLI re-parse emits prd.revised

**Feature:** F007
**Priority:** medium
**Likely files:** bin/src/anvil/state/sqlite.py, bin/src/anvil/cli/prd.py, tests/test_replay.py
**Dependencies:** T024

Torn-trailing-line tolerance and local/git mode behavior must match replay_from_empty exactly or the bounded variant diverges.

**Acceptance criteria:**

- replay_to_event_id(events_path, stop_after_event_id) reconstructs the DB as of that event reusing _apply_write_only; for [parsed(rev1), revised(rev2)] stopping after rev1 yields the rev1 live set, after rev2 the rev2 set; read-only, no new log.
- CLI: first parse of a prd_id emits prd.parsed; re-parse of an existing prd_id emits prd.revised with a diff against current live rows.
- A v6 DB upgraded to v7 then re-parsed produces a revision-2 default PRD with prior requirements superseded, not deleted (audit log shows prd.revised, not a wipe).

**Verification:**

- `cd bin && uv run pytest -q ../tests/ -k 'replay and (revision or bounded or to_event)'`
- `cd bin && uv run pytest -q ../tests/ -k 'cli and prd and revis'`

### T026: SyncMapping prd_id/entity_kind model + validator + replay-equivalent upserted payload

**Feature:** F008
**Priority:** medium
**Likely files:** bin/src/anvil/state/models.py, bin/src/anvil/state/payloads.py, bin/src/anvil/sync/, tests/test_sync.py
**Dependencies:** T006, T008

Overloading task_id on prd-kind rows: nullable task_id + the validator prevent get_sync_mapping/list_sync_mappings from surfacing a milestone row as a task mapping. Columns/migration already landed in Phase 1.

**Acceptance criteria:**

- SyncMapping gains prd_id: str|None=None and entity_kind: Literal['task','prd']='task' with a model_validator that prd-kind rows carry prd_id and a null task_id.
- sync_mapping.upserted payload gains optional prd_id/entity_kind; replay of an old event lacking them yields entity_kind='task' and prd_id='default'.
- Replaying a pre-change events.jsonl reconstructs the same sync_mappings table as the in-place migration.

**Verification:**

- `cd bin && uv run pytest -q -k 'sync_mapping or sync and (push or replay or mapping)'`
- `cd bin && uv run python -c "from anvil.state.models import SyncMapping; import datetime as d; m=SyncMapping(task_id='t1', external_system='github_issues', external_id='1', last_synced_at=d.datetime.now(d.timezone.utc)); assert m.entity_kind=='task'; print('ok')"`

### T027: Per-PRD sync push scoping in the CLI + prd_id stamped on mappings

**Feature:** F008
**Priority:** medium
**Likely files:** bin/src/anvil/cli/sync.py, bin/src/anvil/sync/, tests/test_sync.py
**Dependencies:** T026

Cross-PRD push ordering with deferred milestones is two round-trips per PRD; for the data-only phase there is no milestone, so push is single round-trip per task as today.

**Acceptance criteria:**

- _push_one_task/_persist_mapping_from_push write the task's owning prd_id onto the upserted SyncMapping.
- `anvil sync provider <id> --prd <prd_id> --push` pushes only tasks owned by that PRD; default (no --prd) iterates all PRDs writing correct prd_id.
- A repo with two PRDs produces task mappings whose prd_id matches the owning PRD (no cross-contamination).

**Verification:**

- `cd bin && uv run pytest -q -k 'sync and (prd or scope or dispatch or push)'`
- `cd bin && uv run anvil sync --help`

### T028: Attribute reconciliation sync discrepancies to a PRD

**Feature:** F008
**Priority:** medium
**Likely files:** bin/src/anvil/sync/reconciliation.py, tests/test_reconciliation.py
**Dependencies:** T026

missing_sync_mapping iterates done tasks with no PRD filter; once tasks carry prd_id, decide whether sync scope follows the per-PRD status gate (open decision) — for now keep sync independent and only attribute prd_id.

**Acceptance criteria:**

- missing_sync_mapping and drift_sync_state Discrepancy.payload gain prd_id (from the task/mapping's owning PRD).
- list_sync_mappings yields entity_kind='prd' rows but drift_sync_state skips/special-handles them (no task-shaped drift for a milestone).
- Existing reconciliation tests pass; new tests assert prd_id appears in payloads.

**Verification:**

- `cd bin && uv run pytest -q -k 'recon or drift or missing_sync'`

### T029: DEFERRED placeholder: ensure_release_group Protocol + GitHub milestone client (tracked, not implemented this release)

**Feature:** F008
**Priority:** medium
**Likely files:** docs/backlog/anvil-backlog.md, docs/roadmap.md
**Dependencies:** T027

Documentation-only; ensures the deferred scope is tracked rather than lost.

**Acceptance criteria:**

- A backlog/tech-debt entry records the deferred ensure_release_group(release_tag,target_version,prd_summary,mapping)->ExternalRef Protocol method (optional, capability-gated via supports_milestones), create_milestone/list_milestones GitHub clients, issue-to-milestone assignment, sync.milestone.ensured event + prd-kind mapping persistence, and missing_milestone_mapping reconciliation kind.
- No Protocol/client code lands this release; existing providers unchanged.

**Verification:**

- `cd bin && uv run pytest -q ../tests/test_sync.py`

### T030: Reframe PRD mental model in architecture.md, _positioning.md, roadmap.md, prd-template.md

**Feature:** F009
**Priority:** medium
**Likely files:** docs/architecture.md, docs/_positioning.md, docs/roadmap.md, docs/prd-template.md
**Dependencies:** T016

Docs can land early in parallel as they describe the target; pinned here so they match shipped command names. Token budget not affected by docs (only skills).

**Acceptance criteria:**

- architecture.md mental model + data-model table + storage layout describe a project holding several release-scoped PRDs in one state.db/events.jsonl; prd_status_gate keys on the task's OWNING PRD.
- _positioning.md Terraform analogy frames each PRD as a scoped stack/workspace within the one canonical state; PRD defined consistently as 'a release/milestone-scoped, separately-gated, revisable plan carrying a target version/tag'.
- roadmap.md gains a 'Multi-PRD (release-scoped plans)' theme naming the v6->v7 migration (default PRD owns all rows, zero data loss) + per-PRD gating + replay-equivalence.
- prd-template.md documents .anvil/prds/<id>.md, the **Release:** field + storage, per-PRD re-parse semantics, and the single-PRD -> .anvil/prds/default.md migration note.

**Verification:**

- `grep -niE 'release-scoped|several PRDs|one state.db' docs/architecture.md docs/_positioning.md`
- `grep -n '.anvil/prds/' docs/prd-template.md; cd bin && uv run pytest -q ../tests/test_token_budget.py`

### T031: Rewrite skills (prd, start-prd, plan, claim) for per-PRD select/create, per-PRD gates, amend-aware revision, cross-PRD conflict messaging

**Feature:** F009
**Priority:** medium
**Likely files:** skills/prd/SKILL.md, skills/start-prd/SKILL.md, skills/plan/SKILL.md, skills/claim/SKILL.md
**Dependencies:** T019, T030

Token budget regression: add ONE short Step-0 paragraph + a shared one-liner, not a new always-on section; extract prose to references/ (roadmap P11-SK-C1) if bodies grow. Auto-select the lone PRD so single-PRD users get no new prompt.

**Acceptance criteria:**

- skills/prd: concise 'Step 0 — Select or create the PRD' runs `anvil prd list`, prompts only when >1 PRD exists (auto-selects lone PRD silently); overwrite + approval gates described as per-PRD; 'Iterating' reframed to amend-aware diffable replayable revision.
- skills/start-prd: interview gains one Release/milestone question feeding the Release field + prd_id; writes .anvil/prds/<id>.md (path resolved from CLI, not hardcoded); overwrite check targets the per-PRD file.
- skills/plan: scopes plan/prune to the selected PRD; read-only note that conflict groups span ALL PRDs (next will not route two file-overlapping tasks across PRDs).
- skills/claim: prerequisite states a ready task is claimable iff its OWNING PRD is reviewed/approved; file-overlap/conflict-group checks span all active claims regardless of PRD.
- Token budget test passes (frontmatter combined <=1000, single-skill <=6000, bodies combined <=40000).

**Verification:**

- `grep -niE 'prd list|per-PRD|owning PRD|across PRDs|release' skills/prd/SKILL.md skills/plan/SKILL.md skills/claim/SKILL.md skills/start-prd/SKILL.md`
- `cd bin && uv run pytest -q ../tests/test_token_budget.py`

### T032: Sweep how-to docs + version lockstep + release hygiene for the v7 schema bump

**Feature:** F009
**Priority:** medium
**Likely files:** docs/how-to/getting-started.md, docs/how-to/authoring-a-prd.md, CHANGELOG.md, .claude-plugin/plugin.json, bin/pyproject.toml, bin/src/anvil/__init__.py
**Dependencies:** T031

The SCHEMA_VERSION bump shipped in Phase 0 is publishable; the version lockstep must land before publish or test_version_sync.py fails. Easy to miss the user-facing version docs (not test-enforced).

**Acceptance criteria:**

- Every hardcoded .anvil/prd.md in getting-started.md/authoring-a-prd.md is updated or annotated with the per-PRD path + a single-PRD compat note (file now at .anvil/prds/default.md, `anvil prd list` shows one default PRD); the 're-parse replaces all rows' note scoped to one PRD.
- Version bumped in lockstep across .claude-plugin/plugin.json, bin/pyproject.toml, bin/src/anvil/__init__.py + packaging manifests; CHANGELOG entry added; README badge + getting-started/cli-reference/architecture version examples refreshed.
- test_version_sync.py and test_install_manifests.py pass.

**Verification:**

- `grep -rn '.anvil/prds/' docs/how-to/; grep -rn 'prds/default.md' docs/how-to/`
- `cd bin && uv run pytest -q ../tests/test_version_sync.py ../tests/test_install_manifests.py`

