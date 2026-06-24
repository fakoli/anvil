---
name: plan
description: Turn a reviewed PRD into a ready-to-execute task graph — generate features and tasks, score each on six dimensions, surface dependencies and conflict groups, promote drafted tasks to ready. Use this skill once the PRD is approved and before any agent claims work.
---

# Plan — PRD to Ready Task Graph

Convert an approved PRD into a queue of agent-ready tasks. This skill drives four sequential state transitions: PRD requirements → features and tasks → scored tasks → reviewed-and-ready tasks. Once the queue contains at least one `ready` task, agents can claim work.

---

## When to Use

- Immediately after `anvil prd review --approve` — the PRD is approved and the task graph does not yet exist.
- After a significant PRD revision that adds new `## Features` or `## Tasks` sections — re-plan to generate the updated task graph.
- When `anvil status` shows `PRD: approved` but `Tasks: 0 total (0 ready, ...)` and no tasks exist yet.
- When tasks exist but none have scores — scoring was skipped or the plan was never completed.

**Do not use this skill for re-scoring individual tasks, managing claims, or adjusting task status after work has started.** Once tasks are `ready`, proceed to `/anvil:execute`. Use `/anvil:state-ops` for inspection at any point.

---

## Prerequisites

The PRD must be parsed and in at least `reviewed` status. Confirm before proceeding:

```bash
anvil status
```

Plain `anvil status` prints a `PRD: <status>` line. Look for `PRD: reviewed` or
`PRD: approved`. If it reads `PRD: draft` or `PRD: none`, proceed to `/anvil:prd`
first. (The compact `prd-status:` token only appears under `anvil status
--hook-format`, which the SessionStart hook consumes; you want the plain output
here.)

Commands used in this skill (all ship in 0.1.1):

| Command | Purpose |
|---|---|
| `anvil plan` | Generate features and tasks from the parsed PRD |
| `anvil score [TASK_ID]` | Score tasks across six dimensions |
| `anvil expand TASK_ID --use-llm` | Propose sub-tasks for an oversized task |
| `anvil review tasks` | Promote tasks drafted → reviewed → ready |
| `anvil list [--status X]` | List tasks |
| `anvil show TASK_ID` | Print full task detail |

---

## Workflow

### Step 0 — Scan for unresolved decisions (soft gate, v1.14.0)

Before running `plan`, drive `anvil prd find-decisions` (or the `find_decisions` MCP tool) yourself. The planner's task generation is shaped by the PRD's requirements and features — if those still contain `[NEEDS DECISION]` markers or unresolved Open Questions, the generated task graph will inherit the ambiguity. Surfacing unresolved items before plan runs is cheap; after plan runs, the same ambiguities will land as task descriptions that need re-editing and re-planning.

If `find_decisions` returns empty, skip this step entirely — do not even mention it to the user. The soft gate only fires when there is something to decide.

If it returns non-empty, present the summary and ask:

> Before I generate the task graph, the PRD has **N unresolved items** that will shape what `plan` produces:
> - X `[NEEDS DECISION]` markers (these often live inside requirements or features the planner will derive tasks from)
> - Y `## Open Questions` (these often imply additional tasks once answered)
> - Z missing fields on existing tasks (the review gate will block these later anyway)
>
> Want me to walk them as Q&A now, or proceed to `plan` without resolving? (resolve now / proceed anyway / show me the list)

On `resolve now`, bridge to the `resolve-decisions` skill. After it returns, drive a fresh `prd parse` (resolution edits the markdown; state.db needs to catch up) and then continue with Step 1 below.

On `proceed anyway`, continue to Step 1. The task graph will reflect the ambiguity — flag this back to the user inline ("noting we are planning against N unresolved decisions; the planner will treat any tasks that derive from unresolved items as proposed-pending"). The decisions will surface again at `review tasks` time, and the user can resolve them then.

On `show me the list`, surface a compact one-line-per-item view, then re-ask.

The soft-gate design is deliberate: `find-decisions` non-empty does NOT block planning. The agent surfaces the cost of proceeding without resolving and lets the user choose the cadence.

---

### Step 1 — Generate features and tasks (`plan` guarantees tasks as of v1.15.0)

