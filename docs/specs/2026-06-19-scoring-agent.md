# Code-aware, calibrated scoring done in-session (the scoring agent)

**Date:** 2026-06-19
**Status:** Draft PRD — authored from a dogfooding session + 4 research streams
**Plugin:** `anvil`
**Tracks:** scoring quality (`SL`-adjacent); pairs with [`2026-06-19-ergonomics-unattended.md`](2026-06-19-ergonomics-unattended.md) (routing consumes these scores)
**Breaking:** YES (schema v6→v7: the six score dimensions change). Migration is additive-then-backfill; see §5.

---

## 1. Goal

Make anvil's task scoring **code-aware and calibrated**, and do the reasoning
with the **agent already in the session** instead of a separate LLM API call.

Two failures this session motivated it: a refactor scored `complexity=2` that was
really ~4 (a hidden cross-file dependency), and a brand-new isolated parser scored
`blast_radius=5` (it imports nothing and nothing imports it). Both trace to one
root cause — **scoring the predicted `likely_files` text, not the real code graph.**

The fix is not "call an LLM on the task description" (that is today's `--use-llm`,
and the research shows text-only scoring underperforms historical actuals). It is:
let the in-session agent **read the code, the git history, and past outcomes**, and
propose adjustments to a deterministic rule-based floor.

## 2. Decisions locked (from the session Q&A)

1. **The in-session host skill does the scoring reasoning.** Not MCP sampling —
   Claude Code does not support it (anthropics/claude-code#1785, open since
   2025-06). Not a separate API call when an agent is already present.
2. **Adopt the 6-axis rubric as the Score dimensions** (replacing the current six).
   Schema migration v6→v7.
3. **Keep both scores, surface the diff.** A deterministic rule-based value AND the
   agent-proposed value per axis; flag tasks where `|agent − rule| ≥ 2`.
4. **The agent grounds judgment in:** the codebase (read/grep), git history/churn,
   calibration from past tasks, and the dependency/conflict graph.

## 3. The three tiers (one new, two existing, refined)

| Tier | Who scores | When | Determinism |
|---|---|---|---|
| **0 — Rules** | `score_task()` heuristic (`planning/scoring.py`) | always; the **floor** | deterministic |
| **1 — In-session host** | the `/anvil:plan` skill reasons inline (no API call) | an agent host is present | non-deterministic, *clamped by Tier 0* |
| **2 — Direct API** | `--use-llm` → `resolve_planner_provider()` | headless / CI / non-Claude harness | model-dependent |
| *1b — MCP sampling* | `ctx.sample()` in the `score_tasks` MCP tool | *future*, behind a capability probe | guarded seam only |

**The numbers stay rule-based as a floor; the agent proposes, deterministic rails
clamp.** This extends anvil's existing invariant ("the LLM never overwrites the
numeric scores, only the explanation") rather than discarding it. Tier 1b is wired
but dormant: probe `ctx` for the `sampling` capability and light up only if a host
ever implements it.

### Detection — explicit, not sniffed

Add `--scoring-mode {rules|host|api}` (and `Config.scoring_mode`), default `rules`.
The `/anvil:plan` skill — which *knows* it is in-session — passes `host`. CI/headless
keeps the default or sets `api`. Do **not** sniff `CLAUDECODE`/TTY heuristics
(brittle, unportable).

## 4. The six axes (the new Score dimensions)

Replaces `complexity / parallelizability / context_load / blast_radius /
review_risk / agent_suitability`. Each scored 1–5 with a one-line justification.

1. **Structural entanglement** — files touched, import fan-out, cross-module spread,
   coupling; cyclomatic/cognitive complexity and test coverage of the affected code.
2. **Conceptual difficulty** — concurrency/distributed state, algorithmic depth,
   novelty (net-new vs known pattern), specialized domain knowledge.
3. **Uncertainty** — spec clarity, presence of acceptance criteria, likelihood of
   requirement change, number of open unknowns needing investigation.
4. **Verifiability** — can correctness be checked cheaply and deterministically?
   *Low verifiability RAISES effective difficulty* (no feedback signal → no
   self-correction).
5. **Coordination** — people/teams/approvals; sole-owner areas.
6. **Risk & reversibility** — prod/data blast radius, security/privacy/compliance,
   migration/backfill, rollback difficulty. One-way doors.

These axes are *consumed by score-driven routing* — see the companion ergonomics
PRD. (That is why "uncertainty" and "verifiability" are first-class: they decide
whether the agent acts, asks, or writes a test first.)

