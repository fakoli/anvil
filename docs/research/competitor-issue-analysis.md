# Competitor Issue Analysis: Spec-Kit, Task-Master, BMAD, Spec-Workflow

**Method:** We ranked the 81 highest-engagement open issues across four spec-driven / task-management tools for AI coding agents — `github/spec-kit`, `eyaltoledano/claude-task-master`, `bmad-code-org/BMAD-METHOD`, and `Pimzino/spec-workflow-mcp` — ordered by reaction count, then categorized each by core problem, sentiment, severity, and relevance to fakoli-state. This document synthesizes the recurring themes, the failure modes shared across all four tools, and the concrete implications for fakoli-state's roadmap and positioning. It is deliberately honest about where fakoli-state already wins, where it has real gaps, and where the issue is simply out of scope.

---

## Themes

### 1. Durable State vs. Regenerate-From-Template (the core wedge)

**Summary.** Across all four competitors, the single most painful structural flaw is that state lives in flat markdown/JSON that gets regenerated from templates on every run, so edits are lossy and clobbering. Re-running `specify init` overwrites user-customized files ([spec-kit#916](https://github.com/github/spec-kit/issues/916)), re-planning after a spec edit destroys prior plan work ([spec-kit#1059](https://github.com/github/spec-kit/issues/1059)), feature-add #002 re-derives the whole app instead of reconciling against #001 ([spec-kit#668](https://github.com/github/spec-kit/issues/668)), and per-feature specs go stale after merge with no living module-level record ([spec-kit#1100](https://github.com/github/spec-kit/issues/1100)). Task-Master users want non-chat structured memory / knowledge-graph state ([task-master#502](https://github.com/eyaltoledano/claude-task-master/issues/502)). The shared root cause is the absence of an append-only, in-place-mutated store of record.

- **Issue count:** 7
- **Dominant sentiment:** frustrated
- **Opportunity size:** large
- **Examples:** [spec-kit#916](https://github.com/github/spec-kit/issues/916) · [spec-kit#1059](https://github.com/github/spec-kit/issues/1059) · [spec-kit#668](https://github.com/github/spec-kit/issues/668) · [spec-kit#1100](https://github.com/github/spec-kit/issues/1100) · [task-master#502](https://github.com/eyaltoledano/claude-task-master/issues/502) · [spec-kit#609](https://github.com/github/spec-kit/issues/609) · [BMAD#1808](https://github.com/bmad-code-org/BMAD-METHOD/issues/1808)

**Fakoli implication.** This IS fakoli-state's thesis and its strongest differentiator: a durable SQLite store where features/tasks/claims/evidence are additive rows updated in place (status transitions, re-scoring, dependency edits) rather than templates regenerated. Lead every positioning doc with "edits are recorded transitions, never overwrites." The append-only event log + queryable-after-merge state directly answer drift, lossy re-planning, and living-architecture-knowledge. No competitor can match this without abandoning their file-based model.

---

### 2. Evidence-Gated Completion & Immutable Audit Trail

**Summary.** A distinct, high-severity cluster about trust in "done": [task-master#181](https://github.com/eyaltoledano/claude-task-master/issues/181) is the flagship — manual checkbox tracking is high-friction AND agents lie about completion, so users add separate verify tasks to catch false "done" claims. BMAD's correct-course silently rewrites already-accepted stories with no status gate ([BMAD#1930](https://github.com/bmad-code-org/BMAD-METHOD/issues/1930)), Quick Dev auto-commits/pushes without explicit intent ([BMAD#2178](https://github.com/bmad-code-org/BMAD-METHOD/issues/2178)), and deferred code-review items are write-only and never read back into planning ([BMAD#2199](https://github.com/bmad-code-org/BMAD-METHOD/issues/2199)). The unifying gap: no enforced status lifecycle, no proof attached to completion, no immutability for accepted work.

- **Issue count:** 5
- **Dominant sentiment:** frustrated
- **Opportunity size:** large
- **Examples:** [task-master#181](https://github.com/eyaltoledano/claude-task-master/issues/181) · [BMAD#1930](https://github.com/bmad-code-org/BMAD-METHOD/issues/1930) · [BMAD#2178](https://github.com/bmad-code-org/BMAD-METHOD/issues/2178) · [BMAD#2199](https://github.com/bmad-code-org/BMAD-METHOD/issues/2199) · [BMAD#2135](https://github.com/bmad-code-org/BMAD-METHOD/issues/2135)

**Fakoli implication.** Differentially strong. fakoli-state's DB-enforced status lifecycle + hook-captured evidence + human apply gate (`apply_review_decision`) means completion is a recorded transition with proof, not an agent's self-report — and accepted tasks are immutable historical records. The apply gate answers the auto-commit fear (#2178), the status machine refuses edits to accepted tasks (#1930), and queryable evidence/decision records close the write-only deferred-work loop (#2199). The "agents lie about done" pain (#181) is the single most viscerally-felt issue in the dataset and fakoli-state's cleanest win.

---

### 3. Multi-Agent & Parallel-Work Coordination (leases)

**Summary.** Tools assume a single foreground agent on one feature at a time and have no primitive for parallel/async work. Spec-kit can't hand off to a background agent ([spec-kit#1017](https://github.com/github/spec-kit/issues/1017)) or delegate to subagents with isolated context ([spec-kit#752](https://github.com/github/spec-kit/issues/752)). Task-Master's local `tasks.json` causes merge conflicts the moment multiple people/branches are involved, and users want a Solo/Multiplayer split ([task-master#91](https://github.com/eyaltoledano/claude-task-master/issues/91), [task-master#11](https://github.com/eyaltoledano/claude-task-master/issues/11)). BMAD lacks a portable orchestrator-to-subagent delegation model across its 20+ platforms ([BMAD#1809](https://github.com/bmad-code-org/BMAD-METHOD/issues/1809), [BMAD#1898](https://github.com/bmad-code-org/BMAD-METHOD/issues/1898)) and wants a unified task-to-implementation handoff ([BMAD#813](https://github.com/bmad-code-org/BMAD-METHOD/issues/813)). Users want a done-response that names the next task to keep agents moving ([task-master#235](https://github.com/eyaltoledano/claude-task-master/issues/235)).

- **Issue count:** 8
- **Dominant sentiment:** feature_request
- **Opportunity size:** large
- **Examples:** [task-master#91](https://github.com/eyaltoledano/claude-task-master/issues/91) · [task-master#11](https://github.com/eyaltoledano/claude-task-master/issues/11) · [spec-kit#1017](https://github.com/github/spec-kit/issues/1017) · [spec-kit#752](https://github.com/github/spec-kit/issues/752) · [BMAD#1809](https://github.com/bmad-code-org/BMAD-METHOD/issues/1809) · [BMAD#813](https://github.com/bmad-code-org/BMAD-METHOD/issues/813) · [task-master#235](https://github.com/eyaltoledano/claude-task-master/issues/235) · [BMAD#1898](https://github.com/bmad-code-org/BMAD-METHOD/issues/1898)

**Fakoli implication.** Differentially strong. fakoli-state's exclusive lease-based claims with heartbeats are purpose-built for multiple agents pulling tasks in parallel — the missing "hand-off/parallel agents" primitive. Frame durable claims + work packets as a portable, framework-agnostic delegation substrate (answers #1809/#813) that needs no per-platform spawn primitive, and as the always-available task-tracking capability layer (#1898). DB-backed claims eliminate the `tasks.json` merge conflict (#91/#11) by construction. Ensure finish/evidence responses explicitly name the next ready task (#235). **Caveat:** the lease primitive is only a differentiator if it is provably correct under concurrency — see the engine-reliability backlog.

---

### 4. Context-Frugal Scoped Retrieval (never dump the whole graph)

**Summary.** Two failure modes converge: (1) always-on context tax — spec-kit installs ~18.6k tokens of slash commands that load every session whether used or not ([spec-kit#1401](https://github.com/github/spec-kit/issues/1401)), and slash commands dump whole spec files into the main thread saturating context ([spec-kit#752](https://github.com/github/spec-kit/issues/752)); (2) scale-blocking retrieval — Task-Master's `get_tasks` returns a 41k-token blob at ~85 tasks, exceeding the 25k MCP ceiling and hard-blocking "start next task" ([task-master#1137](https://github.com/eyaltoledano/claude-task-master/issues/1137)). tinySpec ([spec-kit#1174](https://github.com/github/spec-kit/issues/1174)) is the same disease: a fixed workflow generating 30 files / 35 tasks for a one-button change.

- **Issue count:** 4
- **Dominant sentiment:** blocked
- **Opportunity size:** medium
- **Examples:** [task-master#1137](https://github.com/eyaltoledano/claude-task-master/issues/1137) · [spec-kit#1401](https://github.com/github/spec-kit/issues/1401) · [spec-kit#752](https://github.com/github/spec-kit/issues/752) · [spec-kit#1174](https://github.com/github/spec-kit/issues/1174)

**Fakoli implication.** Differentially strong but requires self-audit. fakoli-state pushes workflow logic into an external SQLite engine reached via a thin CLI/MCP surface, with SQL-backed `list_tasks`/`get_next_task` that paginate server-side and work packets that render ONE task at a time — structurally immune to the dump-everything failure (#1137). Make "scoped retrieval, never the whole graph" an explicit selling point. Use six-dimension scoring (complexity, blast radius) to route trivial tasks to a lightweight path (#1174). **CRITICAL caveat:** audit fakoli-state's OWN skill/command token footprint to ensure it actually delivers the context-frugal promise (#1401) rather than reproducing the tax.

---

### 5. Runtime / Editor Neutrality (lean-on strength)

**Summary.** The largest raw volume of issues is the per-editor/per-agent integration treadmill: spec-kit needs bespoke wiring for Cline ([#524](https://github.com/github/spec-kit/issues/524)), Zed ([#571](https://github.com/github/spec-kit/issues/571), [#580](https://github.com/github/spec-kit/issues/580)), Aider ([#396](https://github.com/github/spec-kit/issues/396)), codex-cli prefix churn ([#854](https://github.com/github/spec-kit/issues/854)); fish-shell breaks the bash-only core ([#685](https://github.com/github/spec-kit/issues/685)); Task-Master gets repeated per-IDE asks (Windsurf [#452](https://github.com/eyaltoledano/claude-task-master/issues/452), Cursor, etc.). State that only survives inside a single open thread forces full re-setup on a new thread ([#580](https://github.com/github/spec-kit/issues/580)). Maintainers repeatedly flag these as out-of-scope maintenance tax.

- **Issue count:** 8
- **Dominant sentiment:** feature_request
- **Opportunity size:** medium
- **Examples:** [spec-kit#524](https://github.com/github/spec-kit/issues/524) · [spec-kit#580](https://github.com/github/spec-kit/issues/580) · [spec-kit#685](https://github.com/github/spec-kit/issues/685) · [task-master#452](https://github.com/eyaltoledano/claude-task-master/issues/452) · [spec-kit#854](https://github.com/github/spec-kit/issues/854) · [spec-kit#571](https://github.com/github/spec-kit/issues/571) · [spec-kit#396](https://github.com/github/spec-kit/issues/396) · [BMAD#1956](https://github.com/bmad-code-org/BMAD-METHOD/issues/1956)

**Fakoli implication.** Lean-on strength, not a gap. fakoli-state's CLI + FastMCP-stdio runs in any MCP/ACP-compatible client with no template renaming — runtime-neutrality is the structural answer to this entire class of "support tool X" asks, and durable out-of-thread state directly fixes "state dies when the thread/agent changes" (#580). Position vendor-neutrality against the per-editor treadmill rather than chasing each integration. Keep shell glue thin (validates the no-bash-core bet, #685). **Minor gap:** version-pin and self-describe MCP/CLI command names so host-agent upgrades don't silently break invocation hints (#854).

---

### 6. Brownfield / Existing-Codebase Onboarding (gap to fill)

**Summary.** Every tool optimizes for greenfield and falls down on existing code. Spec-kit explicitly covers only ~25% of real work — the other 75% (bugfix, modify, refactor, emergency prod fixes) is ad-hoc ([spec-kit#712](https://github.com/github/spec-kit/issues/712)); users want to reverse a repo back into PRD/artifacts ([spec-kit#404](https://github.com/github/spec-kit/issues/404)). Task-Master's #1 onboarding ask (34 reactions) is an intelligent recursive scan that summarizes an existing codebase and persists a re-scannable model to plan against the delta ([task-master#78](https://github.com/eyaltoledano/claude-task-master/issues/78)). Brownfield spec drift ([spec-kit#916](https://github.com/github/spec-kit/issues/916)) and module-level living specs ([spec-kit#1100](https://github.com/github/spec-kit/issues/1100)) reinforce this.

- **Issue count:** 4
- **Dominant sentiment:** feature_request
- **Opportunity size:** large
- **Examples:** [task-master#78](https://github.com/eyaltoledano/claude-task-master/issues/78) · [spec-kit#712](https://github.com/github/spec-kit/issues/712) · [spec-kit#404](https://github.com/github/spec-kit/issues/404) · [spec-kit#916](https://github.com/github/spec-kit/issues/916)

**Fakoli implication.** Real gap worth a backlog item where fakoli-state is well-positioned. The durable store + three-source git/fs/db reconciliation already inspects the working tree — extend it into a "scan/ingest existing repo" front door that seeds an initial PRD + task graph AND a persisted, re-scannable codebase model in SQLite that stays live as code grows (#78, #404). Add task types beyond "new feature" (bugfix/refactor/modify) so the PRD→tasks→claims→evidence loop covers ongoing brownfield work (#712). fakoli-state has the durable substrate competitors lack but has no codebase-summarization entry path today.

---

### 7. Collision-Free IDs, Git Flexibility & Path Portability

**Summary.** Identity and git coupling cause recurring bugs and friction. Spec-kit's flat numeric branch convention (`001-feature`) races/collides under parallel creation and can't do domain namespacing ([#1382](https://github.com/github/spec-kit/issues/1382)); a regex regression made numbering always repeat `001-` ([#1066](https://github.com/github/spec-kit/issues/1066)). Forced one-branch-per-feature clashes with real team git workflows and the strict regex errors on non-matching branches ([#232](https://github.com/github/spec-kit/issues/232)). Generated plans embed machine-specific absolute paths, breaking sharing across teammates/OSes ([#588](https://github.com/github/spec-kit/issues/588)); BMAD skills reference files by bare filename so ~10 silently fail to load under opencode ([#1956](https://github.com/bmad-code-org/BMAD-METHOD/issues/1956)). MCP-on-host vs agent-in-container infers the wrong project root on every call ([task-master#288](https://github.com/eyaltoledano/claude-task-master/issues/288)).

- **Issue count:** 6
- **Dominant sentiment:** frustrated
- **Opportunity size:** medium
- **Examples:** [spec-kit#1382](https://github.com/github/spec-kit/issues/1382) · [spec-kit#1066](https://github.com/github/spec-kit/issues/1066) · [spec-kit#232](https://github.com/github/spec-kit/issues/232) · [spec-kit#588](https://github.com/github/spec-kit/issues/588) · [BMAD#1956](https://github.com/bmad-code-org/BMAD-METHOD/issues/1956) · [task-master#288](https://github.com/eyaltoledano/claude-task-master/issues/288)

**Fakoli implication.** Mostly a strength with two concrete gaps. fakoli-state's DB-issued IDs are collision-free by construction (no shared-counter races, no fragile shell regex over branch names) and decouple identity from branch names — frame DB-backed IDs as the cure for #1382/#1066. Let the claim→branch model work on a caller-supplied/existing branch rather than imposing a naming scheme (#232 — a clear adoption lever). SQLite with relative/workspace-rooted references makes artifacts inherently portable (#588), and `./`-prefixed root resolution (already enforced by path-resolution validation) loads identically across runtimes (#1956). **GAP:** add an env-var project-root override + stable root resolution so MCP and CLI agree on the `.fakoli-state` location across container/host divergence (#288).

---

### 8. Parent Roll-Up, Dependency Graph & Decision Back-Propagation

**Summary.** Hierarchical state stays inconsistent because relationships aren't enforced. [task-master#250](https://github.com/eyaltoledano/claude-task-master/issues/250): after expanding a complex task into subtasks the parent still reports its original complexity and re-suggests expansion — users want roll-up scoring and recursive expand-to-threshold. [BMAD#2034](https://github.com/bmad-code-org/BMAD-METHOD/issues/2034): changing a Story leaves the parent Epic's scope stale with no roll-up or "Sync Required" flag. Decomposition surfaces architectural/product decisions that belong upstream in the PRD but the workflow is strictly unidirectional with no back-propagation ([BMAD#1638](https://github.com/bmad-code-org/BMAD-METHOD/issues/1638)). Wiring N×M dependencies needs N×M separate calls, exhausting tool-call budgets ([task-master#615](https://github.com/eyaltoledano/claude-task-master/issues/615)).

- **Issue count:** 4
- **Dominant sentiment:** feature_request
- **Opportunity size:** medium
- **Examples:** [task-master#250](https://github.com/eyaltoledano/claude-task-master/issues/250) · [BMAD#2034](https://github.com/bmad-code-org/BMAD-METHOD/issues/2034) · [BMAD#1638](https://github.com/bmad-code-org/BMAD-METHOD/issues/1638) · [task-master#615](https://github.com/eyaltoledano/claude-task-master/issues/615)

**Fakoli implication.** Differentially strong — fakoli-state's recent score-recursion + parent roll-up work (commit `0fec432`) directly answers #250 and #2034; surface roll-up scoring, a target-threshold recursive-expand option, and three-source reconciliation that keeps epic/story/feature state consistent automatically. **GAPS:** (1) extend the decision store (`find_decisions`/`resolve-decisions`) so planning can persist decisions that back-reference and update the PRD, closing the unidirectional gap (#1638); (2) add a batch dependency-edit primitive (multi-source/multi-target in one CLI/MCP call) so agents wire graphs without burning tool-call budgets (#615).

---

### 9. Structured, Verifiable Spec Grammar & Iterative Clarification

**Summary.** Free-form prose specs are interpreted inconsistently by agents and under-specify interfaces. Users want EARS/Gherkin/BDD structured requirements baked into the spec phase for machine-parseability ([spec-kit#1356](https://github.com/github/spec-kit/issues/1356)). The `/clarify` command's hard 5-question cap can't fully de-ambiguate and doesn't re-evaluate residual ambiguity ([spec-kit#617](https://github.com/github/spec-kit/issues/617)). Most acutely, [BMAD#1904](https://github.com/bmad-code-org/BMAD-METHOD/issues/1904): planning produces no concrete API contract (no OpenAPI, no per-endpoint schema), so the coding agent guesses contracts from prose and produces ~8 cascading integration bugs, ~65% root-caused to under-specified specs.

- **Issue count:** 3
- **Dominant sentiment:** feature_request
- **Opportunity size:** medium
- **Examples:** [spec-kit#1356](https://github.com/github/spec-kit/issues/1356) · [BMAD#1904](https://github.com/bmad-code-org/BMAD-METHOD/issues/1904) · [spec-kit#617](https://github.com/github/spec-kit/issues/617)

**Fakoli implication.** Primarily a GAP where fakoli-state could lean its review machinery. The deterministic PRD parser and acceptance-criteria fields could adopt/template an EARS/Gherkin-style structured pattern, raising parse reliability and grounding the six-dimension review-risk score (#1356 — currently a format it does not enforce). Require structured, verifiable contract/schema fields per task and enforce them via PRD/task review gates so cross-agent interfaces are pinned before parallel work begins (#1904 — high-value given fakoli-state's parallel-claim model). The `review_prd` / `find-decisions` loop can drive ambiguity resolution iteratively against persisted `[NEEDS DECISION]` markers instead of a fixed N-question cap (#617).

---

### 10. Install, Distribution & Upgrade-Safe Migration

**Summary.** Onboarding and upgrade friction recurs across all tools. Spec-kit install takes hours with module/python errors and firewall issues; users want `pip install` ([#204](https://github.com/github/spec-kit/issues/204)) and a `specify upgrade {version}` migration command after breaking template changes stranded projects ([#781](https://github.com/github/spec-kit/issues/781)). Task-Master has four+ conflicting install paths and even agents confuse the package names ([#1550](https://github.com/eyaltoledano/claude-task-master/issues/1550)), wants global config so settings aren't copied per-project ([#1031](https://github.com/eyaltoledano/claude-task-master/issues/1031)), Homebrew ([#538](https://github.com/eyaltoledano/claude-task-master/issues/538)), and Docker MCP catalog publishing ([#934](https://github.com/eyaltoledano/claude-task-master/issues/934)). BMAD per-project installs duplicate ~4MB and the upgrade path deletes custom agents; users want global install + link/unlink ([#1728](https://github.com/bmad-code-org/BMAD-METHOD/issues/1728)) and pruning of stale upstream-deleted files ([#2032](https://github.com/bmad-code-org/BMAD-METHOD/issues/2032)).

- **Issue count:** 7
- **Dominant sentiment:** feature_request
- **Opportunity size:** medium
- **Examples:** [spec-kit#781](https://github.com/github/spec-kit/issues/781) · [spec-kit#204](https://github.com/github/spec-kit/issues/204) · [task-master#1550](https://github.com/eyaltoledano/claude-task-master/issues/1550) · [BMAD#1728](https://github.com/bmad-code-org/BMAD-METHOD/issues/1728) · [BMAD#2032](https://github.com/bmad-code-org/BMAD-METHOD/issues/2032) · [task-master#1031](https://github.com/eyaltoledano/claude-task-master/issues/1031) · [task-master#934](https://github.com/eyaltoledano/claude-task-master/issues/934)

**Fakoli implication.** Mixed strength/gap — distribution ergonomics are a competitor weak spot fakoli-state should not reproduce. **STRENGTH:** uv-resolved-on-first-invocation + marketplace `/plugin install` sidesteps npm/pip/brew friction (#204, #538, #1550). **GAPS worth backlog items:** (1) ship a schema/state migration command so on-disk `.fakoli-state` artifacts upgrade cleanly across engine versions instead of breaking projects (#781) — analogous to BMAD's reconcile-and-prune need (#2032), where three-source reconciliation already provides the detect-and-prune primitive; (2) cleanly separate upgrade-safe engine from per-project state so updates never clobber user data, plus a global-config layer (`~/.config/fakoli-state/`) for defaults with project override precedence (#1728, #1031); (3) low-effort reach: publish the FastMCP stdio server to the Docker MCP catalog (#934).

---

### 11. Centralized Task Backend & External Tracker Sync (anti-lock-in)

**Summary.** Teams repeatedly hit the local-file ceiling and want a shared source of truth — but resist vendor lock-in. Task-Master users want GitHub Projects/Issues as a centralized backend ([#11](https://github.com/eyaltoledano/claude-task-master/issues/11)) and an explicit Solo-vs-Multiplayer split ([#91](https://github.com/eyaltoledano/claude-task-master/issues/91)), while commenters prefer local-first with an OPTIONAL external projection and a maintainer warns integrations are "out of scope for OSS, belongs to the commercial product" — leaving a real unmet need. Mermaid/diagram visibility of state ([#1377](https://github.com/eyaltoledano/claude-task-master/issues/1377)) and structured queryable memory ([#502](https://github.com/eyaltoledano/claude-task-master/issues/502)) round out the desire for a legible shared model.

- **Issue count:** 3
- **Dominant sentiment:** feature_request
- **Opportunity size:** medium
- **Examples:** [task-master#11](https://github.com/eyaltoledano/claude-task-master/issues/11) · [task-master#91](https://github.com/eyaltoledano/claude-task-master/issues/91) · [task-master#1377](https://github.com/eyaltoledano/claude-task-master/issues/1377)

**Fakoli implication.** Differentially strong and a clear positioning win. fakoli-state's design point is exactly local-first SQLite (no merge conflicts on a text file) + exclusive claims for concurrency + opt-in bidirectional GitHub-Issues sync as a PROJECTION (Linear/Monday/Jira on roadmap) — answering both the team-coordination need and the anti-lock-in concern that competitors explicitly refuse to build in their OSS tier (#11, #91). Promote this comparison directly. Secondary win: auto-emit a Mermaid dependency/state-machine diagram from the persisted task graph, turning docs into a live generated artifact (#1377).

---

## Where All These Tools Fail Users

These are gaps that **no** tool in the dataset solves — the ecosystem-wide white space:

1. **Closing the verification feedback loop.** Deferred/failed-review findings and manual-test results never flow BACK into planning. BMAD writes deferred items to markdown that nothing ever reads ([#2199](https://github.com/bmad-code-org/BMAD-METHOD/issues/2199)), and the "repeat failure" where a fixed business rule is re-violated in a second endpoint because the lesson was never persisted ([#2135](https://github.com/bmad-code-org/BMAD-METHOD/issues/2135)) is the canonical symptom. No tool turns review evidence into queryable records that future task-planning surfaces on file overlap. fakoli-state's queryable evidence/decision store + file-overlap conflict detection is uniquely positioned here — and no competitor solves it at all.

2. **Bidirectional decision back-propagation.** Decomposition and implementation constantly surface decisions that belong upstream in the PRD/architecture, but every tool's flow is strictly unidirectional — decisions are lost or drift ([spec-kit#609](https://github.com/github/spec-kit/issues/609), [BMAD#1638](https://github.com/bmad-code-org/BMAD-METHOD/issues/1638), [BMAD#1980](https://github.com/bmad-code-org/BMAD-METHOD/issues/1980)). No tool keeps upstream requirements and downstream tasks reconciled when a decision is made mid-stream.

3. **Spec/plan-vs-code drift detection over time.** Tools regenerate or go stale but none continuously reconcile three sources (intent in spec, plan in tasks, reality in code/git) to flag divergence as it accumulates. This is the brownfield-drift complaint ([#916](https://github.com/github/spec-kit/issues/916), [#1100](https://github.com/github/spec-kit/issues/1100)) that no file-based tool can solve — it requires durable state to diff against, which only fakoli-state structurally has.

4. **Right-sizing process to task size with an evidence trail.** tinySpec ([#1174](https://github.com/github/spec-kit/issues/1174)) wants a lightweight path for trivial changes, but no tool routes by measured complexity/blast-radius while still recording proof — it's either heavyweight-everything or untracked-ad-hoc. The six-dimension score + fast-lane is a fakoli-state-shaped answer nobody offers.

5. **An enforced cross-agent interface contract before parallel work begins.** [BMAD#1904](https://github.com/bmad-code-org/BMAD-METHOD/issues/1904) traces ~65% of integration bugs to under-specified specs. No tool pins API/schema contracts as verifiable task fields gated by review prior to handing parallel agents the work — a gap that becomes acute precisely BECAUSE fakoli-state enables parallel claims, making it both a risk and an opportunity to own.

---

## Cross-Cutting Observations

- **The dominant competitor anti-pattern is flat markdown/JSON as state-of-record regenerated from templates.** Nearly every high-severity issue across all four repos (spec-kit #916/#1059/#668/#1100, BMAD #1930/#2034/#2032/#2199, Task-Master #91/#11/#181/#502) traces to the same root: no durable, in-place-mutated, enforced-status store. This is precisely the void fakoli-state's SQLite + event-log + reconciliation fills, making durable state not one feature but the unifying answer to the majority of the backlog.

- **Maintainers repeatedly declare the most-demanded capabilities OUT OF SCOPE for their OSS core** (Task-Master #91 centralized backend "belongs to commercial product", #813/#452 "PR-only", BMAD #452/#1809 framework-dependent). This leaves durable team coordination, external-tracker sync, and parallel-agent handoff as a structurally unmet need across the ecosystem — fakoli-state's strongest land-grab.

- **"Agents lie about done"** (Task-Master #181) and its cousins (BMAD #1930 silent rewrites, #2178 auto-commit) reveal that the deepest user distrust is of unverified agent self-reporting. Evidence-gated completion + human apply gate is fakoli-state's most emotionally resonant differentiator, not just a technical one.

- **Per-editor / per-agent / per-provider integration is the single largest VOLUME of issues but the lowest architectural value** — it is a maintenance tax competitors keep paying. fakoli-state's CLI+MCP runtime-neutrality converts this entire recurring cost into a one-time structural win; resist being drawn into the same treadmill.

- **Greenfield bias is universal:** every tool nails first-build and collapses on brownfield (spec-kit explicitly ~25% coverage, #712). The durable store + git/fs reconciliation gives fakoli-state a credible path to the underserved 75% (modify/refactor/bugfix), but only if a "scan/ingest existing repo" front door is built — currently fakoli-state's most valuable missing entry point.

- **Model-provider plumbing** (Cursor/Bedrock/OpenRouter/Copilot LM API routing, per-agent model config) accounts for a large slice of Task-Master and BMAD issues but is overwhelmingly not relevant to a state engine. fakoli-state's host-driven, no-bundled-LLM, no-stored-keys design sidesteps this entire complaint class and is inherently enterprise/air-gap friendly — a positioning note, not a backlog.

- **Scale breaks file/JSON-based tools hard** (Task-Master #1137 hard-blocks at ~85 tasks via a 41k-token dump). fakoli-state's server-side SQL pagination + one-task-at-a-time work packets are structurally immune — but the codebase MUST audit its own always-on skill/command token footprint (spec-kit #1401) to avoid reproducing the context tax it claims to solve.

---

## Appendix: All Findings

| Repo#Num | Category | Sev | Sentiment | Fakoli Relevance | One-line problem |
|---|---|---|---|---|---|
| spec-kit#916 | state_persistence | 4 | frustrated | fakoli_addresses | No durable workflow for evolving specs; re-init overwrites customizations, specs drift from code. |
| spec-kit#1356 | spec_quality | 3 | feature_request | fakoli_gap | Wants EARS/Gherkin structured requirements grammar for machine-parseable, unambiguous specs. |
| spec-kit#524 | portability_agent_support | 3 | feature_request | fakoli_already_has | Per-editor wiring (Cline) requires bespoke template renaming and maintainer work. |
| spec-kit#752 | task_coordination | 4 | feature_request | fakoli_addresses | Slash commands saturate main context; wants subagent delegation with isolated context. |
| spec-kit#571 | portability_agent_support | 2 | feature_request | fakoli_already_has | No native Zed IDE support; thin +1 per-editor-integration request. |
| spec-kit#609 | docs | 2 | confused | fakoli_addresses | Unclear what belongs in CLAUDE.md vs constitution.md; scattered project truth. |
| spec-kit#1174 | ux_dx | 4 | feature_request | fakoli_addresses | Fixed 4-step workflow over-scaffolds (30 files for a one-button change); wants tinySpec. |
| spec-kit#1382 | spec_quality | 3 | feature_request | fakoli_addresses | Flat numeric branch naming races/collides; wants configurable namespacing/timestamp IDs. |
| spec-kit#1418 | ux_dx | 2 | question | not_relevant | Unclear when/how to feed UI mockups to a text-only spec agent (multimodal authoring). |
| spec-kit#581 | task_coordination | 4 | confused | fakoli_gap | No monorepo model: one spec vs per-subproject, where dirs live, shared state across stacks. |
| spec-kit#580 | portability_agent_support | 3 | blocked | fakoli_addresses | Zed Agent unsupported; state dies on new thread, forcing full re-setup. Suggests ACP. |
| spec-kit#396 | portability_agent_support | 2 | feature_request | fakoli_addresses | Add Aider as a supported agent target; per-agent coverage item (PR in flight). |
| spec-kit#1059 | state_persistence | 4 | frustrated | fakoli_addresses | Re-planning after a spec edit overwrites the plan from template, destroying prior work. |
| spec-kit#1401 | performance_scale | 4 | frustrated | fakoli_addresses | ~18.6k tokens of slash commands load every session, a permanent context tax. |
| spec-kit#712 | task_coordination | 4 | feature_request | fakoli_addresses | Only covers ~25% greenfield; bugfix/modify/refactor/prod-fix work is unstructured. |
| spec-kit#1066 | reliability_bug | 4 | bug_report | fakoli_addresses | Regex regression: branch auto-numbering always repeats 001- instead of incrementing. |
| spec-kit#1100 | state_persistence | 4 | feature_request | fakoli_addresses | Per-feature specs go stale after merge; wants persistent module-level living specs. |
| spec-kit#204 | onboarding_setup | 3 | feature_request | fakoli_gap | Install takes hours (module/python/firewall errors); wants pip install specify-cli. |
| spec-kit#232 | integration | 4 | frustrated | fakoli_addresses | Forced one-branch-per-feature + strict regex clashes with real team git workflows. |
| spec-kit#1017 | task_coordination | 3 | feature_request | fakoli_addresses | No hand-off to a background/async agent; assumes single foreground agent. |
| spec-kit#685 | portability_agent_support | 2 | feature_request | fakoli_already_has | Bash-only core fails catastrophically under fish shell. |
| spec-kit#617 | spec_quality | 3 | feature_request | fakoli_addresses | /clarify caps clarification at 5 questions; no residual-ambiguity re-evaluation. |
| spec-kit#781 | onboarding_setup | 4 | feature_request | fakoli_gap | Breaking template change stranded projects; wants specify upgrade {version} migration. |
| spec-kit#1377 | docs | 2 | feature_request | fakoli_gap | Wants Mermaid flowchart/sequence diagrams of the end-to-end workflow in docs. |
| spec-kit#854 | portability_agent_support | 2 | bug_report | fakoli_gap | codex-cli version bump changed slash-command names; stale hint text confuses users. |
| spec-kit#1051 | docs | 2 | feature_request | not_relevant | Wants a speckit docs generate command (doc-authoring from codebase analysis). |
| spec-kit#588 | portability_agent_support | 3 | bug_report | fakoli_addresses | Plans embed machine-specific absolute paths, breaking sharing across teammates/OSes. |
| spec-kit#404 | integration | 3 | feature_request | fakoli_gap | Wants to reverse an existing/brownfield repo back into PRD + artifacts (/capture). |
| spec-kit#668 | spec_quality | 4 | feature_request | fakoli_addresses | Can't iterate on a spec; feature-add re-derives whole app, no durable memory of #001. |
| spec-kit#181 | verification_evidence | 4 | feature_request | fakoli_addresses | Manual checkbox tracking + agents lie about completion; users add verify tasks to catch it. |
| task-master#78 | onboarding_setup | 4 | feature_request | fakoli_gap | High-demand recursive scan to summarize existing codebase + persist re-scannable model. |
| task-master#11 | integration | 4 | feature_request | fakoli_addresses | Local tasks.json doesn't scale to teams; wants centralized backend without lock-in. |
| task-master#865 | portability_agent_support | 2 | feature_request | not_relevant | Wants model requests routed through Cursor subscription; blocked upstream. |
| task-master#1089 | portability_agent_support | 1 | feature_request | not_relevant | Thin request to support qwen CLI as a provider; niche, no maintainer commitment. |
| task-master#1031 | onboarding_setup | 3 | feature_request | fakoli_gap | Per-project config forces duplicating model/provider settings; wants global config. |
| task-master#906 | integration | 3 | feature_request | fakoli_gap | Wants task work to auto-pull fresh docs via Context7 MCP rather than stale training data. |
| task-master#229 | portability_agent_support | 3 | bug_report | not_relevant | Assumes npm; pnpm/monorepo users can't init after global install. |
| task-master#781 | integration | 2 | feature_request | fakoli_already_has | Wants free/cheap OpenRouter models (:free variant) for research tasks. |
| task-master#91 | state_persistence | 4 | feature_request | fakoli_addresses | Local tasks.json causes merge conflicts; wants Solo vs Multiplayer centralized source. |
| task-master#250 | task_coordination | 3 | feature_request | fakoli_addresses | After subtask expansion, parent still reports original complexity; wants roll-up + recurse. |
| task-master#18 | integration | 2 | feature_request | fakoli_gap | Wants MCP sampling so host client drives completions without a separate API key. |
| task-master#934 | onboarding_setup | 2 | feature_request | fakoli_gap | Wants MCP server published to Docker MCP catalog for one-step sandboxed install. |
| task-master#880 | integration | 3 | frustrated | not_relevant | Requires a separate LLM key even inside an AI IDE; wants MCP sampling to reuse host model. |
| task-master#1550 | onboarding_setup | 3 | confused | fakoli_gap | Four+ conflicting install/init paths; agents even confuse package names. |
| task-master#955 | integration | 2 | feature_request | not_relevant | Wants per-model roles declared inline in MCP config instead of a separate file. |
| task-master#180 | integration | 2 | feature_request | not_relevant | Wants requesty.ai LLM router/gateway as a provider with base-URL override. |
| task-master#568 | integration | 2 | feature_request | not_relevant | Wants to reuse VS Code Copilot/Roo model providers; blocked by VS Code LM API scope. |
| task-master#813 | task_coordination | 2 | feature_request | fakoli_addresses | Wants Aider as execution backend for a unified task-to-implementation handoff. |
| task-master#874 | integration | 2 | feature_request | not_relevant | Enterprise Bedrock gateways need custom base URL + injected headers; hardcoded endpoints. |
| task-master#1137 | performance_scale | 4 | blocked | fakoli_addresses | get_tasks returns a 41k-token blob at ~85 tasks, exceeding MCP ceiling; no pagination. |
| task-master#1058 | other | 1 | praise_with_ask | not_relevant | Automated "your project was featured" promotional notice; no actionable signal. |
| task-master#5 | other | 2 | feature_request | not_relevant | Internal JS→TypeScript migration of the host project; not state semantics. |
| task-master#615 | task_coordination | 3 | feature_request | fakoli_gap | Linking N×M dependencies needs N×M calls, exhausting tool-call budgets; wants bulk command. |
| task-master#259 | integration | 1 | feature_request | not_relevant | Wants CodeRabbit AI PR review on the host project's own PRs (maintainer CI preference). |
| task-master#538 | onboarding_setup | 2 | feature_request | fakoli_already_has | npm/bun install friction; wants a Homebrew formula/tap for one-command install. |
| task-master#452 | portability_agent_support | 2 | feature_request | fakoli_already_has | Wants deep Windsurf Wave 8 Cascade customization; editor-specific surface coupling. |
| task-master#502 | state_persistence | 2 | feature_request | fakoli_addresses | Wants richer non-chat memory (knowledge graphs, PKM) for structured project knowledge. |
| task-master#476 | ux_dx | 2 | feature_request | not_relevant | Can't easily reset model config to default; model list outdated/incomplete. |
| task-master#288 | portability_agent_support | 3 | frustrated | fakoli_gap | MCP-on-host vs agent-in-container infers wrong project root on every call. |
| task-master#235 | task_coordination | 2 | feature_request | fakoli_already_has | Done-task response gives no continuation; agent stalls instead of picking up next task. |
| BMAD#1728 | onboarding_setup | 4 | feature_request | fakoli_gap | Per-project installs duplicate ~4MB; upgrade deletes custom agents. Wants global + link/unlink. |
| BMAD#1956 | portability_agent_support | 4 | frustrated | fakoli_addresses | Skills reference workflow.md by bare filename; ~10 silently fail to load under opencode. |
| BMAD#2178 | task_coordination | 3 | frustrated | fakoli_addresses | Quick Dev auto-commits/pushes without explicit intent; wants opt-in control. |
| BMAD#1809 | portability_agent_support | 3 | feature_request | fakoli_addresses | No portable orchestrator/sub-agent delegation model across 20+ platforms. |
| BMAD#1930 | state_persistence | 4 | bug_report | fakoli_addresses | correct-course silently rewrites already-accepted stories; no status gate or audit trail. |
| BMAD#2338 | onboarding_setup | 3 | frustrated | not_relevant | Per-user settings not shared across worktrees; wants layered global user config. |
| BMAD#1898 | portability_agent_support | 2 | feature_request | fakoli_addresses | Workflows can't detect platform features; capable platforms go unused. |
| BMAD#2034 | state_persistence | 3 | feature_request | fakoli_addresses | Changing a Story leaves parent Epic stale; no roll-up or "Sync Required" flag. |
| BMAD#2124 | ux_dx | 3 | feature_request | not_relevant | Wants per-agent/per-command model selection; customization not picked up on reinstall. |
| BMAD#2199 | verification_evidence | 3 | bug_report | fakoli_addresses | Deferred code-review items are write-only; never read back into planning. |
| BMAD#2394 | reliability_bug | 3 | frustrated | not_relevant | {output_folder} not interpolated; literal dir created (regression of closed #1962). |
| BMAD#2419 | ux_dx | 2 | feature_request | fakoli_addresses | Two similarly named workflows blur story vs implementation-plan altitude. |
| BMAD#2453 | reliability_bug | 3 | bug_report | not_relevant | Reviewer "agents" are actually skills; runtime mismatch burns tokens, nondeterministic. |
| BMAD#1638 | task_coordination | 3 | feature_request | fakoli_gap | Decomposition surfaces upstream decisions but workflow is unidirectional; decisions lost. |
| BMAD#1808 | state_persistence | 2 | feature_request | fakoli_addresses | Implementation artifacts have no chronological ordering; wants date-prefixed naming. |
| BMAD#1904 | spec_quality | 4 | bug_report | fakoli_gap | No concrete API-contract artifact; agent guesses contracts, ~8 cascading integration bugs. |
| BMAD#2032 | state_persistence | 3 | frustrated | fakoli_addresses | quick-update never deletes upstream-removed files; stale components linger and break. |
| BMAD#2187 | other | 2 | feature_request | not_relevant | Roster lacks a DevOps/infra persona lost in the v4→v6 rewrite. |
| BMAD#2135 | state_persistence | 3 | feature_request | fakoli_addresses | No durable memory; same business rule re-violated in a 2nd endpoint (repeat failure). |
| BMAD#1980 | state_persistence | 2 | feature_request | fakoli_addresses | architecture.md blurs durable decisions with ephemeral guidance; wants ADR separation. |
| spec-workflow#229 | ux_dx | 1 | feature_request | not_relevant | Spec docs can't bundle/render image assets (SVG/PNG) via relative paths in the editor UI. |

---

*81 findings across 4 repositories. Severity 1 (low) to 4 (high). Fakoli relevance: `fakoli_addresses` (durable-state thesis already covers), `fakoli_already_has` (existing structural strength), `fakoli_gap` (real backlog opportunity), `not_relevant` (out of scope for a state engine).*
