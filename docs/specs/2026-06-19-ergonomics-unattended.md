# Ergonomics & unattended autonomy (placement, unattended mode, score-driven routing)

**Date:** 2026-06-19
**Status:** Draft PRD — authored from a dogfooding session + 4 research streams
**Plugin:** `anvil`
**Tracks:** ergonomics; depends on [`2026-06-19-scoring-agent.md`](2026-06-19-scoring-agent.md) (routing consumes the six axes)
**Breaking:** NO (additive: new commands, a new mode flag, a routing layer)

---

## 1. Goal

Two things hurt during a long autonomous session this run:

1. **Discoverability/placement.** anvil has 8 skills and **no index, no `/help`, no
   menu** — a new user must guess that `start-prd` is the front door. Peers ship
   `/flow`, `/crew`, `/help` for exactly this.
2. **Friction under autonomy.** The skills pause for confirmation at every gate —
   right for a human at the keyboard, wrong when the user has gone to bed and
   pre-authorized an overnight run. Each pause + per-step narration is dead weight.

This PRD covers three connected pieces: **placement** improvements (§3), an
**unattended mode** (§4), and **score-driven routing** (§5) — the layer that decides,
*per task*, which gates unattended mode may auto-pass.

## 2. What anvil already does better than peers (do NOT regress)

- A **SessionStart state hook** (`hooks/detect-state.sh`) peers lack — language +
  live `anvil status` with gracefully degraded fallbacks.
- An explicit **"no CLI to-do list" discipline** — skills drive commands inline and
  hand off to the next skill by name.
- **One well-placed gate**: only `apply --approve` demands explicit confirmation;
  everything else runs inline. Better than blanket confirm-everything.
- A **CLI⇄MCP equivalence** surface (`AGENTS.md`) most peers don't offer.

## 3. Placement & discoverability (additive)

Grounded in the peer survey (precedent in parentheses):

1. **`commands/anvil.md` — a no-arg index/status menu** *(fakoli-flow `/flow`,
   fakoli-crew `/crew`)*. Lists the 8 skills + the `start-prd → prd → plan → claim →
   execute → finish` pipeline, then shells `anvil status` so it doubles as
   on-demand state.
2. **Enrich the SessionStart banner to name the next action** *(fakoli-flow)*.
   anvil already prints state; append routing, e.g.
   `prd-status:approved → next: /anvil:claim`, or empty → `→ run /anvil:start-prd`.
3. **`commands/anvil-help.md` quick-reference card** *(ponytail/ralph-loop/hookify
   `/help`)* — lifecycle, each skill's one-liner, CLI⇄MCP equivalence.
4. **Unambiguous front door** — `start-prd`'s description says "START HERE if no
   `.anvil/`"; the index lists it first.
5. **`/anvil:status` thin alias** to the hook-format output *(hookify `/list`)*,
   distinct from the heavier read-only `state-ops` skill.

## 4. Unattended mode

A mode for **pre-authorized** autonomous runs. Activated explicitly (e.g.
`--unattended` / a skill arg / config), never inferred. It removes ergonomic
pauses while **safety gates remain, conditioned on the task's score (§5)**.

### 4.1 What it toggles (all four, from the Q&A)

- **Auto-approve pre-authorized *ergonomic* gates** — parse-count confirms,
  `prd review` (structural, non-permanent), `review tasks` promotion, the
  auto-expand summary checkpoint, `find-decisions` soft gate, submit/handoff
  prompts. (The *permanent* gates are conditional — §4.2.)
- **Batch + auto-PR + poll CI** — one PR per item, opened automatically, CI/Greptile
  polled, findings surfaced and addressed. (The loop run by hand this session.)
- **Terser output + single end-of-run report** — no per-step narration; one
  structured report (shipped / blocked / decisions / follow-ups).
- **Auto-commit + longer leases** — commit per task with trailers; lease
  auto-renew so a long task doesn't expire mid-run.

### 4.2 Safety gates that STAY (but become score-conditional)

The research said `prd review --approve` and `apply --approve` must stay because
they are permanent/irreversible. The synthesis with score-driven routing: these
gates **auto-pass only when the task's Risk & Reversibility axis is low**; a
`risk_reversibility ≥ 4` task **halts for a human even in unattended mode**.

- `prd review --approve` — pre-authorized **once at run start**, never per-PRD.
- `apply --approve` — auto-passes when `risk_reversibility < 4` **and** the evidence
  gate passed; otherwise queued for human.
- The evidence gate, single-winner lease, and FK audit protections are **never**
  relaxed.
