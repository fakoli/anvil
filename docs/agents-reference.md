# Agents reference

> fakoli-state ships 6 plugin-owned agents. Each has a specific role; each defers to a `fakoli-crew` specialist when that plugin is installed (so the same agent works standalone or as part of a richer crew composition).

This document is the canonical per-agent reference. For the higher-level integration picture across all three plugins, see [Integrating with fakoli-flow and fakoli-crew](how-to/integrating-with-fakoli-flow-and-crew.md). For the architectural role of agents inside the plugin, see [architecture.md](architecture.md).

---

## Quick lookup

| Agent | Color | Tools | Defers to (when fakoli-crew installed) |
|---|---|---|---|
| [planner](#planner) | white | Read, Grep, Glob, Bash | `fakoli-crew:guido` (HOW only — architecture) |
| [critic](#critic) | magenta | Read, Grep, Glob, Bash | `fakoli-crew:critic` (full fallback) |
| [sentinel](#sentinel) | gray | Read, Grep, Glob, Bash | `fakoli-crew:sentinel` (full fallback) |
| [state-keeper](#state-keeper) | teal | Read, Grep, Glob, Bash, Edit, Write | `fakoli-crew:keeper` (repo-wide scope only) |
| [marketplace-scribe](#marketplace-scribe) | cyan | Read, Write, Edit, Bash, Glob, Grep | `fakoli-crew:keeper` (whole-file regen only) |
| [docs-scribe](#docs-scribe) | purple | Read, Write, Edit, Glob, Grep | `fakoli-crew:herald` (outward docs only) |

Tool lists are read from each agent's frontmatter. The `state-keeper` agent declares `Edit` and `Write` but is restricted by its Iron Rule to writing only sync-report files under `.fakoli-state/.sync-reports/` — never source files, state files, or git refs.

---

## Per-agent reference

### planner

**Purpose:** PRD-to-tasks decomposition. Reads `.fakoli-state/prd.md`, proposes Features that group related Requirements, drafts Tasks with acceptance criteria and verification commands, and flags high-complexity tasks that should be expanded.

**Frontmatter:** `color: white` · `model: opus` · `tools: [Read, Grep, Glob, Bash]`

**When to dispatch:**
- After `fakoli-state prd parse` and `fakoli-state prd review --approve` — the first task graph needs to be generated.
- After new Requirements (e.g., R005-R008) are added to an existing PRD — the task graph needs incremental extension without losing claims on existing tasks.
- When a task scores `complexity >= 4` and the user wants suggested subtasks — feeds the `fakoli-state expand` LLM-augmentation path.

**Iron Rule:** Never modifies `.fakoli-state/state.db` or `.fakoli-state/events.jsonl` directly. Proposes; the CLI commands (`plan`, `score`, `expand`) do the writes. Direct state-file edits bypass the audit log and break the replay guarantee.

**Defer behavior:** Partial. When `fakoli-crew:guido` is available, planner keeps the WHAT (task structure, dependencies, scoring) but defers the HOW (interface design, type system choices, project structure) — it flags those as "guido consult" entries in the Concerns section of its output. Planner never delegates the whole proposal; it always returns the structured Features/Tasks/Concerns block.

**Output shape:** Markdown block with `## Features`, `## Tasks`, and `## Concerns` sections. The `fakoli-state:plan` skill parses this output to drive the `fakoli-state plan` CLI invocation.

**Source:** [`agents/planner.md`](../agents/planner.md)

**See also:** [authoring-a-prd.md](how-to/authoring-a-prd.md) · [cli-reference.md#plan](cli-reference.md#plan)

---

### critic

**Purpose:** Acceptance-criteria contract review. Reads the diff for a submitted task, compares it against the task's `acceptance_criteria` and `verification` fields, and returns a PASS / SHOULD FIX / MUST FIX verdict.

**Frontmatter:** `color: magenta` · `model: opus` · `tools: [Read, Grep, Glob, Bash]`

**When to dispatch:**
- After a claimed task has been submitted (status `needs_review`) and before `fakoli-state apply --approve`.
- Inside `/flow:execute` as the critic gate that runs after every wave that writes code.

**Iron Rule:** Never modifies any source file, test file, or state file. Reads, analyzes, and reports. If a bug is found, the fix is shown in the report — not applied. The welder agent or the CLI does all writes.

**Defer behavior:** Full fallback. When `fakoli-crew:critic` is installed, the crew agent takes precedence — it carries language-specific expertise (Python type annotations, TypeScript strictness, Rust lifetimes) that this fallback does not replicate at full depth. The plugin-owned critic remains responsible for acceptance-criteria contract checks; the two can run together and merge verdicts.

**Output shape:** Markdown report with an Acceptance Criteria table (each criterion marked SATISFIED or UNSATISFIED), Findings grouped by severity (MUST FIX / SHOULD FIX / CONSIDER / NIT), and a one-line Verdict.

**Verdict rules:**
- **MUST FIX** — any acceptance criterion unsatisfied, or any MUST FIX finding.
- **SHOULD FIX** — all criteria satisfied; SHOULD FIX findings remain.
- **PASS** — no findings at SHOULD FIX or above.

**Source:** [`agents/critic.md`](../agents/critic.md)

**See also:** [integrating-with-fakoli-flow-and-crew.md → Example 1](how-to/integrating-with-fakoli-flow-and-crew.md#example-1-flowexecute-consumes-fakoli-state-next--claim--submit) · [cli-reference.md#submit](cli-reference.md#submit)

---

### sentinel

**Purpose:** Evidence validation. Re-runs verification commands from the task spec, checks each acceptance criterion against fresh evidence, and returns a binary PASS / FAIL scorecard. Different from critic — sentinel validates that evidence proves the work was done; critic reviews whether the code is good.

**Frontmatter:** `color: gray` · `model: opus` · `tools: [Read, Grep, Glob, Bash]`

**When to dispatch:**
- After submission and before merge — the final gate that confirms the evidence actually demonstrates the acceptance criteria pass.
- Inside `/flow:verify` for the final evidence-validation step before `/flow:finish` calls `fakoli-state apply --approve`.

**Iron Rule:** Never modifies any source file, test file, state file, or evidence file. Reads, runs read-only commands, and reports. Every finding is binary — PASS or FAIL. Does not fix; does not suggest; only validates.

**Defer behavior:** Full fallback. When `fakoli-crew:sentinel` is installed, the crew agent takes precedence — it has broader validation depth (CI workflow checks, version sync, comprehensive linting) than this fallback. The plugin-owned sentinel remains responsible for re-running task-spec verification commands; the two can run together and merge scorecards for maximum coverage.

**Output shape:** Plain-text SENTINEL REPORT with one row per acceptance criterion (`[PASS]`, `[FAIL]`, or `[N/A ]`), one row per verification command, and a SUMMARY line ending in `READY` or `NOT READY`.

**Evidence standards:**
- **Counts as PASS:** exit code 0 from a fresh run; expected string present in command output you ran yourself; file exists at the expected path; exact test count matches.
- **Does NOT count:** "should work" reasoning; evidence from a stale buffer entry; a claimed fix without a re-run; partial output.
- **On conflict:** if a command that should PASS exits non-zero, do not retry — mark FAIL with verbatim error output.

**Source:** [`agents/sentinel.md`](../agents/sentinel.md)

**See also:** [integrating-with-fakoli-flow-and-crew.md → Example 1](how-to/integrating-with-fakoli-flow-and-crew.md#example-1-flowexecute-consumes-fakoli-state-next--claim--submit) · [cli-reference.md#submit](cli-reference.md#submit)

---

### state-keeper

**Purpose:** Sync reconciliation. Detects drift between fakoli-state's three sources of truth — the SQLite canonical state, the project filesystem (packets, evidence buffer, worktrees), and git (branches, claims, commits). Returns a structured discrepancy report. Reports only — never remediates.

**Frontmatter:** `color: teal` · `model: opus` · `tools: [Read, Grep, Glob, Bash, Edit, Write]`

Edit and Write are scoped strictly to producing sync-report files under `.fakoli-state/.sync-reports/` when the caller requests one. Source files, state files, evidence files, and git refs are never touched.

**When to dispatch:**
- Trigger phrases: "reconcile state", "sync drift", "check for orphans", "audit fakoli-state".
- After a rebase, force-push, or manual filesystem cleanup that may have broken state-engine assumptions.
- When a claim is suspected stale (worktree gone, branch missing).
- When a task is marked synced (`external_id` present) but the `sync_mappings` row may never have landed.
- As the scan phase of `fakoli-state sync` (no `--fix`).

**Iron Rule:** Never auto-remediates. Never deletes branches, worktrees, packets, evidence files, state rows, or events. Never runs destructive git operations (`git branch -D`, `git worktree remove`, `git push --force`, etc.). Sole output is a discrepancy report; remediation is the user's explicit choice via `fakoli-state sync --fix --yes`.

**Defer behavior:** Scope-split. When `fakoli-crew:keeper` is installed, the two have non-overlapping scopes:
- Route to `fakoli-crew:keeper` for cross-plugin sync, CI workflow drift, contributor docs, multi-plugin registry/marketplace regen.
- Route to `fakoli-state:state-keeper` for orphan branches in one project, orphan packets, stale claims, missing `sync_mappings`, audit-log spot-checks.

Both can fire in parallel when a question touches both scopes.

**The four reconciliation checks:**
1. **Orphan branches** — git branch whose embedded task ID is not present in `fakoli-state list --status all`.
2. **Orphan packets** — packet directory under `packets/` with no matching task in SQLite.
3. **Stale claims** — claim row in SQLite with no matching worktree at the expected path.
4. **Missing sync_mappings** — task with sync evidence (events log shows `sync.pushed`) but no row in `sync_mappings`.

**Output shape:** Markdown report with Summary counts, one section per check kind (each with a table of discrepancies + suggested fixes), and a Verdict of `CLEAN` or `DRIFT`.

**Source:** [`agents/state-keeper.md`](../agents/state-keeper.md)

**See also:** [cli-reference.md#sync](cli-reference.md#sync) · [architecture.md → Per-layer responsibilities (Sync engine row)](architecture.md#per-layer-responsibilities)

---

### marketplace-scribe

**Purpose:** Marketplace artifact maintenance. Keeps the public-facing listing of fakoli-state honest and current — the marketplace entry, the registry index row, and the root README plugins table. Fires after any version bump, agent add/remove, skill add/remove, or CLI surface change.

**Frontmatter:** `color: cyan` · `model: opus` · `tools: [Read, Write, Edit, Bash, Glob, Grep]`

**When to dispatch:**
- Trigger phrases: "update marketplace", "regen registry", "sync plugins table", "marketplace entry drift".
- After `plugin.json`'s `version` field changes.
- After a new file appears in (or disappears from) `agents/` or `skills/`.
- After a new `fakoli-state <subcommand>` ships or an existing one is renamed.
- When a user, sentinel, or state-keeper notices the registry and the filesystem disagree.

**Iron Rule:** Never invents a capability the plugin does not actually ship. Every agent, skill, command, or CLI subcommand listed in marketplace.json or the README must correspond to a file read in this session. Drift in the other direction — derived artifact ahead of source — destroys trust with first-time visitors.

**What it owns:**
- `.claude-plugin/marketplace.json` — the fakoli-state row only.
- `README.md` (root) plugins table — the fakoli-state row only.
- `registry/*.json` — the fakoli-state entry across every registry file that indexes it.

**What it does NOT own:**
- `plugins/fakoli-state/.claude-plugin/plugin.json` (description belongs to docs-scribe; structural fields belong to smith).
- Repo-wide `CLAUDE.md`, `.github/workflows/`, `docs/contributing.md` (belong to `fakoli-crew:keeper`).
- Other plugins' marketplace rows or registry entries (also `fakoli-crew:keeper`).
- Anything under `plugins/fakoli-state/docs/` or the plugin's CHANGELOG (belong to docs-scribe).

**Defer behavior:** Scope-split. When `fakoli-crew:keeper` is installed:
- Route to `marketplace-scribe` for fakoli-state's row in marketplace.json, registry, README plugins table.
- Route to `fakoli-crew:keeper` for whole-file regeneration, repo-wide README restructuring, CI workflow paths, registry schema changes affecting every plugin.

When in doubt — if a request seems to cross plugin boundaries — prefer `fakoli-crew:keeper`.

**Output shape:** Markdown report with Source of Truth section (from plugin.json + filesystem), Artifact Changes section (one before/after table per artifact), and a Verdict of `IN SYNC`, `APPLIED`, or `DRIFT REMAINING`.

**Source:** [`agents/marketplace-scribe.md`](../agents/marketplace-scribe.md)

---

### docs-scribe

**Purpose:** Inward-facing documentation maintenance. Owns the `docs/` folder (specs, runbooks, design notes, plan archives), the plugin's CHANGELOG, and the `description` field of `plugin.json`. Audits cross-references — broken wikilinks, mismatched section anchors, dangling `see also` pointers, references to files that moved or were archived.

**Frontmatter:** `color: purple` · `model: opus` · `tools: [Read, Write, Edit, Glob, Grep]`

**When to dispatch:**
- Trigger phrases: "update fakoli-state docs", "fix broken links", "write the changelog", "doc cross-reference audit", "after-phase docs sweep".
- After a schema change (migration, model class change, column added or removed).
- After a new CLI command or subcommand ships.
- After a new agent is added.
- After a phase in `docs/plans/` is marked COMPLETE.
- When broken links, dangling anchors, or stale `see also` pointers are reported.

**Iron Rule:** Never edits a doc without first reading the source of truth it is supposed to describe. If a spec describes the schema, read the schema. If a runbook describes a CLI command, read the CLI source. Docs that lie are worse than no docs at all.

**What it owns:**
- `plugins/fakoli-state/docs/**/*.md` — all inward-facing docs.
- `plugins/fakoli-state/docs/plans/` — phase plans and agent status archives.
- `plugins/fakoli-state/CHANGELOG.md` — append-only ledger of user-visible changes.
- `plugins/fakoli-state/.claude-plugin/plugin.json` (`description` field only).

**What it does NOT own:**
- `.claude-plugin/marketplace.json`, the root `README.md`, `registry/*.json` (belong to marketplace-scribe).
- Repo-wide `CLAUDE.md`, contributor docs, CI workflow docs (belong to `fakoli-crew:keeper`).
- `plugin.json`'s structural fields (`name`, `version`, `author`, `repository`, `license`, `keywords`) — those are smith's lane.
- Agent or skill internals — those agents/skills speak for themselves.

**Defer behavior:** Scope-split. When `fakoli-crew:herald` is installed:
- Route to `fakoli-crew:herald` for root README, marketplace listing prose, badges, value-proposition rewrites for first-time visitors.
- Route to `docs-scribe` for anything inside `plugins/fakoli-state/docs/`, the plugin's CHANGELOG, the plugin.json description field.

The split-by-audience is deliberate: marketplace-scribe writes for strangers, docs-scribe writes for contributors, state-keeper writes for operators.

**CHANGELOG discipline:**
- Append-only. Never rewrite history; add a correction entry instead.
- Every entry dated (UTC) and tagged with the version it shipped in.
- Group under standard headings: Added, Changed, Deprecated, Removed, Fixed, Security.
- Link to the relevant phase plan or spec section for non-trivial changes.

**Output shape:** Markdown sweep report with Source of Truth Read section, Cross-Reference Audit table, Doc-vs-Source Drift section (one subsection per drifted doc), CHANGELOG entry summary, plugin.json description before/after, and a Verdict of `IN SYNC`, `APPLIED`, or `OPEN QUESTIONS`.

**Source:** [`agents/docs-scribe.md`](../agents/docs-scribe.md)

---

## Defer-to-crew pattern explained

Every plugin-owned agent body starts with a detection step that runs the same shell check:

```bash
claude plugin list 2>/dev/null | grep -q "fakoli-crew"
```

- The `2>/dev/null` suppresses stderr when `claude` is not on `PATH` (e.g., in some MCP server contexts).
- The grep pattern is unanchored. `claude plugin list` renders each row as `  ❯ fakoli-crew@fakoli-plugins` (indented marker line, then `<plugin>@<source>` slug); a `^` anchor would never match. The unanchored substring match is safe because no other installed plugin contains the string `fakoli-crew`.
- The exit code is the contract — no JSON parsing, no `/help` introspection.

Exit code 0 means a crew specialist is available and the agent defers (full fallback) or scope-splits (partial defer). Non-zero means the agent runs its plugin-local body in full. This makes the integration zero-config: install fakoli-crew and the deferral activates; uninstall and the local body runs. No settings.json toggles, no per-task overrides.

Two of the six agents (`critic`, `sentinel`) are full fallbacks — when the crew sibling exists, the plugin-owned agent steps aside entirely. The other four (`planner`, `state-keeper`, `marketplace-scribe`, `docs-scribe`) are scope-splits — both run, but at different levels of granularity. The split-by-scope pattern lets each plugin own a tightly defined surface without the two agents fighting over the same files.

---

## Standalone mode

If only fakoli-state is installed, all 6 agents run their full local body. No degradation in capability — the deferral is an optimization (using a more specialized crew agent), not a requirement. The plugin-owned `critic` still produces PASS / SHOULD FIX / MUST FIX verdicts against acceptance criteria. The plugin-owned `sentinel` still re-runs verification commands and produces the binary scorecard. `planner` still proposes Features and Tasks; `state-keeper` still detects the four discrepancy kinds; `marketplace-scribe` still keeps the fakoli-state row honest; `docs-scribe` still sweeps the inward docs and CHANGELOG.

This is the v0 wedge: a solo developer with one Claude Code session can drive the full PRD-to-shipped lifecycle — and the full doc-and-state maintenance lifecycle — without ever installing fakoli-flow or fakoli-crew.

---

## See also

- [Integrating with fakoli-flow and fakoli-crew](how-to/integrating-with-fakoli-flow-and-crew.md) — the canonical answer to "what happens when I install all three?"
- [Architecture](architecture.md) — the plugin trinity diagram and where agents sit in the component graph
- [Skills reference](skills-reference.md) — the seven plugin-owned skills and their bridge points
- [CLI reference](cli-reference.md) — every command an agent might invoke
- [Authoring a PRD](how-to/authoring-a-prd.md) — the upstream input that planner consumes
