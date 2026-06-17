# SL1-RR-1 Event-Sourced Write Path — Execution Plan

**Goal:** Replace `apply_event` with an event-sourced `append(EventDraft) -> Event|None` write path (decide/apply split, log-as-id-authority, append-only canonical log, relaxed-default durability) so a poison replay line is structurally impossible.
**Spec:** plugins/fakoli-state/docs/specs/2026-06-01-sl1-rr-1-event-sourcing-write-path.md
**Language:** Python (plugins/fakoli-state/bin/pyproject.toml)
**Crew:** fakoli-crew v2.0.1 (8 agents)

> Scout findings (read before executing): `.fakoli/runs/plan-2026-06-01-sl1-rr-1-event-sourcing-write-path-202606012249/agent-scout-status.md`. Key corrections to the spec's assumptions: **24** dispatch actions (16 real-mutation + 17 audit-only), `next_event_id` has zero production callers, `_apply_event_sqlite_only` carries a `NotImplementedError` silent-skip that must be removed, Config is a frozen dataclass (flat `durability` field), and the P4 golden `expected-state.json` is unchanged by the refactor.

**Verify base command:** `uv run --project plugins/fakoli-state/bin --extra all-providers --with pytest pytest` (run from repo root; CI sets `TERM=dumb` and unsets `FORCE_COLOR`).

---

### Task 1: Add the `durability` configuration knob

**Intent:** Add a `durability` setting that selects relaxed (default) vs strict persistence, following the existing flat-field config pattern.
**Acceptance criteria:**
- `Config` exposes a `durability` field that accepts only `"relaxed"` or `"strict"` and defaults to `"relaxed"` when the key is absent.
- An invalid value raises the same validation error type that other literal-typed config fields raise.
- `write_default_config` output includes the `durability` key documented with its two legal values and the relaxed default.
- Loading a config with no `durability` key yields `"relaxed"` (back-compat).
**Scope:** plugins/fakoli-state/bin/src/fakoli_state/config.py
**Agent:** guido
**Verify:** `uv run --project plugins/fakoli-state/bin --extra all-providers --with pytest pytest plugins/fakoli-state/bin/tests/test_config.py`
**Depends on:** (none)

**Prescriptive detail (configuration values):**
- Key name: `durability` (flat top-level, matching `git_ops_mode`/`branch_prefix` — not nested under `storage:`).
- Allowed values: `relaxed`, `strict`. Default: `relaxed`.
- Validate via the existing `_validate_literal(value, ("relaxed", "strict"), "durability")` helper.

---

### Task 2: Introduce `EventDraft`/`Event` types and the new error signals

**Intent:** Establish the type-level command/event distinction and the three append outcomes the new write path needs.
**Acceptance criteria:**
- An `EventDraft` model exists carrying every event field except `id`; `Event` extends it by adding the assigned `id`.
- `EventRejected` (illegal transition / bad payload) and `IdempotentNoOp` (legal but already-satisfied) exception types exist alongside the existing `TransactionAborted`/`StateLocked`.
- The new types import and a round-trip (`EventDraft` → `Event` with an id) serializes/validates without error.
- `PENDING_EVENT_ID` still exists at this stage (its removal is deferred to the caller-migration task) so the build remains green.
**Scope:** plugins/fakoli-state/bin/src/fakoli_state/state/models.py, plugins/fakoli-state/bin/src/fakoli_state/state/backend.py
**Agent:** guido
**Verify:** `uv run --project plugins/fakoli-state/bin --extra all-providers --with pytest pytest plugins/fakoli-state/bin/tests/test_models.py`
**Depends on:** (none)

---

### Task 3: Split every action handler into `_check_*` / `_write_*` behind a unified dispatch