Invoke `anvil plan` yourself — via Bash, the MCP `plan_tasks` tool when available, or whichever execution primitive the runtime exposes:

```bash
anvil plan           # add --prd <prd_id> to scope to one named release PRD
```

Reads the parsed PRD from `state.db` and emits `feature.created` and `task.created` events. **Multi-PRD scoping:** `plan` (and its orphan-prune below) operates on the **selected PRD only** — pass `--prd <prd_id>` to plan a named release PRD; omit it for the default. Dependency inference and conflict-group detection run automatically — tasks that share `likely_files` entries are grouped into the same conflict group. **Read-only note:** conflict groups span **ALL PRDs**, not just the one you planned — `anvil next` will not route two file-overlapping tasks across PRDs into parallel claims, so a `v0.2` task colliding on a file with an active `default` claim stays blocked until that claim clears.

**The CLI now GUARANTEES tasks AND orphan-free state (v1.15.0).** Two integrity guarantees were added together in v1.15.0:

1. If the PRD has features+requirements but no `## Tasks` section, `plan` calls the LLM itself to generate them (instead of silently returning `0 tasks` and forcing the agent to dispatch a separate planner subagent).
2. If tasks were removed from the selected PRD between parses, `plan` emits `task.deleted` events automatically so state.db stays in sync (instead of leaving orphans behind). Same for features. The prune is scoped to the planned PRD's partition — other PRDs' tasks are never touched. Safe statuses (proposed / drafted / ready) prune silently; unsafe statuses (claimed / in_progress / needs_review / …) fail loudly with a clear list and the `--prune-force` escape hatch. Tasks with claims/evidence rows can NEVER be deleted at the SQL layer (the audit history is FK-protected by schema).

The output line tells you what happened. A plain run prints `Planned N features, M tasks.`; when the LLM backstop fired, the line names the count and the exact PRD file it appended to:

```
Planned 3 features, 19 tasks (19 generated via LLM (anthropic), appended to /home/you/.anvil/workspaces/<key>/.anvil/prd.md).
```

The `appended to <path>` segment echoes the real PRD file location, which lives in the HOME workspace under the default layout — read it off this line rather than assuming an in-repo path.

When you see `(N generated via LLM ...)`, surface it explicitly in chat so the user knows their PRD was modified:

> Plan generated 3 features and 19 tasks. The PRD had no `## Tasks` section, so I generated them via LLM and appended a `## Tasks` block to the PRD file (the path is echoed in the `appended to …` line, auditable on disk). Want to review the generated tasks before continuing? (show me / looks good / I want to edit first)

If the LLM call fails (no `ANTHROPIC_API_KEY`, network failure, malformed response), the CLI exits non-zero with a clear message. **Do not paper over a failure by dispatching the planner subagent as a workaround** — surface the error to the user and ask whether they want to set up the LLM path or author tasks manually in `## Tasks`.

If you genuinely don't want LLM auto-gen for a specific call (e.g. on a CI machine without API keys), pass `--no-llm`. The CLI exits 1 with a clear "0 tasks generated; author them manually" message.

**Pause and present the task list.** Run `anvil list` yourself and present titles, features, and priorities in chat:

> Plan generated 3 features, 8 tasks. Here they are:
> [list output]
> Anything mis-scoped or missing before I run `score`? (yes / looks good / let me check first)

Catching mis-scoped tasks here costs one loop; catching them after scoring or claiming costs three.

### Step 1.5 — Present post-plan decisions as structured Q&A

**One decision per turn. Ask, wait for the answer, apply, then surface the next decision.** Never batch three decisions into one wall-of-questions — that's the same anti-pattern the resolve-decisions skill names at the PRD layer, and it produces the same failure mode here (the user picks one and leaves the rest unresolved, or skips everything because the wall is overwhelming).

When the LLM-generated task list lands, it may carry decisions the user has to make before scoring/claiming starts — for example: scope overruns ("87h estimated, 80h budget"), structural concerns about the PRD ("R010 says ≥32 tools but F003 description says ≥35"), or expansion candidates the LLM flagged. **Surface each decision as a structured Q&A turn, not as prose with bullets.**

