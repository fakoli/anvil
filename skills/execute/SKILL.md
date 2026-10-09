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

Follow [Native resume and frozen handoff](../../AGENTS.md#native-resume-and-frozen-handoff)
before reclaiming or writing. For an ordinary eligible task without an active
claim, use `/anvil:claim`. A bundle delegate instead returns work to its
coordinator, who owns all member mutations; do not claim a bundle member
independently. Commands used in this skill:

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
an unstated premise with no bounded safe default, stop and surface it to the
owner. Release an ordinary claim only after writers stop; bundle delegates
leave release and recovery to their coordinator.

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
3. All acceptance criteria are unambiguous. If any are unclear, stop writers and
   surface the unresolved choice. For an ordinary claim, release with the actual
   reason; for a bundle member, return the blocker to the coordinator:

```bash
anvil release CLAIM_ID --reason "acceptance criteria ambiguous on T012 item 3"
```

Resolve the choice with the authorized owner, update the owning PRD and re-parse
with its selection. Resume only under the current native ownership rules.

This check costs one minute. A wrong interpretation discovered at submit costs the full lease window plus rework.

---

### Step 3 — Do the work

This loop runs in the current harness: a Codex/Claude subscription session or a
harness connected to an explicitly selected local model. Anvil's CLI/MCP owns
state; the harness owns reasoning and tools. `llm_provider: harness` disables
nested planning calls. Do not switch the user's selected model, start a model
server, or use API credentials unless explicitly enabled. See
[execution in your harness](../../docs/how-to/execute-in-your-harness.md).

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

Three hook actions run automatically during this step — no manual action required:

**`hook dispatch check-claim`** (PreToolUse on Edit, Write, NotebookEdit) — warns when the file overlaps another actor's active claim scope. The warning is non-blocking: the edit proceeds. Heed it; overlap creates avoidable integration risk.

**`hook dispatch record-file-change`** (PostToolUse on Edit, Write, NotebookEdit) — appends a `file_changed` audit event for every editor-tool path touched. `anvil submit` still requires an explicit, complete `--files-changed` list; derive it from the actual diff rather than assuming the hook injects CLI arguments.

**`hook dispatch heartbeat`** (PostToolUse) — attempts a progress-gated lease renewal for the current actor/session. It renews only after new hook-observed file progress or a verified pending attestation.

---

### Step 4 — Heartbeat the lease during long work

The default lease is 240 minutes unless project/global config or `claim --lease`
overrides it. The automatic heartbeat handles qualified progress, but inspect
`anvil status` and renew manually before the echoed expiry on long-running work:

```bash
anvil renew CLAIM_ID
```

Example:

```bash
anvil renew C004
```

Renewing extends `lease_expires_at` by the resolved lease duration from now and
updates `last_heartbeat_at` only after qualifying progress. A missed heartbeat
does not immediately lose the claim; the stale detector fires on the next
coordination operation. Once the lease has expired, the task returns to `ready`
and another agent can claim it mid-work.

```
Renewed claim 'C004'.
  New lease until: 2026-05-25T17:35:00.000000+00:00
  Last heartbeat:  2026-05-25T13:35:00.000000+00:00
```

Only the owning actor can renew. To check remaining lease time without renewing:

```bash
anvil list --status claimed
```

The output includes `lease_expires_at` for each active claim.

---

### Step 5 — Run verification before submitting

Use the shared workflow's environment and evidence preflight before costly
verification. Read optional `verification.profile` metadata as frozen runner,
command and file bindings; Anvil validates them but does not execute the runner.
`anvil evidence-preflight TASK_ID --json` reports advisory capture readiness and
cannot qualify a proof or grant authority.

Apply the work packet's structured `hook_environment` in the process that
invokes tools, then execute the task's `verification.commands`. The shell-free
`capture-evidence` dispatcher (PostToolUse Bash) captures stdout, stderr, and
exit code into the claim's pending evidence buffer only when
`ANVIL_CLAIM_ID`, `ANVIL_ACTOR`, active ownership, and any persisted session
identity match exactly. It never guesses from a sole or actor-only claim. The
legacy `capture-evidence.sh` wrapper delegates to the same capture command.

The verification commands are the objective acceptance gate. Submit only when all verification commands exit 0. If a command fails:

1. Read the error output.
2. Fix the code.
3. Re-run the verification command.

Retain failed captures when fixing and rerunning; never erase them or relabel
old proof attribution. The Review engine checks evidence against the task's
requirements. Advisory required-evidence warnings and enforceable typed-proof
gates have different approval behavior; `/anvil:finish` inspects both.

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

`--commands` and `--files-changed` are both **required** and **repeatable**: pass the flag once per value (one occurrence == one value, so commands or paths with embedded commas survive intact), or pass a single comma-separated occurrence for the simple case shown above. `--output-file` attaches up to 8000 characters as a descriptive excerpt; it never creates a typed proof or satisfies `required_proofs`. For an external/subagent run under an ordinary claim, repeat `--command-proof-file ARTIFACT` to import a bounded claim-bound proof batch. Every proof must match the explicit claim owner/context and an exact `--commands` value or the whole submission is refused. Bundle members use the coordinator's member-bound capture described in [bundle coordination](../../docs/how-to/coordinating-a-bundle.md#invariants). `--pr-url` links the branch's PR if one exists.

Signed command proofs also depend on current issuer membership in
`ANVIL_TRUST_LIST` or `~/.anvil/trust.txt` during both append and replay. Back up
and restore that trust list with state and retain the signing key or fingerprint;
missing membership fails closed. Self-attested proof replay is trust-list
independent.

`submit` does the following atomically:

1. Writes an `Evidence` row to `state.db` with the commands run, files changed, and output excerpt.
2. Auto-releases the task claim, including a bundle member authorization. The
   bundle coordinator lease and bundle custody remain until their lifecycle
   releases them; a submitted member cannot be reclaimed independently.
3. Transitions the task from `claimed` to `needs_review` through the
   `evidence.submitted` projection.

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

Review the printed evidence summary and freeze the shared workflow's handoff
before review. If a field looks wrong, inspect with `anvil show T012` and return
the blocker to the owner; do not retarget old evidence. Repair of a submitted
bundle member requires the documented replacement-generation flow.

---

### Step 7 — Wait for apply

The task stays in `needs_review` until an authorized disposition. The
`/anvil:finish` skill validates the independent reviews and explicit user
authority before running `anvil apply TASK_ID` with a disposition. Existing
authority may cover routine intermediate dispositions; never infer approval
from unattended execution or green checks. Preserve human-only, protected and
final user project-validation gates from the shared workflow.

Until `apply` is called:

- The branch persists on the claim's git branch.
- The task is visible in `anvil list --status needs_review`.
- No other agent can re-claim the task.

After returning the frozen handoff and stopping its writers, continue authorized
independent work. A bundle delegate returns to the coordinator; an ordinary
worker can inspect the next task:

```bash
anvil next
```

---

## Edge Cases

**Verification fails mid-work**: do not submit. Fix and re-run. The packet's `verification.commands` are the contract; submit only when they all exit 0.

**Claim went stale mid-work**: stop writers and follow the shared native resume
workflow. Inspect current readiness and custody before an ordinary fresh claim:

```bash
anvil claim T012
```

The old branch, claim and evidence remain history. A new claim has fresh
attribution and may select a different worktree; use its actual packet and
location. Bundle or root custody requires its owner's recovery flow.

**Need to abandon**: stop writers before releasing an ordinary claim. Bundle
delegates return the blocker to their coordinator:

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
- **Ignoring the echoed lease expiry.** The automatic heartbeat is progress-gated and cannot extend a claim indefinitely without new evidence. Inspect status and renew before expiry when work is genuinely continuing.
- **Submitting an incomplete file list.** The record-file-change hook creates audit events, but `submit` takes explicit `--files-changed` values. Derive the complete list from the actual diff.

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
| `anvil submit TASK_ID` | records evidence and auto-releases the task claim; bundle coordinator custody remains |
| `anvil apply TASK_ID` | human review gate (accept / reject) |
| `anvil conflicts` | persisted conflict groups (overlapping likely_files) |
| `hook dispatch capture-evidence` | PostToolUse Bash; buffers verification output |
| `hook dispatch check-claim` | PreToolUse Edit/Write/NotebookEdit; warns on overlap with another actor's active claim scope |
| MCP `generate_work_packet` | the packet over MCP for tools/agents that consume it programmatically |
