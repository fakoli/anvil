# Anvil — standalone build: final report

> **Anvil — the system of record for agent teams** (formerly fakoli-state). Now at **v0.0.8**.

Historical build report from 2026-06-17. This repo was extracted as the
standalone Anvil product from the fakoli-plugins monorepo and driven through
the backlog listed below. The GitHub repository now lives at
`fakoli/anvil`; this report preserves the extraction context.

## Headline

- **Extracted** to a self-contained repo (own CI, README, LICENSE, `.gitignore`), tests green.
- **Trimmed** for standalone: always-on context footprint **7,248 → ~5,500 tok** (~24% off, ~30% before the new commands re-added a little), the monorepo-only `marketplace-scribe` agent removed, agent-description examples relocated to bodies (no capability lost).
- **Backlog: 18/18 shipped.** Version climbed **1.23.8 → 1.40.0**, then the product was **renamed to Anvil (0.0.8)**. Full suite **1,671 passed** (from 1,432 at import — +239 tests).
- **Method:** every item was implemented by one subagent, then reviewed by fresh subagents using only the diff and acceptance criteria. Failed items were reverted before the next item started, and the cumulative suite was re-verified at each milestone.

## The arc

The standalone extraction followed a competitive analysis against spec-kit and
ponytail. The analysis identified durable, evidence-gated, lease-coordinated
state as the core product boundary: a layer stateless spec-driven tools do not
provide. The build focused on fixing correctness issues, making the surface
scriptable and portable, and completing the extraction backlog.

## Everything shipped

**Critical path (in the v1.23.8 import — built earlier in the session):**
| Ver | Item |
|-----|------|
| 1.23.3 | TOCTOU claim-race fix + CLI lease config + concurrency suite (benchmark collisions → 0) |
| 1.23.4 | machine-readable `--json` across 13 commands |
| 1.23.5 | enforceable evidence gate (CLI **and** MCP, strict mode) |
| 1.23.6 | context-footprint self-audit (reproducible) |
| 1.23.7 | `ANVIL_ROOT` portability + `schema_version` exposure |
| 1.23.8 | read-only `drift` command (+ fixed 2 latent reconciliation bugs) |

**Standalone phase:**
| Ver | Item |
|-----|------|
| 1.24.0 | agent-description trims + removed monorepo-only `marketplace-scribe` |
| 1.25.0 | `migrate state` command (explicit, backed-up, dry-run schema migration) |
| 1.25.1 | token-footprint CI budget gate test |
| 1.26.0 | `init --with-sample` (zero-to-`next` in one offline command) |
| 1.27.0 | brownfield `scan` / `init --from-repo` (draft PRD + codebase model from an existing tree) |
| 1.28.0 | `anvil doctor` health diagnosis (+ `--json`) |
| 1.29.0 | version-pin + `describe` self-describing command surface |
| 1.30.0 | `next_ready` field in finish/submit responses (CLI + MCP) |
| 1.31.0 | global-config layer `~/.config/anvil` with project override |
| 1.32.0 | decision back-propagation to the PRD |
| 1.33.0 | caller-supplied / existing-branch claims (`claim --branch`) |
| 1.33.1 | standalone Getting Started docs (crew/flow-free) |
| 1.34.0 | non-feature task types (bugfix/refactor/modify) through the loop |
| 1.35.0 | `graph --format mermaid` dependency/state diagram |
| 1.36.0 | FastMCP stdio server → Docker MCP catalog packaging |
| 1.37.0 | batch dependency-edit primitive (atomic, cycle-detecting) |
| 1.38.0 | EARS/Gherkin acceptance grammar in the PRD parser |
| 1.39.0 | fast-lane work packets for trivial-scored tasks |
| 1.40.0 | surface prior unresolved review findings on file overlap (T017) |

## What the blind-review loop caught (the value of not trusting reports)

Across the session, the review-plus-verification loop caught defects in several
engine waves: a reviewer read the wrong worktree, a harness mangled valid JSON,
an MCP evidence gate was bypassable, init/read directory handling split
silently, dotfile paths were corrupted, and one T017 attempt introduced a
circular import that blocked test collection. These are the kinds of issues the
product is designed to surface: evidence should outrank status claims.

## Current state

- Branch `main`, working tree clean, **v0.0.8 (schema 5)**, CLI command `anvil`.
- `cd bin && uv run pytest -q` → **1,671 passed** (the 4 optional-`openai` tests pass with
  `uv sync --extra all-providers`, which CI uses; they're the only ones that need it).
- CI: `.github/workflows/ci.yml` (py3.11/3.12, uv sync + pytest + benchmark smoke + the
  token-budget gate).
- Benchmark (`benchmarks/run_benchmark.py`) and context audit (`benchmarks/context_audit.py`)
  are reproducible and committed.

## Not done (by design)

- **Deferred post-v1** (per the earlier Opus review): bidirectional GitHub projection,
  structured contract fields. Read-only `drift` + the existing `sync` cover most of the need.
- **Known follow-up:** `_apply_ddl` re-stamps an un-migratable DB to the current schema
  version *before* raising, masking the mismatch on later opens. Pre-existing; a clean fix
  needs core `initialize`/`_apply_ddl` reordering (now partly mitigated by the explicit
  `migrate state` command).

## Recommended next steps

1. **Create the GitHub repo and push** (`gh repo create fakoli/anvil --source . --push`)
   — CI will run on first push.
2. **Lead the README with benchmark evidence** (collisions 13→0, evidence gate, `drift`).
3. **Point monorepo users to this standalone repo** once it's published.
4. Fix the `_apply_ddl` schema-masking ordering when you next touch core init.
5. Decide whether the monorepo keeps an anvil copy or redirects to this repo.