For Claude Code runtimes, use the `AskUserQuestion` tool so the user gets a structured pick UI rather than free-form text to type. For other runtimes, fall back to explicit numbered prompts:

> **Decision 1 — Scope overrun (87h vs 80h budget)**
> The generated tasks total ~87h of work; your declared phase budget is 80h. How should we resolve the 7h overrun?
> 1. Cut T014 + trim T002 (lands at ~80h; F004 keeps T012+T013)
> 2. Cut T008 + T018 + trim T007 (distributed across features)
> 3. Defer T017 (Wasm network policy — affects F005)
> 4. Keep all tasks and accept the overrun
>
> Pick 1 / 2 / 3 / 4 (or describe your own).

Always: agent generates the question, proposes 2-4 candidate answers when the surrounding context allows, accepts the pick, applies the choice (edit `prd.md`, re-parse, etc.). One decision per turn — do NOT batch three decisions into one question.

When the LLM flagged tasks for expansion, do **not** open a per-task Q&A here — expansion is no longer a decision the user makes task-by-task. Scoring (Step 2) emits an EXPANSION QUEUE for every task at/above the configured `auto_expand_threshold`, and Step 3 auto-expands the whole queue with one summary checkpoint at the end. Only surface expansion as a question if the project has opted out (`auto_expand: false` in `.anvil/config.yaml`) or the user has said they want to pick manually.

The one-decision-per-turn rule still applies whenever the post-plan output surfaces structural concerns about the PRD (e.g., "R010 vs F003 drift"). Each concern is one Q&A turn with proposed fix options, not a wall of "issues to consider."

---

### Step 2 — Score every task

Once the user confirms the task list, invoke the scorer yourself:

```bash
anvil score
```

Populates all six dimensions on each `Task`. The scorer is rule-based — no LLM required. Dimensions:

| Dimension | Scale | What it measures |
|---|---|---|
| `complexity` | 1–5 | Estimated implementation effort |
| `parallelizability` | 1–5 | How independently this task can run from others |
| `context_load` | 1–5 | How much context an agent needs to hold while working |
| `blast_radius` | 1–5 | How much of the codebase a mistake here could damage |
| `review_risk` | 1–5 | How carefully a human reviewer needs to inspect the output |
| `agent_suitability` | 1–5 | How well-suited a typical frontier model is to this task |

When tasks score at/above the configured `auto_expand_threshold` (default 4) and `auto_expand` is enabled (default true), the CLI output ends with an **EXPANSION QUEUE** section — one entry per oversized task with its complexity, a suggested sub-task count, and the exact follow-up command:

```
EXPANSION QUEUE (complexity >= 4)
---------------------------------
  T001         complexity=5  suggested-subtasks=4  Storage backend refactor
    $ anvil expand T001 --use-llm
  T003         complexity=4  suggested-subtasks=3  Auth middleware
    $ anvil expand T003 --use-llm

2 task(s) queued for expansion. ...
```

**The queue drives Step 3 automatically — do not ask the user per task.** Unless the user opted out (`auto_expand: false` in `.anvil/config.yaml`, or they said so in chat), proceed straight to Step 3 and expand every queued task. The queue replaces the old "flag for expand and ask" dance: the score already made the decision; your job is to execute it and present one summary afterward.

Two score signals still warrant explicit attention in chat (these are NOT auto-handled):

- **`agent_suitability <= 2`**: flag for human attention. Low suitability means the task involves judgment calls, ambiguous requirements, or architecturally broad changes that a model is likely to get wrong.
- **`blast_radius >= 4`**: flag for careful claim ordering. High blast-radius tasks touch foundational code and should not run in parallel with other tasks that share files.

---

### Step 3 — Auto-expand the queued tasks (v1.21.0)

**Default behavior: expand every task in the EXPANSION QUEUE automatically — no per-task user Q&A.** Dispatch the planner agent (`agents/planner.md`) to work the queue, or drive the commands yourself when the runtime has a shell:

```bash
anvil expand T001 --use-llm --format prd
```

