---
name: prd
description: Author, parse, and review a project PRD in anvil — capture the requirements that everything downstream (features, tasks, claims, evidence) gets generated from. Use this skill when starting a new project or revising requirements before any planning work happens.
---

# PRD — Author, Parse, and Review Requirements

Write the contract that everything downstream depends on. The PRD is the single source of truth for every `Requirement`, `Feature`, and `Task` row in `state.db`. Nothing can be claimed until this document exists, parses cleanly, and clears the review gate.

---

## When to Use

- Starting a new project — before any planning, scoring, or task assignment happens.
- Revising the PRD after stakeholder feedback changes the scope or acceptance criteria.
- Recovering after a scope change mid-project — re-anchor what the work is before resuming claims.
- Before any invocation of `/anvil:plan` — planning reads from a parsed PRD; authoring must come first.
- When `anvil status` reports `PRD: draft` or `PRD: none` and the project can't proceed.
- When a co-authored PRD is ready for a formal review and approval step.

**Do not use this skill to generate or score tasks.** Once the PRD is approved, proceed to `/anvil:plan` for the task graph. This skill only authors, parses, and reviews requirements.

---

## Prerequisites

The project must be initialized. State lives in the HOME workspace by default (`~/.anvil/workspaces/<key>/.anvil/...`), not in the repo, so check init through the CLI rather than a literal path:

```bash
anvil status >/dev/null 2>&1 || echo "MISSING: run anvil init first"
```

If it reports `MISSING`, run:

```bash
anvil init --name "<project-name>"
```

`anvil init` echoes the workspace paths and the prd.md location it expects (`Next step: author your PRD at <path>`). `anvil status` prints a `Path:` line with the active `.anvil` directory; use that whenever you need to read or write the PRD, not a hardcoded in-repo path.

The structured template at `docs/prd-template.md` (relative to the plugin root) is the canonical contract. The parser enforces it — any deviation from the required sections produces a parse error.

---

## Workflow

### Step 0 — Select or create the PRD

A project can hold several release-scoped PRDs in one `state.db`; each is separately gated. Run `anvil prd list` to discover them: it prints one line per PRD (`<id>  [<status> r<revision>]`, with the default PRD marked `*`). Pick which one you are authoring. **If only one PRD exists, auto-select it silently, say nothing, proceed to Step 1.** Only prompt when `anvil prd list` shows more than one PRD:

> This project has multiple PRDs: `default` (approved), `v0.2` (draft). Which one are you authoring — or are you creating a new release PRD? (default / v0.2 / new)

The selected `<prd_id>` scopes everything below: its on-disk file, its overwrite/approval gates, and the `Requirement`/`Feature`/`Task` rows that re-parse replaces. To create a new named PRD, author its file at the per-PRD path the CLI resolves (Step 1) and parse it with `anvil prd parse --prd <prd_id>`. The default PRD takes no flag.

---

### Step 1 — Author or update the selected PRD

Drive this step inline. Check for an existing file before suggesting any edit — the subsequent `anvil prd parse` step (Step 2) rewrites the `Requirement`, `Feature`, and `Task` rows **in the selected PRD's partition** of `state.db` (the first parse of a PRD is a destructive create; a re-parse amends non-destructively — see "Revising a PRD" below).

First resolve the PRD path from the CLI — never hardcode it. `anvil status`
prints the active `.anvil` directory and `anvil prd source-name [--prd <id>]`
prints the portable relative source name. Join those two values; this preserves
uppercase and Windows-reserved IDs without exposing an absolute path during
parse. `anvil init` also echoes the default PRD location. Read the resolved file
with whatever read primitive the runtime exposes (Bash, MCP filesystem tool).

**If the file exists**, do not edit or re-parse without confirmation. Read the file, surface a one-line summary (first heading and total line count are usually enough), and ask:

