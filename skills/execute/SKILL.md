---
name: execute
description: Run the agentic execution loop on a claimed anvil task — fetch the work packet, do the work, submit completion evidence. Use this skill when an agent has just claimed a task and needs to execute it end-to-end without juggling individual CLI commands.
---

# Execute — Claim to Submit in One Loop

Carry a `ready` task all the way to `needs_review`: fetch the work packet, read it in full, do the work, heartbeat the lease, run verification, and submit evidence. Nothing moves to `needs_review` without passing through here.

---

## When to Use

- After `anvil claim TASK_ID` has succeeded — claim ID and branch are in hand.
- For execution: one task, one branch, straight to submit.

**Do not use this skill to inspect the queue without taking work** — use `/anvil:state-ops`. Do not use it to make the ship decision on completed tasks — that is `/anvil:finish`.

---

## Prerequisites

An active claim by the current actor on `TASK_ID`. Verify before proceeding:

```bash
anvil list --status claimed
```

If the task does not appear, claim it first via `/anvil:claim`. Commands used in this skill:

| Command | Role |
|---|---|
| `anvil packet TASK_ID` | render the work packet |
| `anvil submit TASK_ID` | record completion evidence |
| `anvil apply TASK_ID` | human review gate |
| `anvil renew CLAIM_ID` | extend the lease heartbeat |
| `anvil release CLAIM_ID` | return the task to the pool |

---

## Workflow

### Step 1 — Fetch the work packet

```bash
anvil packet TASK_ID
```

Example:

```bash
anvil packet T012
```

The CLI echoes where it wrote the file (`Wrote packet to <path>`). That path lives under the active state layout (the HOME workspace by default, e.g. `~/.anvil/workspaces/<key>/.anvil/packets/T012.md`), not necessarily in-repo. The packet holds the full operating context for this task: goal, acceptance criteria, likely files in scope, constraints, verification commands, and the update protocol. It is a derived view regenerated from canonical state, so it reflects the current snapshot, not a cached copy.

When present, read the packet’s **Active PRD assumptions** section as a
declared constraint. An explicit autonomous delegation permits work within
those bounded premises; it does not permit inventing new scope, obtaining
external authority, or bypassing approval/evidence gates. If execution exposes
an unstated premise with no bounded safe default, release the claim and surface
it rather than silently broadening the work.

Read the packet immediately after fetching it. The acceptance criteria in the packet are the contract that `submit` validates against. Skipping the packet and working from memory or from `show TASK_ID` output risks submitting evidence that misses a required item.

To get the JSON form instead (useful when another tool or agent consumes the packet programmatically):

```bash
anvil packet T012 --format json
```

---

### Step 2 — Confirm scope before writing code

Before touching any file, confirm:

1. The acceptance criteria are concrete and independently verifiable — not aspirational descriptions.
2. The `likely_files` list in the packet does not overlap with files another active claim owns. If overlap exists, resolve it via `/anvil:state-ops` before editing.
3. All acceptance criteria are unambiguous. If any are unclear, release the claim now rather than discovering the problem at submit time:

```bash
anvil release CLAIM_ID --reason "acceptance criteria ambiguous on T012 item 3"
```

Ask the user to clarify, update the PRD, re-parse, and re-claim once the criteria are concrete.

This check costs one minute. A wrong interpretation discovered at submit costs the full lease window plus rework.

---

### Step 3 — Do the work

Do the work directly in this session. Read the work packet, implement against the acceptance criteria, and run the verification commands yourself (Step 5) when the implementation is complete.

---

### Step 3a — Implementation discipline

Work where the claim lives: if the claim created a worktree (the default under
`worktree_isolation: require`, or `--worktree`), do ALL edits inside that
worktree directory — never in the shared checkout, where a concurrent loop's
edits can collide with yours.

Commit incrementally to the claim's branch:

```
agent/t012-add-retry-backoff
```

