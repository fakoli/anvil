---
name: resolve-decisions
description: Walk the PRD's unresolved items — `[NEEDS DECISION]` markers, `## Open Questions`, and missing acceptance-criteria or verification fields — and drive each one as a Q&A turn with the user, proposing concrete options when possible and applying the chosen answer with `anvil prd resolve-decision`. Use this skill when `anvil prd find-decisions` reports unresolved items, or when other skills (prd, plan) detect decisions blocking progress.
---

# Resolve Decisions — Walk Open Items as Q&A

Turn every `[NEEDS DECISION]` marker, unresolved `## Open Question`, or missing acceptance-criterion into a one-question conversational turn — propose options when the surrounding context lets you, accept the user's pick, and apply the answer with `anvil prd resolve-decision`. The agent does the framing and the typing; the user does the deciding. Let the CLI own the file write: it locates the decision by id, rewrites the right span without touching unrelated content, and records the resolution as an event.

The anti-pattern this skill exists to prevent: handing the user a list of "open questions to resolve in your editor first" and then waiting. An LLM's strength over a CLI is turning *blocked on a decision* into *let me ask you the right question*. Pasting a to-do list of unresolved decisions is the same failure mode as pasting a to-do list of CLI commands.

---

## When to Use

- After `anvil prd parse` succeeds and the agent notices unresolved items (`prd` skill Step 3 routes here when `find_decisions` returns non-empty).
- Before `anvil plan` runs, when `find_decisions` reports `[NEEDS DECISION]` markers or unresolved Open Questions that would shape task generation (`plan` skill Step 0 routes here).
- When the user explicitly asks to "resolve open questions", "answer the NEEDS DECISION items", or "fill in the missing acceptance criteria".
- When `anvil review tasks` blocks tasks for missing acceptance criteria or verification commands — those become `missing_field` decisions this skill can drive Q&A on, instead of asking the user to re-edit the PRD by hand.

**Do not use this skill** to author requirements from scratch — use `start-prd`. **Do not use this skill** to score or expand tasks — that is the `plan` skill's job. **Do not use this skill** to make `apply --approve` decisions — that is the `finish` skill's gate.

---

## Prerequisites

The project must be initialized and the PRD must parse cleanly. Confirm init first, then parse:

```bash
anvil status >/dev/null 2>&1 || echo MISSING
anvil prd parse 2>&1 | tail -3
```

If `anvil status` reports MISSING, the project is not initialized — route back to the bridging skill rather than guessing a path. State lives in the HOME workspace by default (`anvil status` prints the resolved state directory on its `Path:` line), so never reach for a literal in-repo `.anvil/...` file.

The detector lives in the `anvil.planning.decisions` module; the CLI surface is `anvil prd find-decisions` (read-only scan) plus `anvil prd resolve-decision` (writes the answer back); the MCP equivalent of the scan is the `find_decisions` tool. Confirm any command with `anvil <cmd> --help`.

---

## Workflow

### Step 1 — Scan for unresolved items

Drive the scan yourself; do not tell the user to run the CLI. Invoke `anvil prd find-decisions` (or the `find_decisions` MCP tool when the runtime exposes it) and parse the result. Surface a one-paragraph summary inline:

> I found **N** unresolved items in the PRD:
> - **X** `[NEEDS DECISION]` markers (inline, often tied to specific requirements or features)
> - **Y** `## Open Questions` items (top-level uncertainties about scope or approach)
> - **Z** missing fields on tasks (acceptance criteria or verification commands the review gate requires)
>
> Want me to walk them one at a time? (yes / not yet / show me the list first)

On `show me the list first`, present the full list compactly (one line per decision: id, kind, location, first 60 chars of text). Then re-ask "ready to walk them?"

On `not yet`, stop. Confirm the items are visible in `anvil prd find-decisions` for later.

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

On the answer, apply it with `anvil prd resolve-decision`, passing the decision id from the scan and the chosen answer. The CLI does the inline rewrite for you (it replaces `[NEEDS DECISION: which encoding?]` with the resolution and leaves the rest of the sentence verbatim), saves the PRD wherever it lives, and records a `prd.decision_resolved` event:

```bash
anvil prd resolve-decision ND-001 --resolution "UTF-8 only"
```

The CLI echoes the rewrite so you can confirm it landed, then move on:

```
Resolved ND-001 (needs_decision) in /…/.anvil/prd.md.
  section:  Requirements (line 13)
  before:   - R001: The system must validate inputs [NEEDS DECISION: which encoding?].
  after:    - R001: The system must validate inputs UTF-8 only.
  recorded: E000010 (prd.decision_resolved)
Run `anvil prd parse` to refresh state.db.
```

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

On the answer, apply it with `anvil prd resolve-decision`. The CLI **moves the resolved item out of `## Open Questions` and into a `## Decisions` section** (creating that section if it does not exist) and records the event, so you do not edit the file by hand:

```bash
anvil prd resolve-decision OQ001 --resolution "MessagePack — ~3x smaller on disk; accepted the msgpack dependency over JSON's bigger files."
```

The CLI writes the `## Decisions` entry as `- **<question>** → **Decision:** <resolution>` and echoes what changed:

```
Resolved OQ001 (open_question) in /…/.anvil/prd.md.
  section:  ## Open Questions → ## Decisions
  before:   - Which serialization format should we use for the on-disk packet cache?
  after:    - **Which serialization format should we use for the on-disk packet cache?** → **Decision:** MessagePack — ~3x smaller…
  recorded: E000011 (prd.decision_resolved)
Run `anvil prd parse` to refresh state.db.
```