## 5. What the agent pulls from (ranked — the value-add)

Ranked by research (efferent coupling > churn > coverage as fault predictors):

1. **Real dependency fan-out** — resolve imports/call-sites of `likely_files`. A
   file nothing imports is low-entanglement regardless of size (fixes the
   "isolated parser = 5" error). Efferent coupling beats keyword guessing.
2. **Co-change / evolutionary coupling from git** — `git log` files that
   historically commit together surface the *hidden* dependency a refactor drags
   in (fixes the "complexity-2 was really 4" error).
3. **Relative code churn** — high-churn touched files predict defects; normalize by
   size (absolute churn misleads).
4. **Test surface on the diff** — low coverage of changed lines raises the
   verifiability axis (a *modifier*, not a predictor — coverage ≠ assertion quality).
5. **Cognitive-complexity hotspots** of touched functions (better than cyclomatic).
6. **Concurrent-branch/author churn** → coordination/merge-conflict signal
   (advisory; predictors ~60%).

The agent needs `Read`, `Grep`, `Bash` (for `git log`), and the dependency graph.

## 6. Calibration (the biggest lever)

The agile/ML literature is blunt: **historical actuals beat model sophistication**
(UCL replication, arXiv 2201.05401). And it is nearly free here — `Evidence` already
stores `files_changed`, `commit_sha`, review verdict, and reopened status.

- **New `task_outcomes` table** in `state.db`: on completion store
  `(axis, predicted, actual_proxy, files_touched, review_caught, reopened)`.
- **Per-axis correction factor, shrunk toward 1.0 by sample size:**
  `f_adj = (n·f + k·1) / (n + k)`, with `f = mean(actual/predicted)` and `k ≈ 5`.
  Reference-class forecasting + local calibration in one query — no retraining.
- **Agent path:** retrieve the 3–5 *nearest* past tasks (similarity-selected, not
  random) as few-shot anchors.

## 7. Pitfalls to design against

1. **Trusting `likely_files` blindly** — both failures came from this. The agent
   must verify the files exist and resolve their *real* graph; an empty/wrong
   `likely_files` triggers **discovery**, not a default score.
2. **Small-sample overfitting** — a thin category gives an unstable factor. Require
   a min-n, cap the multiplier, shrink toward the global prior (already in §6).
3. **Goodhart gaming** — if scores feed routing/eval they get padded. Keep the
   calibration signal *separate* from any performance judgement; keep the
   rule-based score as an auditable floor (agent proposes, rails clamp).

## 8. Schema migration (v6 → v7)

The six dims change names/meaning, so this is breaking. Sequence:

1. Add the six new axis columns + keep the old ones for one release (additive ALTER).
2. Re-express `score_task()` (the rule floor) in the six axes.
3. Backfill: map old→new where sensible (`blast_radius→risk_reversibility`,
   `review_risk→verifiability` inverse, `agent_suitability→` inverse of
   uncertainty+verifiability) for historical rows; mark backfilled rows.
4. Remap dependent thresholds: `auto_expand` (was `complexity≥4`) → "several axes
   ≥4 or conceptual_difficulty≥4"; the old `agent_suitability≤2` flag → routing
   (companion PRD). Add a `task_outcomes` table (§6).
5. Drop the old columns in the following release. Three-file version bump per
   CLAUDE.md; replay-equivalence test must pass on the migrated log.

## 9. Open questions

- Exact old→new axis backfill mapping (some are inverses; some have no clean source).
- Does the rule floor produce all six axes, or only the ones it can compute
  deterministically (leaving the rest agent-only with a "rule: n/a" marker)?
- Does the diff-flag (`|agent − rule| ≥ 2`) block promotion or just annotate?
- Where does the agent's per-axis justification live — `Score.explanation` (today)
  or a structured per-axis field?

## 10. References

- `bin/src/anvil/planning/scoring.py` (rule scorer + `_augment_explanation` seam),
  `cli/plan.py` (`--use-llm` ~L91-119, `score` ~L686), `state/models.py`
  (`Score` L249, `Evidence` L420 — outcome fields already present),
  `mcp_server.py:2232` (`score_tasks`, rule-only, unused `ctx`).
- MCP sampling spec: modelcontextprotocol.io/specification/2025-06-18/client/sampling;
  Claude Code gap: anthropics/claude-code#1785.
- Estimation research: arXiv 2201.05401 (text models underperform actuals);
  Zimmermann TSE 2005 (evolutionary coupling); Nagappan & Ball (relative churn);
  arXiv 2403.08430 (similarity-selected few-shot).
