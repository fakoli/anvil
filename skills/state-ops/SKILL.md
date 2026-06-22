---
name: state-ops
description: Inspect anvil project state — list tasks, show task details, find the next claimable task, summarize active claims and blockers, check file-conflict warnings, and reconcile state with the filesystem and git. Use this skill when you want to see what anvil knows without changing anything.
---

# State-Ops — State Inspection

Read the canonical SQLite state that anvil maintains: project summary, task inventory, claim activity, dependency graph, conflict warnings, and filesystem reconciliation. This skill makes no mutations.

---

## When to Use

- When orienting before starting a work session — run `status` first, always.
- Before claiming a task — confirm the PRD is approved and the task is `ready`.
- When a claim was interrupted and the state of the queue is unclear.
- When multiple agents are active and conflict risk is non-trivial.
- When a task shows as `blocked` and the blocker chain needs tracing.
- When suspicious that orphan branches or stale packets exist on disk.
- When reporting project progress to a human or another skill.

State-ops is NOT for authoring or reviewing PRDs — use the `prd` skill. State-ops is NOT for generating plans or scoring tasks — use the `plan` skill. State-ops is NOT for claiming work or submitting evidence — use the `claim` and `execute` skills. This skill only reads.

---

## Prerequisites

The project must have run `anvil init` at least once. Check with `anvil status`,
which is **layout-aware** — it resolves the default HOME workspace
(`~/.anvil/workspaces/…`) OR a local in-repo `.anvil/`. A raw `ls .anvil/state.db`
is WRONG under the default workspace layout (state is not in the repo) and will
report "missing" even when the project is initialized:

```bash
anvil status >/dev/null 2>&1 || echo "MISSING: run anvil init first"
```

If `anvil status` exits non-zero (it prints `not initialized`), refuse to proceed
and tell the caller to run:

```bash
anvil init --name "<project-name>"
```

`anvil status` is the only command to run before init is confirmed — do not list
tasks or mutate state until it reports the project is initialized.

---

## Workflow

### Step 1 — Get the project summary

**Phase 2 — available now.**

```bash
anvil status
```

Returns: PRD status (`draft` / `reviewed` / `approved`), total task count, per-status breakdown (`proposed`, `drafted`, `reviewed`, `ready`, `claimed`, `in_progress`, `done`), active claim count, blocker count, and sync state.

Run this first. The output orients every subsequent decision — it answers whether work is even possible before touching any other command.

Key signals to look for:

- `prd-status: draft` — the claim gate is closed. No task can be claimed until the PRD reaches `approved`. Proceed to the `prd` skill instead.
- `ready-tasks: 0` — all tasks are either upstream of ready or already active. Check blockers or the plan skill.
- `blockers: N` where N > 0 — identify which tasks are blocked before picking new work.
- `active-claims: N` — determine whether adding another claim creates conflict risk.

### Step 2 — List tasks by filter

**Phase 3 — pending.**

```bash
anvil list [--status STATUS] [--feature FEATURE_ID]
```

Returns: a table of `TaskID`, title, status, priority, and `agent_suitability` score (1-5).

Filters:

- `--status ready` — tasks available to claim right now.
- `--status in_progress` — tasks currently under active claims.
- `--feature F001` — all tasks scoped to a specific feature.

Use this to audit plan coverage, pick the next work item by priority, or confirm a feature is fully drafted before raising it for review.

### Step 3 — Drill into a specific task

**Phase 3 — pending.**

```bash
anvil show TASK_ID
```

Example:

```bash
anvil show T012
```

Returns: full task detail including title, intent, acceptance criteria, six-dimension scores (`complexity`, `parallelizability`, `context_load`, `blast_radius`, `review_risk`, `agent_suitability`), `expected_files`, dependencies, and current claim status (agent, lease expiry, heartbeat).

Run `show` before claiming to confirm:

- The acceptance criteria are concrete and independently verifiable.
- `complexity` is under 4 (if 4+, expand first via the `plan` skill).
- No active claim already covers this task.
- `expected_files` does not overlap files currently claimed by another agent.

### Step 4 — Find the next claimable task

**Phase 4 — pending.**

```bash
anvil next
```

Returns: the single highest-priority `ready` task that has no unmet dependencies and no file conflicts with currently-active claims.

This is the standard agent-loop entry point. Run it instead of manually scanning `list` output when the goal is simply to find work. After `next` returns a task ID, run `show TASK_ID` to read the full detail before claiming.

If `next` returns nothing, check `status` — the queue is either empty, fully claimed, or PRD-gated.

### Step 5 — Check for conflicts

**Phase 5 — pending.**

```bash
anvil conflicts
```

Returns: conflict groups — sets of active claims whose `expected_files` overlap. Each group lists the claim IDs, the overlapping file paths, and the agents holding each claim.

