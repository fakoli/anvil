# fakoli-state — Tech Debt Backlog

Items deferred from PR-level critic + Greptile reviews. Each entry links the originating PR + finding so the rationale survives. Ordered by priority within section.

**Status legend**: `OPEN` = unaddressed; `TARGETED-PN` = scheduled for Phase N; `DONE` = closed in commit; `MOVED-P9-BACKLOG` = forward-carried into [`phase-9-backlog.md`](phase-9-backlog.md) for v2.x tracking; `MOVED-P11-BACKLOG` = consolidated under a Phase 11 audit finding in [`phase-11-backlog.md`](phase-11-backlog.md).

> **Phase 9 (v1.9.0) status, 2026-05-25.** PR #49 (Phase 8) deferred a
> handful of items that PR #50 (Phase 9) closed: the audit-honesty
> `sync.pull.completed`-vs-`deferred` confusion, the `local_moved`-only
> `in_sync` bug-collapse, and the Phase 7 C2/C3/C4 leftovers. Items
> below carrying `DONE (Phase 9)` were closed in that release. Items
> tagged `MOVED-P9-BACKLOG` are not abandoned — they have moved to
> `phase-9-backlog.md` where they sit alongside the v2.x roadmap
> (Linear/Monday/Jira providers, webhook sync, immediate-apply
> `*_applied` variants).
>
> **Backlog-hygiene pass, 2026-05-26 (post-v1.10.0).** Audited the 18
> remaining `OPEN` items against the current code in `main`. Found that
> CL-1, CL-3, CL-8, CL-11, CL-13, and PS-1 were silently closed during
> the PR #49 / v1.7.1 welder backlog wave (those closures were recorded
> in `CHANGELOG.md` § 1.7.1 but the per-item `Status` lines here still
> said `OPEN`). Updated to `DONE (v1.7.1)` with commit-trail cross-refs.
> Verified the remaining 12 items are still genuine debt — see the
> `**Status**: OPEN` lines that survive this pass. No item was moved to
> `phase-11-backlog.md`; the closest cross-ref is CL-10 ↔ P11-HK-N3
> (capture-evidence pattern set), but those are independent findings
> on the same file rather than the same fix — both stay where they are.

---

## SL-1 (replay integrity) follow-ups

### SL1-RR-1 · A poison canonical line aborts a full replay

**From**: SL-1 Wave 3 critic, surfaced by the replay-equivalence fixture work. **Status**: DONE (branch `feat/fakoli-state-sl1-rr-1-event-sourcing`).

