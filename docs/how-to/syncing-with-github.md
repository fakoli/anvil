# Syncing with GitHub Issues

The canonical state lives in `.anvil/` (SQLite + JSONL). GitHub Issues
is an opt-in projection — a familiar surface external stakeholders can read
and comment on without coupling truth to GitHub. Setting up sync gives you
bidirectional flow between local tasks and Issues while keeping the source
of truth local.

For the underlying mechanics (status-label mapping, body-footer convention,
audit-event vocabulary, schema migrations), see [`../github-sync.md`](../github-sync.md).
For the provider Protocol (writing your own Linear / Monday / Jira backend),
see [`../sync-providers.md`](../sync-providers.md).

New to anvil's vocabulary (task, requirement, workspace)? See the [glossary](../glossary.md).

---

## Prerequisites

- A GitHub repo (the target for Issues sync). Format is `<owner>/<repo>`.
- `gh` CLI installed AND authenticated. Confirm with:

  ```bash
  gh auth status
  ```

  If not authenticated, run `gh auth login` and follow the prompts. The
  provider re-uses your `gh` session — no PAT plumbing required.

- **Fallback (no gh CLI)**: set `GITHUB_TOKEN` to a personal access token
  with the `repo` scope. The HTTP transport reads it at request time:

  ```bash
  export GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxxx
  ```

- The target repo must be set via `GITHUB_REPOSITORY`:

  ```bash
  export GITHUB_REPOSITORY=owner/repo
  ```

  The CLI instantiates `GitHubIssuesProvider()` with no kwargs, so
  `GITHUB_REPOSITORY` is the only way to scope a CLI sync. Missing or
  malformed values raise `ValueError` at provider init.

---

## Probe before mutating state

Run a health check first — it's a network/auth probe that touches no local
state:

```bash
anvil sync github --health
```

Sample output:

```
Provider: GitHub Issues (github_issues)
  available:        True
  auth_configured:  True
  last_check_at:    2026-05-25T18:42:11+00:00
```

If `auth_configured: False`, fix `gh auth login` / `GITHUB_TOKEN` before
proceeding. The health check never raises — it encodes failures as fields
on `ProviderHealth` so a CI runner can grep the output.

---

## Configure the provider (optional)

For most setups, env vars are enough. If you want to narrow
reconciliation's "which providers count?" scan to a deliberate subset,
edit `.anvil/config.yaml`:

```yaml
# anvil configuration
project_name: 'my-project'
project_id: '...'

# GitHub sync conflict strategy (top-level key).
sync_github_enabled: true
sync_github_conflict_strategy: prompt   # local_wins | remote_wins | prompt | manual_merge

# Optional: pin the providers the reconciliation engine scans.
# Absent  → falls back to every registered provider (default).
# Empty [] → opts out of every provider; sync becomes a no-op.
sync:
  providers:
    - github_issues
    # - linear_issues   # contributor-registered providers also accepted
```

The `sync.providers` schema is forward-compatible: today only `github_issues`
ships in-tree, but contributor providers (Linear, Monday, Jira) register
into the same `PROVIDER_REGISTRY` and surface here verbatim. See
[`../sync-providers.md`](../sync-providers.md) for the registration mechanics.

---

## First sync — push existing tasks

```bash
anvil sync github --push
```

What happens per task:

- If a `SyncMapping` row already exists (`task_id` ↔ `external_id`), the
  provider updates the GitHub Issue in place.
- Otherwise, the provider creates a new Issue. The body includes a footer
  marking the canonical anvil task ID so round-trip parsing is
  reliable:

  ```
  <original task description>

  ---
  _synced from anvil task T001_
  ```

- A `SyncMapping` row records `task_id ↔ issue_number`, `external_url`, and
  the `last_synced_at` timestamp.
- An audit event lands in `.anvil/events.jsonl` (`sync.push.started`
  → `sync.push.completed` per task).

Sample output:

```
Sync against GitHub Issues (github_issues): push={'pushed': 14, 'failed': 0, 'skipped': 0} pull={'pulled': 0, 'failed': 0, 'skipped': 0, 'manual_merge_pending': 0}
```

---

## Pull changes from GitHub

```bash
anvil sync github --pull
```

