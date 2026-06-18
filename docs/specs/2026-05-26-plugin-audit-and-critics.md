# Spec — anvil Phase 10: plugin-dev best-practices audit + 5 new critic agents

**Date:** 2026-05-26
**Author:** Claude (autonomous brainstorm session with user)
**Status:** draft — pending user approval
**Target releases:**
- `anvil` v1.9.0 → v1.10.0
- `fakoli-crew` v2.0.1 → v2.1.0 (minor bump — 5 new agents)

---

## Goal

Bring `anvil` into closer alignment with the `plugin-dev` plugin's best-practices guidance for agent, skill, hook, MCP, and plugin-structure development. Achieve this by:

1. Creating five new cross-plugin specialist critic agents inside `fakoli-crew` — one per applicable plugin-dev specialty.
2. Running those critics against `anvil v1.9.0` as their first real-world audit.
3. Producing a single consolidated audit doc with severity-sorted findings.
4. Applying every MUST FIX finding immediately (inline fix-cycle discipline matching `fakoli-flow:execute`'s critic gate); deferring SHOULD FIX / CONSIDER / NIT to a new `docs/phase-11-backlog.md`.
5. Shipping anvil as v1.10.0 and fakoli-crew as v2.1.0 in a single PR-per-plugin pair.

This phase delivers both **immediate quality improvement** to anvil AND **reusable audit machinery** that every future fakoli plugin can invoke against itself.

---

## Context

`anvil v1.9.0` ships with 6 plugin-owned agents (planner, critic, sentinel, state-keeper, marketplace-scribe, docs-scribe), 7 skills (brainstorm, claim, execute, finish, plan, prd, state-ops), 4 hooks (capture-evidence.sh, check-claim.sh, detect-state.sh, record-file-change.sh), 1 MCP server with 13 tools, and a `plugin.json` manifest. Across Phases 1–9 it was built fast — 9 phases, ~30 days, 17 PRs. Some surfaces were built early and have not been re-audited against the plugin-dev best-practices skills that landed later in the ecosystem.

`plugin-dev` offers 7 specialty skills (agent-development, skill-development, hook-development, command-development, mcp-integration, plugin-settings, plugin-structure) and 3 reviewer agents (agent-creator, plugin-validator, skill-reviewer). Of the 7 skills, 5 are directly applicable to anvil's surface area: **agent, skill, hook, mcp, structure**. (`command-development` is N/A — anvil exposes no Claude Code slash commands. `plugin-settings` is small and rolled into `structure-critic`.)

`fakoli-crew v2.0.1` houses 8 cross-plugin specialist agents today: critic, guido, herald, keeper, scout, sentinel, smith, welder. The 5 new critics fit naturally alongside this roster — same naming convention, same single-noun discipline, same defer-to patterns.

---

## Architecture

### Component placement

**New critics in fakoli-crew** (`plugins/fakoli-crew/agents/`):
- `agent-critic.md`
- `skill-critic.md`
- `hook-critic.md`
- `mcp-critic.md`
- `structure-critic.md`

Placed in fakoli-crew (not anvil) because they are **plugin-generic** — they audit any plugin's surfaces, not anvil-specific structure. Reusable by every future fakoli plugin and by external plugin authors who install fakoli-crew.

**anvil can invoke them** via:
- `fakoli-flow:execute` (which auto-discovers fakoli-crew agents)
- direct `Agent(subagent_type="fakoli-crew:agent-critic", prompt="...")` from any session that has fakoli-crew installed
- a future anvil skill could wrap them for one-command audit (deferred to Phase 11 — out of scope here)

**No coordinator agent.** The fakoli-flow wave engine already dispatches parallel agents per its declared dependency graph. Adding a "lead critic" or "audit-conductor" agent would duplicate that orchestration. The critics are leaf agents.

### Invocation surface

Each critic is callable as a leaf agent. It reads files in scope, writes findings to `docs/plans/agent-<name>-critic-status.md` per the established status-file protocol, and returns. It NEVER edits the files it audits — pure read + report.

Critics report findings in the existing `fakoli-crew:critic` severity rubric:
- **MUST FIX** — correctness bugs, security issues, broken contracts, anything that should block ship
- **SHOULD FIX** — quality issues worth addressing but not blocking
- **CONSIDER** — suggestions for improvement
- **NIT** — minor style issues

### Audit-doc consolidation

After all 5 critics return, `fakoli-crew:keeper` reads the 5 status files and produces a single consolidated audit doc at `plugins/anvil/docs/audits/2026-05-26-plugin-audit.md` (a new `audits/` directory). The doc is severity-sorted, has per-critic detail sections, and tracks closure of MUST FIX items as the welder applies fixes.

Future audits land at `docs/audits/<YYYY-MM-DD>-plugin-audit.md` — date-stamped, never overwritten. Comparing two audit docs across time shows regression vs progress per critic.

---

## Critic personas

Each critic borrows the methodology of the equivalent `plugin-dev:` skill, then formats findings using the fakoli-crew severity rubric.

### `agent-critic`

**Scope:** all `*.md` files in `<plugin-root>/agents/`.

**Checks:**
- Frontmatter: `name`, `description`, `color`, `model`, `allowed-tools` present and valid
- `name` is unique within the plugin and matches the filename
- `color` does not collide with another agent in the same plugin
- `model` is one of the supported model IDs (`opus`, `sonnet`, `haiku`, or a full ID)
- `allowed-tools` is the tightest reasonable restriction (no Bash for pure-review agents, etc.)
- Description block contains exactly 3 `<example>` blocks with `<commentary>` rationale
- System prompt has clear scope boundaries (what the agent owns / what it defers to)
- Defer-to rules name real agents (no dangling references)
- File length proportionate to complexity (state-keeper at ~300 lines is the reference)

**Severity guidance:**
- MUST FIX: missing required frontmatter, color collision, dangling defer-to reference
- SHOULD FIX: fewer than 3 examples, missing commentary, defer rules unclear
- CONSIDER: file significantly shorter/longer than peer agents
- NIT: minor description prose issues

### `skill-critic`

**Scope:** all `SKILL.md` files in `<plugin-root>/skills/`.

**Checks:**
- Frontmatter: `name`, `description` (required)
- Description is concrete and triggering — names specific scenarios, not vague capability claims
- Body uses one-question-at-a-time discipline where appropriate
- Hard gates (`<HARD-GATE>` blocks) are present where decisions are irreversible
- Decision-flow diagram present for non-trivial skills (3+ steps)
- Lazy-loading discipline: skill body stays short; supporting material in `references/`, not inline
- References named in body actually exist on disk
- No fuzzy detection language ("if X seems available" — should be explicit shell check)

**Severity guidance:**
- MUST FIX: missing required frontmatter, dangling reference path, missing hard gate on irreversible action
- SHOULD FIX: fuzzy detection prose, missing decision diagram, body padded with reference-worthy content
- CONSIDER: description could trigger more reliably with examples
- NIT: prose style

### `hook-critic`

**Scope:** all `*.sh` files in `<plugin-root>/hooks/` + `hooks.json`.

**Checks:**
- Shebang is `#!/usr/bin/env bash` (portable)
- `${CLAUDE_PLUGIN_ROOT}` used for any plugin-internal path
- NO `set -e` (the hook contract is non-blocking; `set -e` shadows it)
- NO piped `grep` antipattern where structured tools exist (use `jq` for JSON)
- Stdin handling correct (read from stdin if PostToolUse, no stdin if SessionStart)
- Idempotent: re-running the hook produces the same effect
- Exit code semantics: 0 always (hooks don't block); errors go to stderr + log
- Performance: no slow subprocesses on hot events (PreToolUse fires per Edit/Write)
- `hooks.json` matcher patterns valid; events spelled correctly; commands point at real .sh files

**Severity guidance:**
- MUST FIX: `set -e` present, non-zero exit code, dangling matcher, performance regression on hot path
- SHOULD FIX: piped-grep antipattern, missing `${CLAUDE_PLUGIN_ROOT}`, missing portable shebang
- CONSIDER: hook does work that could be deferred to CLI
- NIT: log formatting

### `mcp-critic`

**Scope:** `.mcp.json` + every Python file backing an MCP tool (for anvil: `bin/src/anvil/mcp_server.py` + supporting modules).

**Checks:**
- `.mcp.json` schema valid (`mcpServers.<name>.type`, `command`, `args`)
- `${CLAUDE_PLUGIN_ROOT}` used in `args` so the server resolves regardless of install location
- Tool decorations (`@mcp.tool()`) have clear `description=` string telling the agent when to use the tool
- Every tool parameter has a typed annotation; no `Any` without justification
- Errors return structured payloads (`{"code", "message", "target_id?", ...}`) not raw exceptions or `repr()`
- No secrets logged or returned (audit prints, returned strings)
- stdio transport for local plugins; sse only when remote
- Tools that mutate state require explicit actor identification (claim ownership pattern)

**Severity guidance:**
- MUST FIX: tool returns raw `repr()` / unstructured error, secret leaked, missing actor guard on mutation
- SHOULD FIX: vague tool description, untyped parameter, missing `${CLAUDE_PLUGIN_ROOT}`
- CONSIDER: tool surface could be tightened or merged with sibling
- NIT: docstring prose

### `structure-critic`

**Scope:** `plugin.json`, `.claude-plugin/` directory contents, `README.md`, `CHANGELOG.md`, root marketplace.json entry, registry/*.json entries.

**Checks:**
- `plugin.json` all required fields (`name`, `version`, `description`, `author`, `repository`, `license`, `keywords`)
- Version syncs across 4 sources of truth: `plugin.json`, `pyproject.toml` (if Python), `__init__.py` (if Python), `marketplace.json`
- README has: title, badges, value proposition, install block, surface tables (agents/skills/hooks/commands count + names), config, requirements, author
- README agent table count matches `ls agents/ | wc -l`
- CHANGELOG follows Keep a Changelog format; `[Unreleased]` section empty after a tag
- `marketplace.json` plugin entry has same name, description, repository as plugin.json
- `registry/*.json` plugin entries reflect current version
- No dead files in `.claude-plugin/` or root (everything referenced by manifest or README)

**Severity guidance:**
- MUST FIX: version mismatch across sources, missing required plugin.json field, marketplace entry doesn't match
- SHOULD FIX: README counts stale, CHANGELOG `[Unreleased]` not empty after tag, dead file
- CONSIDER: badge set could be richer, surface tables could include trigger examples
- NIT: README polish

---

## Data flow (audit lifecycle)

```
User invokes: /fakoli-flow:execute plan-file=docs/plans/2026-05-26-phase-10-plugin-audit.md
        |
        v
Wave 1 (parallel × 5):
  smith → agent-critic.md
  smith → skill-critic.md
  smith → hook-critic.md
  smith → mcp-critic.md
  smith → structure-critic.md
        |
        v
Wave 1 verify: each critic .md frontmatter valid; allowed-tools tight; colors unique
        |
        v
Wave 2 (parallel × 5 — all 5 critics dispatched on anvil v1.9.0):
  agent-critic     → docs/plans/agent-agent-critic-status.md
  skill-critic     → docs/plans/agent-skill-critic-status.md
  hook-critic      → docs/plans/agent-hook-critic-status.md
  mcp-critic       → docs/plans/agent-mcp-critic-status.md
  structure-critic → docs/plans/agent-structure-critic-status.md
        |
        v
Wave 3 (keeper consolidation):
  keeper reads 5 status files → docs/audits/2026-05-26-plugin-audit.md
  Severity-sorted findings table; per-critic detail sections.
        |
        v
Wave 4 (welder fix-cycle for MUST FIX only):
  for each MUST FIX:
    welder applies patch
    relevant critic re-runs on patched file
    audit doc updated: → fixed in commit <sha>
  SHOULD FIX / CONSIDER / NIT logged to docs/phase-11-backlog.md (no action this phase)
        |
        v
Wave 5 (review):
  fakoli-crew:critic — review all Wave 4 fixes
  fakoli-crew:sentinel — full anvil pytest + acceptance scorecard
        |
        v
Wave 6 (release prep):
  keeper: anvil v1.10.0 version bump (4 sources)
  keeper: fakoli-crew v2.1.0 version bump (3 sources)
  keeper: CHANGELOG entries both plugins
  keeper: marketplace + registry regen
  keeper: root README updates (both plugin tables)
  keeper: fakoli-crew agent table grows 8 → 13 rows
```

---

## Error handling

- **A critic returns NEEDS_REVIEW (unexpected file shape, missing fixture, etc.):** surface to user immediately, do not proceed to Wave 3.
- **A critic finds zero issues:** valid state, not an error. Audit doc still gets generated showing zero findings under that critic.
- **A MUST FIX welder fix breaks a test:** sentinel catches it in Wave 5; welder loops back (max 3 iterations per finding per fakoli-flow:execute discipline).
- **A MUST FIX cannot be resolved in 3 fix-cycle iterations:** escalate to user with the critic's persistent finding; do NOT silently mark fixed.
- **Critic agent has dangling defer-to reference:** structure-critic catches this when auditing its sibling critics in a future audit; for THIS phase the smith who wrote them is responsible for accuracy.
- **fakoli-flow:execute itself fails to dispatch:** falls back to manual sequential dispatch (per the established skill graceful-degradation pattern).

---

## Testing

### Critic correctness tests

Each critic gets one smoke test in `plugins/fakoli-crew/tests/fixtures/audit-targets/`:

| Critic | Bad fixture | Expected verdict |
|---|---|---|
| agent-critic | `bad-agent.md` with no frontmatter | MUST FIX surfaced |
| skill-critic | `bad-skill.md` with vague description | SHOULD FIX surfaced |
| hook-critic | `bad-hook.sh` with `set -e` | MUST FIX surfaced |
| mcp-critic | `bad-mcp.json` with missing `args` | MUST FIX surfaced |
| structure-critic | `bad-plugin.json` missing `version` | MUST FIX surfaced |

Each fixture is paired with one assertion test that dispatches the critic against it and asserts the expected severity appears in the returned status file. Implementation: shell-based test (matches anvil's `test_hooks.sh` precedent) OR Python pytest using a recorded-agent test double.

### Phase-end regression

- `anvil` full pytest suite: 965+ passing (baseline) — any MUST FIX fix must not break.
- `fakoli-crew` plugin validation: `bash scripts/generate-index.sh --check` clean.
- 4-way version sync verified for anvil at v1.10.0; 3-way version sync for fakoli-crew at v2.1.0.

### What is NOT tested

- The critics' **judgment** — that's inherently subjective and gated by the meta-critic in Wave 5 (which is fakoli-crew:critic, the human-judgment senior reviewer).
- LLM behavior — the critics are deterministic prompt-driven agents; their output is whatever the model produces. Testing the prompts directly is the wrong layer.

---

## Acceptance criteria (sentinel scorecard rollup)

1. All 5 critics exist in `plugins/fakoli-crew/agents/` with valid frontmatter, unique colors (no collision with existing fakoli-crew critic/guido/herald/keeper/scout/sentinel/smith/welder), distinct `allowed-tools` sets matching the rubric.
2. All 5 critic smoke-test fixtures + tests exist and pass.
3. Audit doc `plugins/anvil/docs/audits/2026-05-26-plugin-audit.md` exists with a non-empty findings table and per-critic detail sections.
4. Every MUST FIX item from the audit has either `→ fixed in commit <sha>` OR `→ deferred with reason` annotation (no silent gaps).
5. anvil pytest suite still passes (target 965+).
6. New `plugins/anvil/docs/phase-11-backlog.md` exists and lists all deferred SHOULD FIX / CONSIDER / NIT items, cross-referenced by critic.
7. fakoli-crew CHANGELOG entry + version bump to v2.1.0 (minor bump for 5 new agents).
8. anvil CHANGELOG entry + version bump to v1.10.0; 4 sources of truth in sync (plugin.json, pyproject.toml, __init__.py, marketplace.json).
9. Root `.claude-plugin/marketplace.json` + `registry/*.json` regenerated.
10. `plugins/fakoli-crew/README.md` agent table grows from 8 → 13 rows.
11. `plugins/anvil/README.md` version badge + agent table accurate post-fix.

---

## Out of scope (deferred)

- A anvil skill that wraps the 5 critics into one command (e.g., `/anvil:audit`) — Phase 11.
- SHOULD FIX / CONSIDER / NIT items found by the audit — Phase 11 backlog.
- Audits of plugins other than anvil — out of scope; the critics are reusable, but this phase only runs them once on one plugin.
- Webhook-style auto-audit triggers (cron, post-commit hook, CI workflow) — Phase 12+.
- Linear/Monday/Jira/GitHub Projects sync providers (carry-forward from Phase 9 backlog) — separate phases.

---

## Risks

- **Critic prompt quality is unbounded.** A critic that's too aggressive flags MUST FIX on stylistic preference; one that's too lenient misses real bugs. Mitigation: each critic's prompt is reviewed by fakoli-crew:critic (meta-review) in Wave 5; smoke-test fixtures pin expected severity for at least one known-bad input per critic.
- **5 critics running in parallel could overwhelm the orchestrator.** Mitigation: all 5 are read-only file walks; their wall-clock is dominated by LLM latency, not contention. Wave 2 should complete in <5 minutes.
- **MUST FIX count could be large.** Phase 10 is bounded by "apply MUST FIX immediately"; if the critics surface 50+ MUST FIX items, Wave 4 becomes a multi-day fix marathon. Mitigation: at Wave 3 keeper consolidation, if MUST FIX count > 20, escalate to user with option to defer some MUST FIX items to Phase 11 (with explicit user approval annotation in the audit doc, never silent).
- **Critic placement in fakoli-crew expands its surface.** Mitigation: only 5 new files; fakoli-crew already has 8 agents; this is a 60% growth in count but each new agent is small (<300 lines) and tightly scoped.

---

## Open questions

None blocking. Proceed to `/flow:plan <this-spec-path>` after user approval.

---

## See also

- `plugins/anvil/docs/plans/2026-05-25-phase-9.md` — the immediately prior phase plan; same wave-engine pattern this phase uses.
- `plugins/anvil/docs/phase-9-backlog.md` — backlog of items deferred from Phase 9; some may also surface in this audit.
- `~/.claude/plugins/cache/.../plugin-dev/skills/agent-development/SKILL.md` (and siblings) — the canonical methodology the 5 critics each adapt.
- `plugins/fakoli-crew/agents/critic.md` — the existing senior reviewer; meta-reviews this phase's Wave 4 fixes.
