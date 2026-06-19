# Agent Workflow Formats — Claude Code Dynamic Workflows vs Codex Automations

> Reference notes on the two harness-native orchestration surfaces that sit
> *above* Anvil: Claude Code **dynamic workflows** (JavaScript orchestrating
> subagents) and OpenAI **Codex automations** (scheduled natural-language
> prompts). Part 1 is the dynamic-workflows anatomy; Part 2 is Codex automations
> plus the broader OpenAI orchestration landscape. The closing section,
> **"Anvil as the Workflow Substrate,"** frames why this matters: these primitives
> are ephemeral and harness-specific, and Anvil is the durable, governed
> state + audit layer underneath them.
>
> Compiled 2026-06-19. Companion to
> [`intent-driven-development-landscape.md`](./intent-driven-development-landscape.md)
> (the strategic map) and the local design draft `anvil-workflow-substrate-design.md`.

---

## Part 1 — Claude Code Dynamic Workflows

### What it is

A **dynamic workflow** is a small **JavaScript program** that orchestrates a
fleet of subagents *deterministically*. The clean split:

- **JS = control plane** — loops, conditionals, fan-out, ordering. Deterministic,
  runs in the harness.
- **Subagents = the work** — each `agent(...)` call spawns a fresh model instance
  that does one chunk and returns a result (raw data, not chat).

It runs in the **background**: the tool returns immediately with a `runId` plus a
persisted `scriptPath`, and a notification fires on completion. The script's
final `return` is the result.

### Anatomy — two parts, always in order

A workflow file is exactly two pieces: a `meta` literal, then the body.

```javascript
export const meta = {            // MUST be a pure literal (no vars/calls/spreads/interpolation)
  name: 'my-workflow',           // required
  description: 'one-liner shown in the permission dialog',  // required
  whenToUse: 'optional, shown in the workflow list',
  phases: [                      // optional; one entry per phase() call, titles matched exactly
    { title: 'Find',   detail: 'fan out searchers' },
    { title: 'Verify', detail: 'adversarially check each hit' },
  ],
}
// ── body starts here (async context: top-level await + return are legal) ──
phase('Find')
const hits = await agent('Find all the X', { schema: HITS_SCHEMA })
phase('Verify')
const checked = (await parallel(hits.items.map(h => () =>
  agent(`Verify: ${h.title}`, { schema: VERDICT_SCHEMA })
))).filter(Boolean)
return { checked }
```

**The meta block** is metadata only — it must be a pure object literal (no
variables, function calls, spreads, or string interpolation) because the harness
parses it statically before running anything. `name` and `description` are
required; `whenToUse` and `phases[]` are optional. Phase titles in `meta.phases`
must match the `phase()` calls in the body exactly.

**The body** runs in an async context, so top-level `await` and `return` are
legal. The globals below are injected — you do not import them.

### Building blocks (globals in the body)

- **`agent(prompt, opts?) -> Promise`** — spawn one subagent.
  - no `schema` → returns final text (a string).
  - with `schema` (a JSON Schema) → forces structured output, validated at the
    tool layer (retries on mismatch), returns the parsed object. This is the
    workhorse.
  - `opts`: `label`, `phase`, `model`/`effort` (tier overrides — usually omit),
    `isolation:'worktree'` (own git worktree for parallel file mutation;
    expensive), `agentType` (a named subagent, e.g. `'Explore'`,
    `'code-reviewer'`).
  - returns `null` on skip/death → pair with `.filter(Boolean)`.
- **`pipeline(items, stage1, stage2, …) -> Promise<any[]>`** — each item flows
  through ALL stages independently, with **no barrier between stages**. This is
  the DEFAULT shape. The stage callback gets `(prevResult, originalItem, index)`.
  A throwing stage drops that item to `null`.
- **`parallel(thunks) -> Promise<any[]>`** — run concurrently with a **BARRIER**
  (awaits all). A throwing thunk → `null` in results. Never rejects.
- **`phase(title)`** — start a progress group. **`log(msg)`** — emit a narrator
  line.
- **`args`** — the value passed as the tool's `args` (parameterize reusable
  workflows).
