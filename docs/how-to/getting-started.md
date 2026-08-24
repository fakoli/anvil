# Getting started with anvil

> anvil is a local-first, backend-neutral project-state layer for humans and AI coding agents — the durable record of every requirement, task, claim, and piece of evidence in your project, stored in SQLite in a per-project workspace under `~/.anvil/` and exposed through a CLI and an MCP server. This walkthrough takes you from an empty directory to a shipped task in about five minutes.

New to anvil's vocabulary — packet, claim, lease, gate? Keep the [glossary](../glossary.md) open in another tab.

## What you'll do

In ~5 minutes, you will:

- Initialize state in an empty project directory.
- Author a 12-line PRD against the template.
- Parse, review, and approve the PRD through its two-step gate.
- Generate, score, and promote tasks to `ready`.
- Claim the first task, get a work packet, submit evidence, and apply it.

By the end you will have one task in `done`, one claim recorded in `events.jsonl`, and an `agent/t001-<slug>` git branch holding the work.

## Where your state lives

By default anvil keeps state **outside** your project, in a per-project HOME workspace: `~/.anvil/workspaces/<dirname>-<hash8>/.anvil/` (e.g. `~/.anvil/workspaces/my-project-183a2542/.anvil`). Your repo stays clean — no `.anvil/` directory appears inside it. `anvil status` prints the exact location on its `Path:` line, and `anvil init` prints the absolute PRD path in its next-step hint. Two environment variables override the default: `ANVIL_STATE_LAYOUT=local` restores the in-repo `./.anvil/` layout, and `ANVIL_ROOT=<dir>` pins state to `<dir>/.anvil` literally. Wherever this guide writes `.anvil/…`, it means the state directory `anvil status` reports.

## Prerequisites

