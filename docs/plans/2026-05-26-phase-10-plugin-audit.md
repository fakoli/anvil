# anvil Phase 10 — plugin-dev best-practices audit + 5 new critic agents

**Goal:** Create 5 new cross-plugin specialist critic agents in fakoli-crew, run their first audit on anvil v1.9.0, apply MUST FIX findings inline, and ship anvil v1.10.0 + fakoli-crew v2.1.0.

**Spec:** `plugins/anvil/docs/specs/2026-05-26-plugin-audit-and-critics.md`
**Language:** Python (anvil runtime) + Markdown (critic agent specs) + Bash (smoke-test fixtures and runner)
**Crew:** fakoli-crew v2.0.0 (8 agents: critic, guido, herald, keeper, scout, sentinel, smith, welder)
**Branch:** `feat/anvil-phase-10-audit`
**Working dir:** `/Users/sekoudoumbouya/ai-code/claude-env/fakoli-plugins`

---

## Scout corrections to the spec (verified pre-plan)

Three corrections from `agent-scout-status.md` that this plan reflects (the spec stays as written for historical record; the plan supersedes):

1. **Frontmatter key is `tools:`, not `allowed-tools:`.** The spec mistakenly used `allowed-tools:` (a command-frontmatter key) for agents. All 5 new critic specs use `tools:`. The structure-critic's own check list also corrects this: it MUST flag `allowed-tools:` on agent files as MUST FIX.
2. **`plugins/fakoli-crew/tests/` does not exist** — needs scaffolding. Task T0 added.
3. **Hook-critic ambivalence about `set -e`** — plugin-dev's general recommendation is `set -euo pipefail`, but plugins with a non-blocking hook contract (anvil) explicitly forbid `set -e` to preserve graceful degradation. Hook-critic's system prompt MUST detect the plugin's hook contract first (read `hooks.json` and look for "non-blocking" language in plugin docs), then enforce accordingly.
4. **Recommended non-colliding colors** for the 5 new critics: agent-critic=`magenta`, skill-critic=`teal`, hook-critic=`gray`, mcp-critic=`white`, structure-critic=`brown`. (fakoli-crew currently uses: red, blue, pink, purple, cyan, orange, green, yellow.)

---

## Tasks

### T0 — Scaffold `plugins/fakoli-crew/tests/`

**Intent:** Create the test infrastructure that fakoli-crew lacks today, so the new critic agents have somewhere to land their fixture-based smoke tests.

**Acceptance criteria:**
- `plugins/fakoli-crew/tests/` directory exists with a `README.md` describing the test conventions (bash test scripts following `plugins/anvil/tests/test_hooks.sh` precedent; no Python dependency added to fakoli-crew).
- `plugins/fakoli-crew/tests/fixtures/audit-targets/` subdirectory exists, ready for the 5 known-bad fixtures.
- `plugins/fakoli-crew/tests/test_critics.sh` exists as a bash runner stub that, when executed manually, lists each critic and the manual-verification recipe (which fixture to feed it, which severity to expect). It does NOT attempt to dispatch Claude Code agents from bash (impossible from a shell context).

**Scope:**
- `plugins/fakoli-crew/tests/README.md` (new)
- `plugins/fakoli-crew/tests/fixtures/audit-targets/` (new directory; .gitkeep ok)
- `plugins/fakoli-crew/tests/test_critics.sh` (new)

**Agent:** smith
**Verify:** `test -d plugins/fakoli-crew/tests/fixtures/audit-targets && test -x plugins/fakoli-crew/tests/test_critics.sh && bash plugins/fakoli-crew/tests/test_critics.sh --list`
**Depends on:** (none)

---

### T1 — Create `agent-critic.md`

**Intent:** Add the cross-plugin specialist critic for `<plugin-root>/agents/*.md` files. Adapts plugin-dev's agent-development methodology and reports findings using the fakoli-crew critic severity rubric (MUST FIX / SHOULD FIX / CONSIDER / NIT).