- `plan --prune-force`, `--reject`/`--discard` with branch deletion — never run
  blind; auto-skip→reopen instead.

### 4.3 Merge policy

**Default (decided): auto-merge low-risk tasks after review.** A task auto-merges
when **all** hold: `risk_reversibility < 4`, CI is green, and the Greptile (or a
Copilot) review has passed / been addressed. A `risk_reversibility ≥ 4` task stops
at **PR-open** for human merge.

> This is a deliberate, *bounded* override of anvil's "never auto-merge" principle:
> the human gate is not removed, it is **relocated** — to the morning PR queue for
> high-risk work, and to the automated-review + low-risk-score conjunction for the
> rest. The two independent signals (automated review AND a low risk axis) are what
> make auto-merge defensible; neither alone would be. A `--no-auto-merge` run-level
> override forces PR-open for everything when the operator wants a full morning
> review.

### 4.4 Safety rails (the kill-switch)

A run must be bounded. Enforce all three selected bounds; on any hit, **stop and
write the report**:

- **Token/cost budget** (`--budget`) — directly prevents the documented $437
  overnight runaway.
- **Wall-clock cap** (`--max-hours`) — stop at the time limit regardless of progress.
- **Stop-on-repeat-fail escape** — halt a task lineage after K consecutive
  review/test failures (the ralph-loop infinite-retry failure mode), queue for human.

Plus standing rails: **named-files staging only** (never `git add -A`), isolated
work per item, and **stop-and-ask escape conditions** (ambiguous acceptance
criteria, `risk_reversibility ≥ 4`, breaking/SPEC-FIRST change, repeated reviewer
FAIL) that halt-and-queue rather than guess.

## 5. Score-driven routing (the autonomy decision layer)

Routing turns the six axes (companion PRD) into a **per-task action**. It is what
makes unattended mode safe — not a blanket "auto-approve everything," but a policy
that reads the score and chooses how to proceed. From the rubric:

| Condition | Routing action |
|---|---|
| `risk_reversibility ≥ 4` | Do **not** act autonomously. Surface a plan, isolate the irreversible step, request explicit human confirmation. |
| `uncertainty ≥ 4` | Ask clarifying questions / propose acceptance criteria **before** writing code. Do not guess intent. |
| `verifiability ≤ 2` | Establish a test or check **first**; if impossible, narrow scope until something is verifiable. |
| several axes `≥ 4` | Decompose into independently shippable subtasks; tackle the **highest-uncertainty** one first to retire risk early. |
| all axes `≤ 2` | Apply the known pattern directly, minimal ceremony. |

Routing is consulted by `plan` (decompose), `claim`/`execute` (act vs ask vs
test-first), and `finish` (auto-apply/merge vs halt). In **interactive** mode it
*advises* (surfaces the recommended action); in **unattended** mode it *governs*
(auto-proceeds on the low-risk branches, halts on the high-risk ones).

## 6. Phasing

| Phase | Ships | Depends on |
|---|---|---|
| P1 | Placement: `/anvil` index, enriched banner, `/anvil-help`, `/anvil:status` | — |
| P2 | Score-driven routing as an **advisory** layer (interactive) | scoring PRD (6 axes) |
| P3 | Unattended mode: ergonomic auto-pass + report + auto-commit/PR + kill-switch | P2 |
| P4 | Score-conditional safety gates + merge policy (auto-merge low-risk) | P3 + auto-review |

## 7. Open questions

- Is "pre-authorization" a single run-start confirmation, a config flag, or a
  scoped allowlist (which tasks/files the run may touch)?
- Does routing live in the engine (a `route(task) → action` function) or in the
  skills (each skill consults the score)? Engine is more reusable; skills are
  where the autonomy actually happens.
- How does unattended mode surface a mid-run halt to an absent human — a written
  queue entry, a notification, both?

## 8. References

- Peer precedent: `~/.claude/plugins/.../fakoli-flow/commands/flow.md` +
  `hooks/detect-context.sh`; ralph-loop/hookify/ponytail `/help`+`/list`.
- anvil skills: `skills/{prd,plan,claim,execute,finish,state-ops}/SKILL.md`;
  `hooks/{hooks.json,detect-state.sh}`; `.claude-plugin/plugin.json` (no commands).
- Autonomy patterns: OpenAI Codex automations (async auto-PR into a morning review
  queue); resolve-loop "implement-then-hold breaking changes"; ralph-loop
  `--max-iterations`; overnight-runaway cost rails ($437 case, budget caps).
- The six axes: companion scoring PRD §4. The routing rubric: **this document §5**.