> The PRD already exists (`<first-heading>`, `<N>` lines). Open it for editing, save the current copy as a backup first, or leave it alone? (edit / save-as-backup / cancel)

- On `edit` — read the file in full, propose changes inline (show diffs in chat), and apply them once the user confirms. Do not shell out to `$EDITOR` and wait — drive the edits in the conversation.
- On `cancel` — stop. Confirm the PRD is untouched; offer to run `/anvil:state-ops` to inspect current PRD status.
- On `save-as-backup` — copy the existing PRD to `prd.md.bak` in the same workspace directory, then proceed with inline edits as above.

**If the file does not exist**, author it inline. Compose the draft in the conversation (using the structure below), present it to the user for approval, then write it directly to the PRD path the CLI reported. Do not tell the user to open `$EDITOR` themselves.

The canonical structure is defined in `docs/prd-template.md`. Required sections — the parser fails without them:

- `# Project: <Name>` — H1 title, first line of the file
- `## Summary` — one prose paragraph
- `## Goals` — only the `## Goals` heading must be present (an empty list under it still parses), but author at least one concrete goal
- `## Requirements` — bulleted list of `R001: ...` items

Optional sections that should be present in any non-trivial PRD:

- `## Non-Goals` — even if the answer is "none stated", declare it
- `## Acceptance Criteria` — project-level verifiability, not per-task
- `## Features` — logical groupings of related tasks
- `## Tasks` — hand-authored tasks with `**Acceptance criteria:**` and `**Verification:**` fields
- `## Risks`, `## Open Questions` — informs the planner's scoring
- `## Assumptions` — stable, bounded premises with a rationale and optional
  requirement references; global when no requirements are listed

#### Co-authoring with the user

When co-authoring, resist the urge to dump the full template at once. Drive one topic at a time in the conversation:

1. **What are the goals?** — ask the user what success looks like; capture each answer as a `## Goals` bullet.
2. **What are the requirements?** — translate each "the system must" statement into an `R00N:` bullet and read them back for confirmation.
3. **What are the features and tasks?** — group related requirements, propose the units of work, and confirm groupings before writing.

Separate each topic as its own exchange. Confirm the goals look right before moving to requirements. Only write the file once the user has accepted the final draft.

---

### Step 2 — Parse the markdown into state

Invoke the parse yourself once the file is written — do not hand the user a command to type. Use Bash (`anvil prd parse`), the MCP `parse_prd` tool when available, or whichever execution primitive the runtime exposes:

```bash
anvil prd parse           # default PRD; add --prd <prd_id> for a named release PRD
```

This reads the selected PRD's source (the command echoes only its stable
identity: `default`, the named ID, or `custom`; pass `--file PATH` to point
elsewhere), validates structure, and writes `Requirement`, `Feature`, and `Task`
entities into **that PRD's partition** of `state.db` — other PRDs are untouched.
PRD status becomes `draft`. Surface the parser output inline in the same message
so the user sees the result without a context switch.

**On parse error:** the parser prints a line per problem in the form `Parse error [## Section:0]: <message>`, then exits with `Error: PRD parse failed with N error(s). Fix the issues above and re-run.` Existing `state.db` content is preserved (no silent rollback of previous good state). Read the error, propose the fix inline, apply it after confirmation, and re-run `prd parse` yourself.

Common parse errors:

- `Parse error [## Summary:0]: Missing required '## Summary' section.` (the heading is absent)
- `Parse error [## Goals:0]: Missing required '## Goals' section.` (only the `## Goals` heading is required; an empty list under it still parses)
- `Parse error [## Requirements:0]: Missing required '## Requirements' section.` (the heading is absent)
- A duplicate requirement ID (the same `R00N:` twice) aborts the write with a unique-constraint error; renumber so each ID is distinct

**On success:** the command prints a summary line and stable `PRD source:` identity. Present the counts to the user and confirm they match expectations:

```
Parsed 6 requirements, 3 features, 8 tasks.
```

