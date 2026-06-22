# Spec — Multi-PRD, Revisable, Partitioned in One `state.db`

> **Status:** draft design (2026-06-22). Target: anvil **v0.3**. Companion backlog:
> [`docs/backlog/multi-prd-revisable.prd.md`](../backlog/multi-prd-revisable.prd.md)
> (a parseable anvil PRD — 13 requirements, 9 features, 32 tasks).
> Authored from a 7-facet code-grounded design pass + integration; raw facet
> designs in `codebase-review/north-star-spec/raw/` (not committed).

## North star (one sentence)

> **Many PRDs coexist in one `state.db`, each independently revisable, where a
> PRD is a release/milestone-scoped plan** — partitioned by `prd_id`, gated
> per-PRD, but coordinated globally (one workspace, one event log, one replay).

## 1. Why — the two axes

"Multiple PRDs" and "revise the PRD" are **two orthogonal axes**, and naming
them is the design:

| | Lifecycle: frozen | Lifecycle: revisable |
|---|---|---|
| **Cardinality: one PRD** | today's model | one evolving spec with history |
| **Cardinality: many PRDs** | parallel release/initiative specs | **the target** |

Concretely: a repo wants to run a **0.2.0 hardening release** while a **feature
initiative** is still mid-flight. Today anvil can't — `PRD` is a singleton and
`anvil prd parse` is a destructive whole-DB replace. The fix is to make the PRD
a first-class, identified, release-scoped, revisable object, of which a project
holds several.

## 2. Current model (verified)

- **`PRD` is a literal singleton with no `id`.** `models.py:442` — fields are
  status/summary/goals/…; no identity. `get_prd()` (`backend.py:198`,
  `sqlite.py:949`) does `SELECT * FROM prds … fetchone()`. The table is named
  `prds` but the PK is `project_id` and only one row is ever written.
- **No partition key.** `Requirement.prd_section` (`models.py:474`) is a section
  *name* string; `Feature`→tasks/requirements by id-list; `Task.feature_id` is
  the only parent link. Nothing carries `prd_id`.
- **The claim gate keys on the one PRD.** `transitions.py:121`
  `_can_claim_task(task, prd)` → `prd.status in {reviewed, approved}`
  (`gate_name='prd_status_gate'`); resolved from `get_prd()` at
  `claims/manager.py:437`. 12 `get_prd()` call sites total.
- **Schema at v6**; migration ladder gated on a literal `SCHEMA_VERSION == 6`
  (`sqlite.py:1277-1350`). Replay (`sqlite.py:601`) rebuilds via `initialize()`
  at the current DDL then applies events via `_write_*` only.
- **Workspace = one per canonical repo root** (`_workspace_key`,
  `_home_workspace_base` in `cli/_helpers.py`). `ANVIL_ROOT` /
  `ANVIL_STATE_LAYOUT=local` / `--cwd` already select a different state dir.

**Rejected alternative — multiple workspaces per repo.** It needs no schema
change, but it *fragments the audit log* (one `events.jsonl` per workspace) and
*breaks anvil's core promise*: two agents in two workspaces editing the same file
can't see each other. Coordination must span the repo, so multi-PRD lives in
**one** `state.db`.

## 3. Target model

### 3.1 Data model & schema (v6 → v7)

- **PRD gains identity + release fields.** `PRD.id: PRDID` (new flat `str`
  alias), `title`, `target_version: str|None`, `target_tag: str|None`,
  `is_default: bool`, `revision: int = 1`, `created_at`/`updated_at` (UTC).
  `target_tag` *is* the milestone title used by sync.
- **Partition every entity.** `Requirement`/`Feature`/`Task` each gain
  `prd_id: PRDID`. **Decision:** store `prd_id` *explicitly* on `Task` (not only
  derived via `Feature`) so the cross-PRD conflict join and the per-PRD claim
  gate are single-column lookups; the redundancy is a write-time invariant
  `Task.prd_id == owning Feature.prd_id`.
- **`prds` holds many rows.** **Decision (resolved):** one Project per repo →
  single-column `id` PK; `prd_id` on entity tables is a **plain indexed column
  (no hard FK)** enforced in the write handler + the moat tests (matches today's
  FK-light `requirements` table; simplest migration + replay). A partial unique
  index `ux_prds_default ON prds WHERE is_default = 1` enforces exactly one
  default PRD. (SQLite can't `ALTER` a PK, so the v7 migration rebuilds `prds`
  via `CREATE/INSERT-SELECT/DROP/RENAME` inside a `SAVEPOINT`.)
