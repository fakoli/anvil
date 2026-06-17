# fakoli-state GitHub Issues sync

## What it is

Bidirectional, polling-based sync between fakoli-state tasks and GitHub issues.
Every local `Task` round-trips to an issue in a configured repo, status labels
encode the fakoli-state lifecycle, and divergence is detected per-task via
recorded `last_synced_at` vs the remote `updated_at`. v0 ships the
`GitHubIssuesProvider` only; the same `sync` surface accepts Linear, Monday,
Jira, and any other contributor-registered backend via the `SyncProvider`
Protocol (see [`sync-providers.md`](sync-providers.md)).

---

## Quick start

```bash
# Authenticate. gh CLI is preferred; the provider re-uses your gh session.
gh auth login

# Probe reachability + auth before doing any state mutation.
fakoli-state sync github --health

# One push+pull pass against every local task.
fakoli-state sync github
```

For environments without `gh` installed (CI runners, sandboxes), set a PAT
instead:

```bash
export GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxxx
export GITHUB_REPOSITORY=owner/repo
fakoli-state sync github
```

---

## Configuration

The provider needs a target repository AND a way to authenticate. Both have
two paths; pick one of each.

### Repository selection

| Source                    | Example                              | Precedence |
|---------------------------|--------------------------------------|------------|
| Constructor kwarg `repo=` | `GitHubIssuesProvider(repo="o/r")`   | 1 (highest)|
| `GITHUB_REPOSITORY` env   | `export GITHUB_REPOSITORY=o/r`       | 2          |

The CLI instantiates with no kwargs, so in CLI usage `GITHUB_REPOSITORY` is
the only option. Format is always `<owner>/<repo>`; the constructor raises
`ValueError` on missing or malformed values.

### Authentication

| Source         | Notes                                                            |
|----------------|------------------------------------------------------------------|
| `gh auth login`| Preferred. Re-uses the user's `gh` session, no PAT plumbing.     |
| `GITHUB_TOKEN` | Read at request time by the HTTP transport. PAT with `repo` scope.|

### Transport selection

```bash
fakoli-state sync github                  # transport=auto (default)
fakoli-state sync provider github_issues  # same, generic syntax
```

The provider's `transport` kwarg accepts `auto`, `gh_cli`, or `http`. `auto`
probes `gh --version` and `gh auth status` once at init: success → `gh_cli`,
either failure → `http`. The selection is cached for the instance lifetime;
construct a new provider to re-probe.

The CLI does not currently expose `--transport` directly; the provider
defaults to `auto` and that path covers both authenticated `gh` users and
CI runners with `GITHUB_TOKEN`.

### Configured providers (reconciliation)

`fakoli-state sync` (bare, no subcommand) runs the reconciliation engine,
which uses the *list of configured providers* to decide which tasks count
as "done but unmapped" and need a sync.

