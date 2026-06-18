---
name: claim
description: Acquire an exclusive lease on an anvil task — pick from the ready queue, check for file conflicts, claim the task, and get a working git branch to commit into. Use this skill when ready to start work on an approved task.
---

# Claim — Acquire an Exclusive Lease

Turn a `ready` task into an active claim: a row in `state.db` with a 60-minute lease, a branch checked out, and hooks watching every file touch. This is the entry point to the agentic execution loop. Nothing moves to `claimed` without going through here.

---

## When to Use

- Starting work on a task after `/anvil:plan` has produced a ready queue.
- When resuming after an interrupted session — check `anvil status` first, then re-claim if the previous lease has expired and the task returned to `ready`.
- When coordinating parallel agents — each agent claims a separate task; `claim` enforces the conflict gate.

**Do not use this skill to inspect the queue without taking work** — use `/anvil:state-ops` for that. Do not use this skill to submit completed work — that is the Phase 5 `finish` skill.

---

## Prerequisites

`.anvil/state.db` must exist and the PRD must be in `reviewed` or `approved` status. Confirm before proceeding:

```bash
anvil status
```

Look for `prd-status: approved`. The claim gate enforces this — `anvil claim` raises `ClaimError` when `prd-status` is `draft` or `none`. If the PRD is not approved, proceed to `/anvil:prd` first.

Phase 4 commands used in this skill:

| Command | Phase | Status |
|---|---|---|
| `anvil next` | Phase 4 | available |
| `anvil claim TASK_ID` | Phase 4 | available |
| `anvil release CLAIM_ID` | Phase 4 | available |
| `anvil renew CLAIM_ID` | Phase 4 | available |
| `anvil show TASK_ID` | Phase 3 | available |
| `anvil list --status ready` | Phase 3 | available |

Git is optional. When a git repo is present in the project root, `claim` automatically creates the branch `agent/<task_id_lower>-<slug>`. Without git, claim still succeeds — the record is written to `state.db` and the branch field is left `null`.

---

## Workflow

### Step 1 — See what is claimable

Invoke `anvil next` yourself — via Bash, the MCP equivalent when available, or whichever execution primitive the runtime exposes. Surface the result inline:

```bash
anvil next
```

Returns the single highest-priority `ready` task with no unmet dependencies and no conflict-group overlap with currently active claims. Priority ordering: `critical` > `high` > `medium` > `low`; ties broken by complexity ascending (simpler first), then `created_at` ascending (oldest first).

If the user wants to see the full ready queue rather than the top pick, run `anvil list --status ready` yourself and present it.

If `next` returns nothing, the queue is empty, fully claimed, or PRD-gated. Run `anvil status` yourself and diagnose inline — read `prd-status`, `ready-tasks`, and `active-claims`, then tell the user what's blocking and what to do about it. A non-zero `ready-tasks` count alongside a non-`approved` `prd-status` means the PRD gate is blocking all claims — the ready count is accurate, but the gate is closed.

---

### Step 2 — Inspect the task before claiming

Once `next` returns a candidate (or the user picks one from the ready queue), run `anvil show TASK_ID` yourself and present the full task detail in chat:

```bash
anvil show T012
```

Returns: title, intent, acceptance criteria, verification commands, all six score dimensions (`complexity`, `parallelizability`, `context_load`, `blast_radius`, `review_risk`, `agent_suitability`), `expected_files`, dependency chain, and any active claim on this task.

Audit the result inline and surface anything that should give the user pause before the claim fires:

- The acceptance criteria are concrete and independently verifiable — not aspirational descriptions.
- `complexity` is 3 or under. A score of 4 or 5 means the task should have been expanded via `anvil expand` during planning. Claiming an oversized task and then abandoning it mid-way wastes the lease window.
- `agent_suitability` matches the current executor. A score of 1 or 2 signals that the task requires architectural judgment, significant human context, or decisions that a model is likely to get wrong. Defer those tasks.
- `expected_files` does not include files that look like they belong to a different subsystem — a sign the task scope drifted during authoring.

Present the inspection summary and ask:

> T012 looks claimable: acceptance criteria concrete, complexity 3, agent_suitability 4, expected_files scoped to `src/retry/`. Proceed to claim? (yes / show me more / pick a different task)

This step costs nothing and prevents the most common source of wasted claims.

---

### Step 3 — Check for conflicts

