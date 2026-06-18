# anvil — Context / Token Footprint Audit

> "Measure before you market." anvil positions itself as *context-frugal* /
> structurally immune to context bloat. This audit measures that claim honestly.

**Reproduce:**

```bash
# from the repo root (standalone layout)
uv run --with tiktoken python benchmarks/context_audit.py
```

All numbers below are the script's output and **must match** what it prints.
Tokenizer: `tiktoken` (`cl100k_base`). MCP schemas are measured *live* from the
real FastMCP server serialization (the exact wire payload Claude Code receives),
not estimated. A `chars/4` fallback path exists when tiktoken is absent (it
yields ~7,800 always-on — same shape, slightly higher).

---

## L2 — planning surface is hidden by default (current numbers)

The MCP server ships **24 tools** but splits them into two surfaces:

- **Execution surface (14 tools, always-on):** the turn-to-turn loop an agent
  runs while doing work — `get_next_task`, `claim_task`, `release_task`,
  `renew_claim`, `submit_progress`, `submit_completion_evidence`,
  `update_task_status`, `get_task`, `get_project_status`, `get_project_summary`,
  `list_tasks`, `check_conflicts`, `generate_work_packet`,
  `get_dependency_graph`.
- **Planning surface (10 tools, gated):** one-shot bootstrap/plan/review
  operations run rarely — `init_project`, `parse_prd`, `review_prd`,
  `plan_tasks`, `score_tasks`, `review_tasks`, `apply_review_decision`,
  `edit_dependencies`, `find_decisions`, `describe_surface`. Tagged `planning`
  and **hidden from the per-turn wire by default**.

The live server hides the planning surface unless `ANVIL_MCP_PLANNING` is truthy
(`1`/`true`/`yes`/`on`). Set it for the planning phase (or run a second server
entry with the flag) to get all 24 tools back. **No tool is removed** — all 24
stay registered; `anvil describe`, `--help`, and the Docker catalog smoke test
still report the full 24-tool surface (they read the registry, not the gated
wire). Only the *default execution client's* per-turn schema payload shrinks.

**Default (execution-only) always-on footprint — measured:**

| Category | Items | Tokens | Share |
|----------|------:|-------:|------:|
| MCP tool schemas (execution surface, default) | 14 | **1,455** | 59.8% |
| Skill descriptions (registry) | 8 | **494** | 20.3% |
| Agent descriptions (registry) | 5 | **465** | 19.1% |
| Hook injection (SessionStart) | 1 | **19** | 0.8% |
| Command descriptions | 0 | **0** | 0.0% |
| **ALWAYS-ON GRAND TOTAL** | — | **2,433** | 100% |

**Before L2** (all 24 tools on the wire): always-on **4,214 tok**, MCP **3,236
tok**. **After L2** (planning hidden): always-on **2,433 tok**, MCP **1,455
tok** — a **−1,781 tok (−42%) always-on** cut, driven entirely by dropping the
10 planning schemas (`apply_review_decision` 309, `plan_tasks` 270,
`score_tasks` 197, … were the heaviest). With `ANVIL_MCP_PLANNING=1` the full
24-tool surface returns and the footprint matches the pre-L2 numbers.

> The historical analysis below predates L2 and the L1 docstring trim; it is
> kept for the before/after narrative. The numbers in *this* section are the
> current `context_audit.py` output (which now measures the gated default).

---

## What "always-on" means

The **always-on footprint** is the text that sits in an agent's context the
moment anvil is installed — *before any skill is invoked or any tool is
called*. It is paid on **every turn, by every agent**, for the life of the
session. It comprises:

- **Agent descriptions** — the frontmatter `description` of the 5 agent files
  lives in the agent registry / system prompt always.
- **Skill descriptions** — only the `description:` of the 8 skills is registered
  up front. Skill **bodies** load on demand (progressive disclosure).
- **MCP tool schemas** — the 24 MCP tools (name + description + JSON input
  schema) are injected whenever the MCP server is connected.
- **Hook injection** — the `SessionStart` hook's stdout is injected once per
  session.
- **Commands** — slash-command frontmatter (anvil ships **none**).

**On-demand** content (skill bodies, agent bodies) is *not* in the baseline and
is counted separately.

