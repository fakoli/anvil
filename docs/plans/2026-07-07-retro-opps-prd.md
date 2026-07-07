# Project: Retro-Mined Product Opportunities v1

## Summary

Implements the five highest-leverage product opportunities mined from the
post-session-findings retro corpus (10 sessions): risk-tiered review depth,
pre-merge base-freshness safety, lease/collision early warnings, structured
progress heartbeats, and a preflight GO/NO-GO validator. All five build on
existing anvil seams (six-dim scoring, fast-lane packets, claims/leases,
progress.noted events, doctor chassis); none require a schema migration.

## Goals

- Right-size review effort by task risk instead of uniform max-effort review.
- Catch stale-base and semantic merge drift before a human approves a task.
- Warn before leases expire and before file scopes collide, never blocking.
- Make long-running work observable: phase, elapsed, lease-expiry at a glance.
- Fail fast on malformed PRDs/artifacts before long workflows start.

## Requirements

- R001: Review tier derives fail-safe from existing scores — an unscored or unconfirmed-risk task never earns a lighter review than "standard"; unscored derives "max".
- R002: The derived review tier is surfaced in work packets, `anvil next`/`show`, and MCP task responses without any schema migration.
- R003: Base-freshness checking is local-first — with no reachable remote it degrades to the local default branch and never hard-fails.
- R004: Merged-tree verification (running task verification commands against the would-be merge result) is opt-in and never touches the user's working tree.
- R005: Lease pre-expiry and collision warnings are advisory only — hooks always exit 0 and task selection is never altered.
- R006: `anvil next` surfaces file-collision warnings against active claims for the recommended task.
- R007: Progress phase events reuse the existing `progress.noted` action with optional fields so old events replay unchanged.
- R008: `anvil status` and `anvil notify-digest` expose per-claim phase, elapsed time, and time-to-lease-expiry.
- R009: A preflight check produces a GO/NO-GO verdict (exit 0/1) covering PRD parse, unresolved decisions, verification paths, tree state, and claim sanity.
- R010: Plain `anvil doctor` (without --preflight) output and exit behavior remain byte-compatible.

## Features

### F001: Review Tier Planner

**Requirements:** R001, R002

Derive a light/standard/max `review_tier` from the existing six-dimension score
plus B45 confirmation flags, and surface it everywhere a reviewer or agent decides
how hard to review. Derive-only: no schema change, no persisted field. Extends
backlog item B15 (fast-lane half already shipped as T020 fast_lane_packet).

### F002: Merge Safety Check

**Requirements:** R003, R004

Pre-merge gate verifying a task branch's base freshness against the integration
branch, optionally running the task's verification commands against the merged
tree — catching semantic drift that clean textual merges miss. Local-first
degradation when offline. New `git_ops/freshness.py`; advisory/strict knob on
`apply --approve` mirroring the strict_evidence pattern.

### F003: Concurrency Sentinel

**Requirements:** R005, R006

Advisory early-warning layer for the claim lifecycle: pre-expiry lease warnings
(in-session via the PostToolUse heartbeat hook, out-of-session via notify-digest)
and file-collision visibility on `anvil next`. Never blocks, never changes
selection. Extends B46's progress-gated heartbeat.

### F004: Workflow Heartbeat Bus

**Requirements:** R007, R008

Structured progress events for long-running work: agents publish a phase label per
task; operators read phase, elapsed, and lease-expiry from `anvil status` and
`anvil notify-digest`. Enriches the existing `progress.noted` event (MCP
submit_progress already writes it; nothing reads it back today). No daemons.
Aligns with roadmap SL-4.

### F005: Preflight GO/NO-GO Validator

**Requirements:** R009, R010

One fast validator run before long workflows start: PRD parses cleanly, no
unresolved decision markers, verification paths resolve, git tree sane, no stale
claims. Delivered as `anvil doctor --preflight` reusing the doctor finding chassis.

## Tasks

### T001: Add derive-only review_tier() to planning/scoring.py with config knobs

**Feature:** F001
**Priority:** high
**Likely files:** bin/src/anvil/planning/scoring.py, bin/src/anvil/config.py, tests/test_scoring.py, tests/test_config.py

