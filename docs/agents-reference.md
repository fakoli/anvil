# Agents reference

> anvil ships 5 plugin-owned agents. Each has a specific role and runs its full body in this session.

This document is the canonical per-agent reference. For the architectural role of agents inside the plugin, see [architecture.md](architecture.md).

---

## Quick lookup

| Agent | Color | Tools |
|---|---|---|
| [planner](#planner) | white | Read, Grep, Glob, Bash |
| [critic](#critic) | magenta | Read, Grep, Glob, Bash |
| [sentinel](#sentinel) | gray | Read, Grep, Glob, Bash |
| [state-keeper](#state-keeper) | teal | Read, Grep, Glob, Bash, Edit, Write |
| [docs-scribe](#docs-scribe) | purple | Read, Write, Edit, Glob, Grep |

Tool lists are read from each agent's frontmatter. The `state-keeper` agent declares `Edit` and `Write` but is restricted by its Iron Rule to writing only sync-report files under `.anvil/.sync-reports/` — never source files, state files, or git refs.

---

## Per-agent reference

### planner

**Purpose:** PRD-to-tasks decomposition. Reads `.anvil/prd.md`, proposes Features that group related Requirements, drafts Tasks with acceptance criteria and verification commands, and flags high-complexity tasks that should be expanded.

**Frontmatter:** `color: white` · `model: opus` · `tools: [Read, Grep, Glob, Bash]`

**When to dispatch:**
- After `anvil prd parse` and `anvil prd review --approve` — the first task graph needs to be generated.
- After new Requirements (e.g., R005-R008) are added to an existing PRD — the task graph needs incremental extension without losing claims on existing tasks.
- When a task scores `complexity >= 4` and the user wants suggested subtasks — feeds the `anvil expand` LLM-augmentation path.

**Iron Rule:** Never modifies `.anvil/state.db` or `.anvil/events.jsonl` directly. Proposes; the CLI commands (`plan`, `score`, `expand`) do the writes. Direct state-file edits bypass the audit log and break the replay guarantee.

**Output shape:** Markdown block with `## Features`, `## Tasks`, and `## Concerns` sections. The `anvil:plan` skill parses this output to drive the `anvil plan` CLI invocation.

**Source:** [`agents/planner.md`](../agents/planner.md)

**See also:** [authoring-a-prd.md](how-to/authoring-a-prd.md) · [cli-reference.md#plan](cli-reference.md#plan)

---

### critic

**Purpose:** Acceptance-criteria contract review. Reads the diff for a submitted task, compares it against the task's `acceptance_criteria` and `verification` fields, and returns a PASS / SHOULD FIX / MUST FIX verdict.

**Frontmatter:** `color: magenta` · `model: opus` · `tools: [Read, Grep, Glob, Bash]`

**When to dispatch:**
- After a claimed task has been submitted (status `needs_review`) and before `anvil apply --approve`.

**Iron Rule:** Never modifies any source file, test file, or state file. Reads, analyzes, and reports. If a bug is found, the fix is shown in the report — not applied. The welder agent or the CLI does all writes.

**Output shape:** Markdown report with an Acceptance Criteria table (each criterion marked SATISFIED or UNSATISFIED), Findings grouped by severity (MUST FIX / SHOULD FIX / CONSIDER / NIT), and a one-line Verdict.

**Verdict rules:**
- **MUST FIX** — any acceptance criterion unsatisfied, or any MUST FIX finding.
- **SHOULD FIX** — all criteria satisfied; SHOULD FIX findings remain.
- **PASS** — no findings at SHOULD FIX or above.

**Source:** [`agents/critic.md`](../agents/critic.md)

**See also:** [cli-reference.md#submit](cli-reference.md#submit)

---

### sentinel

**Purpose:** Evidence validation. Re-runs verification commands from the task spec, checks each acceptance criterion against fresh evidence, and returns a binary PASS / FAIL scorecard. Different from critic — sentinel validates that evidence proves the work was done; critic reviews whether the code is good.

**Frontmatter:** `color: gray` · `model: opus` · `tools: [Read, Grep, Glob, Bash]`

**When to dispatch:**
- After submission and before merge — the final gate that confirms the evidence actually demonstrates the acceptance criteria pass.

**Iron Rule:** Never modifies any source file, test file, state file, or evidence file. Reads, runs read-only commands, and reports. Every finding is binary — PASS or FAIL. Does not fix; does not suggest; only validates.

**Output shape:** Plain-text SENTINEL REPORT with one row per acceptance criterion (`[PASS]`, `[FAIL]`, or `[N/A ]`), one row per verification command, and a SUMMARY line ending in `READY` or `NOT READY`.

**Evidence standards:**
- **Counts as PASS:** exit code 0 from a fresh run; expected string present in command output you ran yourself; file exists at the expected path; exact test count matches.
- **Does NOT count:** "should work" reasoning; evidence from a stale buffer entry; a claimed fix without a re-run; partial output.
- **On conflict:** if a command that should PASS exits non-zero, do not retry — mark FAIL with verbatim error output.

**Source:** [`agents/sentinel.md`](../agents/sentinel.md)

**See also:** [cli-reference.md#submit](cli-reference.md#submit)

---

### state-keeper

**Purpose:** Sync reconciliation. Detects drift between anvil's three sources of truth — the SQLite canonical state, the project filesystem (packets, evidence buffer, worktrees), and git (branches, claims, commits). Returns a structured discrepancy report. Reports only — never remediates.

**Frontmatter:** `color: teal` · `model: opus` · `tools: [Read, Grep, Glob, Bash, Edit, Write]`

Edit and Write are scoped strictly to producing sync-report files under `.anvil/.sync-reports/` when the caller requests one. Source files, state files, evidence files, and git refs are never touched.

**When to dispatch:**
- Trigger phrases: "reconcile state", "sync drift", "check for orphans", "audit anvil".
- After a rebase, force-push, or manual filesystem cleanup that may have broken state-engine assumptions.
- When a claim is suspected stale (worktree gone, branch missing).
- When a task is marked synced (`external_id` present) but the `sync_mappings` row may never have landed.
- As the scan phase of `anvil sync` (no `--fix`).

**Iron Rule:** Never auto-remediates. Never deletes branches, worktrees, packets, evidence files, state rows, or events. Never runs destructive git operations (`git branch -D`, `git worktree remove`, `git push --force`, etc.). Sole output is a discrepancy report; remediation is the user's explicit choice via `anvil sync --fix --yes`.

**The four reconciliation checks:**
1. **Orphan branches** — git branch whose embedded task ID is not present in `anvil list --status all`.
2. **Orphan packets** — packet directory under `packets/` with no matching task in SQLite.
3. **Stale claims** — claim row in SQLite with no matching worktree at the expected path.
4. **Missing sync_mappings** — task with sync evidence (events log shows `sync.pushed`) but no row in `sync_mappings`.

**Output shape:** Markdown report with Summary counts, one section per check kind (each with a table of discrepancies + suggested fixes), and a Verdict of `CLEAN` or `DRIFT`.

**Source:** [`agents/state-keeper.md`](../agents/state-keeper.md)

**See also:** [cli-reference.md#sync](cli-reference.md#sync) · [architecture.md → Per-layer responsibilities (Sync engine row)](architecture.md#per-layer-responsibilities)

---

### docs-scribe

**Purpose:** Inward-facing documentation maintenance. Owns the `docs/` folder (specs, runbooks, design notes, plan archives), the plugin's CHANGELOG, and the `description` field of `plugin.json`. Audits cross-references — broken wikilinks, mismatched section anchors, dangling `see also` pointers, references to files that moved or were archived.

**Frontmatter:** `color: purple` · `model: opus` · `tools: [Read, Write, Edit, Glob, Grep]`

**When to dispatch:**
- Trigger phrases: "update anvil docs", "fix broken links", "write the changelog", "doc cross-reference audit", "after-phase docs sweep".
- After a schema change (migration, model class change, column added or removed).
- After a new CLI command or subcommand ships.
- After a new agent is added.
- After a phase in `docs/plans/` is marked COMPLETE.
- When broken links, dangling anchors, or stale `see also` pointers are reported.

**Iron Rule:** Never edits a doc without first reading the source of truth it is supposed to describe. If a spec describes the schema, read the schema. If a runbook describes a CLI command, read the CLI source. Docs that lie are worse than no docs at all.

**What it owns:**
- `docs/**/*.md` — all inward-facing docs.
- `docs/plans/` — phase plans and agent status archives.
- `CHANGELOG.md` — append-only ledger of user-visible changes.
- `.claude-plugin/plugin.json` (`description` field only).

**What it does NOT own:**
- `plugin.json`'s structural fields (`name`, `version`, `author`, `repository`, `license`, `keywords`) — those are smith's lane.
- Agent or skill internals — those agents/skills speak for themselves.

**CHANGELOG discipline:**
- Append-only. Never rewrite history; add a correction entry instead.
- Every entry dated (UTC) and tagged with the version it shipped in.
- Group under standard headings: Added, Changed, Deprecated, Removed, Fixed, Security.
- Link to the relevant phase plan or spec section for non-trivial changes.

**Output shape:** Markdown sweep report with Source of Truth Read section, Cross-Reference Audit table, Doc-vs-Source Drift section (one subsection per drifted doc), CHANGELOG entry summary, plugin.json description before/after, and a Verdict of `IN SYNC`, `APPLIED`, or `OPEN QUESTIONS`.

**Source:** [`agents/docs-scribe.md`](../agents/docs-scribe.md)

---

## See also

- [Architecture](architecture.md) — where agents sit in the component graph
- [Skills reference](skills-reference.md) — the seven plugin-owned skills
- [CLI reference](cli-reference.md) — every command an agent might invoke
- [Authoring a PRD](how-to/authoring-a-prd.md) — the upstream input that planner consumes
