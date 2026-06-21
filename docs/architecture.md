# anvil architecture

> Condensed reference for the current **v0.1.1** standalone state. For the original v0
> vision and aspirational items, see
> [`specs/2026-05-24-anvil-v0.md`](specs/2026-05-24-anvil-v0.md).
> For what is planned but not yet shipped, see
> [`roadmap.md`](roadmap.md).
>
> This document is intentionally short — readable in ten minutes — and
> describes only what exists on disk today. Every entity, command, tool, and
> hook listed here has a file pointer; if it isn't pointed at, it doesn't
> ship.

---

## Mental model

anvil is to agentic software work what Terraform is to infrastructure:
a canonical state file holds the project record, derived views (work packets, markdown
plans, dependency graphs) are projected from it, and the plan-then-apply
rhythm gates execution behind review. The PRD is the configuration; the
SQLite database is the state; `anvil apply` is the commit point that
records evidence and transitions a task to `done`. Drift (stale claims,
orphan branches, sync conflicts) is detected and reconciled, not papered
over.

The full positioning (the five differentiators and the Terraform analogy) is maintained
in [`_positioning.md`](_positioning.md); this document does not duplicate
that material.

---

## Component layers

```mermaid
graph TD
    User["Human developer"]
    Agent["AI coding agent<br/>(Claude Code, Codex, Cursor, ...)"]

    subgraph Entry["Entry surfaces"]
        CLI["CLI<br/>anvil &lt;cmd&gt;"]
        MCP["MCP server<br/>FastMCP stdio<br/>24 tools"]
        Hooks["Hooks<br/>SessionStart / PreToolUse / PostToolUse"]
    end

    subgraph Plugin["Plugin layer (operating discipline)"]
        Skills["Skills<br/>start-prd, prd, plan, claim,<br/>execute, finish, state-ops,<br/>resolve-decisions"]
        Agents["Plugin agents<br/>planner, critic, sentinel,<br/>state-keeper, docs-scribe"]
    end

    subgraph Engine["State engine"]
        Claims["Claims manager<br/>lease + heartbeat + stale reap"]
        Transitions["Transitions<br/>pure state machine"]
        Planning["Planning engine<br/>template parser + LLM helpers"]
        Context["Context engine<br/>work-packet renderer"]
        Review["Review engine<br/>gates: PRD, readiness, evidence"]
        GitOps["Git ops<br/>agent/&lt;task&gt;-&lt;slug&gt; branches"]
    end

    subgraph Storage["Storage layer"]
        Backend["Backend Protocol"]
        SQLite[("SQLite<br/>state.db<br/>WAL + BEGIN IMMEDIATE")]
        Events[("Events<br/>events.jsonl<br/>append-only")]
    end

    subgraph Sync["Sync engine"]
        SyncProto["SyncProvider Protocol<br/>+ registry"]
        GitHub["GitHubIssuesProvider<br/>gh CLI + httpx"]
    end

    User --> CLI
    User --> Skills
    Agent --> MCP
    Agent --> Skills
    Agent --> Hooks

    CLI --> Engine
    MCP --> Engine
    Hooks --> CLI
    Skills --> CLI
    Skills --> Agents
    Agents --> CLI

    Claims --> Backend
    Transitions --> Backend
    Planning --> Backend
    Context --> Backend
    Review --> Backend
    GitOps --> Backend

    Backend --> SQLite
    Backend --> Events

    CLI --> SyncProto
    SyncProto --> GitHub
    GitHub --> Backend
```

Source: [`assets/diagrams/component.mmd`](../assets/diagrams/component.mmd).

<div align="center">
<img src="../assets/how-it-works.png" alt="Isometric diagram: agents claim tasks from the anvil surface, execute, and submit evidence — claim → execute → evidence → done" width="800" />
</div>

### Per-layer responsibilities