> Parsed 6 requirements, 3 features, 8 tasks. Counts look right? Ready for me to run `prd review`? (yes / let me check first)

If the counts are wrong, read `prd.md` and confirm all sections survived the parse without truncation. Re-run until the counts match intent.

**After parse, scan for unresolved decisions before review (soft gate, v1.14.0).** Run `anvil prd find-decisions` (or call the `find_decisions` MCP tool) yourself. If it returns non-empty, do not just list the items — present the summary and ask the user how they want to handle them:

> The parse succeeded, but the PRD has **N unresolved items** that will shape downstream planning:
> - X `[NEEDS DECISION]` markers
> - Y `## Open Questions` items
> - Z missing acceptance-criteria or verification fields on tasks
>
> Want me to walk them as Q&A now, or proceed to review without resolving? (resolve now / proceed without / show me the list)

On `resolve now`, bridge to the `resolve-decisions` skill directly — it drives each item as a one-question turn with proposed options and applies answers to `prd.md`. After resolution, return here for Step 3.

On `proceed without`, continue to Step 3 — Open Questions are informational and don't gate review or approval. Note inline that the items remain in `find-decisions` for later.

On `show me the list`, surface a compact one-line-per-item view, then re-ask the same question.

The soft gate by design — `find-decisions` non-empty does NOT block review. The agent's job is to surface the choice, not to force resolution.

**Optional challenge mode.** Keep the standard experience unchanged unless the
user explicitly asks to be challenged. In that mode, run `anvil prd assess`
(or planning MCP `assess_prd`) after the draft parses, present the highest-value
advisory finding, and ask its suggested question one at a time. The user may
clarify, defer, or accept a documented `## Assumptions` entry. Findings never
block review or approval.

**Explicit autonomous continuation.** When the user says to continue the
workflow autonomously, do not turn assessable gaps into follow-up questions.
Infer only a bounded, reversible default supported by the PRD and repository;
write it as an `A###` assumption with rationale and requirement references,
then re-parse and re-assess before planning. Continue while reporting remaining
advisories. Stop for new external authority, a conflict with scope/non-goals,
or no bounded safe default. The user must still explicitly authorize approval,
merge, publishing, and other existing authority boundaries.

---

### Step 3 — Review the PRD

Run the review yourself once the user is ready. Before invoking it, audit the PRD inline for completeness — surface gaps in the conversation so the user can decide what to fix before the gate fires:

- Are goals concrete statements ("Users can export a CSV with one command") rather than aspirations ("good performance")?
- Is `## Non-Goals` declared — even as a single item? A missing non-goals section is a red flag in any non-trivial project.
- Are `## Acceptance Criteria` written as independently verifiable statements, not restatements of goals?
- Does every task have a non-empty `**Acceptance criteria:**` block and at least one `**Verification:**` command?
- Are open questions either resolved or explicitly parked as known unknowns?

Present any gaps directly in chat:

> Before I run `prd review`, three things might need attention:
> - T003 has no verification commands — want me to add `pytest tests/test_t003.py`?
> - `## Non-Goals` is absent — even "none declared for v1" is better than silence; add it?
> - R004 says "the system handles errors" — what kind, and how? Make this measurable.
>
> Want me to apply these fixes and re-parse, or run review as-is?

Once the user accepts or addresses the items, invoke the review:

```bash
anvil prd review          # add --prd <prd_id> to review a named release PRD
```

Surface the output inline. The review gate is **per-PRD** — it transitions only the selected PRD's status. If it passes, that PRD becomes `reviewed`; tell the user and move to Step 4.

---

### Step 4 — Approve when ready

`prd review --approve` is a hard gate, scoped **per-PRD**. It transitions the selected PRD from `reviewed` to `approved`, and the `anvil claim` gate keys on the **owning PRD of the task**: a task is claimable only when *its* PRD is `reviewed` or `approved` — approving `default` does nothing for a `draft` `v0.2` task. Because approval is permanent in `events.jsonl`, the user MUST explicitly confirm before the agent runs it.

