# fakoli-state — Context / Token Footprint Audit

> "Measure before you market." fakoli-state positions itself as *context-frugal* /
> structurally immune to context bloat. This audit measures that claim honestly.

**Reproduce:**

```bash
cd plugins/fakoli-state
uv run --with tiktoken python benchmarks/context_audit.py
```

All numbers below are the script's output and **must match** what it prints.
Tokenizer: `tiktoken` (`cl100k_base`). MCP schemas are measured *live* from the
real FastMCP server serialization (the exact wire payload Claude Code receives),
not estimated. A `chars/4` fallback path exists when tiktoken is absent (it
yields ~7,800 always-on — same shape, slightly higher).

---

## What "always-on" means

The **always-on footprint** is the text that sits in an agent's context the
moment fakoli-state is installed — *before any skill is invoked or any tool is
called*. It is paid on **every turn, by every agent**, for the life of the
session. It comprises:

- **Agent descriptions** — the frontmatter `description` of the 6 agent files
  (including the long `<example>` blocks) lives in the agent registry / system
  prompt always.
- **Skill descriptions** — only the `description:` of the 8 skills is registered
  up front. Skill **bodies** load on demand (progressive disclosure).
- **MCP tool schemas** — the 22 MCP tools (name + description + JSON input
  schema) are injected whenever the MCP server is connected.
- **Hook injection** — the `SessionStart` hook's stdout is injected once per
  session.
- **Commands** — slash-command frontmatter (fakoli-state ships **none**).

**On-demand** content (skill bodies, agent bodies) is *not* in the baseline and
is counted separately.

---

## Always-on footprint — per-category table

| Category | Items | Tokens | Share |
|----------|------:|-------:|------:|
| MCP tool schemas | 22 | **3,903** | 53.8% |
| Agent descriptions (registry) | 6 | **2,769** | 38.2% |
| Skill descriptions (registry) | 8 | **554** | 7.6% |
| Hook injection (SessionStart) | 1 | **22** | 0.3% |
| Command descriptions | 0 | **0** | 0.0% |
| **ALWAYS-ON GRAND TOTAL** | — | **7,248** | 100% |

**Always-on grand total: ~7,248 tokens.**

Two categories carry 92% of the cost: **MCP tool schemas (53.8%)** and **agent
descriptions (38.2%)**. Skills, hooks, and commands are noise by comparison.

---

## On-demand footprint (progressive disclosure — NOT in baseline)

| Category | Items | Tokens (sum) |
|----------|------:|-------------:|
| Skill bodies (on invocation) | 8 | 31,575 |
| Agent bodies (on dispatch) | 6 | 11,128 |
| **On-demand total (worst case, all loaded)** | 14 | **42,703** |

This 42.7K is the worst case if *every* body loaded simultaneously. Real usage
loads one skill body (~3–5K) plus maybe one agent body (~1–3K) per active
workflow. The progressive-disclosure design genuinely works: ~43K of content is
kept out of the baseline and 86% of the per-skill body weight is deferred.

---

## Top 10 always-on contributors (single items)

| # | Tokens | Share | Item | Category |
|--:|-------:|------:|------|----------|
| 1 | 760 | 10.5% | `docs-scribe` | Agent descriptions |
| 2 | 705 | 9.7% | `marketplace-scribe` | Agent descriptions |
| 3 | 594 | 8.2% | `plan_tasks` | MCP tool schemas |
| 4 | 488 | 6.7% | `state-keeper` | Agent descriptions |
| 5 | 455 | 6.3% | `apply_review_decision` | MCP tool schemas |
| 6 | 409 | 5.6% | `planner` | Agent descriptions |
| 7 | 344 | 4.7% | `score_tasks` | MCP tool schemas |
| 8 | 233 | 3.2% | `critic` | Agent descriptions |
| 9 | 212 | 2.9% | `parse_prd` | MCP tool schemas |
| 10 | 195 | 2.7% | `submit_completion_evidence` | MCP tool schemas |

The two heaviest items in the entire baseline are **agent descriptions for two
repo-maintenance agents** (`docs-scribe`, `marketplace-scribe`) that a standalone
end-user never dispatches — together **1,465 tokens (20% of the always-on
budget)** spent advertising agents that maintain *this plugin's own docs and
marketplace listing*.

---

## Verdict on the "context-frugal" claim: **MIXED — defensible, not bulletproof**

**Where the claim holds (the honest good news):**

- **~7.2K always-on is genuinely modest** for a plugin that ships 6 agents,
  8 skills, 22 MCP tools, and a hook suite. As a fraction of a 200K context
  window that is **~3.6%** — small.
- **The progressive-disclosure architecture is real and load-bearing.** 42.7K of
  skill/agent body content is deferred. The 8 skill descriptions cost just
  **554 tokens always-on** while their bodies (31.6K) stay out of context until
  fired. That is exactly the "structurally immune to bloat" mechanism working as
  advertised. ~86% of skill weight is deferred.
- **Hooks are nearly free** (22 tokens) and correctly engineered: a single
  one-line `SessionStart` injection; `PreToolUse`/`PostToolUse` stdout is
  transient, not baseline.
- **No command bloat** (0 tokens — none ship).

**Where the claim is overstated (the honest bad news):**

