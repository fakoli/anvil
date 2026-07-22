# SL1-RR-1 — Event-Sourced Write Path (decide/apply split, log-as-id-authority)

**Date:** 2026-06-01
**Status:** Approved (brainstorm) — ready for `/flow:plan`
**Plugin:** `anvil`
**Tracks:** tech-debt-backlog `SL1-RR-1`; fakoli-style principle **P4** `open_work`
**Supersedes the tactical fix** described in the original SL1-RR-1 review (Option A / Option C). This spec adopts the deeper root-cause fix.

---

## 1. Goal

Make a "poison canonical line" — a `events.jsonl` line that aborts a full
replay — **structurally impossible**, by restructuring the write path around
event-sourcing's command/event separation. As a consequence, also close the
inverse "post-COMMIT audit gap" on the live path and retire the overloaded
`apply_event` method and the replay skip-list.

The replay-equivalence guarantee proven by `test_replay_equivalence`
(fakoli-style P4) must remain green throughout and at the end.

## 2. Context & root cause

`apply_event` currently behaves two different ways depending on whether
`event.id == PENDING_EVENT_ID`:

- **PENDING (live path):** assign id from `SELECT MAX(id)` *inside* the
  `BEGIN IMMEDIATE` lock, mutate, COMMIT, **then** append JSONL. A crash
  between COMMIT and the JSONL write leaves the projection ahead of the log;
  the event is lost on the next full replay (the documented audit gap at
  `state/sqlite.py:320-337`).
- **Non-PENDING (replay/legacy path):** append JSONL **first**, then mutate.
  If the mutation is rejected, the canonical line is already persisted;
  `replay_from_empty` re-applies it and re-fails, aborting the entire replay
  (SL1-RR-1).

Both gaps share **one root cause**: the event id is sourced from the SQLite
projection (`MAX(id)`), which forces id assignment to happen inside the DB
transaction, which forces an impossible choice between "log first, id unknown"
and "id known, projection committed first."

One layer deeper: the engine **validates at apply time** and can reject an
event that is (or is about to be) in the log. In an event-sourced system,
validation belongs to the *command*, before anything is logged. Once an event
is in the log it is a *fact* that must replay infallibly. The "poison line"
can only exist because today's design violates that separation.

### Reachability (why this is latent today)

Every live caller constructs events with `PENDING_EVENT_ID` (verified across
`cli/`, `planning/`, `mcp_server.py` — zero non-PENDING live ids). The
JSONL-first non-PENDING branch has **no production callers**; its primary
consumer is the **test suite**, whose helpers (`_make_event` defaulting
`event_id="E000003"`, `_event` requiring an explicit `event_id`) feed hardcoded
sequential ids. So SL1-RR-1 is a latent correctness landmine, armed for the
first future caller that supplies real ids (an importer/migration), and
exercised today only by tests.

## 3. Architecture

Four structural moves:

1. **Decide/apply split per action.** Each of the ~17 `_handle_<action>`
   handlers splits into:
   - `_check_<action>(conn, payload, event) -> None` — reads current state,
     raises `EventRejected` on any illegal transition / integrity violation.
     Validation only; performs no writes.
   - `_write_<action>(conn, payload, event) -> None` — executes the mutation,
     **infallible given that the matching `_check_*` passed**.

   The dispatch table maps `action -> ActionSpec(payload_model, check, write)`.

2. **Two entry points, selected by intent (not by a sentinel id):**
   - `append(draft) -> Event` — the *only* production write. Validates,
     assigns id, logs, applies.
   - `replay_from_empty(path)` — rebuild. Applies via `_write_*` only:
     **no validation, no logging, no skip-list.** Infallible for any
     complete, ordered log.

   The non-PENDING-with-logging branch is **deleted** — the architecture no
   longer has that concept.

3. **The log is the id authority.** An in-memory monotonic counter, seeded
   from the log's max id **on open** (not from SQLite `MAX(id)`). A file lock
   (`flock` on `events.jsonl`) serializes the whole append critical section so
   concurrent CLI + MCP appends cannot collide. This *replaces* the
   `BEGIN IMMEDIATE` + `SELECT MAX(id)` race fix from PR #41 Critic-3.

