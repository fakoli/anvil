# anvil Prioritized Backlog

## North Star

Be the durable, runtime-neutral state-of-record for AI-and-human software work: a local-first SQLite store where every requirement, task, claim, and piece of evidence is an additive, in-place transition (never a template regenerated and never an agent's unverified self-report), so multiple agents and humans can coordinate in parallel — in any MCP/ACP host — without overwriting each other, losing decisions, or trusting a fake "done." anvil wins on the one structural thing no file-based competitor can match: **edits are recorded transitions, completion is evidence-gated, and accepted work is immutable.**

## Epics

| Epic | Name | Summary |
|---|---|---|
| E1 | Engine Reliability & Concurrency Correctness | Harden the claim/lease core so single-winner/lease guarantees hold under real parallelism. Closes two benchmark-confirmed engine bugs and locks behavior with a concurrency regression suite. Table-stakes for multi-agent coordination. |
| E2 | Standalone Onboarding & First-Run Self-Sufficiency | Make anvil usable end-to-end with zero crew/flow dependency: one-command init→PRD→plan quickstart, in-product help, and a doctor command. |
| E3 | Portability & Runtime Neutrality | Cement the vendor-neutral, container-safe substrate: env-var project-root override, version-pinned/self-describing CLI+MCP surface, and a token-footprint self-audit. |
| E4 | Machine-Readable Output & Programmatic Surface | Stable, schema-versioned JSON output across CLI commands and an explicit "next ready task" field so non-Claude hosts and scripts can drive anvil. |
| E5 | Brownfield Onboarding & Task-Type Coverage | The missing "scan/ingest existing repo" front door plus non-feature task types (bugfix/refactor/modify) covering the underserved 75% of real work. |
| E6 | Distribution, Migration & Global Config | Upgrade-safe schema/state migration, engine-vs-state separation with a global-config layer, and Docker MCP catalog publishing. |
| E7 | Verification Feedback Loop & Decision Back-Propagation | Close loops no competitor closes: deferred-finding read-back on file overlap, decision back-propagation to the PRD, batch dependency edits, structured contract fields. |
| E8 | Legible Shared Model & External Projection | Auto-generated Mermaid diagrams and opt-in bidirectional GitHub-Issues projection as anti-lock-in positioning. |
| E9 | WF-3 Substrate & Dogfooding Friction Follow-ups | Sand off the friction surfaced building the WF-3 workflow runner by dogfooding anvil on itself: a programmatic engine write API, a verification-command path doctor check, a relaxed evidence gate, and first-class non-PRD (workflow-origin) tasks. Each item carries researched implementation trade-offs. |
| E10 | Framework Integration & Cross-Harness Compatibility | Prove the runtime-neutral moat by breadth: thin adapters that let many agent frameworks/harnesses (Claude Code, Codex, CI, Vercel eve, …) *drive* anvil's governed lifecycle while anvil governs (single-winner leases, evidence gate, audit) — never reimplementing framework features or coupling to a vendor. Track + publish the compatibility surface; eventually benchmark across harnesses. Breadth-as-proof, not feature-shoe-in. |
| E11 | Backlog Generation & Management (first-class, governed) | Make anvil aware of and tooled for the **whole backlog** (not just parsed tasks): a governed `backlog_item` node above requirements + a reusable ideation→item loop that systematizes the brainstorm→research→insight→item flow used to build E9/E10. Research found verified white space — no tool manages a cross-session "what's left" backlog. Stay the governed substrate *under* the loop, not a PM platform. Grounded in [`docs/research/2026-06-19-backlog-management-research.md`](../research/2026-06-19-backlog-management-research.md). |

---

## E1 — Engine Reliability & Concurrency Correctness

### B01 — Fix TOCTOU file-overlap race: re-check `expected_files` overlap inside the claim transaction

- **Priority:** P0  **Effort:** M  **Type:** bug  **Status:** DONE (v1.23.3)
- **Rationale:** Benchmark-confirmed (~8%): two file-overlapping tasks can both be claimed concurrently. `check_conflicts` (claims/manager.py) reads active-claim overlap BEFORE `_backend.append()`, and the in-tx guard `_check_claim_created` / `_write_claim_created` (state/sqlite.py) only checks the task's own status (`ready`/`claimed`), never re-checking `expected_files` overlap. The overlap read sits entirely outside the `BEGIN IMMEDIATE` window, so two claims on disjoint tasks with overlapping files both pass `check_conflicts` and both commit. This breaks the core single-winner promise that makes the lease primitive a differentiator (eyaltoledano/claude-task-master#91/#11).
- **Resolution:** The file-overlap and conflict-group re-check now runs INSIDE the `BEGIN IMMEDIATE` transaction that writes the claim row (`_write_claim_created` in `state/sqlite.py`). The `ConflictWarning` path in `manager.check_conflicts` is retained as a fast pre-check but is no longer the sole guard. The concurrency suite in `tests/test_claims_concurrency.py` (≥200 iterations, ≥8 threads, three contention shapes) proves 0% double-claim rate, confirming the fix is atomic. CHANGELOG entry: v1.23.3.
- **Likely files:** `bin/src/anvil/state/sqlite.py`, `bin/src/anvil/claims/manager.py`, `bin/src/anvil/state/payloads.py`, `bin/tests/test_claims.py`
- **Depends on:** —

### B02 — Thread config `default_lease_minutes` into the CLI `ClaimManager` and accept fractional minutes

- **Priority:** P1  **Effort:** S  **Type:** bug
- **Rationale:** Benchmark-confirmed: cli/claim.py builds `ClaimManager(backend, clock, actor=...)` with no `default_lease_minutes`, so every CLI claim silently uses the hardcoded 60-min default regardless of config.yaml, while the MCP path (mcp_server.py) correctly passes `default_lease_minutes=lease_minutes`. Separately, config.py:358 coerces `default_lease_minutes` via `int(str(data.get(...)))`, which rejects/floors fractional minutes (e.g. 0.5). Silent config drop is exactly the failure class anvil positions against (durable, honored configuration).
- **Acceptance:** cli/claim.py loads config.yaml (it already does for `branch_prefix`) and passes `default_lease_minutes` (and `default_heartbeat_minutes`) into `ClaimManager`. config.py accepts fractional minutes (e.g. 0.5) via float coercion with validation, rejecting only non-numeric/negative/boolean values. Test: a project config with `default_lease_minutes: 30` produces a CLI claim whose `lease_expires_at` is `created_at + 30min` (not 60); a config with `0.5` yields a 30-second lease; the MCP path is unchanged.
- **Likely files:** `bin/src/anvil/cli/claim.py`, `bin/src/anvil/config.py`, `bin/tests/test_cli.py`, `bin/tests/test_config.py`
- **Depends on:** —

### B03 — Concurrency regression suite: single-winner under N threads (same-task, file-overlap, conflict-group)

- **Priority:** P1  **Effort:** M  **Type:** infra
- **Rationale:** The two confirmed bugs were both concurrency races that unit tests missed; without a standing parallel-claim suite they will regress. The lease/claim primitive is anvil's strongest differentiator and must be continuously proven, not asserted. Covers the three race shapes: same-task contention, file-overlap (B01), and conflict-group members.
- **Acceptance:** A pytest module spins N≥8 real threads (or processes) against one shared state.db and asserts: (1) same-task — exactly one of N claimers on the same `task_id` wins, N-1 get `ClaimError`; (2) file-overlap — exactly one of two file-overlapping distinct tasks is claimed; (3) conflict-group — at most one member of a declared conflict group is claimed concurrently. Each scenario runs ≥200 iterations with 0 double-claims and is wired into CI. Suite fails on main if B01/B02 are reverted.
- **Likely files:** `bin/tests/test_claims_concurrency.py`, `bin/tests/conftest.py`, `bin/src/anvil/claims/manager.py`, `bin/src/anvil/state/sqlite.py`
- **Depends on:** B01

---

## E2 — Standalone Onboarding & First-Run Self-Sufficiency

### B04 — One-command standalone quickstart: `init --with-sample` seeds a runnable PRD→plan→next loop

- **Priority:** P2  **Effort:** M  **Type:** feature
- **Rationale:** Polish, not critical path for a standalone launch. Standalone framing requires self-sufficient onboarding with no crew/flow dependency. Today init scaffolds an empty `.anvil` and points users at a template; first run requires manually authoring a PRD before anything is demonstrable. Competitors' onboarding is their #1 friction point (spec-kit#204, task-master#1550). A sample-seeded loop lets a new user reach `anvil next` in one command and see the durable-state value immediately.
- **Acceptance:** `anvil init --with-sample` creates config + a valid sample prd.md and runs parse→plan→score so `anvil next` returns a ready task with no further input. Without the flag, init behavior is unchanged. A new-user smoke test runs `init --with-sample` then `next` in a temp dir and asserts a ready task is returned. README Quick Start updated to show the zero-to-next path with no crew/flow installed.
- **Likely files:** `bin/src/anvil/cli/init_status.py`, `bin/src/anvil/cli/__main__.py`, `bin/tests/test_cli.py`, `plugins/anvil/README.md`
- **Depends on:** —

### B05 — `anvil doctor`: diagnose state, config, lease, and reconciliation health

- **Priority:** P2  **Effort:** M  **Type:** feature
- **Rationale:** Standalone users have no crew/flow to surface problems and no single command to answer "is my state healthy?" A doctor command turns silent-failure classes (stale claims, config not honored — see B02, schema-version mismatch, drift) into actionable output, supporting self-sufficient onboarding and reducing support load. Pairs with three-source reconciliation already in the engine.
- **Acceptance:** `anvil doctor` reports: state.db reachability + schema version, config.yaml parse status and effective lease/heartbeat values, count of active/stale claims, replay integrity (events.jsonl vs db), and a git/fs/db reconciliation drift summary, with a non-zero exit code on any ERROR-level finding. Supports `--json` (depends on B10). Tested against a healthy project (clean exit) and a project with an injected stale claim + schema mismatch (non-zero, both findings listed).
- **Likely files:** `bin/src/anvil/cli/doctor.py`, `bin/src/anvil/cli/__main__.py`, `bin/src/anvil/sync/reconciliation.py`, `bin/tests/test_cli.py`
- **Depends on:** B10

### B06 — Self-sufficient docs: standalone Getting Started that assumes no crew/flow installed

- **Priority:** P2  **Effort:** S  **Type:** docs
- **Rationale:** README and how-to docs currently frame anvil inside "the trinity" with flow/crew. Standalone positioning requires a top-of-funnel path that treats crew/flow as optional integrations. Reduces adoption friction for Codex/Cursor/OpenHands/Copilot users (runtime-neutrality theme #580).
- **Acceptance:** docs/how-to/getting-started.md and README lead with a crew/flow-free walkthrough (init→prd→plan→claim→execute→finish via CLI/MCP only), with an explicit "Optional: fakoli-flow / fakoli-crew integration" section clearly marked as additive. A docs check confirms the standalone path references no crew/flow command as a required step.
- **Likely files:** `plugins/anvil/README.md`, `plugins/anvil/docs/how-to/getting-started.md`, `plugins/anvil/docs/_positioning.md`
- **Depends on:** B04

### B27 — Caller-supplied / existing-branch claims: attach a claim to a named branch

- **Priority:** P2  **Effort:** S  **Type:** feature
- **Rationale:** Today `claim` always imposes its own `agent/<task>-<slug>` branch; teams already working on an existing branch cannot attach a claim to it. Letting the caller supply a branch is a competitor adoption lever (research #232) — it meets users where their git workflow already is instead of forcing anvil's branch naming.
- **Acceptance:** `claim --branch <name>` (or a config equivalent) attaches the claim to an existing or caller-named branch instead of generating `agent/<task>-<slug>`; default behavior (auto-generated branch) is unchanged when the option is absent. Tested: claiming with `--branch existing-feature` records the claim against that branch, and a claim without the option still generates the default branch name.
- **Likely files:** `bin/src/anvil/cli/claim.py`, `bin/src/anvil/claims/manager.py`, `bin/src/anvil/config.py`, `bin/tests/test_cli.py`
- **Depends on:** —

### B29 — `anvil init` at the plugin root: guidance instead of a bare refusal (dogfooding friction)

- **Priority:** P3  **Effort:** S  **Type:** modify
- **Rationale:** Surfaced 2026-06-19 while dogfooding anvil to plan its own WF-3 work. `anvil init` run at the anvil plugin root fails with `Error: this directory is the anvil plugin root. Run anvil init from your project directory, not from inside the plugin.` That guard is correct (don't seed `.anvil/` into the published plugin), but for the legitimate self-hosting case it gives no next step — the user has to guess that `bin/` (the Python package root, where tests already run) is the right project dir. The refusal also fires *after* an accidental `cd` can leave a stray `.anvil/` in a subdir, so the message should name a concrete fallback.
- **Acceptance:** `anvil init` at the plugin root still refuses, but the message suggests an explicit project dir (e.g. "to manage anvil's own work, run from `bin/`" or honor an `--allow-plugin-root`/`ANVIL_PROJECT_ROOT` override). Tested: running at the plugin root prints the suggested path and exits non-zero; running in the suggested dir succeeds.
- **Likely files:** `bin/src/anvil/cli/init_status.py` (guard at `:106-113`), `bin/src/anvil/cli/_helpers.py` (`_is_plugin_root` `:178-192`, `_resolve_base_dir` `:59-87`), `tests/test_cli.py`
- **Depends on:** —
- **Implementation trade-offs** (researched 2026-06-19):
  - The guard is unconditional at `init_status.py:106-113`; detection is `_is_plugin_root` (`<dir>/.claude-plugin/plugin.json` with `name == "anvil"`). `init` passes `cwd=None`, so only `ANVIL_ROOT`/cwd resolve — there is no `--cwd` on `init`.
  - **Opt 1 — actionable message (S):** glob for a sibling `*/pyproject.toml`, suggest `cd <dir> && anvil init` + the `ANVIL_ROOT=<dir>` hint. Still refuses; purely additive. Trade-off: heuristic suggestion could point at a wrong subdir in unusual layouts.
  - **Opt 2 — `--allow-plugin-root` escape hatch (S–M):** opt-in bypass. Trade-off: lets users seed `.anvil/` into the plugin repo — the exact thing the guard prevents; needs gitignore hygiene.
  - **Opt 3 — add `--cwd` to `init` (M):** parity with `status`; `_resolve_base_dir` already supports it. Trade-off: more surface; redundant with `ANVIL_ROOT` but more discoverable.
  - **Recommendation:** Opt 1 now (no downside); defer 2/3 unless requested. See [E9](#e9--wf-3-substrate--dogfooding-friction-follow-ups) for the sibling WF-3 friction items.

---

## E3 — Portability & Runtime Neutrality

### B07 — `ANVIL_ROOT` env override + stable root resolution across container/host

- **Priority:** P1  **Effort:** M  **Type:** feature
- **Rationale:** Confirmed gap: config.py has no env-var project-root override (only a comment about auto-detect from environment) and `_resolve_state_dir` walks from cwd. MCP-on-host vs agent-in-container infers the wrong project root on every call (eyaltoledano/claude-task-master#288). Runtime neutrality is a lean-on strength only if CLI and MCP agree on the `.anvil` location regardless of where they run.
- **Acceptance:** Both CLI (`_resolve_state_dir`) and MCP server honor `ANVIL_ROOT` when set, resolving to that path's `.anvil` before any cwd walk; when unset, behavior is unchanged. Precedence documented: explicit `--cwd`/arg > `ANVIL_ROOT` > cwd walk. Test: with `ANVIL_ROOT` pointed at a fixture project from an unrelated cwd, CLI `next` and the MCP `get_next_task` tool resolve the same state.db.
- **Likely files:** `bin/src/anvil/cli/_helpers.py`, `bin/src/anvil/config.py`, `bin/src/anvil/mcp_server.py`, `bin/tests/test_cli.py`, `bin/tests/test_mcp.py`
- **Depends on:** —

### B08 — Version-pin and self-describe the CLI/MCP command surface

- **Priority:** P2  **Effort:** S  **Type:** feature
- **Rationale:** Host-agent upgrades silently break invocation hints when command names/shapes drift (spec-kit codex-cli prefix churn #854; BMAD bare-filename loads #1956). anvil should self-describe its surface so hosts can introspect rather than hardcode. Low-effort durability win for runtime neutrality.
- **Acceptance:** `anvil --version` reports engine + schema versions, and a `anvil describe` (or MCP capability) emits a machine-readable manifest of CLI subcommands and MCP tool names with a stable `api_version` field. A test asserts the described surface matches the registered Typer commands and FastMCP tools (no drift), failing CI if a command is added/renamed without updating the manifest.
- **Likely files:** `bin/src/anvil/cli/__main__.py`, `bin/src/anvil/mcp_server.py`, `bin/src/anvil/cli/describe.py`, `bin/tests/test_cli.py`
- **Depends on:** —

### B09 — Self-audit plugin token footprint: keep skills/commands context-frugal

- **Priority:** P1  **Effort:** S  **Type:** infra
- **Rationale:** Cheap (S) and it earns the right to make the context-frugality marketing claim the positioning leans on — measure before marketing. CRITICAL self-audit caveat (context-frugal theme; spec-kit#1401 18.6k always-on token tax). anvil claims structural immunity to the dump-everything failure (#1137) but ships 8 skills + 6 agents that load into host context; it must prove it does not reproduce the tax it positions against. This is a measurement+budget guardrail, not a feature.
- **Acceptance:** A CI check measures the combined token footprint of anvil's always-loaded skill frontmatter/SKILL.md and command surface, and fails if it exceeds an explicit budget (set from current measured baseline, with headroom). Any SKILL.md over the per-file ceiling is flagged. Report shows per-skill token counts. Documented budget in docs/architecture.md or a new docs/context-budget.md.
- **Likely files:** `plugins/anvil/skills/*/SKILL.md`, `plugins/anvil/tests/test_token_budget.py`, `plugins/anvil/docs/architecture.md`
- **Depends on:** —

---

## E4 — Machine-Readable Output & Programmatic Surface

### B10 — Stable, schema-versioned `--json` output across read commands

- **Priority:** P0  **Effort:** L  **Type:** feature
- **Rationale:** This is the real product surface for a standalone, non-Claude host. Confirmed absent today — no `--json` flag exists on CLI read commands. It gates the doctor/next/graph and drift items. Standalone framing requires machine-readable output so non-Claude hosts/scripts can drive the engine. Structured queryable state is an explicit user ask (eyaltoledano/claude-task-master#502) and underpins doctor (B05), describe (B08), drift (B26), and external projection (B24). `list_tasks`/`next` must paginate server-side (context-frugal theme #1137) and emit JSON, not human tables.
- **Acceptance:** `anvil next`, `list`, `status`, `show <task>`, and `review tasks` accept `--json` and emit output validated against a versioned JSON schema (`schema_version` field) under schemas/. JSON is stable across runs (deterministic ordering). `list`/`next` support `--limit`/`--offset` pagination. A round-trip test parses each command's `--json` output with the published schema. Human (default) output is unchanged.
- **Likely files:** `bin/src/anvil/cli/__main__.py`, `bin/src/anvil/cli/_helpers.py`, `bin/src/anvil/context/packets.py`, `schemas/cli-output.schema.json`, `bin/tests/test_cli.py`
- **Depends on:** —

### B11 — Expose the existing `SCHEMA_VERSION` (=4) and `_check_schema_version` to tooling

- **Priority:** P2  **Effort:** XS  **Type:** infra
- **Rationale:** Already ~80% built: `SCHEMA_VERSION = 4` and the migration branches (0/1→4, 2→4, 3→4) already exist in `state/sqlite.py`. This item is a thin accessor, NOT new infrastructure — add a public accessor over the existing constant + `_check_schema_version`, and surface the version/mismatch in `status` and `--json`. Doctor (B05), migration (B16), and JSON consumers (B10) all need an authoritative state schema version to compare against engine version; that version already exists and only needs to be made readable.
- **Acceptance:** A public accessor returns the existing `SCHEMA_VERSION` (=4); `status` and `--json` (B10) output surface the on-disk schema version and any mismatch. Opening a db with a newer `schema_version` than the engine supports continues to raise a clear, actionable error via the existing `_check_schema_version` (not a silent partial read). Test asserts the accessor returns the constant, that `status`/`--json` include the version, and that a forward-incompatible version is rejected with guidance to upgrade.
- **Likely files:** `bin/src/anvil/state/sqlite.py`, `bin/src/anvil/cli/init_status.py`, `bin/tests/test_sqlite.py`
- **Depends on:** —

### B12 — `get_next_task` / finish responses explicitly name the next ready task

- **Priority:** P2  **Effort:** S  **Type:** feature
- **Rationale:** Users want a done-response that names the next task to keep agents moving without a separate query (eyaltoledano/claude-task-master#235). anvil already computes the ready queue; surfacing `next_ready` inline on completion/finish closes the agent loop and reinforces the parallel-work substrate.
- **Acceptance:** `submit_completion_evidence` / finish (CLI + MCP) include a `next_ready` field naming the next claimable task (respecting deps, claims, and file-conflict exclusions) or null when none is available. The field appears in both `--json` (B10) and MCP responses. Test: after finishing task A, the response names the correct next ready task and excludes any task whose files overlap an active claim.
- **Likely files:** `bin/src/anvil/mcp_server.py`, `bin/src/anvil/cli/__main__.py`, `bin/src/anvil/claims/manager.py`, `bin/tests/test_mcp.py`
- **Depends on:** B10

---

## E5 — Brownfield Onboarding & Task-Type Coverage

### B13 — Brownfield scan/ingest: seed an initial PRD + task graph from an existing repo

- **Priority:** P2  **Effort:** XL  **Type:** feature
- **Rationale:** Highest land-grab but an XL parity feature — sequence after the engine is trustworthy and scriptable. Biggest brownfield ask in the ecosystem (eyaltoledano/claude-task-master#78, 34 reactions; spec-kit#712 ~25% greenfield coverage, #404 reverse repo→PRD). anvil's three-source reconciliation already inspects the working tree but has no codebase-summarization entry path — its single most valuable missing front door. Extends the durable substrate competitors lack into the underserved 75% of real work.
- **Acceptance:** `anvil scan` (or `init --from-repo`) walks the existing working tree, produces a draft prd.md plus an initial feature/task graph, and persists a re-scannable codebase model in SQLite (module/file inventory the engine can diff against later). Re-running scan reconciles against the persisted model and reports the delta rather than overwriting. Integration test runs scan on a fixture repo and asserts a non-empty draft PRD + tasks + a queryable codebase model row set.
- **Likely files:** `bin/src/anvil/cli/scan.py`, `bin/src/anvil/planning/inference.py`, `bin/src/anvil/sync/reconciliation.py`, `bin/src/anvil/state/schema.py`, `bin/tests/test_scan.py`
- **Depends on:** B11

### B14 — Add non-feature task types (bugfix / refactor / modify) through the PRD→tasks→claims loop

- **Priority:** P2  **Effort:** L  **Type:** feature
- **Rationale:** Every competitor optimizes for new-feature greenfield and falls down on ongoing work (spec-kit#712). For brownfield (B13) to be useful, the full loop must carry task types beyond "new feature" so bugfix/refactor/modify work gets the same durable PRD→tasks→claims→evidence treatment. Routes naturally with the six-dimension score (trivial fast-lane, #1174).
- **Acceptance:** Task model + planner support a `task_type` enum (feature, bugfix, refactor, modify) that flows through plan, score, claim, work-packet rendering, and evidence. `list`/`next` can filter by `task_type` (`--json`, B10). The six-dimension score routes low-complexity/low-blast tasks to a lightweight work-packet variant. Test: a PRD with a bugfix item produces a typed task that claims, executes, and submits evidence end-to-end.
- **Likely files:** `bin/src/anvil/state/models.py`, `bin/src/anvil/planning/template.py`, `bin/src/anvil/planning/scoring.py`, `bin/src/anvil/context/packets.py`, `bin/tests/test_models.py`
- **Depends on:** B13

### B15 — Right-size process by score: fast-lane work packets for trivial changes

- **Priority:** P3  **Effort:** M  **Type:** feature
- **Rationale:** where-all-tools-fail: no tool routes by measured complexity/blast-radius while still recording proof — it's heavyweight-everything or untracked-ad-hoc (tinySpec spec-kit#1174). The six-dimension score + evidence trail is an anvil-shaped answer nobody offers; a fast-lane keeps trivial tasks tracked without full ceremony.
- **Acceptance:** Tasks scoring below configurable complexity/blast thresholds render a minimal work packet (fewer required evidence fields, single-step) while still recording a completion-evidence transition. Threshold is config-driven. Test: a trivial-scored task completes via the fast-lane and still produces an immutable evidence record; a high-blast task still requires the full packet.
- **Likely files:** `bin/src/anvil/context/packets.py`, `bin/src/anvil/planning/scoring.py`, `bin/src/anvil/config.py`, `bin/tests/test_context.py`
- **Depends on:** B14

---

## E6 — Distribution, Migration & Global Config

### B16 — Promote the existing in-init schema migration to an explicit `migrate state` command

- **Priority:** P1  **Effort:** M  **Type:** feature
- **Rationale:** Schema migration ALREADY EXISTS inside `state/sqlite.py` — the branches 0/1→4, 2→4, 3→4 run automatically at init; `migrate.py` only migrates the events.jsonl format for git-backed storage. The work is NOT to build a migration framework; it is to promote the existing in-init migration to an explicit, backed-up, dry-run `migrate state` command with an active-claim guard, so upgrades are observable and safe rather than silent. Competitors' breaking template changes strand projects (spec-kit#781) and upgrades delete user data (BMAD#2032). The thin B11 accessor surfaces the version to migrate from/to.
- **Acceptance:** `anvil migrate state` (or `migrate --schema`) detects the on-disk `schema_version` (via the B11 accessor), runs the existing ordered, idempotent forward migration branches up to the current engine version, dry-run by default with `--yes` to apply, and backs up state.db before mutating. Refuses while claims are active (same guard as migrate-events). Test: a fixture db at an older version migrates to the current version with all rows preserved and replay still passes; re-running migrate is a no-op.
- **Likely files:** `bin/src/anvil/cli/migrate.py`, `bin/src/anvil/state/sqlite.py`, `bin/tests/test_snapshot.py`, `plugins/anvil/docs/migrations.md`
- **Depends on:** —

### B17 — Global-config layer (`~/.config/anvil`) with project-override precedence

- **Priority:** P2  **Effort:** M  **Type:** feature
- **Rationale:** Users want settings that aren't copied per-project (eyaltoledano/claude-task-master#1031) and an engine cleanly separated from per-project state so upgrades never clobber data (BMAD#1728). A global defaults layer with project override keeps per-project config.yaml minimal and durable.
- **Acceptance:** Config loading merges `~/.config/anvil/config.yaml` (global defaults) under project `.anvil/config.yaml` (overrides), with documented precedence: explicit CLI arg > project config > global config > built-in default. `ANVIL_ROOT` (B07) and lease values (B02) participate in this precedence. Test: a global default lease of 45 is overridden to 30 by a project config and to 15 by a CLI flag.
- **Likely files:** `bin/src/anvil/config.py`, `bin/src/anvil/cli/_helpers.py`, `bin/tests/test_config.py`, `plugins/anvil/docs/cli-reference.md`
- **Depends on:** B02

### B18 — Publish the FastMCP stdio server to the Docker MCP catalog

- **Priority:** P3  **Effort:** S  **Type:** infra
- **Rationale:** Low-effort distribution reach the ecosystem explicitly wants (eyaltoledano/claude-task-master#934). anvil already ships a FastMCP stdio server; packaging it for the Docker MCP catalog broadens runtime-neutral reach without per-editor wiring.
- **Acceptance:** A Dockerfile + catalog manifest package the anvil-mcp stdio server with `ANVIL_ROOT` (B07) bind-mount support documented. The image starts the MCP server and a smoke test connects and lists tools. Publishing steps documented in docs/mcp.md.
- **Likely files:** `plugins/anvil/Dockerfile`, `plugins/anvil/bin/anvil-mcp`, `plugins/anvil/docs/mcp.md`
- **Depends on:** B07

---

## E7 — Verification Feedback Loop & Decision Back-Propagation

### B19 — Surface deferred/failed-review evidence back into planning on file overlap

- **Priority:** P2  **Effort:** L  **Type:** feature
- **Rationale:** where-all-tools-fail: deferred/failed-review findings are write-only in competitors and never read back (BMAD#2199), causing repeat-failure where a fixed rule is re-violated in a second endpoint (#2135). anvil's queryable evidence store + file-overlap detection is uniquely positioned to close this loop — no competitor solves it at all.
- **Acceptance:** Deferred/failed review findings are stored as queryable evidence records linked to the files they touched. When a new task is claimed or planned whose `expected_files` overlap a prior deferred finding, the work packet / claim response surfaces those findings. Test: defer a finding on file X, then claim a later task touching file X — the prior finding appears in the work packet.
- **Likely files:** `bin/src/anvil/review/gates.py`, `bin/src/anvil/context/packets.py`, `bin/src/anvil/claims/manager.py`, `bin/tests/test_review.py`
- **Depends on:** —

### B20 — Decision back-propagation: persist decisions that back-reference and update the PRD

- **Priority:** P2  **Effort:** M  **Type:** feature
- **Rationale:** Every tool's flow is strictly unidirectional; decisions surfaced during decomposition/implementation belong upstream in the PRD but are lost or drift (BMAD#1638, spec-kit#609). anvil's `find_decisions`/`resolve-decisions` loop and recent parent roll-up work (commit `0fec432`) make it the only substrate that can keep upstream requirements and downstream tasks reconciled.
- **Acceptance:** Decisions recorded during planning/execution can carry a `prd_ref` and, on resolution, write back a recorded transition that updates the referenced PRD section (e.g. resolving a `[NEEDS DECISION]` marker) without overwriting unrelated content. Test: a decision resolved mid-stream updates the linked PRD requirement and the change is an additive recorded transition visible in the event log.
- **Likely files:** `bin/src/anvil/planning/decisions.py`, `bin/src/anvil/cli/prd.py`, `bin/src/anvil/state/transitions.py`, `bin/tests/test_decisions.py`
- **Depends on:** —

### B21 — Batch dependency-edit primitive (multi-source/multi-target in one call)

- **Priority:** P3  **Effort:** M  **Type:** feature
- **Rationale:** Wiring N×M dependencies needs N×M separate calls, exhausting tool-call budgets (eyaltoledano/claude-task-master#615). A single batch edit lets agents wire graphs without burning the call budget — directly supports the context-frugal theme and the parallel-work substrate.
- **Acceptance:** A CLI command and MCP tool accept a batch of dependency edges (add/remove, multiple sources/targets) applied as one transaction with cycle detection that rejects the whole batch on any invalid edge. Test: a batch of 10 edges applies atomically; a batch introducing a cycle is rejected with no partial application.
- **Likely files:** `bin/src/anvil/cli/plan.py`, `bin/src/anvil/mcp_server.py`, `bin/src/anvil/planning/_plan_helpers.py`, `bin/tests/test_cli_plan.py`
- **Depends on:** —

### B22 — Structured contract/schema fields per task, enforced by review gates

- **Status:** Deferred — post-v1
- **Priority:** P3  **Effort:** L  **Type:** feature
- **Rationale:** Deferred. The failure mode it addresses (under-specified cross-agent contracts) only becomes acute once real parallel-claim usage exists, so it is not on the path to v1. ~65% of integration bugs root-cause to under-specified specs; no tool pins API/schema contracts as verifiable task fields gated by review before parallel work begins (BMAD#1904). This becomes acute precisely BECAUSE anvil enables parallel claims — both a risk and an opportunity to own. Pairs with EARS/Gherkin-style acceptance fields (spec-kit#1356) to raise parser reliability.
- **Acceptance:** Tasks support optional structured contract fields (e.g. interface/schema reference) that, when a task is marked as a cross-agent interface, are required and enforced by the `review_tasks` gate before promotion to ready. Test: a task flagged as an interface with no contract is blocked at review; supplying a valid contract promotes it.
- **Likely files:** `bin/src/anvil/state/models.py`, `bin/src/anvil/review/gates.py`, `bin/src/anvil/planning/template.py`, `bin/tests/test_review.py`
- **Depends on:** —

### B25 — Enforceable evidence gate: refuse completion in strict mode when required evidence is absent

- **Priority:** P1  **Effort:** M  **Type:** feature
- **Rationale:** Today the completion-evidence gate is ADVISORY — it flags missing evidence but does not block submission (the benchmark says so explicitly). "Agents lie about done" (eyaltoledano/claude-task-master#181) is the single most-felt pain in the dataset and goes straight to the wedge: completion is supposed to be evidence-gated. Making the gate able to REFUSE in a configurable strict mode is what turns "done" from a self-report into a verified transition.
- **Acceptance:** A `strict_evidence: true` config (or `--strict` flag) causes `submit_completion_evidence` / apply to REJECT a completion that is missing required evidence or required verification commands, with an actionable error naming what is absent. Default remains advisory (flags but does not block) for back-compat. Tested BOTH ways: a strict-mode submit missing required evidence is rejected; the same submit in default (advisory) mode is accepted with a warning.
- **Likely files:** `bin/src/anvil/review/gates.py`, `bin/src/anvil/cli/__main__.py`, `bin/src/anvil/mcp_server.py`, `bin/src/anvil/config.py`, `bin/tests/test_review.py`
- **Depends on:** —

### B28 — Structured acceptance grammar (EARS/Gherkin) in the PRD parser

- **Priority:** P3  **Effort:** M  **Type:** feature
- **Rationale:** Optionally parsing EARS/Gherkin-style acceptance criteria raises parser reliability and grounds the six-dimension score in structured intent rather than freeform prose (theme #9, spec-kit#1356). Pairs with the contract fields in B22 — structured acceptance is the spec-quality lever that makes downstream scoring and verification more trustworthy.
- **Acceptance:** The PRD parser recognizes a structured acceptance grammar (EARS "WHEN/THEN" or Gherkin "Given/When/Then") when present in acceptance criteria, extracting structured clauses, and falls back to freeform parsing when no structured grammar is detected. Tested: a PRD with EARS/Gherkin acceptance criteria parses into structured clauses; a freeform PRD still parses unchanged.
- **Likely files:** `bin/src/anvil/planning/parser.py`, `bin/src/anvil/planning/inference.py`, `bin/src/anvil/planning/scoring.py`, `bin/tests/test_parser.py`
- **Depends on:** —

---

## E8 — Legible Shared Model & External Projection

### B23 — Auto-emit a Mermaid dependency/state diagram from the persisted task graph

- **Priority:** P3  **Effort:** M  **Type:** feature
- **Rationale:** Competitor-parity legibility ask, not the wedge. Users want diagram visibility of state as a live generated artifact, not hand-drawn docs (eyaltoledano/claude-task-master#1377). anvil already ships `.mmd` diagrams as static assets; generating them from the live graph turns the persisted model into a legible shared artifact and reinforces the durable-state differentiator.
- **Acceptance:** `anvil graph --format mermaid` emits a valid Mermaid dependency (and/or task-status) diagram derived from the current task graph, deterministic for a given state. Output renders without syntax errors. Test: a fixture project with known deps produces a Mermaid graph containing the expected edges and node statuses.
- **Likely files:** `bin/src/anvil/cli/graph.py`, `bin/src/anvil/cli/__main__.py`, `bin/src/anvil/planning/_plan_helpers.py`, `bin/tests/test_cli.py`
- **Depends on:** B10

### B24 — Opt-in bidirectional GitHub-Issues projection as anti-lock-in positioning

- **Status:** Deferred — post-v1
- **Priority:** P3  **Effort:** L  **Type:** feature
- **Rationale:** Deferred post-v1. A READ-ONLY projection would deliver ~80% of the anti-lock-in value at far less risk than full bidirectional sync, so the bidirectional version is not worth the cost on the path to v1. Teams want a shared source of truth but resist lock-in; competitors declare centralized-backend/external-tracker sync out-of-scope for their OSS tier (eyaltoledano/claude-task-master#11/#91). anvil's design point is local-first SQLite + opt-in external projection — a clear positioning win to promote directly. The sync provider scaffolding (sync/provider.py, github-sync docs) already exists to extend.
- **Acceptance:** An opt-in projection syncs anvil tasks to/from GitHub Issues as a PROJECTION (local SQLite remains source of truth), with conflict-resolution strategy honored and no merge conflicts on a text file by construction. Disabled by default. Test (mocked GH API): tasks project to issues and a remote status change pulls back as a recorded transition without clobbering local state.
- **Likely files:** `bin/src/anvil/sync/provider.py`, `bin/src/anvil/sync/reconciliation.py`, `bin/src/anvil/cli/sync.py`, `bin/tests/test_github_issues_provider.py`
- **Depends on:** —

### B26 — Three-source drift command: report spec-vs-plan-vs-code divergence

- **Priority:** P1  **Effort:** M  **Type:** feature
- **Rationale:** White space no competitor solves and only anvil structurally can: the three-source reconciliation primitive (PRD spec, task plan, filesystem/git) already exists in the engine — surface it as a first-class `drift` command with machine-readable output. Divergence between what was specified, what was planned, and what was actually built is invisible in file-based tools; anvil can name it precisely.
- **Acceptance:** `anvil drift` (or `status --drift`) lists divergence between the PRD/spec, the task plan, and the filesystem-or-git, with machine-readable output (`--json`, B10). Each drift entry names the source-of-truth disagreement (e.g. requirement with no task, task with no matching code, code with no task). Tested on a seeded drift fixture: the command reports the injected divergence and exits clean on a non-drifted project.
- **Likely files:** `bin/src/anvil/cli/drift.py`, `bin/src/anvil/cli/__main__.py`, `bin/src/anvil/sync/reconciliation.py`, `bin/tests/test_cli.py`
- **Depends on:** B10

---

## E9 — WF-3 Substrate & Dogfooding Friction Follow-ups

Surfaced 2026-06-19 while building the WF-3 declarative workflow runner (PR #28) by dogfooding anvil on its own repo. Each item below carries a researched **Implementation trade-offs** block (options with cost + a recommendation). See also [B29](#b29--anvil-init-at-the-plugin-root-guidance-instead-of-a-bare-refusal-dogfooding-friction) (init guidance), the first item from the same dogfooding pass.

### B30 — `anvil doctor` check: verification commands whose paths don't resolve from the project root

- **Priority:** P2  **Effort:** S  **Type:** feature
- **Rationale:** A task's `**Verification:**` commands are stored as plain strings with no associated working directory and anvil never executes or validates them — they are recorded verbatim at submit and only substring-matched by the completion gate. With `bin/pyproject.toml` `testpaths = ["../tests"]`, a command written `pytest tests/foo.py` run from `bin/` resolves to the never-collected `bin/tests/` — so it can "pass" by hand yet never run in CI. This caused a real defect during WF-3 (a test landed in `bin/tests/` and would not have run in CI). Anvil has zero awareness of project test layout.
- **Acceptance:** `anvil doctor` flags any ready task whose verification command references a path (e.g. `tests/foo.py`) that does not exist relative to the project root; exit/report surfaces the task ID and the offending command. Tested with a task whose command points at a non-resolving path.
- **Likely files:** `bin/src/anvil/cli/doctor.py` (`_diagnose` `:185-227`), `bin/src/anvil/state/models.py` (`Verification` `:263-270`), `tests/test_cli.py`
- **Depends on:** —
- **Implementation trade-offs** (researched 2026-06-19):
  - Confirmed: commands are surfaced verbatim in the packet (`context/packets.py:301-304`), recorded verbatim at submit (`cli/packet_apply.py:333,363`), gated only by substring match (`review/gates.py:190-236`); the capture-evidence hook never reconciles declared-vs-run commands (`cli/hooks.py:149-209`); `doctor` has no path check.
  - **Opt 1 — path-resolution check (S):** flag verification paths that don't exist relative to project root. Catches the exact `bin/tests/` defect. Trade-off: false positives on cwd-relative or glob/marker-only commands; no notion of the agent's run dir.
  - **Opt 2 — parse `testpaths` and warn on `tests/` vs `../tests` mismatch (M):** precise. Trade-off: couples anvil to pytest config + the bin-subdir convention; brittle for non-pytest/non-uv projects; scope creep toward a test-runner model.
  - **Opt 3 — add optional `cwd` to `Verification` (M–L):** documents the run dir at the source. Trade-off: schema migration + three-file version bump + planner must populate; documents rather than detects existing bad entries.
  - **Recommendation:** Opt 1 now (cheap, real coverage, no pytest coupling); fold in Opt 2's `testpaths` hint only when pytest is detected; Opt 3 as later hardening.

### B31 — Programmatic engine write API: `EvidenceManager` / `ReviewManager` (mirror `ClaimManager`)

- **Priority:** P2  **Effort:** M  **Type:** refactor
- **Rationale:** Claim/release/renew are a reusable service (`ClaimManager`), but `evidence.submitted` and `task.applied` have no equivalent — their `EventDraft` payloads are hand-built inline in CLI command bodies. The same two payload shapes are now duplicated **3–4×** each: CLI (`cli/packet_apply.py`), MCP (`mcp_server.py`), and the WF-3 runner (`workflows/tasks.py`, which had to re-derive them by reading the CLI source). Any host/automation building on anvil (the WF-3 premise) pays this tax; the copies also risk gating logic diverging.
- **Acceptance:** Evidence-submit and apply have a reusable programmatic entry point (manager class or builder fns) that the CLI, MCP server, and workflow runner all call instead of hand-building drafts; existing CLI/MCP behavior unchanged; the WF-3 runner's hand-rolled payloads are removed in favor of it.
- **Likely files:** `bin/src/anvil/cli/packet_apply.py` (`:358-379`, `:739-754`), `bin/src/anvil/mcp_server.py` (`:1056-1075`, `:2623-2638`), `bin/src/anvil/workflows/tasks.py` (`:139-188`), `bin/src/anvil/state/payloads.py`, new `bin/src/anvil/review/` or `bin/src/anvil/evidence/` module
- **Depends on:** —
- **Implementation trade-offs** (researched 2026-06-19):
  - Confirmed: `ClaimManager` builds drafts at `claims/manager.py:459/567/658`; evidence/apply are inline at `packet_apply.py:358-379/739-754`, re-hand-rolled in `mcp_server.py:1056-1075/2623-2638` and `workflows/tasks.py:139-188`. The inline copies also carry non-payload logic (claim lookup, ownership guard, strict-evidence gate, `next_ready`) — only the draft-building is duplicated. All feed `SqliteBackend.append(EventDraft)` validating `EvidenceSubmittedPayload`/`TaskAppliedPayload`.
  - **Opt 1 — pure builder fns (`build_evidence_draft`/`build_apply_draft`) in `state/drafts.py` (S):** 4 call sites delegate; guards stay put. Trade-off: lowest regression risk but doesn't centralize gates — gating can still diverge.
  - **Opt 2 — `EvidenceManager`/`ReviewManager` mirroring `ClaimManager` (M):** real consolidation; gate logic moves out of CLI/MCP. Trade-off: moderate CLI-regression risk (strict-evidence + `next_ready` are woven into CLI control flow); new API surface to stabilize.
  - **Opt 3 — single `EngineService` facade (L):** cleanest long-term seam. Trade-off: highest churn/over-engineering now (YAGNI) for only two missing operations.
  - **Recommendation:** Opt 2 — matches the proven `ClaimManager` pattern and kills the 3–4× duplication; fall back to Opt 1 if CLI test churn is too high.

### B32 — Relax the evidence gate: allow empty `files_changed` when `commands_run` is non-empty

- **Priority:** P3  **Effort:** S  **Type:** modify
- **Rationale:** `evidence.submitted` rejects an empty `files_changed` list (`state/sqlite.py:3467-3470`), but a check-only / verification-only step legitimately changes no files. Callers must record a magic placeholder — the WF-3 runner uses `files = outcome.files_changed or ["(none)"]`. The constraint is an audit-honesty gate (a "done" with zero files *and* zero commands is unverifiable) but it over-fires: requiring *some* proof should mean commands **or** files, not files specifically. Origin is an inherited fakoli-state import (`f36ec8c`) with no documented rationale.
- **Acceptance:** `evidence.submitted` accepts an empty `files_changed` when `commands_run` is non-empty (still rejects when *both* are empty); existing `"(none)"`-style rows remain valid; the WF-3 runner's placeholder is removed. Tested both arms.
- **Likely files:** `bin/src/anvil/state/sqlite.py` (`_check_evidence_submitted` `:3463-3470`), `bin/src/anvil/workflows/runner.py` (`:189`), `tests/test_*`
- **Depends on:** —
- **Implementation trade-offs** (researched 2026-06-19):
  - Confirmed: the gate is a pre-mutation check in `_check_evidence_submitted`; the model layer is permissive (`payloads.py:319` defaults `files_changed=[]`). The `--files-changed` CLI flag is Typer-required; the `record-file-change` hook writes *separate* events, never auto-filling the submission payload — so empty is a real caller burden.
  - **Opt 1 — document + bless the `"(none)"` sentinel (S):** zero migration. Trade-off: keeps a magic string; a fake "done" can still pass by typing `"(none)"`.
  - **Opt 2 — require non-empty `commands_run` *or* `files_changed` (S):** one-line gate change; matches reality (a check step runs commands, changes nothing); honesty preserved (some proof still mandatory); pure loosening, no migration. Trade-off: existing `"(none)"` rows become legacy noise.
  - **Opt 3 — typed `no_files_changed: bool` field instead of a magic string (M):** most explicit for auditors. Trade-off: payload-schema change + migration/back-compat read for existing rows.
  - **Recommendation:** Opt 2 — removes the hack with no migration and no honesty loss.

### B33 — First-class non-PRD (workflow-origin) tasks: add a `tasks.origin` column and filter the PRD queue

- **Priority:** P2  **Effort:** S  **Type:** feature
- **Rationale:** Every task is forced to descend from a PRD feature (`feature_id TEXT NOT NULL REFERENCES features(id)`, `state/schema.py:112`), but WF-3 needs claimable tasks created outside the PRD→plan flow (one per workflow step / fan_out item). T003.1 worked around the FK with a sentinel feature `FWORKFLOW` + a `WT-` id prefix + an `implementation_notes` marker — but the marker is **dead weight**: `is_workflow_task` is never called outside `tasks.py`, and nothing filters origin, so workflow tasks share the `ready` pool and surface in `next_claimable`, `score`, and `list` (they pollute the PRD queue). WF-3's premise is non-PRD loops, so this should be first-class.
- **Acceptance:** Tasks carry an `origin` (`'prd' | 'workflow'`); `create_workflow_task` sets `'workflow'`; `next_claimable`, `score`, and `list` filter to `origin='prd'` by default (workflow tasks reachable via an explicit flag); the dead `is_workflow_task` marker is replaced by the column. Tested: a workflow task does not appear in `anvil next`/`list`/`score` for PRD work.
- **Likely files:** `bin/src/anvil/state/schema.py` (`:112`), `bin/src/anvil/state/sqlite.py` (migration block `:1195-1295`), `bin/src/anvil/state/models.py` (`Task` `:352`), `bin/src/anvil/claims/manager.py` (`next_claimable` `:154-201`), `bin/src/anvil/cli/plan.py` (`:740`, `:1372`), `bin/src/anvil/workflows/tasks.py`
- **Depends on:** —
- **Implementation trade-offs** (researched 2026-06-19):
  - Confirmed: migrations are `PRAGMA user_version`-gated (currently v5), additive `ALTER`s per branch. The `WT-` marker labels but excludes nowhere.
  - **Opt 1 — `origin` column, keep sentinel feature (S):** add `tasks.origin TEXT NOT NULL DEFAULT 'prd'`; filter `origin='prd'` in the 3 query sites. Migration v5→v6, one additive `ALTER … DEFAULT 'prd'` mirroring the existing `task_type` branch — backfill-safe, replay-safe; FK untouched (zero risk). Trade-off: sentinel feature lingers as cosmetic debt.
  - **Opt 2 — nullable `feature_id` + `origin`, drop sentinel (M):** cleanest model. Trade-off: SQLite can't drop `NOT NULL` via `ALTER` — needs a table-rebuild (create-copy-swap); weakens the FK invariant repo-wide (every `feature_id` consumer must handle `None`).
  - **Opt 3 — separate `workflow_tasks` table (L):** strongest isolation. Trade-off: forks claim/evidence/scoring code paths — large query/replay surface and ongoing duplication.
  - **Recommendation:** Opt 1 — additive column + filter; lowest cost/risk, makes the existing dead marker actually load-bearing.

---

## E10 — Framework Integration & Cross-Harness Compatibility

The strategic thesis made concrete: anvil is the **governed state-of-record beneath any agent runtime**, not an agent framework. The more runtimes it can govern *without reimplementing them*, the more that claim is proven. Vercel's **eve** (open-source agent framework, 2026-06-17) independently shipped anvil's own bets — durable execution, evals, human-in-the-loop approvals, subagents, replayable structured events — which confirms the layer is real and that eve sits *above* anvil (a framework to build one agent in), not against it. The right response is breadth + honesty, **not** importing eve's (or any vendor's) feature set or deployment coupling.

### B34 — Broaden the runtime/framework adapter family (Vercel eve + others)

- **Priority:** P2  **Effort:** M  **Type:** feature
- **Rationale:** WF-2 shipped thin loop adapters (`packaging/loops/ci-drain.sh`, `packaging/loops/claude-loop.md`, `packaging/loops/codex-automation.md`) that drive the governed lifecycle (`anvil next -q` → claim → packet → work → submit → apply) from each runtime. eve — and the broader field (LangChain, OpenAI/Claude Agent SDKs) — are exactly the runtimes anvil should *govern*, not compete with. An eve adapter (eve agents as executors, anvil as the audited queue they coordinate through) demonstrates runtime-neutrality and is a natural fit: eve already has per-session durability + approvals, but **lacks anvil's wedge** — multi-agent single-winner leases and evidence-gated (not self-reported) completion. The adapter stays THIN: it drives the existing seam + body, adds no engine change, and couples anvil to no vendor deployment model.
- **Acceptance:** A committed eve adapter under `packaging/loops/` drives one governed task per invocation through anvil's transitions (mirroring `codex-automation.md`); `docs/how-to/drive-the-anvil-loop.md` lists it. No engine change. A second new-framework adapter (or a documented "copy this pattern" recipe) shows the pattern generalizes.
- **Design inputs from the eve teardown** (capture so they are not lost — these inform OTHER specs, they are not eve-imports): (1) eve's `needsApproval: ({toolInput}) => …` predicate-gate generalizes anvil's **score-driven routing** — a gate is a function of the action's params; fold into the routing PRD (`docs/specs/2026-06-19-ergonomics-unattended.md` §5). (2) eve's filesystem-first "a file's place *is* its definition" is an input for **WF-3's format** (steps as a directory vs one YAML).
- **Likely files:** `packaging/loops/eve-*.md`, `docs/how-to/drive-the-anvil-loop.md`, `docs/research/agent-workflow-formats.md`
- **Depends on:** — (WF-1/WF-2 shipped)

### B35 — Harness compatibility matrix: track + publish which runtimes anvil drives, and how

- **Priority:** P2  **Effort:** S–M  **Type:** feature
- **Rationale:** As the adapter family grows (B34), the runtime-neutral claim needs a single, **maintained, honest** surface: which harnesses anvil integrates with, at what level (MCP tools / CLI / loop-adapter), and how each interacts with the governed lifecycle (drain / fire-once / in-session). This is both an outward proof of breadth and an internal truth-tracker so the claim can't drift from reality — anvil should *know* its own compatibility, not merely assert it.
- **Acceptance:** A committed compatibility matrix (in `docs/`, surfaced via `AGENTS.md`/`README`) lists each supported harness × integration surface × interaction model × status (supported / experimental / planned). A check keeps it honest — e.g. every adapter in `packaging/loops/` has a matrix row, and every harness marked "supported" via MCP maps to a real tool. Optionally an `anvil compat` command prints it.
- **Likely files:** `docs/harness-compatibility.md`, `AGENTS.md`, `README`, a small test asserting matrix↔reality
- **Depends on:** B34 (grows with the adapter family)

### B36 — Cross-harness performance benchmark (STRETCH — blocked on harness access)

- **Priority:** P3  **Effort:** XL  **Type:** infra  **Status:** STRETCH / BLOCKED-ON-ACCESS
- **Rationale:** The strongest proof of runtime-neutrality is *measured*: run the same governed workload (e.g. the concurrency-suite shape, or a fixed task queue drained) across multiple harnesses and compare correctness (zero double-claims, zero lost evidence), throughput, and token cost — turning "works on any harness" from a claim into a number. **Honest constraint:** large undertaking, blocked on access to the different harnesses (we do not have them all). Scope down first to a benchmark *interface* (the shape of the measurement) that any single runtime can run, so contributors with access fill in rows over time — rather than one team needing every harness at once.
- **Acceptance:** (when unblocked) a benchmark spec + a runnable harness measuring a fixed governed workload's correctness + throughput on at least one runtime, structured so other runtimes are drop-in rows. Until then: the spec + a single-runtime baseline, with the access blocker named.
- **Likely files:** `bin/benchmarks/` (cross-harness), a `docs/specs/` benchmark spec
- **Depends on:** B34, B35; **external blocker:** access to the target harnesses

---

## E11 — Backlog Generation & Management (first-class, governed)

The full research is in **[`docs/research/2026-06-19-backlog-management-research.md`](../research/2026-06-19-backlog-management-research.md)** (22-agent deep-research workflow: 5-lane landscape → demand sweep → adversarial verification → synthesis). Headline: **no tool autonomously manages a cross-session "what's left" backlog** — Codex Memories *disclaims* it, Cursor *removed* it, and **Height (the one product that built autonomous whole-backlog grooming) shut down Sep 2025**. anvil's moat maps onto the gap (cross-session `next` awareness, evidence-gated *promotion*, the only reference-class-forecasting corpus = its immutable ledger, markdown↔state-DB duality, insight→item→task→evidence traceability). **Guardrail: stay the governed substrate UNDER the loop — not a PM platform (Height's grave); dedup/re-rank ship as evidence-backed `decisions` suggestions, never silent mutations.**

The build items (the `backlog_item` schema node, the `/anvil:ideate` loop, the Definition-of-Ready gate, `anvil backlog capture/next/promote/sync/dedup/rerank`, the MCP surface, the benchmark) are **deliberately not pre-authored here** — they are defined by B37's spec session, since they depend on the open product forks the research left.

### B37 — Spec the backlog-management feature via structured Q&A → PRD

- **Priority:** P1  **Effort:** M  **Type:** feature (design)  **Status:** NEXT — drive in a dedicated session
- **Rationale:** The research (full brief: [`docs/research/2026-06-19-backlog-management-research.md`](../research/2026-06-19-backlog-management-research.md)) found verified white space and a defensible wedge, but left genuine product forks that must be decided before building. This is the design step: drive the structured Q&A — the *same* ideation→item loop the feature itself describes (dogfooding) — to resolve the forks and author a PRD/spec, from which the E11 build items get authored.
- **Open forks to resolve in the Q&A** (research §7, item 7 "Open product questions"): (a) is `backlog_item` a new root above PRDs, or a node beside `prds` under `projects`? (b) the **benchmark** that proves it beats "a folder of markdown" (traceability completeness / dedup precision / re-prioritization stability — SL-2-harness-style); (c) the automation-vs-human-gated boundary (DoR strictness; dedup/re-rank as suggestions only); (d) scope-first: capture+ideate loop only, or also ongoing dedup/re-rank/sync; (e) dedup-link representation (self-referential FK vs `conflict_groups` reuse); (f) does `promote` create one PRD fragment per item or batch related items?
- **Guardrails (do not regress):** governed substrate, *not* a PM platform (Height died); `capture` stays one-command-cheap or people route back to scratch files; dedup/re-rank = appended `decisions` suggestions, never silent auto-merge; additive-only schema migration (version-bump-in-lockstep); a benchmark is required (HN distrusts unmeasured claims).
- **Acceptance:** a committed PRD/spec in `docs/specs/` that resolves the open forks, defines the `backlog_item` schema (additive migration), the ideation→item loop, the DoR gate, the markdown↔state-DB sync, and the benchmark; the E11 build items (B38+) authored from it.
- **Likely files:** `docs/specs/<dated>-backlog-management.md`, `docs/backlog/anvil-backlog.md` (E11 build items)
- **Depends on:** — (research complete; this is the design step)
- **Reference:** the detailed synopsis — [`docs/research/2026-06-19-backlog-management-research.md`](../research/2026-06-19-backlog-management-research.md).

---

## Sequencing Note

The critical path to a standalone v1, in order:

1. **B01 + B02 + B03** (the two engine bugs + the concurrency regression suite) — prove the lease is correct before selling it; single-winner claims are the wedge and nothing downstream matters if they are not provably correct.
2. **B10** (`--json`, now P0) — the real standalone product surface; it gates the doctor, drift, next-ready, and graph items, so non-Claude hosts and scripts can drive the engine.
3. **B25** (enforceable evidence gate, new P1) — make "done" trustworthy; refusing completion without evidence addresses the wedge's most-felt pain ("agents lie about done").
4. **B09** (token self-audit) — earn the context-frugality claim before marketing it; cheap (S) and it backs the positioning the launch leans on.
5. **B07** (`ANVIL_ROOT`) + the thin **B11** accessor — unblock container/host portability so CLI and MCP agree on the project root, and surface the already-existing schema version to tooling.

After the critical path, schedule the remaining differentiated bets — **B26** (three-source drift, P1) once `--json` lands; **B16** (promote in-init migration, P1, no longer blocked on new schema infrastructure); the standalone-onboarding polish (**B04**, **B06**, **B27**); then the XL brownfield front door (**B13**, P2) once the engine is trustworthy and scriptable; and finally the parity/positioning items (**B23** Mermaid, **B28** structured acceptance grammar) and the deferred post-v1 work (**B24**, **B22**). Every item that touches a plugin file must bump `.claude-plugin/plugin.json` and regenerate the registry per repo CLAUDE.md rules.