- **The MCP schemas are the single largest always-on cost (3,903 tok, 53.8%) and
  they are NOT deferrable today.** Whenever the MCP server is connected, all 22
  tool schemas are in context every turn. This is the opposite of "context
  frugal" — it is the one category that grows linearly with every tool added and
  has no progressive-disclosure escape hatch. `plan_tasks` alone is 594 tokens.
- **Agent descriptions are bloated by verbose `<example>` blocks.** Of the 2,769
  tokens in agent descriptions, **1,888 (68%) are `<example>`/`<commentary>`
  blocks** — only 881 tokens are the actual "use this agent when…" trigger text
  the registry needs. `docs-scribe` (760 tok) and `marketplace-scribe` (705 tok)
  carry three full worked examples each.
- **Two of the six always-advertised agents are repo-internal maintenance bots**
  (`docs-scribe`, `marketplace-scribe`) irrelevant to a standalone user, yet they
  occupy the #1 and #2 spots in the baseline (1,465 tok / 20%).

**Bottom line:** fakoli-state is *frugal where it controls the mechanism*
(skills, hooks, commands — excellent) and *not frugal where it doesn't lean on
that mechanism* (MCP schemas, verbose agent examples). The headline 7.2K is fine;
the composition shows ~3.4K of it (47%) is trimmable without losing capability.

---

## Trim recommendations (with measured savings)

Ordered by savings-per-effort. All savings are measured, not guessed.

### 1. Strip `<example>` blocks out of agent descriptions → save ~1,800 tok (25% of baseline)

The `<example>`/`<commentary>` blocks account for **1,888 of 2,769** agent-
description tokens (68%). The registry only needs the trigger sentence + trigger
words to route correctly; the worked examples belong in the agent *body* (which
is on-demand and free until dispatch), not the always-on description.

- Move every `<example>` block from frontmatter `description` into the agent's
  markdown body.
- Keep ~1–2 lines of trigger text per agent in the description.
- **Estimated saving: ~1,800 tokens** (agent descriptions drop from 2,769 →
  ~880). New always-on grand total ≈ **5,450 tok**.

### 2. Make the two repo-maintenance agents opt-in / move them out of the shipped plugin → save ~1,465 tok (20% of baseline)

`docs-scribe` (760) and `marketplace-scribe` (705) maintain *fakoli-state's own*
docs, CHANGELOG, marketplace.json, and registry. A standalone end-user never
dispatches them; they are developer-of-this-plugin tooling. Every install pays
1,465 tok to advertise them.

- Move them to a dev-only location, or gate them behind a separate dev plugin,
  or (cheapest) apply recommendation #1 to them first so their *descriptions*
  shrink even if the agents stay.
- **Estimated saving: ~1,465 tokens** if removed outright; ~1,100 tokens if kept
  but example-stripped (overlaps with #1).

### 3. Trim verbose MCP tool descriptions / defer rarely-used tools → save ~600–1,200 tok

MCP schemas are 3,903 tok and undeferrable while connected. The fattest tools are
planning/review tools used in a single phase: `plan_tasks` (594), `score_tasks`
(344), `apply_review_decision` (455), `parse_prd` (212), `review_prd` (195) —
together ~1,800 tok for the one-time PRD→plan phase.

- **Tighten docstrings:** the per-tool description is derived from the function
  docstring. Cutting each long docstring to a crisp one-to-two-line summary and
  pushing parameter prose into `Field(description=...)` only where needed
  realistically removes **~600–900 tok** across the 22 tools with zero
  capability loss.
- **(Structural, larger) Split the server into a lean default tool surface +
  optional planning tools.** The execution-loop tools (`get_next_task`,
  `claim_task`, `submit_*`, `update_task_status`) are what agents use turn-to-
  turn; the PRD/plan/score/review tools are used once. Exposing the planning
  tools behind a second MCP server or a lazily-mounted toolset would remove
  **~1,200 tok** from steady-state execution contexts.

### 4. Consolidate skill descriptions (low priority) → save ~150 tok

Skill descriptions are already lean (554 tok total). `start-prd` (129) and
`resolve-decisions` (99) are the only outliers and embed example trigger phrases.
Marginal — only worth doing alongside a broader pass. **~150 tok.**

### Combined realistic target

Applying #1 + #3 (docstring tighten) — the two no-capability-loss changes —
takes the always-on baseline from **7,248 → ~4,700 tok (–35%)**. Adding #2
(de-shipping the maintenance agents) reaches **~3,800 tok (–48%)**. That is the
difference between "modest" and "genuinely frugal," and it lets the marketing
claim stand on measured ground.

---

## Methodology notes

- **MCP schemas measured live.** The script shells into the plugin's own venv,
  loads the FastMCP `mcp` instance, and serializes each tool via
  `to_mcp_tool()` to the exact compact-JSON wire form the client receives. No
  estimation. If the runtime is unreachable the script reports the MCP subtotal
  as 0 and says so (it does not fabricate).
- **Agent/skill descriptions** are parsed from frontmatter without PyYAML; YAML
  block scalars (`description: >`) are reconstructed faithfully (the indented
  body is what the registry actually carries).
- **Hook injection** is the real stdout of `detect-state.sh` run in an
  uninitialized scratch dir (the install-day baseline). Per-tool-use hook output
  is transient and excluded from the always-on figure by design.
- **On-demand totals** sum *all* bodies as a worst case; steady-state usage
  loads a small fraction.
- Numbers use `cl100k_base`. Claude's production tokenizer differs slightly, so
  treat these as accurate-to-±5% relative measures, which is the right
  resolution for trim decisions.