4. **The canonical log holds only real events.** Rejections and idempotent
   no-op warnings move out of `events.jsonl` into a sibling `audit.jsonl`,
   which is never replayed. Consequence: `replay_from_empty` no longer skips
   anything — the `error.transaction_aborted` / `warn.idempotent_no_op`
   action-name skip logic (`state/sqlite.py:417-435`) is deleted. Every line
   in `events.jsonl` is a fact.

This also fixes the **skew direction**: log-first means a crash can only leave
the *log ahead of the projection* (self-healing via forward catch-up on open),
never the projection ahead of the log (today's unrecoverable PENDING gap).

## 4. Data model & interfaces

### Types — retire the `PENDING_EVENT_ID` sentinel

```python
class EventDraft(BaseModel):      # an intended mutation; id not yet assigned
    timestamp: datetime
    actor: str
    action: str
    target_kind: str
    target_id: str
    payload_json: dict[str, Any]
    # NO id field

class Event(EventDraft):          # a fact: a draft assigned an id and applied
    id: str                       # "E000001"...
```

You `append(EventDraft)` and receive an `Event`. The type system prevents
handing an unassigned draft to replay, or a real `Event` to `append`. The
`PENDING_EVENT_ID` magic string is removed.

### Backend Protocol surface

```python
def append(self, draft: EventDraft) -> Event | None: ...
    # validate -> assign id from log -> append events.jsonl -> apply (infallible)
    # Returns the materialized Event on success.
    # Returns None for a legal idempotent no-op (nothing logged; audited).
    # Raises EventRejected (illegal transition / bad payload) — nothing written to events.jsonl.
    # Raises TransactionAborted (infra failure after log append) — rare, loud.
    # Raises StateLocked (flock contention timeout).

def replay_from_empty(self, events_path: str) -> None: ...   # signature unchanged
    # rebuild via _write_* only; no validation, no logging, no skip-list

# REMOVED from the protocol:
#   apply_event(event)   -> replaced by append(draft)
#   next_event_id()      -> id authority is internal to append's locked section
```

All events, **including bootstrap** (`project.created`, `state.initialized`),
flow through the uniform `append` path. There is no bootstrap-aware branch;
ordering prerequisites (e.g. "feature needs a project") are enforced by each
action's own `_check_*`. The only true prerequisite is that `initialize()`
(schema creation) runs before the first `append`, which is already a separate
step.

### Dispatch table

```python
class ActionSpec(NamedTuple):
    payload_model: type[BaseModel]
    check: Callable[[Connection, BaseModel, Event], None]   # raises EventRejected (illegal) or IdempotentNoOp (already satisfied)
    write: Callable[[Connection, BaseModel, Event], None]   # infallible given check passed

ACTION_DISPATCH: dict[str, ActionSpec]
```

A `_check_*` has three outcomes: return normally (proceed to log + write),
raise `EventRejected` (illegal transition / bad payload — see error types), or
raise `IdempotentNoOp` (legal but already-satisfied, e.g. releasing an
already-released claim). `append` catches `IdempotentNoOp`, writes an
`idempotent_no_op` line to `audit.jsonl`, and returns `None` without logging or
mutating.

### Error types

- `EventRejected` (new) — illegal transition / bad payload on `append`.
  Normal, expected control path. Nothing written to `events.jsonl`.
- `IdempotentNoOp` (new) — legal but already-satisfied request (e.g. releasing
  an already-released claim). Caught inside `append`; not propagated to the
  caller as an error — `append` returns `None`.
- `TransactionAborted` (narrowed) — now means *only* an unexpected
  infrastructure failure (disk, SQLite operational, a bug in a `_write_*`).
  Should be vanishingly rare; never a normal-flow control path.
- `StateLocked` (unchanged) — `flock` contention beyond the timeout.

### `audit.jsonl` line shape (sibling of `events.jsonl`, never replayed)

```json
{"ts":"...","kind":"rejection","actor":"...","attempted_action":"task.applied","target_id":"T003","reason":"evidence.submitted payload requires non-empty 'commands_run'"}
{"ts":"...","kind":"idempotent_no_op","action":"claim.released","target_id":"T001","reason":"..."}
{"ts":"...","kind":"write_failed_after_log","event_id":"E000042","action":"task.applied","reason":"..."}
```

No `id` field — these are not events and never collide with the `E######` space.

### Lock interface

A `_append_lock()` context manager wrapping `fcntl.flock(events_fd, LOCK_EX)`
with a timeout mirroring `busy_timeout=5000` (contention beyond it raises
`StateLocked`), with a single in-process `threading.Lock` nested inside for
same-process MCP + thread safety.

### Durability mode

```
durability: relaxed   # DEFAULT — laptop: synchronous=NORMAL, buffered log, no per-event fsync
           | strict   # CI/shared/server: synchronous=FULL + fsync(log) before COMMIT
```

## 5. Data flow

### Write (the only production path)

```
append(draft):
  with _append_lock():                      # flock(events.jsonl) + in-proc lock
    _check_<action>(conn, payload, draft)    # raises EventRejected -> audit.jsonl, re-raise
    id    = _next_seq()                      # log-owned counter; increments at log-append time
    event = Event(id=id, **draft)
    append_line(events.jsonl, event)         # (1) log write — source of truth (log-first)
    BEGIN IMMEDIATE
      _write_<action>(conn, payload, event)  # (2) infallible projection mutation
      _insert_event_row(conn, event)
    COMMIT
  return event
```

If validation fails, we raise *before* (1) — nothing touches `events.jsonl`.
The in-memory counter increments at **log-append time** (not commit) so that a
re-run in the same process after a write failure gets the next id and the
failed event remains accounted-for in the log.

### Rejection / no-op (never in the canonical log)

```
_check_* raises EventRejected   -> append_line(audit.jsonl, {kind:"rejection", ...}); re-raise
_check_* raises IdempotentNoOp  -> append_line(audit.jsonl, {kind:"idempotent_no_op", ...}); return None (no event)
```

### Rebuild

```
replay_from_empty(path):
  close(); delete state.db (+ -wal/-shm); initialize()
  for line in path:                          # every line is a real Event — no skip-list
    event = Event.model_validate(line)        # torn trailing line tolerated; interior corruption -> raise
    BEGIN IMMEDIATE; _write_<action>(conn, payload, event); _insert_event_row; COMMIT
  _next_seq = max(event.id)                    # counter re-synced post-rebuild
```

### Open + self-heal (skew recovery)

```
open():
  initialize()                                # ensure schema
  log_max   = scan_tail(events.jsonl)          # LOG is the id authority; reads last line only
  table_max = SELECT MAX(id) FROM events
  if table_max < log_max:                      # crash left log ahead of projection
      forward_catch_up: _write_* the tail (table_max+1 .. log_max)   # self-heal
  _next_seq = log_max                           # never reassigns an orphaned id
```

`scan_tail` reads only the log's final line (seek-to-EOF, scan back to last
newline — O(line), not O(file)). Forward catch-up reuses `_write_*`, sharing
the replay code path; there is no third apply implementation.

