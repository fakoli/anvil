# Project: fakoli-state backlog

## Summary

fakoli-state is the durable, runtime-neutral state-of-record for AI-and-human software work: a local-first SQLite store where every requirement, task, claim, and piece of evidence is an additive, in-place transition rather than a regenerated template or an unverified agent self-report. This backlog hardens the concurrency-critical claim/lease core, delivers standalone (crew/flow-free) onboarding and a machine-readable programmatic surface, opens a brownfield scan/ingest front door, and closes the verification-feedback and decision-back-propagation loops that no competing tool (spec-kit, task-master, BMAD, spec-workflow) solves. The work is derived from analysis of 81 top-engagement competitor issues, prioritizing the structural differentiators competitors cannot match without abandoning their file-based models.

## Goals

- Make the claim/lease single-winner guarantee provably correct under real parallelism.
- Let a new user reach a ready task end-to-end with zero crew/flow dependency in one command.
- Expose stable, schema-versioned machine-readable output so any MCP/ACP host or script can drive the engine.
- Cement runtime/container portability so CLI and MCP always agree on the project root and command surface.
- Open a brownfield scan/ingest path covering the underserved 75% of real work (bugfix/refactor/modify), not just greenfield.
- Ship upgrade-safe schema/state migration and a global-config layer so engine updates never clobber per-project user data.
- Close the verification-feedback and decision-back-propagation loops, and project state legibly to diagrams and external trackers without lock-in.

## Requirements

- R001: The claim transaction must enforce file-overlap exclusion atomically so two file-overlapping tasks can never both be claimed, with a standing concurrency regression suite proving single-winner under N threads.
- R002: Configured lease/heartbeat values must be honored on every code path (CLI and MCP) and accept fractional minutes without silent loss.
- R003: A new user must be able to run init→PRD→plan→next and reach a ready task with no crew/flow installed, supported by self-sufficient docs and a health-diagnosis command.
- R004: CLI and MCP must resolve the same project root across host/container divergence, self-describe a version-pinned command surface, and keep the plugin's always-loaded token footprint within an audited budget.
- R005: Read commands must emit stable, schema-versioned, paginated JSON; on-disk state must carry an authoritative schema version; and completion responses must name the next ready task.
- R006: The engine must ingest an existing repo into a draft PRD + re-scannable codebase model and carry non-feature task types (bugfix/refactor/modify) through the full loop, right-sizing process by score.
- R007: On-disk `.fakoli-state` artifacts must migrate cleanly across engine versions, merge a global-config layer under project overrides, and be installable via the Docker MCP catalog.
- R008: Deferred/failed-review evidence must be queryable and surfaced on file overlap; decisions must back-propagate to the PRD; dependency edits must batch atomically; and cross-agent contract fields must be enforceable by review gates.
- R009: Persisted task state must be projectable to an auto-generated Mermaid diagram and to an opt-in bidirectional GitHub-Issues projection while local SQLite remains the source of truth.

## Features

### F001: Engine Reliability & Concurrency Correctness
**Requirements:** R001, R002

### F002: Standalone Onboarding & First-Run Self-Sufficiency
**Requirements:** R003

### F003: Portability & Runtime Neutrality
**Requirements:** R004

### F004: Machine-Readable Output & Programmatic Surface
**Requirements:** R005

### F005: Brownfield Onboarding & Task-Type Coverage
**Requirements:** R006

### F006: Distribution, Migration & Global Config
**Requirements:** R007

### F007: Verification Feedback Loop & Decision Back-Propagation
**Requirements:** R008

### F008: Legible Shared Model & External Projection
**Requirements:** R009

## Tasks

### T001: Fix TOCTOU file-overlap race: re-check expected_files overlap inside the claim transaction
**Feature:** F001
**Priority:** high
**Likely files:** bin/src/fakoli_state/state/sqlite.py, bin/src/fakoli_state/claims/manager.py, bin/src/fakoli_state/state/payloads.py, bin/tests/test_claims.py
**Dependencies:**