Add a pure `review_tier(task, *, config)` returning "light"/"standard"/"max".
Max when review_risk or blast_radius >= `review_tier_max_min` (default 4) or either
is None; light requires the existing `is_lightweight` fast-lane gate AND
review_risk <= `review_tier_light_risk_max` (default 2) AND both
`blast_radius_confirmed`/`review_risk_confirmed`; else standard. Add the two knobs
to `Config` with the same validation as the fast-lane ceiling pair. Reuse
`is_lightweight` as the single fast-lane predicate — do not re-implement it.

**Acceptance criteria:**

- An unscored task (any dim None) derives "max"; unconfirmed low-risk derives "standard", never "light".
- A confirmed complexity=2/blast=2/review_risk=2 task derives "light"; review_risk=4 derives "max" regardless of other dims.
- `review_tier_max_min` and `review_tier_light_risk_max` load from config.yaml, reject values outside 1-5, and default to 4/2 when absent.
- No new columns in state.db: SCHEMA_VERSION unchanged.

**Verification:**

- `uv run pytest ../tests/test_scoring.py -q`
- `uv run pytest ../tests/test_config.py -q`

### T002: Render review tier in work packets (markdown + JSON)

**Feature:** F001
**Priority:** high
**Likely files:** bin/src/anvil/context/packets.py, tests/test_context.py, tests/test_packet_quality.py
**Dependencies:** T001

Add a "Review tier" line and one tier-specific reviewer-guidance sentence to
`_render_markdown`, and a `review_tier` key to `_render_json`. Thread the config
ceilings through `render_packet` the same way fast_lane_required_evidence_max
already is. Fast-lane packet body otherwise unchanged.

**Acceptance criteria:**

- `anvil packet <task> --json` includes `"review_tier"` with the derived value for scored and unscored tasks.
- Markdown packet for a max-tier task contains the max-tier guidance line; light-tier packet contains the light guidance line.
- Existing packet snapshot/quality tests pass unmodified except for the added line.

**Verification:**

- `uv run pytest ../tests/test_context.py -q`
- `uv run pytest ../tests/test_packet_quality.py -q`

### T003: Expose review_tier on anvil next/show and MCP get_next_task/get_task

**Feature:** F001
**Priority:** medium
**Likely files:** bin/src/anvil/cli/claim.py, bin/src/anvil/mcp_server.py, tests/test_json_output.py, tests/test_mcp.py
**Dependencies:** T001

Compute the tier at read time (load config once per invocation) and add it to the
`next`/`show` JSON envelopes and the MCP `get_next_task`/`get_task` response
models. Human output of `anvil next` prints the tier on the task line.

**Acceptance criteria:**

- `anvil next --json` data includes `review_tier`; human output shows the tier.
- MCP `get_next_task` and `get_task` responses carry `review_tier` matching the CLI for the same task.
- A task whose risk scores are confirmed at `anvil review tasks` flips its derived tier without re-scoring or migration.

**Verification:**

- `uv run pytest ../tests/test_json_output.py -q`
- `uv run pytest ../tests/test_mcp.py -q`

### T004: Tier-aware finish/critic dispatch + update backlog B15

**Feature:** F001
**Priority:** medium
**Likely files:** skills/finish/SKILL.md, skills/claim/SKILL.md, docs/backlog/anvil-backlog.md, docs/cli-reference.md
**Dependencies:** T002, T003

Doc/skill-only: finish skill reads the packet's review tier and dispatches the
critic at matching depth (light = evidence gate only, standard = evidence + diff
review, max = adversarial pass). Update B15 in the backlog to record the shipped
fast-lane half and fold this tier work in — do not create a duplicate backlog item.

**Acceptance criteria:**

- skills/finish/SKILL.md documents the three-tier critic dispatch keyed on the packet field.
- B15 entry updated in place; no new B-number added for review tiers.
- docs/cli-reference.md documents the two new config knobs with defaults.

**Verification:**

- `grep -n "review tier" ../skills/finish/SKILL.md ../docs/cli-reference.md`
- `grep -c "review_tier" ../docs/backlog/anvil-backlog.md`

### T005: git_ops/freshness.py — base resolution + freshness/conflict report