## 6. Performance & durability

- **Not a background process.** No daemon, poller, or idle loop — work happens
  only while a CLI command or MCP call executes, then the process exits. Zero
  cycles between developer actions.
- **The hot path gets cheaper:** the per-append `SELECT MAX(id)` is removed
  (in-memory counter); `flock` is advisory and contention-only (no spin/poll,
  ~0 cost uncontended); no per-event fsync in `relaxed`.
- **Correctness does not depend on fsync.** Deterministic replay and
  log-never-behind-projection are guaranteed by ordering (log write precedes
  COMMIT) + the log-authority counter + forward catch-up. Worst case on hard
  power-loss under `relaxed`: the last few un-synced events drop from log *and*
  projection together; the user repeats the last action.
- **Reads stay fully concurrent via WAL** — status/list never block on a
  writer. Writes serialize through one fast lock.
- **Scaling boundary (explicit):** single-machine, single-project coordination
  ledger with a low write rate. Serialized writes buy the deterministic total
  order P4's replay proof depends on. If write throughput ever becomes a real
  bottleneck on a bigger machine, the lever is *batching multiple appends under
  one lock acquisition* — a measure-then-add future optimization, **out of
  scope here.** Designing for distributed/high-write now would trade away the
  determinism that is the whole point.

## 7. Error handling