- **`DEFAULT_PRD_ID = 'default'`** — one module constant used identically by the
  migration backfill, every payload `prd_id` default, `resolve_prd_id`, and the
  `get_prd_for_task` fallback. (A second literal would fork replay byte-equality.)
- **ID namespace.** `TaskID`/`FeatureID`/`RequirementID` stay flat global `str`.
  The **default PRD emits BARE ids** (`T001`) → zero id churn on migration and
  every existing claim/evidence/dependency ref keeps resolving. **Named PRDs emit
  PREFIXED ids** (`v0.2:T001`) → the global namespace stays collision-free without
  a composite key. `prd_id` is the partition/filter column; the prefixed id is the
  human identifier.
- **De-literalize the migration gate** into an ordered `_MIGRATIONS` ladder
  applied while `on_disk < SCHEMA_VERSION` (fixes the standing "next bump silently
  deletes upgrade paths" footgun).

### 3.2 Migration & backward compatibility (the load-bearing phase)

One atomic v6→v7 migration, idempotent and crash-safe:

1. Rebuild `prds` (SAVEPOINT) → INSERT the existing single row as `id='default'`,
   `is_default=1`.
2. `ALTER … ADD COLUMN prd_id TEXT NOT NULL DEFAULT 'default'` on
   requirements/features/tasks — backfills **every** existing row to `'default'`
   in one statement (zero data loss). Requirements also gain *nullable*
   `revision_introduced`/`revision_superseded` (used in Phase 6; added now to
   avoid a second migration). `sync_mappings` gain `prd_id` + `entity_kind`.
3. Create indexes + `ux_prds_default` `IF NOT EXISTS`; `PRAGMA user_version = 7`.

**Invariant:** task/feature/requirement row counts identical before/after.
**Replay equivalence:** pre-v7 events omit `prd_id`; payload defaults backfill
them to `'default'`, so `replay_from_empty` on the v7 schema reconstructs the
migrated DB byte-identically. A 2-PRD fixture replays byte-identically too
(the replay-equivalence oracle, SL-1, extended for multi-PRD).

### 3.3 Per-PRD claim gate + the cross-PRD coordination *moat*

- **Per-PRD gate.** `get_prd_for_task(task)` reads `task.prd_id` →
  `get_prd(prd_id)` (no `Feature` join), falling back to the default PRD if
  absent. `ClaimManager.claim()` gates on *that* PRD. So PRD `v0.1` (approved) is
  claimable while `v0.2` (draft) is not. `transitions._can_claim_task` keeps its
  pure `(task, prd)` signature. The duplicated inline MCP gate
  (`mcp_server.py:752-760`) collapses onto `ClaimManager` (preserving error text).
- **Coordination stays global — this is the moat.** `ConflictGroup` carries **no**
  `prd_id`; `infer_conflict_groups`, active-claim exclusion, and the stale reaper
  scan **all** tasks regardless of PRD. A `--prd` filter narrows only the *final*
  candidate list, **after** building exclusion sets from every PRD. Pinned by
  regression tests that **must land before** any `--prd` narrowing exists
  (Phase 3 precedes Phase 5), so a future filter can't silently hide a cross-PRD
  conflict.

### 3.4 Event-sourced revision (Phase 6)

- First parse of a `prd_id` → `prd.parsed` (revision 1). Re-parse of an existing
  `prd_id` → **`prd.revised`** (prev+1) carrying a **full self-describing diff**
  (added / superseded / unchanged requirement sets).
- **Revision → status (resolved):** `prd.revised` demotes an *approved* PRD back
  to `draft` (re-review) **iff** it supersedes/removes an existing requirement;
  a **pure-additive** revision keeps the current status. Deterministic — keys on
  the `requirements_superseded` set in the diff payload. The gate is claim-time
  only, so demotion pauses *new* claims on that PRD but does not disturb in-flight
  work, and is per-PRD. `replay_to_event_id` reconstructs **PRD + requirements**
  as-of a revision (features/tasks are plan-generated, not revision-versioned).
- `_write_prd_revised` **never** `DELETE FROM requirements` — it marks
  `revision_superseded = new_rev` on prior rows and inserts added rows with
  `revision_introduced = new_rev`. Live set = `WHERE prd_id=? AND
  revision_superseded IS NULL`.
- `serialize_state` emits a `prds` list sorted by id and `requirements` sorted by
  `(prd_id, revision_introduced, id)` *including* superseded rows — so multi-PRD,
  multi-revision DBs replay deterministically.
- `replay_to_event_id(events_path, stop_after)` reconstructs the DB **as of** a
  revision — diffable, replayable PRD history, falling straight out of the
  existing append-only log. (anvil is uniquely positioned for this; it's on-brand
  with the replay guarantee.)

### 3.5 CLI / MCP surface (backward-compatible)

- One shared `resolve_prd_id(backend, explicit)`: **explicit `--prd` > `ANVIL_PRD`
  env > (single PRD | `'default'` when zero | ambiguity-error when >1)**. CLI and
  MCP resolvers return identical ids (parity test).
- `--prd`/`prd_id` threads through `prd parse/review`, `plan`, `score`, `list`,
  `show`, `packet`, `next` (CLI) and `parse_prd`/`plan_tasks`/`review_prd`/
  `get_next_task`/`init_project` (MCP). `claim`/`release`/`renew` do **not** take
  `--prd` — the task id already names its PRD.
- **`anvil next` with no `--prd` scans ALL approved PRDs** (resolved) — a
  cross-PRD ready queue (still excluding cross-PRD conflicts + active claims), so
  one fleet works many releases. `--prd` narrows to one release.
- **Named-PRD ids render verbatim** (`v0.2:T001`) in `list`/`show`/packets
  (resolved); the default PRD shows bare `T001`.
- **Read-only rollups default to all PRDs and never raise ambiguity.**
  `anvil status` prints one block per PRD + a project total; `--json` adds
  `prds[]`; the `--hook-format` single line is unchanged (pinned to the
  default/most-mature PRD so the SessionStart hook parser is unaffected).
- **The default PRD's source stays at `.anvil/prd.md`** (resolved — no shim, no
  file migration); named PRDs live at `.anvil/prds/<id>.md`. The default is
  **sticky**: the migrated/first PRD stays `is_default` and `parse` never re-picks
  it (a dedicated re-default command is deferred).