What happens per task with an existing `SyncMapping`:

- Fetch the Issue via `provider.fetch_task(external_id=...)`.
- Compare `remote.last_modified` vs `existing.last_synced_at` and the local
  task's `updated_at`.
- If only the remote moved: apply the remote payload to the local task
  (emits `task.synced_from_remote`), bump the mapping to `in_sync`.
- If only the local moved: bump the mapping to `local_ahead` and emit
  `sync.push.deferred` with `resolution="local_moved_no_push"` so a
  follow-up `--push` advances it.
- If both moved: defer to the configured conflict-resolution strategy
  (see below).
- If the remote was deleted: flip the mapping to `external_deleted`; the
  next bare `anvil sync` surfaces it as a drift discrepancy.

Tasks without a `SyncMapping` are skipped on pull (no remote id to fetch
by). Run `--push` first to create the mapping.

---

## Both directions in one pass

```bash
anvil sync github
```

With neither `--push` nor `--pull`, the engine does both: push every task,
then pull every task that has a mapping. Scope to one task with
`--task T001`.

---

## Watch mode

```bash
anvil sync github --watch
```

Polls every 60 seconds (default) for changes in either direction. Override
the cadence with `--interval`:

```bash
anvil sync github --watch --interval 30      # poll every 30s
anvil sync github --watch --interval 0       # one iteration, then exit (test seam)
```

Stop with Ctrl-C — the SIGINT handler triggers a graceful shutdown that
finishes the current iteration and closes the provider's HTTP transport
before exiting.

Watch mode is daemon-grade: a single failing task (rate-limited, network
blip, manual_merge pending) does NOT kill the loop. Errors print to stderr
and the next poll continues.

---

## Conflict resolution

When a task changes both locally AND remotely between syncs, the configured
strategy decides what happens. The strategy lives on each `SyncMapping`
row (`conflict_resolution_strategy` enum) and defaults to `prompt` on
first push.

| Strategy        | Behaviour                                                                          |
|-----------------|------------------------------------------------------------------------------------|
| `local_wins`    | Mapping flips to `local_ahead`; local re-push is deferred to the next push pass.   |
| `remote_wins`   | Mapping flips to `remote_ahead`; local mutation from remote is deferred to the next pull. |
| `prompt`        | Interactive `[local/remote/skip]`. Defaults to `local_wins` on `--yes` or non-tty. |
| `manual_merge`  | Writes `.anvil/.sync-conflicts/<task_id>.md`; exits `2`; refuses to sync this task until resolved. |

For `manual_merge`: the markdown file shows local and remote side-by-side.
Resolve the file (edit local or accept remote), delete it, then rerun
`anvil sync github` to continue. The batch exits `2` if any task
is parked pending manual merge.