**v1.9.0 (Phase 9 T5)** adds the optional `sync.providers` top-level config
key for explicit selection — see
[`sync-providers.md` → Per-provider configuration](sync-providers.md#per-provider-configuration-v190)
for the full schema and three-way semantics (absent / explicit list /
empty list = opt-out). When the key is absent the engine defaults to
`sorted(PROVIDER_REGISTRY)` — every registered provider participates,
preserving v1.8.0 behaviour for projects that have not yet bothered to
declare a list.

```yaml
# .fakoli-state/config.yaml — opt-in subset
sync:
  providers:
    - github_issues
    - linear_issues   # contributor-registered providers also accepted
```

When multiple providers are configured, the reconciliation engine emits
one `missing_sync_mapping` discrepancy *per missing provider per done
task* (e.g. a task mapped to `github_issues` but not `linear_issues`
produces a single discrepancy with `payload.missing_provider ==
"linear_issues"`).

---

## CLI reference

Every subcommand under `fakoli-state sync` and its flags.

| Command                                                  | Description                                                              |
|----------------------------------------------------------|--------------------------------------------------------------------------|
| `fakoli-state sync`                                      | Run reconciliation only (no provider call). Prints discrepancy report.   |
| `fakoli-state sync --fix --yes`                          | Apply every suggested fix from reconciliation. `--yes` required in CI.   |
| `fakoli-state sync github`                               | Alias for `sync provider github_issues`. Push + pull every local task.   |
| `fakoli-state sync github --push`                        | Push only (skip pull). Useful right after `apply --approve`.             |
| `fakoli-state sync github --pull`                        | Pull only (skip push). Useful for reconciling remote-side edits.         |
| `fakoli-state sync github --task T001`                   | Scope a sync pass to a single task.                                      |
| `fakoli-state sync github --health`                      | Probe reachability + auth. Exits without touching state.                 |
| `fakoli-state sync github --fix`                         | Force `remote_wins` on every conflict for this iteration.                |
| `fakoli-state sync github --watch`                       | Long-running poll loop. Ctrl-C exits.                                    |
| `fakoli-state sync github --watch --interval 30`         | Override poll cadence (seconds). `--interval 0` runs one iteration.      |
| `fakoli-state sync provider <id>`                        | Generic provider invocation. `<id>` resolves via `PROVIDER_REGISTRY`.    |
| `fakoli-state sync provider <id> --push --task T001`     | Single-task push against any registered provider.                        |
| `fakoli-state sync github --yes`                         | Auto-confirm conflict prompts (defaults to `local_wins`).                |

Exit codes:

- `0` — success
- `1` — generic error (auth missing, provider not registered, etc.)
- `2` — operator input required (at least one task is parked in `manual_merge`)

---

## Status label mapping

Every `TaskStatus` maps to exactly one `status:*` label, plus a GitHub
open/closed state. The mapping is in `STATUS_TO_LABEL` /
`LABEL_TO_STATUS` / `DONE_STATUSES` in `sync/providers/github_issues.py`.

| `TaskStatus`      | GitHub label           | Issue state |
|-------------------|------------------------|-------------|
| `proposed`        | `status:proposed`      | open        |
| `drafted`         | `status:drafted`       | open        |
| `reviewed`        | `status:reviewed`      | open        |
| `ready`           | `status:ready`         | open        |
| `claimed`         | `status:claimed`       | open        |
| `in_progress`     | `status:in-progress`   | open        |
| `blocked`         | `status:blocked`       | open        |
| `needs_review`    | `status:needs-review`  | open        |
| `accepted`        | `status:accepted`      | open        |
| `done`            | `status:done`          | **closed**  |
| `rejected`        | `status:rejected`      | open        |

Only `done` closes the issue. `rejected` stays open so a human looking at
the repo can see it was actively rejected, not silently archived.

On update, the provider removes every other `status:*` label it manages
before adding the new one. Non-`status:*` labels (user-added `bug`,
`area/*`, `priority:*`, etc.) are preserved across pushes.

---

## Body footer convention

Every pushed issue gets a footer appended to its body:

```
<original task description>

---
_synced from fakoli-state task T001_
```

The footer is emitted by `_compose_body(task_description, task_id)` and
stripped by `_strip_footer(body)` on fetch so a round-trip
(push → fetch → `ExternalTask.body`) yields the same text the agent
originally wrote. The regex requires the footer to be at the end of the
body; intermediate `---` separators in the task description are not
affected.

---

## Conflict resolution strategies

Each `SyncMapping` carries a `conflict_resolution_strategy` enum. When
`fetch_task` returns a remote payload whose `last_modified` is newer than
the local `last_synced_at` AND the local task's `updated_at` is also newer,
the strategy decides what happens. The emitted `sync.conflict_detected`
event records the choice in `resolution`.

| Strategy        | Behaviour                                                                 | `resolution` string             |
|-----------------|---------------------------------------------------------------------------|---------------------------------|
| `local_wins`    | Record decision; local re-push deferred to the next push pass.            | `local_wins_deferred`           |
| `remote_wins`   | Record decision; local mutation from remote deferred to the next pull.    | `remote_wins_deferred`          |
| `prompt`        | Interactive prompt: `[local/remote/skip]`. Defaults to local on `--yes` or non-tty. | `prompt_chose_local`, `prompt_chose_remote`, `prompt_skipped`, `prompt_defaulted_to_local` |
| `manual_merge`  | Write `.fakoli-state/.sync-conflicts/<task_id>.md`; refuse to sync this task. | `manual_merge_file_written`     |

**`_deferred` is the v1.8.0 + v1.9.0 contract.** Recording a `local_wins` /
`remote_wins` decision does NOT immediately mutate the other side in this
iteration — the mutation rides the next push (for `local_wins`) or next
pull (for `remote_wins`) pass. A future release (tracked in
[`phase-9-backlog.md`](phase-9-backlog.md)) may wire `*_applied` variants
that mutate immediately; until then the deferred contract is the truthful
one.

For `manual_merge`: the markdown file at
`.fakoli-state/.sync-conflicts/<task_id>.md` shows local and remote
side-by-side. Resolve the file (edit local or accept remote), delete it,
then rerun `fakoli-state sync github` to continue. The batch exits with
code `2` if any task is parked pending manual merge.

---

## Audit honesty

The audit-event stream is the canonical record of what the sync engine
actually did vs what was deferred. v1.9.0 (Phase 9 T5) repaired six
dishonest emissions in v1.8.0 where conflict branches emitted
`sync.pull.completed` despite no local mutation having occurred — that
fix makes the JSONL safe to grep for "did this task actually update?".

### `sync.pull.completed` vs `sync.pull.deferred` semantics

| Event                  | Meaning                                                                           |
|------------------------|-----------------------------------------------------------------------------------|
| `sync.pull.completed`  | The pull was honest: fetch succeeded and the mapping was bumped to a truthful state. Includes (1) clean pull mutated the local Task, (2) tombstone (mapping flipped to `external_deleted`, `audit_note="external_deleted"`), (3) no divergence (mapping bumped to `in_sync`), or (4) **local-moved-only** — fetch succeeded, no remote movement observed, mapping bumped to `local_ahead` and a paired `sync.push.deferred` event fires with `resolution="local_moved_no_push"`. |
| `sync.pull.deferred`   | The pull recorded an intent without mutating local state. Fires on (a) `manual_merge` (the merge file was written, operator must act — `audit_note="manual_merge_pending"`) and (b) the six deferred conflict-resolution branches (`local_wins_deferred`, `remote_wins_deferred`, `prompt_defaulted_to_local`, `prompt_chose_local`, `prompt_chose_remote`, `prompt_skipped`). |

The `local_ahead` mapping state captures a bug-collapse from v1.8.0: when
the local Task had moved ahead of `last_synced_at` and the remote had not
changed, the engine used to set `sync_state="in_sync"` (wrong — the local
was ahead). v1.9.0 sets `sync_state="local_ahead"` and emits a
`sync.push.deferred` audit event with
`resolution="local_moved_no_push"` so operators can grep `events.jsonl`
to find tasks awaiting a follow-up `--push`.

### Resolution token vocabulary (v1.9.0)

Audit-stream-visible `resolution` strings produced by Phase 9:

| Resolution token              | Emitted on                  | Branch                           |
|-------------------------------|-----------------------------|----------------------------------|
| `local_wins_deferred`         | `sync.pull.deferred`        | `local_wins` strategy            |
| `remote_wins_deferred`        | `sync.pull.deferred`        | `remote_wins` strategy           |
| `prompt_defaulted_to_local`   | `sync.pull.deferred`        | `prompt` strategy on non-tty / `--yes` |
| `prompt_chose_local`          | `sync.pull.deferred`        | `prompt` strategy, user chose local  |
| `prompt_chose_remote`         | `sync.pull.deferred`        | `prompt` strategy, user chose remote |
| `prompt_skipped`              | `sync.pull.deferred`        | `prompt` strategy, user skipped  |
| `manual_merge_file_written`   | `sync.conflict_detected`    | `manual_merge` strategy          |
| `local_moved_no_push` (NEW)   | `sync.push.deferred`        | local-moved-only pull path       |

The six `local_wins` / `remote_wins` / `prompt_*` tokens also appear on
the paired `sync.conflict_detected` event (one per conflict) so a
forensic query of "show me every deferral and its conflict context" is a
single `jq` over the JSONL.

### Querying the audit log

```bash
# Every deferred pull this week
jq 'select(.action == "sync.pull.deferred")' .fakoli-state/events.jsonl

# Every task with a local_moved_no_push hint awaiting --push
jq 'select(.payload_json.resolution == "local_moved_no_push") | .target_id' \
   .fakoli-state/events.jsonl | sort -u

# Conflict resolution histogram
jq -r 'select(.action == "sync.conflict_detected") | .payload_json.resolution' \
   .fakoli-state/events.jsonl | sort | uniq -c
```

---

## Reconciliation

Bare `fakoli-state sync` runs the `ReconciliationEngine` only — no
provider call, no network. It cross-checks SQLite state vs filesystem
(packets/) vs git (branches/worktrees) and prints a discrepancy report.

Discrepancy kinds:

| Kind                    | Severity | Auto-fix? |
|-------------------------|----------|-----------|
| `orphan_branch`         | warning  | yes       |
| `orphan_packet`         | info     | yes       |
| `orphan_worktree`       | warning  | yes       |
| `stale_claim`           | error    | yes       |
| `missing_sync_mapping`  | warning  | no (prints CLI hint) |
| `drift_sync_state`      | warning  | no (prints CLI hint) |

`fakoli-state sync --fix --yes` applies the auto-fixable kinds via the
backend or a bounded git subprocess. The two stub-fix kinds
(`missing_sync_mapping`, `drift_sync_state`) print the operator-facing
`fakoli-state sync provider <id> --pull --task <id>` command in
`suggested_fix` but require manual execution — pushing or pulling
requires the provider credentials and conflict-resolution flow, which
the reconciliation engine does not own.

---

## Audit events

Every sync mutation emits an event into `events.jsonl` AND the `events`
table in `state.db` (replay-from-empty reconstructs the SyncMapping rows
from these events).

| Action                       | Emitted by                          |
|------------------------------|-------------------------------------|
| `sync.batch.started`         | start of a `_run_sync_once` pass    |
| `sync.batch.completed`       | end of a `_run_sync_once` pass      |
| `sync.push.started`          | per task, before `provider.push_task` |
| `sync.push.completed`        | per task, on success                |
| `sync.push.failed`           | per task, on `SyncProviderError`    |
| `sync.push.deferred`         | per task, on the local-moved-only pull path (v1.9.0; `resolution="local_moved_no_push"`) — hints that a follow-up `--push` is needed |
| `sync.pull.started`          | per task, before `provider.fetch_task`|
| `sync.pull.completed`        | per task, when the pull was honest (see [Audit honesty](#audit-honesty)) |
| `sync.pull.failed`           | per task, on `SyncProviderError`    |
| `sync.pull.deferred`         | per task, when `manual_merge` or any of the six deferred conflict-resolution branches recorded an intent without mutating local state (v1.9.0) |
| `sync.conflict_detected`     | per conflict, every strategy        |
| `sync_mapping.upserted`      | per successful push (after persist) |
| `sync_mapping.deleted`       | per explicit mapping removal        |

Filter the JSONL for forensic queries:

```bash
jq 'select(.action | startswith("sync."))' .fakoli-state/events.jsonl
```

Audit emission failures are non-fatal — a sync that succeeded but whose
audit row failed to write logs to stderr rather than aborting the sync.

---

## Failure modes

| Trigger                                  | Surface                                                                              |
|------------------------------------------|--------------------------------------------------------------------------------------|
| `GITHUB_TOKEN` missing, no `gh auth`     | `--health` reports `auth_configured=False` with a hint; sync ops exit `1`.           |
| `gh` uninstalled mid-`--watch`           | Transport flips to `http` on the next iteration's new provider instance (currently per-watch single instance — re-probe happens on restart). Each iteration prints the error and continues. |
| Rate-limited                             | `RateLimitExceeded` → wrapped as `SyncProviderError` → batch loop continues; that single task gets `sync.push.failed` / `sync.pull.failed`. |
| Issue deleted on remote                  | `fetch_task` returns `None`; sync logs `external_deleted` on stderr; SyncMapping's `sync_state` flips to `external_deleted` so `fakoli-state sync` (reconciliation) surfaces a `drift_sync_state` discrepancy with `payload.reason='external_deleted'`. |
| Provider raises arbitrary exception      | Caught by the best-effort wrapping loop in `_push_one_task` / `_pull_one_task`; surfaced on stderr with `exception_type` recorded in the audit event; loop continues with the next task. |
| `--watch` iteration raises               | Outer `except Exception` in `_run_watch_loop` surfaces the error and keeps polling; the daemon never dies on a single bad pass. |

---

## Migration

v1.8.0 bumps `SCHEMA_VERSION` from `2` to `3` (additive: new
`external_url` column, new `provider_metadata_json` column, new
`UNIQUE(external_system, external_id)`, FK flipped to `ON DELETE
CASCADE`). The upgrade is automatic on first open; no operator action
required. See [`migrations.md`](migrations.md) for the full diff and
rollback notes.

---

## See also

- [`sync-providers.md`](sync-providers.md) — `SyncProvider` Protocol +
  how to add Linear, Monday, Jira.
- [`mcp.md`](mcp.md) — MCP server (does not currently expose sync tools;
  agents call the CLI directly).
- [`migrations.md`](migrations.md) — schema version history.
- `specs/2026-05-24-fakoli-state-v0.md` — canonical design spec
  including the Phase 8 sync contract.