### 3.6 Release / sync (data plumbing only this release)

- `SyncMapping` gains `prd_id` + `entity_kind: Literal['task','prd']` (validator:
  prd-kind rows carry `prd_id`, null `task_id`). Push stamps the owning `prd_id`;
  `anvil sync … --prd <id> --push` scopes to one release; reconciliation
  discrepancies gain `prd_id`.
- **Sync is independent of the per-PRD status gate by default** (resolved) — so
  you can seed issues for a draft release. A config knob
  `sync.gate_by_prd_status` (default off) restricts pushing to reviewed/approved
  PRDs. **`PRD.target_tag` is unique per project** (resolved), enforced by a
  validation check → a clean 1:1 PRD↔release/milestone mapping.
- **Deferred (tracked, not built):** a **provider-neutral**
  `ensure_release_group(...)` Protocol method (resolved name — covers GitHub
  Milestone / Linear Cycle / Jira FixVersion) + the GitHub client,
  issue→group assignment, and `sync.release_group.ensured` event — behind a
  `supports_release_groups` capability flag.

## 4. Phasing

Ordered so the suite and the replay guarantee stay green after **every** phase,
and so backward-compat for existing single-PRD DBs is established **before** any
new surface exists. (Full task list with acceptance criteria + verification
commands in the companion backlog PRD.)

| Phase | Title | Why here |
|---|---|---|
| **0** | Model + schema + payloads (no behavior change) | Foundation: `PRDID`, `prd_id` fields, v7 DDL (incl. nullable revision + sync columns), payload defaults |
| **1** | v6→v7 migration + default-PRD backfill + de-literalized ladder + replay oracle | The backward-compat keystone; every later phase assumes a PRD exists |
| **2** | Backend partition API (`get_prd(prd_id)`, `list_prds`, filters, scoped writes) | The seam every surface consumes; zero user-visible change |
| **3** | Per-PRD claim gate + **cross-PRD moat (pinned)** | Safety-critical guarantee locked **before** any `--prd` narrowing |
| **4** | Parser `prd_id` load-bearing + per-PRD `plan`/prune | First phase that can mint a 2nd PRD — after the gate + moat are proven |
| **5** | CLI/MCP `--prd` surface, `resolve_prd_id`, ambiguity, per-PRD status rollup | Makes multi-PRD usable end-to-end with disambiguation in place |
| **6** | Event-sourced revision (`prd.revised`, supersede, replay-to-revision) | Isolated: changes re-parse semantics, needs its own multi-revision coverage |
| **7** | Release/sync data plumbing (`SyncMapping.prd_id`, per-PRD push) | Milestone network wiring explicitly deferred |
| **8** | Skills + docs + positioning reframe + version lockstep | Last, so docs describe commands that actually exist |

