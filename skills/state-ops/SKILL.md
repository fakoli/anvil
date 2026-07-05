---
name: state-ops
description: Inspect anvil project state — list tasks, show task details, find the next claimable task, summarize active claims and blockers, check file-conflict warnings, and reconcile state with the filesystem and git. Use this skill when you want to see what anvil knows without changing anything.
---

# State-Ops — State Inspection

Read the canonical SQLite state that anvil maintains: project summary, task inventory, claim activity, dependency graph, conflict warnings, and filesystem reconciliation. This skill makes no mutations.

---

## When to Use

- When orienting before starting a work session — run `status` first, always.
- Before claiming a task — confirm the PRD has passed review (`reviewed` or `approved`) and the task is `ready`.
- When a claim was interrupted and the state of the queue is unclear.
- When multiple agents are active and conflict risk is non-trivial.
- When a task shows as `blocked` and the blocker chain needs tracing.
- When suspicious that orphan branches or stale packets exist on disk.
- When reporting project progress to a human or another skill.

State-ops is NOT for authoring or reviewing PRDs — use the `prd` skill. State-ops is NOT for generating plans or scoring tasks — use the `plan` skill. State-ops is NOT for claiming work or submitting evidence — use the `claim` and `execute` skills. This skill only reads.

---

## Prerequisites

The project must have run `anvil init` at least once. State lives in the layout anvil chose (the HOME workspace by default, `~/.anvil/workspaces/<key>/.anvil/...`), not in the repo, so check init through the CLI rather than a literal path:

```bash
anvil status >/dev/null 2>&1 || echo "MISSING: run anvil init first"
```

If it reports `MISSING`, refuse to proceed and tell the caller to run:

```bash
anvil init --name "<project-name>"
```

Do not attempt to read state, list tasks, or call any other `anvil` command until `anvil status` confirms the project is initialized.

---

## Workflow

### Step 1 — Get the project summary

```bash
anvil status
```

Plain `anvil status` prints a human-readable summary like:

```
anvil for "myproject" (id: myproject)
Path: /Users/you/.anvil/workspaces/myproject/.anvil
Initialized: 2026-06-22T06:19:23.815479Z

PRD:           approved
Tasks:         2 total (2 ready, 0 in_progress, 0 blocked)
Active claims: 0
Sync:          off
Schema:        6
```

The `Path:` line tells you where state actually lives for this project (the HOME
workspace by default, an in-repo `.anvil/` only under `ANVIL_STATE_LAYOUT=local`).
To inspect a **different** project's state, pass `--cwd <project-dir>` — the flag
points at the *project* directory anvil derives the workspace from. **There is no
`--workspace` flag:** never point a flag at the `~/.anvil/workspaces/<key>/`
path directly (`anvil status --workspace <path>` fails with `No such option
'--workspace'`); select state by project via `--cwd`, and read the resolved
location off the `Path:` line.
`PRD:` is one of `none`, `draft`, `reviewed`, or `approved`. The `Tasks:` line gives
the total plus the ready / in_progress / blocked counts.

Run this first. The output orients every subsequent decision — it answers whether work is even possible before touching any other command.

Key signals to look for:

- `PRD: draft` — the claim gate is closed. No task can be claimed until the PRD reaches `reviewed` or `approved`. Proceed to the `prd` skill instead.
- `Tasks: N total (0 ready, ...)` — all tasks are either upstream of ready or already active. Check blockers or the plan skill.
- `(... 0 blocked)` becoming non-zero — identify which tasks are blocked before picking new work.
- `Active claims: N` — determine whether adding another claim creates conflict risk.