**Intent:** Refactor the 24 action handlers into a validation phase and an infallible mutation phase, registered in one dispatch table, while preserving current `apply_event` behavior so the existing suite stays green.
**Acceptance criteria:**
- Every one of the 24 dispatched actions has a `_check_<action>` (reads state, raises `EventRejected` on illegal input, raises `IdempotentNoOp` on already-satisfied requests) and a `_write_<action>` (performs the mutation, contains no validation/raises).
- A single dispatch structure maps each action to its `(payload_model, check, write)`.
- The 17 audit-only actions (`state.initialized`, `file_changed`, `progress.noted`, and the 13 `sync.*`) have a check that always proceeds and a write that is a no-op.
- `apply_event` is internally re-expressed as "validate via `_check_*`, then mutate via `_write_*`" with no externally observable change; the full existing suite passes unchanged.
**Scope:** plugins/fakoli-state/bin/src/fakoli_state/state/sqlite.py
**Agent:** guido
**Verify:** `uv run --project plugins/fakoli-state/bin --extra all-providers --with pytest pytest plugins/fakoli-state/bin/tests/test_sqlite.py plugins/fakoli-state/bin/tests/test_claims.py`
**Depends on:** Task 2

---

### Task 4: Build the `append` write path, log-authority counter, lock, self-heal, and strict replay

**Intent:** Replace the dual-ordering `apply_event` with a single log-first `append` path that assigns ids from the log, serializes via a file lock, heals skew on open, and rebuilds via a strict no-skip replay.
**Acceptance criteria:**
- `append(EventDraft) -> Event|None` is the sole write entry point: it validates (`_check_*`), assigns an id from an in-memory counter, appends the line to `events.jsonl` before the SQLite mutation, then applies and commits; it returns the materialized `Event`, or `None` when `_check_*` raised `IdempotentNoOp`.
- A validation failure writes zero lines to `events.jsonl` and exactly one `rejection` line to `audit.jsonl`; an idempotent no-op writes zero event lines and one `idempotent_no_op` line to `audit.jsonl`.
- The id counter is seeded from the log's max id on open (not from SQLite `MAX(id)`); a concurrent-append critical section is guarded by an `fcntl.flock` on `events.jsonl` with a contention timeout surfacing as `StateLocked`.
- On open, when the events table is behind the log, the missing tail is re-applied via `_write_*` (forward catch-up) and the counter is seeded from the log max so an orphaned id is never reassigned.
- `replay_from_empty` applies every line via `_write_*` with no action-name skip-list and no `NotImplementedError` silent-skip; an interior malformed line raises, a torn trailing line is tolerated.
- `apply_event`, `next_event_id`, and the canonical-log writes in `_append_abort_event`/`_append_warn_log` are removed; the Backend Protocol reflects the new surface.
- `durability="strict"` issues `synchronous=FULL` plus an `fsync` of the log before COMMIT; `relaxed` does neither.
**Scope:** plugins/fakoli-state/bin/src/fakoli_state/state/sqlite.py, plugins/fakoli-state/bin/src/fakoli_state/state/backend.py
**Agent:** welder
**Verify:** `uv run --project plugins/fakoli-state/bin --extra all-providers --with pytest pytest plugins/fakoli-state/bin/tests/test_sqlite.py -k "append or replay or lock or durability"`
**Depends on:** Task 1, Task 3

---

### Task 5: Migrate all production callers to `append` and remove `PENDING_EVENT_ID`

