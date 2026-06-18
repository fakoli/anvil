---
name: finish
description: Decide what to do with an anvil task that has submitted evidence and is awaiting human review — accept and ship, reject and reopen, or hold for further investigation. Use this skill when one or more tasks are in needs_review and need a final disposition.
---

# Finish — Review Evidence and Ship

Drive the final leg of the task lifecycle: read the evidence, pick a disposition, call `apply`, and hand off to the project's git workflow for merging. Nothing moves from `needs_review` to `done` (or back to `drafted`) without going through here.

---

## When to Use

- Tasks appear in `anvil list --status needs_review`.
- Before merging a PR that contains anvil-tracked work — confirm the task has been applied first.
- At end-of-day or end-of-iteration when deciding what to ship versus what to reopen.

**Do not use this skill to execute work or submit evidence** — that is `/anvil:execute`. Do not use it to inspect queue state without making a decision — use `/anvil:state-ops` for read-only inspection.

---

## Prerequisites

One or more tasks in `needs_review`. Confirm before proceeding:

```bash
anvil list --status needs_review
```

Each row shows `TaskID`, title, `claimed_by`, claim duration, and `files_changed` count. Phase 5 commands used in this skill:

| Command | Phase | Status |
|---|---|---|
| `anvil list --status needs_review` | Phase 3 | available |
| `anvil show TASK_ID` | Phase 3 | available |
| `anvil apply TASK_ID --approve` | Phase 5 | available |
| `anvil apply TASK_ID --reject --reason "..."` | Phase 5 | available |

---

## Workflow

### Step 1 — List what needs review

```bash
anvil list --status needs_review
```

Read every row. Before proceeding to any individual task:

- Note tasks that have been in `needs_review` longer than expected — long dwell times may indicate evidence was submitted in a broken state or the reviewer window closed.
- Note whether multiple tasks in the list touch the same files — they may need to be applied in dependency order to avoid conflicts on merge.

When multiple tasks are ready, apply them in dependency order: tasks with no dependents first, then tasks whose dependencies are already `done`.

---

### Step 2 — Inspect each task's evidence

```bash
anvil show TASK_ID
```

Example:

```bash
anvil show T012
```

The output surfaces: `acceptance_criteria`, `evidence.commands_run` (with exit codes), `files_changed` (list), `output_excerpt` (first and last lines of captured output), and `pr_url` if one was linked at submit time.

Read all of it before invoking `apply`. The Review engine pre-checks `evidence_complete` against the task's `required_evidence` list; missing items are flagged in the `show` output:

```
Evidence status: INCOMPLETE
Missing:        pytest -x (no evidence captured)
```

An `INCOMPLETE` flag means the agent ran verification commands outside the hook window or the `capture-evidence.sh` hook did not fire. Do not apply an incomplete evidence row without understanding why — the gap may indicate the verification was never actually run.

For tasks where the evidence looks complete:

- Confirm every acceptance criterion has a corresponding command that exited 0.
- Confirm `files_changed` matches what the acceptance criteria required — a task that was supposed to modify `src/claims/manager.py` but shows only `tests/` in `files_changed` is suspicious.
- If a `pr_url` is linked, open the PR and scan the diff to spot anything the evidence summary missed.

---

### Step 3 — Pick a disposition (the hard handoff gate)

This is the one place in the entire anvil workflow where the agent must wait for explicit user confirmation before executing the next command. `apply --approve` writes a permanent `Review` row to `state.db` and an immutable `task.applied` event to `events.jsonl` — it is the formal "ship it" gate. The agent must not run it on inference.

After surfacing the evidence summary in Step 2, present the disposition options conversationally and ask the user to pick — then run the chosen command yourself:

> The evidence for **T012** is summarized above. How should this be dispositioned?
> 1. **Accept and ship** — verification exited 0, evidence is complete, diff matches acceptance criteria.
> 2. **Reject and reopen** — evidence is incomplete or the implementation does not satisfy acceptance criteria. I will need a reason.
> 3. **Hold for investigation** — evidence is submitted but more context is needed before deciding. I will keep the task in `needs_review`.
> 4. **Discard the work entirely** — the task direction was wrong and the implementation should not be merged. I will also need a reason.
>
> Reply with the number (or just "accept" / "reject" / "hold" / "discard").

Based on the answer, drive the corresponding command yourself rather than asking the user to type it.

#### On "accept" (1)

Confirm one more time before invoking the gate — this is the irreversible-via-audit point:

> Approving will transition T012 `needs_review → accepted → done` and append a permanent `task.applied` event with you as the approver. Confirm? (yes / no)

On `yes`, invoke `anvil apply T012 --approve` (or the `apply_review_decision` MCP tool when available). Surface the response inline. Then ask whether to drive Step 4 (the ship sequence — git merge) now or later. On `no`, return to the disposition prompt.

#### On "reject" (2)

Ask for a concrete reason before invoking:

> Reject T012 with which reason? Concrete is required — "pytest -x reports 3 failures in test_retry.py" is good; "not done" is not.