For a single-line machine-readable form (with the `prd-status:` / `ready-tasks:` /
`blockers:` / `active-claims:` tokens), use `anvil status --hook-format` — see
[Hook-Friendly Output](#hook-friendly-output) below.

### Step 2 — List tasks by filter

```bash
anvil list [--status STATUS] [--feature FEATURE_ID] [--type TYPE]
```

Prints a table with columns `TaskID | Title | Status | Priority | Type | Score | Feature` (the `Score` column is `complexity/agent_suitability`, e.g. `2/2`, or `unscored` until `anvil score` runs):

```
TaskID  Title                       Status  Priority  Type             Score  Feature
-------------------------------------------------------------------------------------
T001    Implement argument parsing  ready   high      feature            2/2  F001
T002    Implement list output       ready   medium    feature            2/2  F001

2 task(s) listed.
```

Filters:

- `--status ready` — tasks available to claim right now.
- `--status in_progress` — tasks currently under active claims.
- `--feature F001` — all tasks scoped to a specific feature.
- `--type feature|bugfix|refactor|modify` — tasks of a single type.

Use this to audit plan coverage, pick the next work item by priority, or confirm a feature is fully drafted before raising it for review.

### Step 3 — Drill into a specific task

```bash
anvil show TASK_ID
```

Example:

```bash
anvil show T001
```

Returns: full task detail in multi-section format — title, feature, status, priority, the six-dimension scores (`complexity`, `parallelizability`, `context_load`, `blast_radius`, `review_risk`, `agent_suitability`) with an explanation, dependencies, conflict groups, acceptance criteria, verification commands, the task `Likely Files`, active claims (agent, lease, heartbeat — `(none)` if unclaimed), and recent events. The task's narrative body is its `description`.

Run `show` before claiming to confirm:

- The acceptance criteria are concrete and independently verifiable.
- `complexity` is under 4 (if 4+, expand first via the `plan` skill).
- No active claim already covers this task.
- The `Likely Files` do not overlap files currently claimed by another agent (cross-check with `anvil conflicts`).

### Step 4 — Find the next claimable task

```bash
anvil next
```

Returns the single highest-priority claimable task without claiming it:

```
Next recommended task: T001
  Title:    Implement argument parsing
  Priority: high
  Complexity: 2

Run `anvil claim T001` to acquire the lease.
```

This is the standard agent-loop entry point. Run it instead of manually scanning `list` output when the goal is simply to find work. After `next` returns a task ID, run `show TASK_ID` to read the full detail before claiming.

If `next` finds nothing claimable, it prints that the queue is empty (exit 0 — an empty queue is not an error; with `-q`/`--quiet` it prints nothing and exits 3 instead). Check `status` — the queue is either empty, fully claimed, or PRD-gated.

### Step 5 — Check for conflicts

```bash
anvil conflicts
```

Returns the persisted conflict groups — sets of tasks whose `likely_files` overlap. Each group lists its ID, the member task IDs, and the shared files:

```
1 conflict group(s):

CG-T001-T002: T001, T002
  Tasks T001 and T002 share overlapping files: src/cli.py
```

These groups are produced by planning inference and persisted to the `conflict_groups` table; `conflicts` reads them back (read-only and deterministic). Run it proactively before claiming a task that touches shared files (e.g., a module's `__init__.py`, a schema file, or a config). A conflict warning at this step is cheaper than a merge conflict later.

`anvil claim` also warns on overlap at claim time, but `conflicts` gives the full picture across all groups, not just the one being attempted.

### Step 6 — Reconcile with filesystem and git

```bash
anvil sync
```

Bare `anvil sync` runs reconciliation and returns a report of orphans — branches that exist in git but have no corresponding claim in anvil's state, work packets with no matching task, and claims with expired leases that were never force-released. On a clean project it prints:

```
Reconciliation scanned at 2026-06-22T06:21:17.805045+00:00
  No discrepancies found.
```

For a purely local report (no external sync provider, always exits 0), `anvil drift` covers the same ground read-only: orphan branches, orphan worktrees, orphan packets, stale claims, and tasks whose plan-declared files have vanished from disk.

Run reconciliation after a session ends abruptly, after a force-push cleans up stale branches, or periodically during long-running projects to keep state clean.

To apply fixes:

```bash
anvil sync --fix
```

The `--fix` flag applies each suggested fix; in a non-interactive context it requires `--yes` to skip the confirmation prompt (confirm this is safe first). Run `anvil sync` without `--fix` first to read the report.

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

Where `STATUS` is one of: `none` (project initialized, no PRD yet), `draft`, `reviewed`, or `approved`. When the project itself is not initialized for this directory, `--hook-format` prints the single literal token `uninitialized` instead of the four-token line (and still exits 0, so a hook never fails the session).

Parse this format when reading `status` from a hook or another skill. Do not parse the human-readable `status` output — its layout may change. The `--hook-format` contract is stable.

Example of a healthy project at the start of an execute session:

```
active-claims:0 ready-tasks:4 blockers:0 prd-status:approved
```

Example of a PRD-gated project:

```
active-claims:0 ready-tasks:8 blockers:0 prd-status:draft
```

The second example blocks claiming even though `ready-tasks` is non-zero — the PRD gate is enforced by the claims manager, not by the `ready-tasks` count. A `draft` (or `rejected`) PRD blocks; `reviewed` or `approved` clears the gate.

---

## Common Pitfalls

- **Claiming before the PRD passes review.** The claims manager enforces this gate — `anvil claim` errors while the PRD is `draft` or `rejected`. A `reviewed` or `approved` PRD clears the gate. Run `anvil status` first and check the `PRD:` line before attempting any claim.
- **Manually editing the state database.** Do not open the SQLite `state.db` directly to fix state. Every mutation should go through the CLI so the change is recorded in the event log. Manual edits produce state that cannot be replayed or audited.
- **Assuming stale claims block the queue.** Stale leases are detected and cleared automatically on the next CLI or MCP operation — no manual intervention is needed. Wait one cycle (run any `anvil` command) and the task returns to `ready`.
- **Confusing `conflicts` (file overlap) with `blockers` (dependency blockers).** `status` reports both separately. `blockers` are tasks stuck in `blocked` status due to unmet task dependencies. `conflicts` are active claims that overlap on files. Address them differently.
- **Running `sync --fix --yes` without reading the report first.** Run `sync` (without `--fix`) first to read the orphan report, then decide whether auto-remediation is appropriate.

---

## Composition with Other Skills

State-ops fits into a repeating inspection-then-action cycle:

| Sequence | Skill |
|---|---|
| Nothing precedes state-ops | State-ops is read-only and safe to run at any point in any session |
| `status` shows `PRD: draft` | Proceed to the `prd` skill to author and review the PRD |
| `status` shows `PRD: approved` (or `reviewed`) with `0 ready` tasks | Proceed to the `plan` skill — tasks may need scoring, expand, or review-to-ready promotion |
| `list` or `next` returns a task ID | Proceed to the `claim` skill to take ownership |
| `show TASK_ID` reveals `complexity` of 4 or more | Return to the `plan` skill — run `anvil expand TASK_ID` before claiming |
| `conflicts` shows overlap | Resolve the conflict first (wait for the other claim to complete, or coordinate with the other agent) before proceeding to `claim` |

State-ops is the starting point of every agent work session. It answers "what is true right now" before any skill decides "what to do next."

---

## Sync operations

Beyond local reconciliation, `anvil sync github` (and `anvil sync provider <id>`) push/pull against an external provider. See [`docs/github-sync.md`](../../docs/github-sync.md) for the full CLI reference, provider variants, and conflict-resolution strategies.