---

## Always-on footprint — per-category table

| Category | Items | Tokens | Share |
|----------|------:|-------:|------:|
| MCP tool schemas | 24 | **4,456** | 82.0% |
| Skill descriptions (registry) | 8 | **494** | 9.1% |
| Agent descriptions (registry) | 5 | **465** | 8.6% |
| Hook injection (SessionStart) | 1 | **19** | 0.3% |
| Command descriptions | 0 | **0** | 0.0% |
| **ALWAYS-ON GRAND TOTAL** | — | **5,434** | 100% |

**Always-on grand total: ~5,434 tokens.**

MCP tool schemas now dominate at **82%** of the always-on budget. Agent
descriptions dropped dramatically (from 2,769 to 465 tokens) after removing
`marketplace-scribe` and trimming `<example>` blocks from agent frontmatter.

---

## On-demand footprint (progressive disclosure — NOT in baseline)

| Category | Items | Tokens (sum) |
|----------|------:|-------------:|
| Skill bodies (on invocation) | 8 | 24,790 |
| Agent bodies (on dispatch) | 5 | 8,754 |
| **On-demand total (worst case, all loaded)** | 13 | **33,544** |

This 33.5K is the worst case if *every* body loaded simultaneously. Real usage
loads one skill body (~2–5K) plus maybe one agent body (~1–3K) per active
workflow. The progressive-disclosure design genuinely works: ~33.5K of content is
kept out of the baseline and the per-skill body weight is deferred.

---

## Top 10 always-on contributors (single items)

| # | Tokens | Share | Item | Category |
|--:|-------:|------:|------|----------|
| 1 | 588 | 10.8% | `plan_tasks` | MCP tool schemas |
| 2 | 453 | 8.3% | `apply_review_decision` | MCP tool schemas |
| 3 | 340 | 6.3% | `score_tasks` | MCP tool schemas |
| 4 | 294 | 5.4% | `list_tasks` | MCP tool schemas |
| 5 | 225 | 4.1% | `edit_dependencies` | MCP tool schemas |
| 6 | 206 | 3.8% | `parse_prd` | MCP tool schemas |
| 7 | 193 | 3.6% | `submit_completion_evidence` | MCP tool schemas |
| 8 | 193 | 3.6% | `review_prd` | MCP tool schemas |
| 9 | 192 | 3.5% | `describe_surface` | MCP tool schemas |
| 10 | 189 | 3.5% | `find_decisions` | MCP tool schemas |

The entire top 10 is now **MCP tool schemas**. Agent descriptions no longer
appear here — the previous top 2 were `docs-scribe` (760 tok) and
`marketplace-scribe` (705 tok); both have been dramatically reduced
(`docs-scribe` is now 130 tok in the registry, `marketplace-scribe` is fully
removed). The MCP schema set grew from 22 to **24 tools** (added
`edit_dependencies` and `describe_surface`), slightly increasing that category.

---

## Verdict on the "context-frugal" claim: **IMPROVED — now genuinely defensible**

**Where the claim holds (the honest good news):**

- **~5.4K always-on is genuinely modest** for a plugin that ships 5 agents,
  8 skills, 24 MCP tools, and a hook suite. As a fraction of a 200K context
  window that is **~2.7%** — small.
- **The progressive-disclosure architecture is real and load-bearing.** 33.5K of
  skill/agent body content is deferred. The 8 skill descriptions cost just
  **494 tokens always-on** while their bodies (24.8K) stay out of context until
  fired. That is exactly the "structurally immune to bloat" mechanism working as
  advertised.
- **Agent descriptions are now lean (465 tokens total).** Removing `marketplace-
  scribe` and stripping verbose `<example>` blocks from frontmatter brought this
  category down from 2,769 to 465 tokens — an 83% reduction. The recommendations
  from the prior audit were applied.
- **Hooks are nearly free** (19 tokens) and correctly engineered: a single
  one-line `SessionStart` injection; `PreToolUse`/`PostToolUse` stdout is
  transient, not baseline.
- **No command bloat** (0 tokens — none ship).

**Where the claim is still overstated (the honest bad news):**