## 5. Decisions locked

- `prd_id` explicit on Task/Feature/Requirement (denormalized; write-time invariant).
- `DEFAULT_PRD_ID = 'default'`, one module constant.
- PRD field is `id`; partition column is `prd_id`; `PRDID` is a flat `str` alias.
- `prds` **single-col `id` PK** (one Project per repo) + partial-unique default index; rebuilt in the migration. `prd_id` is a plain indexed column, no hard FK.
- Named PRDs use **human-chosen** ids (e.g. `v0.2`), typically the `.anvil/prds/<id>.md` stem.
- Default PRD emits **bare** ids; named PRDs emit **prefixed** ids.
- **One** v6→v7 migration adds *all* columns every facet needs (no second migration).
- Re-parse is destructive-per-PRD in Phases 1–5; **non-destructive supersede** lands in Phase 6.
- Cross-PRD coordination stays global by construction; pinned by a regression test.
- The v7 bump is a **publishable** change → full version-lockstep + manifests + version docs.

## 6. Decisions resolved (2026-06-22)

The 12 open decisions were driven as Q&A with the author and resolved as follows
(also recorded in the backlog PRD's `## Decisions` section):

1. **PRD-table key & FK** → one Project per repo; **single-column `id` PK**;
   `prd_id` is a plain indexed column, **no hard FK** (invariant enforced in the
   write handler + moat tests). *(merges the original #1 project-count and #2 FK.)*
2. **`anvil next` (no `--prd`)** → scans **all approved PRDs** (cross-PRD ready
   queue), excluding cross-PRD conflicts + active claims.
3. **ID display** → **verbatim/prefixed** (`v0.2:T001`); default PRD stays bare.
4. **Default PRD source** → stays at **`.anvil/prd.md`** (no shim/migration);
   named PRDs at `.anvil/prds/<id>.md`.
5. **Revise → status** → demote approved → `draft` **iff a requirement is
   superseded/removed**; pure-additive revisions keep status (deterministic via
   the diff's `superseded` set).
6. **`prd.revised` payload** → **full self-describing diff** (added / superseded /
   unchanged).
7. **replay-to-revision** → reconstructs **PRD + requirements only**.
8. **`is_default`** → **sticky** (first/migrated PRD stays default; `parse` never
   re-picks it; a re-default command is deferred).
9. **Sync gating** → **independent** of the per-PRD gate by default; config knob
   `sync.gate_by_prd_status` (default off) to restrict to reviewed/approved.
10. **`target_tag`** → **unique per project**, validated (1:1 PRD↔release).
11. **Deferred method name** → **`ensure_release_group`** (provider-neutral).

## 7. What this means for the product

- **Mental-model shift:** PRD goes from *the* singleton config to *a* release-scoped,
  separately-gated, revisable plan; a project holds several. Positioning moves from
  "one project = one PRD = one plan" (Terraform: one state/project) toward "one
  project state holds many concurrent, versioned plans" (closer to a Linear/Jira
  project with multiple releases).
- **The moat sharpens:** "run N releases in parallel on one repo and anvil still
  stops two agents clobbering the same file, with a replayable history of every
  scope change" is differentiation single-doc tools (CCPM, GH issues) can't match.
- **Releases/milestones become first-class** — per-release readiness, per-release
  claim gates, a clean sync mapping.
- **On-brand with the audit guarantee:** PRD revisions as events = auditable,
  diffable, replayable specs — the same promise the engine already sells, extended
  to the requirements layer.

## 8. Success criteria

1. A v6 DB opens on v7 with one `is_default` `'default'` PRD owning all rows; row
   counts unchanged; migration idempotent + crash-safe.
2. Replay-equivalence holds for both a pre-v7 single-PRD log and a 2-PRD
   multi-revision fixture.
3. An approved PRD's task is claimable while a draft PRD's task is refused at the gate.
4. Two file-overlapping tasks in different PRDs land in one conflict group and are
   never co-routed by `next`.
5. Single-PRD, no-`--prd` CLI/MCP output is byte-identical to v0.2 (modulo additive `prds[]`).
6. `cd bin && uv run pytest -q` green at every phase boundary; `ruff`/`mypy` clean
   (once Phase-3 of the v0.2 review's CI hardening lands).