- Claude Code 1.x (or any MCP-compatible runtime).
- `uv` installed — see [docs.astral.sh/uv](https://docs.astral.sh/uv/). The plugin auto-resolves Python deps on first invocation; no manual `pip install`.
- `git` available on PATH — `claim` creates an `agent/<task>-<slug>` branch.
- An empty or existing project directory you can write to.

## Step 1 — Install the plugin

Add the marketplace once, then install the plugin, both inside Claude Code:

```bash
/plugin marketplace add fakoli/anvil
/plugin install anvil@anvil
```

The install registers five hooks, wires the MCP server, and makes the five plugin agents discoverable at next session start. Verify with:

```bash
anvil --version
# → anvil 0.6.5 (schema 21)
```

> **Not using Claude Code?** Install the CLI + MCP server from PyPI instead —
> `uv tool install anvil-state` (or `pipx install anvil-state`) — then wire your
> harness with `anvil install <harness>`. See
> [using anvil on any harness](using-anvil-on-any-harness.md).

## Step 2 — Initialize state in your project

```bash
cd /path/to/your/project
anvil init --name "My Project"
```

Output:

```
Initialized anvil for 'My Project' (id: my-project)

  ~/.anvil/workspaces/my-project-183a2542/.anvil/config.yaml
  ~/.anvil/workspaces/my-project-183a2542/.anvil/state.db
  ~/.anvil/workspaces/my-project-183a2542/.anvil/events.jsonl
  ~/.anvil/workspaces/my-project-183a2542/.anvil/packets/

Next step: author your PRD at ~/.anvil/workspaces/my-project-183a2542/.anvil/prd.md, then run `anvil prd parse`.

Your prd.md must contain these required sections:
  # Project: <Name>
  ## Summary
  ## Goals
  ## Requirements
Optional ## Features / ## Tasks use bold-inline fields, e.g.
  **Feature:** F001   (under a ### Txxx task heading)
  **Requirements:** R001, R002   (under a ### Fxxx feature heading)
See docs/prd-template.md for the full template.
```

Note the paths: state landed in the HOME workspace, not in your repo. `prd.md` is intentionally NOT auto-created — you author it next, at the absolute path `init` just printed, against the template.

## Step 3 — Author your PRD

Open the `prd.md` path that `init` printed (under `~/.anvil/workspaces/…`) in your editor and paste a minimal valid PRD. The required sections are `# Project:`, `## Summary`, `## Goals`, `## Requirements`, plus at least one task in `## Tasks` to actually have something to claim. Any task that declares a `**Feature:** F00N` line must have a matching `### F00N:` block in `## Features`. Full schema in [`../prd-template.md`](../prd-template.md).

> **Requirement IDs are strict.** Every requirement must use the canonical `R0NN` form — `R001`, `R002`, `R003`. Suffixed or ad-hoc IDs like `R003a` are refused by the parser, so number requirements canonically *before* you run `anvil prd parse` — splitting a requirement means renumbering, not suffixing.

> **Multi-PRD note.** A project can hold several release-scoped PRDs in one `state.db`, each separately gated; run `anvil prd list` to see them. The default PRD's source is the bare `.anvil/prd.md` used throughout this guide (conceptually `.anvil/prds/default.md`); a named release PRD uses its portable source under `.anvil/prds/` and is parsed with `anvil prd parse --prd <prd_id>`. Lowercase non-reserved IDs keep the familiar `<prd_id>.md` spelling; see the PRD template for uppercase and Windows-reserved filename rules. Re-parsing a PRD replaces the `Requirement` rows in **that PRD's partition only**, leaving sibling PRDs untouched (Features and Tasks are (re)generated by `anvil plan`, which prunes orphans). Single-PRD projects can ignore all of this and keep using `.anvil/prd.md`.

```markdown
# Project: My Project

## Summary

A small utility that uppercases the contents of a text file in place.

## Goals

- Convert any UTF-8 text file to uppercase with one command.
- Exit non-zero with a clear message on missing or unreadable files.

## Requirements

- R001: The CLI accepts one positional argument: the file path.
- R002: The file is read as UTF-8 and rewritten in uppercase in place.
- R003: Missing or unreadable files exit 1 with a message naming the file.

## Features

### F001: Uppercase CLI

The CLI entry point that reads a text file as UTF-8 and rewrites it in place
with uppercase contents.

**Requirements:** R001, R002, R003

## Tasks

### T001: Implement uppercase CLI

**Feature:** F001
**Priority:** high
**Likely files:** src/upper/cli.py

Parse the positional file argument, read as UTF-8, write back uppercased.

**Acceptance criteria:**

- `upper sample.txt` rewrites `sample.txt` with uppercase contents.
- `upper missing.txt` exits 1 and prints a message naming the file.

**Verification:**

- `pytest tests/test_cli.py -v`
```

## Step 4 — Parse and review the PRD

```bash
anvil prd parse
# → Parsed 3 requirements, 1 features, 1 tasks.
# → PRD source: default

anvil prd assess            # optional, advisory, and read-only

anvil prd review            # draft → reviewed
# → PRD reviewed by 'human'.
# → Run `anvil prd review --approve` to approve.

anvil prd review --approve  # reviewed → approved
# → PRD approved by 'human'.
```

The assessment highlights missing user context, outcomes, observable behavior,
boundaries, acceptance scenarios, and verification with a suggested challenge
question. Its findings are advisory: they do not alter parsing, approval, or
claiming. The two-step review gate remains deliberate. `prd review` records
that a human has read the PRD; `prd review --approve` unlocks task claiming.
The claims manager refuses to claim any new task while the PRD is in `draft` or
`reviewed` status. Only an `approved` PRD whose exact source/material binding is
still current lets work begin. Existing active claims are not revoked by a
later PRD edit.

## Step 5 — Generate and score tasks

```bash
anvil plan
# → Planned 1 features, 1 tasks.

anvil score
# TaskID       Complexity Parallel CtxLoad Blast Review Agent
# -----------------------------------------------------------
# T001                  2        4       2     4      3     2
#
# Scored 1 task(s).

anvil review tasks
# → Promoted 1 task(s) to reviewed.
# → Promoted 1 task(s) to ready.
# → 2 total promotion(s). No tasks blocked.

anvil list --status ready
# TaskID  Title                    Status  Priority  Type             Score  Feature
# ----------------------------------------------------------------------------------
# T001    Implement uppercase CLI  ready   high      feature            2/2  F001
#
# 1 task(s) listed.
```

Six dimensions: complexity, parallelizability, context load, blast radius, review risk, agent suitability — each 1–5. Scores drive `anvil next` routing and `expand` recommendations. In the list table, **Type** is the kind of change the task represents (`feature`, `bugfix`, `refactor`, or `modify`) and **Score** is shorthand for `complexity/agent-suitability`.

## Step 6 — Claim and ship the first task

```bash
anvil next
# → Next recommended task: T001
#     Title:    Implement uppercase CLI
#     Priority: high
#     Complexity: 2

anvil claim T001
# → Claimed task 'T001' as 'cc80db5f1e33f5f6'.
#     Claim ID:    CBA2432F4
#     Lease until: 2026-07-02T06:33:13.691911+00:00
#     Branch:      agent/t001-implement-uppercase-cli
```

Claim IDs are random — yours will not be `CBA2432F4` — and the lease defaults to 240 minutes; run `anvil renew <claim-id>` to extend it on long-running work. The claim also created the `agent/t001-implement-uppercase-cli` branch in **your project's** git repo (state lives in the workspace, branches live where the code is).

```bash
anvil packet T001
```

```
Wrote packet to ~/.anvil/workspaces/my-project-183a2542/.anvil/packets/T001.md

# T001 — Implement uppercase CLI

**Feature:** F001 — Uppercase CLI
**Status:** claimed
**Priority:** high
**Type:** feature
**Agent suitability:** 2/5
**Complexity:** 2/5

## Goal

Parse the positional file argument, read as UTF-8, write back uppercased.

## Acceptance criteria

- `upper sample.txt` rewrites `sample.txt` with uppercase contents.
- `upper missing.txt` exits 1 and prints a message naming the file.

...

## Verification

Commands:
- `pytest tests/test_cli.py -v`

Required proofs (typed — captured by the run hooks):
- `pytest tests/test_cli.py -v` exits 0
...
```

The packet — printed to stdout and written under `packets/` in the workspace — contains the exact intent, acceptance criteria, verification commands, and non-goals the agent (or you) need to execute the work. No issue thread to summarize.

Do the work on the `agent/t001-*` branch, then submit evidence and apply:

```bash
anvil submit T001 \
    --commands "pytest tests/test_cli.py" \
    --files-changed src/upper/cli.py
```

```
Evidence submitted for task 'T001'.
  Evidence ID:  EV893EFA1D
  Claim ID:     CBA2432F4 (auto-released)
  Submitted by: cc80db5f1e33f5f6
  Commands:     ['pytest tests/test_cli.py']
  Files:        ['src/upper/cli.py']

Task 'T001' status → needs_review.
Run `anvil apply T001` when ready for human review.
Evidence gate: INCOMPLETE — missing items for required_evidence:
  - `pytest tests/test_cli.py -v` exits 0
```

The `Evidence gate: INCOMPLETE` line is **advisory, not an error** — the submit succeeded and the task moved to `needs_review`. A plain-CLI submit records your commands as strings; the typed exit-code proofs the gate checks for are captured by the run hooks (e.g. when a harness executes the verification commands), so a bare CLI walkthrough is expected to show this line.

```bash
anvil apply T001 --approve
# → Task 'T001' approved by 'human' → done.
# → Signed proof: ~/.anvil/workspaces/my-project-183a2542/.anvil/proofs/T001-E000015.json
```

## What just happened?

`state.db` now records `T001=done` and the claim released. `events.jsonl` has
the full audit trail: `project.created`, `state.initialized`, `prd.parsed`,
`prd.reviewed`, `prd.approved`, one `planning.batch_applied` event containing
the ordered feature/task/status/conflict graph, followed by scoring, task
review, claim, evidence, and apply events. Replaying that log from an empty
database reconstructs `state.db` byte-for-byte — the audit guarantee that
makes `.anvil/` safe to back up by copy.

`anvil status` sums it up, including the `Path:` line pointing at the workspace and a per-bucket task breakdown:

```
anvil for "My Project" (id: my-project)
Path: ~/.anvil/workspaces/my-project-183a2542/.anvil
Initialized: 2026-07-02T02:32:37.623329Z

PRD default (approved)
  Tasks:         1 total (0 ready, 0 claimed, 0 in_progress, 0 needs_review, 0 blocked, 1 done)
  Active claims: 0

PROJECT TOTAL
PRD:           approved
Tasks:         1 total (0 ready, 0 claimed, 0 in_progress, 0 needs_review, 0 blocked, 1 done)
Active claims: 0
Sync:          off
Schema:        21
```

The work packet under `.anvil/packets/T001.md` is the contract that drove the work. For the full picture of how transitions, gates, claims, and the event log fit together, see [`../architecture.md`](../architecture.md).

## Upgrading and uninstalling

Anvil can be loaded from several places at once: the `anvil` CLI and
`anvil-mcp` server from the Python tool install, plus a harness plugin or
generated MCP configuration. Upgrade them as one unit. A long-lived harness
does not replace its already-running MCP process just because the executable
on disk changed.

### Before upgrading: identify the active executable

Run these checks in the project whose state you intend to open.

On Bash:

```bash
command -v anvil
command -v anvil-mcp
anvil --version
```

On PowerShell:

```powershell
Get-Command anvil, anvil-mcp | Select-Object Name, Source | Out-Host
anvil --version
```

### Upgrade the CLI and MCP executable

| Installation method | Upgrade command |
|---|---|
| `uv tool` | `uv tool upgrade anvil-state` |
| `pipx` | `pipx upgrade anvil-state` |

These tool-manager commands replace the executable environment. They do not
open this project's `state.db`.

Before resuming a skill-driven workflow, verify that the upgraded executable
exposes the named-PRD capability required by current skills:

```bash
anvil prd source-name --help
```

A nonzero result means the active executable is still stale; do not infer a
PRD filename or continue with a mutating command.

### Resolve and back up state with the upgraded CLI

The target release provides `--path-only`, which resolves the state directory
without opening its database. Use the upgraded CLI to make the backup before
running ordinary `status` or any other backend-initializing command.

On Bash:

```bash
STATE_DIR=$(anvil status --path-only)
printf 'State directory: %s\n' "$STATE_DIR"
BACKUP_DIR="${STATE_DIR}.pre-upgrade-$(date +%Y%m%d-%H%M%S)"
cp -a "$STATE_DIR" "$BACKUP_DIR"
```

On PowerShell:

```powershell
$stateDir = anvil status --path-only
"State directory: $stateDir"
$backupDir = "$stateDir.pre-upgrade-$(Get-Date -Format yyyyMMdd-HHmmss)"
Copy-Item -Recurse -LiteralPath $stateDir -Destination $backupDir
```

If the upgraded engine is newer and you want a deliberate migration, choose
that path now, before ordinary `status` initializes the backend:

```bash
anvil migrate state        # dry run; review the reported backup path
anvil migrate state --yes  # apply only after reviewing the dry run
```

Otherwise continue with ordinary status and allow the supported automatic
migration. In either case, inspect the resulting version boundary.

On Bash:

```bash
anvil --version
STATUS_JSON=$(anvil status --json || true)
printf '%s\n' "$STATUS_JSON" | python -c 'import json,sys; p=json.load(sys.stdin); s=p["data"] if p["ok"] else p["error"]; print({"status":"compatible" if p["ok"] else s["code"],"engine_schema":s["schema_version"] if p["ok"] else s.get("supported_schema"),("pre_open_database_schema" if p["ok"] else "database_schema"):s["db_schema_version"] if p["ok"] else s.get("database_schema")})'
```

On PowerShell:

```powershell
anvil --version
$status = anvil status --json | ConvertFrom-Json
if ($status.ok) {
    [pscustomobject]@{
        status = "compatible"
        engine_schema = $status.data.schema_version
        pre_open_database_schema = $status.data.db_schema_version
    }
} else {
    [pscustomobject]@{
        status = $status.error.code
        engine_schema = $status.error.supported_schema
        database_schema = $status.error.database_schema
    }
}
```

`anvil --version` identifies the CLI engine and supported schema, for example
`anvil 0.6.5 (schema 21)`. `schema_version` is that engine schema.
`db_schema_version` (shown as `pre_open_database_schema`) is the database stamp
observed before the backend opens. If the command succeeds with a lower
pre-open value, that same call completed a supported migration; rerun the
status block to confirm the values are now equal. On mismatch, no migration
occurs and the comparison comes from `supported_schema` and `database_schema`
in the closed error envelope. `--path-only` never opens the database, so it
still identifies the backup target when the installed engine cannot open that
schema.

### Refresh the harness integration

| Installed integration | Refresh command | Required follow-up |
|---|---|---|
| Claude Code plugin | `claude plugin marketplace update anvil`, then `claude plugin update anvil@anvil` | Fully restart Claude Code so SessionStart and MCP load the new plugin. |
| Codex native integration | `codex plugin marketplace upgrade anvil`, then `anvil install codex --write` | Restart Codex after the refreshed marketplace and CLI/MCP install are verified. |
| OpenClaw native integration | `anvil install openclaw --write` | Restart OpenClaw after the refreshed CLI/MCP install is verified. |
| Other MCP client | `anvil mcp-config <client>` and replace the managed config block | Restart the client so it launches a fresh `anvil-mcp`. |

Use this order:

1. Stop state mutations and identify the active executables.
2. Upgrade the Python CLI/MCP install; this does not open project state.
3. With the upgraded CLI, resolve the state path and make the backup.
4. Run the engine-version and schema checks.
5. Refresh the plugin or harness integration that launches the MCP server.
6. Fully restart every harness and MCP server process.
7. Verify the live MCP initialize metadata below.

### Verify the live MCP process

This sends one MCP initialize request to the same `anvil-mcp` executable a
harness launches and prints its `serverInfo`. The reported version must match
the engine version from `anvil --version`, not the FastMCP dependency version.

On Bash:

```bash
MCP_INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"anvil-version-check","version":"1"}}}'
printf '%s\n' "$MCP_INIT" | anvil-mcp 2>/dev/null |
  python -c 'import json,sys; print(json.loads(sys.stdin.readline())["result"]["serverInfo"])'
```

On PowerShell:

```powershell
$mcpInit = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"anvil-version-check","version":"1"}}}'
($mcpInit | anvil-mcp 2>$null | Select-Object -First 1 |
    ConvertFrom-Json).result.serverInfo
```

If SessionStart names different plugin and PATH versions, refresh only the
stale component it identifies, then restart the harness. If MCP still reports
the old version, an old server process is still alive or the harness is
launching a different executable than `Get-Command`/`command -v` found.

If the database schema is newer than the engine, do not delete `state.db` or
the state directory. Upgrade the stale CLI/plugin/MCP component and restart
the harness. Routine version recovery never requires deleting state; see
[Migrations](../migrations.md) for the supported migration ladder.

### Uninstall or roll back
- **Roll back a harness install**: `anvil install <harness> --rollback`
  restores every file `anvil install <harness> --write` modified from its
  backup and deletes anything anvil created (native installs also run the
  harness's own removal command, e.g. `codex mcp remove`). To remove the
  Claude Code plugin itself, use Claude Code's own `/plugin` management UI —
  the plugin never writes files outside Claude Code's own config, so there is
  nothing else on disk to clean up.

## Common stumbles

- **"PRD must be in 'reviewed' status to approve"** — you ran `prd review --approve` without first running `prd review`. The two-step pattern is intentional. Run `anvil prd review` first, then `anvil prd review --approve`.
- **"No ready tasks"** — your PRD's `## Tasks` section is empty, or `review tasks` blocked promotion because `**Acceptance criteria:**` or `**Verification:**` is missing on a task. Both fields are required by the `drafted → reviewed` gate. Re-check [`../prd-template.md`](../prd-template.md).
- **"PRD file not found"** — `init` does not create `prd.md`. Author it at the absolute path `init` printed (under `~/.anvil/workspaces/…`) before running `prd parse`.
- **`Warning: git branch not created — not a git repository` on claim** — the claim itself still succeeded, but there is no repo to hold the `agent/<task>-<slug>` branch. Run `git init` (plus a first commit) in your project, then `anvil release <claim-id>` and re-claim.
- **`uv` not found** — install it: `pip install uv` or follow [docs.astral.sh/uv](https://docs.astral.sh/uv/).
- **Want to start over?** — run `anvil init --force` to wipe and re-scaffold. Don't reach for `rm -rf .anvil/` in your project: in the default layout there is no `.anvil/` there, so it's a no-op — state lives in the HOME workspace.

## Where to next

- [Author a real PRD: `authoring-a-prd.md`](authoring-a-prd.md)
- [Full lifecycle deep dive: `claiming-and-shipping-a-task.md`](claiming-and-shipping-a-task.md)
- [Sync to GitHub Issues: `syncing-with-github.md`](syncing-with-github.md)
- [Architecture reference: `../architecture.md`](../architecture.md)
- [CLI reference: `../cli-reference.md`](../cli-reference.md)
- [PRD template and schema: `../prd-template.md`](../prd-template.md)