**Feature:** F002
**Priority:** high
**Likely files:** bin/src/anvil/git_ops/freshness.py, bin/src/anvil/git_ops/__init__.py, tests/test_git_ops.py

New module with `resolve_base` (origin/<default> when a remote exists and a
timeout-bounded fetch succeeds, else local default branch, with `remote_checked`
and `reason` recorded) and `check_freshness` (behind-count via merge-base
rev-list + textual-conflict probe via `git merge-tree --write-tree`, reported as
unavailable on git < 2.38). Follow worktree.py's subprocess/timeout conventions;
every failure path returns a report, never raises.

**Acceptance criteria:**

- In a fixture repo with no remote, `resolve_base` returns the local default branch with `remote_checked=False` and a reason string; no exception.
- A branch 2 commits behind base reports `behind_count == 2`; an up-to-date branch reports 0.
- A branch that textually conflicts with base reports `has_conflicts=True`; on git without merge-tree support the report says the probe was skipped, not failed.
- No function in the module writes to the repo working tree.

**Verification:**

- `uv run pytest ../tests/test_git_ops.py -q`

### T006: anvil merge-check command with --run-checks merged-tree verification

**Feature:** F002
**Priority:** high
**Likely files:** bin/src/anvil/cli/merge_check.py, bin/src/anvil/cli/__init__.py, bin/src/anvil/git_ops/freshness.py, tests/test_cli.py
**Dependencies:** T005

`anvil merge-check <task> [--run-checks] [--json]`: resolve the task's
agent/<task>-<slug> branch, print the freshness report; with --run-checks, build a
throwaway detached worktree of the merged tree under the state dir's tmp area, run
the task's verification.commands, report per-command exit codes, and always clean
the worktree up (try/finally + git worktree remove --force). Exit 0 when fresh (or
offline-degraded), 1 when stale or a merged-tree command fails.

**Acceptance criteria:**

- `anvil merge-check <task> --json` emits `{behind_count, has_conflicts, base_ref, remote_checked, checks: [...]}`.
- With --run-checks in a fixture where base moved and a merged-tree test fails, exit code is 1 and the failing command is named; the throwaway worktree is removed even on failure.
- In a repo with no remote the command completes with exit 0 and reports the local base was used.
- The user's working tree and current branch are untouched by the command.

**Verification:**

- `uv run pytest ../tests/test_cli.py -q -k merge_check`
- `uv run pytest ../tests/test_git_ops.py -q`

### T007: Wire freshness gate into apply --approve (advisory/strict config)

**Feature:** F002
**Priority:** medium
**Likely files:** bin/src/anvil/cli/packet_apply.py, bin/src/anvil/config.py, tests/test_cli.py, docs/cli-reference.md
**Dependencies:** T005

Add config knob `merge_check: off|advisory|strict` (default advisory). On
`apply --approve`, run the cheap freshness check (never --run-checks) and print an
advisory block alongside the evidence gate; strict refuses approval with exit 1 and
JSON error code `base_stale`. Follow the exact strict_evidence precedence pattern
(flag > config > default). `--reject` is never affected; `off` skips entirely.

**Acceptance criteria:**

- Default (advisory): stale base prints a warning but approval proceeds; JSON data gains a `merge_check` block.
- `merge_check: strict` + stale base: `apply --approve` exits 1 with code `base_stale`; fresh base approves normally.
- Offline / no-remote projects approve without warning noise beyond a single "local base used" note.
- `anvil apply <task>` review-only mode shows the same merge_check block without gating.

**Verification:**

- `uv run pytest ../tests/test_cli.py -q -k "apply and merge"`
- `uv run pytest ../tests/test_config.py -q`

### T008: Pre-expiry lease warning in heartbeat hook + notify-digest

**Feature:** F003
**Priority:** high
**Likely files:** bin/src/anvil/cli/hooks.py, bin/src/anvil/cli/notify_digest.py, bin/src/anvil/config.py, tests/test_cli.py, tests/test_notify_digest.py

Add `lease_warning_minutes` config knob (default 10, 0 disables). In
hook_heartbeat, after the renew loop, emit a single stderr warning per claim when
remaining lease < threshold, debounced via a per-claim marker file in the state tmp
dir (no extra DB round-trip; warn based on post-renew lease_expires_at whatever the
B46 progress gate decided). Extend notify-digest's line and JSON with an
`expiring_soon` count. Hook contract unchanged: always exit 0, swallow every error.