This preserves the audit trail — future re-reads can see *what was unclear at draft time* and *what was decided*. If the resolution materially affects a requirement or feature, surface that to the user inline: *"This decision also implies R007 should change to read X instead of Y; want me to update R007 too?"*, then resolve the corresponding `[NEEDS DECISION]` marker (or, if there is none, ask the user to revise that requirement directly).

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

On the answer, apply each chosen bullet with `anvil prd resolve-decision` against the missing-field id from the scan (`MF-T012-AC` for acceptance criteria, `MF-T012-V` for verification). The CLI appends the bullet under the task's `### T012:` block, so call it once per bullet:

```bash
anvil prd resolve-decision MF-T012-AC --resolution "On 429/503, the client retries up to 3 times with exponential backoff (1s/2s/4s)."
```

Same pattern for a missing verification command (`MF-T012-V`) — propose 2-3 candidate `pytest` / shell invocations based on the likely files, accept the pick, and resolve each one. The CLI records a `prd.decision_resolved` event per call and echoes the edit.

**On any decision the LLM cannot propose options for** (the context is too thin, the question is too open), do not invent options. Ask the open-ended question and accept whatever answer the user gives:

> **OQ003 — Open Questions item 3**
> *"What is the upper bound on payload size?"*
>
> I do not have enough context to propose options here — what bound do you want?

### Step 3 — Re-parse after the batch is resolved

Each `anvil prd resolve-decision` call already edited the PRD and recorded an event, but it does not refresh `state.db`; that is what `anvil prd parse` does. Once every decision is answered (or the user explicitly skips the remaining ones), drive a re-parse yourself so the canonical state catches up:

> All N decisions have been applied to the PRD. Re-parsing to refresh state.db — ready? (yes / wait, I want to re-read the PRD first)

On `yes`, invoke `anvil prd parse`. Surface the new counts. If the re-parse surfaces fresh errors, drive a fix immediately — do not hand the user a "go fix it in the editor" message. Read the parse error, identify which span is wrong, and resolve it with `anvil prd resolve-decision` (or, if the break is outside any decision span, propose the corrected text and apply it once the user confirms).

After re-parse, optionally re-run `anvil prd find-decisions` to confirm the unresolved count is 0 (or to surface anything the resolution exposed — e.g., a `needs_decision` rewrite that introduced a new field with empty acceptance criteria).

### Step 4 — Hand off to the next skill

Once the PRD is fully resolved:

> All decisions resolved. The PRD is ready for the next step. What's next?
> 1. **Continue into `/anvil:plan`** — generate features and tasks now that the PRD is unambiguous.
> 2. **Review the PRD one more time first** — I'll open it inline so you can scan the `## Decisions` section.
> 3. **Stop here** — we're done with the resolver; you'll drive plan later.

On `1`, invoke the `plan` skill directly (do not paste `anvil plan` as a command for the user to type).
On `2`, show the relevant sections inline; when the user says "looks good," re-ask the next-step question.
On `3`, confirm and stop.

---

## Anti-pattern to avoid

Ending the skill with a message like *"OQ001 (success criterion) and OQ006 (time budget) should be resolved before planning. Open `.anvil/prd.md` in your editor to fix them, then re-run `anvil prd parse` and `anvil plan`."* That handoff treats an unresolved decision like a known bug instead of a question the agent could have asked. The whole point of an LLM agent inside the conversation is that it can frame the right question with concrete options — pasting the list of unresolved items as a to-do is forfeiting that strength.

The rule: **for every unresolved item, the agent generates the question and proposes 2-4 candidate answers (when context allows). The user picks. The agent applies the answer with `anvil prd resolve-decision`.** No "open the editor" handoffs unless the user explicitly opts out.

**When to actually hand off to the editor:** if the user says "let me think about these — I'll edit them directly later," or if a decision is genuinely too cross-cutting to express as a single Q&A turn (e.g., "redesign the whole authentication architecture"). In those cases, list the unresolved items compactly, point the user at `anvil prd find-decisions` so they can re-surface the list later, and stop.

---

## Decision-presentation discipline (canonical)

Whenever any anvil skill surfaces a multi-option choice to the user, present it as a **structured Q&A turn**, not as prose with bullets.

**Use `AskUserQuestion` when running inside Claude Code.** The labeled options become an explicit pick UI and the answer comes back as a known label, so the agent can act unambiguously. For other runtimes, fall back to explicit numbered prompts ("Pick 1 / 2 / 3").

**Anti-pattern:** ending a turn with paragraph-style alternatives that ask for a decision but don't pin down the answer shape. For example:

> "Two options: Cut T014 (planner's recommendation). Cut T008 + T018 (distributed). My recommendation is the first. What's your call?"

Replace with a structured prompt so the answer is one of N labels and the agent knows exactly what to do next:

> 1. Cut T014 + trim T002 (planner's recommendation; lands at ~80h)
> 2. Cut T008 + T018 + trim T007 (distributed; keeps all features intact)
> 3. Defer T017 (Wasm network policy; affects F005)
> 4. Keep all tasks and accept the overrun
>
> Pick 1 / 2 / 3 / 4 (or describe).

The rule applies across all skills: any time the agent could present 2+ options for the user to pick, use structured Q&A. Prose-with-bullets that lacks an explicit "pick N" prompt forces the user to type free-form intent the agent then has to interpret — wasted turn.

---

## Composition with Other Skills

| Position | Skill |
|---|---|
| Before this skill | `prd` Step 3 (after `parse_prd` succeeds) — soft-gates into resolve-decisions when find_decisions returns non-empty |
| Before this skill | `plan` Step 0 — soft-gates into resolve-decisions when `[NEEDS DECISION]` or unresolved Open Questions would shape task generation |
| After Step 4 (resolved) | `plan` — continue into task generation now that the PRD is unambiguous |
| If the user opts out | Return to whatever skill bridged here; proceed without resolving (the soft gate is by design) |

