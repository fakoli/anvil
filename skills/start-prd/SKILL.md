---
name: start-prd
description: Bootstrap a `.anvil/prd.md` draft from a rough project idea — interview the user question-by-question and write the result so `anvil prd parse` can consume it. Use this skill when the user has a project intent but does not yet have a PRD (e.g., asks to "start a PRD", "draft requirements", "author a PRD", or "spec out a project").
---

# Start a PRD — Rough Idea to PRD Draft

Produce a parseable PRD from an unstructured prompt by interviewing the user one question at a time. This skill writes `.anvil/prd.md` — it does not parse, review, or approve. Those steps belong to the `prd` skill.

---

## When to Use

- The user has an idea ("I want to build a CLI that converts CSV to Parquet") but no PRD yet.
- `anvil status` reports `prd-status: none` and the user is not ready to write the template by hand.
- A rough scope was discussed in chat and now needs to be captured as a structured document.
- The user explicitly asks to "start a PRD", "draft requirements", "author a PRD", or "spec out" a project before planning.

**Do not use this skill** to parse, review, or approve a PRD that already exists — use the `prd` skill. **Do not use this skill** to score, plan, or expand tasks — use the `plan` skill. **Do not use this skill** when the user already has a complete `prd.md` in hand and just wants it loaded into state.

---

## Prerequisites

The project must be initialized. Confirm with `anvil status`, which is layout-aware
(it resolves the default HOME workspace or a local in-repo `.anvil/`); a raw
`ls .anvil/state.db` is wrong under the default workspace layout (state lives in
`~/.anvil/workspaces/…`) and reports "missing" even when initialized:

```bash
anvil status >/dev/null 2>&1 || echo "MISSING: run anvil init first"
```

If it exits non-zero, run:

```bash
anvil init --name "<project-name>"
```

This skill writes a file; it does not require any `anvil` CLI subcommand to be available beyond `init`.

---

## Workflow

### Step 1 — Interview the user

Run the interview directly. Ask one question per message. Wait for the answer before asking the next.

**Question 1 — The rough idea.** Open with:

> What are you building? A one or two sentence pitch is enough — we will refine from there.

Capture the user's answer verbatim. This becomes the seed for `## Summary`.

**Question 2 — Target users.** Ask:

> Who is this for? Internal team, external developers, end users, yourself?

The answer feeds the `## Summary` paragraph and may shape `## Non-Goals` (if the answer narrows the audience).

**Question 3 — Primary success criterion.** Ask:

> If only one thing has to be true for this project to be considered a success, what is it?

This becomes the lead bullet in `## Goals` and often the lead bullet in `## Acceptance Criteria`.

**Question 4 — Key non-goals.** Ask:

> What is explicitly out of scope for this version? Even "none declared" is a valid answer — but stating non-goals up front prevents the planner from sprawling.

This populates `## Non-Goals`. Push back gently if the user says "nothing" — most non-trivial projects have at least one obvious exclusion (e.g., "no auth in v1", "single-user only").

**Question 5 — Must-have features.** Ask:

> What are the two or three things this absolutely must do? Bullet form is fine.

Each item becomes a candidate `## Features` entry (or a `## Requirements` bullet if it is small enough to express as a single requirement).

**Question 6 — Risks and unknowns.** Ask:

> Are there any known risks, unknowns, or decisions you have not made yet?

The answer populates `## Risks` and `## Open Questions`. If the user says "none", record an empty section rather than skipping it — the visibility of "no risks identified" is itself useful information.

**Stop at six questions unless something material remains unclear.** Asking more questions than necessary fatigues the user and rarely improves the draft. If the answers are sparse, ask a single follow-up before moving to Step 2 — do not chain three more questions to "fix" thin input.

### Step 2 — Generate the PRD draft and show it to the user

Compose a draft that matches the structure in `docs/prd-template.md` (relative to the plugin root). The minimum draft uses the four required sections plus `## Non-Goals` and `## Acceptance Criteria`:

```markdown
# Project: <Name extracted from Question 1>

## Summary

<One paragraph synthesized from Questions 1 and 2.>

## Goals

- <Primary success criterion from Question 3.>
- <Each must-have feature framed as a goal statement.>

## Non-Goals

- <Each non-goal from Question 4, one per bullet.>

## Requirements

- R001: <First atomic "the system must..." statement, derived from must-have features.>
- R002: <Second atomic requirement.>
- R003: <...>

## Acceptance Criteria

- <Verifiable statement matching the primary success criterion.>
- <Additional verifiable statements covering each must-have feature.>

## Risks

- <From Question 6, or "none identified" as a single bullet.>

## Open Questions

- <From Question 6, or "none identified" as a single bullet.>
```

