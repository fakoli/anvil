# Glossary

Short definitions for the vocabulary anvil's docs use without re-explaining
it every time. Each entry links to the doc that covers the term in depth.

### claim

An exclusive lease on one task plus (when git is available) a branch to do
the work on, recorded atomically in SQLite via a `claim.created` event.
Claiming moves a task from `ready` to `claimed`, runs a pre-claim conflict
check against the `expected_files` of every other actor's active claim, and
records the claiming actor for the audit trail. See
[Claiming and shipping a task](how-to/claiming-and-shipping-a-task.md).

### evidence buffer

`.anvil/.evidence-buffer/` is a transient directory the `capture-evidence.sh`
hook appends to whenever a recognized verification command (`pytest`,
`ruff check`, `mypy`, `npm test`, `cargo test`, `bun test`) runs while a claim
is active. `anvil submit` reads the buffer file for the active claim and
folds matching records into the durable `evidence.submitted` event, but it
never deletes, truncates, or rotates the buffer file itself. See
[Evidence buffer](evidence-buffer.md).

### evidence gate

The check `anvil apply` (and the summary `anvil submit` prints immediately
after submitting) runs against a task's `verification.required_evidence`,
matching each required item — test output, PR link, screenshots, files
changed — to a structured field on the submitted `Evidence` row by substring
rule. A task with an unmet item reports `Evidence gate: INCOMPLETE` and
`apply --approve --strict` refuses to accept it. See
[Claiming and shipping a task — how the evidence gate matches](how-to/claiming-and-shipping-a-task.md#how-the-evidence-gate-matches).

### harness

Any coding-agent runtime or IDE integration that drives anvil through the
CLI or the MCP server — Claude Code, Codex, Cursor, OpenClaw, and others.
`anvil install <harness>` wires up the natively-supported ones end-to-end;
every other harness gets MCP-only best-effort configuration via
`anvil mcp-config <harness>`. See
[Using Anvil on any coding harness](how-to/using-anvil-on-any-harness.md).

### lease

The time-boxed exclusivity window on a claim (`lease_expires_at`), renewed
with `anvil renew` before it lapses. It defaults to 240 minutes unless
overridden by `--lease` on `claim`/`renew`, `default_lease_minutes` in
project or global `config.yaml`, or the built-in default (in that order of
precedence). When a lease passes its expiry, the next mutating CLI or MCP
call by any actor reaps it — the claim flips to `stale` and the task returns
to `ready`. See
[Claiming and shipping a task — renew the lease](how-to/claiming-and-shipping-a-task.md).

### loop (the anvil loop)

The pattern of repeatedly running `anvil next -q` to pick the next
claimable task, then driving the same governed body — `claim` → `packet` →
do the work → `submit` → `apply` — for it, until the queue empties (exit
code `3`). Any runtime can drive the loop — a POSIX shell `while`, a Claude
Code `/loop`, a scheduled Codex automation, or `anvil run-workflow` for
declarative, non-PRD-derived flows — because the leasing and state live in
anvil, not the runtime. See
[Drive the anvil loop](how-to/drive-the-anvil-loop.md).

### packet

The complete context one agent needs to execute one task, and nothing
else — goal, acceptance criteria, scope (likely files), dependencies,
constraints, verification commands, and (when held) the active claim's
lease and branch. `anvil packet <task-id>` renders it from canonical state
to `.anvil/packets/<task-id>.md` (or `.json` with `--format json`). See
[Claiming and shipping a task — get the work packet](how-to/claiming-and-shipping-a-task.md).

### PRD

The markdown source of truth — `prd.md` for the default PRD, or
`prds/<prd_id>.md` for a named release — that `anvil prd parse` reads
deterministically (no LLM) into `Requirement`, `Feature`, and `Task` rows.
A project can hold one `default` PRD plus multiple named release PRDs,
all persisted in the same `state.db` / `events.jsonl`. See
[PRD template](prd-template.md).

### requirement / feature / task hierarchy

anvil's three-level plan structure: a **requirement** (`RNNN`) states what
the system must do; a **feature** (`FNNN`) groups requirements into a
shippable unit via its `**Requirements:**` field; a **task** (`TNNN`, or
`TNNN.M` for a subtask created by `anvil expand`) is the concrete unit of
work a claim executes, linked to its feature via `**Feature:**`. See
[PRD template — ID conventions](prd-template.md#id-conventions).

### supersede (PRD revision lineage)

What happens when `anvil prd parse` re-parses an already-parsed PRD: instead
of deleting rows, it emits a non-destructive `prd.revised` event that marks
changed or removed requirements' `revision_superseded` column, keeping the
prior rows for lineage (`include_superseded=True` returns the full
history). `anvil plan` then fails loudly rather than pruning a `claimed` or
`in_progress` task that referenced a superseded requirement. See
[PRD template — parser behavior at a glance](prd-template.md#parser-behavior-at-a-glance).

### workspace

The directory holding one project's canonical state — `state.db`,
`events.jsonl`, `config.yaml`, `packets/`, and the evidence buffer. By
default this is a per-project HOME workspace at
`~/.anvil/workspaces/<key>/.anvil/`, keyed by the repo's main git worktree
so every worktree of a repo shares one `state.db`. Set
`ANVIL_STATE_LAYOUT=local` to opt back into the legacy in-repo `.anvil/`
layout, or `ANVIL_ROOT=<dir>` to pin state to an exact path. See
[Where anvil stores its state](how-to/state-location.md).
