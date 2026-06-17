---
name: execute
description: Run the agentic execution loop on a claimed fakoli-state task — fetch the work packet, do the work, submit completion evidence. Use this skill when an agent has just claimed a task and needs to execute it end-to-end without juggling individual CLI commands.
---

# Execute — Claim to Submit in One Loop

Carry a `ready` task all the way to `needs_review`: fetch the work packet, read it in full, do the work, heartbeat the lease, run verification, and submit evidence. Nothing moves to `needs_review` without passing through here. This skill covers the solo-agent path; `fakoli-flow:execute` wraps it with wave orchestration and critic gates for multi-agent teams.

---

## When to Use

- After `fakoli-state claim TASK_ID` has succeeded — claim ID and branch are in hand.
- When `fakoli-flow:execute` is NOT installed. When it IS installed, prefer that; `fakoli-flow:execute` wraps this skill with wave-based dispatch, critic gates between waves, and coordinated submit timing.
- For solo execution: one agent, one task, one branch, straight to submit.

**Do not use this skill to inspect the queue without taking work** — use `/fakoli-state:state-ops`. Do not use it to make the ship decision on completed tasks — that is `/fakoli-state:finish`.

---

## Prerequisites

An active claim by the current actor on `TASK_ID`. Verify before proceeding:

```bash
fakoli-state list --status claimed
```

If the task does not appear, claim it first via `/fakoli-state:claim`. Phase 5 commands used in this skill:

| Command | Phase | Status |
|---|---|---|
| `fakoli-state packet TASK_ID` | Phase 5 | available |
| `fakoli-state submit TASK_ID` | Phase 5 | available |
| `fakoli-state apply TASK_ID` | Phase 5 | available (human-only) |
| `fakoli-state renew CLAIM_ID` | Phase 4 | available |
| `fakoli-state release CLAIM_ID` | Phase 4 | available |

---

## Workflow

### Step 0 — Detect whether `fakoli-flow:execute` is available

Before running the standalone execute loop, run the explicit plugin check so the decision is deterministic and reproducible across sessions — no introspection of in-memory command lists, no fuzzy "if it seems available" prose:

```bash
claude plugin list 2>/dev/null | grep -q "fakoli-flow"
```

- **Exit code 0** (`fakoli-flow` plugin present): prefer `/fakoli-flow:execute` for the wave-engine path. It wraps this skill with wave-based dispatch, critic gates between waves, and coordinated submit timing for multi-agent teams. Branch on the user's existing workflow preferences when deciding whether to bridge.
- **Non-zero exit** (plugin absent, or `claude` CLI itself not on `PATH`): proceed with the standalone path below (Step 1 onward). The fall-through is intentional graceful degradation: missing tooling never blocks the execute flow.

The grep pattern is intentionally unanchored. Actual `claude plugin list` output renders each installed plugin as `  ❯ fakoli-flow@fakoli-plugins` (indented marker line, plugin name suffixed with `@<source>`); a leading `^` anchor would never match. The unanchored substring is safe because `fakoli-flow` is a unique slug within the marketplace.

### Step 1 — Fetch the work packet

```bash
fakoli-state packet TASK_ID
```

Example:

```bash
fakoli-state packet T012
```

Writes `.fakoli-state/packets/T012.md` with the full operating context for this task: intent, acceptance criteria, likely files in scope, prior decisions, verification commands, and the output contract. The packet is a derived view regenerated from canonical state — it reflects the current snapshot of `state.db`, not a cached copy.

Read the packet immediately after fetching it. The acceptance criteria in the packet are the contract that `submit` validates against. Skipping the packet and working from memory or from `show TASK_ID` output risks submitting evidence that misses a required item.

To get the JSON form instead (useful when another tool or agent consumes the packet programmatically):

```bash
fakoli-state packet T012 --format json
```

---

### Step 2 — Confirm scope before writing code

Before touching any file, confirm:

1. The acceptance criteria are concrete and independently verifiable — not aspirational descriptions.
2. The `likely_files` list in the packet does not overlap with files another active claim owns. If overlap exists, resolve it via `/fakoli-state:state-ops` before editing.
3. All acceptance criteria are unambiguous. If any are unclear, release the claim now rather than discovering the problem at submit time:

```bash
fakoli-state release CLAIM_ID --reason "acceptance criteria ambiguous on T012 item 3"
```

Ask the user to clarify, update the PRD, re-parse, and re-claim once the criteria are concrete.

This check costs one minute. A wrong interpretation discovered at submit costs the full lease window plus rework.

---

### Step 3 — Do the work (route to a fakoli-crew specialist when available, v1.15.0)

Before opening the editor yourself, decide WHO should do the work — you-the-agent, or a specialist crew member. The decision is deterministic, not a question to put to the user.

**Detection — same explicit shell check as Step 0:**

```bash
claude plugin list 2>/dev/null | grep -q "fakoli-crew"
```

**If fakoli-crew is absent** (or `claude` CLI not on `PATH`): do the work yourself in this session. Skip the routing block below; continue with the hooks-and-commits prose.

**If fakoli-crew is installed**: analyze the task and dispatch to the best-fit crew member directly. **Do not** ask the user "want me to do this here, or dispatch?" — that meta-question forces the user to make a routing decision the agent has the context to make. Asking it biases toward the worst answer (self-implement, bypassing the specialist team).

Routing heuristic (apply in order; take the first row that fits):

| Signal in task | Likely crew member | Rationale |
|---|---|---|
| `likely_files` includes `.claude-plugin/`, `hooks/hooks.json`, command frontmatter | `fakoli-crew:smith` | plugin-structure work — manifests, hook wiring, frontmatter |
| `likely_files` includes new abstractions / interface design / type system (`.ts`, `.py`, `.rs` with "Protocol" / "interface" / "trait" in title or criteria) | `fakoli-crew:guido` | design + interface specialist |
| `likely_files` includes existing-file integration (verbs like "wire", "integrate", "refactor to use", "connect") | `fakoli-crew:welder` | facade + re-export integration specialist |
| Task title verb: "Research", "Document the X API" | `fakoli-crew:scout` | API research → structured reference doc |
| `likely_files` includes README, docs, `*.md` user-facing copy | `fakoli-crew:herald` | user-facing documentation specialist |
| `likely_files` includes `CLAUDE.md`, `.github/workflows/`, contributor docs, `pyproject.toml` infra | `fakoli-crew:keeper` | repository infrastructure |
| Acceptance criteria centered on tests + verification of existing behavior | `fakoli-crew:sentinel` | binary PASS/FAIL validation |
| Multi-file refactor + integration + cross-package wiring (3+ files across 2+ packages) | `fakoli-flow:execute` (if installed, see Step 0) OR pair `guido` for design + `welder` for integration | wave-based orchestration |

**Tie-break rule.** When two rows both match (e.g. a TS task with "Protocol" in the title AND verbs like "wire" — guido row 2 and welder row 3 both fire), prefer the row whose Signal column more narrowly describes the **primary verb** of the task title. "Design" / "Define" / "Specify" / "Author the interface" → guido (row 2). "Integrate" / "Wire" / "Connect" / "Refactor to use" → welder (row 3). If both verbs are present in the title equally, pick guido first (design before integration is the workflow order).

After dispatch, the agent's job is to:
- Read the work packet
- Brief the crew specialist with the task ID + acceptance criteria + likely_files + verification commands
- Surface the specialist's output inline (do not ghost the user — show progress)
- Run the verification commands yourself (Step 5) once the specialist returns

**Only ask the user when ALL of these conditions are clearly true** (any other situation: dispatch silently):

- (a) Two or more rows in the table above match AND the tie-break rule above does not disambiguate (e.g. the task touches three crews and you cannot identify a clear primary), OR
- (b) Zero rows match — the task's `likely_files` and verb shape don't fit any row (genuine novelty, not just "I'm not sure"), OR
- (c) The user explicitly said earlier in this session "I want to drive this one myself" — track the preference for the rest of the session.

"The task is unusual" alone is NOT a reason to ask. If you find yourself reaching for that phrase, recheck rows 1-8 — almost every real engineering task fits one. The bias when in doubt: dispatch, not ask.