Invoke `anvil claim TASK_ID` yourself once the user confirms. The command performs the conflict check before writing anything:

```bash
anvil claim T012
```

The manager checks two conflict conditions before issuing the lease:

1. **File overlap** — another active claim by a different actor has at least one file in common with `expected_files` of T012.
2. **Conflict group** — T012 belongs to a `conflict_group` that already has an active claim on a sibling task.

If either condition is true and `--force` is not passed, `claim` raises `ClaimError` and prints the overlapping claim ID, the other actor's identity, and the overlapping files. Example:

```
ClaimError: Task 'T012' conflicts with active claims: claim C003 by agent-scout
(files: ['src/anvil/state/backend.py']). Use --force to override.
```

Surface the conflict in chat. If it is acceptable — for example, the other actor owns C003 on a read-only research task and T012 writes to a different function in the same file — ask the user explicitly before forcing:

> T012 conflicts with C003 (agent-scout) on `src/anvil/state/backend.py`. Force the claim? `--force` is audit-logged. (yes / no / inspect C003 first)

On `yes`, re-run with `--force` yourself:

```bash
anvil claim T012 --force
```

The override is logged as a warning in `events.jsonl` with the actor identity, the claim being forced, and the overlapping files. Every forced claim is auditable.

---

### Step 4 — Acquire the lease

A clean claim (no conflicts, or `--force` accepted) prints the claim result. Surface it inline:

```
Claimed T012: add-retry-backoff
Claim ID:     C004
Branch:       agent/t012-add-retry-backoff
Lease:        60 min (expires 2026-05-24T19:00:00Z)
```

The task transitions from `ready` to `claimed` in `state.db`. Two events are appended to `events.jsonl`: `claim.created` and `task.status_changed`.

If the user wants a separate git worktree (useful when running two agents in parallel from the same repo without checkout conflicts), invoke `--worktree` yourself:

```bash
anvil claim T012 --worktree
```

This creates `../wt-t012/` with the branch already checked out. Each worktree is fully independent — no stashing required when switching between tasks.

Without a git repo present, `claim` still succeeds and prints:

```
Warning: not a git repository — no branch created (record-only mode).
```

The claim is valid. Work proceeds in the repo root. The branch field on the Claim row is left `null`. Tell the user inline what mode the claim is in (branch vs. record-only) so there is no surprise later.

---

### Step 5 — Work on the branch

Actual code changes happen here, inside the conversation. The agent makes the edits directly on `agent/t012-add-retry-backoff` (or the worktree branch). Commit incrementally — incremental commits make the eventual PR reviewable and give a recovery point if the session is interrupted.

Two hooks run automatically during this phase (`check-claim.sh` and `record-file-change.sh`). See `/anvil:execute` Step 3a for full hook descriptions.

---

### Step 6 — Heartbeat the lease

The default lease is 60 minutes. For sessions longer than 55 minutes, renew before the lease expires. Invoke renewal yourself whenever the agent notices the timer:

```bash
anvil renew C004
```

Renewing extends `lease_expires_at` by another 60 minutes from now and updates `last_heartbeat_at`. The command errors if the lease is already expired — re-claiming is the only option at that point.

```
Renewed C004: lease extended to 2026-05-24T20:05:00Z
```

Automated agents should renew every 5 minutes. A missed heartbeat does not immediately lose the claim — the lease detector runs on the next CLI or MCP operation. Once the lease expires, the task returns to `ready` and any agent can claim it.

Only the owning actor can renew a claim. To release another actor's stale claim, use `release --force` (Step 8).

---

### Step 7 — Submit when complete (Phase 5)

Once verification passes and the work is ready to leave the agent's hands, drive the submit yourself. Read the verification output, summarize what was completed, and ask the user to acknowledge before submitting:

> T012 verification passed: 14 tests added, retry-backoff implemented in `src/retry/backoff.py`, no other files touched. Ready to submit? Submitting transitions T012 to `needs_review` and auto-releases C004. (yes / not yet / let me re-check)

On `yes`, invoke `anvil submit TASK_ID` yourself and surface the output:

```bash
anvil submit T012
```

`submit` reads the evidence packet from `events.jsonl`, transitions the task to `needs_review`, and auto-releases the claim. Tell the user the task is now in `needs_review` and ask whether to hand off to `/anvil:finish` for the apply gate.