- **`budget`** — `{ total, spent(), remaining() }`, a token target (for scaling
  depth).
- **`workflow(nameOrRef, args?)`** — run another workflow inline (ONE level deep).

### Mechanics / runtime

- Background execution; returns `runId` + `scriptPath` (the script is
  auto-persisted to a file).
- Concurrency cap ≈ `min(16, cores − 2)` agents at once; excess queue. Lifetime
  cap of 1000 agents. One `parallel`/`pipeline` call takes ≤ 4096 items.
- **Iterate**: edit the persisted `scriptPath`, then re-invoke with
  `{ scriptPath }`.
- **Resume**: `{ scriptPath, resumeFromRunId }` → the longest unchanged prefix of
  `agent()` calls returns cached results; the first edited/new call onward
  re-runs.
- Subagents can reach session MCP tools via ToolSearch.

### The one mechanic that matters most: pipeline vs parallel

- `parallel` is a **barrier** — everything waits for the slowest agent.
- `pipeline` has **no barrier** — item A can be in stage 3 while B is still in
  stage 1.
- Use a barrier ONLY when a stage needs ALL prior results at once: dedup across
  the whole set, early-exit on zero, "compare against the others." Otherwise
  prefer `pipeline` — you stop paying for idle fast agents.

### Constraints (gotchas)

- **Plain JS, not TS** — no type annotations / interfaces / generics (they won't
  parse).
- **No `Date.now()` / `Math.random()` / argless `new Date()`** — they throw
  (they would break deterministic resume). Pass timestamps via `args`; vary by
  index instead of randomness.
- **No filesystem / Node APIs in the script** (subagents can use tools, but the
  orchestration script itself cannot).
- A subagent's final text IS the return value — which is why `schema` matters for
  anything downstream code consumes.
- Only runs when opted in (ultracode on, or "use a workflow" / a skill invokes
  it).

### How to build one

1. **Decide the shape** — what fans out, what verifies, what synthesizes.
   (Comprehensive = decompose + cover in parallel. Confident = independent
   perspectives + an adversarial check before committing.)
2. Write the `meta` literal (phase titles == your `phase()` calls).
3. Define a JSON Schema per structured step.
4. Write the body with `phase` / `agent` / `pipeline` / `parallel`.
5. Invoke via the Workflow tool (inline `script`, or `args` to parameterize).

### Reusable patterns

- **Fan-out → verify** (pipeline): each dimension reviews, then each finding is
  adversarially verified as soon as its review completes.
- **Adversarial verify**: N skeptics per finding, each prompted to REFUTE; kill
  on majority. Perspective-diverse verifiers (correctness / security / repro)
  beat N identical refuters.
- **Judge panel**: N independent attempts from different angles → parallel judges
  score → synthesize from the winner.
- **Loop-until-dry / until-count / until-budget**: keep spawning finders until K
  empty rounds / a target count / budget exhausted. Dedup against a `seen` set,
  not against confirmed results (else rejected items reappear forever).
- **Completeness critic**: a final agent asking "what's missing?"

### Real example in this repo

[`.claude/skills/resolve-loop/resolve-item.workflow.js`](../../.claude/skills/resolve-loop/resolve-item.workflow.js)
— research (parallel, diverse angles) → judge (a ponytail gate) →
implement-in-worktree → adversarial review loop. It exercises `meta` + `phase` +
`parallel` + `agent(schema)` + a bounded loop.

---

## Part 2 — OpenAI's Equivalent: Codex Automations + the Landscape

### Headline

**Codex "automations" are NOT a committed, hand-authorable workflow/template
format.** They are **scheduled natural-language prompts** configured in the Codex
app (a UI form, or by asking Codex in a thread). There is no
`.codex/automations/*.yaml`, no `codex.yaml`, no repo-committed schema. (App
state persists locally at `~/.codex/automations/<id>/automation.toml` — an
implementation detail, community-confirmed, not an authoring surface.)

### What a Codex automation actually is

- "Recurring background tasks": run a prompt on a schedule, post findings to the
  **Triage** inbox, auto-archive empty runs.
- Two kinds: **thread automations** (heartbeat wake-ups attached to a thread;
  minute-level loops; keep context) and **standalone/project automations** (fresh
  independent scheduled runs → Triage; can span projects).
