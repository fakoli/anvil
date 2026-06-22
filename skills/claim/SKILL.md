---
name: claim
description: Acquire an exclusive lease on an anvil task — pick from the ready queue, check for file conflicts, claim the task, and get a working git branch to commit into. Use this skill when ready to start work on an approved task.
---

# Claim — Acquire an Exclusive Lease

Turn a `ready` task into an active claim: a persisted claim record with a 60-minute lease, a branch checked out, and hooks watching every file touch. This is the entry point to the agentic execution loop. Nothing moves to `claimed` without going through here.

---

## When to Use

- Starting work on a task after `/anvil:plan` has produced a ready queue.
- When resuming after an interrupted session — check `anvil status` first, then re-claim if the previous lease has expired and the task returned to `ready`.
- When coordinating parallel agents — each agent claims a separate task; `claim` enforces the conflict gate.

**Do not use this skill to inspect the queue without taking work**; use `/anvil:state-ops` for that. Do not use this skill to submit completed work; that is the `finish` skill.

---

## Prerequisites

anvil must be initialized and the PRD must be in `reviewed` or `approved` status. Confirm before proceeding:

```bash
anvil status >/dev/null 2>&1 || echo "MISSING: run anvil init first"
anvil status
```

Plain `anvil status` prints a `PRD:` line; look for `PRD:           reviewed` or `PRD:           approved`. The claim gate enforces this: `anvil claim` raises `ClaimError` when the PRD is in `draft`, `rejected`, or absent. `reviewed` and `approved` both pass. If the PRD is still `draft`, run `anvil prd review` (or `--approve`) first, or proceed to `/anvil:prd`.

Commands used in this skill (all ship today; confirm any with `anvil <cmd> --help`):

| Command | Purpose |
|---|---|
| `anvil next` | Pick the highest-priority claimable task without claiming it |
| `anvil claim TASK_ID` | Acquire an exclusive lease and create a branch |
| `anvil release CLAIM_ID` | Release a claim, returning the task to `ready` |
| `anvil renew CLAIM_ID` | Extend the lease heartbeat |
| `anvil show TASK_ID` | Print full task detail |
| `anvil list --status ready` | List the ready queue |

Git is optional. When a git repo is present in the project root, `claim` automatically creates the branch `agent/<task_id_lower>-<slug>`. Without git, claim still succeeds: the record is written to state and the branch field is left `null`.

---

## Workflow

### Step 1 — See what is claimable

Invoke `anvil next` yourself — via Bash, the MCP equivalent when available, or whichever execution primitive the runtime exposes. Surface the result inline:

```bash
anvil next
```

Returns the single highest-priority `ready` task with no unmet dependencies and no conflict-group overlap with currently active claims. Priority ordering: `critical` > `high` > `medium` > `low`; ties broken by complexity ascending (simpler first), then `created_at` ascending (oldest first).

If the user wants to see the full ready queue rather than the top pick, run `anvil list --status ready` yourself and present it.

If `next` returns nothing, the queue is empty, fully claimed, or PRD-gated. Run `anvil status` yourself and diagnose inline: read the `PRD:` line, the `Tasks:` line (`N total (M ready, ...)`), and the `Active claims:` count, then tell the user what's blocking and what to do about it. A non-zero ready count alongside a `PRD:` that is neither `reviewed` nor `approved` means the PRD gate is blocking all claims; the ready count is accurate, but the gate is closed.

---

### Step 2 — Inspect the task before claiming

Once `next` returns a candidate (or the user picks one from the ready queue), run `anvil show TASK_ID` yourself and present the full task detail in chat:

```bash
anvil show T012
```

Returns: title, feature, status, priority, all six score dimensions (`complexity`, `parallelizability`, `context_load`, `blast_radius`, `review_risk`, `agent_suitability`) with explanation, dependency chain, conflict groups, acceptance criteria, verification commands, `Likely Files`, any active claim on this task, and recent events.

Audit the result inline and surface anything that should give the user pause before the claim fires:

- The acceptance criteria are concrete and independently verifiable — not aspirational descriptions.
- `complexity` is 3 or under. A score of 4 or 5 means the task should have been expanded via `anvil expand --use-llm` during planning. Claiming an oversized task and then abandoning it mid-way wastes the lease window.
- `agent_suitability` matches the current executor. A score of 1 or 2 signals that the task requires architectural judgment, significant human context, or decisions that a model is likely to get wrong. Defer those tasks.
- `Likely Files` does not include files that look like they belong to a different subsystem, which would be a sign the task scope drifted during authoring.

Present the inspection summary and ask:

> T012 looks claimable: acceptance criteria concrete, complexity 3, agent_suitability 4, likely files scoped to `src/retry/`. Proceed to claim? (yes / show me more / pick a different task)

This step costs nothing and prevents the most common source of wasted claims.

---

### Step 3 — Check for conflicts