| Failure mode | Where | Surfaces as | Log effect |
|---|---|---|---|
| Validation failure | `_check_*` raises `EventRejected` | Clear caller error; *normal* path | No `events.jsonl` line; `rejection` in `audit.jsonl` |
| Idempotent no-op | engine detects | Returns normally | No event; `idempotent_no_op` in `audit.jsonl` |
| Write failure after log append | `_write_*`/SQLite raises despite passing check | `TransactionAborted` — loud infra alarm | Log line stays (append-only); SQLite rolled back; `write_failed_after_log` audited; healed by forward catch-up on next open |
| Lock contention | `flock` timeout | `StateLocked` — "retry" | None |
| Replay hits a write failure | `replay_from_empty` | **Hard raise** — integrity alarm | n/a |
| Torn final log line | crash mid-append (`relaxed`) | Tolerated silently | Only trailing partial line ignored |

Three deliberate decisions:

1. **The log is strictly append-only — no truncation, even on write failure.**
   A post-check write failure means a real bug or disk emergency; roll back
   SQLite, leave the log line, let forward catch-up re-apply on next open. The
   event is never lost and the failure is loud.
2. **Replay failures are loud — the skip-list is gone for good.** Every log
   line is a validated fact; a replay/catch-up write failure is a genuine
   integrity alarm and **raises**. We never silently skip, because skipping
   diverges state and would quietly break the P4 replay-equivalence guarantee.
3. **Only the trailing log line may be torn; an interior malformed line is
   corruption.** A crash can only damage the last append, and that event was
   never committed nor returned to a caller — so ignoring a partial final line
   is safe. A malformed interior line is real corruption → hard fail.

**Severity inversion:** today `TransactionAborted` is a normal outcome (bad
payload, illegal transition all funnel through it), which is *why* the poison
line and skip-list exist. Moving rejection to a pre-log validation gate makes
`TransactionAborted` rare-and-alarming, the canonical log failure-free, and
replay strict.

## 8. Testing

### A. Test-suite migration

Migrate the ~5 helpers (`_make_event` in `test_claims`/`test_reconciliation`/
`test_sqlite`; `_event` in `test_snapshot`/`fixtures/replay/regenerate.py`) to
build an `EventDraft` (no id) and call `append`, returning the materialized
`Event`. Tests that hardcoded ids now assert against the **returned** id.
Direct `Event(id=PENDING_EVENT_ID, …)` constructions are updated individually.
Honors the project's **no-mocking rule** — real appends, real ids.

### B. New behavioral tests