(As with every `.anvil/…` path in this guide, `.sync-conflicts/` sits under
wherever `anvil status` reports state actually lives — by default a
per-project HOME workspace under `~/.anvil/workspaces/<key>/`, not an
in-repo directory. See [`getting-started.md#where-your-state-lives`](getting-started.md#where-your-state-lives).)

**`prompt` won't work in `--watch`** (non-tty stdin → defaults to
`local_wins`). For watch mode, either pick a deterministic strategy
(`local_wins`, `remote_wins`, `manual_merge`) on the mapping, or accept
that the prompt path falls back silently. The audit event records
`resolution="prompt_defaulted_to_local"` so you can grep for these later.

The `--fix` flag reconciles remote state into local on conflicts — it forces
a pull for tasks whose `SyncMapping` is in the `conflict` state — useful when
the remote is the trusted version after an out-of-band edit:

```bash
anvil sync github --pull --fix
```

For the full audit-honesty contract (`_deferred` vs `_completed` semantics
in `events.jsonl`), see [`../github-sync.md` → Audit honesty](../github-sync.md#audit-honesty).

---

## Reconciliation: `anvil sync` (no provider)

The bare command runs the `ReconciliationEngine` only — no network, no
provider calls. It cross-checks SQLite state vs filesystem vs git and
prints a discrepancy report.

```bash
anvil sync                # report-only: lists drift
anvil sync --fix          # interactive: prompts before applying fixes
anvil sync --fix --yes    # auto-apply (required in CI / non-interactive)
```

Discrepancy kinds it surfaces:

- `orphan_branch` — `agent/*` branch with no matching claim (auto-fixable)
- `orphan_packet` — work packet on disk with no task row (auto-fixable)
- `orphan_worktree` — git worktree with no live claim (auto-fixable)
- `stale_claim` — claim past its lease with no heartbeat (auto-fixable)
- `missing_sync_mapping` — task is `done` but no mapping for a configured provider (manual: `anvil sync provider <id> --push --task <id>`)
- `drift_sync_state` — mapping in a non-`in_sync` state past the freshness window (manual: pull or push)

The two sync-related kinds print the suggested command but require manual
execution — pushing/pulling needs provider credentials and conflict-resolution
flow that the reconciliation engine doesn't own.

---

## Common failure modes

| Trigger                                  | What you see + fix                                                                  |
|------------------------------------------|-------------------------------------------------------------------------------------|
| `gh: command not found`                  | Install gh CLI OR set `GITHUB_TOKEN`. The provider auto-falls-back to HTTP.         |
| `401 Unauthorized` on push/pull          | Token expired or scope insufficient. Run `gh auth refresh -s repo` or rotate `GITHUB_TOKEN`. |
| `cannot instantiate provider 'github_issues'` | `GITHUB_REPOSITORY` env var missing or malformed. Export it as `owner/repo`.   |
| `RateLimitExceeded` (HTTP 429)           | Wrapped as `SyncProviderError`; the batch loop continues with the next task. That task gets `sync.push.failed` / `sync.pull.failed`. Re-run after the window resets. |
| `external_deleted` on stderr             | Issue was deleted on GitHub. Mapping flips to `external_deleted`; bare `anvil sync` surfaces it as drift; `--fix` prompts to remove the mapping. |
| Watch mode missed changes during a blip  | The outer `except Exception` keeps polling. Re-run `anvil sync github --pull` once to catch up. |
| `--fix` without `--yes` in non-tty       | Exits `1` with `--fix requires --yes in non-interactive mode`.                      |
| Exit code `2` from a sync run            | At least one task is parked in `manual_merge`. Resolve the file under `.anvil/.sync-conflicts/` (workspace-relative — see the note above), delete it, rerun. |

For the complete failure-mode matrix (per-iteration error survival, transport
flips, audit emission failures), see [`../github-sync.md` → Failure modes](../github-sync.md#failure-modes).

---

## Generic provider invocation

The `sync github` subcommand is an alias for `sync provider github_issues`.
The generic form takes any registered provider id:

```bash
anvil sync provider github_issues --push --task T001
anvil sync provider linear_issues --pull              # if registered
```

Same flags, same exit codes (`0` success, `1` generic error, `2` operator
input required).

---

## Writing your own provider

The `SyncProvider` Protocol lives in
`bin/src/anvil/sync/provider.py`. The GitHub Issues provider
(`bin/src/anvil/sync/providers/github_issues.py`) is the reference
implementation — dual transport (`gh_cli` / `http`), idempotent push,
tombstone-aware fetch, non-throwing `health_check`.

The Protocol uses `typing.Protocol` (structural typing, no inheritance).
A new provider needs five methods: `push_task`, `fetch_task`, `list_tasks`,
`delete_task`, `health_check`. Register at module load with
`register_sync_provider("my_provider_id", MyProviderClass)`, and the CLI's
`sync provider my_provider_id` command works end-to-end with no further
plumbing.

See [`../sync-providers.md`](../sync-providers.md) for the interface
contract, the `RecordedSyncProvider` test double, the error hierarchy, and
the step-by-step walkthrough for adding Linear support. The Phase 9 roadmap
in [`../roadmap.md`](../roadmap.md) tracks the in-tree Linear / Monday / Jira
providers.

---

## Where to next

- [Deep mechanics, audit vocabulary, status-label table → `../github-sync.md`](../github-sync.md)
- [Provider authoring, Protocol contract, error hierarchy → `../sync-providers.md`](../sync-providers.md)
- [Roadmap for Linear / Monday / Jira providers → `../roadmap.md`](../roadmap.md)
