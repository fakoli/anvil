---
name: resolve-decisions
description: Walk the PRD's unresolved items — `[NEEDS DECISION]` markers, `## Open Questions`, and missing acceptance-criteria or verification fields — and drive each one as a Q&A turn with the user, proposing concrete options when possible and applying the chosen answer to `.fakoli-state/prd.md`. Use this skill when `fakoli-state prd find-decisions` reports unresolved items, or when other skills (prd, plan) detect decisions blocking progress.
---

# Resolve Decisions — Walk Open Items as Q&A

Turn every `[NEEDS DECISION]` marker, unresolved `## Open Question`, or missing acceptance-criterion into a one-question conversational turn — propose options when the surrounding context lets you, accept the user's pick, and apply the answer to `prd.md`. The agent does the framing and the typing; the user does the deciding.

The anti-pattern this skill exists to prevent: handing the user a list of "open questions to resolve in your editor first" and then waiting. An LLM's strength over a CLI is turning *blocked on a decision* into *let me ask you the right question*. Pasting a to-do list of unresolved decisions is the same failure mode as pasting a to-do list of CLI commands.

---

## When to Use

- After `fakoli-state prd parse` succeeds and the agent notices unresolved items (`prd` skill Step 3 routes here when `find_decisions` returns non-empty).
- Before `fakoli-state plan` runs, when `find_decisions` reports `[NEEDS DECISION]` markers or unresolved Open Questions that would shape task generation (`plan` skill Step 0 routes here).
- When the user explicitly asks to "resolve open questions", "answer the NEEDS DECISION items", or "fill in the missing acceptance criteria".
- When `fakoli-state review tasks` blocks tasks for missing acceptance criteria or verification commands — those become `missing_field` decisions this skill can drive Q&A on, instead of asking the user to re-edit the PRD by hand.

**Do not use this skill** to author requirements from scratch — use `start-prd`. **Do not use this skill** to score or expand tasks — that is the `plan` skill's job. **Do not use this skill** to make `apply --approve` decisions — that is the `finish` skill's gate.

---

## Prerequisites

`.fakoli-state/prd.md` must exist and parse cleanly. Confirm:

```bash
fakoli-state prd parse 2>&1 | tail -3
```

The detector is at `bin/src/fakoli_state/planning/decisions.py`; the CLI surface is `fakoli-state prd find-decisions`; the MCP equivalent is the `find_decisions` tool.

| Command | Phase | Status |
|---|---|---|
| `fakoli-state prd find-decisions` | Phase 7+ | available (v1.14.0) |
| `find_decisions` MCP tool | Phase 7+ | available (v1.14.0) |

---

## Workflow

### Step 1 — Scan for unresolved items

Drive the scan yourself; do not tell the user to run the CLI. Invoke `fakoli-state prd find-decisions` (or the `find_decisions` MCP tool when the runtime exposes it) and parse the result. Surface a one-paragraph summary inline:

> I found **N** unresolved items in the PRD:
> - **X** `[NEEDS DECISION]` markers (inline, often tied to specific requirements or features)
> - **Y** `## Open Questions` items (top-level uncertainties about scope or approach)
> - **Z** missing fields on tasks (acceptance criteria or verification commands the review gate requires)
>
> Want me to walk them one at a time? (yes / not yet / show me the list first)

On `show me the list first`, present the full list compactly (one line per decision: id, kind, location, first 60 chars of text). Then re-ask "ready to walk them?"

On `not yet`, stop. Confirm the items are visible in `fakoli-state prd find-decisions` for later.

On `yes`, proceed to Step 2.

### Step 2 — Drive each decision as one Q&A turn

Iterate the decision list in the order the detector returned (it is deliberately stable: `needs_decision` first, then `open_question`, then `missing_field`). For each item, present the question conversationally and **propose concrete options when the surrounding context allows you to**. This is the LLM-leverage moment — turn an unresolved item into a multiple-choice question whenever possible.

**For a `needs_decision` marker:**

> **ND-001 — Summary section**
> The PRD says: *"The system must validate inputs [NEEDS DECISION: which encoding?]."*
>
> Based on the surrounding paragraph (about validating incoming HTTP requests), three reasonable answers:
> 1. **UTF-8 only** — strict, fails on anything else. Simplest. Best if all known clients send UTF-8.
> 2. **UTF-8 with Latin-1 fallback** — pragmatic for legacy clients. Slightly more code.
> 3. **Detect with `chardet` and accept any standard encoding** — most permissive; adds a dependency.
> 4. **Other (describe)**
>
> Pick (1 / 2 / 3 / or describe your own).

On the answer, rewrite the marker inline in `.fakoli-state/prd.md`. For option 1: replace `[NEEDS DECISION: which encoding?]` with `(decision: UTF-8 only)` (or just inline the answer prose if it reads better). Preserve the rest of the sentence verbatim. Save the file, then move on.

**For an `open_question` item:**

> **OQ001 — Open Questions item 1**
> *"Which serialization format should we use for the on-disk packet cache?"*
>
> Three reasonable answers based on the rest of the PRD:
> 1. **JSON** — human-readable, no extra dependency, fine for our packet sizes.
> 2. **MessagePack** — ~3× smaller on disk, requires `msgpack` dep.
> 3. **Protocol Buffers** — schema enforcement + cross-language; overkill for this use case.
> 4. **Defer to v2** — note as a non-goal for now.
>
> Pick (1 / 2 / 3 / 4 / or describe).

On the answer, **move the resolved item from `## Open Questions` to a new `## Decisions` section** (create it if it does not exist, just above `## Risks` or at the end of the file). The `## Decisions` entry takes the form:

```markdown
- **OQ001 (resolved 2026-05-26):** Which serialization format for the packet cache?
  → **Decision:** MessagePack. Rationale: ~3× smaller on disk; user accepted the `msgpack` dependency over JSON's bigger files.
```

This preserves the audit trail — future re-reads can see *what was unclear at draft time* and *what was decided*. Delete the original bullet from `## Open Questions`. If the resolution materially affects a requirement or feature, also add or update the relevant `R00N:` or `F00N:` block — surface this to the user inline: *"This decision also implies R007 should change to read X instead of Y; want me to update R007 too?"*

**For a `missing_field` item:**

> **MF-T012-AC — T012 acceptance criteria**
> Task T012 (*"Implement retry-with-backoff for transient HTTP failures"*) has no acceptance criteria. The review gate requires at least one.
>
> Based on the task description, four candidate criteria:
> 1. *On 429 / 503, the client retries up to 3 times with exponential backoff (1s / 2s / 4s).*
> 2. *On 500-class errors, the client logs the failure and re-raises after retries are exhausted.*
> 3. *On 4xx (non-429), the client does NOT retry and surfaces the error.*
> 4. *None of these — let me describe my own.*
>
> Add (1) only, (1+2), (1+2+3), or (4) describe?

On the answer, edit the relevant `### T012:` block in `.fakoli-state/prd.md` to add an `**Acceptance criteria:**` field with the chosen bullets. Same pattern for missing `**Verification:**` commands — propose 2-3 candidate `pytest` / shell invocations based on the likely files, accept the pick, write them in.

**On any decision the LLM cannot propose options for** (the context is too thin, the question is too open), do not invent options. Ask the open-ended question and accept whatever answer the user gives:

> **OQ003 — Open Questions item 3**
> *"What is the upper bound on payload size?"*
>
> I do not have enough context to propose options here — what bound do you want?

### Step 3 — Re-parse after the batch is resolved

Once every decision is answered (or the user explicitly skips the remaining ones), drive a re-parse yourself so the canonical state catches up:

> All N decisions have been applied to `.fakoli-state/prd.md`. Re-parsing to refresh state.db — ready? (yes / wait, I want to re-read the file first)

On `yes`, invoke `fakoli-state prd parse`. Surface the new counts. If the re-parse surfaces fresh errors (e.g. you accidentally broke the markdown structure during inline rewrites), drive a fix immediately — do not hand the user a "go fix it in the editor" message. Read the parse error, identify which edit caused it, propose the corrected text, ask the user to confirm, apply.

After re-parse, optionally re-run `fakoli-state prd find-decisions` to confirm the unresolved count is 0 (or to surface anything the resolution exposed — e.g., a `needs_decision` rewrite that introduced a new field with empty acceptance criteria).

### Step 4 — Hand off to the next skill

Once the PRD is fully resolved:

> All decisions resolved. The PRD is ready for the next step. What's next?
> 1. **Continue into `/fakoli-state:plan`** — generate features and tasks now that the PRD is unambiguous.
> 2. **Review the PRD one more time first** — I'll open it inline so you can scan the `## Decisions` section.
> 3. **Stop here** — we're done with the resolver; you'll drive plan later.

On `1`, invoke the `plan` skill directly (do not paste `fakoli-state plan` as a command for the user to type).
On `2`, show the relevant sections inline; when the user says "looks good," re-ask the next-step question.
On `3`, confirm and stop.

---

## Anti-pattern to avoid

Ending the skill with a message like *"OQ001 (success criterion) and OQ006 (time budget) should be resolved before planning. Open `.fakoli-state/prd.md` in your editor to fix them, then re-run `fakoli-state prd parse` and `fakoli-state plan`."* That handoff treats an unresolved decision like a known bug instead of a question the agent could have asked. The whole point of an LLM agent inside the conversation is that it can frame the right question with concrete options — pasting the list of unresolved items as a to-do is forfeiting that strength.

The rule: **for every unresolved item, the agent generates the question and proposes 2-4 candidate answers (when context allows). The user picks. The agent writes the answer to the file.** No "open the editor" handoffs unless the user explicitly opts out.

**When to actually hand off to the editor:** if the user says "let me think about these — I'll edit them directly later," or if a decision is genuinely too cross-cutting to express as a single Q&A turn (e.g., "redesign the whole authentication architecture"). In those cases, list the unresolved items compactly, point the user at `fakoli-state prd find-decisions` so they can re-surface the list later, and stop.

---

## Composition with Other Skills

| Position | Skill |
|---|---|
| Before this skill | `prd` Step 3 (after `parse_prd` succeeds) — soft-gates into resolve-decisions when find_decisions returns non-empty |
| Before this skill | `plan` Step 0 — soft-gates into resolve-decisions when `[NEEDS DECISION]` or unresolved Open Questions would shape task generation |
| After Step 4 (resolved) | `plan` — continue into task generation now that the PRD is unambiguous |
| If the user opts out | Return to whatever skill bridged here; proceed without resolving (the soft gate is by design) |

---

## Phase 7+ Notes

| Feature | Phase | Status |
|---|---|---|
| `fakoli-state prd find-decisions` CLI | v1.14.0 | available |
| `find_decisions` MCP tool | v1.14.0 | available |
| `[NEEDS DECISION]` marker detection | v1.14.0 | available |
| `## Open Questions` detection | v1.14.0 | available |
| Missing-field detection (empty acceptance_criteria, empty verification) | v1.14.0 | available |
| `## Decisions` audit-trail section auto-creation | v1.14.0 | available (driven by the skill — not a CLI command) |
| LLM-assisted option generation when proposing answers | v1.14.0 | inherent — the agent running this skill IS the LLM |
| Per-requirement / per-feature detection (e.g. empty requirement text) | v1.15+ | pending — detection module has reserved parameters |
