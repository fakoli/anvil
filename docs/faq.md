# Frequently asked questions

Practical answers for evaluating, installing, and operating anvil. For
positioning ("why is this different from X"), see the comparison table in the
[README](https://github.com/fakoli/anvil/blob/main/README.md#anvil-vs-an-issue-tracker) and
[`_positioning.md`](_positioning.md). For architectural depth, see
[`architecture.md`](architecture.md); for design rationale, see
[`design.md`](design.md).

---

## Getting started

### Do I need a GitHub account or repository?

No. Canonical state lives locally (SQLite + JSONL) — by default in a
per-project workspace under your **home** directory,
`~/.anvil/workspaces/<dir>-<hash8>/.anvil/`, not inside your repo. Run
`anvil status` to see the exact resolved path, or set
`ANVIL_STATE_LAYOUT=local` to keep state in-repo at `./.anvil/` instead.
GitHub Issues is an opt-in *sync target* via the
bidirectional sync engine — never the source of truth. The CLI works fully
offline; `init`, `plan`, `claim`, `submit`, and `apply` make zero network
calls.

If you do want GitHub Issues as an external projection (so non-developers can
read and comment in a familiar surface), set `GITHUB_REPOSITORY` and either
authenticate `gh` or export `GITHUB_TOKEN`, then run
`anvil sync github`. The mappings flow both ways and conflicts are
labeled rather than auto-resolved.

See [`design.md` § Why local-first](design.md) for the rationale,
[`how-to/state-location.md`](how-to/state-location.md) for exactly where
state lives and how to override it, and
[`how-to/syncing-with-github.md`](how-to/syncing-with-github.md) for the
sync setup walkthrough.

### Do I need an Anthropic, OpenAI, or other LLM API key?

No, not for the core flow. The PRD parser, six-dimension scorer, and
dependency inferencer are deterministic and rule-based — they ship as Python
in `bin/src/anvil/planning/` and run with no network.

The three optional `--use-llm` augmentations do not require an API key
either: by default they run through the Claude Agent SDK subscription
path (no API key — it uses your existing `claude` CLI login), with
`ANTHROPIC_API_KEY`, Bedrock, and OpenAI-compatible custom endpoints as
configurable alternatives. `plan --use-llm` extends short task
descriptions, `score --use-llm` adds a trade-off paragraph to the
explanation, and `expand --use-llm` proposes sub-tasks for tasks with
`complexity >= 4`. The numeric scores, task IDs, dependencies, and status
transitions are never touched by the model — the LLM layer is strictly
additive.

`expand` is the only command that *requires* `--use-llm`; everything else
has a deterministic baseline. For the API-key providers, the default model
is `claude-sonnet-4-6` with ephemeral prompt caching on by default.

See [`llm.md`](llm.md) for the full augmentation contract.

### Which agent runtimes does anvil work with?

Any MCP-compatible runtime. The plugin is built first for Claude Code (where
the skills, agents, and hooks compose natively), but the CLI and FastMCP
stdio server are runtime-neutral. anvil is documented as working with
Claude Code, Codex, Cursor, OpenHands, and Copilot.

Two surfaces are always available:

- **CLI** (`anvil <cmd>`) — runtime-agnostic; any shell-capable agent
  can call it via Bash, and humans use it directly.
- **MCP server** — 35 registered tools over FastMCP stdio; the lean
  execution default serves 24 on the wire (set `ANVIL_MCP_PLANNING=1` to
  add the 11 planning tools). Any MCP client connects; tool responses are
  structured JSON with explicit error envelopes.

When a runtime cannot speak MCP (Cursor has no shell, some Copilot modes
have no stdio), the CLI surface is the fallback. Hooks are Claude
Code-specific because they use the SessionStart / PreToolUse / PostToolUse
contract — agents in other runtimes still get full coordination via
claims + leases, just without the editor-time warning.

See [`architecture.md` § CLI / MCP / hooks surface](architecture.md) and
[`_positioning.md`](_positioning.md).

---

## Day-to-day operation

### My claim lease expired — what happens to my task?

The next mutating CLI or MCP call invokes `detect_and_release_stale()`,
which releases your expired lease with `release_reason="stale"`. The task
returns to `ready` and becomes available to other actors. The audit event
preserves the original claimant, so the history is not lost.

To resume work yourself, run `anvil claim T001` again. If another
actor claimed it in the meantime, the call exits non-zero with
`task already claimed by <actor>`; pass `--force` to take it over (logged
in the audit trail), or run `anvil next` to pick a different
ready task.

Default lease is 240 minutes (configurable in `.anvil/config.yaml`); the lease is extended
by `anvil renew <claim-id>` or by the MCP `renew_claim` tool. Long-running
work should heartbeat every few minutes — see
[`architecture.md` § Concurrency model](architecture.md) for the four
layered mechanisms (SQLite WAL + `BEGIN IMMEDIATE`, leases, heartbeats,
stale reaping).

Full walkthrough: [`how-to/claiming-and-shipping-a-task.md`](how-to/claiming-and-shipping-a-task.md).

### Two agents want to work on the same task — what happens?

First to call `claim` wins. The claim transaction runs inside SQLite's
`BEGIN IMMEDIATE` mode, so concurrent claimers serialize at the database
layer. The losing call exits non-zero with `task already claimed by <actor>`
and prints the active claim id and lease expiry.

The losing actor's options:

- Run `anvil next` to get the next ready task with no conflict.
- Wait for the lease to expire or for the holder to `release` voluntarily.
- Pass `--force` to override (logged as a `claim.force_released` event so
  the takeover is auditable).

A second safety layer catches overlap *before* the claim attempt: if the
target task shares a `ConflictGroup` with an already-claimed task,
`anvil next` will not surface it and `claim` will warn via
pre-claim conflict check. See
[`architecture.md` § Concurrency model](architecture.md).

### Does `--use-llm` cost money?

Yes — Anthropic charges per token. The deterministic path is free and
always available. The LLM layer is opt-in per command.

Cost-shaping defaults are in place: `temperature=0.0` for repeatability,
and prompt caching is on by default (every Anthropic call sends the system
block with `cache_control: {"type": "ephemeral"}`). A typical `score
--use-llm` run against a 20-task batch hits the 5-minute ephemeral cache
on tasks 2–20 and pays only for the cold system block plus per-task user
and output tokens.

Per-call output ceilings are bounded by named constants:
`_SCORE_EXPLAIN_MAX_TOKENS` (300), `_DESCRIPTION_ENRICH_MAX_TOKENS` (400),
and `_EXPAND_MAX_TOKENS` (2000). `expand` is the heaviest call but is
gated on `complexity >= 4` and invoked one task at a time.

If the LLM call fails mid-operation, the engine falls back to the
deterministic baseline and emits a stderr warning — the operation never
aborts mid-batch. See [`llm.md` § Cost notes](llm.md).

### How do I migrate from `agent-*-status.md` markdown files?

There is no shipped migration tool. The recommended path is:

1. `anvil init --name "<project>"` — scaffolds `.anvil/`.
2. Author `.anvil/prd.md` against the schema in
   [`prd-template.md`](prd-template.md). Existing intent / acceptance
   criteria from your markdown status files map cleanly into PRD task
   blocks.
3. `anvil prd parse` then `prd review --approve` to promote the
   PRD to `approved`.
4. `anvil plan` then `score` to materialize tasks and dependencies.
5. Once tasks exist in the database, the old `agent-*-status.md` files can
   be deleted. Their role (per-agent status notes) is replaced by claim
   rows, evidence, and the `events.jsonl` audit log.

An automated importer is not on the roadmap — PRD authoring is a thinking
exercise as much as a data-entry one, and copy-pasting forces the author
to revisit intent. Community contributions for a migrator would be
welcome; open an issue describing your source format.

See [`how-to/getting-started.md`](how-to/getting-started.md) and
[`how-to/authoring-a-prd.md`](how-to/authoring-a-prd.md).

---

## Hooks, storage, and concurrency

### How do I temporarily disable a hook?

The five hooks are wired in
[`hooks/hooks.json`](https://github.com/fakoli/anvil/blob/main/hooks/hooks.json) at the `SessionStart`,
`PreToolUse`, and `PostToolUse` events — including `heartbeat`, which
fires at `PostToolUse` on Edit/Write/NotebookEdit and Bash and renews the
active lease. Every entry's `command` runs the shell-free dispatcher —
`uv run --project bin python -m anvil.cli hook dispatch <name>` — there is
no `.sh` script file for the manifest to resolve.

To disable one hook without uninstalling the plugin, delete or comment out
its block in `hooks.json` and restart your Claude Code session. To turn
every hook off at once, disable or uninstall the anvil plugin through your
harness's plugin configuration; every hook also fast-paths to a silent
no-op if it can't resolve any anvil state for the project.

All five hooks are non-blocking by design: each `anvil hook dispatch ...`
call wraps its body in `try/except Exception: pass` and always exits 0,
regardless of internal failure. A hook that errors out internally already
behaves like a disabled hook: it warns once to stderr and gets out of the
way. See [`design.md` § Why hooks are non-blocking](design.md).

To debug a hook that is misbehaving, run the dispatcher directly with a
sample payload on stdin to inspect its stderr:
`echo '{}' | uv run --project bin python -m anvil.cli hook dispatch <name>`.
A `ANVIL_HOOK_DEBUG=1` env var that redirects hook stderr to
`.anvil/.hook-debug.log` is tracked as a Phase 11 backlog item
([P11-HK-C2](roadmap.md)) but does not ship today.

### Where does my data live, and should I commit it to git?

By default, nowhere near your repo. State lives in a per-project workspace
under your home directory, keyed by the project's canonical git repo:

```text
~/.anvil/workspaces/<dir>-<hash8>/.anvil/
├── config.yaml         # project-level config (sync providers, lease defaults)
├── state.db            # SQLite, WAL mode — the canonical state
├── events.jsonl        # append-only audit log (replay source)
├── prd.md              # PRD source (you edit this)
└── packets/            # generated work packets (per-task markdown / json)
```

`<dir>-<hash8>` is the repo basename plus a short hash of its absolute
path, so two projects that share a folder name never collide, and every git
worktree of the same repo resolves to the same workspace. `anvil status`
prints the exact resolved path on its `Path:` line. See
[`how-to/state-location.md`](how-to/state-location.md) for the full
resolution order (including `ANVIL_ROOT` and `ANVIL_STATE_LAYOUT`).

Because the default layout lives outside the repo, there is nothing under
`.anvil/` for `git clone` to carry — the "commit it to git" question only
applies if you opt back into the old in-repo layout. Two ways to keep your
state durable:

- **Back it up out-of-band (default layout).** Use `anvil backup` /
  `anvil restore` (see [Backup and recovery](#backup-and-recovery) below),
  or `cp -R` the workspace directory yourself. Nothing here touches git.
- **Pin an in-repo state dir and commit it.** Set
  `ANVIL_STATE_LAYOUT=local` so state resolves to `<repo>/.anvil/` again,
  then pick one of two commit policies:
  - **Commit everything.** State, audit log, and packets all survive
    `git clone`. Simplest for solo work or small teams. Beware:
    `state.db` is binary and merge conflicts are unrecoverable manually
    (use replay instead).
  - **Gitignore `state.db`** (and `*.wal`, `*.shm`) but commit
    `events.jsonl`. The replay guarantee means `state.db` is regenerable
    from the event log; this avoids binary merge conflicts while
    preserving audit history across clones.

See [`architecture.md` § Storage layout](architecture.md) and
[`design.md` § Why local-first](design.md).

### Can I inspect state with `sqlite3` or SQLite Browser?

Yes. `state.db` is a standard SQLite file in WAL mode — any SQLite tool
works. Find its path from `anvil status`'s `Path:` line (by default
`~/.anvil/workspaces/<dir>-<hash8>/.anvil/state.db`, not a path inside
your repo):

```bash
STATE_DIR=$(anvil status | grep '^Path:' | awk '{print $2}')
sqlite3 "$STATE_DIR/state.db" .schema
sqlite3 "$STATE_DIR/state.db" "SELECT id, status, title FROM tasks;"
```

The schema is version 8; older databases are auto-upgraded via the
additive v6 → v7 → v8 migration ladder (`anvil migrate state` — dry-run by
default, `--yes` to apply — or automatically on open). Pydantic models in
[`bin/src/anvil/state/models.py`](https://github.com/fakoli/anvil/blob/main/bin/src/anvil/state/models.py)
define every entity; the SQLite implementation lives in
[`bin/src/anvil/state/sqlite.py`](https://github.com/fakoli/anvil/blob/main/bin/src/anvil/state/sqlite.py).

Read-only inspection is safe and concurrent — WAL mode lets readers
proceed without blocking the CLI's writers. Do not edit rows directly:
state mutations should go through the CLI or MCP server so the
corresponding event lands in `events.jsonl` (the replay guarantee depends
on every mutation being represented in the log).

---

## Backup and recovery

### How do I back up `.anvil/`?

Copy the state directory wholesale — find its path with `anvil status`
(`Path:` line; by default `~/.anvil/workspaces/<dir>-<hash8>/.anvil/`, not
a path inside your repo):

```bash
STATE_DIR=$(anvil status | grep '^Path:' | awk '{print $2}')
cp -R "$STATE_DIR" "/backup/location/anvil-$(date +%Y-%m-%d)"
```

That captures `state.db`, `events.jsonl`, `prd.md`, `config.yaml`, and any
generated packets. Restore by copying back. Because `state.db` is in WAL
mode, also capture the `*.wal` and `*.shm` sidecar files if the database
is open at copy time — or shut down active sessions first.

The replay guarantee (see next question) means `events.jsonl` alone is
enough to reconstruct `state.db`, so the audit log is the *minimum* you must
preserve. Because the default layout keeps state outside your repo, `git
commit` doesn't help here unless you've pinned an in-repo state dir with
`ANVIL_STATE_LAYOUT=local` — otherwise back the audit log up the same way
as the rest of the state directory, or use `anvil backup` below.

Shipping today: `anvil backup` pushes `events.jsonl` (and optionally
`state.db`, with `--include-db`) to a configured `durable_store: s3`, and
`anvil restore` pulls it back and rebuilds state via replay. `cp -R`
remains the fully-local flow. A native `anvil snapshot` subcommand (a
local `sqlite3 .backup` wrapper with retention) is on the roadmap — see
[`roadmap.md` § Snapshot / replay](roadmap.md), item P9B-7 — but is not
yet shipped; `anvil backup` / `restore` and the `anvil replay` command
(see the next question) are the supported recovery paths today.

### What if `state.db` gets corrupted?

Restore from a backup of the state directory (safe to `cp -R` — see the
previous question), or rebuild it directly from `events.jsonl` with the
shipped `anvil replay` command. The fastest recovery path today:

```bash
STATE_DIR=$(anvil status | grep '^Path:' | awk '{print $2}')
anvil replay --from-events "$STATE_DIR/events.jsonl" --into /tmp/state.db.rebuilt
mv "$STATE_DIR/state.db" "$STATE_DIR/state.db.broken"   # keep the broken db for forensics
rm -f "$STATE_DIR"/state.db-wal "$STATE_DIR"/state.db-shm
mv /tmp/state.db.rebuilt "$STATE_DIR/state.db"
```

`anvil replay --from-events <path> --into <path>` reads every event from
the source JSONL and replays it into a fresh SQLite database at `--into`
(deleting and rebuilding that target from scratch). It refuses to target
the live `state.db` directly, to prevent accidental data loss — which is
why the example above rebuilds into a scratch path and swaps it in after.

The replay guarantee is the central audit property of the engine: replaying
every event from `events.jsonl` against an empty database reconstructs
canonical SQLite state exactly. That property is what makes `events.jsonl`
the *minimum* you must preserve. Since the default layout keeps it outside
your repo, back it up the same way as the rest of the state directory (or
push it with `anvil backup`, see above). If you've pinned an in-repo state
dir with `ANVIL_STATE_LAYOUT=local`, committing `events.jsonl` to git also
gives you a distributed audit log recoverable from any clone even if every
local `state.db` is lost.

Event ids are assigned inside the mutating transaction, not before it, so
the JSONL ordering is consistent with the SQLite commit order. See
[`architecture.md` § Event log and JSONL replay](architecture.md).

---

## Roadmap and contributing

### When will Linear, Monday, or Jira support land?

None yet — `github_issues` is the only sync provider that ships today.
On the roadmap, per [`roadmap.md`](roadmap.md):

- `LinearIssuesProvider` (GraphQL transport, item P9B-1) and
  `MondayBoardsProvider` (REST + JSON with people-columns, item P9B-2).
  Both are OPEN and in development. Webhook-based sync (P9B-5) is
  SPEC-FIRST alongside them.
- `JiraIssuesProvider` (per-project workflow discovery, P9B-3) and
  `GitHubProjectsProvider` (Projects v2 board surface, P9B-4). Both OPEN,
  as follow-on work after Linear/Monday land.

The `SyncProvider` Protocol has already shipped and is deliberately
registry-driven so contributors can add providers without engine
changes. If you want to add one now rather than wait, see the next
question.

### How do I write my own sync provider?

Implement the `SyncProvider` Protocol from
[`bin/src/anvil/sync/provider.py`](https://github.com/fakoli/anvil/blob/main/bin/src/anvil/sync/provider.py)
and register it in
[`registry.py`](https://github.com/fakoli/anvil/blob/main/bin/src/anvil/sync/registry.py). The
`GitHubIssuesProvider` at
[`sync/providers/github_issues.py`](https://github.com/fakoli/anvil/blob/main/bin/src/anvil/sync/providers/github_issues.py)
is the reference implementation — read it alongside the contributor
guide at [`sync-providers.md`](sync-providers.md), which walks through
the Linear case step by step.

Per-provider acceptance criteria (from `roadmap.md` § Next → Theme: Sync
providers): provider module + transport (GraphQL or REST) + full-lifecycle
respx tests + live nightly workflow gated on the provider's API key secret +
`anvil sync <provider_id> --health` works.

Provider config schemas in `config.yaml` (item P9B-9) are co-required
with the first new provider — that work is SPEC-FIRST and tracked
in the same Next bucket.

### How do I contribute?

Open a pull request against
[github.com/fakoli/anvil](https://github.com/fakoli/anvil).
No dedicated `CONTRIBUTING.md` currently ships; use the README's "Status"
section and the
[`roadmap.md`](roadmap.md) item taxonomy (Phase 11 backlog items
prefixed `P11-XX-XN`, Phase 9 carry-forward items prefixed `P9B-N`).

Three contribution shapes most appreciated right now:

- **Sync providers.** Linear / Monday providers are on the roadmap but
  community implementations are welcome. Follow
  [`sync-providers.md`](sync-providers.md).
- **Phase 11 backlog batches.** 56 SHOULD FIX / CONSIDER / NIT items
  tracked in [`phase-11-backlog.md`](archive/phase-11-backlog.md); the
  cross-cutting themes in `roadmap.md` indicate which items batch
  cleanly.
- **Test coverage.** Keep the local pytest suite and live-test documentation
  current when adding a surface or provider; [`live-tests.md`](live-tests.md)
  describes the nightly workflow.

For new architectural choices (a second backend, a daemon, a webhook
listener), write a SPEC-FIRST design doc under `docs/specs/` before
opening a PR — the SPEC-FIRST roadmap items are the precedents to
mirror.
