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

The table columns are `TaskID`, `Title`, `Status`, `Priority`, `Type`, `Score`, `Feature`. (To see who claimed a task and what the evidence was, run `anvil show TASK_ID` and `anvil apply TASK_ID`; see Step 2.) Commands used in this skill:

| Command | What it does |
|---|---|
| `anvil list --status needs_review` | List tasks awaiting review |
| `anvil show TASK_ID` | Print full task detail (criteria, scores, likely files) |
| `anvil apply TASK_ID` | Review-only: show the evidence gate without deciding |
| `anvil apply TASK_ID --approve` | Approve: needs_review → done |
| `anvil apply TASK_ID --reject --reason "..."` | Reject: needs_review → drafted |

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

### Step 2 — Inspect each task's detail and evidence gate

Two reads, both layout-aware:

```bash
anvil show TASK_ID
anvil apply TASK_ID
```

`anvil show TASK_ID` prints the task detail: title, feature, status, the six-dimension `Scores` (with an explanation block), `Dependencies`, `Conflict Groups`, `Acceptance Criteria`, `Verification Commands`, `Likely Files`, `Active Claims`, and `Recent Events` (where `evidence.submitted` confirms evidence was recorded). It does **not** print the evidence contents; for that, read the submission and the gate.

`anvil apply TASK_ID` with no `--approve`/`--reject` flag is **review-only**: it reports the task is awaiting review and prints the evidence gate without changing anything:

```
Task 'T012' awaiting review (status: needs_review).

Evidence gate: INCOMPLETE — missing items for required_evidence:
  - `pytest -x` exits 0

Pass --approve to accept or --reject --reason TEXT to reject.
```

An `INCOMPLETE` gate means a required-evidence item was not captured: the agent ran verification commands outside the hook window or the `capture-evidence.sh` hook did not fire. Do not approve over an incomplete gate without understanding why; the gap may indicate the verification was never actually run. (The gate is **advisory** by default: `--approve` still proceeds. Run `anvil apply --strict` to refuse approval while the gate is incomplete.)

Before approving:

- Confirm every acceptance criterion (from `anvil show`) has a corresponding verification command that exited 0.
- Confirm the files the agent changed match what the acceptance criteria required: a task that was supposed to modify `src/claims/manager.py` but only touched `tests/` is suspicious. The submitted `--files-changed` list appears in the `anvil submit` output and the task's `evidence.submitted` event.
- If a PR URL was linked at submit time (`anvil submit --pr-url`), open the PR and scan the diff to spot anything the evidence summary missed.

---

### Step 3 — Pick a disposition (the hard handoff gate)

This is the one place in the entire anvil workflow where the agent must wait for explicit user confirmation before executing the next command. `apply --approve` transitions the task and appends an immutable `task.applied` event to the append-only event log (and on approval writes a signed acceptance proof); it is the formal "ship it" gate. The agent must not run it on inference.

After surfacing the task detail and evidence gate from Step 2, present the disposition options conversationally and ask the user to pick — then run the chosen command yourself:

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

> Approving will transition T012 `needs_review → done` (through `accepted`) and append a permanent `task.applied` event with you as the approver. Confirm? (yes / no)

On `yes`, invoke `anvil apply T012 --approve` (or the equivalent `apply_review_decision` MCP tool). The command prints `Task 'T012' approved by '<reviewer>' → done.` and the path to a signed proof under the workspace's `proofs/` directory. Surface the response inline. Then ask whether to drive Step 4 (the ship sequence, i.e. the git merge) now or later. On `no`, return to the disposition prompt.

#### On "reject" (2)

Ask for a concrete reason before invoking:

> Reject T012 with which reason? Concrete is required — "pytest -x reports 3 failures in test_retry.py" is good; "not done" is not.

Once the user supplies a reason, invoke `anvil apply T012 --reject --reason "<their reason>"` directly. The command prints `Task 'T012' rejected by '<reviewer>' → drafted (rejection recorded; task returned to 'drafted' for rework).` and echoes the reason. Surface the response. The rejection is recorded as a `task.applied` event and the original branch + evidence are preserved in the audit log. Tell the user the task is back at `drafted` and ask whether to re-trigger `anvil review tasks` or leave it for the agent to fix the underlying issue.

#### On "hold" (3)

Do not invoke `apply` at all. Capture the open questions inline:

> What context do we need before this can be dispositioned? I will add it to the task notes so the next review has the full picture.

Once the user lists the open questions, record them. There is no CLI subcommand for editing a task's notes in place, so capture the context where the next reviewer will see it: edit the PRD at the path `anvil prd parse` echoes as `PRD source:`, re-run `anvil prd parse`, and coordinate with the next reviewer directly; drive that loop yourself. (PRD-level decision markers can be resolved with `anvil prd resolve-decision`; see `/anvil:resolve-decisions`.) Then loop back to Step 2.

#### On "discard" (4)

Like reject, but with a discard-specific reason — and you also need to clean up the branch.

> Discard T012 with which reason? (For the audit log — "approach superseded by T015" is typical.)