| Test | Asserts |
|---|---|
| **Poison-line impossibility** (SL1-RR-1 regression) | `append` of a draft whose `_check_*` fails → **zero** new canonical lines in `events.jsonl`, one `rejection` in `audit.jsonl`; subsequent `replay_from_empty` byte-equal to pre-append state |
| **ID-reuse / self-heal** | Inject log-ahead skew (log has `E00000N`, projection lacks it) → on open `_next_seq` seeds from `log_max`, forward catch-up applies the tail, a following `append` gets `E00000(N+1)` — orphaned id never reassigned |
| **Concurrency (PR #41 Critic-3)** | Two backends sharing one `events.jsonl`, interleaved appends → no id collision, no lost event; `flock` serializes |
| **Append-only on write failure** | Real failure injection (subclass overriding one `_write_*` to raise, or an FK violation) → log line remains, SQLite rolled back, `write_failed_after_log` audited, next open catch-up converges |
| **Replay strictness** | Corrupt *interior* line → replay **raises** (not skips); torn *trailing* line → tolerated |
| **Decide/apply contract** | Per action: `_check_*` rejects illegal state with no side effects; `_write_*` succeeds whenever its `_check_*` passed |
| **Durability modes** | `relaxed` (default): `synchronous=NORMAL`, no per-event fsync. `strict`: `synchronous=FULL` + fsync(log) before COMMIT (verified via a real fsync spy / pragma read, not a mock of business logic) |

### C. P4 golden equivalence — update, don't weaken

Regenerate `tests/fixtures/replay/sample-project/` to the new shape: canonical
`events.jsonl` contains **only** real events (the old `error.transaction_aborted`
line `E000099` moves to `audit.jsonl`). `test_replay_equivalence`'s byte-equal
`serialize_state(normal) == serialize_state(replay) == golden` assertion stays
green — it is the P4 proof and the embodiment fakoli-style P4 points at.

### D. Performance guardrail (smoke, not a flaky perf gate)

A test with a large synthetic log asserting `scan_tail` reads bounded bytes
(last line only), not the whole file. No wall-clock perf gate in CI.

### E. CI

Full suite runs on every anvil PR via `.github/workflows/anvil.yml`
(with the `TERM=dumb` / unset `FORCE_COLOR` guard already in place), invoked as
`uv run --project bin --extra all-providers --with pytest pytest`.

## 9. Acceptance criteria

1. `apply_event` and `next_event_id` are removed from the Backend Protocol and
   all implementations; `append(EventDraft) -> Event` is the sole production
   write entry point. `PENDING_EVENT_ID` is removed.
2. Every action has a `_check_<action>` and `_write_<action>` registered in
   `ACTION_DISPATCH`; `_write_*` performs no validation. A `_check_*` signals
   one of three outcomes: proceed, `EventRejected`, or `IdempotentNoOp`
   (`append` returns `None` for the last).
3. A failed `append` (validation) writes **zero** lines to `events.jsonl` and
   exactly one `rejection` line to `audit.jsonl`.
4. `events.jsonl` contains only canonical events; `replay_from_empty` contains
   no action-name skip-list and applies every line.
5. Event ids are assigned from an in-memory counter seeded from `log_max` on
   open; concurrent appends (CLI + MCP) never collide and never drop an event.
6. Forward catch-up converges a log-ahead-of-projection skew on open without
   reassigning the orphaned id.
7. `relaxed` durability is the default; `strict` is opt-in and the only mode
   that fsyncs per event.
8. The poison-line regression test and the ID-reuse/self-heal test pass.
9. `test_replay_equivalence` (P4) remains green against the regenerated golden.
10. The full anvil suite passes in CI.
11. fakoli-style P4 `open_work` is updated to mark SL1-RR-1 resolved and point
    at the new embodiments; `tech-debt-backlog.md` SL1-RR-1 marked `DONE`.
12. `plugins/anvil` version bumped (minor — new write-path architecture)
    and `registry/` regenerated.

## 10. Out of scope

- Batching multiple appends under one lock acquisition (future throughput
  optimization; measure first).
- An importer / migration tool that supplies externally-authored event ids
  (the future caller this hardening anticipates). When built, it routes through
  `replay_from_empty` / a dedicated import path, not `append`.
- The "Option C" replay healer (skip canonical lines carrying an abort
  tombstone). Made unnecessary by this design — poison lines are now
  unrepresentable — and recorded here only to note it is deliberately dropped.
- Distributed / multi-machine / high-write operation.

## 11. References

- `tech-debt-backlog.md` § SL1-RR-1 (originating finding)
- fakoli-style principle **P4** (`data/principles.json`) — `open_work`
- `state/sqlite.py`: `apply_event` (231-373), `replay_from_empty` (379-438),
  `_apply_event_sqlite_only` (2572-2599), `_append_abort_event` (2612-2649),
  `_apply_mutation` + handlers (967+)
- PR #41 Critic-3 (the `next_event_id` race this design's lock supersedes)
- `test_replay_equivalence.py`, `test_snapshot.py`, `test_sqlite.py` (suites to migrate)