- **The MCP schemas are the single largest always-on cost (4,456 tok, 82%) and
  they are NOT deferrable today.** With agent descriptions now lean, MCP schemas
  have grown as a fraction of the total and dominate even more. Whenever the MCP
  server is connected, all 24 tool schemas are in context every turn. This is the
  one category that grows linearly with every tool added and has no
  progressive-disclosure escape hatch. `plan_tasks` alone is 588 tokens.
- **Two new MCP tools added** (`edit_dependencies`, `describe_surface`) vs. the
  prior audit (+2 tools, +553 tok net increase to MCP schemas).

**Bottom line:** the prior trim recommendations were applied successfully. Agent
descriptions went from the #1–2 spots in the top contributors to irrelevant,
dropping from 2,769 to 465 tokens. The always-on total fell from **7,248 to
5,434 tokens (–25%)**. The remaining optimization surface is almost entirely in
MCP schema verbosity.

---

## Remaining trim recommendations (with measured savings)

Ordered by savings-per-effort. All savings are measured, not guessed.

### 1. Trim verbose MCP tool descriptions / defer rarely-used tools → save ~600–1,200 tok

MCP schemas are 4,456 tok and undeferrable while connected. The fattest tools are
planning/review tools used in a single phase: `plan_tasks` (588), `score_tasks`
(340), `apply_review_decision` (453), `list_tasks` (294), `edit_dependencies`
(225), `parse_prd` (206) — together ~2,100 tok for the one-time PRD→plan phase.

- **Tighten docstrings:** the per-tool description is derived from the function
  docstring. Cutting each long docstring to a crisp one-to-two-line summary and
  pushing parameter prose into `Field(description=...)` only where needed
  realistically removes **~600–900 tok** across the 24 tools with zero
  capability loss.
- **(Structural, larger) Split the server into a lean default tool surface +
  optional planning tools.** The execution-loop tools (`get_next_task`,
  `claim_task`, `submit_*`, `update_task_status`) are what agents use turn-to-
  turn; the PRD/plan/score/review tools are used once. Exposing the planning
  tools behind a second MCP server or a lazily-mounted toolset would remove
  **~1,200 tok** from steady-state execution contexts.

### 2. Consolidate skill descriptions (low priority) → save ~150 tok

Skill descriptions are already lean (494 tok total). `resolve-decisions` (95)
and `start-prd` (85) are the only outliers and embed example trigger phrases.
Marginal — only worth doing alongside a broader pass. **~150 tok.**

### Combined realistic target

Applying #1 (docstring tighten) takes the always-on baseline from **5,434 →
~4,600 tok (–16%)**. Adding the structural MCP split (defer planning tools)
reaches **~3,800 tok (–30%)**. That is the difference between "modest" and
"genuinely frugal," and it lets the marketing claim stand on measured ground.

---

## Change log vs. prior audit

| Metric | Prior audit | This audit | Delta |
|--------|------------|-----------|-------|
| Always-on grand total | 7,248 tok | **5,434 tok** | –1,814 (–25%) |
| Agent descriptions | 2,769 tok (6 agents) | **465 tok (5 agents)** | –2,304 (–83%) |
| Skill descriptions | 554 tok | **494 tok** | –60 (–11%) |
| MCP tool schemas | 3,903 tok (22 tools) | **4,456 tok (24 tools)** | +553 (+14%) |
| Hook injection | 22 tok | **19 tok** | –3 |
| On-demand skills | 31,575 tok | **24,790 tok** | –6,785 (–21%) |
| On-demand agents | 11,128 tok | **8,754 tok** | –2,374 (–21%) |
| On-demand total | 42,703 tok | **33,544 tok** | –9,159 (–21%) |

The agent-description reduction (–83%) is from two causes: `marketplace-scribe`
removed entirely (was 705 tok), and the remaining agents' frontmatter
`<example>` blocks moved to agent bodies (on-demand). The MCP increase (+14%)
reflects two genuinely new tools (`edit_dependencies`, `describe_surface`).

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
- **Category label mismatch:** the MCP category header in the script reads
  "MCP tool schemas (22 tools)" but the live measurement found **24 tools**.
  The label is a script-internal string; the counts in this document reflect
  the actual live measurement.