| Layer | What it owns | Key files |
|---|---|---|
| Plugin manifest | Discoverability, version, keywords | [`.claude-plugin/plugin.json`](../.claude-plugin/plugin.json) |
| CLI | Pure state operations — CRUD, scoring, packet generation, sync. No workflow choreography. | [`bin/src/anvil/cli/__init__.py`](../bin/src/anvil/cli/__init__.py) |
| MCP server | Runtime-neutral capability surface — 24 stdio tools any MCP client can call | [`bin/src/anvil/mcp_server.py`](../bin/src/anvil/mcp_server.py) |
| Hooks | Non-blocking enforcement the model would otherwise forget | [`hooks/hooks.json`](../hooks/hooks.json), [`hooks/*.sh`](../hooks/) |
| Skills | Workflow choreography — one-question-at-a-time, propose approaches, gate transitions | [`skills/*/SKILL.md`](../skills/) |
| Plugin agents | Specialist roles owned by this plugin | [`agents/*.md`](../agents/) |
| Backend protocol | The seam between state-engine logic and storage; SqliteBackend is the only impl that ships | [`bin/src/anvil/state/backend.py`](../bin/src/anvil/state/backend.py), [`bin/src/anvil/state/sqlite.py`](../bin/src/anvil/state/sqlite.py) |
| Transitions | Pure state machine — no I/O, no DB, no side-effects beyond `model_copy()` | [`bin/src/anvil/state/transitions.py`](../bin/src/anvil/state/transitions.py) |
| Claims manager | Atomic lease + heartbeat; stale detection on every operation | [`bin/src/anvil/claims/manager.py`](../bin/src/anvil/claims/manager.py), [`bin/src/anvil/claims/stale.py`](../bin/src/anvil/claims/stale.py) |
| Planning engine | Template-first PRD parser; optional LLM augmentation; deterministic six-dim scorer | [`bin/src/anvil/planning/`](../bin/src/anvil/planning/) |
| Context engine | Renders work packets (markdown + JSON) from canonical state | [`bin/src/anvil/context/packets.py`](../bin/src/anvil/context/packets.py) |
| Review engine | Pure transition-gate functions (readiness, evidence) | [`bin/src/anvil/review/gates.py`](../bin/src/anvil/review/gates.py) |
| Git ops | Auto-create `agent/<task>-<slug>` branch on `claim`; optional worktree | [`bin/src/anvil/git_ops/`](../bin/src/anvil/git_ops/) |
| Sync engine | Bidirectional GitHub Issues projection via the `SyncProvider` Protocol | [`bin/src/anvil/sync/`](../bin/src/anvil/sync/) |

The two iron rules of the layering:

1. **CLI is the one-and-only mutator.** Hooks shell out to the CLI; the MCP
   server opens a `SqliteBackend` directly but only via the same engine
   functions the CLI uses. Skills and agents do not write to `.anvil/`
   directly — they call the CLI.
2. **Transitions are pure.** Every status change is a function from
   `(entity, context) -> new entity`. Persisting the result is the backend's
   job, not the transition's. This is what makes the JSONL replay possible.

---

## Data model

The full type system lives in
[`bin/src/anvil/state/models.py`](../bin/src/anvil/state/models.py)
— **25 Pydantic v2 classes** total (11 enums + 14 models). Every field is
validated at every transition (`extra="forbid"`,
`validate_assignment=True`); all timestamps are UTC-required.

### Enums (11)