**Acceptance criteria:**
- File `plugins/fakoli-crew/agents/agent-critic.md` exists with valid frontmatter: `name: agent-critic`, `description: <triggers + 3 example blocks>`, `color: magenta`, `model: opus`, `tools: Read, Grep, Glob`.
- Description block contains exactly 3 `<example>` triggers each with a `<commentary>` rationale, matching the existing `plugins/fakoli-crew/agents/critic.md` template format.
- System prompt enumerates the scope rubric: frontmatter validity (name/description/color/model/tools), color collision detection across siblings, description-must-have-3-examples requirement (with commentary), allowed-tools tightness (no Bash on review-only agents), defer-to rules (no dangling references), model selection appropriateness, file length proportionality.
- System prompt explicitly catches the spec-discovered antipattern: `allowed-tools:` used on an agent file must be flagged MUST FIX (it's a command-key, not an agent-key).
- File length proportionate to existing agents (~200–300 lines).

**Scope:**
- `plugins/fakoli-crew/agents/agent-critic.md` (new)

**Agent:** smith
**Verify:** `head -10 plugins/fakoli-crew/agents/agent-critic.md | grep -E "(name: agent-critic|color: magenta|model: opus|tools:)"` shows 4 matches; file > 200 lines.
**Depends on:** (none)

---

### T2 — Create `skill-critic.md`

**Intent:** Add the cross-plugin specialist critic for `<plugin-root>/skills/*/SKILL.md` files. Adapts plugin-dev's skill-development methodology.

**Acceptance criteria:**
- File `plugins/fakoli-crew/agents/skill-critic.md` exists with frontmatter: `name: skill-critic`, `color: teal`, `model: opus`, `tools: Read, Grep, Glob`.
- 3 `<example>` triggers with commentary.
- System prompt enumerates: frontmatter validity (name + description required, description must be triggering not vague), one-question-at-a-time discipline check, hard-gate presence on irreversible actions, decision-flow diagram presence for skills with 3+ steps, lazy-loading discipline (body stays short; supporting material lives in `references/`), no-fuzzy-detection rule (`if X seems available` is SHOULD FIX; explicit shell check is the bar), referenced paths must exist on disk.
- File length 200–300 lines.

**Scope:**
- `plugins/fakoli-crew/agents/skill-critic.md` (new)

**Agent:** smith
**Verify:** Frontmatter grep + line count check (same shape as T1).
**Depends on:** (none)

---

### T3 — Create `hook-critic.md`

**Intent:** Add the cross-plugin specialist critic for `<plugin-root>/hooks/*.sh` + `hooks.json`. Adapts plugin-dev's hook-development methodology, with explicit awareness that the `set -e` rule depends on the plugin's hook contract.

**Acceptance criteria:**
- File `plugins/fakoli-crew/agents/hook-critic.md` exists with frontmatter: `name: hook-critic`, `color: gray`, `model: opus`, `tools: Read, Grep, Glob, Bash`. (Bash is needed for shellcheck-style introspection.)
- 3 `<example>` triggers with commentary.
- System prompt enumerates: shebang must be `#!/usr/bin/env bash` (portable), `${CLAUDE_PLUGIN_ROOT}` usage for plugin-internal paths, no-piped-grep antipattern (use `jq` for JSON), stdin handling correctness (PostToolUse reads stdin; SessionStart doesn't), idempotency, performance on hot events (PreToolUse fires per Edit/Write), `hooks.json` matcher patterns + event-name validity + command file existence.
- **Critical contract-awareness rule** in the system prompt: before flagging `set -e` or its absence, hook-critic MUST detect the plugin's hook contract by reading `hooks.json` and the plugin's docs/README for "non-blocking" language. If non-blocking: `set -e` is MUST FIX (it breaks the contract). If standard: `set -euo pipefail` is the recommendation; its absence is SHOULD FIX.
- File length 200–300 lines.

**Scope:**
- `plugins/fakoli-crew/agents/hook-critic.md` (new)

**Agent:** smith
**Verify:** Frontmatter grep + line count + grep for "non-blocking" in body confirming contract-awareness rule is present.
**Depends on:** (none)

---

### T4 — Create `mcp-critic.md`

**Intent:** Add the cross-plugin specialist critic for `.mcp.json` + MCP server implementation files. Adapts plugin-dev's mcp-integration methodology.

**Acceptance criteria:**
- File `plugins/fakoli-crew/agents/mcp-critic.md` exists with frontmatter: `name: mcp-critic`, `color: white`, `model: opus`, `tools: Read, Grep, Glob`.
- 3 `<example>` triggers with commentary.
- System prompt enumerates: `.mcp.json` schema validity (`mcpServers.<name>.type/command/args`), `${CLAUDE_PLUGIN_ROOT}` in `args` for portable resolution, tool `@mcp.tool()` decorations with `description=` strings, typed parameter annotations (no untyped `Any` without justification), structured error returns (no raw `repr()` or unstructured exceptions), no secret-leak in audit prints or returned strings, stdio vs sse transport choice rationale, actor-identification requirement on mutating tools.
- File length 200–300 lines.

**Scope:**
- `plugins/fakoli-crew/agents/mcp-critic.md` (new)

**Agent:** smith
**Verify:** Frontmatter grep + line count.
**Depends on:** (none)

---

### T5 — Create `structure-critic.md`

**Intent:** Add the cross-plugin specialist critic for plugin manifests + marketplace artifacts + README + CHANGELOG + cross-file version consistency. Standalone (does not delegate to `plugin-dev:plugin-validator`).

**Acceptance criteria:**
- File `plugins/fakoli-crew/agents/structure-critic.md` exists with frontmatter: `name: structure-critic`, `color: brown`, `model: opus`, `tools: Read, Grep, Glob, Bash`. (Bash needed for running `scripts/generate-index.sh --check` and version-grep across multiple files.)
- 3 `<example>` triggers with commentary.
- System prompt enumerates: `plugin.json` required fields (name/version/description/author/repository/license/keywords), version sync across N sources (plugin.json, pyproject.toml if Python, `__init__.py` if Python, marketplace.json entry, registry/index.json entry), README surface tables matching actual `ls` counts, CHANGELOG Keep-a-Changelog format with `[Unreleased]` emptied after tag, marketplace.json plugin entry matching plugin.json on name/description/repository, registry entries reflecting current version, no dead files in `.claude-plugin/`.
- System prompt explicitly notes it is standalone and does NOT call `plugin-dev:plugin-validator`.
- File length 250–350 lines (slightly longer than peers because the rubric is broader).

**Scope:**
- `plugins/fakoli-crew/agents/structure-critic.md` (new)

**Agent:** smith
**Verify:** Frontmatter grep + line count + grep for "standalone" in body confirming the no-delegate decision.
**Depends on:** (none)

---

### T6 — Create 5 known-bad fixtures

**Intent:** Populate `plugins/fakoli-crew/tests/fixtures/audit-targets/` with one known-bad input per critic. Each fixture intentionally contains one or more antipatterns the corresponding critic must surface.

**Acceptance criteria:**
- `plugins/fakoli-crew/tests/fixtures/audit-targets/bad-agent.md` — an agent file with missing `name` frontmatter AND using `allowed-tools:` instead of `tools:` (intentional; agent-critic must MUST-FIX both).
- `plugins/fakoli-crew/tests/fixtures/audit-targets/bad-skill.md` — a SKILL.md with vague description ("a skill that helps with things") and no decision flow (intentional; skill-critic must SHOULD-FIX both).
- `plugins/fakoli-crew/tests/fixtures/audit-targets/bad-hook.sh` — a hook with `set -e` and no `${CLAUDE_PLUGIN_ROOT}` usage on a plugin-internal path (intentional; hook-critic must MUST-FIX both IF the fixture's adjacent `bad-hooks.json` documents a non-blocking contract).
- `plugins/fakoli-crew/tests/fixtures/audit-targets/bad-mcp.json` — `.mcp.json` missing the `args` field (intentional; mcp-critic must MUST-FIX).
- `plugins/fakoli-crew/tests/fixtures/audit-targets/bad-plugin.json` — plugin.json missing `version` and with `description` shorter than the spec floor (intentional; structure-critic must MUST-FIX).
- Each fixture starts with a leading comment block explaining what antipatterns it contains and what severity each critic should surface — so future maintainers don't accidentally "fix" the bug.

**Scope:**
- 5 new fixture files under `plugins/fakoli-crew/tests/fixtures/audit-targets/`
- 1 supporting `bad-hooks.json` next to `bad-hook.sh` for the contract-detection setup

**Agent:** smith
**Verify:** `ls plugins/fakoli-crew/tests/fixtures/audit-targets/ | wc -l` returns ≥6 (5 fixtures + 1 supporting hooks.json).
**Depends on:** T0

---

### T7 — Manual-verification recipe doc

**Intent:** Document the recipe for verifying each critic against its fixture. Since bash cannot dispatch Claude Code agents, the smoke test is a documented manual procedure that a developer (or sentinel in Wave 6) executes once per critic.

**Acceptance criteria:**
- `plugins/fakoli-crew/tests/RECIPES.md` exists with one section per critic.
- Each section lists: (a) which fixture to feed it, (b) the exact Agent dispatch one-liner, (c) the expected severity (e.g., "agent-critic MUST surface at least 1 MUST FIX on bad-agent.md"), (d) how to interpret pass/fail.
- `plugins/fakoli-crew/tests/test_critics.sh --list` output includes a pointer to RECIPES.md.

**Scope:**
- `plugins/fakoli-crew/tests/RECIPES.md` (new)
- `plugins/fakoli-crew/tests/test_critics.sh` (extend; depends on T0 having created the stub)

**Agent:** herald
**Verify:** `grep -c '^## ' plugins/fakoli-crew/tests/RECIPES.md` returns ≥5 (one section per critic).
**Depends on:** T0, T6

---

### T8 — Run first audit on anvil v1.9.0 (5 critics in parallel)

**Intent:** Dispatch all 5 critics simultaneously against anvil v1.9.0's surface area. Each critic writes findings to its own status file. Read-only; no mutations to anvil files.

**Acceptance criteria:**
- `plugins/anvil/docs/plans/agent-agent-critic-status.md` exists with findings on `plugins/anvil/agents/*.md` (6 agents reviewed).
- `plugins/anvil/docs/plans/agent-skill-critic-status.md` exists with findings on `plugins/anvil/skills/*/SKILL.md` (7 skills reviewed).
- `plugins/anvil/docs/plans/agent-hook-critic-status.md` exists with findings on `plugins/anvil/hooks/*.sh` + `hooks.json` (4 hooks + 1 config reviewed). Contract-detection step confirms anvil's non-blocking contract was recognized.
- `plugins/anvil/docs/plans/agent-mcp-critic-status.md` exists with findings on `plugins/anvil/.mcp.json` + MCP server source (13 tools reviewed).
- `plugins/anvil/docs/plans/agent-structure-critic-status.md` exists with findings on `plugins/anvil/.claude-plugin/plugin.json`, `pyproject.toml`, `__init__.py`, `README.md`, `CHANGELOG.md`, plus root `.claude-plugin/marketplace.json` and `registry/*.json` entries.
- Each status file uses the same severity rubric (MUST FIX / SHOULD FIX / CONSIDER / NIT) and includes a "Files reviewed" section listing what it actually opened.

**Scope:**
- read-only audit of anvil v1.9.0 surface area; status files only.

**Agent:** dispatched in parallel — `agent-critic`, `skill-critic`, `hook-critic`, `mcp-critic`, `structure-critic` (all in fakoli-crew once T1-T5 have created them)
**Verify:** all 5 status files exist and non-empty (`ls -la plugins/anvil/docs/plans/agent-*-critic-status.md | wc -l` == 5).
**Depends on:** T1, T2, T3, T4, T5, T0 (need the agents to exist; tests/ infrastructure not strictly needed for the audit but the bundle ships together)

---

### T9 — Consolidate audit into `docs/audits/2026-05-26-plugin-audit.md`

**Intent:** Read the 5 critic status files, produce a single severity-sorted audit doc with per-critic detail sections and a MUST FIX tracking table.

**Acceptance criteria:**
- `plugins/anvil/docs/audits/2026-05-26-plugin-audit.md` exists.
- Top section: summary line with finding counts per severity.
- "Findings table (severity-sorted)" with columns: Severity, Critic, Target file, Line, Finding, Action.
- Five "Per-critic detail" sections containing the verbatim findings from each status file.
- "Items applied this phase" section (initially empty; gets populated by welder commits during T11).
- "Items deferred to Phase 11" section listing all SHOULD FIX / CONSIDER / NIT items with file:line + the critic that found each (used to bootstrap `phase-11-backlog.md` in T12).

**Scope:**
- `plugins/anvil/docs/audits/2026-05-26-plugin-audit.md` (new; `docs/audits/` directory created)

**Agent:** keeper
**Verify:** `test -s plugins/anvil/docs/audits/2026-05-26-plugin-audit.md && grep -cE "^## " plugins/anvil/docs/audits/2026-05-26-plugin-audit.md` returns ≥7 sections.
**Depends on:** T8

---

### T10 — User checkpoint: MUST FIX count review

**Intent:** Surface to the user the total MUST FIX count from T9's audit. If > 20, escalate to user with the option to defer some MUST FIX items to Phase 11 (per spec Risk mitigation). If ≤ 20, proceed.

**Acceptance criteria:**
- A status note in `plugins/anvil/docs/plans/agent-checkpoint-status.md` summarizing: total MUST FIX count, list of file paths affected, recommendation (proceed / escalate).
- If MUST FIX > 20: NEEDS_REVIEW status surfaced; do not start T11 until user approves deferral list.
- If MUST FIX ≤ 20: COMPLETE status; T11 proceeds.

**Scope:**
- `plugins/anvil/docs/plans/agent-checkpoint-status.md` (new)

**Agent:** keeper (lightweight — read the audit doc + write a one-page summary)
**Verify:** Status file exists with one of {COMPLETE, NEEDS_REVIEW}.
**Depends on:** T9

---

### T11 — Apply MUST FIX items (parallel or serial by file overlap)

**Intent:** Apply every MUST FIX item the audit surfaced. Annotate each in the audit doc with `→ fixed in commit <sha>` or `→ deferred with reason <text>` (the latter only with explicit user approval recorded in `agent-checkpoint-status.md`).

**Acceptance criteria:**
- Every MUST FIX row in `docs/audits/2026-05-26-plugin-audit.md`'s findings table has either a `→ fixed in commit <sha>` annotation OR a `→ deferred: <approved-reason>` annotation.
- No silent gaps. (The audit doc is the source of truth; the keeper's release commit references it.)
- For each fix: relevant critic re-runs on the changed file and confirms closure (status file updated with "RE-RUN <timestamp>: closed").
- Test suite still passes (run after every fix, or batched per file group).
- File-overlap rule honored: if two MUST FIX items touch the same file, serialize them (no parallel welders on the same file).

**Scope:**
- determined dynamically by audit findings; could span any of the audit's reviewed files.

**Agent:** welder (potentially N parallel welders if MUST FIX items affect N disjoint files)
**Verify:** `grep -c "→ fixed\|→ deferred" plugins/anvil/docs/audits/2026-05-26-plugin-audit.md` >= MUST FIX count from T9.
**Depends on:** T10

---

### T12 — Create `docs/phase-11-backlog.md`

**Intent:** Materialize the SHOULD FIX / CONSIDER / NIT items from the audit into a Phase 11 backlog doc, following the format of `docs/phase-9-backlog.md`.

**Acceptance criteria:**
- `plugins/anvil/docs/phase-11-backlog.md` exists.
- Each item has: severity, critic that found it, file:line, finding text, recommended action, target phase (defaults to Phase 11; some may be deferred further).
- Cross-references the audit doc by anchor.
- A summary table at top with severity counts.

**Scope:**
- `plugins/anvil/docs/phase-11-backlog.md` (new)

**Agent:** herald
**Verify:** `test -s plugins/anvil/docs/phase-11-backlog.md && grep -cE "^### " plugins/anvil/docs/phase-11-backlog.md` returns N matching the deferred item count.
**Depends on:** T9

---

### T13 — Meta-review of Wave 5 fixes by fakoli-crew:critic

**Intent:** A senior-engineer review of every welder fix applied in T11. Catches over-eager fixes, fixes that introduced new bugs, and SHOULD-FIX items that slipped into MUST-FIX scope.

**Acceptance criteria:**
- `plugins/anvil/docs/plans/agent-critic-status.md` exists with PASS or NEEDS_REVIEW verdict.
- Reviews every file changed during T11 (file list extracted from `git diff main..HEAD --name-only` or from welder status files).
- Reports findings using the standard severity rubric. Any new MUST FIX surfaced here triggers a fresh fix-cycle (welder → re-review, max 3 iterations).

**Scope:**
- read-only review of T11 changes.

**Agent:** fakoli-crew:critic
**Verify:** `grep -E "^(PASS|NEEDS_REVIEW)" plugins/anvil/docs/plans/agent-critic-status.md`.
**Depends on:** T11

---

### T14 — Sentinel acceptance scorecard

**Intent:** Run the 11 acceptance criteria from the spec as a binary PASS/FAIL scorecard.

**Acceptance criteria:**
- `plugins/anvil/docs/plans/agent-sentinel-status.md` exists with a scorecard covering all 11 spec acceptance items.
- Cites real command output (not summaries) for each PASS.
- For any FAIL, reports the exact divergence.
- Final verdict line: COMPLETE (all PASS) or NEEDS_REVIEW (any FAIL).

**Scope:**
- read-only verification across anvil, fakoli-crew, root marketplace artifacts.

**Agent:** fakoli-crew:sentinel
**Verify:** `grep -E "^Final verdict: (COMPLETE|NEEDS_REVIEW)" plugins/anvil/docs/plans/agent-sentinel-status.md`.
**Depends on:** T11

---

### T15 — Release prep (anvil v1.10.0 + fakoli-crew v2.1.0)

**Intent:** Sync versions, write CHANGELOG entries for both plugins, regenerate marketplace + registry, update both README agent tables.

**Acceptance criteria:**
- `plugins/anvil/.claude-plugin/plugin.json`, `plugins/anvil/bin/pyproject.toml`, `plugins/anvil/bin/src/anvil/__init__.py`, and the anvil entry in `.claude-plugin/marketplace.json` all read `1.10.0`.
- `plugins/fakoli-crew/.claude-plugin/plugin.json` and the fakoli-crew entry in `.claude-plugin/marketplace.json` both read `2.1.0`.
- `plugins/anvil/CHANGELOG.md` has new `[1.10.0] — 2026-05-26` entry covering: 5 new fakoli-crew critic agents, first audit applied, MUST FIX items closed.
- `plugins/fakoli-crew/CHANGELOG.md` has new `[2.1.0] — 2026-05-26` entry covering the 5 new critic agents (with brief descriptions).
- `plugins/fakoli-crew/README.md` agent table grows from 8 to 13 rows (add agent-critic, skill-critic, hook-critic, mcp-critic, structure-critic with one-line descriptions).
- `plugins/anvil/README.md` version badge updated to 1.10.0.
- `bash scripts/generate-index.sh` runs cleanly; root marketplace.json and registry/*.json reflect both version bumps.

**Scope:**
- 7 manifest/CHANGELOG/README files + script run.

**Agent:** keeper
**Verify:** `bash scripts/generate-index.sh --check && grep "1.10.0" plugins/anvil/.claude-plugin/plugin.json plugins/anvil/bin/pyproject.toml plugins/anvil/bin/src/anvil/__init__.py && grep "2.1.0" plugins/fakoli-crew/.claude-plugin/plugin.json`.
**Depends on:** T13, T14

---

## Wave Assignment

Computed from the Depends-on graph:

```
Wave 1 (parallel — all new-file work, no overlap):
  T0  smith  → scaffold fakoli-crew/tests/
  T1  smith  → agent-critic.md
  T2  smith  → skill-critic.md
  T3  smith  → hook-critic.md
  T4  smith  → mcp-critic.md
  T5  smith  → structure-critic.md

Wave 2 (parallel — depends on T0):
  T6  smith  → 5 known-bad fixtures
  (T7 deferred to Wave 3 because it also depends on T6)

Wave 3 (sequential edge — T7 needs T6):
  T7  herald → RECIPES.md + test_critics.sh extension

Wave 4 (parallel × 5 — the first audit; depends on T1-T5 + T0):
  T8  agent-critic, skill-critic, hook-critic, mcp-critic, structure-critic (parallel)

Wave 5 (single — keeper consolidation, depends on T8):
  T9  keeper → docs/audits/2026-05-26-plugin-audit.md

Wave 6 (single — user checkpoint, depends on T9):
  T10 keeper → MUST FIX count review (may pause for user input)

Wave 7 (parallel where files disjoint; depends on T10):
  T11 welder(s) → apply MUST FIX items + re-run originating critic per fix
  T12 herald     → docs/phase-11-backlog.md (depends on T9 only — can start in parallel with T11)

Wave 8 (parallel × 2 — review; both depend on T11):
  T13 fakoli-crew:critic   → meta-review of fixes
  T14 fakoli-crew:sentinel → acceptance scorecard

Wave 9 (single — release prep, depends on T13 + T14):
  T15 keeper → version bumps + CHANGELOGs + marketplace regen
```

**Total tasks:** 16
**Total waves:** 9 (largest parallelism: Wave 4 with 5 simultaneous critic dispatches)

---

## Self-review notes

1. **Spec coverage**: all 11 spec acceptance criteria mapped to T1–T15. Critic personas → T1–T5. Fixtures + tests → T0, T6, T7. First audit → T8. Consolidation → T9. MUST FIX fix-cycle → T11. Phase 11 backlog → T12. Release prep → T15.
2. **Criteria clarity**: every acceptance bullet is grep-checkable or file-existence-checkable.
3. **Dependency correctness**: T7 depends on T6 (fixtures first); T8 depends on critics existing; T9 on audit done; T10–T15 follow a clear linear chain except T12 which can parallel T11 (no file overlap). No circular deps.
4. **Agent assignment**: smith owns new-file creation (agents + fixtures + scaffold); herald owns docs (RECIPES.md + phase-11-backlog.md); keeper owns consolidation + release; critic + sentinel own review (one each). Welder gets the implementation-mutation work in T11.
5. **Code-free check**: no function bodies; only intent statements + verifiable acceptance criteria + grep/file-existence verify commands. Color values are configuration, not code.

Plan saved to `plugins/anvil/docs/plans/2026-05-26-phase-10-plugin-audit.md`. Handing off to `/fakoli-flow:execute`.
