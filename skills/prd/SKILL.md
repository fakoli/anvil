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
- When `anvil status` reports `prd-status: draft` or `prd-status: none` and the project can't proceed.
- When a co-authored PRD is ready for a formal review and approval step.

**Do not use this skill to generate or score tasks.** Once the PRD is approved, proceed to `/anvil:plan` for the task graph. This skill only authors, parses, and reviews requirements.

---

## Prerequisites

`.anvil/` must exist. Confirm before proceeding:

```bash
ls .anvil/state.db 2>/dev/null || echo "MISSING: run anvil init first"
```

If `state.db` is absent, run:

```bash
anvil init --name "<project-name>"
```

Phase 3 commands used in this skill:

| Command | Phase | Status |
|---|---|---|
| `anvil prd parse` | Phase 3 | available |
| `anvil prd review` | Phase 3 | available |
| `anvil prd review --approve` | Phase 3 | available |

The structured template at `docs/prd-template.md` (relative to the plugin root) is the canonical contract. The parser enforces it — any deviation from the required sections produces a `ParseError`.

---

## Workflow

### Step 1 — Author or update `.anvil/prd.md`

Drive this step inline. Check the filesystem for an existing PRD before suggesting any edit — the subsequent `anvil prd parse` step (Step 2) is destructive and replaces every `Requirement`, `Feature`, and `Task` row in `state.db`.

Run the existence check yourself (Bash, MCP filesystem tool, or whichever read primitive the runtime exposes):

```bash
ls .anvil/prd.md 2>/dev/null
```

**If the file exists**, do not edit or re-parse without confirmation. Read the file, surface a one-line summary (first heading and total line count are usually enough), and ask:

> `.anvil/prd.md` already exists (`<first-heading>`, `<N>` lines). Open it for editing, save the current copy as a backup first, or leave it alone? (edit / save-as-backup / cancel)

- On `edit` — read the file in full, propose changes inline (show diffs in chat), and apply them once the user confirms. Do not shell out to `$EDITOR` and wait — drive the edits in the conversation.
- On `cancel` — stop. Confirm the PRD is untouched; offer to run `/anvil:state-ops` to inspect current PRD status.
- On `save-as-backup` — copy the existing file to `.anvil/prd.md.bak`, then proceed with inline edits as above.

**If the file does not exist**, author it inline. Compose the draft in the conversation (using the structure below), present it to the user for approval, then write it directly to `.anvil/prd.md`. Do not tell the user to open `$EDITOR` themselves.

The canonical structure is defined in `docs/prd-template.md`. Required sections — the parser fails without them:

- `# Project: <Name>` — H1 title, first line of the file
- `## Summary` — one prose paragraph
- `## Goals` — bulleted list, at least one item
- `## Requirements` — bulleted list of `R001: ...` items

Optional sections that should be present in any non-trivial PRD:

- `## Non-Goals` — even if the answer is "none stated", declare it
- `## Acceptance Criteria` — project-level verifiability, not per-task
- `## Features` — logical groupings of related tasks
- `## Tasks` — hand-authored tasks with `**Acceptance criteria:**` and `**Verification:**` fields
- `## Risks`, `## Open Questions` — informs the planner's scoring

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
anvil prd parse
```

This reads `.anvil/prd.md`, validates structure, and writes `Requirement`, `Feature`, and `Task` entities to `state.db`. PRD status becomes `draft`. Surface the parser output inline in the same message so the user sees the result without a context switch.

**On parse error:** the parser surfaces each `ParseError` with the section name and, where possible, the line number. Existing `state.db` content is preserved — no silent rollback of previous good state. Read the error, propose the fix to `prd.md` inline, apply it after confirmation, and re-run `prd parse` yourself.

Common parse errors:

- `missing required section: Summary` — the `## Summary` heading is absent or has no body
- `missing required section: Goals (must have at least one item)` — the list is empty
- `ParseError: duplicate ID R003` — the same requirement ID appears twice; renumber
- A task block has a `**Feature:** F002` reference but no `### F002:` heading exists in `## Features`

**On success:** the command prints a summary. Present the counts to the user and confirm they match expectations:

```
parsed 6 requirements, 3 features, 8 tasks
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
anvil prd review
```

Surface the output inline. If the review gate passes, PRD status becomes `reviewed`; tell the user and move to Step 4.