| Enum | Values | Purpose |
|---|---|---|
| `PRDStatus` | draft, reviewed, approved, rejected | Gates task claimability |
| `FeatureStatus` | proposed, ready, in_progress, done | Coarse-grain status on a Feature |
| `TaskStatus` | proposed, drafted, reviewed, ready, claimed, in_progress, blocked, needs_review, accepted, done, rejected | The 11-status task lifecycle (see [Task lifecycle](#task-lifecycle)) |
| `TaskPriority` | low, medium, high, critical | Sort key for `anvil next` |
| `ClaimType` | task, feature, file_scope, exploratory | Distinguishes whole-task vs partial leases |
| `ClaimStatus` | active, released, stale, force_released | Lease lifecycle |
| `ReviewTargetKind` | prd, task, feature | What a `Review` row points at |
| `ReviewDecision` | approve, reject, needs_changes | Reviewer verdict |
| `ExternalSystem` | github_issues | Canonical first-party provider ids (extensible via registry) |
| `SyncState` | in_sync, local_ahead, remote_ahead, conflict, external_deleted, remote_unknown | Per-mapping conflict / health label |
| `ConflictResolutionStrategy` | local_wins, remote_wins, prompt, manual_merge | How to resolve a divergence |

### Embedded value objects (2)

| Model | Purpose |
|---|---|
| `Score` | Six-dimension scoring on a Task: complexity, parallelizability, context_load, blast_radius, review_risk, agent_suitability (each 1-5 or null) |
| `Verification` | Embedded on Task: `commands`, `manual_steps`, `required_evidence` — the contract the evidence gate checks against |

### Top-level entities (12)

| Entity | Purpose |
|---|---|
| `Project` | Root entity that owns all other entities in the database |
| `PRD` | Product Requirements Document — the gate that controls task claimability |
| `Requirement` | A single atomic requirement derived from a section of the PRD |
| `Feature` | A logical grouping of tasks that delivers a user-observable capability |
| `Task` | The primary unit of work — claimable, scoreable, evidence-backed |
| `Claim` | An exclusive lease that an agent holds on a Task while working on it |
| `Evidence` | Completion evidence submitted by an agent after finishing a Task |
| `Decision` | An architectural or design decision recorded for audit and context |
| `Review` | A human or agent review verdict on a PRD, Task, or Feature |
| `Event` | An immutable append-only log entry; monotonic id `E000001`, `E000002`, ... |
| `SyncMapping` | Tracks a Task's relationship to an issue in an external system |
| `ConflictGroup` | A named set of tasks whose `expected_files` overlap |

Type aliases (`TaskID`, `FeatureID`, `RequirementID`, `ClaimID`, `EvidenceID`,
`DecisionID`, `ReviewID`, `EventID`) are plain `str` newtypes — no runtime
overhead but grep-able at every call site.

The shared model config on every entity:

```python
_MODEL_CONFIG = ConfigDict(
    frozen=False,             # mutable for state transitions
    validate_assignment=True, # but assignment-validated end-to-end
    extra="forbid",           # unknown fields are an error
)
```

---

## Task lifecycle

```mermaid
stateDiagram-v2
    [*] --> proposed: plan / expand

    proposed --> drafted: task_proposed_to_drafted
    drafted --> reviewed: task_drafted_to_reviewed<br/>gate: acceptance_criteria<br/>+ verification.commands
    reviewed --> ready: task_reviewed_to_ready
    ready --> claimed: task_ready_to_claimed<br/>gate: PRD reviewed or approved

    claimed --> in_progress: task_claimed_to_in_progress
    in_progress --> blocked: task_in_progress_to_blocked<br/>(reason required)
    blocked --> in_progress: task_blocked_to_in_progress

    in_progress --> needs_review: task_in_progress_to_needs_review<br/>(evidence submitted)

    needs_review --> accepted: task_needs_review_to_accepted<br/>gate: required_evidence present
    needs_review --> rejected: task_needs_review_to_rejected<br/>(reason required)

    accepted --> done: task_accepted_to_done
    rejected --> drafted: task_rejected_to_drafted

    done --> [*]

    note right of claimed
        Claim row holds lease +
        heartbeat. Stale leases
        are reaped on every
        mutating CLI / MCP call.
    end note

    note right of needs_review
        Human or critic agent
        verdict. Rejection loops
        back through drafted.
    end note
```

Source: [`assets/diagrams/lifecycle.mmd`](../assets/diagrams/lifecycle.mmd).

All 11 statuses are defined in `TaskStatus` and the allowed transitions are
the public functions in
[`bin/src/anvil/state/transitions.py`](../bin/src/anvil/state/transitions.py).
The module is pure (no I/O); each function returns a new `Task` via
`model_copy(update=...)`.

### Gates on the lifecycle

Three named gates appear in the transition module; each raises
`TransitionError(gate_name=...)` with a structured error envelope:

| Gate | Where it fires | What it checks |
|---|---|---|
| `readiness_gate` | drafted → reviewed | `task.acceptance_criteria` and `task.verification.commands` must both be non-empty |
| `prd_status_gate` | ready → claimed | Project PRD must be in `reviewed` or `approved` (refuses while `draft`) |
| `evidence_gate` | needs_review → accepted | Every item in `task.verification.required_evidence` must appear as a substring of at least one Evidence field |

### Who drives each transition

| Transition | Typical driver | CLI verb |
|---|---|---|
| proposed → drafted → reviewed → ready | Planner agent or human via `plan` / `review` | `anvil plan`, `anvil review tasks` |
| ready → claimed | Coding agent (or human) | `anvil claim T012` |
| claimed → in_progress | Auto on first heartbeat or file change | (implicit) |
| in_progress ↔ blocked | Agent or human | `anvil hook ... block` |
| in_progress → needs_review | Coding agent submitting evidence | `anvil submit T012 ...` |
| needs_review → accepted or rejected | Human reviewer or critic agent | `anvil apply T012 --approve` / `--reject` |
| accepted → done | Auto on `apply --approve` | (implicit) |
| rejected → drafted | Author revises and re-submits | `anvil plan` (re-edit) |

Only `drafted ↔ ready` and the `blocked` toggle are exposed via the
`update_task_status` MCP tool; all other transitions require a more
specific CLI verb (claim, submit, apply) so that the necessary
side-effects (lease creation, evidence write, claim release) happen
atomically.

---

## Event log and JSONL replay

Every state mutation appends one `Event` row to two places:

1. The `events` table inside `state.db` (assigned the monotonic id
   `E000001`, `E000002`, ... inside the same `BEGIN IMMEDIATE`
   transaction that mutated state).
2. `events.jsonl` — a newline-delimited JSON mirror, append-only, written
   after the SQLite commit succeeds.

The replay guarantee is the central audit property of the engine: **replaying
`events.jsonl` from an empty database must reconstruct canonical SQLite state
exactly**. This is what makes the engine safe to back up by copying
`.anvil/` and what makes a corrupted database recoverable.

A native `anvil replay --from-events events.jsonl` subcommand is
planned for v2.1 (item P9B-7 — see
[`roadmap.md` § Snapshot / replay](roadmap.md#theme-snapshot--replay)) and
**does not ship today**. Until it does, the supported backup and recovery
flow is to copy `.anvil/` wholesale; the replay guarantee makes
that safe and minimal:

```bash
# Back up before destructive work.
cp -r .anvil /backup/location/anvil-$(date +%Y-%m-%d)

# Recover from a corrupted state.db by restoring the backup.
rm -f .anvil/state.db .anvil/state.db-wal .anvil/state.db-shm
cp /backup/location/anvil-YYYY-MM-DD/state.db .anvil/state.db
```

`events.jsonl` is the durable audit log even without replay tooling —
commit it to git alongside the repo and you have a distributed audit
trail recoverable from any clone.

Event ids are assigned inside the lock, not before it, to eliminate a
read-before-lock race surfaced in PR #41 (Critic-3). The `Event.id`
validator accepts a `"PENDING"` sentinel so callers can defer id
assignment to the backend's `apply_event` method.

---

## Storage layout

`anvil init` scaffolds this layout inside the user's project root
(not inside the plugin):

```text
<user-project>/.anvil/
├── config.yaml         # project-level config (sync providers, lease defaults, ...)
├── state.db            # SQLite — the canonical state (WAL mode)
├── events.jsonl        # append-only audit / event log (replay source)
├── prd.md              # the PRD source (edited by hand; re-parsed via `prd parse`)
└── packets/            # generated work packets (per-task markdown / json)
```

A `snapshots/` subdirectory was originally planned (and is shown in the v0
spec) but the `anvil snapshot` subcommand has not yet shipped — see
[Roadmap → v2.1 → snapshot subcommand](roadmap.md#theme-snapshot--replay).
Backups today are done by copying `.anvil/` wholesale; the replay
guarantee makes that safe.

`hooks` and the CLI alike resolve `STATE_DIR` relative to
`${CLAUDE_PROJECT_DIR:-$PWD}/.anvil` so every agent invocation,
regardless of cwd at call time, addresses the same project's state.

---

## Concurrency model

Multiple humans and multiple agents must coordinate on the same canonical
state without overlapping each other's work. anvil achieves this with four
mechanisms layered together:

1. **SQLite WAL + `BEGIN IMMEDIATE`.** Every mutating operation runs inside
   an immediate-mode transaction, so concurrent writers serialize at the
   SQLite layer. Reads use WAL snapshots and do not block writers.
2. **Claim leases with heartbeats.** A `Claim` row carries
   `lease_expires_at` and `last_heartbeat_at`. The CLI's `renew` command
   (and the MCP `renew_claim` tool) extends the lease. Default lease is 60
   minutes (configurable via `.anvil/config.yaml`); the in-code
   default lives at [`claims/manager.py`](../bin/src/anvil/claims/manager.py).
3. **Stale-claim reaping.** Every mutating CLI command and every mutating
   MCP tool calls
   [`detect_and_release_stale()`](../bin/src/anvil/claims/stale.py)
   at entry. Leases past their expiry are auto-released with
   `release_reason="stale"`; the audit event preserves the original
   claimant. Read-only listers skip reaping for latency.
4. **Conflict groups.** A `ConflictGroup` row names a set of tasks whose
   `expected_files` overlap. `anvil next` and the
   `get_next_task` MCP tool refuse to surface a task whose conflict group
   already has an active claim — preventing two agents from being routed
   to overlapping work even when neither task is itself claimed.

The PreToolUse `check-claim.sh` hook adds a final layer of safety at the
Claude Code editor surface: before an Edit / Write / NotebookEdit fires,
the hook asks the CLI whether the current actor holds a claim covering the
target file, and surfaces a warning (non-blocking, per the hook contract)
when no claim is held.

---

## CLI / MCP / hooks surface

### CLI commands

Full reference is available at
[`docs/cli-reference.md`](cli-reference.md). The command surface assembled
in [`bin/src/anvil/cli/__init__.py`](../bin/src/anvil/cli/__init__.py):

- Lifecycle setup and inspection: `init`, `status`, `describe`, `doctor`
- PRD authoring: `prd parse`, `prd review` (sub-app)
- Planning: `plan`, `score`, `expand`, `review tasks` (sub-app)
- Listing / inspecting: `list`, `show`, `scan`, `drift`, `graph`, `conflicts`
- Claiming: `claim`, `release`, `renew`, `next`
- Working: `packet`, `submit`, `apply`
- Harness config: `mcp-config`
- Migration / replay: `migrate state`, `migrate-events`, `replay`
- Hooks: `hook ...` (sub-app — called by `hooks/*.sh`)
- Sync: `sync ...` (sub-app — `sync github`, `sync github --health`, ...)

### MCP tools (24)

Full reference is at [`docs/mcp.md`](mcp.md). Source:
[`bin/src/anvil/mcp_server.py`](../bin/src/anvil/mcp_server.py).

| # | Tool | Mutates | Reaps stale |
|---|---|---|---|
| 1 | `get_project_summary` | no | yes |
| 2 | `get_project_status` | no | no |
| 3 | `list_tasks` | no | no |
| 4 | `get_task` | no | no |
| 5 | `get_next_task` | no | no |
| 6 | `get_dependency_graph` | no | no |
| 7 | `claim_task` | yes | yes |
| 8 | `release_task` | yes | yes |
| 9 | `renew_claim` | yes | yes |
| 10 | `update_task_status` | yes | yes |
| 11 | `submit_progress` | yes (audit-only) | yes |
| 12 | `submit_completion_evidence` | yes | yes |
| 13 | `generate_work_packet` | no | no |
| 14 | `check_conflicts` | no | no |
| 15 | `edit_dependencies` | yes | no |
| 16 | `init_project` | yes | no |
| 17 | `parse_prd` | yes | no |
| 18 | `review_prd` | yes | no |
| 19 | `plan_tasks` | yes | no |
| 20 | `score_tasks` | yes | no |
| 21 | `review_tasks` | yes | no |
| 22 | `apply_review_decision` | yes | no |
| 23 | `find_decisions` | no | no |
| 24 | `describe_surface` | no | no |

Sync tools (`sync_run`, `sync_health`, `sync_status`, `sync_reconcile`) are
not yet on the MCP surface — agents that want sync today shell out via Bash
to `anvil sync`. See
[Roadmap → v2.1 → MCP sync tools](roadmap.md#theme-mcp-surface-sync-tools).

### Hooks (4)

Wired in [`hooks/hooks.json`](../hooks/hooks.json). All four are
**non-blocking**: they must `exit 0` regardless of internal failure, must
not use `set -e` / `set -u` / `set -o pipefail`, must wrap CLI calls with
`|| true`, and must complete in well under their declared timeout.

| Hook | Trigger | Script | Purpose |
|---|---|---|---|
| `detect-state` | SessionStart | [`detect-state.sh`](../hooks/detect-state.sh) | Surface project state info into the session context |
| `check-claim` | PreToolUse on `Edit / Write / NotebookEdit` | [`check-claim.sh`](../hooks/check-claim.sh) | Warn (non-blocking) if the agent has no active claim covering the file |
| `record-file-change` | PostToolUse on `Edit / Write / NotebookEdit` | [`record-file-change.sh`](../hooks/record-file-change.sh) | Record the change against the active claim for orphan detection |
| `capture-evidence` | PostToolUse on `Bash` | [`capture-evidence.sh`](../hooks/capture-evidence.sh) | When the command matches a verification pattern, buffer it as evidence for the active claim |

### Skills (8)

Workflow choreography lives in [`skills/*/SKILL.md`](../skills/) —
start-prd, prd, plan, claim, execute, finish, state-ops, resolve-decisions.

Skill frontmatter is always loaded into the model's context (it is the
plugin's command surface), so the combined skill footprint is kept under an
explicit token budget enforced in CI — see
[`context-budget.md`](context-budget.md) and
[`tests/test_token_budget.py`](../tests/test_token_budget.py).

### Plugin agents (5)

Defined in [`agents/*.md`](../agents/):

- `planner` — drafts feature / task decomposition from a parsed PRD
- `critic` — reviews PRD or task drafts; produces an approve / reject / needs_changes verdict
- `sentinel` — pre-merge verification
- `state-keeper` — operational hygiene: orphan claims, drift, schema migrations
- `docs-scribe` — keeps `docs/` synchronised with shipped behaviour

Each agent's frontmatter pins `tools:` (least privilege).

---

## Where to read the code

Map from architectural layer to source file. Every layer of this document
points at a file you can grep.

| Layer | File(s) |
|---|---|
| Entry: CLI assembly | [`bin/src/anvil/cli/__init__.py`](../bin/src/anvil/cli/__init__.py) |
| Entry: MCP server (24 tools) | [`bin/src/anvil/mcp_server.py`](../bin/src/anvil/mcp_server.py) |
| Entry: hooks manifest | [`hooks/hooks.json`](../hooks/hooks.json) |
| Type system | [`bin/src/anvil/state/models.py`](../bin/src/anvil/state/models.py) |
| Transitions (pure) | [`bin/src/anvil/state/transitions.py`](../bin/src/anvil/state/transitions.py) |
| Backend Protocol | [`bin/src/anvil/state/backend.py`](../bin/src/anvil/state/backend.py) |
| SQLite impl + schema | [`bin/src/anvil/state/sqlite.py`](../bin/src/anvil/state/sqlite.py), [`schema.py`](../bin/src/anvil/state/schema.py) |
| Event payloads | [`bin/src/anvil/state/payloads.py`](../bin/src/anvil/state/payloads.py) |
| Claims manager | [`bin/src/anvil/claims/manager.py`](../bin/src/anvil/claims/manager.py) |
| Stale reaping | [`bin/src/anvil/claims/stale.py`](../bin/src/anvil/claims/stale.py) |
| Planning (template + LLM + scoring) | [`bin/src/anvil/planning/`](../bin/src/anvil/planning/) |
| Context (work packets) | [`bin/src/anvil/context/packets.py`](../bin/src/anvil/context/packets.py) |
| Review gates | [`bin/src/anvil/review/gates.py`](../bin/src/anvil/review/gates.py) |
| Git ops | [`bin/src/anvil/git_ops/`](../bin/src/anvil/git_ops/) |
| Sync Protocol + registry | [`bin/src/anvil/sync/provider.py`](../bin/src/anvil/sync/provider.py), [`registry.py`](../bin/src/anvil/sync/registry.py) |
| GitHub provider | [`bin/src/anvil/sync/providers/github_issues.py`](../bin/src/anvil/sync/providers/github_issues.py) |
| Reconciliation | [`bin/src/anvil/sync/reconciliation.py`](../bin/src/anvil/sync/reconciliation.py) |
| Plugin config | [`bin/src/anvil/config.py`](../bin/src/anvil/config.py) |
| Clock abstraction | [`bin/src/anvil/clock.py`](../bin/src/anvil/clock.py) |

---

## What is NOT here yet

This document describes the current shipped behaviour only. The full backlog of
planned-but-not-yet-shipped items is in [`roadmap.md`](roadmap.md);
the high-level buckets:

- **v2.0 — sync providers + immediate-apply resolution.**
  - `LinearIssuesProvider` (GraphQL transport).
  - `MondayBoardsProvider` (REST + JSON, people-columns).
  - Webhook-based sync as an alternative to polling (spec-first).
  - `*_applied` conflict-resolution variants — wiring the
    `remote_wins_applied` / `local_wins_applied` deferrals at
    `cli/sync.py:1054, :1068`.
  - Provider config schemas in `config.yaml` (per-provider settings).
- **v2.1 — follow-on capability.**
  - `JiraIssuesProvider` (per-project workflow discovery).
  - `GitHubProjectsProvider` (Projects v2 board surface).
  - `anvil snapshot` subcommand (`sqlite3 .backup` wrapper, retention).
  - MCP sync surface — four new tools (`sync_run`, `sync_health`,
    `sync_status`, `sync_reconcile`).
- **v2.x — hygiene.**
  - Composition deduplication across the three doc/state agents.
  - Skill subdirectory extraction (`references/`, `examples/`, `scripts/`).
  - Hook concurrency hardening (`flock` on shared append targets).
- **Unscheduled.**
  - Config-driven matcher framework for `capture-evidence.sh`.

Each item carries a backlog id (`P9B-N` from the Phase 9 backlog or
`P11-XX-XN` from the Phase 11 backlog) preserved across audits — see
[`roadmap.md`](roadmap.md) for per-item acceptance criteria, file pointers,
and welder-effort estimates.

---

## Further reading

- [`_positioning.md`](_positioning.md) — differentiators and the Terraform analogy (internal positioning reference)
- [`specs/2026-05-24-anvil-v0.md`](specs/2026-05-24-anvil-v0.md) — the original 358-line v0 build spec (this document is its condensed shipped sibling)
- [`mcp.md`](mcp.md) — full 24-tool MCP reference with error envelope contract
- [`github-sync.md`](github-sync.md) — bidirectional GitHub Issues sync reference
- [`sync-providers.md`](sync-providers.md) — contributor guide for new sync providers
- [`prd-template.md`](prd-template.md) — PRD authoring schema and worked example
- [`llm.md`](llm.md) — `--use-llm` augmentation, prompt caching, `RecordedLLMProvider` test pattern
- [`roadmap.md`](roadmap.md) — what is planned next