**Acceptance criteria:**

- With a claim whose lease expires within the threshold, `anvil hook heartbeat` prints exactly one `[anvil:lease]` warning to stderr across two consecutive invocations, and exits 0.
- A claim renewed back above the threshold clears the debounce marker so a later crossing warns again.
- `anvil notify-digest --json` includes `expiring_soon`; the human line stays silent when all counts are zero.
- `lease_warning_minutes: 0` produces no warnings; an uninitialized project produces no output and exit 0.

**Verification:**

- `uv run pytest ../tests/test_cli.py -q -k heartbeat`
- `uv run pytest ../tests/test_notify_digest.py -q`
- `uv run pytest ../tests/test_config.py -q`

### T009: Advisory collision warnings on anvil next (reuse manager.check_conflicts)

**Feature:** F003
**Priority:** medium
**Likely files:** bin/src/anvil/cli/claim.py, bin/src/anvil/mcp_server.py, tests/test_json_output.py, tests/test_mcp.py

After `anvil next` selects a candidate, run the recommended task's likely_files
through the existing ClaimManager.check_conflicts logic against active claims and
attach an advisory `conflict_warnings` list to the JSON envelope and a one-line
human note. Mirror the field on MCP get_next_task. Selection order untouched.

**Acceptance criteria:**

- With another actor holding a claim overlapping the recommended task's likely_files, `anvil next --json` includes a non-empty `conflict_warnings` naming the claim, owner, and files; the recommended task is unchanged versus before.
- With no overlap, `conflict_warnings` is an empty list and human output has no extra line.
- MCP `get_next_task` returns the same warnings for the same state.
- `anvil claim` behavior (existing warning + --force) is unchanged.

**Verification:**

- `uv run pytest ../tests/test_json_output.py -q`
- `uv run pytest ../tests/test_mcp.py -q`
- `uv run pytest ../tests/test_claims.py -q`

### T010: Add phase/detail to progress.noted payload and MCP submit_progress

**Feature:** F004
**Priority:** high
**Likely files:** bin/src/anvil/state/payloads.py, bin/src/anvil/mcp_server.py, tests/test_mcp.py

Extend ProgressNotedPayload with optional `phase: str | None = None` and
`detail: str | None = None`. Thread an optional `phase` parameter through the MCP
submit_progress tool into the event payload. The event stays audit-only (no
dispatch-table change). Old JSONL rows without the new keys must still replay.

**Acceptance criteria:**

- ProgressNotedPayload still validates the old shape (task_id/actor/notes/noted_at only) — old events replay.
- ProgressNotedPayload accepts `phase` and `detail`; unknown keys are still rejected (extra="forbid").
- MCP submit_progress(task_id, actor, notes, phase="tests") appends a progress.noted event whose payload carries phase.
- Replay equivalence suite still passes.

**Verification:**

- `uv run pytest ../tests/test_mcp.py -q`
- `uv run pytest ../tests/test_replay_equivalence.py ../tests/test_schema_version.py -q`

### T011: New anvil progress CLI command

**Feature:** F004
**Priority:** high
**Likely files:** bin/src/anvil/cli/progress.py, bin/src/anvil/cli/__init__.py, tests/test_progress_cli.py
**Dependencies:** T010

Add `anvil progress TASK_ID PHASE [--detail TEXT] [--json] [--cwd PATH]` as a new
top-level command module mirroring the MCP tool body: resolve actor via
resolve_actor, state dir via _resolve_state_dir, validate the task exists, append
one progress.noted event with phase/detail. Standard JSON envelope; unknown task
exits 1 with code task_not_found.

**Acceptance criteria:**

- `anvil progress T001 build --detail "compiling" --json` prints an ok envelope and appends exactly one progress.noted event with phase == "build".
- Unknown task id exits 1; with --json the failure envelope carries code task_not_found.
- Uninitialized project exits 1 with the canonical not_initialized handling.
- `anvil progress --help` documents the phase argument.

**Verification:**

- `uv run pytest ../tests/test_progress_cli.py -q`
- `uv run pytest ../tests/test_json_output.py -q`