**The hard handoff lives in `finish`, not here.** `anvil apply TASK_ID --approve` is the only command in this leg of the lifecycle that requires explicit user confirmation before the agent runs it. That gate lives in `/anvil:finish` — invoke that skill rather than running `apply --approve` from here.

If Phase 5 `submit` is not yet available in the running environment, fall back to the explicit Phase 4 release flow: run `anvil release C004` yourself (see Step 8), confirm the task is back in `ready`, and tell the user the PR/merge step is theirs.

---

### Step 8 — Release explicitly when abandoning

When work must stop before completion — blocked on an upstream issue, deprioritized, or handed off — invoke release yourself so the task returns to the pool. Always ask the user for a reason first so the audit trail is informative:

> Releasing C004. What's the reason? (e.g., "blocked: upstream T009 not merged")

Then run:

```bash
anvil release C004 --reason "blocked: upstream T009 not merged"
```

The `--reason` string is stored in `release_reason` on the Claim row and logged in `events.jsonl`. Another agent can then pick up the task via `anvil next`.

To release a claim held by a different actor (use sparingly — logged in audit trail), confirm with the user before forcing:

```bash
anvil release C004 --force
```

`--force` bypasses the actor-ownership check and also allows releasing claims in non-`active` states (e.g., `stale`). Every forced release is recorded with the releasing actor's identity.

---

## Anti-pattern to avoid

The agent drives commands inline; it does not hand the user a numbered CLI to-do list. See `/anvil:plan` for the canonical statement. The only command that requires explicit user confirmation before the agent runs it is `apply --approve` (which lives in `/anvil:finish`).

**When to actually hand off CLI commands:** if the user explicitly opts out ("just give me the commands"), or if the runtime lacks the tool needed to execute them. In those cases, a CLI list is the right output. Otherwise, drive.

For **decision-presentation discipline** — how to surface multi-option choices (e.g., which task to claim, whether to force-claim) — see the canonical description in `/anvil:resolve-decisions`. The same structured Q&A pattern applies here.

---

## Common Pitfalls

- **Claiming while PRD is still `draft`.** The claim gate checks `prd-status` and raises `ClaimError` before touching anything else. Run `anvil prd review --approve` first.
- **Ignoring the `agent_suitability` score.** Claiming a task scored `1` or `2` with a small or local model burns 60 minutes and produces output that needs complete rework. Check `anvil show TASK_ID` before committing.
- **Skipping the heartbeat on long sessions.** Leases expire silently. The task returns to `ready` and another agent can claim it while work is still in progress. Set a timer and run `anvil renew CLAIM_ID` every 5 minutes.
- **Calling `renew` after the lease has already expired.** `renew` raises `ClaimError` on an expired lease — the lease cannot be extended retroactively. Re-claim the task after it returns to `ready`.

---

## Composition with Other Skills

| Position | Skill |
|---|---|
| Before this skill | `/anvil:plan` must have produced `ready` tasks; optionally `/anvil:state-ops` to read the queue first |
| After claiming | Work the branch, heartbeat, complete; then Phase 5 `finish` for submit + ship |
| If `show TASK_ID` reveals `complexity >= 4` | Return to `/anvil:plan` and expand the task before claiming |
| If `next` returns nothing | `/anvil:state-ops` to diagnose — check `status`, `list --status drafted`, trace blockers |

---

## Phase 4 Limitations

`submit` and `apply` ship in Phase 5. Until then, the claim lifecycle ends at `release` — no automated transition to `done`.

| Feature | Phase | Status |
|---|---|---|
| `anvil next` | Phase 4 | available |
| `anvil claim TASK_ID` | Phase 4 | available |
| `anvil claim TASK_ID --worktree` | Phase 4 | available |
| `anvil release CLAIM_ID` | Phase 4 | available |
| `anvil renew CLAIM_ID` | Phase 4 | available |
| `check-claim.sh` hook (PreToolUse) | Phase 4 | available |
| `record-file-change.sh` hook (PostToolUse) | Phase 4 | available |
| `anvil submit TASK_ID` (auto-release + needs_review) | Phase 5 | pending |
| `anvil apply TASK_ID` (evidence review + done) | Phase 5 | pending |
| `anvil conflicts` (full conflict map across all active claims) | Phase 5 | pending |
| Per-file scope check refinement in `check-claim.sh` | Phase 5 | pending — current hook warns on any active claim, not per-claim file scope |
| PR creation / commit assistance | Out of scope | anvil coordinates work; it does not write code |