**Intent:** Convert every production event producer from `Event(id=PENDING_EVENT_ID, …)` + `apply_event` to `EventDraft` + `append`, handling the `None` no-op return, and delete the now-unused sentinel.
**Acceptance criteria:**
- No reference to `apply_event`, `next_event_id`, or `PENDING_EVENT_ID` remains anywhere under `plugins/fakoli-state/bin/src/`.
- Every production caller (mcp_server, cli/*, planning/*, claims/*) constructs an `EventDraft` and calls `append`; call sites that could be idempotent no-ops handle a `None` return without error.
- The package imports cleanly and `fakoli-state --help` plus an `init`→`prd parse`→`plan` smoke sequence complete with exit code 0.
**Scope:** plugins/fakoli-state/bin/src/fakoli_state/mcp_server.py, plugins/fakoli-state/bin/src/fakoli_state/cli/plan.py, plugins/fakoli-state/bin/src/fakoli_state/cli/init_status.py, plugins/fakoli-state/bin/src/fakoli_state/cli/packet_apply.py, plugins/fakoli-state/bin/src/fakoli_state/cli/sync.py, plugins/fakoli-state/bin/src/fakoli_state/planning/_plan_helpers.py, plugins/fakoli-state/bin/src/fakoli_state/claims/manager.py, plugins/fakoli-state/bin/src/fakoli_state/claims/stale.py
**Agent:** welder
**Verify:** `uv run --project plugins/fakoli-state/bin --extra all-providers --with pytest pytest plugins/fakoli-state/bin/tests/test_cli.py plugins/fakoli-state/bin/tests/test_mcp.py`
**Depends on:** Task 4

---

### Task 6: Migrate the test suite to the `append` API

**Intent:** Update test helpers and call sites so tests append drafts and assert against engine-assigned ids rather than hardcoded ones, with no mocking of business logic.
**Acceptance criteria:**
- The `_make_event`/`_event`/`_make_project_event` helpers (test_claims, test_sqlite, test_snapshot, test_reconciliation) build `EventDraft`s (no id) and return the materialized `Event` from `append`.
- Tests that previously asserted hardcoded ids now assert against the returned `Event.id`; no test references `PENDING_EVENT_ID` or `apply_event`.
- The full existing suite (excluding the new behavioral tests in Task 7 and the fixture in Task 8) passes.
**Scope:** plugins/fakoli-state/bin/tests/test_claims.py, plugins/fakoli-state/bin/tests/test_sqlite.py, plugins/fakoli-state/bin/tests/test_snapshot.py, plugins/fakoli-state/bin/tests/test_reconciliation.py
**Agent:** welder
**Verify:** `uv run --project plugins/fakoli-state/bin --extra all-providers --with pytest pytest plugins/fakoli-state/bin/tests/test_claims.py plugins/fakoli-state/bin/tests/test_snapshot.py plugins/fakoli-state/bin/tests/test_reconciliation.py`
**Depends on:** Task 5

---

### Task 7: Add the SL1-RR-1 behavioral regression tests

**Intent:** Prove the new invariants — poison-line impossibility, id-reuse self-heal, concurrency safety, append-only write-failure, replay strictness, the decide/apply contract, and durability modes — with real (non-mocked) failure injection.
**Acceptance criteria:**
- A poison-line test asserts a failed `append` leaves zero new canonical lines, one `rejection` in `audit.jsonl`, and a subsequent `replay_from_empty` byte-equal to the pre-append state.
- An id-reuse/self-heal test injects a log-ahead-of-projection skew, reopens, and asserts forward catch-up converges and the next `append` gets the next sequential id (the orphaned id is never reassigned).
- A concurrency test exercises two backends sharing one `events.jsonl` with interleaved appends and asserts no id collision and no dropped event.
- An append-only test injects a real `_write_*` failure (subclass override or FK violation) and asserts the log line remains, SQLite is rolled back, a `write_failed_after_log` line is audited, and the next open catch-up converges.
- A replay-strictness test asserts an interior malformed line raises while a torn trailing line is tolerated.
- A durability test asserts `strict` fsyncs/sets `synchronous=FULL` and `relaxed` does not, verified without mocking business logic.
**Scope:** plugins/fakoli-state/bin/tests/test_sqlite.py, plugins/fakoli-state/bin/tests/test_replay_equivalence.py
**Agent:** welder
**Verify:** `uv run --project plugins/fakoli-state/bin --extra all-providers --with pytest pytest plugins/fakoli-state/bin/tests/test_sqlite.py -k "poison or self_heal or concurren or append_only or strict or durability"`
**Depends on:** Task 4

---

### Task 8: Regenerate the P4 replay-equivalence golden fixture

**Intent:** Rebuild the sample-project fixture under the new write path so the rejection lives in `audit.jsonl` and the canonical log is failure-free, keeping the P4 equivalence proof green.
**Acceptance criteria:**
- `regenerate.py` produces the fixture via `append` (no direct JSONL writes); the former `error.transaction_aborted`/`warn.idempotent_no_op` lines are produced by a real rejected/no-op `EventDraft` and land in `audit.jsonl`, not `events.jsonl`.
- The fixture's `events.jsonl` contains only real events; `expected-state.json` is unchanged from the current golden.
- `test_replay_equivalence` passes against the regenerated fixture.
**Scope:** plugins/fakoli-state/bin/tests/fixtures/replay/regenerate.py, plugins/fakoli-state/bin/tests/fixtures/replay/sample-project/events.jsonl, plugins/fakoli-state/bin/tests/fixtures/replay/sample-project/expected-state.json
**Agent:** welder
**Verify:** `uv run --project plugins/fakoli-state/bin --extra all-providers --with pytest pytest plugins/fakoli-state/bin/tests/test_replay_equivalence.py`
**Depends on:** Task 5

---

### Task 9: Full-suite validation scorecard against the spec's acceptance criteria

**Intent:** Confirm the complete change satisfies every spec acceptance criterion with a binary pass/fail scorecard backed by command output.
**Acceptance criteria:**
- The full fakoli-state suite passes via the base verify command.
- A grep confirms zero remaining references to `apply_event`, `next_event_id`, or `PENDING_EVENT_ID` across `bin/src/` and `bin/tests/`.
- Each of the 12 spec acceptance criteria is marked PASS with the supporting command/output, or FAIL with the exact gap.
**Scope:** plugins/fakoli-state/ (read-only validation; no edits)
**Agent:** sentinel
**Verify:** `uv run --project plugins/fakoli-state/bin --extra all-providers --with pytest pytest plugins/fakoli-state/bin/tests`
**Depends on:** Task 6, Task 7, Task 8

---

### Task 10: Code review of the write-path refactor

**Intent:** Review the refactor for correctness, concurrency safety, and adherence to the spec's invariants, returning severity-rated findings.
**Acceptance criteria:**
- The lock critical section, counter seeding, and forward-catch-up are reviewed for the id-reuse and skew-direction guarantees with no MUST-FIX correctness defect outstanding.
- The decide/apply split is confirmed to keep `_write_*` free of validation/raises and `_check_*` free of writes.
- Any MUST-FIX findings are resolved before sign-off; SHOULD-FIX/CONSIDER findings are recorded.
**Scope:** plugins/fakoli-state/bin/src/fakoli_state/state/sqlite.py, plugins/fakoli-state/bin/src/fakoli_state/state/backend.py, plugins/fakoli-state/bin/src/fakoli_state/state/models.py
**Agent:** critic
**Verify:** (review task — no command; verdict recorded in status)
**Depends on:** Task 9

---

### Task 11: Update ledgers, changelog, version, and registry

**Intent:** Record the closure across the project's books — mark SL1-RR-1 done, refresh the P4 ledger entry, changelog the architecture, bump the version, and regenerate the registry.
**Acceptance criteria:**
- `tech-debt-backlog.md` SL1-RR-1 is marked `DONE` with a commit/PR cross-ref; the fakoli-style P4 `open_work` is updated to mark SL1-RR-1 resolved and point at the new embodiments (the regenerated equivalence test + the new behavioral tests), and `fakoli-style` validate passes.
- `plugins/fakoli-state` `CHANGELOG.md` gains an entry describing the event-sourced write path; the plugin version is bumped (minor) and `registry/index.json` reflects the new version.
- `./scripts/validate.sh plugins/fakoli-state` passes and the README/marketplace/registry remain in sync.
**Scope:** plugins/fakoli-state/docs/tech-debt-backlog.md, plugins/fakoli-state/CHANGELOG.md, plugins/fakoli-state/.claude-plugin/plugin.json, plugins/fakoli-style/data/principles.json, registry/index.json
**Agent:** keeper
**Verify:** `./scripts/validate.sh plugins/fakoli-state && cd plugins/fakoli-style && uv run --script scripts/validate.py`
**Depends on:** Task 10