---

### Step 4 — Approve when ready

`prd review --approve` is a hard gate. It transitions the PRD from `reviewed` to `approved` and the `anvil claim` gate enforces it — no task can be claimed while the PRD is in `draft` or `reviewed` status. Because approval is permanent in `events.jsonl`, the user MUST explicitly confirm before the agent runs it.

Before asking, read the full PRD back to the user (or show a concise structural summary — sections present, requirement/feature/task counts, any items the review surfaced). Then ask:

> The PRD is reviewed. Approving it is permanent and opens the claim gate. Ready to approve? (yes / no / let me re-read first)

- **On `yes`** — invoke `anvil prd review --approve` yourself, surface the output, then run `anvil status` and confirm `prd-status: approved`. Tell the user the project is ready for `/anvil:plan` and ask whether to drive that skill next.
- **On `no`** — stop. The PRD stays in `reviewed`; the user can come back to it later.
- **On `let me re-read first`** — wait. When the user signals ready, return to the confirm prompt above.

**Keep approval a deliberate, separate step.** In a team context, the reviewer and approver should differ: the agent reviews for structural completeness; the human approves the scope. In a solo context, the read-back-then-confirm pattern above is the substitute for a second pair of eyes.

---

## Anti-pattern to avoid

Ending this skill with a numbered list like "1. Edit `prd.md` 2. Run `prd parse` 3. Run `prd review` 4. Run `prd review --approve` 5. Run `plan`..." That handoff style only makes sense when the work is leaving this session entirely — queued for another agent, scheduled for tomorrow, blocked on stakeholder review. When the agent and user are in the same conversation, drive each command, surface its output, and present the next decision. Do not delegate the typing.

**When to actually hand off CLI commands:** if the user explicitly opts out ("just give me the commands"), or if the runtime lacks the tool needed to execute them (e.g., MCP-only client with no shell and no `parse_prd` tool). In those cases, a CLI list is the right output. Otherwise, drive.

---

## Iterating

The PRD will change. Here is the safe sequence for updates:

1. Edit `.anvil/prd.md` with the revised content. Before any destructive re-parse, confirm the user intends to overwrite the existing `Requirement`/`Feature`/`Task` rows. Mirror the Step 1 overwrite-gate pattern: show the user a one-line summary (heading + line count) of the current `prd.md`, and prompt `proceed / cancel / save-as-backup` before running `prd parse`. On `save-as-backup`, copy `.anvil/prd.md` to `.anvil/prd.md.bak` first.
2. Run `anvil prd parse` again. Re-parse replaces all `Requirement`, `Feature`, and `Task` entities — it is not a merge.
3. Re-run `anvil prd review` if the changes are material (added/removed requirements, changed acceptance criteria, altered feature scope).
4. Re-run `anvil prd review --approve` for significant scope changes. Minor editorial corrections (typo fixes, clarified wording, unchanged structure) do not require re-approval.

**Coordinate before re-parsing a live project.** Which command does which prune is load-bearing — read both lines below carefully:

- **`prd parse`** destructively replaces the `requirements` table. Requirements removed from prd.md vanish from state.db on the next `prd parse`. There is no "force" required and no safety check — Requirements have no claim or evidence to protect.
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
- **Approving without re-reading.** Run `cat .anvil/prd.md` before invoking `--approve`. An approval event is permanent in `events.jsonl`. It cannot be undone without replaying from a snapshot.
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
| If `anvil status` shows `prd-status: draft` | Return here to complete review and approval |

---

## Phase 3 Limitations

LLM-assisted PRD drafting (start-prd-to-prd pipeline) is deferred to Phase 7. The `start-prd` skill will bridge to `fakoli-flow:brainstorm` at that point, using an LLM to co-author from rough ideas.

Until Phase 7 ships, the agent co-authors with the user directly using the deterministic template. The one-question-at-a-time pattern described in Step 1 is the Phase 3 substitute for automated PRD-drafting assistance.

| Feature | Phase |
|---|---|
| `anvil prd parse` | Phase 3 — available |
| `anvil prd review` | Phase 3 — available |
| `anvil prd review --approve` | Phase 3 — available |
| LLM-assisted drafting via `--use-llm` | Phase 7 — pending |
| `anvil start-prd` CLI command | Phase 7 — pending |
