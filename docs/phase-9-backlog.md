> ## Archived — see [roadmap.md](roadmap.md) for live planning
>
> This file is preserved as a historical audit trail of what was deferred at the
> close of Phase 9. The live forward-planning source of truth has moved to
> [`docs/roadmap.md`](roadmap.md) (version × theme organized; evolves continuously).
>
> Every forward-looking item below (P9B-1 through P9B-9) has been re-homed in the
> roadmap with its original ID preserved. Use this file only to understand the
> Phase 9 decision context. For "what's planned next", read `roadmap.md`.

# anvil — Post-v1.9.0 Backlog (v2.x roadmap + carry-forward tech debt)

This file is the forward-looking companion to
[`tech-debt-backlog.md`](tech-debt-backlog.md). Where `tech-debt-backlog.md`
catalogues items by *origin PR* and tracks their close-out across phases,
this file catalogues items by *future release intent* — what v2.0 / v2.1 /
v2.x will plausibly carry.

Phase 9 (v1.9.0) closed the audit-honesty deferrals from Phase 8 and the
Phase 7 LLM-augmentation cleanup. Eight items below were considered for
inclusion in Phase 9 but consciously deferred — either the implementation
risk was higher than the plan window allowed, the design needed a separate
spec doc, or the work depended on infrastructure that does not exist yet.

**Status legend**: `OPEN` = unscheduled; `TARGETED-VN.M` = scheduled for that release; `SPEC-FIRST` = needs a design doc before implementation.

---

## v2.0 — Additional sync providers

The `SyncProvider` Protocol shipped in v1.8.0 was deliberately
registry-driven so contributors can add providers without engine changes.
v1.8.0 / v1.9.0 ship `github_issues` only. The next batch:

### P9B-1 · LinearIssuesProvider · `linear_issues`

**Status**: OPEN. **Target**: v2.0.

GraphQL-only API; httpx client with respx mocking. Status mapping needs a per-team workflow inspection step (Linear lets teams customise their workflow states). Step-by-step contributor guide in `docs/sync-providers.md` § "Step-by-step: add Linear support" — uncommented placeholder for the work.

**Acceptance**:

- `bin/src/anvil/sync/providers/linear.py` with full Protocol surface.
- `bin/src/anvil/sync/clients/linear_api.py` — GraphQL transport.
- `tests/test_linear_provider.py` with respx mocks; full lifecycle test.
- Live-Linear nightly workflow under `.github/workflows/anvil-live-linear.yml` gated on `LINEAR_API_KEY` secret.
- `anvil sync linear_issues --health` works.

### P9B-2 · MondayBoardsProvider · `monday_boards`

**Status**: OPEN. **Target**: v2.0.

Monday has people-columns and per-board custom columns; the `provider_metadata` dict will carry the bulk of the shape. Auth via Monday API key.