**Acceptance criteria:**
- The cross-task overlap re-check runs inside the SAME BEGIN IMMEDIATE transaction that inserts the claim (_check_claim_created in state/sqlite.py) — not in a separate read before the transaction.
- Within that transaction, active claims for the same project are queried and the claim is rejected (EventRejected) if any active claim overlaps the incoming claim for a different task, covering BOTH raw expected_files overlap AND the conflict-group path (today _check_claim_created checks neither, only the task's own status).
- The ConflictWarning path in manager.check_conflicts remains as a fast pre-check but is no longer the sole guard.
- Two threads claiming two distinct file-overlapping tasks result in exactly one success and one conflict error, with 0% double-claim across at least 200 iterations; a parallel test asserts the same single-winner guarantee for two distinct tasks in the same conflict group.

**Verification:**
- `cd bin && uv run pytest tests/test_claims.py -k 'overlap or conflict_group' -q`

### T002: Thread config default_lease_minutes into the CLI ClaimManager and accept fractional minutes
**Feature:** F001
**Priority:** high
**Likely files:** bin/src/fakoli_state/cli/claim.py, bin/src/fakoli_state/config.py, bin/tests/test_cli.py, bin/tests/test_config.py
**Dependencies:**

**Acceptance criteria:**
- cli/claim.py loads config.yaml and passes default_lease_minutes and default_heartbeat_minutes into ClaimManager.
- config.py accepts fractional minutes (e.g. 0.5) via validated float coercion, rejecting only non-numeric/negative/boolean values.
- A project config with default_lease_minutes 30 produces a CLI claim whose lease_expires_at is created_at plus 30 minutes (not 60); a config of 0.5 yields a 30-second lease; the MCP path is unchanged.

**Verification:**
- `cd bin && uv run pytest tests/test_cli.py tests/test_config.py -k lease -q`

### T003: Concurrency regression suite: single-winner under N threads
**Feature:** F001
**Priority:** high
**Likely files:** bin/tests/test_claims_concurrency.py, bin/tests/conftest.py, bin/src/fakoli_state/claims/manager.py, bin/src/fakoli_state/state/sqlite.py
**Dependencies:** T001

**Acceptance criteria:**
- A pytest module spins at least 8 real threads against one shared state.db and asserts exactly one winner for same-task contention (N-1 get ClaimError), exactly one winner for two file-overlapping distinct tasks, and at most one member of a declared conflict group claimed concurrently.
- Each scenario runs at least 200 iterations with 0 double-claims and is wired into CI.
- The suite fails on main if T001 or T002 are reverted.

**Verification:**
- `cd bin && uv run pytest tests/test_claims_concurrency.py -q`

### T004: One-command standalone quickstart: init --with-sample seeds a runnable PRD-to-next loop
**Feature:** F002
**Priority:** medium
**Likely files:** bin/src/fakoli_state/cli/init_status.py, bin/src/fakoli_state/cli/__main__.py, bin/tests/test_cli.py, plugins/fakoli-state/README.md
**Dependencies:**

**Acceptance criteria:**
- `fakoli-state init --with-sample` creates config plus a valid sample prd.md and runs parse, plan, and score so `fakoli-state next` returns a ready task with no further input.
- Without the flag, init behavior is unchanged.
- README Quick Start shows the zero-to-next path with no crew/flow installed.

**Verification:**
- `cd bin && uv run pytest tests/test_cli.py -k with_sample -q`

### T005: FAKOLI_STATE_ROOT env override and stable root resolution across container/host
**Feature:** F003
**Priority:** high
**Likely files:** bin/src/fakoli_state/cli/_helpers.py, bin/src/fakoli_state/config.py, bin/src/fakoli_state/mcp_server.py, bin/tests/test_cli.py, bin/tests/test_mcp.py
**Dependencies:**

**Acceptance criteria:**
- Both CLI (_resolve_state_dir) and the MCP server honor FAKOLI_STATE_ROOT when set, resolving to that path's .fakoli-state before any cwd walk; when unset, behavior is unchanged.
- Precedence is documented: explicit --cwd/arg, then FAKOLI_STATE_ROOT, then cwd walk.
- With FAKOLI_STATE_ROOT pointed at a fixture project from an unrelated cwd, CLI next and the MCP get_next_task tool resolve the same state.db.

**Verification:**
- `cd bin && uv run pytest tests/test_cli.py tests/test_mcp.py -k state_root -q`

### T006: Stable, schema-versioned --json output across read commands
**Feature:** F004
**Priority:** high
**Likely files:** bin/src/fakoli_state/cli/__main__.py, bin/src/fakoli_state/cli/_helpers.py, bin/src/fakoli_state/context/packets.py, schemas/cli-output.schema.json, bin/tests/test_cli.py
**Dependencies:**

**Acceptance criteria:**
- This is the real standalone product surface (priority P0) for non-Claude hosts; it is confirmed absent today and gates the doctor (T010), drift (T025), next-ready (T014), and graph (T019) items.
- `fakoli-state next`, `list`, `status`, `show <task>`, and `review tasks` accept --json and emit output validated against a versioned JSON schema (schema_version field) under schemas/.
- JSON output is deterministically ordered across runs, and list/next support --limit and --offset pagination.
- A round-trip test parses each command's --json output with the published schema; default human output is unchanged.

**Verification:**
- `cd bin && uv run pytest tests/test_cli.py -k json_output -q`

### T007: Expose the existing SCHEMA_VERSION (=4) and _check_schema_version to tooling
**Feature:** F004
**Priority:** medium
**Likely files:** bin/src/fakoli_state/state/sqlite.py, bin/src/fakoli_state/cli/init_status.py, bin/tests/test_sqlite.py
**Dependencies:**

**Acceptance criteria:**
- This is a thin accessor, not new infrastructure: SCHEMA_VERSION = 4 and the migration branches (0/1→4, 2→4, 3→4) already exist in state/sqlite.py.
- A public accessor returns the existing SCHEMA_VERSION (=4), and status plus --json (T006) output surface the on-disk schema version and any mismatch.
- Opening a db with a newer schema_version than the engine supports continues to raise a clear, actionable error via the existing _check_schema_version (not a silent partial read).
- A test asserts the accessor returns the constant, that status/--json include the version, and that a forward-incompatible version is rejected with upgrade guidance.

**Verification:**
- `cd bin && uv run pytest tests/test_sqlite.py -k schema_version -q`

### T008: Brownfield scan/ingest: seed an initial PRD and task graph from an existing repo
**Feature:** F005
**Priority:** medium
**Likely files:** bin/src/fakoli_state/cli/scan.py, bin/src/fakoli_state/planning/inference.py, bin/src/fakoli_state/sync/reconciliation.py, bin/src/fakoli_state/state/schema.py, bin/tests/test_scan.py
**Dependencies:** T007

**Acceptance criteria:**
- `fakoli-state scan` (or `init --from-repo`) walks the existing working tree, produces a draft prd.md plus an initial feature/task graph, and persists a re-scannable codebase model in SQLite.
- Re-running scan reconciles against the persisted model and reports the delta rather than overwriting.
- An integration test runs scan on a fixture repo and asserts a non-empty draft PRD plus tasks plus a queryable codebase model row set.

**Verification:**
- `cd bin && uv run pytest tests/test_scan.py -q`

### T009: Promote the existing in-init schema migration to an explicit migrate state command
**Feature:** F006
**Priority:** high
**Likely files:** bin/src/fakoli_state/cli/migrate.py, bin/src/fakoli_state/state/sqlite.py, bin/tests/test_snapshot.py, plugins/fakoli-state/docs/migrations.md
**Dependencies:**

**Acceptance criteria:**
- Schema migration ALREADY EXISTS inside state/sqlite.py (the branches 0/1→4, 2→4, 3→4 run automatically at init); migrate.py only migrates the events.jsonl format. The work is to promote the existing in-init migration to an explicit, backed-up, dry-run `migrate state` command with an active-claim guard, not to build a migration framework.
- `fakoli-state migrate state` detects the on-disk schema_version (via the T007 accessor), runs the existing ordered idempotent forward migration branches up to the current engine version, runs dry-run by default with --yes to apply, and backs up state.db before mutating.
- Migration refuses while claims are active (same guard as migrate-events).
- A fixture db at an older version migrates to the current version with all rows preserved, replay still passes, and re-running migrate is a no-op.

**Verification:**
- `cd bin && uv run pytest tests/test_snapshot.py -k migrate -q`

### T010: fakoli-state doctor: diagnose state, config, lease, and reconciliation health
**Feature:** F002
**Priority:** medium
**Likely files:** bin/src/fakoli_state/cli/doctor.py, bin/src/fakoli_state/cli/__main__.py, bin/src/fakoli_state/sync/reconciliation.py, bin/tests/test_cli.py
**Dependencies:** T006

**Acceptance criteria:**
- `fakoli-state doctor` reports state.db reachability and schema version, config parse status and effective lease/heartbeat values, active/stale claim counts, replay integrity, and a git/fs/db reconciliation drift summary, exiting non-zero on any ERROR-level finding.
- doctor supports --json.
- Tested against a healthy project (clean exit) and a project with an injected stale claim plus schema mismatch (non-zero, both findings listed).

**Verification:**
- `cd bin && uv run pytest tests/test_cli.py -k doctor -q`

### T011: Self-sufficient docs: standalone Getting Started that assumes no crew/flow installed
**Feature:** F002
**Priority:** medium
**Likely files:** plugins/fakoli-state/README.md, plugins/fakoli-state/docs/how-to/getting-started.md, plugins/fakoli-state/docs/_positioning.md
**Dependencies:** T004

**Acceptance criteria:**
- docs/how-to/getting-started.md and README lead with a crew/flow-free walkthrough (init, prd, plan, claim, execute, finish via CLI/MCP only).
- An explicit "Optional: fakoli-flow / fakoli-crew integration" section is clearly marked as additive.
- A docs check confirms the standalone path references no crew/flow command as a required step.

**Verification:**
- `grep -L -e 'flow:' -e 'crew:' plugins/fakoli-state/docs/how-to/getting-started.md`

### T012: Version-pin and self-describe the CLI/MCP command surface
**Feature:** F003
**Priority:** medium
**Likely files:** bin/src/fakoli_state/cli/__main__.py, bin/src/fakoli_state/mcp_server.py, bin/src/fakoli_state/cli/describe.py, bin/tests/test_cli.py
**Dependencies:**

**Acceptance criteria:**
- `fakoli-state --version` reports engine plus schema versions, and `fakoli-state describe` (or an MCP capability) emits a machine-readable manifest of CLI subcommands and MCP tool names with a stable api_version field.
- A test asserts the described surface matches the registered Typer commands and FastMCP tools (no drift).
- CI fails if a command is added or renamed without updating the manifest.

**Verification:**
- `cd bin && uv run pytest tests/test_cli.py -k describe -q`

### T013: Self-audit plugin token footprint: keep skills/commands context-frugal
**Feature:** F003
**Priority:** high
**Likely files:** plugins/fakoli-state/skills/*/SKILL.md, plugins/fakoli-state/tests/test_token_budget.py, plugins/fakoli-state/docs/architecture.md
**Dependencies:**

**Acceptance criteria:**
- A CI check measures the combined token footprint of always-loaded skill frontmatter/SKILL.md and the command surface, and fails if it exceeds an explicit budget set from the current measured baseline with headroom.
- Any SKILL.md over the per-file ceiling is flagged, and the report shows per-skill token counts.
- The budget is documented in docs/architecture.md or a new docs/context-budget.md.

**Verification:**
- `cd plugins/fakoli-state && uv run pytest tests/test_token_budget.py -q`

### T014: get_next_task / finish responses explicitly name the next ready task
**Feature:** F004
**Priority:** medium
**Likely files:** bin/src/fakoli_state/mcp_server.py, bin/src/fakoli_state/cli/__main__.py, bin/src/fakoli_state/claims/manager.py, bin/tests/test_mcp.py
**Dependencies:** T006

**Acceptance criteria:**
- submit_completion_evidence / finish (CLI and MCP) include a next_ready field naming the next claimable task (respecting deps, claims, and file-conflict exclusions) or null when none is available.
- The field appears in both --json and MCP responses.
- After finishing task A, the response names the correct next ready task and excludes any task whose files overlap an active claim.

**Verification:**
- `cd bin && uv run pytest tests/test_mcp.py -k next_ready -q`

### T015: Add non-feature task types (bugfix / refactor / modify) through the PRD-to-claims loop
**Feature:** F005
**Priority:** medium
**Likely files:** bin/src/fakoli_state/state/models.py, bin/src/fakoli_state/planning/template.py, bin/src/fakoli_state/planning/scoring.py, bin/src/fakoli_state/context/packets.py, bin/tests/test_models.py
**Dependencies:** T008

**Acceptance criteria:**
- The task model and planner support a task_type enum (feature, bugfix, refactor, modify) that flows through plan, score, claim, work-packet rendering, and evidence.
- list/next can filter by task_type via --json, and the six-dimension score routes low-complexity/low-blast tasks to a lightweight work-packet variant.
- A PRD with a bugfix item produces a typed task that claims, executes, and submits evidence end-to-end.

**Verification:**
- `cd bin && uv run pytest tests/test_models.py -k task_type -q`

### T016: Global-config layer (~/.config/fakoli-state) with project-override precedence
**Feature:** F006
**Priority:** medium
**Likely files:** bin/src/fakoli_state/config.py, bin/src/fakoli_state/cli/_helpers.py, bin/tests/test_config.py, plugins/fakoli-state/docs/cli-reference.md
**Dependencies:** T002

**Acceptance criteria:**
- Config loading merges ~/.config/fakoli-state/config.yaml (global defaults) under project .fakoli-state/config.yaml (overrides), with precedence: explicit CLI arg, then project config, then global config, then built-in default.
- FAKOLI_STATE_ROOT and lease values participate in this precedence.
- A global default lease of 45 is overridden to 30 by a project config and to 15 by a CLI flag.

**Verification:**
- `cd bin && uv run pytest tests/test_config.py -k global_config -q`

### T017: Surface deferred/failed-review evidence back into planning on file overlap
**Feature:** F007
**Priority:** medium
**Likely files:** bin/src/fakoli_state/review/gates.py, bin/src/fakoli_state/context/packets.py, bin/src/fakoli_state/claims/manager.py, bin/tests/test_review.py
**Dependencies:**

**Acceptance criteria:**
- Deferred/failed review findings are stored as queryable evidence records linked to the files they touched.
- When a new task is claimed or planned whose expected_files overlap a prior deferred finding, the work packet / claim response surfaces those findings.
- Deferring a finding on file X and then claiming a later task touching file X makes the prior finding appear in the work packet.

**Verification:**
- `cd bin && uv run pytest tests/test_review.py -k deferred_overlap -q`

### T018: Decision back-propagation: persist decisions that back-reference and update the PRD
**Feature:** F007
**Priority:** medium
**Likely files:** bin/src/fakoli_state/planning/decisions.py, bin/src/fakoli_state/cli/prd.py, bin/src/fakoli_state/state/transitions.py, bin/tests/test_decisions.py
**Dependencies:**

**Acceptance criteria:**
- Decisions recorded during planning/execution can carry a prd_ref and, on resolution, write back a recorded transition that updates the referenced PRD section without overwriting unrelated content.
- Resolving a [NEEDS DECISION] marker updates the linked PRD requirement.
- The change is an additive recorded transition visible in the event log.

**Verification:**
- `cd bin && uv run pytest tests/test_decisions.py -k backprop -q`

### T019: Auto-emit a Mermaid dependency/state diagram from the persisted task graph
**Feature:** F008
**Priority:** low
**Likely files:** bin/src/fakoli_state/cli/graph.py, bin/src/fakoli_state/cli/__main__.py, bin/src/fakoli_state/planning/_plan_helpers.py, bin/tests/test_cli.py
**Dependencies:** T006

**Acceptance criteria:**
- `fakoli-state graph --format mermaid` emits a valid Mermaid dependency (and/or task-status) diagram derived from the current task graph, deterministic for a given state.
- Output renders without syntax errors.
- A fixture project with known deps produces a Mermaid graph containing the expected edges and node statuses.

**Verification:**
- `cd bin && uv run pytest tests/test_cli.py -k graph_mermaid -q`

### T020: Right-size process by score: fast-lane work packets for trivial changes
**Feature:** F005
**Priority:** low
**Likely files:** bin/src/fakoli_state/context/packets.py, bin/src/fakoli_state/planning/scoring.py, bin/src/fakoli_state/config.py, bin/tests/test_context.py
**Dependencies:** T015

**Acceptance criteria:**
- Tasks scoring below configurable complexity/blast thresholds render a minimal work packet (fewer required evidence fields, single-step) while still recording a completion-evidence transition.
- The threshold is config-driven.
- A trivial-scored task completes via the fast-lane and still produces an immutable evidence record; a high-blast task still requires the full packet.

**Verification:**
- `cd bin && uv run pytest tests/test_context.py -k fast_lane -q`

### T021: Publish the FastMCP stdio server to the Docker MCP catalog
**Feature:** F006
**Priority:** low
**Likely files:** plugins/fakoli-state/Dockerfile, plugins/fakoli-state/bin/fakoli-state-mcp, plugins/fakoli-state/docs/mcp.md
**Dependencies:** T005

**Acceptance criteria:**
- A Dockerfile plus catalog manifest package the fakoli-state-mcp stdio server with FAKOLI_STATE_ROOT bind-mount support documented.
- The image starts the MCP server and a smoke test connects and lists tools.
- Publishing steps are documented in docs/mcp.md.

**Verification:**
- `docker build -t fakoli-state-mcp plugins/fakoli-state && docker run --rm fakoli-state-mcp --help`

### T022: Batch dependency-edit primitive (multi-source/multi-target in one call)
**Feature:** F007
**Priority:** low
**Likely files:** bin/src/fakoli_state/cli/plan.py, bin/src/fakoli_state/mcp_server.py, bin/src/fakoli_state/planning/_plan_helpers.py, bin/tests/test_cli_plan.py
**Dependencies:**

**Acceptance criteria:**
- A CLI command and MCP tool accept a batch of dependency edges (add/remove, multiple sources/targets) applied as one transaction with cycle detection that rejects the whole batch on any invalid edge.
- A batch of 10 edges applies atomically.
- A batch introducing a cycle is rejected with no partial application.

**Verification:**
- `cd bin && uv run pytest tests/test_cli_plan.py -k batch_deps -q`

### T023: Structured contract/schema fields per task, enforced by review gates (deferred)
**Feature:** F007
**Priority:** low
**Likely files:** bin/src/fakoli_state/state/models.py, bin/src/fakoli_state/review/gates.py, bin/src/fakoli_state/planning/template.py, bin/tests/test_review.py
**Dependencies:**

**Acceptance criteria:**
- Tasks support optional structured contract fields (e.g. interface/schema reference) that are required and enforced by the review_tasks gate when a task is marked as a cross-agent interface.
- A task flagged as an interface with no contract is blocked at review.
- Supplying a valid contract promotes the task to ready.

**Verification:**
- `cd bin && uv run pytest tests/test_review.py -k contract_field -q`

### T024: Opt-in bidirectional GitHub-Issues projection as anti-lock-in positioning (deferred)
**Feature:** F008
**Priority:** low
**Likely files:** bin/src/fakoli_state/sync/provider.py, bin/src/fakoli_state/sync/reconciliation.py, bin/src/fakoli_state/cli/sync.py, bin/tests/test_github_issues_provider.py
**Dependencies:**

**Acceptance criteria:**
- An opt-in projection syncs fakoli-state tasks to/from GitHub Issues as a projection with local SQLite remaining the source of truth, honoring a conflict-resolution strategy and producing no text-file merge conflicts.
- The projection is disabled by default.
- With a mocked GitHub API, tasks project to issues and a remote status change pulls back as a recorded transition without clobbering local state.

**Verification:**
- `cd bin && uv run pytest tests/test_github_issues_provider.py -q`

### T025: Enforceable evidence gate: refuse completion in strict mode when required evidence is absent
**Feature:** F007
**Priority:** high
**Likely files:** bin/src/fakoli_state/review/gates.py, bin/src/fakoli_state/cli/__main__.py, bin/src/fakoli_state/mcp_server.py, bin/src/fakoli_state/config.py, bin/tests/test_review.py
**Dependencies:**

**Acceptance criteria:**
- Today the completion-evidence gate is advisory (it flags missing evidence but does not block); making it able to refuse addresses the most-felt pain in the dataset ("agents lie about done", task-master#181).
- A strict_evidence: true config (or --strict flag) causes submit_completion_evidence / apply to reject a completion missing required evidence or required verification commands, with an actionable error naming what is absent.
- The default remains advisory (flags but does not block) for back-compat.
- Tested both ways: a strict-mode submit missing required evidence is rejected; the same submit in default advisory mode is accepted with a warning.

**Verification:**
- `cd bin && uv run pytest tests/test_review.py -k strict_evidence -q`

### T026: Three-source drift command: report spec-vs-plan-vs-code divergence
**Feature:** F008
**Priority:** high
**Likely files:** bin/src/fakoli_state/cli/drift.py, bin/src/fakoli_state/cli/__main__.py, bin/src/fakoli_state/sync/reconciliation.py, bin/tests/test_cli.py
**Dependencies:** T006

**Acceptance criteria:**
- White space no competitor solves and only fakoli-state structurally can: the three-source reconciliation primitive already exists and is surfaced here as a first-class command.
- `fakoli-state drift` (or status --drift) lists divergence between the PRD/spec, the task plan, and the filesystem-or-git, with machine-readable output (--json, T006).
- Each drift entry names the source-of-truth disagreement (e.g. requirement with no task, task with no matching code, code with no task).
- Tested on a seeded drift fixture: the command reports the injected divergence and exits clean on a non-drifted project.

**Verification:**
- `cd bin && uv run pytest tests/test_cli.py -k drift -q`

### T027: Caller-supplied / existing-branch claims: attach a claim to a named branch
**Feature:** F002
**Priority:** medium
**Likely files:** bin/src/fakoli_state/cli/claim.py, bin/src/fakoli_state/claims/manager.py, bin/src/fakoli_state/config.py, bin/tests/test_cli.py
**Dependencies:**

**Acceptance criteria:**
- Today claim always imposes its own agent/<task>-<slug> branch; allowing an existing/named branch is a competitor adoption lever (research #232) that meets users where their git workflow already is.
- `claim --branch <name>` (or a config equivalent) attaches the claim to an existing or caller-named branch instead of generating agent/<task>-<slug>.
- Default behavior (auto-generated branch) is unchanged when the option is absent.
- Tested: claiming with --branch existing-feature records the claim against that branch, and a claim without the option still generates the default branch name.

**Verification:**
- `cd bin && uv run pytest tests/test_cli.py -k claim_branch -q`

### T028: Structured acceptance grammar (EARS/Gherkin) in the PRD parser
**Feature:** F007
**Priority:** low
**Likely files:** bin/src/fakoli_state/planning/parser.py, bin/src/fakoli_state/planning/inference.py, bin/src/fakoli_state/planning/scoring.py, tests/test_parser.py
**Dependencies:**

**Acceptance criteria:**
- Optionally parsing EARS/Gherkin-style acceptance criteria raises parser reliability and grounds the six-dimension score in structured intent (theme #9, spec-kit#1356).
- The PRD parser recognizes a structured acceptance grammar (EARS "WHEN/THEN" or Gherkin "Given/When/Then") when present in acceptance criteria, extracting structured clauses, and falls back to freeform parsing when no structured grammar is detected.
- Tested: a PRD with EARS/Gherkin acceptance criteria parses into structured clauses; a freeform PRD still parses unchanged.

**Verification:**
- `cd bin && uv run pytest ../tests/test_parser.py -k acceptance_grammar -q`
