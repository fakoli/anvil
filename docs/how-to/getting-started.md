# Getting started with anvil

> anvil is a local-first, backend-neutral project-state layer for humans and AI coding agents — the durable record of every requirement, task, claim, and piece of evidence in your project, stored in SQLite under `.anvil/` and exposed through a CLI and an MCP server. This walkthrough takes you from an empty directory to a shipped task in about five minutes.

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
/plugin install anvil
```

The install registers four hooks, wires the MCP server, and makes the six plugin agents discoverable at next session start. Verify with:

```bash
anvil --version
# → anvil 0.1.2
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

  .anvil/config.yaml
  .anvil/state.db
  .anvil/events.jsonl
  .anvil/packets/

Next step: author your PRD at .anvil/prd.md, then run `anvil prd parse`.
```

`prd.md` is intentionally NOT auto-created — you author it next against the template.

## Step 3 — Author your PRD

Open `.anvil/prd.md` in your editor and paste a minimal valid PRD. The required sections are `# Project:`, `## Summary`, `## Goals`, `## Requirements`, plus at least one task in `## Tasks` to actually have something to claim. Any task that declares a `**Feature:** F00N` line must have a matching `### F00N:` block in `## Features`. Full schema in [`../prd-template.md`](../prd-template.md).

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
# → PRD source: .anvil/prd.md

anvil prd review            # draft → reviewed
# → PRD reviewed by 'human'.
# → Run `anvil prd review --approve` to approve.

anvil prd review --approve  # reviewed → approved
# → PRD approved by 'human'.
```

The two-step gate is deliberate. `prd review` records that a human has read the PRD; `prd review --approve` unlocks task claiming. The claims manager refuses to claim any task while the PRD is in `draft` or `reviewed` status — only `approved` (or explicitly `reviewed` for the readiness gate) lets work begin.

## Step 5 — Generate and score tasks

```bash
anvil plan
# → Planned 1 features, 1 tasks.

anvil score
# TaskID       Complexity Parallel CtxLoad Blast Review Agent
# ---------------------------------------------------------------
# T001                  2        4       2     2      2     4
#
# Scored 1 task(s).

anvil review tasks
# → Promoted 1 task(s) to reviewed.
# → Promoted 1 task(s) to ready.
# → 2 total promotion(s). No tasks blocked.

anvil list --status ready
# TaskID  Title                    Status  Priority  Score  Feature
# ----------------------------------------------------------------------
# T001    Implement uppercase CLI  ready   high      2/4    F001
#
# 1 task(s) listed.
```

Six dimensions: complexity, parallelizability, context load, blast radius, review risk, agent suitability — each 1–5. Scores drive `anvil next` routing and `expand` recommendations.

## Step 6 — Claim and ship the first task

```bash
anvil next
# → T001 — Implement uppercase CLI (ready, priority=high)

anvil claim T001
# → Claim C001 active; branch agent/t001-implement-uppercase-cli created

anvil packet T001
# → Wrote .anvil/packets/T001.md
```

Open `.anvil/packets/T001.md` — it contains the exact intent, acceptance criteria, verification commands, and non-goals the agent (or you) need to execute the work. No issue thread to summarize.

Do the work on the `agent/t001-*` branch, then submit evidence and apply:

```bash
anvil submit T001 \
    --commands "pytest tests/test_cli.py" \
    --files-changed src/upper/cli.py
# → Evidence submitted; task T001 → needs_review.

anvil apply T001 --approve
# → Task T001 applied; event task.applied recorded in events.jsonl.
```

## What just happened?

`state.db` now records `T001=done` and `C001` released. `events.jsonl` has the full audit trail: `project.created`, `prd.parsed`, `prd.reviewed`, `prd.approved`, `task.created`, `task.scored`, `task.status_changed` × N, `claim.created`, `evidence.submitted`, `task.applied`. Replaying that log from an empty database reconstructs `state.db` byte-for-byte — the audit guarantee that makes `.anvil/` safe to back up by copy.

The work packet under `.anvil/packets/T001.md` is the contract that drove the work. For the full picture of how transitions, gates, claims, and the event log fit together, see [`../architecture.md`](../architecture.md).

## Common stumbles

- **"PRD must be in 'reviewed' status to approve"** — you ran `prd review --approve` without first running `prd review`. The two-step pattern is intentional. Run `anvil prd review` first, then `anvil prd review --approve`.
- **"No ready tasks"** — your PRD's `## Tasks` section is empty, or `review tasks` blocked promotion because `**Acceptance criteria:**` or `**Verification:**` is missing on a task. Both fields are required by the `drafted → reviewed` gate. Re-check [`../prd-template.md`](../prd-template.md).
- **"PRD file not found"** — `init` does not create `prd.md`. Author it at `.anvil/prd.md` before running `prd parse`.
- **Claim won't work / git error** — you are not in a git repo, or your working tree is dirty. Run `git init` (if needed), commit or stash pending changes, then retry `anvil claim T001`.
- **`uv` not found** — install it: `pip install uv` or follow [docs.astral.sh/uv](https://docs.astral.sh/uv/).
- **Want to start over?** — `rm -rf .anvil/` and re-run `anvil init`. Or use `anvil init --force` to wipe and re-scaffold in place.

## Where to next

- [Author a real PRD: `authoring-a-prd.md`](authoring-a-prd.md)
- [Full lifecycle deep dive: `claiming-and-shipping-a-task.md`](claiming-and-shipping-a-task.md)
- [Sync to GitHub Issues: `syncing-with-github.md`](syncing-with-github.md)
- [Architecture reference: `../architecture.md`](../architecture.md)
- [CLI reference: `../cli-reference.md`](../cli-reference.md)
- [PRD template and schema: `../prd-template.md`](../prd-template.md)