In every other case: **route silently and report the routing decision inline** ("Dispatching to fakoli-crew:guido for the HttpTransport interface design; I'll surface the proposal here and run verification afterward.").

**Anti-pattern to avoid:** ending Step 3 with "How would you like to proceed with implementation? Four reasonable approaches: 1. I implement it here, 2. dispatch to fakoli-crew, 3. …". That meta-question is the same prose-with-bullets shape v1.13.0 named — but worse, because the question has an obvious answer (whichever specialist matches the task) and the user has no information the agent lacks. The framing biases toward "do it yourself" because the question reads as "should I bother the team?" rather than "which specialist owns this?". Just dispatch.

---

### Step 3a — Implementation discipline (whoever does the work)

Whether the work is done by you-the-agent or a dispatched crew specialist, the same incremental-commit discipline applies. Commit incrementally to the claim's branch:

```
agent/t012-add-retry-backoff
```

(Or whatever branch prefix the project configured — v1.15.0 made `branch_prefix` host-project-configurable via `.fakoli-state/config.yaml`. The default is `agent/`.)

Incremental commits create a recoverable trail. If the agent session is interrupted, the commits survive on the branch and the work does not need to restart from zero.

Two hooks run automatically during this step — no manual action required:

**`check-claim.sh`** (PreToolUse on Edit, Write, NotebookEdit) — warns whenever any active claim exists, prompting a check that the file being modified is within this claim's `likely_files` scope. The warning is non-blocking: the edit proceeds. Heed the warning; file overlap with another claim creates a merge conflict that is painful to resolve after submit.

**`record-file-change.sh`** (PostToolUse on Edit, Write, NotebookEdit) — appends a `file_changed` event to `events.jsonl` for every file touched. This populates the `files_changed` list that `submit` reads and includes in the Evidence row. Do not maintain a manual list; the hook tracks it.

---

### Step 4 — Heartbeat the lease during long work

The default lease is 60 minutes. For sessions longer than 55 minutes, renew before the lease expires:

```bash
fakoli-state renew CLAIM_ID
```

Example:

```bash
fakoli-state renew C004
```

Renewing extends `lease_expires_at` by another 60 minutes from now and updates `last_heartbeat_at`. Run this every 5 minutes during active work — set a timer at the start of a long session. A missed heartbeat does not immediately lose the claim; the stale detector fires on the next CLI or MCP operation. Once the lease has expired, the task returns to `ready` and another agent can claim it mid-work.

```
Renewed C004: lease extended to 2026-05-25T14:35:00Z
```

Only the owning actor can renew. To check remaining lease time without renewing:

```bash
fakoli-state list --status claimed
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
fakoli-state submit TASK_ID --commands "pytest -x" --files-changed src/foo.py,src/bar.py
```

Additional flags:

```bash
fakoli-state submit T012 \
  --commands "pytest -x,ruff check src/" \
  --files-changed src/fakoli_state/claims/manager.py,src/fakoli_state/cli.py \
  --output-file /tmp/pytest-out.log \
  --pr-url https://github.com/org/repo/pull/42
```

`--commands` is a comma-separated list of the verification commands that were run. `--files-changed` is a comma-separated list of files the agent touched. `--output-file` attaches a log file to the Evidence row. `--pr-url` links the branch's PR if one exists.

`submit` does the following atomically:

1. Writes an `Evidence` row to `state.db` with the commands run, files changed, and output excerpt.
2. Auto-releases the claim (`claim.released` event).
3. Transitions the task from `claimed` to `needs_review` (`task.status_changed` event).

The CLI prints the evidence summary immediately:

```
Submitted T012: add-retry-backoff
Evidence:  E000041
Commands:  pytest -x (exit 0), ruff check src/ (exit 0)
Files:     2 changed
Status:    needs_review
```

Review the printed evidence summary before walking away. If a field looks wrong (wrong file list, missing command), inspect with `fakoli-state show T012` and coordinate with the human reviewer before they invoke `apply`.

---

### Step 7 — Wait for apply

`fakoli-state apply TASK_ID` is a human-only step. The task stays in `needs_review` until the human reviewer invokes it. The `/fakoli-state:finish` skill drives that decision — surfacing the evidence, picking a disposition (accept, reject, hold, discard), and running `apply`.