### T012: Surface phase, elapsed, and lease-expiry in status and notify-digest

**Feature:** F004
**Priority:** medium
**Likely files:** bin/src/anvil/state/backend.py, bin/src/anvil/state/sqlite.py, bin/src/anvil/cli/init_status.py, bin/src/anvil/cli/notify_digest.py, tests/test_notify_digest.py
**Dependencies:** T010

Add one backend method `latest_event_payload(target_id, action)` returning the most
recent matching event's payload+timestamp or None (list_events drops payloads so it
cannot be reused). In `anvil status`, render one line per active claim: task id,
actor, latest phase, elapsed since claimed_at, minutes until lease_expires_at; add
the same fields to --json under active_claims. In notify-digest, add a
claims-expiring-soon segment. MUST NOT touch the --hook-format single-line path
(consumed by the SessionStart hook).

**Acceptance criteria:**

- With an active claim and a recorded phase, `anvil status` shows the phase label, an elapsed duration, and a lease-expiry countdown for that claim.
- `anvil status --json` includes per-claim phase, elapsed_seconds, lease_expires_in_seconds.
- A claim with no progress events renders a placeholder phase (no crash on missing payload).
- `anvil status --hook-format` output is unchanged; notify-digest still prints nothing and exits 0 on a clean queue.

**Verification:**

- `uv run pytest ../tests/test_notify_digest.py -q`
- `uv run pytest ../tests/test_json_output.py ../tests/test_cli.py -q`

### T013: doctor --preflight with PRD-parse and unresolved-decision probes

**Feature:** F005
**Priority:** high
**Likely files:** bin/src/anvil/cli/doctor.py, tests/test_doctor_preflight.py

Add `--preflight` and `--prd` options to doctor. `--prd` uses the shared
PRD_OPTION/resolve_prd_id from cli/_helpers.py. When --preflight is set, _diagnose
appends: (1) PRD-parse probe — read prd_source_path, run parse_prd; ParseError or
missing file → ERROR naming the path; (2) decisions probe — find_unresolved_decisions:
needs_decision markers and tasks missing acceptance-criteria/verification → ERROR,
open_question items → WARNING. Human output gains a final PREFLIGHT: GO / NO-GO
line; --json keeps the doctor envelope plus data.preflight=true and data.go.
Exit contract unchanged: 0 when no ERROR finding, 1 otherwise.

**Acceptance criteria:**

- On a healthy project with a clean PRD, `anvil doctor --preflight` prints PREFLIGHT: GO and exits 0.
- With an unresolved needs-decision marker in the PRD, doctor --preflight reports an ERROR finding, prints PREFLIGHT: NO-GO, exits 1.
- With a syntactically broken PRD (missing ## Goals), the parse probe is an ERROR naming the PRD path, exit 1.
- `anvil doctor --preflight --json` emits valid JSON with data.preflight == true and data.go matching the exit code.
- Plain `anvil doctor` (no flag) output and exit behavior are byte-compatible with today.

**Verification:**

- `uv run pytest ../tests/test_doctor_preflight.py -q`
- `uv run pytest ../tests/test_doctor_verification_paths.py -q`

### T014: Preflight git tree-state probe

**Feature:** F005
**Priority:** medium
**Likely files:** bin/src/anvil/cli/doctor.py, bin/src/anvil/git_ops/worktree.py, tests/test_doctor_preflight.py
**Dependencies:** T013

Add a tree-state probe to the --preflight set: reuse _is_dirty from
git_ops/worktree.py (export or thin public wrapper) against the project root.
Dirty tree → WARNING; not a git repo → INFO; git missing/timeout → INFO, never a
crash. Read-only and fast (single git status --porcelain).

**Acceptance criteria:**

- In a repo with uncommitted changes, doctor --preflight includes a WARNING tree-state finding but still exits 0 when no ERROR exists.
- In a clean repo the tree-state finding is OK.
- In a non-git directory the probe yields INFO, not ERROR, and doctor completes.
- The probe never runs without --preflight.

**Verification:**

- `uv run pytest ../tests/test_doctor_preflight.py -q`
- `uv run pytest ../tests/test_git_ops.py -q`