The fix went beyond the original Option A (append-JSONL-only-after-COMMIT). It adopted the **full event-sourced write path**: a decide/apply split (`_check_*` / `_write_*` per action), `append(EventDraft) -> Event | None` as the sole production write entry point, log-as-id-authority via `flock` (closing the PR #41 Critic-3 cross-process id-collision race), append-only `events.jsonl` with a sibling `audit.jsonl` for rejections and idempotent no-ops, and strict no-skip-list replay via `_write_*` only. The design also closed the inverse post-COMMIT audit gap (crash between COMMIT and JSONL write). `apply_event`, `next_event_id`, and `PENDING_EVENT_ID` were removed. Tracked in fakoli-style principle **P4** `open_work` — that open work is now resolved.

---

## Phase 8 / Phase 9 closures (sync + LLM cleanups)

These items came out of PR #49 (Phase 8) critic + Greptile reviews and the
PR #45/47 Phase 7 deferrals; PR #50 (Phase 9, v1.9.0) closed them.

### P9-1 · Audit-event honesty — `sync.pull.completed` emitted on deferred branches

**From**: PR #49 critic CONSIDER #1. **Status**: DONE (Phase 9 T5 — `feat/fakoli-state-phase-9`).

v1.8.0 emitted `sync.pull.completed` for six conflict-resolution branches that did NOT actually mutate local state (`local_wins_deferred`, `remote_wins_deferred`, `prompt_defaulted_to_local`, `prompt_chose_local`, `prompt_chose_remote`, `prompt_skipped`). The JSONL was lying about what happened.

**Fix**: those branches now emit `sync.pull.deferred` (truthful) with the same resolution token in the payload. `sync.pull.completed` is reserved for the four honest cases enumerated in `SyncPullCompletedPayload`'s docstring (clean pull, tombstone, in_sync no-divergence, local-moved-only with paired `sync.push.deferred` hint). 5 new tests in `tests/test_cli_sync.py::TestDeferredConflictBranchesEmitPullDeferred`.

---

### P9-2 · `local_moved`-only path collapsed `sync_state` to `in_sync` instead of `local_ahead`

**From**: PR #49 critic CONSIDER #2. **Status**: DONE (Phase 9 T5).

When the local Task had moved ahead of `last_synced_at` and the remote had not changed, the engine used to set `sync_state="in_sync"` (wrong — the local was clearly ahead). The wrong state meant `fakoli-state sync` (reconciliation) could not surface the task as needing a push.

**Fix**: the branch now sets `sync_state="local_ahead"` and emits a `sync.push.deferred` audit event with `resolution="local_moved_no_push"` so operators can grep `events.jsonl` for tasks awaiting a follow-up `--push`. 2 new tests in `tests/test_cli_sync.py::TestLocalMovedOnlyEmitsLocalAhead`.

---

### P9-3 · `SyncAuditPayload` was a single all-optional model — accepted nonsense payloads

**From**: PR #49 critic + Phase 9 T3 plan. **Status**: DONE (Phase 9 T3).

v1.8.0's `SyncAuditPayload` declared every field as `str | None = None`, so a `sync.batch.completed` event with `strategy="foo"` validated fine (the `strategy` field belongs to `sync.conflict_detected` only). Field-vs-action mismatches were silently accepted.

**Fix**: replaced with a Pydantic v2 discriminated union — one concrete subclass per `sync.*` action, `extra="forbid"` on each, dispatched O(1) on the `action` literal. `ACTION_TO_PAYLOAD` exported for the SQLite dispatcher. Backwards-compatible: the `SyncAuditPayload` name still exists as a module-level type-form (`Annotated[Union[...], Field(discriminator="action")]`). Callers that used `SyncAuditPayload.model_validate(d)` directly migrate to `TypeAdapter(SyncAuditPayload).validate_python(d)` or look up the concrete subclass via `ACTION_TO_PAYLOAD[action]`.

---

### P9-4 · `RecordedLLMProvider.record_key` ignored `max_tokens` / `temperature`

**From**: Phase 7 C2 deferral. **Status**: DONE (Phase 9 T6).

v1.7.0's recorded-provider key was `sha256(system + "\n---\n" + user)` — two recordings produced under different tuning args silently collided. Tests that pre-computed keys against the wrong constant would unknowingly mask real engine drift.

**Fix**: extended signature to `record_key(system, user, *, max_tokens=4096, temperature=0.0)`; canonical hash now folds in `str(int(max_tokens))` and `repr(float(temperature))` as length-prefixed chunks 3 and 4. `repr(float(...))` is the spec-conformant round-trip encoding so `0`, `0.0`, `0.00` all collapse to the same key. 4 new tests in `tests/test_llm.py::TestRecordedLLMProviderKey`; collateral updates to 8 call sites in `tests/test_llm_integration.py` + 1 in `tests/test_cli.py` to pass the correct per-call-site constant (`_SCORE_EXPLAIN_MAX_TOKENS=300`, `_DESCRIPTION_ENRICH_MAX_TOKENS=400`, `_EXPAND_MAX_TOKENS=2000`).

---

### P9-5 · Brainstorm-flow bridge used fuzzy detection

**From**: Phase 7 C3 deferral. **Status**: DONE (Phase 9 T6).

`skills/brainstorm/SKILL.md` had fuzzy prose ("if fakoli-flow seems available") for detecting whether to bridge to `/fakoli-flow:brainstorm`. The detection was non-deterministic across sessions.

**Fix**: explicit `claude plugin list 2>/dev/null | grep -q "^fakoli-flow"` shell check with exit-code-driven branching. Slash-command name corrected to the fully-qualified `/fakoli-flow:brainstorm` (the old `/flow:brainstorm` was a typo that would have broken the bridge invocation when fakoli-flow IS installed). Detection is OPTIONAL — exit non-zero (or missing `claude` binary) falls through to the local interview.

---

### P9-6 · `expand --use-llm` had no `--format prd` UX

**From**: Phase 7 C4 deferral. **Status**: DONE (Phase 9 T6).

`fakoli-state expand T012 --use-llm` printed human-readable per-subtask blocks that the user had to manually translate into PRD `### TXxx` markdown before `prd parse`. The translation step was lossy and error-prone.

**Fix**: added `--format {text,prd}` Typer flag. `--format prd` emits ready-to-paste markdown blocks matching `docs/prd-template.md`'s `## Tasks` schema. `**Feature:**` and `**Priority:**` fields are populated from the parent task's metadata (critic CONSIDER fix — eliminates the manual-edit step). 11 new tests in `tests/test_cli_plan.py` covering both formats + validation + help-text.

---

### P9-7 · Multi-provider config — no way to opt out of every sync provider

**From**: Phase 9 T5 plan. **Status**: DONE (Phase 9 T5).

v1.8.0 had no config knob for narrowing or opting out of the sync provider iteration; the engine always iterated `sorted(PROVIDER_REGISTRY)`.

**Fix**: optional top-level `sync.providers` config key with three-way semantics: absent = registry fallback (v1.8.0 default), explicit list = use it, empty list = opt out entirely. `Config.sync_providers: tuple[str, ...] | None` pins both behaviours; 7 new tests in `tests/test_config.py::TestSyncProvidersConfig`. Documented in `docs/sync-providers.md` § "Per-provider configuration (v1.9.0)".

---

### P9-8 · Two new plugin-owned doc agents — marketplace-scribe + docs-scribe

**From**: User directive in Phase 9 plan T4. **Status**: DONE (Phase 9 T4).

The plugin needed agents specifically for its own documentation maintenance so doc drift could be addressed without pulling in the marketplace-wide `fakoli-crew:keeper` for plugin-internal work.

**Fix**: created `agents/marketplace-scribe.md` (cyan, opus — owns `.claude-plugin/marketplace.json`, root README plugins table, `registry/*.json`) and `agents/docs-scribe.md` (purple, opus — owns plugin `docs/`, `CHANGELOG.md`, `plugin.json.description`). Both defer outward to crew when crew is installed. Color collisions checked vs the existing four agents (planner=white, critic=magenta, sentinel=gray, state-keeper=teal).

---

## Phase 6 Must-Close (Backend Protocol coherence + concurrency)

These three items MUST land in Phase 6 because the MCP server inherits all of them.

### P6-1 · Backend Protocol gaps — three `backend._conn` reach-throughs in cli.py

**From**: PR #41 Critic-2 (architecture). **Status**: DONE (PR #44, feat/fakoli-state-phase-6-prep).

Three CLI callers bypass the Backend Protocol via `backend._conn`:
- `_fetch_recent_events` (cli.py:1388) — used by `show TASK_ID`
- `packet` feature lookup (cli.py:1773) — reads features by positional `row[4]`, fragile to schema changes
- `_fetch_latest_evidence` (cli.py:2195) — used by `apply`

The MCP server will need all three queries. Without Protocol methods, the MCP impl will inherit the same reach-through pattern and the abstraction is dead by construction.

**Fix**: extend `Backend` Protocol with `get_feature(feature_id)`, `list_events(target_id, target_kind, limit)`, `get_latest_evidence(task_id)`. Implement in `SqliteBackend`. Eliminate every `backend._conn` access in `cli.py`.

---

### P6-2 · `next_event_id` race — read-before-lock allows event drop

**From**: PR #41 Critic-3. **Status**: DONE (PR #44, via PENDING_EVENT_ID sentinel pattern).

`next_event_id` is `SELECT MAX(id)` with no lock. Two concurrent processes (CLI + MCP server is the first realistic scenario) can both observe MAX=N, both attempt `INSERT E{N+1}`, and the second's `INSERT OR IGNORE` silently no-ops — event survives in JSONL but missing from SQLite events table. Replay then produces a diverging DB.

**Attempted fix** in PR #41: switch to UUID-based IDs. **Reverted** because:
- Schema CHECK constraint `id GLOB 'E[0-9]*'` rejects hex chars
- ~60 tests hardcode `E000001`/`E000002` sequential expectations

**Proper fix for Phase 6**: generate the ID INSIDE `apply_event`'s `BEGIN IMMEDIATE` transaction. Callers pass `event_without_id` (or a `partial_event` shape); `apply_event` assigns ID inside the lock. Update the schema CHECK constraint if needed (or stay sequential — the inside-lock generation makes sequential safe).

Single-CLI usage is race-free today. The MCP server in Phase 6 is the trigger for actually fixing this.

---

### P6-3 · `TaskStatus.stale` is structurally unreachable

**From**: PR #41 Critic-2. **Status**: DONE (Option A — feat/fakoli-state-phase-6-prep).

`_handle_claim_stale` transitions the task directly from `claimed/in_progress/blocked` → `ready`, bypassing `TaskStatus.stale` entirely. Option A (delete the dead code) was executed:
- `TaskStatus.stale` removed from `models.py` enum
- `task_to_stale()`, `task_stale_to_ready()`, and `_claim_expired()` removed from `transitions.py` and `__all__`
- `stale_count` removed from the `status` command output (`cli/init_status.py`)
- Task lifecycle diagram updated in `docs/specs/2026-05-24-fakoli-state-v0.md`
- Related tests in `test_models.py` and `test_transitions.py` updated
- `ClaimStatus.stale` is intentionally preserved — claims CAN be stale; tasks cannot.

---

## Phase 6 Should-Close (CLI organization + dispatch consistency)

### P6-4 · `cli.py` is 2,499 lines — split into per-command modules

**From**: PR #41 Critic-2. **Status**: DONE (PR #44 — split into 8-module cli/ package).

The file is past the tipping point for a single module. By Phase 8 with `sync`, `replay`, and MCP wiring added, this becomes 4,000+ lines.

**Suggested split** (natural boundaries already visible in the code):
```
cli/
├── __init__.py          # assembles sub-apps; ~60 lines
├── _helpers.py          # _open_backend, _resolve_state_dir, _next_event_id, _reap_stale_claims, _get_project_id
├── init.py              # init, status
├── prd.py               # prd parse, prd review
├── plan.py              # plan, score, expand, review tasks, list, show
├── claim.py             # claim, release, renew, next
├── packet_apply.py      # packet, submit, apply
├── hooks.py             # hook check-claim, hook record-file-change, hook capture-evidence
└── conflicts.py         # conflicts (Phase 6+)
```

Zero runtime risk; pure refactor; do it BEFORE Phase 6 adds MCP wiring.

---

### P6-5 · Event handler dispatch + payload validation centralization

**From**: PR #41 Critic-2. **Status**: DONE (PR #44 — 17 per-action Pydantic payload models + dict dispatch).

`_apply_mutation` has a 17-handler `elif` chain. Each handler signature differs (some take `event_id`, some take `timestamp`, some take neither). Each does ad-hoc `payload.get(...)` validation.

**Fix**: per-action Pydantic payload models (`PrdParsedPayload`, `EvidenceSubmittedPayload`, etc.) validated once before routing. Removes duplicated checks; Phase 8 GitHub-sync event payloads become trivial to add.

---

## Cleanup (any phase; small surface)

### CL-1 · check-claim.sh ignores its own CLI subcommand

**From**: PR #41 Critic-2. **Status**: DONE (v1.7.1 — `hooks/check-claim.sh` now invokes `fakoli-state hook check-claim --file --actor` (the Phase 5 per-file subcommand); coarse status-parse fallback fires only when the CLI is unavailable).

Phase 4 added `cli.py:hook_check_claim` with full per-file `expected_files` checking. `check-claim.sh` was not updated to call it — still uses the Phase 4 coarse "any active claim → warn" approach. Per-file warning logic in CLI is dead from the hook's perspective.

**Fix**: replace count-based logic in `check-claim.sh` with `"$CLI" hook check-claim --file "$FILE_PATH" --actor "$ACTOR"`. Fall through to coarse check only when CLI unavailable.

---

### CL-2 · `--commands` / `--files-changed` comma-split corrupts embedded commas

**From**: PR #41 Critic-1. **Status**: OPEN.

`cli.py:1926`: `commands.split(",")` mangles `pytest --runxfail,foo.py` into `["pytest --runxfail", "foo.py"]`. File paths with commas (legal on macOS/Linux) corrupt similarly.

**Fix**: accept the flags multiple times (`--command CMD` repeatable) instead of comma-splitting. Update execute SKILL.md doc example.

---

### CL-3 · `_reap_stale_claims` swallows `SchemaMismatch`

**From**: PR #41 Critic-3. **Status**: DONE (v1.7.1 — `cli/_helpers.py::_reap_stale_claims` now re-raises `SchemaMismatch` and narrows the swallow to `(StateLocked, TransactionAborted)`).

`cli.py:1413-1427`: bare `except Exception: pass` swallows schema mismatches. A user with an outdated DB sees a confusing secondary error from their primary command instead of the clean SchemaMismatch.

**Fix**: catch and re-raise `SchemaMismatch`; swallow only operational errors.

---

### CL-4 · ConflictGroup records never persisted

**From**: PR #41 Critic-3. **Status**: OPEN.

`infer_all()` produces ConflictGroup records. `plan` counts and prints them. But nothing writes them to the `conflict_groups` table — the table is always empty. The future `conflicts` CLI command will return empty.

**Fix**: in `plan`, emit a `conflict_group.created` event per group; add handler.

---

### CL-5 · `conflicts` command referenced in docstring but not implemented

**From**: PR #41 Critic-3. **Status**: OPEN.

`cli.py:22` module docstring lists `conflicts` as Phase 5. The `@app.command` registration is missing. `fakoli-state --help` lies.

**Fix**: implement the command (depends on CL-4 for actual data).

---

### CL-6 · `fakoli-state evidence attach` references → already replaced

**From**: PR #41 Critic-2. **Status**: DONE (PR #41 fixup commit).

---

### CL-7 · `agents/critic.md` + `agents/sentinel.md` color collisions with fakoli-crew

**From**: PR #41 Critic-1. **Status**: DONE (this PR — state/critic purple → magenta; state/sentinel cyan → gray).

`fakoli-state/agents/critic.md` uses `color: purple` — same as `fakoli-crew:keeper`. `sentinel.md` uses `color: cyan` — same as `fakoli-crew:scout`. When both plugins are installed (the documented expected configuration), the agent picker shows two purple agents and two cyan agents with no visual distinction.

**Fix**: assign distinct unused colors (e.g., `orange` for critic, `yellow` for sentinel).

---

### CL-8 · Double-submit with different evidence_id inserts duplicate row

**From**: PR #41 Critic-1. **Status**: DONE (v1.7.1 — `_handle_evidence_submitted` now rejects double-submit with a different `evidence_id` for the same claim by emitting the established `warn.idempotent_no_op` JSONL tombstone instead of inserting a duplicate row).

`_handle_evidence_submitted` only blocks duplicate evidence_id (via `INSERT OR IGNORE`). If a caller submits twice with DIFFERENT evidence_ids on a task already at `needs_review`, the second INSERT succeeds; two evidence rows now exist for one submission slot. `_fetch_latest_evidence` returns whichever has the later `submitted_at` — non-deterministic when FrozenClock gives both the same timestamp in tests.

**Fix**: pre-INSERT check — if `evidence_id` is new but task is already at/past `needs_review`, reject with a clear error.

---

### CL-9 · `gates._contains_test_keyword` matches `pytest --collect-only`

**From**: PR #41 Critic-1. **Status**: DONE (v1.7.1 — `review/gates.py::_COLLECT_ONLY_RE` word-boundary regex rejects `--collect-only` / `--co`; 6 regression tests in `test_review.py`).

`pytest --collect-only` exits 0 but runs zero tests. A task requiring "test pass" evidence is satisfied by an agent who only collected tests.

**Fix**: exclude `--collect-only` / `--co` patterns in `_contains_test_keyword`.

---

### CL-10 · capture-evidence.sh + gates.py pattern sets are not aligned

**From**: PR #41 Critic-1. **Status**: OPEN.

Hook captures: pytest, ruff check, mypy, npm test, cargo test, bun test.
Gate recognizes additionally: go test, mvn test, gradle test, make test, python -m unittest, pnpm test.

Agent running `go test ./...` gets no capture (hook skips it) but the gate passes the requirement. Reviewer sees PASSED with no evidence for that command.

**Fix**: lift the pattern set into Phase 6 config (`.fakoli-state/config.yaml`); both hook and gate read from one source.

---

### CL-11 · `template.py:374` calls `datetime.now()` directly

**From**: PR #41 Critic-3. **Status**: DONE (v1.7.1 — `planning/template.py::_parse_tasks` now requires a `clock: Clock` parameter; `parse_prd` accepts an optional `clock: Clock` that defaults to `SystemClock()` for backwards compat. CL-11 docstring on `_parse_tasks` makes the contract explicit).

`_parse_tasks` bypasses the Clock abstraction. Parsed task timestamps are not test-controllable without monkeypatching.

**Fix**: pass a Clock parameter through `parse_prd`; default to `SystemClock()` for backwards compat.

---

### CL-12 · `score_all()`, `infer_dependencies()`, `infer_conflict_groups()` dead public API

**From**: PR #41 Critic-3. **Status**: OPEN.

These are in `__all__` but have no callers outside the module. Misleading public surface.

**Fix**: remove from `__all__` (keep callable internally). Or remove entirely if truly unused.

---

### CL-13 · `next_event_id` returns hardcoded `"E000001"` when conn is None

**From**: PR #41 Critic-2. **Status**: DONE (v1.7.1 — `SqliteBackend.next_event_id` now opens with `conn = self._require_conn()`; the docstring explicitly cites CL-13 and explains the silent-collision footgun the change closes).

The other `Backend` methods call `_require_conn()` to raise on uninitialized state. `next_event_id` instead silently returns a plausible-looking ID. A caller invoking it before `initialize()` gets a misleading success.

**Fix**: call `self._require_conn()` first.

---

### CL-14 · `skills/finish/SKILL.md` references nonexistent `review.created` event

**From**: PR #41 Critic-2. **Status**: DONE (this PR — text now describes the actual `task.applied` event semantics).

SKILL.md line 99 states "Two events are appended to `events.jsonl`: `review.created` and `task.status_changed`." Neither is emitted by `apply`; the actual event is `task.applied`.

**Fix**: update skill body to match the implemented event name.

---

### CL-15 · `.evidence-buffer/` directory has no documented contract

**From**: PR #41 Critic-2. **Status**: DONE (this PR — docs/evidence-buffer.md covers format, lifecycle, orphan.json policy, sentinel interaction, cleanup).

Written by `capture-evidence.sh` + `hook capture-evidence`; consumed only by `sentinel` agent. No README/spec/skill mentions the format, lifecycle, or cleanup policy. `orphan.json` accumulates indefinitely.

**Fix**: add a `docs/evidence-buffer.md` covering format, relationship to `submit`, sentinel's consume-and-rotate behavior, and rotation policy.

---

### CL-16 · `_handle_claim_stale` task transition skips the `stale` intermediate

**From**: PR #41 Critic-2. **Status**: DONE; resolved via P6-3 (Option A — dead code deleted).

---

## Test Quality (any phase; suite hygiene)

### TQ-1 · `_sqlite_dump` docstring claims user_version filtering; doesn't filter

**From**: PR #41 Critic-4. **Status**: OPEN.

`tests/test_sqlite.py:101-116`. Currently harmless (CPython's `iterdump()` doesn't emit `PRAGMA user_version` today). If that ever changes, all 5 audit-guarantee replay tests flap nondeterministically.

**Fix**: either implement the documented filter or delete the misleading docstring claim.

---

### TQ-2 · `test_replay_includes_claim_stale` skips `prd.reviewed`

**From**: PR #41 Critic-4. **Status**: OPEN.

Tests an invalid state sequence (`prd.parsed → prd.approved` without `prd.reviewed`). If the handler ever enforces a reviewed prerequisite, this test breaks cryptically.

**Fix**: insert the `prd.reviewed` event between parsed and approved.

---

### TQ-3 · Two `unittest.mock.patch` usages on `SqliteBackend` violate the no-mocking rule

**From**: PR #41 Critic-4. **Status**: OPEN.

`test_claims.py:1179-1212` patches `apply_event`. `test_claims.py:1224-1257` patches `list_active_claims` to return a fabricated non-active claim — exercising a defensive branch that can never fire in practice.

**Fix**: replace with real failure injection (e.g., `DELETE FROM tasks` to force the stale handler's task UPDATE to match 0 rows). Or delete the unreachable defensive branch + test entirely.

---

### TQ-4 · `test_init_creates_state_directory` first invoke pollutes real cwd

**From**: PR #41 Critic-4. **Status**: OPEN.

`tests/test_cli.py:37-42`: the first `runner.invoke` runs without `chdir(tmp_path)`, then the result is immediately overwritten. Could create `.fakoli-state/` in the test-runner cwd.

**Fix**: delete the dead first-invoke block.

---

### TQ-5 · `test_version_still_works` hardcodes "1.4.0"

**From**: PR #41 Critic-4. **Status**: DONE (PR #42 fixup — test now imports `__version__` from `fakoli_state`).

Fails on every version bump. Should assert `from fakoli_state import __version__` then `assert __version__ in result.output`.

---

### TQ-6 · `_do_init_and_plan` doesn't assert exit codes

**From**: PR #41 Critic-4. **Status**: OPEN.

`tests/test_cli.py:940-972`. If `prd parse` or `plan` fails, all tests using the helper silently get `task_id = None` and skip the real behavior via vacuous `assert task_id is not None`.

**Fix**: add `assert result.exit_code == 0` after each sub-command.

---

### TQ-7 · Phase 3 CLI tests assert on output strings, not SQLite state

**From**: PR #41 Critic-4. **Status**: OPEN.

`test_plan_generates_features_and_tasks` asserts `"feature" in result.output.lower()`. An implementation that prints "feature not created" would pass.

**Fix**: end each CLI integration test with a direct SQLite row-count assertion.

---

### TQ-8 · `tests/test_sqlite.py` is 3924 lines — split per phase

**From**: PR #41 Critic-4. **Status**: OPEN.

Natural split points already marked with section comments. Split into `test_sqlite_phase2.py` ... `test_sqlite_phase5.py`.

---

## Performance / Scale

### PS-1 · `_check_group_conflicts` has N+1 query

**From**: PR #41 Critic-2. **Status**: DONE (v1.7.1 — `ClaimManager._check_group_conflicts` collapses 1+N round-trips into 2 via a single bulk `list_tasks()` + in-memory `dict[task_id, Task]` lookup; docstring carries the PS-1 reference).

For each active claim, `manager.py:700-720` calls `backend.get_task(active_claim.task_id)` inside a loop. With 10 parallel agents, a claim operation costs 1 + N + N SQL round-trips.

**Fix**: prefetch all tasks for active claims in a single `list_tasks()` call and build a local map.

---

### PS-2 · Snapshots/ directory is dead scaffolding

**From**: PR #41 Critic-2. **Status**: DONE (this PR — `init` no longer pre-creates `.fakoli-state/snapshots/`; the `fakoli-state snapshot` command will create it on first use when implemented).

`init` creates `.fakoli-state/snapshots/`, prints it, preserves it on `--force`. Nothing writes to it. Either implement `fakoli-state snapshot` (a `sqlite3 .backup` wrapper) or stop creating the directory.

---

## Closed in PR #41 fixup commits (for reference)

- DONE · Greptile #1: `_is_pr_related` bare "pr" substring
- DONE · Greptile #2: capture-evidence.sh 8 python3 spawns → 1
- DONE · Greptile #3: `packet --format json` echoes JSON not markdown
- DONE · Greptile #4: `_fetch_latest_evidence` datetime parsed 3x → 1x
- DONE · Critic-1: `task.applied --reject` auto-promote to drafted
- DONE · Critic-3: `warn.idempotent_no_op` replay crash
- DONE · Critic-3: `release()` double-emit destroying evidence
- DONE · Critic-3: `claim()` double-emit
- DONE · Critic-3: capture-evidence.sh `datetime.utcnow()` deprecated
- DONE · Critic-2: `evidence attach` ghost command
- DONE · Critic-4: 3 hook tests with always-passing assertions (+ exposed a real heredoc/pipe bug in capture-evidence.sh)