Until `apply` is called:

- The branch persists on the claim's git branch.
- The task is visible in `fakoli-state list --status needs_review`.
- No other agent can re-claim the task.

No action required here. Proceed to the next task in the queue:

```bash
fakoli-state next
```

---

## Edge Cases

**Verification fails mid-work**: do not submit. Fix and re-run. The packet's `verification.commands` are the contract; submit only when they all exit 0.

**Claim went stale mid-work**: the task has returned to `ready`. Re-claim it:

```bash
fakoli-state claim T012
```

The branch's commits are preserved on `agent/t012-<slug>`. Continue work on the same branch; the new claim ID replaces the expired one. If another agent claimed the task in the window between expiry and re-claim, coordinate via `fakoli-state show T012` to check the current holder.

**Need to abandon**: release the claim so the task returns to the pool:

```bash
fakoli-state release CLAIM_ID --reason "blocked on upstream T009 — not merged yet"
```

The `--reason` string is stored in the Claim row and logged in `events.jsonl`. Another agent picks up the task via `fakoli-state next`.

**Packet is stale**: if the PRD was revised after the packet was generated, re-fetch:

```bash
fakoli-state packet T012
```

The command overwrites the previous `.fakoli-state/packets/T012.md`. Re-read the packet before continuing.

---

## Common Pitfalls

- **Working from memory instead of the packet.** `fakoli-state show TASK_ID` is a summary; the packet is the full operating context. Always read the packet before writing code.
- **Submitting without running all verification commands.** The Review engine checks completeness at `apply` time. Submitting partial evidence delays the ship decision and may require reopening the task.
- **Skipping heartbeats on sessions longer than 55 minutes.** Leases expire silently. Set a timer at session start and renew every 5 minutes.
- **Manually tracking files changed.** The `record-file-change.sh` hook tracks this automatically from every Edit, Write, and NotebookEdit call. Pass the hook-tracked list to `--files-changed`; do not reconstruct it by hand.
- **Editing `state.db` directly to fix a stuck claim.** Use `fakoli-state release --force` instead. Direct edits bypass `events.jsonl` and produce state that cannot be replayed or audited.

---

## Composition with Other Skills

| Position | Skill |
|---|---|
| Before this skill | `/fakoli-state:claim` — active claim required before execute starts |
| If scope is ambiguous before step 3 | Return to `/fakoli-state:state-ops` to inspect conflicts; resolve before editing |
| If `complexity >= 4` at packet read | Return to `/fakoli-state:plan` — the task should have been expanded; release claim first |
| After submit | `/fakoli-state:finish` drives the apply step and ship decision |
| If task returns nothing from `next` after submit | `/fakoli-state:state-ops` to diagnose queue state |

**When `fakoli-flow:execute` is installed:** that skill wraps this one. It reads `fakoli-state next`, calls `fakoli-state claim`, dispatches agents against non-overlapping tasks in parallel waves, gates waves with critic review, and coordinates submit timing. Solo agents use this skill directly; orchestrated agent teams use `fakoli-flow:execute`, which calls each step here in sequence for each wave.

**When `fakoli-crew` is installed:** `welder` is the standard executor for integration tasks; `scout` claims research tasks. Each crew agent runs this skill's steps internally, tagged with `--actor fakoli-crew:welder` on claim. The execute loop is identical; the actor identity differs.

---

## Phase 5 Limitations

| Feature | Phase | Status |
|---|---|---|
| `fakoli-state packet TASK_ID` | Phase 5 | available |
| `fakoli-state submit TASK_ID` | Phase 5 | available |
| `fakoli-state apply TASK_ID` | Phase 5 | available (human-only) |
| `capture-evidence.sh` hook (PostToolUse Bash) | Phase 5 | available |
| LLM-assisted self-review on submit | Phase 7 | pending |
| MCP `generate_work_packet` (JSON form via MCP) | Phase 6 | pending |
| `fakoli-state conflicts` (full conflict map) | Phase 5 | available |
| Per-file scope check refinement in `check-claim.sh` | Phase 5 | available — warns per claim's likely_files scope |
