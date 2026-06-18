# Spec: Git-Backed Events — Making State Travel With the Repo

**Status:** PROPOSED
**Date:** 2026-06-10
**Owner:** anvil
**Depends on:** event-sourced write path (SL1-RR-1, shipped in #75)

## Why

anvil's moat is durable, evidence-bearing, project-scoped state. But today that
state is *machine*-scoped, not *repo*-scoped: `.anvil/events.jsonl` is gitignored,
so a clone, a second worktree, a teammate, or a CI runner starts blind. The GitHub
Issues sync bridges some of this, but it is a bespoke bridge to an external system —
not a property of the state engine itself.

Beads (steveyegge/beads, ~24k stars) demonstrated the right end-state: a git-committed
JSONL log as the merge-friendly source of truth, with SQLite as a disposable local
cache, and git itself as the sync, history, and conflict layer. anvil is
unusually close to this already — the JSONL log **is** our source of truth and the
SQLite projection **is** rebuildable byte-for-byte via replay. One design decision
blocks the rest: event IDs.

## The blocker: sequence-numbered IDs

Event IDs are assigned as `E{N}` (`E000001`, `E000002`, …) from `_next_seq` inside the
append critical section (`state/sqlite.py:418-421`). That guarantees uniqueness on one
machine and makes replay trivially ordered — and it collides the moment two branches or
two machines append independently. Task IDs (`T001`, …) have the same problem one level
up.

This is exactly the problem Beads solved with hash-based hierarchical IDs (`bd-a3f8.1`).

## Design

### 1. Hash-chained event IDs

Replace `E{N}` with a content hash:

```
event_id = "E-" + sha256(parent_event_id || canonical_json(payload) || actor || ts)[:12]
```

- `parent_event_id` is the ID of the previous event *as seen by the writer* — the log
  becomes a hash chain (per-branch, it is linear; across branches, it forks).
- IDs are globally unique without coordination. Two machines can append concurrently
  and merge later with zero collision risk.
- The local SQLite projection keeps a `seq` column **assigned at replay time** for
  ordering and display. `seq` is derived state, never written to the log.

### 2. Hash-suffixed entity IDs

Tasks, features, and claims get collision-free canonical IDs (`T-4f2a9c`), generated
the same way. Human-friendly short aliases (`T001`) become a **local display mapping**
in the projection — stable per machine, never authoritative, never in the log. CLI and
MCP accept either form.

### 3. Git layout and merging

```
.anvil/
  events.jsonl      ← committed (the state)
  state.db          ← gitignored (disposable projection)
  .gitattributes    ← events.jsonl merge=union
```

- `merge=union` handles the append-only case: concurrent appends on two branches union
  into one file. Order across the merge point is arbitrary — which is fine, because:
- **Replay becomes order-tolerant**: load all lines, dedupe by event ID (idempotent —
  the same event merged twice applies once), order by hybrid logical clock
  (`(lamport, ts, event_id)` tiebreak), then apply. This replaces today's
  strict-sequence replay.

### 4. Conflict semantics (the interesting 5%)

Most events commute (task created on branch A, different task scored on branch B).
The ones that don't are claims — two branches claiming the same task. Post-merge, the
replay sees both `claim.created` events. Resolution rule:

- Earliest HLC wins the claim; the later claim is materialized as `claim.superseded`
  (a new event appended by the reconciler, preserving the audit trail).
- This is the existing reconciliation engine's job — it already detects and repairs
  drift between sources of truth; merged-branch claim conflicts become one more
  discrepancy kind.

The conflict-group and `check_conflicts` machinery is unchanged — it still prevents
conflicting *local* claims at claim time; the reconciler handles the cross-branch case
that no claim-time check can see.

### 5. Migration

- Schema v4. `anvil migrate --git-events` rewrites the local log with hash IDs
  (emitting an `id_mapping` table for old references), flips the `.gitignore` entries,
  writes `.gitattributes`, and sets `events.storage: git` in config.
- `E{N}` mode remains supported indefinitely for existing projects (`events.storage:
  local`, the default until v2). The replay byte-equality guarantee is preserved
  per-mode.

### 6. What we deliberately do NOT take from Beads

- **Dolt / versioned-SQL backends** — git + JSONL + projection is enough; a second
  database technology is surface area without a user.
- **A background sync daemon** — anvil is invoked, does its work, exits.
  Reconciliation on initialize() covers catch-up.
- **Memory-decay compaction of closed items** — different concern (agent context
  curation, not state). Log growth is handled by snapshot-and-archive instead: a
  `snapshot` event materializes current state, and pre-snapshot events move to
  `events-archive-<date>.jsonl` (committed, but never replayed unless verifying).

## Risks

| Risk | Mitigation |
|---|---|
| Evidence payloads (command output excerpts) now live in git history | Evidence bodies stay in the gitignored evidence buffer; the log stores hashes + paths. Add a pre-commit scrub check for secret-shaped strings in event payloads |
| Repo size growth on busy projects | Snapshot-and-archive compaction (above); payload excerpts capped (already enforced by Pydantic validators) |
| Order-tolerant replay is a behavior change to the engine's core | Phase it: keep strict replay for `local` mode; order-tolerant replay only in `git` mode; replay-equivalence tests run both modes over the same fixture corpus |
| `merge=union` duplicating a line on weird merges | Replay dedupes by event ID; duplication is harmless by construction |

## Phasing

- **Phase A (ship first):** hash event IDs + `merge=union` + dedup/HLC replay, opt-in
  via config. Single-claimer-per-branch is documented as the expectation.
- **Phase B:** reconciler learns `claim.superseded` for cross-branch claim conflicts.
- **Phase C:** actor identity across machines (who is `agent-3` on another laptop?) —
  needed before true multi-machine fleets; out of scope until A/B prove out.

## Decision log

- 2026-06-10: CLI/MCP dispatch unification investigated and **rejected** — both
  surfaces are already thin wrappers over shared managers (ClaimManager et al.,
  70-100% reuse per operation). A unified dispatch layer would add ~200 lines of
  indirection to save ~50 lines of boilerplate. Investment goes to the shared managers
  and to this spec instead. (One real divergence found and tracked separately: MCP
  `score_tasks` is deterministic-only while the CLI `expand` path supports `--use-llm`.)