For each queued task: run the expand command, take the returned `### T00X.N` blocks, apply them to the `## Tasks` section of the PRD file (the path `anvil prd parse` echoes as `PRD source:` — it lives in the HOME workspace under the default layout, not in-repo), dropping or keeping the parent block per the parser's behavior — confirm before removing. Then re-run the pipeline once at the end:

```bash
anvil prd parse
anvil plan
anvil score
```

**Skip auto-expansion only when the user opted out** — `auto_expand: false` in `.anvil/config.yaml` (the queue section will not even render), or an explicit instruction in chat ("don't split anything yet"). In the opt-out case, fall back to asking once: "N tasks scored at/above the expansion threshold — want me to expand them?"

**One summary checkpoint after the queue is drained.** Do not narrate each expansion as a separate decision; collect the results and present a single recap before moving to Step 4:

> Auto-expanded 3 queued tasks:
> - T001 (complexity 5) → T001.1–T001.4 (storage backend split by layer)
> - T003 (complexity 4) → T003.1–T003.3 (auth middleware: parse / verify / wire)
> - T007 (complexity 4) → T007.1–T007.3 (migration: schema / backfill / cutover)
>
> Re-scored: no remaining tasks at/above threshold. Anything you want re-merged or re-split before I run `review tasks`?

**Recursion is automatic across re-scores (v1.23.0).** After expansion, an
expanded parent becomes a *container* and rolls out of the queue — its stored
complexity score is preserved (audit history), but it is no longer surfaced as
actionable. The re-score in the recap step then evaluates the new *children*:
any child that is itself still at/above the threshold re-enters the EXPANSION
QUEUE, so deep work decomposes lineage-by-lineage without a separate "recurse"
command. Two safety rails bound this: a child more than `DEFAULT_RECURSION_DEPTH_CAP`
(3) levels deep is dropped from the auto-queue — repeated splitting of one
lineage is a signal the PRD block needs human restructuring, not another
automatic split — and a malformed parent cycle is detected and skipped rather
than looped. If the recap shows the same lineage expanding round after round,
stop and restructure that part of the PRD by hand.

If a re-score still queues a task (a sub-task scored at/above threshold again), surface it in the same checkpoint rather than silently looping — repeated expansion of the same lineage is a sign the PRD block needs human restructuring, not another LLM pass.

If the LLM call fails (no API key, network failure), surface the error and fall back to proposing subtask blocks inline in the conversation, applying them to `prd.md` after the user confirms.

---

### Step 4 — Review tasks to promote them

Invoke the gate yourself:

```bash
anvil review tasks
```

Promotes tasks through `drafted → reviewed → ready`. The gate checks two conditions for each task:

1. `acceptance_criteria` is non-empty.
2. `verification.commands` is non-empty (at least one shell command).

Tasks that pass both conditions are promoted to `ready`. Tasks that fail the gate stay at `drafted` with a failure reason printed. A clean run prints:

```
Promoted 3 task(s) to reviewed.
Promoted 3 task(s) to ready.

6 total promotion(s). No tasks blocked.
```

A run with a gate failure prints the blocked task and its exact reason:

```
Promoted 0 task(s) to reviewed.
Promoted 0 task(s) to ready.

Blocked 1 task(s):
  T004: Task 'T004' cannot move to 'reviewed': verification.commands (must be non-empty).
```

For each blocked task, surface the exact missing field in chat — do not just report "blocked". Propose the fix inline, apply it to the PRD after confirmation (edit the `## Tasks` block in the file `anvil prd parse` echoes as `PRD source:`), re-parse, and re-run `review tasks` yourself. Do not retry without fixing the underlying gap — the gate will block on the same condition again.

When the gate passes for every expected task, run `anvil list --status ready` yourself and present the queue:

> Review passed. Ready queue:
> [list output]
> Ready for `/anvil:execute`? (yes / not yet — anything to adjust first)

---

### Step 5 — Verify the ready queue

If `anvil list --status ready` returned non-empty in Step 4 and the user confirmed, the plan is complete — hand off into `/anvil:execute` by invoking that skill, not by listing CLI commands.

If the ready list is empty after Step 4 succeeded, something blocked the gate. Diagnose inline:

```bash
anvil list --status drafted
```