Run this proactively before claiming a task that touches shared files (e.g., a module's `__init__.py`, a schema file, or a config). A conflict warning at this step is cheaper than a merge conflict later.

`anvil claim` also warns on overlap at claim time, but `conflicts` gives the full picture across all active claims, not just the one being attempted.

### Step 6 — Reconcile with filesystem and git

**Phase 8 — pending.**

```bash
anvil sync
```

Returns: a reconciliation report listing orphans — branches that exist in git but have no corresponding claim in `state.db`, packets in `.anvil/packets/` with no matching task, and claims with expired leases that were never force-released.

Run this after a session ends abruptly, after a force-push cleans up stale branches, or periodically during long-running projects to keep state clean.

To apply fixes interactively:

```bash
anvil sync --fix
```

The `--fix` flag prompts before each remediation. It does not delete data silently. Pass `--yes` to auto-confirm when running in a non-interactive context (confirm this is safe first).

---

## Hook-Friendly Output

The `SessionStart` hook (`detect-state.sh`) calls `status` in compact form for machine consumption:

```bash
anvil status --hook-format
```

Emits exactly one line:

```
active-claims:N ready-tasks:N blockers:N prd-status:STATUS
```

Where `STATUS` is one of: `none` (no PRD found), `draft`, `reviewed`, or `approved`.

Parse this format when reading `status` from a hook or another skill. Do not parse the human-readable `status` output — its layout may change. The `--hook-format` contract is stable.

Example of a healthy project at the start of an execute session:

```
active-claims:0 ready-tasks:4 blockers:0 prd-status:approved
```

Example of a PRD-gated project:

```
active-claims:0 ready-tasks:8 blockers:0 prd-status:draft
```

The second example blocks claiming even though `ready-tasks` is non-zero — the PRD gate is enforced by the claims manager, not by `ready-tasks` count.

---

## Common Pitfalls

- **Claiming before PRD is approved.** The claims manager enforces this gate — `anvil claim` will error if `prd-status` is not `approved`. Run `status` first and check `prd-status` before attempting any claim.
- **Manually editing `state.db`.** Do not use sqlite3 directly on `state.db` to fix state. Every mutation should go through the CLI so the change is recorded in `events.jsonl`. Manual edits produce state that cannot be replayed or audited.
- **Assuming stale claims block the queue.** Stale leases are detected and cleared automatically on the next CLI or MCP operation — no manual intervention is needed. Wait one cycle (run any `anvil` command) and the task returns to `ready`.
- **Confusing `conflicts` (file overlap) with `blockers` (dependency blockers).** `status` reports both separately. `blockers` are tasks stuck in `blocked` status due to unmet task dependencies. `conflicts` are active claims that overlap on files. Address them differently.
- **Running `sync --fix --yes` without reading the report first.** Run `sync` (without `--fix`) first to read the orphan report, then decide whether auto-remediation is appropriate.

---

## Composition with Other Skills

State-ops fits into a repeating inspection-then-action cycle:

| Sequence | Skill |
|---|---|
| Nothing precedes state-ops | State-ops is read-only and safe to run at any point in any session |
| `status` shows `prd-status: draft` | Proceed to the `prd` skill to author or approve the PRD |
| `status` shows `prd-status: approved`, `ready-tasks: 0` | Proceed to the `plan` skill — tasks may need scoring, expand, or review-to-ready promotion |
| `list` or `next` returns a task ID | Proceed to the `claim` skill (Phase 4) to take ownership |
| `show TASK_ID` reveals `complexity ≥ 4` | Return to the `plan` skill — run `anvil expand TASK_ID` before claiming |
| `conflicts` shows overlap | Resolve the conflict first (wait for the other claim to complete, or coordinate with the other agent) before proceeding to `claim` |

State-ops is the starting point of every agent work session. It answers "what is true right now" before any skill decides "what to do next."

---

## Sync operations

Phase 8 ships bidirectional sync. See [`docs/github-sync.md`](../../docs/github-sync.md) for the full CLI reference, provider variants, and conflict-resolution strategies.

---

## Phase 2 Limitations

The current branch (`feat/anvil-phase-2-state-engine`) ships only two working CLI commands. All other commands listed in this skill will error with `command not found` or `NotImplementedError` until their respective phases land.

### Working in Phase 2

| Command | Status |
|---|---|
| `anvil init` | Phase 2 — available |
| `anvil status` | Phase 2 — available |
| `anvil status --hook-format` | Phase 2 — available |
| `anvil status --cwd PATH` | Phase 2 — available |

### Pending (do not invoke)

| Command | Target Phase |
|---|---|
| `anvil list` | Phase 3 |
| `anvil show TASK_ID` | Phase 3 |
| `anvil next` | Phase 4 |
| `anvil claim TASK_ID` | Phase 4 |
| `anvil conflicts` | Phase 5 |
| `anvil sync` | Phase 8 |

During Phase 2, the full state-ops workflow collapses to: run `anvil status`, read the output, and proceed to whichever skill the summary indicates. Do not call any command not in the working list — they will error and produce no useful output.
