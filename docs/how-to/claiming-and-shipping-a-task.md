# Claiming and shipping a task

> Claims are how anvil coordinates concurrent work across humans and AI agents. A claim is an exclusive lease on one task plus a git branch to do the work on, recorded atomically in SQLite with a heartbeat-extended expiry so a crashed or abandoned agent never permanently parks a task. This how-to walks the full lifecycle from `next` through `apply` from one contributor's perspective — every flag and behaviour described here is verified against `bin/src/anvil/cli/` and `bin/src/anvil/claims/`.

If you have not yet authored a PRD and promoted at least one task to `ready`, start at [`getting-started.md`](getting-started.md) and [`authoring-a-prd.md`](authoring-a-prd.md). This document assumes you have an approved PRD and at least one task in `ready`.

## The lifecycle at a glance

```text
ready → claimed → in_progress → needs_review → accepted → done
                       ↓               ↓
                    blocked         rejected → drafted
```

`claim` moves `ready → claimed`. The first hook-recorded file change auto-transitions to `in_progress`. `submit` moves to `needs_review`. `apply --approve` moves through `accepted → done`; `apply --reject` returns the task to `drafted` for rework.

The full 11-status state machine — including the named gates that fire on each transition — is documented in [`../architecture.md#task-lifecycle`](../architecture.md). The concurrency primitives that make claims safe under multi-actor load are in [`../architecture.md#concurrency-model`](../architecture.md).

## Step 1 — Pick the next task: `anvil next`

`next` is a non-mutating recommender. It scans tasks in `ready` status, filters out any with unmet dependencies or active claims (including conflict-group siblings), and returns the highest-priority candidate.

```bash
anvil next
```

Sample output:

```text
Next recommended task: T012
  Title:    Wire submit-progress evidence buffer flush
  Priority: high
  Complexity: 3

Run `anvil claim T012` to acquire the lease.
```

### How `next` ranks candidates

From `claims/manager.py::next_claimable()`, the sort key is:

1. **Priority desc** — `critical > high > medium > low`.
2. **Complexity asc** — lower score wins (simpler first); unscored tasks rank last.
3. **`created_at` asc** — oldest task wins on ties (fairness).

A task is excluded from the candidate set if any of the following hold:

- Status is not `ready`.
- Any task in `task.dependencies` is not yet `done`.
- An active claim exists for the task by any actor.
- A task in any of its `conflict_groups` already has an active claim.

The `--actor <name>` flag sets the identity recorded in the claim audit trail but does not affect ranking — `next` returns the same task regardless of actor.

`next` reaps stale claims before scanning, so an expired claim by another actor will not hide a task from you on this call.

## Step 2 — Claim the task: `anvil claim T012`

```bash
anvil claim T012
```