- **One prompt per run** — it can be multi-step inside the model's own reasoning
  and can call skills via `$skill-name`, but there is **NO author-controlled
  fan-out / branching across subagents**.
- **Triggers: schedule only** — minute intervals, daily/weekly presets, or custom
  **cron**. No event triggers (no on-PR / on-push / on-issue / webhook).
- Runs in your local project or a background **git worktree**; write actions
  (including PRs) are gated by the selected approval mode (the canonical
  bug-triage automation is report-only).

### The "templates" that DO exist (≠ a config format)

Starter **prompt** boilerplate with bracketed placeholders in the use-case docs —
e.g. bug-triage: `Run a bug triage sweep for [repo/service/team]… Schedule:
[every weekday morning / daily]`. This is prompt boilerplate, not a config-file
gallery.

### Adjacent committed / event-driven surface (a DIFFERENT feature)

The **Codex GitHub Action** (`openai/codex-action@v1`) is committed YAML in
`.github/workflows/`, fires on real GitHub events (`on: pull_request` / push /
issue), runs `codex exec`, takes its prompt inline or via
`prompt-file: .github/codex/prompts/*.md`, and posts results as PR comments. This
is the committed + event-driven Codex surface — but OpenAI does **not** call it
"automations." Do not conflate the two: automations are UI-configured scheduled
prompts; the GitHub Action is committed, event-driven CI.

### Don't conflate (other Codex files; none define automations)

- `AGENTS.md` — freeform markdown guidance Codex reads before working.
- `.codex/config.toml` / `~/.codex/config.toml` — run behavior (model, approval,
  sandbox, MCP). Unrelated to automations.

### Broader OpenAI orchestration landscape (context)

- **Agents SDK** (Python/TS) — the truest *code* analog to dynamic workflows:
  your code deterministically calls `Runner.run` on sub-agents; fan-out via
  `asyncio.gather`; primitives = Agent / handoffs / tools / guardrails / sessions
  / Runner. No template format (it's code). Runs in YOUR infra.
- **Agent Builder (AgentKit)** — the *visual* analog; If/else + While via CEL,
  Human-approval / MCP nodes. **DEPRECATED** (shuts down 2026-11-30); one-way
  export to Agents SDK code; no confirmed importable declarative format.
- **ChatGPT "Scheduled tasks" / "Workspace Agents"** — natural-language
  "automations" plus a schedule; cron-for-prompts; no authorable template.

### Comparison — Claude dynamic workflows vs Codex automations

| Dimension | Claude dynamic workflows | Codex automations |
|---|---|---|
| Authoring | **Code** (JS orchestrating subagents) | **UI form / NL** (schedule + prompt); no committed file |
| Unit of work | A program (steps, conditionals, data passing) | One prompt per run |
| Control flow / fan-out | First-class (`parallel` / `pipeline` / loops) | None author-controlled (single agent run) |
| Determinism | High (the script is the harness) | Low (NL prompt + model) |
| Structured output | Yes (JSON Schema) | No (findings → Triage) |
| Triggers | In-session / background; you wire them | Schedule only (cron); no events |
| Where it runs | Local (session / background) | Codex app (local / worktree) or cloud container |
| Committed artifact | The `.js` (but session-authored, harness-specific) | None for automations; the Codex GitHub Action YAML is the committed-but-separate surface |

### The gap / opportunity

**Nobody ships a portable, committed, declarative, multi-step
workflow/automation FORMAT for coding agents.**

- Claude dynamic workflows = code — powerful, but session-authored,
  harness-specific, not a committed cross-tool artifact.
- Codex automations = UI-configured scheduled prompts — single-agent, not
  committed.
- The only committed declarative surfaces are CI-shaped single-prompt (the Codex
  GitHub Action YAML) or instruction files (`AGENTS.md`).

→ Whitespace for a **harness-neutral, repo-committed workflow spec** (steps +
fan-out + triggers + typed outputs) that multiple agents could execute. This
rhymes directly with Anvil's cross-harness + durable-state positioning.

---

## Anvil as the Workflow Substrate

The takeaway from Parts 1 and 2: every harness-native orchestration primitive —
Claude dynamic workflows (JS orchestrating subagents), Codex automations
(scheduled prompts), the OpenAI Agents SDK (code) — is **ephemeral and
harness-specific**. They keep intermediate state in script variables and throw it
away when the session ends. **Anvil is the durable, governed state + audit layer
underneath that runtime.**

### The second front door

Today Anvil's only front door is the **PRD**:
`PRD → parse → review → plan/score → tasks → claim → packet → submit evidence →
apply`. That is the greenfield path. The **second front door is the
workflow/loop itself.**

**Key refinement: the PRD IS the spec.** For the common case you do not author a
workflow file from scratch — Anvil already turns the PRD into a ready queue. The
job is to *transfer/drive* that queue into whatever loop or automation a runtime
offers, so each step runs Anvil's governed transitions.

### The seam — `anvil next`

The seam is the `next` command
([`bin/src/anvil/cli/claim.py:500`](../../bin/src/anvil/cli/claim.py)). It already
returns the next ready task, or — with `--json` — `{"data": {"task": null}}` on an
empty queue (exit 0). The only missing bit is a **branchable exit code** for
jq-less shells and automations: add a `-q` / `--quiet` flag → **exit 0 if a task
is ready, exit 3 if the queue is empty.** That single change makes the queue
drainable from any plain shell.

### One primitive, two modes

The loop body already exists:
`claim → packet (the contract that teaches the steps) → do the work →
submit --evidence → apply (the gate)`. The `execute` skill wraps this.

Both usage modes are the **same primitive** — "one governed task per invocation":

- **Fire once** — run the body once for `anvil next`'s task (a Codex automation
  fire, a CI job, a cron tick).
