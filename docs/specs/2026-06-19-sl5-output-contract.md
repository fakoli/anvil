# SL-5 — Contract-level conflict with after-the-fact reconciliation

**Date:** 2026-06-19
**Status:** Draft — needs approval before implementation
**Plugin:** `anvil`
**Tracks:** roadmap integrity-track `SL-5` (Wave 3: "earn the reframe")
**Depends on:** SL-3 (`DiffProof` — the typed diff this spec's drift check compares against). SL-3 must ship first.
**Breaking:** YES (model + conflict-detection semantics change). `Task` gains an `OutputContract`; `ConflictGroup` is keyed on contract overlap, not `expected_files`/`likely_files` overlap. Schema version bumps `6 → 7` (after SL-3 takes `6`).

---

## 1. Goal

Lift conflict detection out of the advisory-only class. Today a `ConflictGroup`
is *"a named set of tasks whose `expected_files` overlap"*
(`bin/src/anvil/state/models.py:618-629`), computed by file-set intersection in
`infer_conflict_groups` (`bin/src/anvil/planning/inference.py:175-263`). Two
problems:

1. **False conflicts.** Two tasks that both touch `models.py` are grouped even if
   one adds a class and the other edits an unrelated function — they could run in
   parallel safely, but the file-overlap heuristic forbids it.
2. **It depends on an honest up-front declaration with no after-check.** This is
   the *same gameability class* the old substring gate was in (the roadmap calls
   this out explicitly, `docs/roadmap.md:143-148`): a task declares its files,
   the engine trusts the declaration, and nothing ever compares the declaration
   to what the task *actually did*.

SL-5 adds a typed `OutputContract` to `Task` (the symbols, modules, endpoints,
and tables a task promises to produce/own), keys `ConflictGroup` on **contract**
overlap, and adds a **post-`apply` drift check** that compares the declared
contract against the task's actual `DiffProof` (from SL-3), firing a
`contract.drift_detected` event when they diverge. The reconciliation step is
what makes the declaration *checkable* rather than merely *trusted*.

## 2. Context & root cause

### How conflict detection works today

- `Task.conflict_groups: list[str]` (`models.py:362`) and `likely_files:
  list[str]` (`models.py:367`) hold the declaration.
- `Claim.expected_files: list[str]` (`models.py:390`) holds the per-claim
  declaration.
- `infer_conflict_groups(tasks)` (`inference.py:175-263`) intersects each pair's
  file sets (`_files_set`, `inference.py:199-201`); a non-empty, non-subset
  overlap creates `ConflictGroup(id="CG-<sorted-task-ids>", ...,
  reason="Tasks A and B share overlapping files: ...")` (`inference.py:236-247`).
- Groups persist via `conflict_group.upserted` events (`mcp_server.py:2145-2156`,
  `ConflictGroupUpsertedPayload` at `payloads.py:220`) into the
  `conflict_groups` table (`schema.py:219-224`).
- At claim time, `ClaimManager._check_group_conflicts` warns (does not block)
  when a task shares a `conflict_group` with an active claim
  (`conflicts.py:11-13` doc; enforcement seam at `sqlite.py:3032`).
- `anvil conflicts` (`cli/conflicts.py:54-139`) reads them back.

### Root cause

The unit of conflict is the **file**, and the file declaration is **never
reconciled against reality**. A file is too coarse (whole-file overlap ≠ real
conflict) and too trusting (no after-check). The correct unit is the *contract*:
the named outputs a task owns. Two tasks editing the same file but owning
disjoint symbols do not conflict; a task that declares it owns symbol `foo` but
its actual diff touches `bar` has drifted from its contract — and SL-3's
`DiffProof` (`files_changed`, `diff_sha256`) is exactly the observed artifact to
check against.

## 3. Proposed design

### 3.1 The `OutputContract` model (`state/models.py`)

Mirror the embedded-value-object style (`Score`, `Verification`:
`models.py:249-270`): Pydantic `BaseModel`, `_MODEL_CONFIG`
(`models.py:225-229`).

```python
class OutputContract(BaseModel):
    """What a Task promises to produce / own. The unit of conflict detection
    and the target of the post-apply drift check (SL-5)."""
    model_config = _MODEL_CONFIG
    symbols:   list[str] = Field(default_factory=list)  # "module.path:func_or_class"
    modules:   list[str] = Field(default_factory=list)  # importable module paths / file paths
    endpoints: list[str] = Field(default_factory=list)  # "METHOD /route/path"
    tables:    list[str] = Field(default_factory=list)  # DB table / migration names

    def overlaps(self, other: OutputContract) -> set[str]:
        """Return the set of contract items this contract shares with other."""
        return (
            (set(self.symbols)   & set(other.symbols))
            | (set(self.modules) & set(other.modules))
            | (set(self.endpoints) & set(other.endpoints))
            | (set(self.tables) & set(other.tables))
        )
```

`Task` (`models.py:346-375`) gains:

```python
    output_contract: OutputContract = Field(default_factory=OutputContract)
```

`default_factory=OutputContract` (an empty contract) means every pre-SL-5 task
deserialises with an empty contract — full backward compatibility, exactly the
pattern `task_type: TaskType = TaskType.feature` used (`models.py:357-360`).

### 3.2 `ConflictGroup` keyed on contract overlap

Update the `ConflictGroup` docstring and `infer_conflict_groups`. The model
fields (`id`, `name`, `task_ids`, `reason` — `models.py:618-629`) are unchanged;
only the *meaning* and the *grouping key* change:

```python
class ConflictGroup(BaseModel):
    """A named set of tasks whose OutputContracts overlap (SL-5).

    Two tasks conflict when they declare ownership of the same contract item
    (symbol, module, endpoint, or table) — NOT merely when they touch the same
    file. Claiming one while another is active is allowed but warned."""
```

`infer_conflict_groups` (`inference.py:175-263`) is rewritten to intersect
`OutputContract`s instead of file sets:

- Replace `file_sets[t.id] = _files_set(t)` (`inference.py:199-201`) with the
  task's `output_contract`.
- Replace `overlap = set_a & set_b` (`inference.py:226`) with
  `overlap = contract_a.overlaps(contract_b)`.
- The strict-subset "this is a dependency, not a conflict" rule
  (`inference.py:230-233`) is **dropped for contracts**: contract ownership is not
  subset-structured the way file scope was; any shared contract item is a real
  conflict regardless of the rest of each contract.
- `reason` becomes `"Tasks A and B both declare contract items: <sorted
  overlap>"`.

### 3.3 Back-compat with file-overlap (the fallback)

A task that declares an **empty** `output_contract` (every pre-SL-5 task, and any
task a planner has not yet given a contract) must not silently lose conflict
protection. `infer_conflict_groups` therefore runs a two-tier rule per pair:

1. If **both** tasks have a non-empty `output_contract`, group on contract
   overlap (the new, precise rule).
2. If **either** task has an empty `output_contract`, fall back to the legacy
   `likely_files` overlap rule (`_files_set`, the current `inference.py` logic),
   so an un-migrated task is never *less* protected than today.

This makes the upgrade non-regressive: a project with zero declared contracts
behaves byte-identically to today (all conflicts via file overlap); a project
where the planner has filled in contracts gets the precise contract-overlap
grouping. The two-tier rule is documented in the function docstring and is the
explicit acceptance test "two tasks touch the same file but declare
non-overlapping contracts and run in parallel" (§4).

### 3.4 Post-`apply` drift check + reconciliation (`cli/packet_apply.py`)

After a successful `apply --approve` transitions a task to `accepted`/`done`
(`cli/packet_apply.py`, the `task.applied` path; `TaskAppliedPayload` at
`payloads.py:327-335`), run a reconciliation step:

1. Load the task's `output_contract` and the accepted evidence's `DiffProof`
   (SL-3 — `Evidence.proofs`, the `DiffProof.files_changed`).
2. Compute drift via a new pure function in `review/gates.py`:

```python
@dataclass(frozen=True)
class ContractDrift:
    task_id: str
    declared_modules: list[str]      # contract.modules
    actual_files: list[str]          # diff_proof.files_changed
    undeclared_files: list[str]      # touched but not in contract.modules
    unfulfilled_modules: list[str]   # declared but not touched

def contract_drift(task: Task, diff_proof: DiffProof) -> ContractDrift | None:
    """Pure. None when the declared contract.modules exactly covers the diff's
    files_changed; otherwise a ContractDrift describing the divergence."""
```

Module-level granularity is the practical drift check (symbols/endpoints/tables
require language-aware diff parsing — deferred, §10). `contract.modules` lists
the file paths the task promised to change; `DiffProof.files_changed` lists what
it actually changed.

3. If `contract_drift` returns a non-`None` result, append a
   `contract.drift_detected` event (additive, replayable):

```python
class ContractDriftDetectedPayload(BaseModel):       # state/payloads.py
    model_config = ConfigDict(extra="forbid")
    task_id: str
    declared_modules: list[str]
    actual_files: list[str]
    undeclared_files: list[str]
    unfulfilled_modules: list[str]
    detected_at: str                                  # ISO 8601 UTC
```

The drift event is **advisory by default but recorded**: it does not block the
`apply` (the task is already accepted on its evidence gate), but it lands in
`events.jsonl` as a permanent, queryable fact. A config flag
`contract_drift_enforcing` (mirroring the existing advisory/enforcing evidence
gate toggle at `config.py:217-231`) can promote it to a hard gate that blocks
`apply --approve` until the contract is reconciled. `anvil conflicts` (or a new
`anvil drift` view, reusing `cli/drift.py`'s existing structure) surfaces tasks
with recorded drift.

### 3.5 Storage

- `tasks` table: `output_contract TEXT NOT NULL DEFAULT '{}'` JSON column
  (`schema.py:110-128`), exactly like the `verification`/`scores` JSON columns.
- `TaskCreatedPayload` / `TaskScoredPayload` (`payloads.py:115, 145`) carry the
  contract through the create/score path so a replanned task's contract is a
  logged fact.
- `contract.drift_detected` is a new event action; no new table (it is read by
  scanning events, like other audit-style facts).

## 4. Acceptance

1. `OutputContract` exists; `Task.output_contract` is added with
   `default_factory=OutputContract`.
2. `ConflictGroup` is keyed on contract overlap when both tasks declare
   contracts; falls back to `likely_files` overlap when either is empty.
3. **Parallel-safe test:** two tasks whose `likely_files` both contain
   `models.py` but whose `output_contract.symbols`/`modules` are disjoint produce
   **no** `ConflictGroup` and can both be claimed concurrently without a warning.
4. **Drift test:** a task whose `output_contract.modules = ["a.py"]` but whose
   accepted `DiffProof.files_changed = ["a.py", "b.py"]` produces a
   `contract.drift_detected` event with `undeclared_files = ["b.py"]`.
5. **Back-compat test:** a project with zero declared contracts produces the
   identical `ConflictGroup` set it produced before SL-5 (file-overlap path).
6. `contract.drift_detected` replays deterministically (P4).
7. Schema version `6 → 7`; `migrations.md` documents the v6→v7 auto-upgrade.
8. `plugins/anvil` version bumped; `registry/` regenerated.

## 5. Migration

Schema: bump `SCHEMA_VERSION = 7` (`schema.py:39`); add `tasks.output_contract`
to the DDL; add a `v6 → v7` branch in `_check_schema_version`
(`sqlite.py:1195-1307`) calling a new `_ensure_output_contract_column` helper
shaped exactly like `_ensure_task_type_column` (`sqlite.py:1322-1339`):
duplicate-column-tolerant `ALTER TABLE tasks ADD COLUMN output_contract TEXT NOT
NULL DEFAULT '{}'`. Chain it into every existing upgrade branch (now `→ 7`).
Purely additive — the `'{}'` default backfills every existing task to an empty
contract, which the §3.3 two-tier rule treats exactly like a pre-SL-5 task.

Data: **no data migration is required.** Empty contracts are the correct legacy
meaning; the file-overlap fallback preserves old behaviour. A planner populates
contracts going forward via `anvil plan` (the planner agent infers a task's
`output_contract` from its acceptance criteria / likely_files; that inference is
itself an SL-6-adjacent planner improvement, out of scope here).

## 6. Backward-compat / replay implications (fakoli-style P4)

- `Task.output_contract` defaults to an empty `OutputContract`, so a
  `task.created` payload from a pre-SL-5 log deserialises cleanly (the new field
  is absent → default applied). No lenient validator needed beyond the default.
- `contract.drift_detected` is purely additive — appending it never invalidates a
  prior event, and `replay_from_empty` re-applies it as a no-op projection write
  (it is an audit fact; the drift view derives from the event stream).
- Regenerate the replay golden to include one task with a non-empty
  `output_contract` and one `contract.drift_detected` event;
  `test_replay_equivalence` stays green (same P4 discipline as SL1-RR-1 §8C and
  SL-3 §6).

## 7. Risks

- **Contract quality depends on the planner.** A precise `output_contract`
  requires the planner to know a task's symbols/modules. Mitigation: the
  file-overlap fallback (§3.3) means a bad/empty contract degrades gracefully to
  today's behaviour, never worse.
- **Module-only drift granularity.** Symbol/endpoint/table drift is not checked
  (only `modules` vs `files_changed`). This is honest scoping — file-level drift
  is the high-value, language-agnostic check; finer granularity needs AST/route
  parsing (§10).
- **Advisory drift may be ignored.** A recorded-but-non-blocking drift event can
  be overlooked. Mitigation: the `contract_drift_enforcing` config toggle and the
  `anvil drift`/`conflicts` surface make it visible and optionally blocking.
- **Dropping the subset rule** (§3.2) could over-group contracts that nest. In
  practice contract items are flat ownership claims, not nested scopes, so any
  shared item is a genuine co-ownership conflict — the subset rule was a
  file-scope artifact.

## 8. Implementation steps

1. Add `OutputContract` (with `overlaps`) to `state/models.py`; add
   `Task.output_contract`; export from `__all__`; update the `ConflictGroup`
   docstring.
2. Bump `SCHEMA_VERSION = 7`; add `tasks.output_contract` DDL;
   `_ensure_output_contract_column` + `v6→v7` branch; chain into prior branches.
3. Thread `output_contract` through `TaskCreatedPayload` / `TaskScoredPayload`
   and the task INSERT/SELECT in `sqlite.py`.
4. Rewrite `infer_conflict_groups` to the two-tier (contract-then-file) rule;
   update `reason` text.
5. Add `contract_drift` + `ContractDrift` to `review/gates.py` (pure).
6. Add `ContractDriftDetectedPayload`; register the `contract.drift_detected`
   action (`_check_*`/`_write_*` per SL1-RR-1's dispatch contract).
7. Wire the post-`apply` reconciliation into `cli/packet_apply.py`'s
   `task.applied` path; add the `contract_drift_enforcing` config flag.
8. Surface drift in `anvil drift` / `anvil conflicts`.
9. Regenerate the replay golden; bump version; regen `registry/`.

## 9. Test plan

| Test | Asserts |
|---|---|
| **Parallel-safe** | Same `likely_files`, disjoint contracts → no `ConflictGroup`; both claimable concurrently (no warning) |
| **Contract conflict** | Same `output_contract.symbols` item → one `ConflictGroup` with a contract-item `reason` |
| **File fallback** | Both tasks empty contract, overlapping `likely_files` → identical grouping to pre-SL-5 (golden) |
| **Mixed tier** | One empty + one populated contract, overlapping files → file-overlap fallback fires |
| **Drift fires** | Declared `modules=["a.py"]`, actual `files_changed=["a.py","b.py"]` → `contract.drift_detected` with `undeclared_files=["b.py"]` |
| **No drift** | Declared exactly covers diff → `contract_drift` returns `None`, no event |
| **Enforcing toggle** | `contract_drift_enforcing=true` blocks `apply --approve` on drift; default advisory does not |
| **Replay (P4)** | Golden with a non-empty contract + a drift event replays byte-equal |
| **Schema upgrade** | v6 db auto-upgrades to v7 with `output_contract` defaulting `'{}'` |

CI: full suite via `.github/workflows/anvil.yml`.

## 10. Out of scope

- Symbol/endpoint/table-level drift parsing (needs AST + route + migration
  introspection); SL-5 checks module/file-level drift only.
- Planner inference of `output_contract` from acceptance criteria (an SL-6-class
  planning improvement).
- Cross-project / distributed contract coordination.
- Auto-remediation of drift (reconciliation **records**; the human decides — same
  posture as `state-keeper` drift reporting).

## 11. References

- `bin/src/anvil/state/models.py:346-375` (`Task`), `:362` (`conflict_groups`),
  `:367` (`likely_files`), `:390` (`Claim.expected_files`), `:618-629`
  (`ConflictGroup`)
- `bin/src/anvil/planning/inference.py:175-263` (`infer_conflict_groups`,
  `_files_set`)
- `bin/src/anvil/cli/conflicts.py:54-139`; `bin/src/anvil/cli/drift.py`
- `bin/src/anvil/state/payloads.py:115, 145, 220, 327`
  (`TaskCreated`/`TaskScored`/`ConflictGroupUpserted`/`TaskApplied`)
- `bin/src/anvil/state/schema.py:39, 110-128, 219-224`;
  `bin/src/anvil/state/sqlite.py:1195-1339` (migration pattern),
  `:3032` (group-conflict claim seam)
- `bin/src/anvil/config.py:217-231` (advisory/enforcing gate-toggle precedent)
- `docs/specs/2026-06-19-sl3-proofartifact.md` (`DiffProof`, `Evidence.proofs`)