(Or whatever branch prefix the project configured. The `branch_prefix` config key is host-project-configurable in the active layout's `config.yaml`; the default is `agent/`, and `anvil claim` echoes the actual branch on its `Branch:` line.)

Incremental commits create a recoverable trail. If the agent session is interrupted, the commits survive on the branch and the work does not need to restart from zero.

Two hooks run automatically during this step — no manual action required:

**`check-claim.sh`** (PreToolUse on Edit, Write, NotebookEdit) — warns whenever any active claim exists, prompting a check that the file being modified is within this claim's `likely_files` scope. The warning is non-blocking: the edit proceeds. Heed the warning; file overlap with another claim creates a merge conflict that is painful to resolve after submit.

**`record-file-change.sh`** (PostToolUse on Edit, Write, NotebookEdit) — appends a `file_changed` event to `events.jsonl` for every file touched. This populates the `files_changed` list that `submit` reads and includes in the Evidence row. Do not maintain a manual list; the hook tracks it.

---

### Step 4 — Heartbeat the lease during long work

The default lease is 60 minutes. For sessions longer than 55 minutes, renew before the lease expires:

```bash
anvil renew CLAIM_ID
```

Example:

```bash
anvil renew C004
```

Renewing extends `lease_expires_at` by another 60 minutes from now and updates `last_heartbeat_at`. Run this every 5 minutes during active work — set a timer at the start of a long session. A missed heartbeat does not immediately lose the claim; the stale detector fires on the next CLI or MCP operation. Once the lease has expired, the task returns to `ready` and another agent can claim it mid-work.

```
Renewed claim 'C004'.
  New lease until: 2026-05-25T14:35:00.000000+00:00
  Last heartbeat:  2026-05-25T13:35:00.000000+00:00
```

Only the owning actor can renew. To check remaining lease time without renewing:

```bash
anvil list --status claimed
```

The output includes `lease_expires_at` for each active claim.

---

### Step 5 — Run verification before submitting

Execute the task's `verification.commands` from the work packet. The `capture-evidence.sh` hook (PostToolUse Bash) captures stdout, stderr, and exit code from each registered verification command into the claim's pending evidence buffer automatically.

The verification commands are the objective acceptance gate. Submit only when all verification commands exit 0. If a command fails:

1. Read the error output.
2. Fix the code.
3. Re-run the verification command.

Do not submit failing evidence. The Review engine checks `evidence_complete` against the task's `required_evidence` list — missing or failed verification commands are flagged and block `apply`.

For tasks with expensive verification (integration tests, linting over a full codebase), run the cheap unit tests first to catch obvious failures before the slow gate.

---

### Step 6 — Submit the completion

```bash
anvil submit TASK_ID --commands "pytest -x" --files-changed src/foo.py,src/bar.py
```

Additional flags:

```bash
anvil submit T012 \
  --commands "pytest -x,ruff check src/" \
  --files-changed src/anvil/claims/manager.py,src/anvil/cli.py \
  --output-file /tmp/pytest-out.log \
  --pr-url https://github.com/org/repo/pull/42
```

`--commands` and `--files-changed` are both **required** and **repeatable**: pass the flag once per value (one occurrence == one value, so commands or paths with embedded commas survive intact), or pass a single comma-separated occurrence for the simple case shown above. `--output-file` attaches a log file to the Evidence row. `--pr-url` links the branch's PR if one exists.

`submit` does the following atomically:

1. Writes an `Evidence` row to `state.db` with the commands run, files changed, and output excerpt.
2. Auto-releases the claim (`claim.released` event).
3. Transitions the task from `claimed` to `needs_review` (`task.status_changed` event).

The CLI prints the evidence summary immediately:

```
Evidence submitted for task 'T012'.
  Evidence ID:  EV066F22C4
  Claim ID:     C004 (auto-released)
  Submitted by: agent
  Commands:     ['pytest -x', 'ruff check src/']
  Files:        ['src/anvil/claims/manager.py', 'src/anvil/cli.py']

Task 'T012' status → needs_review.
Run `anvil apply T012` when ready for human review.
```

If the task declares `required_evidence` that the submission does not satisfy, the CLI appends an `Evidence gate: INCOMPLETE` block listing the missing items. That is advisory at submit time but blocks a strict `apply`.

Review the printed evidence summary before walking away. If a field looks wrong (wrong file list, missing command), inspect with `anvil show T012` and coordinate with the human reviewer before they invoke `apply`.

---

### Step 7 — Wait for apply

`anvil apply TASK_ID` is a human-only step. The task stays in `needs_review` until the human reviewer invokes it. The `/anvil:finish` skill drives that decision — surfacing the evidence, picking a disposition (accept, reject, hold, discard), and running `apply`.

Until `apply` is called:

- The branch persists on the claim's git branch.
- The task is visible in `anvil list --status needs_review`.
- No other agent can re-claim the task.

No action required here. Proceed to the next task in the queue:

```bash
anvil next
```

---

## Edge Cases

**Verification fails mid-work**: do not submit. Fix and re-run. The packet's `verification.commands` are the contract; submit only when they all exit 0.

**Claim went stale mid-work**: the task has returned to `ready`. Re-claim it:

```bash
anvil claim T012
```

The branch's commits are preserved on `agent/t012-<slug>`. Continue work on the same branch; the new claim ID replaces the expired one. If another agent claimed the task in the window between expiry and re-claim, coordinate via `anvil show T012` to check the current holder.

**Need to abandon**: release the claim so the task returns to the pool:

```bash
anvil release CLAIM_ID --reason "blocked on upstream T009 — not merged yet"
```

The `--reason` string is stored in the Claim row and logged in `events.jsonl`. Another agent picks up the task via `anvil next`.

**Packet is stale**: if the PRD was revised after the packet was generated, re-fetch:

```bash
anvil packet T012
```

The command overwrites the previous packet file (the CLI re-echoes `Wrote packet to <path>`). Re-read the packet before continuing.

---

## Common Pitfalls

- **Working from memory instead of the packet.** `anvil show TASK_ID` is a summary; the packet is the full operating context. Always read the packet before writing code.
- **Submitting without running all verification commands.** The Review engine checks completeness at `apply` time. Submitting partial evidence delays the ship decision and may require reopening the task.
- **Skipping heartbeats on sessions longer than 55 minutes.** Leases expire silently. Set a timer at session start and renew every 5 minutes.
- **Manually tracking files changed.** The `record-file-change.sh` hook tracks this automatically from every Edit, Write, and NotebookEdit call. Pass the hook-tracked list to `--files-changed`; do not reconstruct it by hand.

---

## Composition with Other Skills

| Position | Skill |
|---|---|
| Before this skill | `/anvil:claim` — active claim required before execute starts |
| If scope is ambiguous before step 3 | Return to `/anvil:state-ops` to inspect conflicts; resolve before editing |
| If `complexity >= 4` at packet read | Return to `/anvil:plan` — the task should have been expanded; release claim first |
| After submit | `/anvil:finish` drives the apply step and ship decision |
| If task returns nothing from `next` after submit | `/anvil:state-ops` to diagnose queue state |

---

## Surface Notes

Every command in this loop ships in the current engine. The execution surface is:

| Surface | Where |
|---|---|
| `anvil packet TASK_ID` | renders the packet; `--format json` for the machine form |
| `anvil submit TASK_ID` | records evidence and auto-releases the claim |
| `anvil apply TASK_ID` | human review gate (accept / reject) |
| `anvil conflicts` | persisted conflict groups (overlapping likely_files) |
| `capture-evidence.sh` hook | PostToolUse Bash; buffers verification output |
| `check-claim.sh` hook | PreToolUse Edit/Write/NotebookEdit; warns per claim's likely_files scope |
| MCP `generate_work_packet` | the packet over MCP for tools/agents that consume it programmatically |