Before asking, read the full PRD back to the user (or show a concise structural summary — sections present, requirement/feature/task counts, any items the review surfaced). Then ask:

> The PRD is reviewed. Approving it is permanent and opens the claim gate. Ready to approve? (yes / no / let me re-read first)

- **On `yes`** — invoke `anvil prd review --approve` yourself (add `--prd <prd_id>` for a named PRD; it prints `PRD approved by '<reviewer>'.`), surface the output, then run `anvil status` and confirm the selected PRD's block shows `approved`. Tell the user the project is ready for `/anvil:plan` and ask whether to drive that skill next.
- **On `no`** — stop. The PRD stays in `reviewed`; the user can come back to it later.
- **On `let me re-read first`** — wait. When the user signals ready, return to the confirm prompt above.

**Keep approval a deliberate, separate step.** In a team context, the reviewer and approver should differ: the agent reviews for structural completeness; the human approves the scope. In a solo context, the read-back-then-confirm pattern above is the substitute for a second pair of eyes.

---

## Anti-pattern to avoid

The agent drives commands inline; it does not hand the user a numbered CLI to-do list. See `/anvil:plan` for the canonical statement. **When to actually hand off CLI commands:** if the user explicitly opts out, or if the runtime lacks the required tool. Otherwise, drive.

---

## Revising a PRD (amend-aware, diffable, replayable)

A PRD is revisable: the FIRST parse of a `prd_id` emits a `prd.parsed` event, and every later re-parse of that same `prd_id` emits a `prd.revised` event into `events.jsonl`, so each revision is an **amend** appended to the history, not an overwrite of the past. The change is **diffable** (`prd.revised` records the new requirements as added/superseded/unchanged against the prior parse) and the whole project stays **replayable** — rebuilding `state.db` from `events.jsonl` reconstructs the PRD at any revision. Re-parse scopes to the **selected PRD only**: other PRDs' partitions are untouched. (To filter the audit trail for a revision, look for `prd.revised`, not `prd.parsed`.)

Here is the safe sequence for a revision:

1. Edit the selected PRD's file with the revised content (join the `Path:` directory from `anvil status` with the portable relative name from `anvil prd source-name [--prd <id>]`; never derive it from parse output). Before re-parsing, confirm the user intends to revise the existing `Requirement`/`Feature`/`Task` rows **in this PRD's partition**. Mirror the Step 1 confirm pattern: show the user a one-line summary (heading + line count) of the current PRD, and prompt `proceed / cancel / save-as-backup` before running `prd parse`. On `save-as-backup`, copy the file to `<name>.md.bak` in the same directory first.
2. Run `anvil prd parse` again (add `--prd <prd_id>` for a named PRD). A re-parse emits `prd.revised`, which amends the requirements in that PRD's partition — added rows are inserted, removed rows are superseded (lineage retained), unchanged rows are carried forward. It is an amend recorded in the event log, not a merge and not a destructive overwrite.
3. Re-run `anvil prd review` if the changes are material (added/removed requirements, changed acceptance criteria, altered feature scope).
4. Re-run `anvil prd review --approve` for significant scope changes. Minor editorial corrections (typo fixes, clarified wording, unchanged structure) do not require re-approval.

**Coordinate before re-parsing a live project.** Which command does which prune is load-bearing — read both lines below carefully:

- **`prd parse`** is non-destructive on a re-parse — which is exactly the case this section covers. Only the FIRST parse of a `prd_id` is a destructive create (`DELETE … WHERE prd_id=?`). A re-parse of an existing PRD emits `prd.revised`, which **supersedes** the changed requirements (stamps `revision_superseded` on the old row, INSERTs the new) and **never deletes** a requirement row — the requirements table is the append-only lineage of the PRD. A requirement removed from the file drops out of the live set but its lineage survives replay; it does not vanish. Other PRDs' requirements are untouched. There is no "force" required and no safety check — Requirements have no claim or evidence to protect.
- **`plan`** prunes orphan Features and Tasks (v1.15.0). Any feature or task that existed in state.db but is no longer present in the new parse gets a `feature.deleted` / `task.deleted` event emitted by `plan`. NOT by `prd parse` — running `prd parse` alone leaves orphans until `plan` runs.

Safety is built into the deletion path:

- Tasks in `proposed`, `drafted`, or `ready` status delete cleanly — these statuses carry no claim or evidence history worth preserving.
- Tasks in `claimed`, `in_progress`, `needs_review`, or beyond cause `plan` to **fail loudly** (exit 1) with a clear list of which tasks block the prune and how to resolve. The agent must NOT silently bypass this; surface the list to the user and ask whether to release the affected claims, complete the work, or re-run `plan --prune-force` (which deletes the task row but leaves audit history in `events.jsonl`, `claims`, and `evidence`).
- Tasks that have any `claims` or `evidence` rows can NEVER be deleted at the SQL layer (schema FK RESTRICT — `--prune-force` does not override). The audit history outlives the task; if you really want the orphan gone, accept that the row remains and the data is reachable via `events.jsonl`.

Before re-parsing while active claims exist:

1. Run `anvil status` to confirm no active claims.
2. If claims exist, coordinate with the agents holding them. Release the claims first, or wait for them to complete.
3. Tasks whose IDs survive the re-parse (same `T00N` ID in the file) have their claim and evidence history preserved via the event log. Tasks removed from `prd.md` are pruned per the safety rules above.

Avoid editing a task's acceptance criteria or scope while that task is `claimed` or `in_progress`. The agent working the task has already been given a work packet derived from the old spec. Release the claim first, update the PRD, re-parse, then let the agent re-claim.

---

## Common Pitfalls

- **Parsing a thinking-out-loud draft.** `prd.md` is not a scratchpad. Parse only when the document is intended as a real spec. Parsing a half-formed draft seeds `state.db` with garbage requirements that downstream planning will dutifully score and promote.
- **Approving without re-reading.** Read the full PRD before invoking `--approve`. Resolve its path by joining the `Path:` directory from `anvil status` with the portable relative name from `anvil prd source-name [--prd <id>]`; the stable `PRD source:` identity printed by parse is not a filesystem path. An approval event is permanent in `events.jsonl`. It cannot be undone without replaying from a snapshot.
- **Skipping `## Non-Goals`.** The planner agent uses non-goals to bound task generation. Without them, tasks may sprawl into adjacent features. Even one item is better than none.
- **Tasks without verification commands.** The `review tasks` gate (in the plan skill) requires at least one item under `**Verification:**`. Add shell commands — `pytest tests/test_foo.py`, `python -m mymodule --help` — so the gate does not block the entire queue.
- **Re-parsing with active claims and no coordination.** This silently replaces task rows. Agents holding those tasks will find their task ID in an unexpected state on next heartbeat. Always check `anvil status` before re-parsing.

---

## Composition with Other Skills

| Position | Skill |
|---|---|
| Before this skill | Usually none — prd is the entry point for new projects |
| After Step 2 (parse success) | `/anvil:state-ops` to verify counts and structure |
| After Step 4 (approved) | `/anvil:plan` to generate features, tasks, and scores |
| If `anvil status` shows `PRD: draft` | Return here to complete review and approval |

---

## Related entry points

This skill assumes a draft `prd.md` already exists (or that you author one inline). If the user has only a rough idea and no PRD yet, bootstrap one first with `/anvil:start-prd`, which interviews the user and writes a `prd.md` that `anvil prd parse` can consume. To drive unresolved `[NEEDS DECISION]` markers and open questions surfaced after parse, use `/anvil:resolve-decisions`.