**Acceptance**: same shape as P9B-1 but using REST+JSON (Monday's GraphQL is opt-in per workspace).

### P9B-3 · JiraIssuesProvider · `jira_issues`

**Status**: OPEN. **Target**: v2.1.

Jira's workflow/status taxonomy is per-project; the provider needs a one-time discovery call to map anvil's 11 `TaskStatus` values to the project's actual statuses. Auth via PAT + email pair.

**Acceptance**: same shape as P9B-1; one-time `--discover-statuses` flag that writes the discovered mapping into `.anvil/config.yaml` under `sync.providers.jira_issues.status_map`.

### P9B-4 · GitHubProjectsProvider · `github_projects`

**Status**: OPEN. **Target**: v2.1.

Sibling to `github_issues` but for Projects v2 (the newer board surface). Shares the gh-CLI / httpx transport from `github_issues` but addresses a different remote object kind. Probably co-locates in `sync/providers/github_projects.py`.

---

## v2.0 — Sync infrastructure upgrades

### P9B-5 · Webhook-based sync (vs polling)

**Status**: SPEC-FIRST. **Target**: v2.0.

`--watch` polls every N seconds. For providers that publish webhooks (GitHub, Linear, Monday, Jira), the engine should accept push-based sync via a long-running listener. Webhook secret in `.anvil/config.yaml`; HMAC verification on every payload.

Needs a design doc first: the engine's current "one fetch round-trip per task per pass" assumption does not hold under webhooks (events arrive out of order, may duplicate, may race with manual sync calls). The reconciliation engine has to become idempotent over arbitrary event ordering rather than just over sequential polling iterations.

**Spec scope**:

- Webhook listener as a separate `anvil webhook-listen --provider X --port 8080` subcommand (decouples lifetime from CLI sync calls).
- Event de-duplication via `(provider_id, external_id, last_modified)` tuple — first event wins, later same-tuple events are ignored.
- Out-of-order delivery — the listener queues events and processes them in `last_modified` order with a configurable max-delay.
- HMAC verification per provider (GitHub uses `X-Hub-Signature-256`; Linear uses `Linear-Signature`; etc.).
- Failure mode when the listener crashes: the polling fallback must continue to work and reconcile catch-up state from the JSONL audit log.

### P9B-6 · Immediate-apply `*_applied` resolution variants

**Status**: TARGETED-V2.0. **Target**: v2.0.

Phase 9 T5 (`agent-welder-honesty-status.md` § 2 — "Immediate-apply variants for local_wins / remote_wins: deferred") explicitly deferred wiring `remote_wins_applied` / `local_wins_applied` per the TODOs at `cli/sync.py:1054` and `:1068`. The conflict-safety design (re-fetch on a moving target, retry/back-off contract) needs to be specified before the implementation can land.

**Acceptance**:

- `remote_wins_applied`: when the conflict-resolution chose remote, immediately call `_apply_remote_to_local` inline inside the pull loop. Emit `sync.pull.completed` with `resolution="remote_wins_applied"`.
- `local_wins_applied`: when the conflict-resolution chose local, immediately call `provider.push_task(...)` inside the pull loop. Emit `sync.pull.completed` with `resolution="local_wins_applied"`. Define the retry/back-off contract for the race where a parallel remote edit lands between the `local_wins` decision and the `push_task` call.
- Updated `docs/github-sync.md` § "Audit honesty" — the `*_applied` tokens join the controlled vocabulary alongside the `*_deferred` ones.
- `tests/test_cli_sync.py` — 4+ new tests covering both immediate-apply paths plus the race-edit fallback.

### MPRD-RG1 · PRD→release-group sync (`ensure_release_group` Protocol + GitHub milestone client)

**Status**: OPEN. **Target**: v2.0.

Deferred out of the multi-PRD release (Phase 7 of `docs/specs/2026-06-22-multi-prd-revisable.md`; tracked as **T029** in `docs/backlog/multi-prd-revisable.prd.md`). That release shipped the release/sync **data plumbing only** — `SyncMapping.prd_id` + `entity_kind: Literal['task','prd']`, per-PRD `--prd` push scoping, and `prd_id` on reconciliation discrepancies. The network-touching milestone wiring was explicitly **not** built so no existing provider had to change.

**Acceptance** (none of this lands in the multi-PRD release; existing providers unchanged):

- A **provider-neutral** `ensure_release_group(release_tag, target_version, prd_summary, mapping) -> ExternalRef` Protocol method (covers GitHub Milestone / Linear Cycle / Jira FixVersion), **optional and capability-gated** via a `supports_milestones` / `supports_release_groups` flag so existing providers stay unchanged.
- GitHub client `create_milestone` / `list_milestones` calls plus issue→milestone assignment for the issues a PRD owns.
- A `sync.milestone.ensured` (release-group) audit event and **prd-kind** `SyncMapping` row persistence (`entity_kind='prd'`, carrying `prd_id`, null `task_id`) recording the 1:1 PRD↔release/milestone mapping.
- A `missing_milestone_mapping` reconciliation discrepancy kind for a PRD that has a `target_tag` but no persisted milestone mapping (sibling to `missing_sync_mapping`; `drift_sync_state` already special-cases prd-kind rows so a milestone produces no task-shaped drift).
- The milestone-ensure step is wired into `anvil sync … --prd <id> --push` only for providers that advertise the capability.

Re-homed in the live roadmap as `[MPRD-RG1]` (`docs/roadmap.md` § v2.0 → Theme: Sync infrastructure).

---

## v2.x — Carry-forward from `tech-debt-backlog.md`

The items below remain open in `tech-debt-backlog.md` after Phase 9
closed P9-1..P9-8. They are not scheduled for any specific Phase 9
follow-up but track them here so the next planning pass picks them up.

### Cleanup (CL-N items still open)

| ID | Source | Description |
|---|---|---|
| CL-1 | PR #41 Critic-2 | `check-claim.sh` does not call the Phase 4 CLI subcommand; per-file warning logic in CLI is dead from the hook's perspective. |
| CL-2 | PR #41 Critic-1 | `--commands` / `--files-changed` comma-split mangles commands with embedded commas. Switch to repeatable flag. |
| CL-3 | PR #41 Critic-3 | `_reap_stale_claims` bare `except Exception: pass` swallows `SchemaMismatch`. |
| CL-4 | PR #41 Critic-3 | `ConflictGroup` records never persisted; `conflict_groups` table always empty. |
| CL-5 | PR #41 Critic-3 | `conflicts` CLI command referenced in docstring but not implemented. Depends on CL-4. |
| CL-8 | PR #41 Critic-1 | Double-submit with different `evidence_id` inserts duplicate row. |
| CL-10 | PR #41 Critic-1 | `capture-evidence.sh` + `gates.py` pattern sets are not aligned; agent running `go test` gets no capture but the gate passes. |
| CL-11 | PR #41 Critic-3 | `template.py:374` calls `datetime.now()` directly — bypasses Clock abstraction. |
| CL-12 | PR #41 Critic-3 | `score_all() / infer_dependencies() / infer_conflict_groups()` are in `__all__` but have no external callers — misleading public surface. |
| CL-13 | PR #41 Critic-2 | `next_event_id` returns hardcoded `"E000001"` when conn is None instead of raising. |

### Test Quality (TQ-N items still open)

| ID | Source | Description |
|---|---|---|
| TQ-1 | PR #41 Critic-4 | `_sqlite_dump` docstring claims user_version filtering but doesn't filter. Either implement or delete the claim. |
| TQ-2 | PR #41 Critic-4 | `test_replay_includes_claim_stale` skips `prd.reviewed` between parsed and approved. |
| TQ-3 | PR #41 Critic-4 | Two `unittest.mock.patch` usages on `SqliteBackend` violate the no-mocking rule. Replace with real failure injection or delete unreachable branches. |
| TQ-4 | PR #41 Critic-4 | `test_init_creates_state_directory` first `runner.invoke` runs without `chdir(tmp_path)` — pollutes real cwd. |
| TQ-6 | PR #41 Critic-4 | `_do_init_and_plan` doesn't assert exit codes — sub-commands can silently fail. |
| TQ-7 | PR #41 Critic-4 | Phase 3 CLI tests assert on output strings, not SQLite state — "feature not created" would pass `"feature" in result.output.lower()`. |
| TQ-8 | PR #41 Critic-4 | `tests/test_sqlite.py` is 3,924 lines — split per phase. |

### Performance / Scale (PS-N items still open)

| ID | Source | Description |
|---|---|---|
| PS-1 | PR #41 Critic-2 | `_check_group_conflicts` has N+1 query — for each active claim, calls `backend.get_task()` inside a loop. Pre-fetch via `list_tasks()` + local map. |

---

## v2.x — New work surfaces

### P9B-7 · `anvil snapshot` subcommand

**Status**: OPEN. **Target**: v2.1.

Phase 5 (v1.4.0) removed the pre-created `.anvil/snapshots/` directory because nothing wrote to it. The intent was always to ship a `sqlite3 .backup` wrapper as `anvil snapshot` that writes a timestamped `.db` into that directory and prunes by retention policy. The directory will be created on first invocation.

**Acceptance**:

- `anvil snapshot [--retention 30d|count:N]` writes `.anvil/snapshots/YYYY-MM-DDTHH-MM-SSZ.db`.
- `anvil snapshot --list` shows existing snapshots with size + age.
- `anvil snapshot --restore <name>` restores a snapshot atomically (writes to a temp file, swaps via rename).
- Documented in `docs/specs/2026-05-24-anvil-v0.md` § Snapshots.

### P9B-8 · MCP sync tools surface

**Status**: OPEN. **Target**: v2.1.

The MCP server (Phase 6) exposes 13 read/mutate tools but does NOT expose `sync_*` tools. Agents that want sync today have to shell out via Bash. A `sync_run` / `sync_health` / `sync_status` tool surface would close that gap — useful for agents running in sandboxes without shell access.

**Acceptance**:

- 4 new MCP tools: `sync_run(provider, *, direction='both', task_id=None)`, `sync_health(provider)`, `sync_status()`, `sync_reconcile(*, fix=False)`.
- Tool errors map cleanly to `ToolError(message)` with the same exception classes the CLI handles.
- Documented in `docs/mcp.md` § Sync tools.
- Tests in `tests/test_mcp.py`.

### P9B-9 · Provider config schemas in `config.yaml`

**Status**: SPEC-FIRST. **Target**: v2.0 (alongside P9B-1 Linear).

The current `sync.providers` config key is a flat list. As soon as providers need per-provider config (Linear team ID, Monday board ID, Jira project key + workflow map), the flat list becomes a nested map. The transition needs a design doc — does the new map shape coexist with the flat list, or replace it?

Probable shape:

```yaml
sync:
  providers:
    github_issues: {}             # no per-provider config
    linear_issues:
      team_id: ENG
    jira_issues:
      project_key: ENG
      status_map:                 # filled by --discover-statuses
        ready: "Open"
        in_progress: "In Progress"
        done: "Done"
```

Migration path from the flat list form (a list of strings is shorthand for "the listed providers with empty config") keeps v1.9.0 configs valid.

---

## Closed in Phase 9 (cross-reference)

See [`tech-debt-backlog.md`](tech-debt-backlog.md) § "Phase 8 / Phase 9
closures (sync + LLM cleanups)" for the full list of P9-1..P9-8 entries
with implementation details and test counts.

| ID | Title | Closed in |
|---|---|---|
| P9-1 | Audit-event honesty — `sync.pull.completed` emitted on deferred branches | Phase 9 T5 |
| P9-2 | `local_moved`-only path set `sync_state="in_sync"` instead of `local_ahead` | Phase 9 T5 |
| P9-3 | `SyncAuditPayload` single all-optional model accepted nonsense | Phase 9 T3 |
| P9-4 | `RecordedLLMProvider.record_key` ignored `max_tokens` / `temperature` | Phase 9 T6 |
| P9-5 | Brainstorm-flow bridge used fuzzy detection | Phase 9 T6 |
| P9-6 | `expand --use-llm` had no `--format prd` UX | Phase 9 T6 |
| P9-7 | Multi-provider config — no way to opt out of every sync provider | Phase 9 T5 |
| P9-8 | Two new plugin-owned doc agents — `marketplace-scribe`, `docs-scribe` | Phase 9 T4 |
