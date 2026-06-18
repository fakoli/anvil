# Context budget

> A self-imposed token budget on anvil's plugin surface, enforced in CI
> by [`tests/test_token_budget.py`](../tests/test_token_budget.py).

anvil ships as a Claude Code plugin. Its skills are part of the model's
context, and that context has a cost:

- **Skill frontmatter is always loaded.** Claude Code injects each skill's
  frontmatter `name` + `description` into the system prompt on *every* turn so
  the model can decide when to invoke a skill. This is the plugin's
  always-on "command surface" — the slash commands a user sees — and it is
  paid for whether or not any skill ever fires.
- **A skill body loads on invocation.** The full `SKILL.md` body is pulled in
  only when the skill is actually used, but an oversized body still increases
  context use the moment a user reaches for that skill.

Without a budget, skills accrete prose and the always-loaded surface grows.
This budget keeps the surface bounded and gives CI a gate that catches
meaningful growth, such as a skill body doubling in size, without flapping on
ordinary edits.

## Budgets

Token counts are approximated with the standard `ceil(chars / 4)` heuristic.
We deliberately avoid a real tokenizer (e.g. `tiktoken`) so the gate is
deterministic across machines and needs no network fetch or model-specific BPE
— we care about *relative* growth against a fixed baseline, not byte-exact
agreement with any one model.

| Knob | Value | What it caps |
|------|-------|--------------|
| `ALWAYS_LOADED_FRONTMATTER_BUDGET` | **1000** tok | Combined frontmatter of all skills (always loaded, every turn) |
| `PER_SKILL_FULL_CEILING` | **6000** tok | Any single `SKILL.md` body (loaded on invocation) |
| `TOTAL_FULL_BUDGET` | **40000** tok | All `SKILL.md` bodies combined |

## Baseline

Measured at the time the gate was introduced (backlog T013):

| Metric | Baseline (tok) | Budget (tok) | Headroom |
|--------|----------------|--------------|----------|
| Always-loaded frontmatter, all skills | ~688 | 1000 | ~45% |
| Largest single `SKILL.md` body (`plan`) | ~5097 | 6000 | ~18% |
| All `SKILL.md` bodies combined | ~33200 | 40000 | ~20% |

Per-skill body baseline (full file, `ceil(chars / 4)`):

| Skill | Tokens |
|-------|--------|
| `plan` | ~5097 |
| `claim` | ~4886 |
| `execute` | ~4686 |
| `prd` | ~4340 |
| `finish` | ~4131 |
| `start-prd` | ~3565 |
| `state-ops` | ~3474 |
| `resolve-decisions` | ~3021 |

The gate reports per-skill token counts on failure, so a single CI run names
every offending skill.

## Changing a budget

If growth is intentional (a new skill, a deliberate expansion), raise the
constant in [`tests/test_token_budget.py`](../tests/test_token_budget.py)
**and** update the table above in the same change. The test asserts this doc
exists and references each budget knob by name, so the doc and the gate cannot
silently drift.

Before raising a budget, prefer trimming: move long worked-examples into
linked reference docs under `docs/`, tighten skill descriptions to one or two
sentences, and link to `README` / `docs/cli-reference.md` instead of
duplicating content (the documentation-hygiene pattern used across the
plugin).