Once the user supplies a reason, invoke `anvil apply T012 --reject --reason "<their reason>"` directly. Surface the response. The task transitions `needs_review → rejected → drafted` and the original branch + Evidence row are preserved in the audit log. Tell the user the task is back at `drafted` and ask whether to re-trigger `review tasks` or leave it for the agent to fix the underlying issue.

#### On "hold" (3)

Do not invoke `apply` at all. Capture the open questions inline:

> What context do we need before this can be dispositioned? I will add it to the task notes so the next review has the full picture.

Once the user lists the open questions, append them to the task's `implementation_notes` via the appropriate state-engine path (until Phase 6 ships a `decision` CLI subcommand, edit `prd.md`, re-parse, and coordinate with the next reviewer directly — drive that loop yourself). Then loop back to Step 2.

#### On "discard" (4)

Like reject, but with a discard-specific reason — and you also need to clean up the branch.

> Discard T012 with which reason? (For the audit log — "approach superseded by T015" is typical.)

Invoke `anvil apply T012 --reject --reason "discarded — <their reason>"` directly. Then ask whether to delete the branch now:

> The work is discarded. Delete the branch `agent/t012-<slug>` now? (yes / no)

On `yes`, run `git branch -D agent/t012-<slug>` yourself. On `no`, leave the branch intact and tell the user the audit log retains the `Evidence` row and rejection `Review` row regardless.

**The rule:** the agent picks the question, the user picks the answer, the agent runs the command. The handoff is the *decision*, not the *typing*.

---

### Step 4 — Ship the merged work

For every task that received `--approve`:

1. Merge the `agent/<task>-<slug>` branch to the project's main branch:

```bash
git checkout main
git merge --no-ff agent/t012-add-retry-backoff -m "merge: T012 add-retry-backoff"
git push origin main
```

Or open a PR from the branch and merge via the project's PR workflow. Reference the anvil task ID in the PR body for traceability:

```
Closes anvil:T012 — add-retry-backoff
```

2. After merging, clean up the branch:

```bash
git branch -d agent/t012-add-retry-backoff
```

Branches accumulate. After a task is `done` and its branch is merged, delete it. `anvil sync` (Phase 8) will flag undeleted agent branches as orphans.

anvil does not auto-merge. The deliberate separation between `apply` (state transition) and `merge` (git operation) means the reviewer controls the merge strategy, PR template, and commit message — without the tool imposing a workflow.

---

### Step 5 — Sync to external tracker (optional)

If the project has an external sync provider configured and the task is now at `status=done`, see [`docs/github-sync.md`](../../docs/github-sync.md) for the full CLI surface and conflict-resolution strategies. Otherwise, skip this step — the local `state.db` + `events.jsonl` is the canonical record.

---

## Common Pitfalls

- **Applying without reading the evidence.** `evidence_complete` is a heuristic based on `required_evidence` fields — it checks presence, not correctness. The diff and the output excerpt are the ground truth. Read them.
- **Rejecting without a `--reason`.** The flag is required. A rejection without a reason leaves the next agent (or the next session of the same agent) without context for why the task failed review. Concrete reasons prevent duplicate mistakes.
- **Applying out of dependency order.** If T013 depends on T012, apply T012 first and merge it before applying T013. Applying in the wrong order creates a branch that cannot be cleanly merged until its dependency lands on main.
- **Forgetting to delete merged branches.** Agent branches accumulate. After merge, `git branch -d` the branch. Running `anvil sync` (Phase 8) will report stale agent branches, but it is easier to clean up immediately.
- **Manually editing `state.db` to change a task status.** Use `anvil apply` so the `Review` row and status transition are recorded in `events.jsonl`. Direct edits produce state that cannot be replayed or audited.

For **decision-presentation discipline** — how to surface multi-option dispositions (accept/reject/hold/discard) as structured Q&A — see the canonical description in `/anvil:resolve-decisions`. The same pattern applies to any choice this skill surfaces.

---

## Composition with Other Skills

| Position | Skill |
|---|---|
| Before this skill | `/anvil:execute` — evidence must be submitted; task must be in `needs_review` |
| For read-only inspection before deciding | `/anvil:state-ops` — inspect queue without making a disposition |
| After reject + redraft | `/anvil:plan` — if the task needs re-scoping; `/anvil:execute` — to re-claim and re-attempt |
| After accept + merge | The project's normal PR + deploy workflow; anvil does not drive deployment |

Before invoking `anvil apply`, you may dispatch the plugin-local `sentinel` agent (if available in this session) against the task's evidence bundle. Sentinel produces a pass/fail recommendation that supplements — but does not replace — the reviewer's judgment. The `apply` call is always a human decision.

---

## Phase 5 Limitations

| Feature | Phase | Status |
|---|---|---|
| `anvil apply TASK_ID --approve` | Phase 5 | available |
| `anvil apply TASK_ID --reject --reason "..."` | Phase 5 | available |
| `anvil list --status needs_review` | Phase 3 | available |
| `anvil show TASK_ID` (with evidence) | Phase 5 | available |
| LLM-assisted disposition recommendation | Phase 7 | pending |
| GitHub Issues mirror (close issue when task done) | Phase 8 | pending |
| `anvil decision` CLI subcommand | Phase 6 | pending |
| Automated PR creation on accept | Phase 8 | pending (use `gh pr create` manually) |