Invoke `anvil claim TASK_ID` yourself once the user confirms. The command performs the conflict check before writing anything:

```bash
anvil claim T012
```

The manager checks two conflict conditions before issuing the lease:

1. **File overlap**: another active claim by a different actor has at least one file in common with the `likely_files` of T012.
2. **Conflict group**: T012 belongs to a `conflict_group` that already has an active claim on a sibling task.

If either condition is true and `--force` is not passed, `claim` refuses (exit 1) and prints the overlapping claim ID, the other actor's identity, and the overlapping files. Example:

```
Warning: task 'T012' has file conflicts with active claims:
  Claim C003 by 'agent-scout': overlapping files: ['src/anvil/state/backend.py']
Pass --force to override and claim anyway.
```

Surface the conflict in chat. If it is acceptable — for example, the other actor owns C003 on a read-only research task and T012 writes to a different function in the same file — ask the user explicitly before forcing:

> T012 conflicts with C003 (agent-scout) on `src/anvil/state/backend.py`. Force the claim? `--force` is audit-logged. (yes / no / inspect C003 first)

On `yes`, re-run with `--force` yourself:

```bash
anvil claim T012 --force
```

The override is logged as a warning (actor identity, the claim being forced, and the overlapping files), and the resulting `claim.created` event records the claim itself. Every forced claim is auditable.

---

### Step 4 — Acquire the lease

A clean claim (no conflicts, or `--force` accepted) prints the claim result. Surface it inline:

```
Claimed task 'T012' as 'agent'.
  Claim ID:    C0FCF72C8
  Lease until: 2026-05-24T19:00:00.000000+00:00
  Branch:      agent/t012-add-retry-backoff

Run `anvil renew C0FCF72C8` to extend the lease before it expires.
```

The actor defaults to `$USER` (or `agent`); pass `--actor` to override. The task transitions from `ready` to `claimed`, and two events are appended to the event log: `claim.created` and `task.status_changed`.

If the user wants a separate git worktree (useful when running two agents in parallel from the same repo without checkout conflicts), invoke `--worktree` yourself:

```bash
anvil claim T012 --worktree
```

This creates `../wt-t012/` with the branch already checked out. Each worktree is fully independent — no stashing required when switching between tasks.

Without a git repo present, `claim` still succeeds. It prints a warning to stderr and omits the `Branch:` line:

```
Warning: git branch not created — not a git repository
Claimed task 'T012' as 'agent'.
  Claim ID:    C0FCF72C8
  Lease until: 2026-05-24T19:00:00.000000+00:00

Run `anvil renew C0FCF72C8` to extend the lease before it expires.
```

The claim is valid. Work proceeds in the repo root. The branch field on the Claim row is left `null`. Tell the user inline whether a branch was created so there is no surprise later.

---

### Step 5 — Work on the branch

Actual code changes happen here, inside the conversation. The agent makes the edits directly on `agent/t012-add-retry-backoff` (or the worktree branch). Commit incrementally — incremental commits make the eventual PR reviewable and give a recovery point if the session is interrupted.

Two hooks run automatically during this phase (`check-claim.sh` and `record-file-change.sh`). See `/anvil:execute` Step 3a for full hook descriptions.

---

### Step 6 — Heartbeat the lease

The default lease is 60 minutes. For sessions longer than 55 minutes, renew before the lease expires. Invoke renewal yourself whenever the agent notices the timer:

```bash
anvil renew C0FCF72C8
```

Renewing extends `lease_expires_at` by another 60 minutes from now and updates `last_heartbeat_at`. The command errors if the lease is already expired — re-claiming is the only option at that point.

```
Renewed claim 'C0FCF72C8'.
  New lease until: 2026-05-24T20:05:00.000000+00:00
  Last heartbeat:  2026-05-24T19:05:00.000000+00:00
```

Automated agents should renew every 5 minutes. A missed heartbeat does not immediately lose the claim — the lease detector runs on the next CLI or MCP operation. Once the lease expires, the task returns to `ready` and any agent can claim it.

Only the owning actor can renew a claim. To release another actor's stale claim, use `release --force` (Step 8).

---

### Step 7 — Submit when complete

Once verification passes and the work is ready to leave the agent's hands, drive the submit yourself. Read the verification output, summarize what was completed, and ask the user to acknowledge before submitting:

> T012 verification passed: 14 tests added, retry-backoff implemented in `src/retry/backoff.py`, no other files touched. Ready to submit? Submitting transitions T012 to `needs_review` and auto-releases the claim. (yes / not yet / let me re-check)

On `yes`, invoke `anvil submit TASK_ID` yourself. `submit` requires `--commands` (the verification command(s) that were run) and `--files-changed` (the files modified); both are repeatable, so pass each flag once per value:

```bash
anvil submit T012 --commands "pytest tests/test_backoff.py -v" --files-changed "src/retry/backoff.py"
```

It transitions the task to `needs_review`, auto-releases the claim, and prints the evidence record plus a gate check:

```
Evidence submitted for task 'T012'.
  Evidence ID:  EVB5F89694
  Claim ID:     C0FCF72C8 (auto-released)
  Submitted by: agent
  Commands:     ['pytest tests/test_backoff.py -v']
  Files:        ['src/retry/backoff.py']

Task 'T012' status → needs_review.
Run `anvil apply T012` when ready for human review.
```

Tell the user the task is now in `needs_review` and ask whether to hand off to `/anvil:finish` for the apply gate.

**The hard handoff lives in `finish`, not here.** `anvil apply TASK_ID --approve` is the only command in this leg of the lifecycle that requires explicit user confirmation before the agent runs it. That gate lives in `/anvil:finish`; invoke that skill rather than running `apply --approve` from here.

---

### Step 8 — Release explicitly when abandoning

When work must stop before completion — blocked on an upstream issue, deprioritized, or handed off — invoke release yourself so the task returns to the pool. Always ask the user for a reason first so the audit trail is informative:

> Releasing C0FCF72C8. What's the reason? (e.g., "blocked: upstream T009 not merged")

Then run:

```bash
anvil release C0FCF72C8 --reason "blocked: upstream T009 not merged"
```

The CLI confirms with `Released claim 'C0FCF72C8'.` and a `Reason:` line. The reason is stored on the Claim row and logged in the event log. Another agent can then pick up the task via `anvil next`.

To release a claim held by a different actor (use sparingly — logged in audit trail), confirm with the user before forcing:

```bash
anvil release C0FCF72C8 --force
```

`--force` bypasses the actor-ownership check and also allows releasing claims in non-`active` states (e.g., `stale`). Every forced release is recorded with the releasing actor's identity.

---

## Anti-pattern to avoid

The agent drives commands inline; it does not hand the user a numbered CLI to-do list. See `/anvil:plan` for the canonical statement. The only command that requires explicit user confirmation before the agent runs it is `apply --approve` (which lives in `/anvil:finish`).

**When to actually hand off CLI commands:** if the user explicitly opts out ("just give me the commands"), or if the runtime lacks the tool needed to execute them. In those cases, a CLI list is the right output. Otherwise, drive.

For **decision-presentation discipline** — how to surface multi-option choices (e.g., which task to claim, whether to force-claim) — see the canonical description in `/anvil:resolve-decisions`. The same structured Q&A pattern applies here.

---

## Common Pitfalls

- **Claiming while PRD is still `draft`.** The claim gate checks the PRD status and raises `ClaimError` before touching anything else (only `reviewed` or `approved` pass). Run `anvil prd review` (then `--approve` if you want it approved) first.
- **Ignoring the `agent_suitability` score.** Claiming a task scored `1` or `2` with a small or local model burns 60 minutes and produces output that needs complete rework. Check `anvil show TASK_ID` before committing.
- **Skipping the heartbeat on long sessions.** Leases expire silently. The task returns to `ready` and another agent can claim it while work is still in progress. Set a timer and run `anvil renew CLAIM_ID` every 5 minutes.
- **Calling `renew` after the lease has already expired.** `renew` raises `ClaimError` on an expired lease — the lease cannot be extended retroactively. Re-claim the task after it returns to `ready`.

---

## Composition with Other Skills

| Position | Skill |
|---|---|
| Before this skill | `/anvil:plan` must have produced `ready` tasks; optionally `/anvil:state-ops` to read the queue first |
| After claiming | Work the branch, heartbeat, complete; then `/anvil:finish` for submit + ship |
| If `show TASK_ID` reveals `complexity >= 4` | Return to `/anvil:plan` and expand the task (`anvil expand --use-llm`) before claiming |
| If `next` returns nothing | `/anvil:state-ops` to diagnose — check `status`, `list --status drafted`, trace blockers |

---

## Scope notes

Every command named in this skill ships today; confirm any with `anvil <cmd> --help`. The full claim, submit, and apply lifecycle is available: `submit` auto-releases the claim and moves the task to `needs_review`, and `/anvil:finish` drives `apply`.

| Surface | Notes |
|---|---|
| `anvil next` / `anvil claim` / `anvil release` / `anvil renew` | Core claim lifecycle |
| `anvil claim TASK_ID --worktree` | Optional independent worktree at `../wt-<task_id>/` |
| `anvil submit TASK_ID` | Records evidence (`--commands` + `--files-changed` required), auto-releases, moves task to `needs_review` |
| `anvil apply TASK_ID` | Human review gate (`needs_review` → accepted → done, or rejected); lives in `/anvil:finish` |
| `anvil conflicts` | Lists the persisted conflict groups |
| `check-claim.sh` hook (PreToolUse) | Per-file scope check against active claims via `anvil hook check-claim` |
| `record-file-change.sh` hook (PostToolUse) | Records touched files against the active claim |
| PR creation / commit assistance | Out of scope: anvil coordinates work; it does not write code |
