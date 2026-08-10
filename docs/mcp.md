# anvil MCP server

> **Audience:** users installing or integrating the MCP server with an agent harness.

## What it does

Agents need to read and write canonical project state without each one shelling out to the
CLI per operation and without fighting over the same SQLite rows. The MCP server has 36
registered tools (24 on the wire by default — see
[Tool surface gating](#tool-surface-gating)) over stdio so that any MCP-compatible
runtime — Claude Code, Codex, Cursor, OpenHands,
Copilot, or a local script — can drive the full PRD → plan → review → approve → claim →
apply workflow as first-class tool calls. Read-only tools return structured Pydantic
objects; lease-sensitive claim, renew, and release tools reap stale claims before writing.
MCP initialize metadata reports the Anvil engine version (the same value as
`anvil --version`), rather than the version of the FastMCP transport package.

The toolset is organized by lifecycle phase:

- **Bootstrap & status** (`init_project`, `get_project_status`, `get_project_summary`)
- **PRD lifecycle** (`parse_prd`, `assess_prd`, `review_prd`)
- **Planning & scoring** (`plan_tasks`, `score_tasks`, `review_tasks`)
- **Task inspection** (`list_tasks`, `get_task`, `get_next_task`, `get_dependency_graph`,
  `check_conflicts`, `edit_dependencies`)
- **Claiming & execution** (`claim_task`, `release_task`, `renew_claim`,
  `generate_work_packet`, `submit_progress`, `submit_completion_evidence`,
  `update_task_status`)
- **Execution bundles** (`create_bundle`, `list_bundles`, `get_bundle`, `claim_bundle`,
  `generate_bundle_packet`, `submit_bundle_progress`, `record_bundle_review`,
  `finalize_bundle_review`, `checkpoint_bundle`, `reconcile_bundle`, `supersede_bundle`)
- **Review gate** (`apply_review_decision`)
- **Decision resolution** (`find_decisions`)
- **Introspection** (`describe_surface`)

Planning tools do not create branches or worktrees. `claim_task` and `claim_bundle`,
however, use the same transactional shared-branch claim plan as the CLI when `cwd`
resolves to a Git repository: they may create or check out the claim branch. Isolated
worktree creation remains CLI-only. If Git is unavailable, the non-isolated state-only
claim path remains usable.

---

## Tool surface gating

All 36 tools are registered, but the live stdio server exposes only the **24 execution
tools** on the wire by default — the turn-to-turn loop an agent runs while doing work:

`get_next_task`, `claim_task`, `release_task`, `renew_claim`, `submit_progress`,
`submit_completion_evidence`, `update_task_status`, `get_task`, `get_project_status`,
`get_project_summary`, `list_tasks`, `check_conflicts`, `generate_work_packet`,
`get_dependency_graph`, `list_bundles`, `get_bundle`, `claim_bundle`,
`generate_bundle_packet`, `submit_bundle_progress`, `record_bundle_review`,
`finalize_bundle_review`, `checkpoint_bundle`, `reconcile_bundle`, `supersede_bundle`

The other **12 planning tools** are hidden by default so steady-state execution clients
never pay their schema cost on every turn:

`init_project`, `parse_prd`, `assess_prd`, `review_prd`, `plan_tasks`, `score_tasks`, `review_tasks`,
`apply_review_decision`, `edit_dependencies`, `find_decisions`, `describe_surface`,
`create_bundle`

Set `ANVIL_MCP_PLANNING=1` (any of `1`/`true`/`yes`/`on`) in the server's environment to
keep all 36 tools on the wire — use it for the planning phase, or run a second server
entry with the flag set. No tool is removed by the gate: introspection surfaces
(`anvil describe`, the `--help` tool list, the Docker catalog smoke test) always report
all 36.

---

## Installation

The server is wired automatically once the plugin is installed. No manual configuration is
required. The plugin ships a `.mcp.json` at its root that Claude Code reads on session start:

```json
{
  "mcpServers": {
    "anvil": {
      "type": "stdio",
      "command": "uv",
      "args": [
        "run",
        "--quiet",
        "--project",
        "${CLAUDE_PLUGIN_ROOT}/bin",
        "python",
        "-m",
        "anvil.mcp_server"
      ]
    }
  }
}
```

`${CLAUDE_PLUGIN_ROOT}` is the absolute path to the installed plugin directory. `uv run
--quiet --project ${CLAUDE_PLUGIN_ROOT}/bin` syncs the plugin's locked environment when
needed (covering cold starts and `git pull` updates), then delegates to
`python -m anvil.mcp_server` without relying on a shell or emitting uv chatter into the
stdio MCP stream.

Each tool call opens a fresh `SqliteBackend` against `.anvil/state.db` resolved from
the agent's current working directory at call time. Agents can invoke from any project
directory — the server re-resolves state on every call.

**Prerequisite**: `anvil init` must have been run in the project root before any tool
call will succeed.

---

## Tool reference

Tools are grouped below by access pattern: read-only tools first, mutating tools second.

### Read-only tools

---

### `get_project_summary`

Returns a snapshot of overall project health: task counts by status, active claim count,
blocked task count, and ready task count. Stale-claim reaping runs before the read, so
counts reflect freshly expired leases.

**Inputs**

None.

**Output**

```json
{
  "project_id": "string",
  "project_name": "string",
  "project_description": "string",
  "prd_status": "string | null",
  "task_counts": {
    "proposed": 0,
    "drafted": 0,
    "reviewed": 0,
    "ready": 0,
    "claimed": 0,
    "in_progress": 0,
    "blocked": 0,
    "needs_review": 0,
    "accepted": 0,
    "done": 0,
    "rejected": 0
  },
  "active_claim_count": 0,
  "blocked_task_count": 0,
  "ready_task_count": 0
}
```

`prd_status` is `null` when no PRD has been parsed yet.

**Failure modes**

- `ToolError` — project not initialized (`.anvil/` missing).
- `ToolError` — project row not found in state.db (run `anvil init`).

**When to call**: at session start or before orchestrating a wave, to decide how many agents
to spawn and whether the queue is draining or stacking up.

---

### `list_tasks`

Returns tasks filtered by status, feature, or claiming actor. All three filters are optional
and combinable. `status` and `feature_id` are pushed to SQL; `claimed_by` is an in-memory
filter joined against active claims.

**Inputs**

| Parameter    | Type            | Required | Default |
|--------------|-----------------|----------|---------|
| `status`     | `string \| null` | no       | `null`  |
| `feature_id` | `string \| null` | no       | `null`  |
| `claimed_by` | `string \| null` | no       | `null`  |

Valid `status` values: `proposed`, `drafted`, `reviewed`, `ready`, `claimed`,
`in_progress`, `blocked`, `needs_review`, `accepted`, `done`, `rejected`.

**Output**

A JSON array of Task objects serialized from their Pydantic models. Each element includes
full task fields: `id`, `title`, `status`, `priority`, `feature_id`, `dependencies`,
`conflict_groups`, `expected_files`, `scores`, and all other Task model fields.

**Failure modes**

- `ToolError` — state directory not found.

**When to call**: when a coordinator agent needs to see all `ready` tasks before deciding
which ones to dispatch in a wave.

---

### `get_task`

Returns the full Task object for a single task ID.

**Inputs**

| Parameter | Type     | Required |
|-----------|----------|----------|
| `task_id` | `string` | yes      |

**Output**

A single Task object serialized to JSON (same shape as one element from
`list_tasks`), plus a derived `review_tier` field (`light`/`standard`/`max`)
computed at read time from the merged project config — identical to the CLI
`show`/`next` value for the same task.

**Failure modes**

- `ToolError` — task not found: `"Task '{task_id}' not found."`.
- `ToolError` — state directory not found.

**When to call**: after `get_next_task` returns a candidate, to read the full acceptance
criteria and constraints before calling `claim_task`.

---

### `get_next_task`

Returns the single highest-priority `ready` task that has no active claim and no unsatisfied
dependencies, together with the complete accept-rate governor calculation. Sort key
is shared with `ClaimManager.next_claimable()`: `priority desc` (`critical` >
`high` > `medium` > `low`), then `complexity asc` (unscored ranks last),
creation time asc, and `id asc` as the stable final tiebreak. The response's
`task` field is `null` when no claimable task
is available; `governor.withheld_reason` distinguishes a governed withhold from
an empty queue.

Stale-claim reaping runs before the selection, so expired leases are cleared before the
candidate set is computed. Tasks in active conflict groups (where a conflicting task is
already claimed) are excluded.

**Inputs**

| Parameter         | Type             | Required | Default |
|-------------------|------------------|----------|---------|
| `actor`           | `string \| null` | no       | `null`  |
| `prd_id`          | `string \| null` | no       | `null`  |
| `max_blast`       | `int \| null`    | no       | `null`  |
| `max_review_risk` | `int \| null`    | no       | `null`  |

`actor` selects the finalized-review history used by the accept-rate governor.

`prd_id` scopes the candidate pool to one PRD partition; the exclusion sets (active claims,
done-dependency set, active conflict groups) still span all PRDs, so cross-PRD coordination
still applies. `null` keeps the all-PRDs behavior.

`max_blast` / `max_review_risk` are optional risk-axis ceilings: when set, a task is offered
only if that dimension is CONFIRMED (human/LLM, not the filename-regex heuristic) and at or
below the ceiling, using the same `within_risk_ceiling` helper the CLI's
`ClaimManager.next_claimable` uses — so a weak/local runner can declare a ceiling and never
be handed high-risk work.

**Output**

An object with `task`, `governor`, and `actor_identity`. `task` is a serialized
Task or `null`; a returned task carries `review_tier` and advisory
`conflict_warnings`. `governor` reports `as_of`, `window_start`, `window_days`,
`numerator`, `denominator`, `rate`, `floor`, `configured_floor`, review-queue
depth/cap, `withheld_reason`, `offer_throttled`, and bounded recovery guidance.
One accepted finalized review contributes `1/1`, one quality rejection
contributes `0/1`, and evidence/process rejections contribute neither. A zero
denominator yields `rate: null` and does not fail the floor.

Clearing the review queue alone is insufficient to repair a low rate. Recovery
comes from accepted finalized reviews, expiry from the configured window, or a
configured floor change. Claiming a known ID directly bypasses only offer
throttling; ownership, conflict, PRD, risk, and evidence gates remain enforced.

**Failure modes**

- `ToolError` — state directory not found.

**When to call**: the standard first step for any agent entering the work loop — call
`get_next_task`, then `claim_task` on the returned ID.

---

### `generate_work_packet`

Renders a work packet for a task in markdown or JSON format. The packet includes task intent,
acceptance criteria, constraints, non-goals, open dependencies, and the active claim if one
exists. Delegates to `anvil.context.packets.render_packet`.

**Inputs**

| Parameter | Type                       | Required | Default      |
|-----------|----------------------------|----------|--------------|
| `task_id` | `string`                   | yes      |              |
| `format`  | `"markdown" \| "json"`     | no       | `"markdown"` |

**Output**

```json
{
  "format": "markdown",
  "content": "# T012 — Implement auth middleware\n..."
}
```

`content` is a `string` when `format` is `"markdown"` and a `dict` when `format` is
`"json"`.

**Failure modes**

- `ToolError` — task not found.
- `ToolError` — state directory not found.

**When to call**: immediately after `claim_task` succeeds, to get the structured prompt
the agent will work against.

---

### `check_conflicts`

Cross-references a list of proposed file paths against the `expected_files` of all currently
active claims, excluding the task's own claim. Returns one conflict entry per overlapping
file per claim.

**Inputs**

| Parameter        | Type           | Required |
|------------------|----------------|----------|
| `task_id`        | `string`       | yes      |
| `proposed_files` | `list[string]` | yes      |

**Output**

```json
{
  "conflicts": [
    {
      "file": "src/auth/middleware.py",
      "claim_id": "C001",
      "claimed_by": "agent-welder-1",
      "task_id": "T008"
    }
  ]
}
```

An empty `conflicts` list means no overlaps were detected.

**Failure modes**

- `ToolError` — state directory not found.

**When to call**: before declaring `expected_files` in a `claim_task` call, to surface
potential write conflicts before work begins rather than discovering them at merge time.

---

### `get_dependency_graph`

Returns nodes, directed edges, and the `ready_to_claim` set for a given scope. Edges run
from dependency to dependent (`from → to`). `ready_to_claim` lists task IDs that are in
`ready` status, have all dependencies in `done` status, and have no active claim.

**Inputs**

| Parameter   | Type                             | Required | Default  |
|-------------|----------------------------------|----------|----------|
| `scope`     | `"all" \| "feature" \| "task"`   | no       | `"all"`  |
| `target_id` | `string \| null`                 | no       | `null`   |

`target_id` is required when `scope` is `"feature"` or `"task"`. When `scope` is `"task"`,
the graph covers the target task and all its transitive dependencies.

**Output**

```json
{
  "nodes": [
    {
      "id": "T001",
      "title": "Scaffold auth module",
      "status": "done",
      "priority": "high",
      "feature_id": "F001"
    }
  ],
  "edges": [
    { "from": "T001", "to": "T002" }
  ],
  "ready_to_claim": ["T002", "T003"]
}
```

**Failure modes**

- `ToolError` — `target_id` is `null` when `scope` is `"feature"` or `"task"`.
- `ToolError` — state directory not found.

**When to call**: when a planner agent needs to decide which tasks are unblocked and safe
to dispatch in parallel this wave.

---

### Mutating tools

`claim_task`, `claim_bundle`, `release_task`, `renew_claim`, `submit_progress`,
`submit_completion_evidence`, `update_task_status`, and `get_project_summary` run
`detect_and_release_stale` at the top of their call. This is automatic on those paths.
Other mutators validate their own lifecycle preconditions but do not promise a global
stale-claim sweep. See
[Stale-claim reaping](#stale-claim-reaping) for details.

---

### `edit_dependencies`

Validates a batch of dependency edits before applying it, rejecting cycles. This is a
planning-gated tool (hidden from the wire unless `ANVIL_MCP_PLANNING=1`; see
[Tool surface gating](#tool-surface-gating)). It does not run stale-claim reaping — it
only rewrites dependency lists, so no claim state is touched.

`add` / `remove` are `[source, target]` pairs meaning *source depends on target*. The whole
batch is validated up front before anything is written: any unknown task ID, self-dependency,
or resulting cycle rejects the entire batch with no mutation. `prd_id` selects the source-task
owner (or resolves the single/default PRD when omitted), and `cwd` selects the project root.
Sources must belong to the selected PRD; dependency targets may belong to another PRD.
After validation, one `task.dependencies_batch_edited` event carries every changed task and
its exact prior ordered dependencies. State revalidates ownership, endpoints, stale
preconditions, and the final graph while holding the append lock, then commits all edits
together. A no-op request emits no event, and task status is never changed. If the atomic
append is rejected, MCP returns the fixed ToolError
`dependency update was rejected by state validation.`; the CLI returns the same message
with JSON error code `event_rejected`. Backend validation details are not exposed.
Malformed pairs, unknown tasks, self-loops, and cycles also return fixed, bounded
ToolErrors; raw edge and task values are never reflected in those errors or in
server logs. The public schema remains `list[list[string]] | null`; runtime
shape validation occurs before state access. A request is capped at 10,000
total `add` plus `remove` pairs, and cap+1 receives a fixed ToolError.

**Inputs**

| Parameter | Type                     | Required | Default |
|-----------|--------------------------|----------|---------|
| `actor`   | `string`                 | yes      |         |
| `add`     | `list[list[string]] \| null` | no   | `null`  |
| `remove`  | `list[list[string]] \| null` | no   | `null`  |
| `prd_id`  | `string \| null`         | no       | single/default PRD |
| `cwd`     | `string \| null`         | no       | server launch directory |

At least one of `add` / `remove` must contain an edge, or the tool raises `ToolError`.

**Output**

```json
{
  "prd_id": "default",
  "changed": ["T003"],
  "added": [["T003", "T001"]],
  "removed": []
}
```

`prd_id` is the resolved source-task owner. `changed` lists every task whose dependency set
was actually mutated; `added` / `removed`
are the `[source, target]` edges that took effect — no-op edges (e.g. re-adding an edge that
already exists) are excluded from both.

**Failure modes**

- `ToolError` — no edges supplied (both `add` and `remove` empty).
- `ToolError` — malformed edge (not a 2-element `[source, target]` pair).
- `ToolError` — unknown task referenced by an edge.
- `ToolError` — a source task is outside the selected PRD.
- `ToolError` — self-dependency (`source == target`).
- `ToolError` — the batch would introduce a dependency cycle.
- `ToolError` — state directory not found.
- `ToolError` — the atomic backend append was rejected. MCP returns the fixed message
  `dependency update was rejected by state validation.` without the raw backend reason;
  the CLI uses the same text and JSON error code `event_rejected`.

Dependency-batch refusals are bounded and do not expose raw payload or backend validation
details. A rejected batch adds nothing to `events.jsonl` and leaves the complete dependency
projection unchanged.

**When to call**: when a planner agent needs to correct inferred dependencies (add a missing
edge, drop a spurious one) before promoting tasks to `ready`, without hand-editing state.db.

---

### `claim_task`

Acquires an exclusive lease on a task for the given actor. It first resolves a
read-only shared-branch Git plan, then revalidates that plan under the same
cross-process lock used by `ClaimManager.claim`. State and the planned branch mutation
therefore succeed together; a Git failure or interruption releases the claim and
compensates only Git artifacts created by that invocation. Isolated worktrees require
the CLI. Stale-claim reaping runs first.

**Gate**: the task's owning PRD must be in `reviewed` or `approved` status. If the PRD is in
any other status (e.g. `draft`) or missing, the tool raises a `ToolError` and no claim is
created.

**Inputs**

| Parameter                | Type                    | Required | Default |
|--------------------------|-------------------------|----------|---------|
| `task_id`                | `string`                | yes      |         |
| `claimed_by`             | `string`                | yes      |         |
| `expected_files`         | `list[string] \| null`  | no       | `[]`    |
| `lease_duration_seconds` | `int`                   | no       | `900`   |
| `shared_tree`            | `bool`                  | no       | `false` |
| `cwd`                    | `string \| null`        | no       | project root |

`lease_duration_seconds` is converted to minutes (floor, minimum 1) before being passed
to `ClaimManager`. The default 900 seconds gives a 15-minute MCP-side override — note that
the CLI's `ClaimManager` ships with a 240-minute default (see
[`bin/src/anvil/claims/manager.py`](https://github.com/fakoli/anvil/blob/main/bin/src/anvil/claims/manager.py)),
and the project-level override is read from `.anvil/config.yaml`.

**Output**

```json
{
  "id": "C001",
  "task_id": "T012",
  "claimed_by": "agent-welder-1",
  "lease_expires_at": "2026-05-25T14:15:00+00:00",
  "branch": "agent/t012-implement-auth",
  "worktree_path": null,
  "git_metadata": {"mode": "shared", "canonical_root": "...", "claim_start_sha": "...", "branch": "agent/t012-implement-auth", "target_path": "..."},
  "expected_files": ["src/auth/middleware.py", "tests/test_auth.py"],
  "generation": 2,
  "attestation_context": {"repository_id": "...", "claim_start_sha": "...", "expected_paths": [{"path": "src/auth/middleware.py", "baseline_sha256": "..."}]},
  "continuation": {"attest_progress": {"argv": ["anvil", "progress", "T012", "<phase>", "--attestation-file", "<path>", "--actor", "agent-welder-1"]}}
}
```

The immutable attestation context binds external progress to this claim generation,
repository, PRD/task revisions, and canonical expected-path baselines. `git_metadata`
records the exact selected base, claim-start commit, branch, canonical root, and target.
Here `canonical_root` is the stable Git repository identity shared by all linked
callers; for `--separate-git-dir` repositories that identity is the common Git
directory, while CLI isolated-worktree placement remains main-checkout-adjacent.
`branch`, `worktree_path`, and `git_metadata` are nullable on the state-only path. In a
non-Git project the claim still succeeds with `attestation_context: null` and an
additive warning; legacy hook-observed file progress remains available.

**Failure modes**

- `ToolError` — PRD is not in `reviewed` or `approved` status, or missing.
- `ToolError` — `ClaimError` from `ClaimManager` (task already claimed, task not in claimable state, etc.).
- `ToolError` — bounded Git-plan refusal; no active claim or invocation-owned Git artifact remains.
- `ToolError` — state directory not found.

**When to call**: after `get_next_task` or `get_task` confirms the task is ready and
the agent has checked conflicts.

---

### `release_task`

Releases the active claim on a task held by `actor`. The claim is located by task ID, then
the exact persisted owner must match `actor`; a mismatch refuses with structured owner and
`ANVIL_ACTOR` / actor-argument remedies. Stale-claim reaping runs first.

Actor values on MCP lifecycle tools are local coordination and audit attribution, not
cryptographic authentication.

**Inputs**

| Parameter | Type            | Required | Default |
|-----------|-----------------|----------|---------|
| `task_id` | `string`        | yes      |         |
| `actor`   | `string`        | yes      |         |
| `reason`  | `string \| null` | no      | `null`  |
| `target_kind` | `"task" \| "bundle"` | no | `"task"` |
| `cwd` | `string \| null` | no | `Path.cwd()` |

For a coordinator bundle claim, pass the bundle ID in `task_id` and explicitly set
`target_kind="bundle"`. The explicit discriminator prevents a same-named task from being
released accidentally.

**Output**

```json
{
  "released": true,
  "claim_id": "C001",
  "actor_identity": {"actor": "agent-x", "authenticated": false, "notice": "..."}
}
```

**Failure modes**

- `ToolError` — no active claim found for the task.
- `ToolError` — `ClaimError` from `ClaimManager`.
- `ToolError` — state directory not found.

**When to call**: when an agent determines it cannot complete a task and wants to return it
to the `ready` pool for another agent to pick up.

---

### `renew_claim`

Extends the lease on an active claim. Use this as a heartbeat during long-running work to
prevent the claim from going stale. Stale-claim reaping runs first, so the claim must still
be active at the point of the call.

**Inputs**

| Parameter        | Type     | Required | Default |
|------------------|----------|----------|---------|
| `task_id`        | `string` | yes      |         |
| `actor`          | `string` | yes      |         |
| `extend_seconds` | `int`    | no       | `900`   |
| `target_kind`    | `"task" \| "bundle"` | no | `"task"` |
| `cwd`            | `string \| null` | no | `Path.cwd()` |

`extend_seconds` is converted to minutes (floor, minimum 1). For task claims, the default
extends by 15 minutes from the time of the call. For bundle claims it adds that interval to
the later of the current lease expiry or the current time.
For a coordinator bundle lease, pass the bundle ID as `task_id` and set
`target_kind="bundle"`.

**Output**

```json
{
  "lease_expires_at": "2026-05-25T14:30:00+00:00",
  "renewed": true,
  "actor_identity": {"actor": "agent-x", "authenticated": false, "notice": "..."},
  "progress": {"source": "attestation", "digest": "...", "generation": 2, "trust_mode": "configured_issuer_verified"}
}
```

**Failure modes**

- `ToolError` — no active claim found (claim may have already gone stale).
- `ToolError` — `ClaimError` from `ClaimManager`.
- `ToolError` — state directory not found.

**When to call**: every ~5 minutes while actively working on a claimed task (recommended
in the execute skill). Missing a renewal window causes the claim to go stale and the task
to re-enter the `ready` pool.

---

### `submit_progress`

Records an in-progress note for a task without changing its status. With ordinary text it
writes a `progress.noted` event to the JSONL audit log. It does not require a claim, but
when the task has an active claim the exact owner must supply the progress actor.
`phase` is an optional structured label ("build", "tests", "review-fixes", …)
for the heartbeat bus; `detail` is free-text elaboration for the phase.
Alternatively, pass strict canonical `attestation_base64` plus the project `cwd` to verify
and record `progress.attested`. The receipt includes digest, generation, kind, and trust
mode. The next renewal consumes that accepted artifact exactly once. Free-text notes never
authorize renewal.
See [Attesting progress from an external writer](how-to/attesting-external-progress.md)
for both payload kinds, canonical bytes, signing/trust rules, and base64 encoding.

**Inputs**

| Parameter | Type     | Required |
|-----------|----------|----------|
| `task_id` | `string` | yes      |
| `actor`   | `string` | yes      |
| `notes`   | `string \| null` | unless no attestation |
| `phase`   | `string` | no       |
| `detail`  | `string` | no       |
| `attestation_base64` | `string \| null` | no |
| `cwd` | `string \| null` | no |

**Output**

```json
{
  "recorded": true,
  "event_action": "progress.attested",
  "attestation": {"digest": "...", "generation": 2, "kind": "commit", "trust_mode": "configured_issuer_verified"}
}
```

**Failure modes**

- `ToolError` — task not found.
- `ToolError` — state directory not found.

**When to call**: to emit a mid-task checkpoint visible in the event log — for example,
after completing one sub-step of a multi-step task, so the audit trail reflects partial
progress.

---

### `submit_completion_evidence`

Submits completion evidence for a task. Requires an active claim. Emits an
`evidence.submitted` event that auto-releases the claim and transitions the task to
`needs_review`. Mirrors `anvil submit` from the CLI.

**Inputs**

| Parameter        | Type                    | Required | Default |
|------------------|-------------------------|----------|---------|
| `task_id`        | `string`                | yes      |         |
| `actor`          | `string`                | yes      |         |
| `commands_run`   | `list[string]`          | yes      |         |
| `files_changed`  | `list[string]`          | yes      |         |
| `output_excerpt` | `string \| null`        | no       | `null`  |
| `pr_url`         | `string \| null`        | no       | `null`  |
| `commit_sha`     | `string \| null`        | no       | `null`  |
| `category`       | `string \| null`        | no       | `null`  |
| `command_proof_artifacts_base64` | `list[string] \| null` | no | `null` |
| `cwd`            | `string \| null`        | no       | `null`  |

`category` (evidence contracts, issue #153) is the evidence role —
`completion` (the default when omitted), `diagnostic`, `blocked`,
`advisory`, or `promotion_quality`. `diagnostic`/`advisory` evidence can
never satisfy a completion claim; an invalid value raises `ToolError`
(`invalid_category`). Mirrors `anvil submit --category`.

`command_proof_artifacts_base64` contains canonical base64 encodings of
canonical JSON claim-bound command-proof envelopes. The entire batch is loaded
and verified before the single evidence event is appended. Every artifact must
match the explicit claim owner, claim ID and generation, task/PRD revision,
repository and canonical `cwd`, and one exact `commands_run` value. One invalid,
duplicate, stale, oversized, nonzero, or mismatched artifact refuses the whole
submission. These artifacts attest reported bytes; Anvil does not execute the
command. Unsigned artifacts are reported as `claim_owner_self_attested`; only a
valid configured issuer is reported as `configured_issuer_verified`.
Configured-issuer membership is revalidated from `ANVIL_TRUST_LIST` or
`~/.anvil/trust.txt` both at live append and during event-log replay. Operators
must back up and restore the effective trust list with state and retain each
signing public key or fingerprint; missing or changed membership fails closed
and aborts append or replay. Self-attested proof replay does not depend on the
external trust list.

`output_excerpt` is descriptive only. Text such as `exit: 0` in that field does
not create a typed command proof or satisfy `required_proofs`.

**Output**

```json
{
  "evidence_id": "EV3A9F1C2D",
  "task_status": "needs_review",
  "next_ready": {
    "id": "T014",
    "title": "Implement the converter",
    "priority": "high"
  },
  "claim_bound_command_proofs": [
    {
      "digest": "…",
      "generation": 1,
      "trust_mode": "claim_owner_self_attested",
      "issuer_id": null,
      "command": "pytest -q",
      "output_sha256": "…"
    }
  ],
  "hook_command_proofs": [],
  "missing_claim_bound_proofs": [],
  "missing_legacy_evidence": []
}
```

`evidence_id` is an `"EV"` prefix followed by 8 uppercase hex characters, generated at
call time.

The four proof fields are additive receipts. `claim_bound_command_proofs`
describes validated imports without echoing their bounded output bytes;
`hook_command_proofs` identifies exact-claim hook captures and returns their
`claim_id`, `generation`, `actor`, `semantic_digest`, and
`source: "hook_claim_bound"` audit metadata;
`missing_claim_bound_proofs` and `missing_legacy_evidence` keep typed proof gaps
distinct from descriptive evidence gaps.

`next_ready` names the next claimable task now that this one has left the active set —
respecting dependencies, active claims, conflict groups, and file-conflict exclusions
(a task whose `likely_files` overlap another agent's active claim is never named). It is
`null` when no task is claimable, letting the agent chain straight into the next piece of
work without a second round-trip to `get_next_task`.

**Failure modes**

- `ToolError` — task not found.
- `ToolError` — no active claim found for the task (claim the task before submitting).
- `ToolError` — `EventRejected` from the backend.
- `ToolError` — state directory not found.

**When to call**: when the agent's work is complete and it is ready to hand off to review.
This is the last step in the execute loop before the agent exits.

---

### `update_task_status`

Transitions a task to a new status. Only the following transitions are permitted:

| From           | To allowed        |
|----------------|-------------------|
| `drafted`      | `ready`           |
| `ready`        | `drafted`         |
| `in_progress`  | `blocked`         |
| `claimed`      | `blocked`         |
| `blocked`      | `in_progress`     |

Any other transition raises a `ToolError` with the current status and the allowed targets.
Stale-claim reaping runs first.

**Inputs**

| Parameter   | Type                               | Required | Default |
|-------------|------------------------------------|----------|---------|
| `task_id`   | `string`                           | yes      |         |
| `to_status` | `"drafted" \| "ready" \| "blocked" \| "in_progress"` | yes      |         |
| `actor`     | `string`                           | yes      |         |
| `reason`    | `string \| null`                   | no       | `null`  |

**Output**

```json
{
  "from_status": "drafted",
  "to_status": "ready"
}
```

**Failure modes**

- `ToolError` — task not found.
- `ToolError` — transition not allowed (message includes current status and valid targets).
- `ToolError` — `EventRejected` from the backend.
- `ToolError` — state directory not found.

**When to call**: when a planner agent marks reviewed tasks as `ready` before a work wave,
or when a sentinel marks an `in_progress` task as `blocked` after discovering a dependency
that cannot be resolved yet.

---

### Execution bundle tools

These tools coordinate a milestone-sized bundle through one coordinator claim and one
bounded review gate. Every tool accepts optional `cwd`; mutators also require `actor`.
`create_bundle` is planning-gated, while the remaining bundle tools are on the default
execution surface. Bundle lease renewal and release reuse `renew_claim` and `release_task`
with `target_kind="bundle"`; the default remains `target_kind="task"` so colliding task and
bundle IDs are unambiguous.

### `create_bundle`

Creates a planned bundle. Required inputs are `bundle_id`, `prd_id`, ordered `task_ids`,
`coordinator`, and `actor`. Optional policy inputs are `max_tasks` (12),
`max_serial_stages` (6), `max_reviews` (3), `max_rereviews` (1), and
`required_angles`. Returns `BundleDetailResponse`. Member tasks must exist in the named
PRD and satisfy the bundle's dependency and throughput constraints.

### `list_bundles`

Lists bundles in stable ID order. Optional `prd_id` filters the result. Returns
`BundleListResponse` with compact, explicitly typed bundle records.

### `get_bundle`

Reads one bundle by `bundle_id`, including its coordinator claim and recorded review
verdicts. Returns `BundleDetailResponse` and fails with `bundle_error` when absent.

### `claim_bundle`

Atomically claims a planned bundle and creates member task authorizations. Inputs are
`bundle_id`, `actor`, optional `lease_minutes` (240), and optional `shared_tree` (false).
Returns the bundle, coordinator claim, and isolation warnings. Under required worktree
isolation, callers must use the Git-aware CLI claim path or explicitly opt into a shared
tree. The response also includes the exact coordinator identity and structured bundle
renew, release, progress, and complete continuations; it never substitutes task submit.

### `generate_bundle_packet`

Renders the aggregate coordinator packet for `bundle_id`. Inputs are `actor` and optional
`format` (`markdown` or `json`). Returns `WorkPacketResponse`.

### `submit_bundle_progress`

Records coordinator progress with `bundle_id`, `actor`, and `phase`; optional inputs are
`detail` and `member_task_ids`. Set `complete=true` only after every member has acceptable
completion evidence. Completion is retry-safe and does not append a progress event when
readiness fails. Returns the bundle plus readiness fields; an unready bundle fails with
`bundle_not_ready` and per-member blockers.

### `record_bundle_review`

Records one independent verdict. Inputs are `bundle_id`, `actor`, `review_round`, `angle`,
`decision` (`approve`, `reject`, or `needs_changes`), and optional `notes`. Returns the
bundle and current gate. Duplicate reviewers and invalid rounds fail closed.

### `finalize_bundle_review`

Finalizes a passed bounded review gate for `bundle_id` as `actor`. Returns the bundle and
gate; missing angles, insufficient independent approvals, or blocking verdicts fail with
`bundle_error`.

### `checkpoint_bundle`

Records delivery metadata for a bundle. The recommended sequence checkpoints after review,
but the operation itself validates only the bundle and delivery reference. Inputs are
`bundle_id`, `actor`, and at least one of `commit_sha` or `pr_url`. Returns
`BundleCheckpointResponse`.

### `reconcile_bundle`

Idempotently reconciles delivery state from `commit_sha` or `pr_url`, plus optional
`merged`, for `bundle_id`. At least one delivery reference is required; `merged` alone is
not sufficient. Returns `BundleDetailResponse`; a proven integration advances the bundle
without duplicating prior checkpoint events.

### `supersede_bundle`

Marks `bundle_id` superseded by `replacement_bundle_id` while retaining its audit history.
Requires `actor` and returns `BundleDetailResponse`. A replacement created after the
source reaches `replan_required` may retain the same member task IDs; supersession reopens
shared review-state tasks for fresh replacement evidence without deleting prior evidence.

See [Coordinating a milestone bundle](how-to/coordinating-a-bundle.md) for ownership,
bounded delegation, stalled-worker recovery, review rework, adoption, supersession, and
delivery examples.

---

### Workflow tools

These eight tools complete the lifecycle so a non-Claude-Code MCP client can run the entire
PRD-to-done flow without touching the CLI. All eight accept an optional `cwd` argument so a
single MCP session can target multiple project roots. None of them perform git operations.

---

#### Bootstrap & status

---

### `init_project`

Scaffolds a `.anvil/` directory in the target project root. Creates the canonical
layout (`config.yaml`, `state.db`, `events.jsonl`, `packets/`), seeds the project row, and
emits `project.created` + `state.initialized`. Mirrors `anvil init` minus git
operations.

**Inputs**

| Parameter | Type             | Required | Default        |
|-----------|------------------|----------|----------------|
| `name`    | `string \| null` | no       | basename of `cwd` |
| `cwd`     | `string \| null` | no       | `Path.cwd()`   |

**Output**

```json
{
  "project_id": "from-mcp",
  "project_name": "From MCP",
  "state_dir": "/abs/path/.anvil",
  "created": true
}
```

**Failure modes**

- `ToolError` — directory is the plugin root (refuses to init inside the plugin).
- `ToolError` — `.anvil/` already exists (use CLI `init --force` to reinit).
- `ToolError` — scaffold I/O failure.

**When to call**: the very first MCP call against a fresh project root. `init_project` is
planning-gated — it is not on the wire unless the server runs with
`ANVIL_MCP_PLANNING=1` (see [Tool surface gating](#tool-surface-gating)).

---

### `get_project_status`

Returns PRD status, task counts by state, active claims, ready-queue depth, and
initialization flag. Mirrors `anvil status`. Returns `initialized: false` with empty
counts when `.anvil/` is absent — does **not** raise. Use this as the canonical
"am I bootstrapped?" probe.

**Inputs**

| Parameter | Type             | Required | Default      |
|-----------|------------------|----------|--------------|
| `cwd`     | `string \| null` | no       | `Path.cwd()` |
| `prd_id`  | `string \| null` | no       | explicit value, `ANVIL_PRD`, then single/default PRD |

**Output**

```json
{
  "initialized": true,
  "project_id": "proj-test",
  "project_name": "Status Project",
  "state_dir": "/abs/path/.anvil",
  "prd_status": "reviewed",
  "task_counts": { "proposed": 0, "drafted": 0, "...": "..." },
  "total_tasks": 3,
  "ready_queue_depth": 2,
  "active_claim_count": 1
}
```

`get_project_status` differs from `get_project_summary` in two ways: it accepts an explicit
`cwd`, and it answers gracefully when the project is not initialized.

**Failure modes**

None — always returns a response.

---

#### PRD lifecycle

---

### `parse_prd`

Reads the resolver-managed default or named PRD source, or an explicit `file=`
path; parses via `anvil.planning.template.parse_prd`; and emits a
create-if-absent `prd.parsed` or non-destructive `prd.revised` event on success.
The resolver normally selects `~/.anvil/workspaces/<key>/.anvil/prd.md`, shared
by the repository's worktrees; `ANVIL_STATE_LAYOUT=local` opts into
`<cwd>/.anvil/prd.md`.
Parse errors are returned in the response (not raised) so the caller can decide
whether to fix and retry. Mirrors `anvil prd parse`.

**Inputs**

| Parameter | Type             | Required | Default                          |
|-----------|------------------|----------|----------------------------------|
| `file`    | `string \| null` | no       | managed source in resolver-selected state directory |
| `prd_id`  | `string \| null` | no       | default PRD                      |
| `cwd`     | `string \| null` | no       | `Path.cwd()`                     |

**Output**

```json
{
  "prd_status": "draft",
  "requirement_count": 2,
  "feature_count": 1,
  "task_count": 2,
  "errors": [],
  "error_count": 0,
  "errors_shown": 0,
  "errors_omitted": 0,
  "errors_truncated": false,
  "error_messages_truncated": 0,
  "prd_path": "default"
}
```

When `errors` is non-empty, no `prd.parsed` event is emitted (matching the CLI which exits 1
before applying); the caller should fix the PRD and re-call. Public diagnostics
contain at most 20 sanitized entries and 1,024 UTF-8 bytes per message; the
additive count fields distinguish a complete small list from a truncated one.

**Failure modes**

- `ToolError` — project not initialized.
- `ToolError` — selected PRD source missing or not a verified regular file.
- `ToolError` — selected PRD source exceeds the byte ceiling or is not UTF-8.
- `ToolError` — a concurrent parse/review/approval made the optimistic event
  precondition stale (the winning state is left unchanged).

`prd_path` is retained as a compatibility field name, but its value is the
stable source identity (`default`, a named PRD id, or `custom`), never a
filesystem path.

**When to call**: right after the user (or another agent) writes `prd.md`.

---

### `assess_prd`

Requires an initialized Anvil project, then reads and parses the selected PRD
and returns deterministic, location-aware
behavioural-readiness findings. The tool is read-only and advisory: it emits no
event and cannot block parse, review, approval, planning, claims, or an
explicitly autonomous run. It mirrors `anvil prd assess` and recognises
EARS/Gherkin-shaped acceptance criteria as an input without requiring either
grammar.

**Inputs**

| Parameter | Type | Required | Default |
|-----------|------|----------|---------|
| `file` | `string \| null` | no | selected PRD source |
| `prd_id` | `string \| null` | no | default PRD |
| `cwd` | `string \| null` | no | `Path.cwd()` |

**Output**

```json
{
  "prd_source": "default",
  "advisory": true,
  "count": 1,
  "findings": [{
    "id": "BR-001",
    "category": "user_context",
    "severity": "warning",
    "location": "## Summary",
    "message": "The summary does not name the person or role whose behaviour should change.",
    "challenge_question": "Who is the primary user or operator, and what situation are they in?"
  }]
}
```

Malformed or non-UTF-8 PRDs raise `ToolError`, matching the CLI's failed
assessment contract, and produce no findings. A caller that has explicitly
opted into challenge mode can ask one suggested question at a time; otherwise
it can simply report or ignore these advisory findings. Relative `file` paths
resolve against `cwd` on both the CLI and MCP surfaces.

---

### `review_prd`

Transitions the PRD: `draft → reviewed` (default) or `reviewed → approved` (when
`approve=true`). Emits `prd.reviewed` or `prd.approved`. Mirrors `anvil prd review`
and `prd review --approve`.

**Inputs**

| Parameter  | Type             | Required | Default   |
|------------|------------------|----------|-----------|
| `approve`  | `bool`           | no       | `false`   |
| `reviewer` | `string`         | no       | `"human"` |
| `notes`    | `string \| null` | no       | `null`    |
| `cwd`      | `string \| null` | no       | `Path.cwd()` |

**Output**

```json
{
  "from_status": "draft",
  "to_status": "reviewed",
  "reviewer": "alice"
}
```

**Failure modes**

- `ToolError` — no PRD found (run `parse_prd` first).
- `ToolError` — wrong starting status for the requested transition.
- `ToolError` — a concurrent same-revision lifecycle transition made the
  revision/status precondition stale; the winning state remains unchanged.
- `ToolError` — project not initialized.

---

#### Planning & scoring

---

### `plan_tasks`

Runs the planner pipeline against the current PRD: builds the complete canonical
feature/task graph, runs dependency + conflict-group inference, promotes
`proposed → drafted`, then persists the whole transition in one
`planning.batch_applied` event bound to the exact PRD revision and source digest.
A refusal or losing Git lineage cannot leave a partial or mismatched graph. Mirrors
`anvil plan`.

`use_llm` defaults to `true`: when the PRD has features but no `## Tasks` section, the
deterministic parser yields zero tasks, so `plan_tasks` calls the LLM task-generation
backstop, appends the generated `## Tasks` section to `prd.md`, and re-parses before any
events are emitted. The provider defaults to the Claude subscription via the Agent SDK; pin
`anthropic`/`bedrock`/`custom` in `.anvil/config.yaml`, or set `llm_fallback: true` for
env auto-detect. Set `use_llm=false` to opt out and keep the deterministic parse; if the PRD
still has zero tasks in that case, `plan_tasks` returns `task_count=0` rather than raising
(unlike the CLI's `--no-llm`, which fails loudly in the same scenario). When the PRD already
has a `## Tasks` section, `use_llm` has no effect — the deterministic parse is always used.

**Inputs**

| Parameter     | Type             | Required | Default      |
|---------------|------------------|----------|--------------|
| `cwd`         | `string \| null` | no       | `Path.cwd()` |
| `use_llm`     | `bool`           | no       | `true`       |
| `prune_force` | `bool`           | no       | `false`      |
| `prd_id`      | `string \| null` | no       | `null`       |

`prune_force`: tasks that were in state.db but are absent from the re-parsed PRD are orphans.
If any orphan has advanced past `ready` status (claimed, in progress, needs review, etc.),
the tool raises `ToolError` rather than silently discarding claim/evidence history — pass
`prune_force=true` to delete them anyway (the audit trail is preserved either way).

`prd_id`: PRD partition to plan (multi-PRD). A non-default id reads its portable source under `.anvil/prds/`,
scopes orphan-prune to that partition, and stamps the partition into every feature/task
event. `null` (or `"default"` / `"prd"`) keeps the bare `.anvil/prd.md` source and the
default partition.

**Output**

```json
{
  "feature_count": 1,
  "task_count": 2,
  "conflict_group_count": 0,
  "warnings": [],
  "warning_count": 0,
  "warnings_shown": 0,
  "warnings_omitted": 0,
  "warnings_truncated": false,
  "warning_messages_truncated": 0,
  "llm_generated": false,
  "llm_provider": null,
  "pruned_task_ids": [],
  "pruned_feature_ids": []
}
```

`warnings` mirrors the bounded parse errors surfaced as warnings during plan
(matching the CLI); the adjacent count fields disclose omitted diagnostics.
`llm_generated` is `true` when this call drafted a `## Tasks` section via the LLM backstop
and appended it to `prd.md`; `llm_provider` names the resolved provider in that case, else
`null`. `pruned_task_ids` / `pruned_feature_ids` list any IDs deleted by the orphan-prune
step (empty when nothing was pruned).

**Failure modes**

- `ToolError` — project not initialized.
- `ToolError` — PRD file not found.
- `ToolError` — LLM task-generation backstop failed (no provider available, the provider
  call itself failed, or the response contained no parseable task blocks).
- `ToolError` — orphan tasks advanced past `ready` and `prune_force` was not set.
- `ToolError` — `EventRejected` from the backend during event append.

**When to call**: right after `review_prd` (draft → reviewed) so the plan reflects the
latest PRD content.

---

### `score_tasks`

Runs the rule-based scoring engine on a single task or all unscored tasks in
one resolved PRD. Set `all_prds=true` for an explicit project-wide run. Scored
and skipped counts, ownership checks, and the recursive expansion queue all use
the same scope. Emits `task.scored` per scored task and mirrors
`anvil score [TASK_ID]` in deterministic mode.

**Inputs**

| Parameter  | Type             | Required | Default      |
|------------|------------------|----------|--------------|
| `task_id`  | `string \| null` | no       | `null` (score all unscored) |
| `prd_id`   | `string \| null` | no       | resolved env/default/single PRD |
| `all_prds` | `boolean`        | no       | `false`      |
| `cwd`      | `string \| null` | no       | `Path.cwd()` |

**Output**

```json
{
  "prd_id": "default",
  "all_prds": false,
  "scored": [
    {
      "task_id": "T001",
      "complexity": 3,
      "parallelizability": 4,
      "context_load": 2,
      "blast_radius": 3,
      "review_risk": 2,
      "agent_suitability": 4
    }
  ],
  "skipped_already_scored": 0
}
```

**Failure modes**

- `ToolError` — `task_id` provided but not found.
- `ToolError` — `prd_id` and `all_prds=true` are combined, the selected PRD
  does not exist, or an explicit task belongs to another PRD.
- `ToolError` — project not initialized.
- `ToolError` with `score_incomplete` — a scoring implementation returned an
  incomplete or out-of-range dimension. The entire request is validated before
  any `task.scored` event is appended, so a mixed valid/invalid batch leaves all
  prior scores and the event frontier unchanged.

---

### `review_tasks`

Promotes tasks through `drafted → reviewed → ready` using the gate functions in
`anvil.state.transitions`. By default it resolves one PRD using explicit
`prd_id`, `ANVIL_PRD`, then single/default selection. Set `all_prds=true` for
an explicit project-wide mutation. Both promotion passes and durable risk-score
confirmation stay inside the reported scope. Mirrors `anvil review tasks`.

**Inputs**

| Parameter  | Type             | Required | Default      |
|------------|------------------|----------|--------------|
| `cwd`      | `string \| null` | no       | `Path.cwd()` |
| `prd_id`   | `string \| null` | no       | resolved     |
| `all_prds` | `boolean`        | no       | `false`      |

**Output**

```json
{
  "promoted_to_reviewed": ["T001", "T002"],
  "promoted_to_ready":    ["T001", "T002"],
  "blocked": [],
  "prd_id": "default",
  "all_prds": false
}
```

A task that fails the `drafted → reviewed` gate (missing acceptance criteria or
verification commands) appears in `blocked` instead of either promotion list.

**Failure modes**

- `ToolError` — project not initialized, selected PRD is missing, or
  `prd_id` is combined with `all_prds=true`.

---

#### Review gate

---

### `apply_review_decision`

Applies a human review decision to a task in `needs_review` status. With `approve=true` the
task moves through `needs_review → accepted → done` (the backend handles the auto-promotion).
With `approve=false` (and a non-empty `reason`) the task is rejected — typically returned
to `drafted` for rework. Mirrors `anvil apply TASK_ID --approve` and `--reject
--reason TEXT`.

**Inputs**

| Parameter   | Type             | Required | Default   |
|-------------|------------------|----------|-----------|
| `task_id`   | `string`         | yes      |           |
| `approve`   | `bool`           | yes      |           |
| `reviewer`  | `string`         | no       | `"human"` |
| `reason`    | `string \| null` | no       | `null` (required when `approve=false`) |
| `reason_code` | rejection reason enum \| null | no | `null` |
| `quality_findings` | list of quality-finding enums \| null | no | `null` |
| `strict`    | `bool \| null`   | no       | `null` (defers to config `strict_evidence`) |
| `cwd`      | `string \| null` | no       | `Path.cwd()` |

For rejection, `reason_code` and `quality_findings` are assertions only. The
backend derives the category under the append lock from the latest persisted
review attempt and its claim. Typed findings always produce counting
`quality`; an incomplete typed-evidence gate with no findings may produce
non-counting `evidence_resubmission`; `process` requires an exact persisted
process predicate. Omitted, ambiguous, or falsified assertions remain
`quality`. The response includes the immutable `rejection` object and the
immediate `rejection_metrics` projection. That projection includes the
persisted review timestamp as `as_of`, numerator, denominator, rate, floor,
window, queue depth/cap, counting classification, and recovery guidance. A
non-counting evidence/process rejection therefore cannot lower the displayed
rate.

When the task declares an **evidence contract** (named `claims` and/or
`Artifact assertions`, see [PRD template](prd-template.md)), `approve=true`
is held to it **independent of `strict`/`strict_evidence`**: the artifacts
are re-evaluated at approval time and an unproven enforceable claim raises
`ToolError` (`claim_unproven`) with the task left in `needs_review`. Named
claims always enforce; on the implicit task-level claim an unmet command
proof alone defers to `strict_evidence`, while an artifact contradiction,
missing artifact, or `blocked`-category evidence (or a `diagnostic_only`
verdict from `diagnostic`/`advisory`-category evidence) always enforces.

**Output**

```json
{
  "task_id": "T001",
  "decision": "accepted",
  "from_status": "needs_review",
  "to_status": "done",
  "reviewer": "alice",
  "next_ready": {
    "id": "T002",
    "title": "Implement the error handler",
    "priority": "medium"
  }
}
```

`to_status` reflects the backend's post-promotion status (typically `done` on approval).

`next_ready` names the next claimable task after this disposition — an approval that marks
a task `done` can unblock dependents — using the same dependency-, claim-, conflict-group-
and file-overlap-aware selection as `submit_completion_evidence`. It is `null` when no task
is claimable.

**Failure modes**

- `ToolError` — task not found.
- `ToolError` — task not in `needs_review` status (submit evidence first).
- `ToolError` — `approve=false` without a `reason`.
- `ToolError` — `claim_unproven`: the task's evidence contract has an
  enforceable unproven claim (approval refused; task stays `needs_review`).
- `ToolError` — `evidence_incomplete`: strict evidence mode and required
  evidence is missing.
- `ToolError` — project not initialized.

---

### Decision resolution

One read-only tool that surfaces unresolved PRD items so the `resolve-decisions` skill can
drive Q&A with the user. Detection logic lives in `anvil.planning.decisions` and is
shared with the CLI subcommand `anvil prd find-decisions`.

---

### `find_decisions`

Scans the PRD for three categories of items needing a human decision:

1. **`needs_decision`** — inline `[NEEDS DECISION]` markers anywhere in the raw markdown
   (with an optional `: <question>` payload).
2. **`open_question`** — items under the `## Open Questions` section (skipping
   "none identified" placeholders).
3. **`missing_field`** — tasks in the backend whose `acceptance_criteria` or
   `verification.commands` are empty (gates the review pipeline would block on).

The tool is read-only — no events are emitted. It is the sibling of `parse_prd` intended to
power the `resolve-decisions` skill's Q&A loop. Mirrors `anvil prd find-decisions`.

**Inputs**

| Parameter | Type             | Required | Default      |
|-----------|------------------|----------|--------------|
| `cwd`     | `string \| null` | no       | `Path.cwd()` |

**Output**

```json
{
  "prd_id": "v0.2",
  "prd_source": "v0.2",
  "decisions": [
    {
      "id": "ND-001",
      "kind": "needs_decision",
      "location": "Summary (line 5)",
      "text": "which format?",
      "context_paragraph": "The system must serialize inputs [NEEDS DECISION: which format?].",
      "suggested_resolution_field": "inline rewrite"
    }
  ],
  "counts_by_kind": {
    "needs_decision": 1,
    "open_question": 0,
    "missing_field": 0
  },
  "total": 1
}
```

Stable order: all `needs_decision` first (in source order), then `open_question`
(in PRD order), then `missing_field` (in task-ID order). Resolution is iterative — the
agent walks the list and drives one Q&A per entry, so ordering shapes the conversation.

**Failure modes**

- `ToolError` — project not initialized.
- `ToolError` — PRD file missing. (Mirrors `parse_prd` rather than returning an empty
  response, so a fresh project doesn't silently look "resolved".)

**CLI equivalent**

```bash
anvil prd find-decisions
anvil prd find-decisions --prd v0.2
anvil prd find-decisions --file path/to/prd.md --prd v0.2
```

**When to call**: after `parse_prd` succeeds but before `review_prd` or `plan_tasks`, so
unresolved markers and missing fields are surfaced and resolved before downstream tools
treat the PRD as ready.

---

### Introspection

One read-only tool that returns a machine-readable manifest of the command surface. It is
the sibling of `anvil describe` and needs no initialized project.

---

### `describe_surface`

Returns a machine-readable manifest of the anvil command surface: the CLI subcommands,
their exact root/group/leaf long options, and the MCP tool names this engine exposes, plus build
identity, engine version, schema version, a stable `api_version` to pin against,
and the versioned provider-read operation catalog with packaged schema and fixture resources.
Introspected live from the same builder the CLI `anvil
describe` uses — the CLI and MCP surfaces can never disagree — so it never needs a project
to be initialized. This tool is planning-gated (hidden from the wire unless
`ANVIL_MCP_PLANNING=1`; see [Tool surface gating](#tool-surface-gating)), but the
introspection surfaces themselves (`anvil describe`, the `--help` tool list, the Docker
catalog smoke test) always report the full 36-tool surface regardless of the gate.

**Inputs**

None.

**Output**

```json
{
  "api_version": "14",
  "engine_version": "0.6.4",
  "display_version": "0.6.4",
  "build_kind": "release_artifact",
  "commit": "abcdef123456",
  "tag": "v0.6.4",
  "tag_distance": 0,
  "dirty": false,
  "schema_version": 20,
  "envelope": "v1.24",
  "cli": {
    "commands": ["apply", "...", "prd source-name", "..."],
    "options": {"prd source-name": ["--cwd", "--json", "--prd"], "...": []},
    "contracts": [
      {"path": [], "kind": "group", "flags": ["--install-completion", "--show-completion", "--version"]},
      {"path": ["prd"], "kind": "group", "flags": []},
      {"path": ["prd", "source-name"], "kind": "command", "flags": ["--cwd", "--json", "--prd"]}
    ],
    "contract_count": 79,
    "count": 70
  },
  "mcp": {
    "tools": ["claim_task", "..."],
    "count": 36
  },
  "operation_catalog": {
    "catalog_version": 1,
    "operations": [
      {
        "operation_id": "state.prd.content",
        "operation_version": 1,
        "effect": "read",
        "transport": {"kind": "cli", "command": "prd show", "json_required": true},
        "schema_resources": {
          "input": "contracts/provider-reads/v1/prd-content-input.schema.json",
          "output": "contracts/provider-reads/v1/prd-content-output.schema.json",
          "error": "contracts/provider-reads/v1/read-error.schema.json"
        }
      },
      {
        "operation_id": "state.project.snapshot",
        "operation_version": 1,
        "effect": "read",
        "transport": {"kind": "cli", "command": "project snapshot", "json_required": true},
        "schema_resources": {
          "input": "contracts/provider-reads/v1/project-snapshot-input.schema.json",
          "output": "contracts/provider-reads/v1/project-snapshot-output.schema.json",
          "error": "contracts/provider-reads/v1/read-error.schema.json"
        }
      }
    ]
  }
}
```

`cli.commands`, `cli.contracts`, and `mcp.tools` are sorted for stable, diffable output.
Grouped CLI commands render space-joined (e.g. `"prd parse"`) so the exact invocation path
is visible. Each contract records long options owned by that exact node; a group does not
inherit options from its descendants. Short aliases such as `-V` are intentionally outside
this skill/release contract.

Provider consumers must fail closed before reading state unless the manifest
contains API version 9, operation-catalog version 1, the required operation at
version 1, and the exact version-1 schema resource paths. Do not infer
compatibility from the engine version. The provider reads use their cataloged
CLI transports; an MCP-only host can still discover and pin the same contract
with `describe_surface`. See [Provider read contracts](contracts/provider-reads-v1.md)
for packaged fixtures, limits, digests, refusal behavior, and the Workbench
hierarchy mapping.

**Failure modes**

None — always returns a response.

**When to call**: when an MCP-only host needs to discover the full command surface (or pin
against `api_version`) without shelling out to the CLI.

---

## Error model

Every failure raises a FastMCP `ToolError`. Most messages are human-readable strings
describing what failed, what was expected, and what the agent should do next. There is no
outer protocol envelope — `ToolError` is surfaced directly to the MCP client.

Schema compatibility failures are the exception: their `ToolError` message is a bounded,
path-free JSON object so clients can act on stable fields without parsing backend text:

```json
{"error":{"code":"schema_mismatch","database_schema":21,"direction":"newer","engine_version":"0.6.4","guidance":"Upgrade anvil-state, then restart the CLI, harness, and MCP server. Do not delete state.","remediation_code":"upgrade_engine","restart_required":true,"supported_schema":20}}
```

The server closes a backend that fails initialization. Because each tool call opens fresh
state, a long-lived MCP connection detects a database that becomes incompatible between
calls and returns the same closed error without restarting the transport.

Example error message from `claim_task` when the PRD gate fires (the task's
owning PRD is not yet reviewed/approved; enforced by ClaimManager's per-PRD gate):

```
Task 'T012' cannot be claimed: PRD must be in {'reviewed', 'approved'}, got 'draft'.
Review and approve the PRD before claiming tasks.
```

Example error message from `update_task_status` when the transition is invalid:

```
Cannot transition task 'T012' from 'done' to 'ready'. Allowed targets from 'done': none.
This tool supports only: drafted↔ready and blocked toggle.
```

The spec describes a structured `{code, message, target_id, payload}` envelope for future
versions; the current implementation uses the `ToolError` string directly. Agents should
treat any `ToolError` as a terminal condition for the current operation and log the message
before deciding whether to retry, release, or escalate.

---

## Stale-claim reaping

`get_next_task`, `claim_task`, `claim_bundle`, `release_task`, `renew_claim`, `submit_progress`,
`submit_completion_evidence`, `update_task_status`, and `get_project_summary` call
`detect_and_release_stale` before performing their operation. Other tools, including
the remaining read-only listers, do not promise reaping.

Reaping scans all active claims, identifies those whose `lease_expires_at` timestamp has
passed, marks them stale, and returns the associated tasks to the `ready` pool. If the
reaper itself throws an exception, the error is swallowed and the main operation proceeds
(best-effort, never blocking).

An MCP-only queue loop may call `get_next_task` directly: it performs the same
best-effort stale sweep as `anvil next` before selecting a candidate.

---

## Publishing to the Docker MCP catalog

The stdio server ships a `Dockerfile` (repo root) and a Docker MCP catalog manifest
(`server.yaml`) so it can be distributed through the
[Docker MCP catalog / registry](https://github.com/docker/mcp-registry). This lets any
Docker-MCP-Gateway user run anvil as a containerized MCP server without a local `uv`
or Python toolchain — the image bundles a pinned CPython and the locked dependency set.

### Image contents and statelessness

The image packages only what the MCP surface needs: `bin/pyproject.toml`, `bin/uv.lock`,
`bin/src/anvil/`, and `README.md`. It installs dependencies from the lockfile with
`uv sync --frozen --no-dev` (no LLM-provider extras — those stay opt-in, matching the host
install) and runs as a non-root `fakoli` user. **No project state is baked into the image.**
The engine resolves `.anvil/state.db` from `ANVIL_ROOT` (falling back to the
working directory), so the host project is **bind-mounted at runtime**.

### Build and smoke test

```bash
# From the repo root (build context = repo root):
docker build -t anvil-mcp .

# Smoke test: the entry point handles --help/--version and exits 0 without
# opening a backend or blocking on stdio. This is the catalog smoke test.
docker run --rm anvil-mcp --help
docker run --rm anvil-mcp --version
```

The `--help` page lists every registered MCP tool (introspected live from the FastMCP
surface, so it never drifts) and documents the `ANVIL_ROOT` bind-mount convention.

### Run against a host project

```bash
docker run --rm -i \
  -v "$PWD:/project" \
  -e ANVIL_ROOT=/project \
  anvil-mcp
```

`-i` keeps stdin open for the stdio transport. `ANVIL_ROOT=/project` makes the
server look for `/project/.anvil` **literally**, so the mounted project must
carry its state in-tree: initialise it with `ANVIL_STATE_LAYOUT=local anvil
init` (a bare `anvil init` puts state in the host's `~/.anvil/workspaces/`,
which the container never sees), or call the `init_project` tool over MCP
(requires `ANVIL_MCP_PLANNING=1`, since `init_project` is planning-gated).

Equivalent `mcpServers` entry for an MCP client that launches Docker directly:

```json
{
  "mcpServers": {
    "anvil": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "-v", "${PWD}:/project",
        "-e", "ANVIL_ROOT=/project",
        "anvil-mcp"
      ]
    }
  }
}
```

### Catalog submission

The repo-root `server.yaml` is the Docker MCP catalog manifest. To publish:

1. Fork [`docker/mcp-registry`](https://github.com/docker/mcp-registry) and copy this repo's
   `server.yaml` to `servers/anvil/server.yaml` in the fork.
2. Pin `source.commit` to the anvil commit you are publishing.
3. Validate locally (requires the Docker MCP toolkit and `task`):

   ```bash
   task build   -- --tools anvil   # builds mcp/anvil from ./Dockerfile
   task catalog -- anvil            # generates catalogs/anvil/catalog.yaml
   docker mcp catalog import "$PWD/catalogs/anvil/catalog.yaml"
   ```

4. Open a PR against `docker/mcp-registry`.

The manifest declares a `project_path` parameter that the gateway maps to the container's
`/project` volume, plus `ANVIL_ROOT=/project`, so catalog users get the bind-mount
wiring automatically.

---

## See also

- [`specs/2026-05-24-anvil-v0.md`](specs/2026-05-24-anvil-v0.md) — canonical
  design spec: data model, task lifecycle, phasing plan, integration contracts.
- [`hooks-reference.md`](hooks-reference.md) — claim discipline hooks: `check-claim.sh`,
  `record-file-change.sh`, `capture-evidence.sh`, `detect-state.sh`.