Read every row, surface the failure reason, propose the fix in chat, apply it to `prd.md` after confirmation, then re-run the relevant pipeline steps yourself.

---

### Step 6 — Drill into specific tasks

Run `anvil show TASK_ID` yourself whenever a task looks suspicious — a title that seems too broad, a `blast_radius` of 5 on something that should be isolated, or a dependency chain that creates a bottleneck:

```bash
anvil show T003
```

Surface the result inline. `anvil show` prints these sections: title, feature, status, priority, the six-dimension `Scores` breakdown (with an `Explanation` block), `Dependencies`, `Conflict Groups`, `Acceptance Criteria`, `Verification Commands`, `Likely Files`, `Active Claims`, and `Recent Events`. (The JSON form exposes the task's `likely_files` and `description` fields — note `likely_files`, not `expected_files`; `expected_files` is a claim-level field, not a task field.) These are planning issues that are far cheaper to fix before claiming than after.

---

## Anti-pattern to avoid

Ending this skill with a numbered list like "1. Run `score` 2. Expand T001 3. Run `review tasks` 4. Run `list --status ready` 5. Run `/anvil:execute`..." That handoff style only makes sense when the work is leaving this session entirely — queued for another agent, scheduled for tomorrow, blocked on stakeholder review. When the agent and user are in the same conversation, drive each command, surface its output, and present the next decision. Pause-and-present discipline is the whole point of interactive driving — it preserves the user's judgment at every gate without forcing them into a CLI.

**When to actually hand off CLI commands:** if the user explicitly opts out ("just give me the commands"), or if the runtime lacks the tool needed to execute them (e.g., MCP-only client with no shell and no `plan` tool). In those cases, a CLI list is the right output. Otherwise, drive.

---

## Common Pitfalls

- **Planning against an unreviewed PRD.** The claim gate requires the PRD to be `reviewed` *or* `approved` (only `draft`/`rejected` block) — but planning against a `draft` PRD produces a task graph that will be replaced on the next parse. Get the PRD to at least `reviewed` before running `plan`.
- **Running `plan` twice without re-parsing.** A second `plan` invocation on unmodified state will either re-emit duplicate events or error with a conflict. If the PRD has changed, re-parse first. If nothing has changed, skip `plan`.
- **Treating scores as fixed truth.** The scoring engine uses rule-based heuristics against task fields. A task with a one-word description will score misleadingly. If a score seems wrong (e.g., `blast_radius: 1` on a task that clearly touches a shared schema file), adjust the `**Likely files:**` field in `prd.md`, re-parse, and re-score.
- **Skipping the pause after `plan`.** Jumping straight to `score` and `review tasks` without reviewing the task list means catching structural problems only after the queue is ready. A task graph that doesn't reflect real work is worse than no task graph.

---

## Composition with Other Skills

| Position | Skill |
|---|---|
| Before this skill | `/anvil:prd` — PRD must be at least `reviewed` |
| After Step 1 (plan) | `/anvil:state-ops` — inspect the raw task graph before scoring |
| After Step 5 (ready queue confirmed) | `/anvil:execute` — agents can now claim and work tasks |
| If `show TASK_ID` reveals complexity at/above `auto_expand_threshold` | Expand in Step 3, then re-run `score` and `review tasks` |

---

## Command Reference

Every command this skill names ships in 0.1.1. Notes on the LLM-augmented and
adjacent commands:

| Command | Notes |
|---|---|
| `anvil plan` | Generates features and tasks; LLM backstop fills a missing `## Tasks` section |
| `anvil score` | Rule-based by default; `--use-llm` appends a trade-off summary (numeric scores unchanged) |
| `anvil review tasks` | Promotes drafted → reviewed → ready |
| `anvil list` | Columns: TaskID, Title, Status, Priority, Type, Score, Feature |
| `anvil show TASK_ID` | Full task detail, including `Likely Files` |
| `anvil expand TASK_ID --use-llm` | Proposes 2-5 sub-tasks; driven automatically by the Step 2 EXPANSION QUEUE |
| `anvil next` | Picks the highest-priority claimable task without claiming it |
| Planner agent (`agents/planner.md`) | Dispatched by `plan` when the PRD has no `## Tasks` section |