Add a `## Features` section only when the user named distinct groupings. Add a `## Tasks` section only when the user asked for hand-authored tasks; otherwise let `anvil plan` generate them later.

**Show the draft to the user before writing.** Present the full proposed `prd.md` content inline (or as a fenced markdown block) and ask:

> Here is the PRD draft I assembled from your answers. Does this look right? Reply with edits or "looks good" to write it to `.anvil/prd.md`.

Wait for explicit approval. Apply any requested edits in-place and re-present until the user accepts.

### Step 3 — Write `.anvil/prd.md`

Once the user has approved the draft, check whether `.anvil/prd.md` already exists:

```bash
ls .anvil/prd.md 2>/dev/null
```

**If the file exists**, do not overwrite without confirmation. Show the user a one-line summary of the existing file (first heading, line count) and ask:

> `.anvil/prd.md` already exists. Overwrite it with the new draft? (yes / no / save-as-backup)

- On `yes` — write the new draft to `.anvil/prd.md`.
- On `no` — stop. Tell the user the draft was not written; offer to save it to a sibling path (e.g., `.anvil/prd.draft.md`).
- On `save-as-backup` — copy the existing file to `.anvil/prd.md.bak` first, then write the new draft to `.anvil/prd.md`.

**If the file does not exist**, write the draft directly to `.anvil/prd.md`.

### Step 4 — Parse the draft and continue into the `prd` skill

After the file is written, drive the parse inline rather than handing the user a list of CLI commands to run. The user just approved the draft content — asking them to also run `anvil prd parse` themselves adds friction without adding value.

Confirm the parse and run it:

> Draft written to `.anvil/prd.md`. Ready to parse it into `state.db`? (yes / no / let me edit first)

- **On `yes`** — invoke `anvil prd parse` (via Bash, the MCP `parse_prd` tool when available, or whichever tool the runtime exposes). Surface the result inline. The user sees the parse output in the same conversation, not after a context switch:
  > Parsed 6 requirements, 3 features, 8 tasks. Any unexpected counts? The next step is `prd review` — want me to drive that with you now? (yes / not yet)
- **On `no`** — stop. Confirm the file is on disk at `.anvil/prd.md` and tell the user it is theirs to refine.
- **On `let me edit first`** — wait. When the user signals they are ready, return to the confirm step above and run the parse.

When the user says yes to continuing into review, hand off to the `prd` skill **by invoking it directly** — do not paste a CLI to-do list. The `prd` skill is designed to drive the `review` → `approve` flow conversationally, with the same one-question-at-a-time discipline this skill uses.

See `/anvil:plan` for the canonical anti-pattern statement on driving commands inline versus handing off CLI lists. **When to actually hand off:** if the user explicitly opts out, or if the runtime lacks the required tool. Otherwise, drive.

---

## Anti-Patterns

- **Asking 20 questions at once.** A wall of questions produces a wall of one-word answers. Stay strictly at one question per message — even if it feels slow.
- **Writing the PRD without showing the user a draft for review.** The interview answers are raw input; the translation into PRD bullets is interpretive. Always show the draft and wait for explicit approval before writing the file.
- **Overwriting an existing `.anvil/prd.md` without confirmation.** A silent overwrite can destroy a hand-authored PRD that took hours to craft. Always check for an existing file and prompt before clobbering.
- **Auto-running `anvil prd parse` after writing.** The user should read the draft on disk before parsing. Hand off the next-step command; do not invoke it.
- **Skipping `## Non-Goals` because the user said "none".** Record "none identified" as an explicit bullet instead of omitting the section. Visibility matters for the planner and for reviewers.

---

## Composition with Other Skills

| Position | Skill |
|---|---|
| Before this skill | Usually none — start-prd is the entry point when no PRD exists |
| After Step 3 (file written) | `prd` — parse, review, and approve the draft |
| After `prd review --approve` | `plan` — generate features, tasks, and scores |

---

## Phase 7 Notes

| Feature | Phase | Status |
|---|---|---|
| Six-question interview | Phase 7 | available — pure markdown choreography |
| `anvil start-prd` CLI command | Phase 7+ | pending — for now, run this skill via `/anvil:start-prd` |