What happens, in order, inside the CLI ([`cli/claim.py::claim`](https://github.com/fakoli/anvil/blob/main/bin/src/anvil/cli/claim.py)):

1. **Stale-claim reap.** `detect_and_release_stale()` releases any expired leases first so the conflict check sees current truth.
2. **Pre-claim conflict check.** `manager.check_conflicts()` compares the task's `likely_files` against the `expected_files` of every active claim by another actor. Any overlap is printed to stderr; without `--force` the command exits non-zero before mutating state.
3. **Atomic claim transaction.** `manager.claim()` emits a `claim.created` event; the backend's SQLite handler inserts the `Claim` row and flips the task to `claimed` inside one `BEGIN IMMEDIATE` transaction.
4. **Git branch creation.** `create_branch_for_task()` runs `git checkout -b agent/<task_id>-<slug>`. The slug is derived from `task.title` (lowercase, alphanumeric + hyphens, max 40 chars). If the branch name collides with an existing one, `-2`, `-3`, ... is appended (capped at 20 attempts).
5. **Optional worktree.** When `--worktree` is passed, `create_worktree_for_task()` runs `git worktree add ../wt-<task_id> <branch>` next to the project root.

Sample output:

```text
Claimed task 'T012' as 'alice'.
  Claim ID:    C9F3A210
  Lease until: 2026-05-25T15:23:00+00:00
  Branch:      agent/t012-wire-submit-progress-evidence-buffer-flush

Run `anvil renew C9F3A210` to extend the lease before it expires.
```

### Claim flags

| Flag | Effect |
|---|---|
| `--worktree` | Also create a git worktree at `../wt-<task_id>/` so you can work on multiple claims in parallel without checkout-thrash. Skipped (with a stderr warning) if the working tree is dirty. |
| `--force` | Override file-overlap and conflict-group warnings; the conflict event is still logged. Use sparingly. |
| `--actor <name>` | Identity recorded on the claim. Defaults to `$USER`, then `agent`. |
| `--lease <minutes>` | Lease duration for this claim, overriding `default_lease_minutes` from project/global `config.yaml` (precedence: this flag > project config > global config > built-in default of `240`). |
| `--branch <name>` | Attach the claim to an existing or caller-named branch instead of the generated `agent/<task_id>-<slug>` name. The branch is checked out if it exists, created otherwise, and the name is recorded on the claim. |

### What the claim records

The `Claim` row carries `expected_files` (copied from `task.likely_files`), `claimed_by`, `lease_expires_at` (now + default lease), `last_heartbeat_at`, and `status="active"`. The `expected_files` list is what the `check-claim.sh` PreToolUse hook uses to warn when an Edit/Write targets a file outside the recorded scope.

### Git is not required

`create_branch_for_task()` returns a non-blocking warning when `git` is missing or the cwd is not a git repository — the claim still succeeds, you just don't get a branch. anvil must work without git so non-source projects (writing, research) can use it.

## Step 3 — Get the work packet: `anvil packet T012`

```bash
anvil packet T012
```

The packet is the complete context one agent needs to execute the task — and nothing else. It is rendered from canonical state by [`context/packets.py::render_packet`](https://github.com/fakoli/anvil/blob/main/bin/src/anvil/context/packets.py) and written to `.anvil/packets/T012.md`.

> Every `.anvil/…` path in this guide is shorthand for wherever `anvil status` reports state actually lives — by default a per-project HOME workspace under `~/.anvil/workspaces/<key>/`, not an in-repo directory. See [`getting-started.md#where-your-state-lives`](getting-started.md#where-your-state-lives).

Sections in the markdown packet:

- **Header** — task ID, title, feature, status, priority, agent-suitability and complexity scores.
- **Goal** — the task description verbatim.
- **Acceptance criteria** — bulleted, from `task.acceptance_criteria`.
- **Dependencies (completed)** and **(open)** — the upstream context separated by status.
- **Scope (likely files)** — file paths the agent should focus on.
- **Constraints / non-goals** — from `task.implementation_notes`.
- **Decisions affecting this task** — pre-filtered to ones that reference this task.
- **Verification** — the commands, required-evidence list, and manual steps the gate will check against on apply.
- **Active claim** — claim ID, lease expiry, branch, worktree (when a claim is held).
- **Update protocol** — the exact `renew` and `submit` commands for this claim.

### Two formats

```bash
anvil packet T012                # markdown → .anvil/packets/T012.md
anvil packet T012 --format json  # JSON → .anvil/packets/T012.json
anvil packet T012 -f json        # short form
```

The JSON form mirrors the markdown sections one-for-one and is what the MCP `generate_work_packet` tool returns to agents. The content the CLI writes to disk is also echoed to stdout so callers can pipe.

## Step 4 — Do the work

Switch to the agent branch and edit the files in scope. Three hooks fire during the work session:

- **`check-claim.sh` (PreToolUse on Edit/Write/NotebookEdit)** — warns to stderr if the file you are about to edit is outside the claim's `expected_files`. Non-blocking by hook contract.
- **`record-file-change.sh` (PostToolUse on Edit/Write/NotebookEdit)** — records the change against the active claim for orphan detection.
- **`capture-evidence.sh` (PostToolUse on Bash)** — when the command matches a verification pattern (`pytest`, `npm test`, `cargo test`, `ruff`, `mypy`, ...) the output is buffered against the active claim. The buffer is consumed by `submit` and is documented in [`../evidence-buffer.md`](../evidence-buffer.md).

The first file change auto-transitions the task `claimed → in_progress`.

## Step 5 — Renew the lease before it expires

A claim's lease expires after `default_lease_minutes` (the `ClaimManager` ships with `240` as the in-code default; the project-level override lives in `.anvil/config.yaml`). Renew it before expiry:

```bash
anvil renew C9F3A210
```

Sample output:

```text
Renewed claim 'C9F3A210'.
  New lease until: 2026-05-25T16:23:00+00:00
  Last heartbeat:  2026-05-25T15:23:00+00:00
```

The heartbeat sets `last_heartbeat_at = now` and `lease_expires_at = now + default_lease_minutes`. Only the owning actor can renew an active claim — `renew` fails with `ClaimError` on actor mismatch or non-active status. Pass `renew C9F3A210 --lease <minutes>` to extend by a different duration than `default_lease_minutes` for this renewal.

### What happens if you do not renew

When the lease passes its expiry, the **next mutating CLI or MCP call by any actor** runs `detect_and_release_stale()` at entry, which emits a `claim.stale` event for every expired claim. The SQL handler flips the claim's status to `stale` and the task's status back to `ready`. The audit trail records the original claimant — nothing is silently lost.

A `renew` against an already-expired lease raises `ClaimError`: "lease expired ... please re-claim the task." Re-claiming is a fresh `claim T012`; the old claim row remains in history as `stale`.

You can force-release someone else's stale (or active) claim with `--force`:

```bash
anvil release C9F3A210 --force --reason "stale; reclaiming for hot-fix"
```

Note that `release` takes the **claim ID** (`C9F3A210`), not the task ID — claims have their own identifier (`C` + 8 hex chars) so the audit trail survives multiple claims per task over time.

## Step 6 — Submit evidence: `anvil submit T012`

```bash
anvil submit T012 \
    --commands "pytest tests/test_submit.py -v" \
    --files-changed "bin/src/anvil/cli/packet_apply.py" \
    --files-changed "tests/test_submit.py" \
    --output-file /tmp/pytest-output.log \
    --pr-url "https://github.com/you/repo/pull/142"
```

### Submit flags

| Flag | Required? | Effect |
|---|---|---|
| `--commands` | yes | Verification command that was run (e.g. `pytest tests/`). Repeatable — pass `--commands` once per command so a command containing a comma survives intact. |
| `--files-changed` | yes | File path modified. Repeatable — pass `--files-changed` once per path. |
| `--output-file` | no | Path to a file whose contents are read (truncated to 8000 chars) and stored as the output excerpt. |
| `--pr-url` | no | Pull request URL — checked by the evidence gate when `required_evidence` mentions "PR" or "pull request". |
| `--commit-sha` | no | Commit SHA pinned to this submission. |
| `--known-limitations` | no | Free-text caveats. Checked by the evidence-gate fallback when a required-evidence item does not match any structured field. |
| `--screenshots` | no | Comma-separated paths to screenshot files — required when `required_evidence` mentions "screenshot" (the gate checks `evidence.screenshots` is non-empty). |
| `--actor` | no | Submitting actor; defaults to `$USER`, then `agent`. |

Back-compat: passing `--commands` (or `--files-changed`) exactly once still
splits that single value on commas, so the older
`--commands "a,b" --files-changed "x,y"` form keeps working — but the
repeatable form above is canonical and is the only form that survives a
value with an embedded comma intact.

`submit` locates the active claim for the task (one per task at most), constructs an `Evidence` row with a fresh ID (`EV` + 8 hex), emits an `evidence.submitted` event, and the backend handler atomically:

1. Inserts the `Evidence` row.
2. Transitions the task `in_progress → needs_review`.
3. Releases the active claim.

The output prints an **evidence gate summary** so you see immediately whether the submission will pass the apply gate:

```text
Evidence submitted for task 'T012'.
  Evidence ID:  EVA1B2C3D4
  Claim ID:     C9F3A210 (auto-released)
  Submitted by: alice
  ...
Task 'T012' status → needs_review.
Run `anvil apply T012` when ready for human review.
Evidence gate: PASSED — all required evidence present.
```

If `task.verification.required_evidence` is unsatisfied you get:

```text
Evidence gate: INCOMPLETE — missing items for required_evidence:
  - test output
  - PR link
```

Re-run `submit` with the missing flag (`--commands` for test output, `--pr-url` for PR link, etc.). Each `submit` creates a new `Evidence` row; the latest one is what `apply` reviews.

### How the evidence gate matches

[`review/gates.py::evidence_complete`](https://github.com/fakoli/anvil/blob/main/bin/src/anvil/review/gates.py) maps each item in `required_evidence` to a structured field using substring rules:

| Required-evidence item contains | Checked against |
|---|---|
| "test", "pytest", "cargo test" | `evidence.commands_run` (and the command must actually execute tests — `pytest --collect-only` does not satisfy) |
| "PR" (word-boundary) or "pull request" | `evidence.pr_url` |
| "screenshot" | `evidence.screenshots` non-empty (populated via `--screenshots path1.png,path2.png` on `submit`) |
| "files changed" | `evidence.files_changed` non-empty |
| anything else | substring match in `evidence.output_excerpt` or `evidence.known_limitations` |

Match is case-insensitive. The word-boundary on "PR" exists because plain substring matching gave false positives on words like "improve", "approve", "process" (Greptile + Critic-1, PR #41).

## Step 7 — Apply: `anvil apply T012`

`apply` is the merge gate. It is **human-only by default** — not exposed via the MCP surface — so an agent cannot self-approve its own work.

### Review-only mode (no flag)

Run `apply` without `--approve` or `--reject` to see the gate verdict without mutating state:

```bash
anvil apply T012
```

```text
Task 'T012' awaiting review (status: needs_review).

Evidence gate: PASSED — all required evidence present.

Pass --approve to accept or --reject --reason TEXT to reject.
```

### Approve

```bash
anvil apply T012 --approve
```

Emits a `task.applied` event with `decision="accepted"`. The handler transitions `needs_review → accepted → done` atomically and records the reviewer (defaults to `$USER`, then `human` — set with `--reviewer`).

```text
Task 'T012' approved by 'alice' → done.
```

### Reject

```bash
anvil apply T012 --reject --reason "missing rate-limit test for the 429 path"
```

`--reject` requires `--reason` (the CLI errors out otherwise). The task transitions `needs_review → rejected → drafted`, the reason is logged on the `task.applied` event, and the author can re-edit the PRD, re-run `prd parse`, re-score, and re-claim.

`--approve` and `--reject` are mutually exclusive — passing both errors out.

## When something goes wrong

| Symptom | Cause | Fix |
|---|---|---|
| `Task 'T012' cannot be claimed: status is 'claimed'` | Someone (you or another actor) already holds the claim. | `anvil list --status claimed` to find the holder. Wait, or `release C... --force` if it is stale. |
| `Task 'T012' cannot be claimed: no PRD found` | You ran `claim` before `prd parse`. | Author `.anvil/prd.md`, run `prd parse`, then `prd review`. |
| `conflicts with active claims: ... overlapping files: [...]` | Another actor's claim's `expected_files` overlap yours. | Coordinate, wait, or `claim --force` if you are sure. The conflict is logged either way. |
| `Claim 'C...' lease expired at ... please re-claim` | You let the heartbeat lapse. | `anvil claim T012` again — the stale claim auto-reaps on the next mutating call. |
| `no active claim found for task 'T012'` on `submit` | The claim was released or expired before you submitted. | Re-claim, redo the work (or pick up from the branch) and submit again. |
| `Evidence gate: INCOMPLETE — missing items` | `submit` did not satisfy `task.verification.required_evidence`. | Re-run `submit` with the missing flag (`--commands`, `--pr-url`, etc.). |
| `--reject requires --reason TEXT` | You passed `--reject` without `--reason`. | Add `--reason "<text>"`. |
| `expected 'needs_review'` on apply | Task is not in `needs_review` — likely you ran `apply` before `submit`. | Run `submit` first. |

### Abandoning a claim cleanly

```bash
anvil release C9F3A210 --reason "abandoning, blocked on upstream API"
```

The task returns to `ready` and is immediately claimable by anyone. The reason is recorded on the `claim.released` event so the next claimant has context.

### Force-releasing someone else's claim

```bash
anvil release C9F3A210 --force --reason "stale recovery; original actor offline"
```

`--force` bypasses the actor-ownership check and lets you release an `active` or `stale` claim by anyone. The original `claimed_by` is preserved on the event for audit.

## What gets recorded

Every step above appends to two places: the `events` table inside `state.db` (assigned a monotonic id inside `BEGIN IMMEDIATE`) and `events.jsonl` (append-only mirror, written after commit). A full claim → ship cycle produces this event sequence:

```text
claim.created      → claim row inserted; task ready → claimed
task.status_changed (claimed → in_progress, on first file change)
file_changed       × N (one per recorded edit)
bash_command_run   × M (one per captured verification command)
claim.renewed      × K (one per heartbeat)
evidence.submitted → Evidence row inserted; task in_progress → needs_review; claim auto-released
task.applied       → task needs_review → accepted → done (or → rejected → drafted)
```

This is the audit trail that backs anvil's replay guarantee — see [`../architecture.md#event-log-and-jsonl-replay`](../architecture.md).

## Where to next

- [Full concurrency model and conflict semantics: `../architecture.md#concurrency-model`](../architecture.md)
- [The evidence buffer and how `capture-evidence.sh` feeds `submit`: `../evidence-buffer.md`](../evidence-buffer.md)
- [Sync state to GitHub Issues: `../github-sync.md`](../github-sync.md)
- [MCP surface for agents: `../mcp.md`](../mcp.md)
- [PRD template and the readiness gate: `../prd-template.md`](../prd-template.md)