- **Drain until empty** — `while anvil next -q; do <body>; done` (Claude's
  self-paced `/loop`, or any shell).

Durable state makes both **resumable and safe under concurrency** (single-winner
leases).

### Why this strengthens Anvil

- **Exercises the wedge under real load.** Single-winner leases, file-conflict
  detection, and evidence gating get stress-tested under real parallel load.
- **Covers the brownfield ~75%.** A second entry point covering the ad-hoc /
  brownfield work the PRD front door misses.
- **Collapses the fakoli-flow / fakoli-crew trinity.** Coordination via Anvil
  events instead of grep-parsed status files (SL-4).
- **Correct, not just fast.** Leased + evidence-gated steps cannot double-claim or
  fake "done."

### Where it sits on the roadmap

"Runtime-neutral **workflow**" — the next axis after runtime-neutral **state**.
This is **SL-7** (workflow-step) grown into a product axis, sitting on **SL-3**
(typed proof) and closing **SL-4** (status-file → events).

Backlog items:

- **WF-1** — the `anvil next -q` exit-code seam.
- **WF-2** — committed loop adapters (Claude `/loop`, Codex automation, CI drain)
  plus a how-to.
- **WF-3** — `anvil run-workflow` + a `.anvil/workflows/*.yaml` declarative path —
  *deferred / spec-first*, only for ad-hoc loops NOT derived from a PRD. See the
  local design draft `anvil-workflow-substrate-design.md` for the full narrative.

---

## Sources

**Claude Code dynamic workflows** — the Workflow tool's `meta`/body contract,
globals (`agent`/`pipeline`/`parallel`/`phase`/`args`/`budget`/`workflow`),
resume/iterate mechanics, and constraints, as observed in-harness and in the
in-repo example
[`.claude/skills/resolve-loop/resolve-item.workflow.js`](../../.claude/skills/resolve-loop/resolve-item.workflow.js).

**OpenAI / Codex:**

- developers.openai.com/codex/app/automations
- developers.openai.com/codex/use-cases/automation-bug-triage
- developers.openai.com/codex/changelog
- developers.openai.com/codex/cloud
- developers.openai.com/codex/cloud/environments
- developers.openai.com/codex/github-action
- developers.openai.com/codex/guides/agents-md
- developers.openai.com/codex/config-reference
- openai.github.io/openai-agents-python
- developers.openai.com/api/docs/guides/agent-builder (+ /api/docs/deprecations)
