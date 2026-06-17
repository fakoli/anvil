# Getting started with fakoli-state

> fakoli-state is a local-first, backend-neutral project-state layer for humans and AI coding agents — the durable record of every requirement, task, claim, and piece of evidence in your project, stored in SQLite under `.fakoli-state/` and exposed through a CLI and an MCP server. This walkthrough takes you from an empty directory to a shipped task in about five minutes.

## What you'll do

In ~5 minutes, you will:

- Initialize state in an empty project directory.
- Author a 12-line PRD against the template.
- Parse, review, and approve the PRD through its two-step gate.
- Generate, score, and promote tasks to `ready`.
- Claim the first task, get a work packet, submit evidence, and apply it.

By the end you will have one task in `done`, one claim recorded in `events.jsonl`, and an `agent/t001-<slug>` git branch holding the work.

## Prerequisites

- Claude Code 1.x (or any MCP-compatible runtime).
- `uv` installed — see [docs.astral.sh/uv](https://docs.astral.sh/uv/). The plugin auto-resolves Python deps on first invocation; no manual `pip install`.
- `git` available on PATH — `claim` creates an `agent/<task>-<slug>` branch.
- An empty or existing project directory you can write to.

## Step 1 — Install the plugin

From the fakoli marketplace inside Claude Code:

```bash
/plugin install fakoli-state
```

The install registers four hooks, wires the MCP server, and makes the six plugin agents discoverable at next session start. Verify with:

```bash
fakoli-state --version
# → fakoli-state 1.10.0
```

## Step 2 — Initialize state in your project

```bash
cd /path/to/your/project
fakoli-state init --name "My Project"
```

Output:

```
Initialized fakoli-state for 'My Project' (id: my-project)

  .fakoli-state/config.yaml
  .fakoli-state/state.db
  .fakoli-state/events.jsonl
  .fakoli-state/packets/

Next step: author your PRD at .fakoli-state/prd.md, then run `fakoli-state prd parse`.
```

`prd.md` is intentionally NOT auto-created — you author it next against the template.

## Step 3 — Author your PRD

Open `.fakoli-state/prd.md` in your editor and paste a minimal valid PRD. The required sections are `# Project:`, `## Summary`, `## Goals`, `## Requirements`, plus at least one task in `## Tasks` to actually have something to claim. Any task that declares a `**Feature:** F00N` line must have a matching `### F00N:` block in `## Features`. Full schema in [`../prd-template.md`](../prd-template.md).

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
fakoli-state prd parse
# → Parsed 3 requirements, 1 features, 1 tasks.
# → PRD source: .fakoli-state/prd.md

fakoli-state prd review            # draft → reviewed
# → PRD reviewed by 'human'.
# → Run `fakoli-state prd review --approve` to approve.

fakoli-state prd review --approve  # reviewed → approved
# → PRD approved by 'human'.
```

The two-step gate is deliberate. `prd review` records that a human has read the PRD; `prd review --approve` unlocks task claiming. The claims manager refuses to claim any task while the PRD is in `draft` or `reviewed` status — only `approved` (or explicitly `reviewed` for the readiness gate) lets work begin.

## Step 5 — Generate and score tasks

```bash
fakoli-state plan
# → Planned 1 features, 1 tasks.

fakoli-state score
# TaskID       Complexity Parallel CtxLoad Blast Review Agent
# ---------------------------------------------------------------
# T001                  2        4       2     2      2     4
#
# Scored 1 task(s).

fakoli-state review tasks
# → Promoted 1 task(s) to reviewed.
# → Promoted 1 task(s) to ready.
# → 2 total promotion(s). No tasks blocked.

fakoli-state list --status ready
# TaskID  Title                    Status  Priority  Score  Feature
# ----------------------------------------------------------------------
# T001    Implement uppercase CLI  ready   high      2/4    F001
#
# 1 task(s) listed.
```

Six dimensions: complexity, parallelizability, context load, blast radius, review risk, agent suitability — each 1–5. Scores drive `fakoli-state next` routing and `expand` recommendations.

## Step 6 — Claim and ship the first task

```bash
fakoli-state next
# → T001 — Implement uppercase CLI (ready, priority=high)

fakoli-state claim T001
# → Claim C001 active; branch agent/t001-implement-uppercase-cli created

fakoli-state packet T001
# → Wrote .fakoli-state/packets/T001.md
```

Open `.fakoli-state/packets/T001.md` — it contains the exact intent, acceptance criteria, verification commands, and non-goals the agent (or you) need to execute the work. No issue thread to summarize.

Do the work on the `agent/t001-*` branch, then submit evidence and apply:

```bash
fakoli-state submit T001 \
    --commands "pytest tests/test_cli.py" \
    --files-changed src/upper/cli.py
# → Evidence submitted; task T001 → needs_review.

fakoli-state apply T001 --approve
# → Task T001 applied; event task.applied recorded in events.jsonl.
```

## What just happened?

`state.db` now records `T001=done` and `C001` released. `events.jsonl` has the full audit trail: `project.created`, `prd.parsed`, `prd.reviewed`, `prd.approved`, `task.created`, `task.scored`, `task.status_changed` × N, `claim.created`, `evidence.submitted`, `task.applied`. Replaying that log from an empty database reconstructs `state.db` byte-for-byte — the audit guarantee that makes `.fakoli-state/` safe to back up by copy.

The work packet under `.fakoli-state/packets/T001.md` is the contract that drove the work. For the full picture of how transitions, gates, claims, and the event log fit together, see [`../architecture.md`](../architecture.md).

## Common stumbles

- **"PRD must be in 'reviewed' status to approve"** — you ran `prd review --approve` without first running `prd review`. The two-step pattern is intentional. Run `fakoli-state prd review` first, then `fakoli-state prd review --approve`.
- **"No ready tasks"** — your PRD's `## Tasks` section is empty, or `review tasks` blocked promotion because `**Acceptance criteria:**` or `**Verification:**` is missing on a task. Both fields are required by the `drafted → reviewed` gate. Re-check [`../prd-template.md`](../prd-template.md).
- **"PRD file not found"** — `init` does not create `prd.md`. Author it at `.fakoli-state/prd.md` before running `prd parse`.
- **Claim won't work / git error** — you are not in a git repo, or your working tree is dirty. Run `git init` (if needed), commit or stash pending changes, then retry `fakoli-state claim T001`.
- **`uv` not found** — install it: `pip install uv` or follow [docs.astral.sh/uv](https://docs.astral.sh/uv/).
- **Want to start over?** — `rm -rf .fakoli-state/` and re-run `fakoli-state init`. Or use `fakoli-state init --force` to wipe and re-scaffold in place.

## Where to next

- [Author a real PRD: `authoring-a-prd.md`](authoring-a-prd.md)
- [Full lifecycle deep dive: `claiming-and-shipping-a-task.md`](claiming-and-shipping-a-task.md)
- [Sync to GitHub Issues: `syncing-with-github.md`](syncing-with-github.md)
- [Architecture reference: `../architecture.md`](../architecture.md)
- [CLI reference: `../cli-reference.md`](../cli-reference.md)
- [PRD template and schema: `../prd-template.md`](../prd-template.md)