Invoke `anvil apply T012 --reject --reason "discarded — <their reason>"` directly. Then ask whether to delete the branch now:

> The work is discarded. Delete the branch `agent/t012-<slug>` now? (yes / no)

On `yes`, run `git branch -D agent/t012-<slug>` yourself. On `no`, leave the branch intact and tell the user the audit log retains the `evidence.submitted` and rejection `task.applied` events regardless.

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

Branches accumulate. After a task is `done` and its branch is merged, delete it. `anvil drift` reports undeleted `agent/<task>-<slug>` branches as orphan branches, so they will resurface there until cleaned up.

anvil does not auto-merge. The deliberate separation between `apply` (state transition) and `merge` (git operation) means the reviewer controls the merge strategy, PR template, and commit message — without the tool imposing a workflow.

---

### Step 5 — Sync to external tracker (optional)

If the project has an external sync provider configured and the task is now at `status=done`, run `anvil sync github` to push the disposition to GitHub Issues (use `--task TASK_ID` to scope to one task); see [`docs/github-sync.md`](../../docs/github-sync.md) for the full CLI surface and conflict-resolution strategies. Otherwise, skip this step; anvil's local state store is the canonical record.

---

## Common Pitfalls

- **Applying without reading the evidence.** The `Evidence gate` shown by `anvil apply TASK_ID` is a presence check against the task's `required_evidence`: it checks that items were captured, not that they are correct. The diff and the submitted commands are the ground truth. Read them.
- **Rejecting without a `--reason`.** The flag is required. A rejection without a reason leaves the next agent (or the next session of the same agent) without context for why the task failed review. Concrete reasons prevent duplicate mistakes.
- **Applying out of dependency order.** If T013 depends on T012, apply T012 first and merge it before applying T013. Applying in the wrong order creates a branch that cannot be cleanly merged until its dependency lands on main.
- **Forgetting to delete merged branches.** Agent branches accumulate. After merge, `git branch -d` the branch. `anvil drift` will report stale `agent/<task>-<slug>` branches as orphans, but it is easier to clean up immediately.
- **Hand-editing the state store to change a task status.** Use `anvil apply` so the disposition and status transition are recorded as a `task.applied` event in the append-only log. Direct edits to the database produce state that cannot be replayed or audited.

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

### Tier-aware review depth

The work packet (and `anvil show` / `anvil next`) carries a derived
**review tier** — `light`, `standard`, or `max` — computed from the task's
six-dimension score plus its risk-confirmation flags. Read the packet's
`Review tier:` line (or the `review_tier` JSON key) and dispatch review
effort at the matching depth instead of reviewing everything at maximum:

| Review tier | What the reviewer runs before `apply` |
|---|---|
| `light` | Evidence-gate check only — confirm the submitted commands/files satisfy the required evidence. Confirmed low-risk fast-lane change; no diff read required. |
| `standard` | Evidence gate **plus** a read of the diff against the acceptance criteria. |
| `max` | Evidence gate, diff read, **and** an adversarial pass — dispatch the plugin-local `critic` agent (or the session's strongest reviewer) to actively refute the change. For a task that declares an **evidence contract** (named `claims` / `Artifact assertions`), also dispatch the `sentinel` agent in its **evidence-critic** mode to return a `PROVEN`/`UNPROVEN` verdict per claim (treating diagnostic-category evidence as non-completion) before you approve. High or unconfirmed risk; an unscored task always lands here. |

When the task declares an evidence contract, `anvil apply` prints a
**claim-grouped verdict** (human `Claim <id>: PROVEN/FAILED/...` lines, or
the `claim_verdict` JSON key) and, for a task declaring a contract, an
approval **refuses** with exit 1 / error code `claim_unproven` while any
enforceable claim is unproven — the task stays in `needs_review`. Read that
block first: it tells you exactly which claim the mechanical gate could not
prove, so the `max`-tier evidence critic can focus its semantic pass there.
An advisory `Intent check` block (`intent_warnings`) flags intents the
contract never bound — a prompt to ask whether an artifact assertion is
missing.

The tier is advisory routing for review *effort* — the human
`apply --approve` decision is unchanged at every tier, and a reviewer may
always choose a deeper pass than the tier suggests (never a shallower one
for `max`).

---

## Scope and limitations

The review-and-ship commands are all available:

- `anvil list --status needs_review`, `anvil show TASK_ID`, and `anvil apply TASK_ID` (review-only, `--approve`, `--reject --reason "..."`).
- GitHub Issues sync is available via `anvil sync github` (see Step 5).
- A pass/fail review recommendation is available via the plugin-local `sentinel` agent (see "Composition with Other Skills"); it supplements, never replaces, the human decision.

What `finish` deliberately does **not** do:

- **No auto-merge and no auto-PR.** `apply` is a state transition only; the git merge / PR is yours to drive (Step 4). This separation is intentional: the reviewer owns the merge strategy and commit message.
- **No in-place task-note editor.** To attach context on a "hold" disposition, edit the PRD and re-parse (Step 3, "hold").
