# CLI reference

> **Audience:** users running `anvil` day-to-day — flags, exit codes, and command behavior.

> Single-page reference for the `anvil` CLI: 33 top-level commands plus 16
> subcommands grouped under six sub-apps (`prd`, `review`, `hook`, `sync`,
> `migrate`, `proof`) — 49 commands in total. The most-used lifecycle
> commands get full Synopsis/Flags/Exit-codes treatment below;
> [Additional commands (index)](#additional-commands) covers the rest with a
> one-line entry each. For narrative context on common workflows, see
> [`how-to/getting-started.md`](how-to/getting-started.md),
> [`how-to/authoring-a-prd.md`](how-to/authoring-a-prd.md),
> [`how-to/claiming-and-shipping-a-task.md`](how-to/claiming-and-shipping-a-task.md),
> and [`how-to/syncing-with-github.md`](how-to/syncing-with-github.md).

## Table of contents

- [Conventions](#conventions)
- [Global flags](#global-flags)
- Project lifecycle
  - [`anvil init`](#init)
  - [`anvil status`](#status)
  - [`anvil scan`](#scan)
- PRD authoring
  - [`anvil prd parse`](#prd-parse)
  - [`anvil prd review`](#prd-review)
- Planning
  - [`anvil plan`](#plan)
  - [`anvil score`](#score)
  - [`anvil expand`](#expand)
  - [`anvil review tasks`](#review-tasks)
  - [`anvil list`](#list)
  - [`anvil show`](#show)
- Claims and work
  - [`anvil next`](#next)
  - [`anvil claim`](#claim)
  - [`anvil release`](#release)
  - [`anvil renew`](#renew)
  - [`anvil packet`](#packet)
- Submit and apply
  - [`anvil submit`](#submit)
  - [`anvil apply`](#apply)
- Sync
  - [`anvil sync`](#sync)
  - [`anvil sync github`](#sync-github)
  - [`anvil sync provider`](#sync-provider)
- Cross-harness
  - [`anvil mcp-config`](#mcp-config)
- Hook subcommands (internal)
  - [`anvil hook check-claim`](#hook-check-claim)
  - [`anvil hook record-file-change`](#hook-record-file-change)
  - [`anvil hook capture-evidence`](#hook-capture-evidence)
- [Additional commands (index)](#additional-commands)

---

## Conventions

- Every command supports `--help`. Run `anvil <command> --help` to see
  the live Typer-generated output.
- Every command that needs a project directory accepts a hidden `--cwd PATH`
  override — it points at your **project** directory, from which anvil derives
  the state location. Without it, the command resolves the project from the
  current working directory.
- **There is no `--workspace` flag.** In the default HOME-workspace layout,
  state lives at `~/.anvil/workspaces/<key>/.anvil/` keyed by the project — you
  select it *by project* (run inside the project, or pass `--cwd <project-dir>`),
  never by pointing a flag at the workspace path directly. `anvil status` echoes
  the resolved `.anvil` directory on its `Path:` line, so `anvil status --cwd
  <project>` is how you inspect a specific project's state. (Passing a workspace
  path where a project is expected — e.g. `anvil status --workspace …` — fails
  with `No such option '--workspace'`.)
- Mutating commands write to `state.db` (SQLite) **and** append a JSON line to
  `events.jsonl` in the same transaction. The event log is the source of
  truth; `state.db` is a derived projection that can be rebuilt by replaying
  `events.jsonl`. See [`architecture.md`](architecture.md) for the replay
  contract.
- Actor identity for claims, submissions, and reviews defaults to `$USER`,
  then `agent` (or `human` for `apply`). Override with `--actor`,
  `--reviewer`, etc.
- **`--json`** is near-universal: almost every command accepts it and, when
  passed, emits exactly one line of JSON to stdout —
  `{"ok": true, "command": "<name>", "data": {...}}` on success or
  `{"ok": false, "command": "<name>", "error": {"code": "...", "message":
  "..."}}` on failure (printed to stdout even on failure, so a consumer
  piping stdout always gets parseable JSON) — with no Rich tables, color, or
  warnings mixed in, so output is safe to pipe into `jq` / `json.load`.
- **`--prd` / `ANVIL_PRD`** scope a command to one PRD partition on a
  multi-PRD project (most mutating PRD/planning/claim commands accept it —
  e.g. `prd review`, `plan`, `score`, `claim`, `next`). Precedence: the
  `--prd` flag > the `ANVIL_PRD` environment variable > the project's single
  PRD or marked default PRD. With several non-default PRDs and neither
  selecting one, the command errors rather than guessing. Single-PRD
  projects can omit it entirely for unchanged behaviour.
- Exit codes (consistent across the CLI):
  - `0` — success (including informational no-op states like "no tasks to
    score" or `status --hook-format` on an uninitialised project).
  - `1` — state / gate / validation error (task not found, gate failed,
    `--use-llm` with an explicitly-pinned provider that can't be built, parse
    errors, mutually exclusive flag conflicts, missing required `--reason`,
    etc.).
  - `2` — meaning is command-specific: for `sync` / `sync github` /
    `sync provider` it means one or more tasks parked awaiting
    `manual_merge` resolution; a handful of other commands (`mcp-config`,
    `install`, `deps`, the native-harness gates) reuse `2` for their own
    bad-request / block outcomes — see each command's own Exit codes.

### Global-config layer { #global-config-layer }

Configuration is resolved from up to four layers, lowest precedence to
highest:

1. **Built-in defaults** — the dataclass defaults baked into the engine (e.g.
   a 240-minute lease).
2. **Global config** — `~/.config/anvil/config.yaml`. User-wide
   defaults that every project on the machine inherits, so settings need not
   be copied into each project. The location honours `$XDG_CONFIG_HOME`
   (`$XDG_CONFIG_HOME/anvil/config.yaml`) and can be pinned outright
   with the `ANVIL_GLOBAL_CONFIG` environment variable. This file is
   optional — most projects never need one.
3. **Project config** — `.anvil/config.yaml`. Per-project overrides.
   Any key set here wins over the same key in the global config. The project
   config is the one that must carry the required `project_name` /
   `project_id` (though the global layer *may* supply a default
   `project_name`). `db_path` / `events_path` always resolve next to the
   project config, never under `~/.config`.
4. **Explicit CLI flag** — e.g. `claim --lease 15`. Always wins.

So a global default lease of `45` is overridden to `30` by a project
`config.yaml` and to `15` by `claim --lease 15`. The same precedence applies
to `ANVIL_ROOT` (which selects *which* project's `.anvil/` the
merge reads) and every other config key. A broken or missing global config
never blocks a command: a missing/empty file means "no global defaults", and
a malformed one surfaces a warning while the command proceeds on the
remaining layers.

## Global flags

These appear on the root `anvil` invocation, before any subcommand.

- `--version`, `-V` — print the version (e.g. `anvil 0.5.0 (schema 9)`) and exit.
- `--help` — show root help and exit. Listing the registered commands and
  sub-apps; equivalent to `anvil` with no arguments
  (`no_args_is_help=True`).

---

## Project lifecycle

### `anvil init` { #init }

**Synopsis:** Scaffold a `.anvil/` directory in the current working
directory. Creates `config.yaml`, `state.db` (SQLite, with the canonical
schema), an empty append-only `events.jsonl`, and an empty `packets/`
subdirectory. Emits `project.created` and `state.initialized` events to seed
the project row.

**Flags:**

- `--name TEXT` *(optional)* — human-readable project name. Defaults to the
  basename of the current directory.
- `--id TEXT` *(optional)* — project identifier slug (e.g. `my-project`).
  Defaults to a slug derived from `--name`.
- `--force` *(flag)* — overwrite an existing `.anvil/` directory.
  Wipes `state.db` (including the `-wal` / `-shm` sidecars), `events.jsonl`,
  and `config.yaml`. Preserves `packets/` and `snapshots/` (user-generated).
- `--with-sample` *(flag)* — seed a runnable toy project (sample `prd.md` +
  parsed/planned/scored task graph) so `anvil next` works immediately.
- `--from-repo` *(flag)* — brownfield ingest: after scaffolding, run
  [`anvil scan`](#scan) on the existing working tree to persist a
  re-scannable codebase model, write a draft `prd.md`, and seed an initial
  feature/task graph offline. Mutually exclusive with `--with-sample`.

**Exit codes:**

- `0` — initialisation succeeded.
- `1` — `.anvil/` already exists and `--force` was not passed; or the
  current directory is the anvil plugin root itself (init refuses to
  scaffold inside the plugin).

**Example:**

```bash
cd ~/projects/acme-api
anvil init --name "Acme API"
```

**See also:** [`how-to/getting-started.md`](how-to/getting-started.md) for the
end-to-end first-project walkthrough; [`anvil status`](#status) to
inspect the result.

### `anvil status` { #status }

**Synopsis:** Show the current `anvil` summary for this project.
Default output is a human-readable multi-line block (project name, id, path,
initialised-at, PRD status, task counts by status, active claim count, sync
configuration). Pass `--hook-format` for the single-line compact format
consumed by the SessionStart `detect-state.sh` hook.

**Flags:**

- `--hook-format` *(flag)* — emit a single compact line for hook consumption
  (e.g. `active-claims:0 ready-tasks:5 blockers:0 prd-status:approved`).
  Exits 0 even when `anvil` is not initialised — hooks must never
  fail the session.
- `--cwd PATH` *(hidden)* — project directory to inspect. Defaults to cwd.

**Exit codes:**

- `0` — status printed successfully, **or** `--hook-format` was used on an
  uninitialised project (prints the literal string `uninitialized`).
- `1` — `.anvil/` does not exist and `--hook-format` was *not* passed.

**Example:**

```bash
anvil status
anvil status --hook-format     # for SessionStart hooks
```

**See also:** [`anvil init`](#init) to create the directory;
[`anvil list`](#list) for the per-task view.

### `anvil scan` { #scan }

**Synopsis:** Brownfield ingest of an existing repository. Walks the working
tree (preferring `git ls-files`, which honours `.gitignore`; falling back to a
pruned `os.walk`), persists a re-scannable **codebase model** in its own
`.anvil/scan.db` (kept separate from the event-sourced `state.db` so
replay is never touched), and — on the **first** scan of a project with no PRD
yet — synthesises a draft `prd.md` plus an initial feature/task graph by driving
the same offline parse → plan → score → review pipeline that
`init --with-sample` uses. Re-running `scan` reconciles against the persisted
model and reports the **delta** (added / removed / changed files) instead of
overwriting the seeded graph.

**Flags:**

- `--json` *(flag)* — emit the standard single-line envelope. `data` carries
  `files_scanned`, `components`, `languages`, `first_scan`, `delta`
  (`added` / `removed` / `changed` / `unchanged_count`), and `seeded`
  (feature/task/ready counts on the run that seeded, else `null`).
- `--force` *(flag)* — re-seed the draft PRD and task graph even when a PRD
  already exists. Without it, a re-scan never clobbers an authored PRD.
- `--cwd PATH` *(hidden)* — project directory. Defaults to cwd.

**Exit codes:**

- `0` — scan completed (first-seed or delta report).
- `1` — `.anvil/` does not exist (run `init` / `init --from-repo`
  first), or `ANVIL_ROOT` is set but invalid.

**Example:**

```bash
anvil init --from-repo     # scaffold + first scan in one step
# ... edit code ...
anvil scan                 # refresh the model, see what changed
anvil scan --json | jq .data.delta
```

**See also:** [`anvil init`](#init) (`--from-repo` runs scan for you);
`anvil drift` for intent↔state↔fs divergence on an active
project.

---

## PRD authoring

### `anvil prd parse` { #prd-parse }

**Synopsis:** Parse `.anvil/prd.md` (or `--file PATH`) and store the
result as a `prd.parsed` event. Calls the template parser, validates the
required sections, and persists the full PRD payload (summary, goals,
non-goals, requirements, acceptance criteria, risks, open questions).

**Flags:**

- `--file PATH` *(optional)* — path to the PRD markdown file. Defaults to
  `.anvil/prd.md` in the current project directory.
- `--cwd PATH` *(hidden)* — project directory. Defaults to cwd.

**Exit codes:**

- `0` — PRD parsed and `prd.parsed` event recorded. Prints the count of
  requirements, features, and tasks found.
- `1` — PRD file not found, unreadable, or contains parse errors (every error
  is printed to stderr with `[section:line] message` formatting).

**Example:**

```bash
anvil prd parse
anvil prd parse --file ./drafts/v2-prd.md
```

**See also:** [`how-to/authoring-a-prd.md`](how-to/authoring-a-prd.md);
[`docs/prd-template.md`](prd-template.md) for the required section structure;
[`anvil prd review`](#prd-review) for the next step.

### `anvil prd review` { #prd-review }

**Synopsis:** Transition the PRD through the review lifecycle. Without
`--approve`: `draft` → `reviewed` (emits `prd.reviewed`). With `--approve`:
`reviewed` → `approved` (emits `prd.approved`).

**Flags:**

- `--approve` *(flag)* — approve the PRD (transition `reviewed` → `approved`).
  Without this flag the command performs the `draft` → `reviewed` transition.
- `--reviewer TEXT` *(default: `human`)* — identity of the reviewer recorded
  in the event payload.
- `--notes TEXT` *(optional)* — optional review notes (recorded on the
  `prd.reviewed` event).
- `--cwd PATH` *(hidden)* — project directory. Defaults to cwd.

**Exit codes:**

- `0` — transition recorded successfully.
- `1` — no PRD in state (run `prd parse` first); or the PRD is in the wrong
  status for the requested transition (e.g. `--approve` invoked while the
  PRD is still `draft`).

**Example:**

```bash
anvil prd review --reviewer "alex" --notes "scope looks good"
anvil prd review --approve --reviewer "alex"
```

**See also:** [`anvil prd parse`](#prd-parse);
[`anvil plan`](#plan) for the next step.

---

## Planning

### `anvil plan` { #plan }

**Synopsis:** Generate features and tasks from the parsed PRD. Re-reads
`prd.md`, emits `feature.created` and `task.created` events for each feature
and task found, runs dependency and conflict-group inference, then promotes
all freshly-`proposed` tasks to `drafted`. Idempotent — re-running does not
duplicate tasks (INSERT OR REPLACE semantics) and never regresses status of
tasks that have already advanced past `drafted`.

**Flags:**

- `--use-llm` *(flag)* — augment planning with an LLM. Defaults to your Claude
  subscription via the Agent SDK (no API key; needs the `claude` CLI on PATH);
  pin `anthropic` / `bedrock` / `custom` via `llm_provider:` in
  `.anvil/config.yaml`. Deterministic output is always produced first; LLM
  enrichment is additive (it enriches task descriptions shorter than the
  50-character threshold). LLM failures fall back to the deterministic
  description with a stderr warning — `plan` never aborts on LLM failure.
- `--model NAME` *(default: unset)* — override the LLM model for this run
  (wins over `llm_model` / `llm_tier`); applies to both `--use-llm`
  augmentation and the no-tasks backstop. For agent-sdk a CLI name like
  `sonnet`/`opus` or a full id; for anthropic/bedrock a model id; for custom
  the route name your endpoint serves.
- `--prd TEXT` *(optional)* — named PRD to plan (multi-PRD). Reads
  `.anvil/prds/<id>.md` and scopes feature/task creation, orphan-prune,
  dependency inference, and `proposed` → `drafted` promotion to that PRD's
  partition (conflict-group inference still spans all PRDs). Omit for the
  default PRD (`.anvil/prd.md`).
- `--no-llm` *(flag)* — disable the LLM task-generation backstop. When the
  PRD has features + requirements but no `## Tasks` section, the default
  behaviour calls the LLM to generate tasks and append them to `prd.md`;
  with `--no-llm` the command fails loudly instead so tasks can be authored
  manually.
- `--prune-force` *(flag)* — force-delete orphan tasks (removed from
  `prd.md`) that have already advanced past `ready` status (claimed /
  in_progress / needs_review / etc.). Without it, such orphans make `plan`
  fail loudly so the user can release/complete them first; events/evidence/
  reviews are preserved as audit history either way — only the task row is
  deleted.
- `--cwd PATH` *(hidden)* — project directory. Defaults to cwd.

**Exit codes:**

- `0` — planning succeeded. Prints `Planned N features, M tasks.` and any
  detected conflict-group count.
- `1` — `prd.md` not found or unreadable; an explicitly-pinned provider
  (`llm_provider: bedrock`/`custom`) could not be built (missing extra or
  config); the LLM task-generation backstop failed; or an orphan task past
  `ready` status was found without `--prune-force`. The default agent-sdk
  provider needs no key, so a missing `ANTHROPIC_API_KEY` is *not* an error.

**Example:**

```bash
anvil plan
anvil plan --use-llm        # default: your Claude subscription (no API key)
```

**See also:** [`anvil score`](#score) and
[`anvil review tasks`](#review-tasks) for the next steps in the
planning lifecycle; [`docs/llm.md`](llm.md) for the LLM augmentation
contract.

### `anvil score` { #score }

**Synopsis:** Score tasks across six rule-based dimensions (complexity,
parallelizability, context_load, blast_radius, review_risk,
agent_suitability). Without a task id: scores every task whose scores are
incomplete. With a task id: scores that single task. Emits one `task.scored`
event per task and prints a summary table.

**Positional arguments:**

- `TASK_ID` *(optional)* — task id to score. Omit to score all tasks whose
  scores are currently incomplete.

**Flags:**

- `--use-llm` *(flag)* — append the rule-based explanation with a 1-3
  sentence trade-off summary from the LLM. Defaults to your Claude
  subscription via the Agent SDK (no API key; needs the `claude` CLI); pin a
  different provider via `llm_provider:`. The numeric scores themselves are
  never modified by the LLM.
- `--model NAME` *(default: unset)* — override the LLM model for this run
  (wins over `llm_model` / `llm_tier`). See [`anvil plan`](#plan) for the
  per-provider name conventions.
- `--cwd PATH` *(hidden)* — project directory. Defaults to cwd.

**Exit codes:**

- `0` — scoring completed (including the "no tasks require scoring" no-op).
- `1` — specified `TASK_ID` not found; or an explicitly-pinned
  (`bedrock`/`custom`) provider could not be built. The default agent-sdk
  provider needs no key.

**Example:**

```bash
anvil score                # score every unscored task
anvil score T003
anvil score T003 --use-llm
```

**See also:** [`anvil show`](#show) for the per-task scores breakdown;
[`anvil expand`](#expand) to decompose high-complexity tasks.

### `anvil expand` { #expand }

**Synopsis:** Expand a high-complexity task into 2-5 sub-task proposals via
the LLM. **Requires `--use-llm`** — the deterministic engine never invents
sub-tasks; the deterministic path is manual authoring of `T001.1`, `T001.2`
entries in `prd.md`. Only tasks with `complexity >= 4` are decomposed;
lower-complexity tasks return no proposals. This command does **not** mutate
state — proposals are printed for the human to paste into `prd.md`.

**Positional arguments:**

- `TASK_ID` *(required)* — task id to expand into subtasks.

**Flags:**

- `--use-llm` *(required)* — without this flag, `expand` exits 1 with the
  message pointing at the manual-authoring fallback. With it, the LLM is
  asked for 2-5 independently-claimable sub-task proposals. Defaults to your
  Claude subscription via the Agent SDK (no API key; needs the `claude` CLI);
  pin a provider via `llm_provider:`.
- `--model NAME` *(default: unset)* — override the LLM model for this run
  (wins over `llm_model` / `llm_tier`). See [`anvil plan`](#plan) for the
  per-provider name conventions.
- `--format {text,prd}` *(default: `text`)* — `text` prints a human-readable
  per-subtask block; `prd` renders markdown blocks matching
  [`docs/prd-template.md`](prd-template.md) — paste-ready into the `## Tasks`
  section of `.anvil/prd.md`, inheriting the parent's `feature_id`
  and priority.
- `--cwd PATH` *(hidden)* — project directory. Defaults to cwd.

**Exit codes:**

- `0` — proposals printed (or the task is below the complexity threshold —
  this is a non-error no-op).
- `1` — `--use-llm` was not passed; or `--format` was not one of
  `text` / `prd`; or `TASK_ID` not found; or an explicitly-pinned
  (`bedrock`/`custom`) provider could not be built.

**Example:**

```bash
anvil expand T012 --use-llm
anvil expand T012 --use-llm --format prd >> .anvil/prd.md
```

**See also:** [`anvil score`](#score) (run first to populate the
complexity score); [`docs/llm.md`](llm.md);
[`anvil prd parse`](#prd-parse) to re-parse after pasting blocks.

### `anvil review tasks` { #review-tasks }

**Synopsis:** Promote tasks through the review lifecycle in two stages:
`drafted` → `reviewed`, then `reviewed` → `ready`. The `drafted` → `reviewed`
gate requires non-empty `acceptance_criteria` AND non-empty
`verification.commands`. Prints a summary of how many tasks were promoted at
each stage and lists any blocked tasks with the gate-failure reason.

**Flags:**

- `--cwd PATH` *(hidden)* — project directory. Defaults to cwd.

**Exit codes:**

- `0` — pass completed. Tasks that failed the gate are listed in the output
  but do not change the exit code (this is a batch operation; per-task
  failures are informational).

**Example:**

```bash
anvil review tasks
```

**See also:** [`anvil list`](#list) to inspect the current statuses;
[`anvil plan`](#plan) for the prior step.

### `anvil list` { #list }

**Synopsis:** List tasks with optional status, feature, and type filters.
Prints a table with columns: TaskID, Title, Status, Priority, Type, Score
(`complexity/agent_suitability` or `unscored`), Feature.

**Flags:**

- `--status TEXT` *(optional)* — filter by task status (e.g. `ready`,
  `drafted`, `reviewed`, `in_progress`, `needs_review`, `done`).
- `--open` *(optional)* — show only unfinished tasks: hides the terminal
  statuses `done` and `accepted`. A task resting at `rejected` awaits rework,
  so it counts as open.
- `--summary` *(optional)* — roll tasks up per PRD instead of listing each
  one: table columns `PRD | Open | Total | Breakdown`, PRDs with open work
  first. `Total` is always the true per-PRD count; combining with `--open`
  only hides PRDs that have nothing open. With `--json` the `data` payload is
  `{"summary": [{"prd", "open", "total", "by_status"}, ...], "prd_count",
  "open", "total", "filters"}`.
- `--feature TEXT` *(optional)* — filter by feature id (e.g. `F001`).
- `--type TEXT` *(optional)* — filter by task type: `feature` (default),
  `bugfix`, `refactor`, or `modify`.
- `--cwd PATH` *(hidden)* — project directory. Defaults to cwd.

**Exit codes:**

- `0` — table printed, or the friendly "No tasks found" message.

**Example:**

```bash
anvil list
anvil list --status ready
anvil list --open --summary   # "what's left, per PRD?" in one call
anvil list --feature F001 --status drafted
anvil list --type bugfix
```

**See also:** [`anvil show`](#show) for the per-task detail;
[`anvil next`](#next) for the recommendation.

### `anvil show` { #show }

**Synopsis:** Print full task detail in a human-readable multi-section
format. Sections: title, feature, status, priority, review tier, scores
breakdown (all six dimensions plus explanation), dependencies, conflict
groups, acceptance
criteria, verification commands, likely files, active claim (if any), and
the 10 most recent events targeting this task.

**Positional arguments:**

- `TASK_ID` *(required)* — task id to display (e.g. `T001`).

**Flags:**

- `--cwd PATH` *(hidden)* — project directory. Defaults to cwd.

**Exit codes:**

- `0` — task printed.
- `1` — `TASK_ID` not found.

**Example:**

```bash
anvil show T001
```

**See also:** [`anvil list`](#list) for the table view;
[`anvil claim`](#claim) once you have decided to pick it up.

---

## Claims and work

### `anvil next` { #next }

**Synopsis:** Pick the highest-priority claimable task **without** claiming
it. Prints the recommended task id, title, priority, review tier, and
complexity. Run
`anvil claim TASK_ID` to acquire the lease after reviewing the
recommendation. Reaps any stale claims (expired leases) before recommending.

**Flags:**

- `--actor TEXT` *(optional)* — actor identity; defaults to `$USER` or
  `agent`. Used to scope the "claimable by me" filter when implemented.
- `--type TEXT` *(optional)* — only recommend tasks of this type: `feature`,
  `bugfix`, `refactor`, or `modify`.
- `--max-blast INTEGER` *(optional, `$ANVIL_MAX_BLAST`)* — **[EXPERIMENTAL]**
  risk ceiling for a low-risk runner: only recommend tasks whose
  `blast_radius` is confirmed (via `anvil review tasks`) and `<= N`.
  Unconfirmed/unscored tasks are ineligible even below the ceiling, so the
  filter fails safe rather than open.
- `--max-review-risk INTEGER` *(optional, `$ANVIL_MAX_REVIEW_RISK`)* —
  **[EXPERIMENTAL]** same semantics as `--max-blast` for the confirmed
  `review_risk` dimension.
- `--prd TEXT` *(optional, `$ANVIL_PRD`)* — scope the candidate pool to one
  PRD partition; coordination (conflict-group checks) still spans all PRDs.
- `-q`, `--quiet` *(flag)* — print nothing; use the exit code as the signal
  only (see Exit codes below). Loop seam for `jq`-less shells, e.g.
  `while anvil next -q; do ...; done`.
- `--cwd PATH` *(hidden)* — project directory. Defaults to cwd.

**Exit codes:**

- `0` — recommendation printed, or "No claimable tasks available." printed
  (human mode); or, with `--json` and no `--prd` scoping, the
  `{"task": null}` envelope is emitted — an empty queue is not an error here.
- `3` — with `-q`/`--quiet`: prints nothing and exits 3 whenever the queue is
  empty (the loop-seam signal). Also returned when `--prd` scopes the
  candidate pool and that PRD has no claimable task (both human and `--json`
  modes print/emit a PRD-specific message first).

**Example:**

```bash
anvil next
anvil next --type bugfix
while anvil next -q; do anvil claim "$(anvil next --json | jq -r .data.task.id)"; done
```

**See also:** [`anvil claim`](#claim) to actually pick up the task;
[`anvil list`](#list) for the broader view.

### `anvil claim` { #claim }

**Synopsis:** Acquire an exclusive lease on `TASK_ID` and create an
`agent/<task>-<slug>` git branch. Reaps stale claims, runs the pre-claim
conflict check (file overlap with active claims and conflict-group
membership), and records a `claim.created` event. Optionally creates a git
worktree at `../wt-<task_id>/`.

**Positional arguments:**

- `TASK_ID` *(required)* — task id to claim (e.g. `T001`).

**Flags:**

- `--worktree` *(flag)* — also create a git worktree at `../wt-<task_id>/`.
- `--shared-tree` *(flag)* — claim into the shared checkout even under `worktree_isolation: require` (read-only/docs work); also silences the advisory shared-checkout warning.
  Skipped with a stderr warning when no branch was created (e.g. when the
  branch already exists).
- `--force` *(flag)* — override the pre-claim conflict warnings. Without
  `--force`, file overlap or group conflicts cause the command to exit 1
  after listing every conflicting claim.
- `--actor TEXT` *(optional)* — claim actor; defaults to `$USER` or
  `agent`.
- `--lease FLOAT` *(optional)* — lease duration in minutes for this claim.
  Overrides `default_lease_minutes` from config. Lease precedence: this flag
  > project `config.yaml` > global `config.yaml` > built-in `240` (see
  [Global-config layer](#global-config-layer)).
- `--branch TEXT` *(optional)* — attach the claim to an existing or
  caller-named branch instead of generating the default
  `agent/<task>-<slug>` name. An existing branch is checked out; a new one
  is created. The resolved branch name is recorded on the claim. Omit for
  the default auto-generated branch (unchanged behaviour).
- `--cwd PATH` *(hidden)* — project directory. Defaults to cwd.

**Exit codes:**

- `0` — claim acquired. Prints the claim id, lease expiry, branch name, and
  optional worktree path.
- `1` — `TASK_ID` not found, pre-claim conflicts detected without `--force`,
  or the `ClaimManager` rejected the claim (task in wrong status, already
  claimed by another actor, lease overlap, etc.).

**Example:**

```bash
anvil claim T001
anvil claim T001 --worktree --actor "alex"
anvil claim T001 --force            # override conflict warnings
anvil claim T001 --lease 15         # 15-minute lease (overrides config)
anvil claim T001 --branch my-existing-branch
```

**See also:**
[`how-to/claiming-and-shipping-a-task.md`](how-to/claiming-and-shipping-a-task.md);
[`anvil release`](#release), [`anvil renew`](#renew),
[`anvil submit`](#submit).

### `anvil release` { #release }

**Synopsis:** Release a claim by `CLAIM_ID`, returning the task to `ready`.
Emits a `claim.released` event with the optional reason.

**Positional arguments:**

- `CLAIM_ID` *(required)* — claim id to release (e.g. `C001`).

**Flags:**

- `--force` *(flag)* — force release even if the claim belongs to another
  actor. Without `--force`, releasing someone else's claim fails.
- `--reason TEXT` *(optional)* — human-readable reason for the release
  (recorded on the event).
- `--actor TEXT` *(optional)* — actor identity; defaults to `$USER` or
  `agent`.
- `--cwd PATH` *(hidden)* — project directory. Defaults to cwd.

**Exit codes:**

- `0` — claim released.
- `1` — `CLAIM_ID` not found, already released, or owned by another actor
  without `--force`.

**Example:**

```bash
anvil release C001 --reason "blocked on upstream PR"
anvil release C002 --force --reason "actor abandoned"
```

**See also:** [`anvil claim`](#claim), [`anvil renew`](#renew).

### `anvil renew` { #renew }

**Synopsis:** Extend the lease heartbeat on `CLAIM_ID`. Prints the new lease
expiry and last-heartbeat timestamp. Use this from a long-running agent loop
to prevent the stale-claim reaper from reclaiming the task mid-flight.

**Positional arguments:**

- `CLAIM_ID` *(required)* — claim id to renew (e.g. `C001`).

**Flags:**

- `--actor TEXT` *(optional)* — actor identity; defaults to `$USER` or
  `agent`.
- `--lease FLOAT` *(optional)* — lease extension in minutes. Overrides
  `default_lease_minutes` from config (same precedence as
  [`claim --lease`](#claim)).
- `--cwd PATH` *(hidden)* — project directory. Defaults to cwd.

**Exit codes:**

- `0` — lease renewed.
- `1` — `CLAIM_ID` not found, already released, expired beyond recovery, or
  owned by another actor.

**Example:**

```bash
anvil renew C001
anvil renew C001 --lease 30   # extend by 30 minutes
```

**See also:** [`anvil claim`](#claim), [`anvil release`](#release).

### `anvil packet` { #packet }

**Synopsis:** Render a work packet for `TASK_ID` and write it to
`.anvil/packets/`. The packet bundles task definition, parent
feature, completed dependencies, open dependencies, related decisions, and
active claim metadata into a single self-contained artefact for an agent to
execute against.

**Positional arguments:**

- `TASK_ID` *(required)* — task id to render a work packet for (e.g.
  `T001`).

**Flags:**

- `--format {md,json}`, `-f` *(default: `md`)* — output format. `md` writes
  `packets/<TASK_ID>.md`; `json` writes `packets/<TASK_ID>.json`. Stdout
  echoes the rendered content matching the selected format.
- `--cwd PATH` *(hidden)* — project directory. Defaults to cwd.

**Review tier.** Every packet carries a derived review tier —
`light` / `standard` / `max` — with one line of reviewer guidance
(markdown header + `review_tier` JSON key). The tier is a pure projection
over the six-dimension score plus the risk-confirmation flags, recomputed at
every read (never persisted): `max` when any dimension is unscored or
`review_risk`/`blast_radius` ≥ `review_tier_max_min`; `light` only when the
task passes the fast-lane gate AND `review_risk` ≤
`review_tier_light_risk_max` AND **both** the `blast_radius` and
`review_risk` scores are confirmed (via `anvil review tasks`); `standard`
otherwise. Two
`config.yaml` knobs move the boundaries (1–5 score scale, global-config
mergeable):

```yaml
review_tier_max_min: 4          # DEFAULT; review_risk/blast_radius at/above → max
review_tier_light_risk_max: 2   # DEFAULT; highest confirmed review_risk still light
```

The same tier appears on `anvil next`, `anvil show`, and the MCP
`get_task` / `get_next_task` responses.

**Exit codes:**

- `0` — packet written and echoed.
- `1` — `TASK_ID` not found.

**Example:**

```bash
anvil packet T001
anvil packet T001 --format json
```

**See also:** [`anvil claim`](#claim) (typically run before
generating the packet); the rendered packet feeds directly into Claude Code,
Cursor, or any MCP-aware agent.

---

## Submit and apply

### `anvil submit` { #submit }

**Synopsis:** Record completion evidence for `TASK_ID`; auto-releases the
active claim and transitions the task to `needs_review`. Emits an
`evidence.submitted` event with the commands run, files changed, optional
output excerpt (truncated to 8000 chars), PR url, commit SHA, and known
limitations. Prints a gate summary indicating whether the recorded evidence
satisfies the task's `required_evidence`.

**Positional arguments:**

- `TASK_ID` *(required)* — task id to submit evidence for (e.g. `T001`).

**Flags:**

- `--commands TEXT` *(required)* — comma-separated verification commands
  that were run.
- `--files-changed TEXT` *(required)* — comma-separated file paths modified.
- `--category TEXT` *(optional, default `completion`)* — the evidence role
  (evidence contracts, issue #153): `completion`, `diagnostic`, `blocked`,
  `advisory`, or `promotion_quality`. `diagnostic`/`advisory` evidence can
  never satisfy a completion claim; `blocked` records that the claim could
  not be proven (and refuses the claim gate). An invalid value exits 1 with
  code `invalid_category`. See the evidence-contract gate under
  [`anvil apply`](#apply).
- `--output-file PATH` *(optional)* — path to a file whose content is used
  as the output excerpt (read with `errors="replace"`, truncated to 8000
  chars).
- `--pr-url TEXT` *(optional)* — pull request URL.
- `--commit-sha TEXT` *(optional)* — commit SHA associated with this
  submission.
- `--known-limitations TEXT` *(optional)* — known limitations or caveats.
- `--screenshots TEXT` *(optional)* — comma-separated paths to screenshot
  files. Required when the task's `verification.required_evidence` includes
  an item matching "screenshot" (the gate checks `evidence.screenshots` is
  non-empty). Default: `[]`.
- `--actor TEXT` *(optional)* — actor submitting evidence; defaults to
  `$USER` or `agent`.
- `--cwd PATH` *(hidden)* — project directory. Defaults to cwd.

**Exit codes:**

- `0` — evidence recorded and claim auto-released. The "evidence gate"
  summary may report INCOMPLETE without changing the exit code (gate
  feedback is informational; the human reviewer decides at `apply` time).
- `1` — no active claim found for `TASK_ID` (run `claim` first).

**Example:**

```bash
anvil submit T001 \
  --commands "pytest tests/test_auth.py, ruff check src/auth" \
  --files-changed "src/auth/login.py, tests/test_auth.py" \
  --pr-url "https://github.com/acme/api/pull/42" \
  --commit-sha "abc123def"
```

For a task whose `required_evidence` includes a "screenshots" item, attach
the captures with `--screenshots`:

```bash
anvil submit T002 \
  --commands "pytest tests/test_ui.py" \
  --files-changed "src/ui/login_page.py" \
  --screenshots "docs/images/login-before.png,docs/images/login-after.png"
```

**See also:** [`anvil claim`](#claim) for the prior step;
[`anvil apply`](#apply) for human review;
[`docs/evidence-buffer.md`](evidence-buffer.md) for the hook-captured
evidence buffer that feeds `--output-file`.

### `anvil apply` { #apply }

**Synopsis:** Human review gate. Without `--approve` / `--reject`: review-only
mode — prints the evidence-gate summary and the current status. With
`--approve`: transition `needs_review` → `accepted` → `done`. With
`--reject`: transition `needs_review` → `drafted` (rework path). Emits a
`task.applied` event with the reviewer, decision, and notes.

**Positional arguments:**

- `TASK_ID` *(required)* — task id to apply a review decision to (e.g.
  `T001`).

**Flags:**

- `--approve` *(flag)* — approve: transition `needs_review` → `accepted`
  → `done`.
- `--reject` *(flag)* — reject: transition `needs_review` → `drafted`.
  Requires `--reason`. Mutually exclusive with `--approve`.
- `--reason TEXT` *(required with `--reject`, optional with `--approve`)* —
  review notes.
- `--reviewer TEXT` *(optional)* — reviewer identity; defaults to `$USER`
  or `human`.
- `--cwd PATH` *(hidden)* — project directory. Defaults to cwd.

**Merge check.** Review-only mode and `--approve` also run a cheap
base-freshness probe against the task's claim branch (behind-count vs
`origin/<default>`, textual-conflict check; never the heavy merged-tree run
— that is [`anvil merge-check --run-checks`](#additional-commands)). The
`merge_check` config knob sets the mode:

```yaml
merge_check: advisory   # DEFAULT — report staleness, approval proceeds
# merge_check: strict   # refuse --approve (exit 1, code base_stale) when the
#                       # branch is VERIFIABLY behind its base or conflicted
# merge_check: "off"    # skip the probe entirely
```

**Worktree isolation.** The `worktree_isolation` config knob sets the claim
isolation mode:

```yaml
worktree_isolation: advisory   # DEFAULT — warn when a new claim would share
#                              # the working tree with another active claim
# worktree_isolation: require  # every claim isolates into a git worktree by
#                              # default (as if --worktree); --shared-tree is
#                              # the explicit opt-out. Fail-closed: if the
#                              # worktree cannot be created the claim is
#                              # released and refused (--force keeps it).
# worktree_isolation: "off"    # flag-only (--worktree) behavior
```

The MCP `claim_task` tool honors the same policy: under `require` it refuses
unless `shared_tree=true` (the MCP server cannot create worktrees itself);
under `advisory` the shared-checkout warning is returned in the response
`warnings` list.

Local-first: offline / no-remote projects degrade to the local default
branch and are never refused; an unverifiable probe never gates (a probe
*error* under `strict` prints a stderr warning and skips the gate rather
than blocking). `--reject` is never affected. The JSON envelope carries the
report under `data.merge_check` (and inside `error.merge_check` on a strict
refusal).

> **Ordering caveat — apply before you merge.** The probe measures the
> task's *local* claim branch against the base. In a merge-first workflow
> (PR squash-merged, then `apply`), the surviving local branch is behind the
> base *by its own merge commit* and reads as `STALE` — a false positive
> (there is no reliable git signal for "already squash-merged"). Run
> `apply --approve` before merging the PR, or expect the advisory note; do
> not enable `merge_check: strict` in a merge-first workflow.

**Evidence-contract gate (auto-strict).** A task that declares an evidence
contract — named `claims` and/or `Artifact assertions` in its PRD block (see
`docs/prd-template.md`) — is held to it at `--approve` **independent of
`strict_evidence`**. `apply` re-evaluates the artifacts at approval time and
prints a per-claim verdict (`claim_verdict` JSON key; human `Claim <id>:
<VERDICT>` lines). Per-claim verdict vocabulary:

| Verdict | Meaning |
|---|---|
| `passed` | every bound assertion/proof satisfied on completion-category evidence |
| `failed` | an artifact assertion **contradicted** the claim on an existing artifact |
| `incomplete` | a required proof is unmet, the artifact is not yet written, a named claim binds no contract, or no evidence was submitted |
| `blocked` | the evidence's `category` is `blocked` — the claim could not be proven |
| `diagnostic_only` | assertions pass but the evidence is `diagnostic`/`advisory` — excellent context, proves no completion claim |

The overall verdict is the worst per-claim one (`failed` > `blocked` >
`incomplete` > `diagnostic_only` > `passed`). When any **enforceable**
unproven claim remains, `--approve` refuses with exit 1 and error code
`claim_unproven`; the task stays in `needs_review`. Named claims always
enforce; on the implicit task-level claim, an unmet **command proof alone**
stays governed by `strict_evidence` — everything else on that claim (an
artifact-assertion contradiction, an unwritten or missing artifact, no
evidence submitted, or a `blocked`/`diagnostic_only` category) always
enforces regardless of `strict_evidence`. `--reject` is never gated. An advisory `Intent check` block
(`intent_warnings`) additionally flags task intents that no claim or
assertion covers — never blocking.

**Exit codes:**

- `0` — review decision recorded, **or** review-only mode (neither
  `--approve` nor `--reject`) printed the summary.
- `1` — `TASK_ID` not found; task is not in `needs_review` status; both
  `--approve` and `--reject` were passed; `--reject` was passed without
  `--reason`; `merge_check: strict` refused a stale/conflicted branch
  (code `base_stale`); or the **claim gate** refused a task whose evidence
  contract has an unproven claim (code `claim_unproven`, see below).

**Example:**

```bash
anvil apply T001                                      # review-only
anvil apply T001 --approve --reviewer "alex"
anvil apply T001 --reject --reason "missing tests for edge case X"
```

**See also:** [`anvil submit`](#submit) for the prior step;
[`anvil show`](#show) to inspect the submitted evidence.

---

## Sync

### `anvil sync` { #sync }

**Synopsis:** Run the `ReconciliationEngine` and print a report of any
discrepancies between local state, configured providers, and the event log.
With `--fix`, additionally apply each suggested fix; combine with `--yes` for
CI / non-interactive contexts. Named subcommands (`github`, `provider`) take
over when invoked — this bare form only runs when no subcommand is supplied.

**Flags:**

- `--fix` *(flag)* — after scanning, apply each suggested fix. Requires
  `--yes` in non-interactive mode (stdin/stdout not a tty).
- `--yes` *(flag)* — skip the confirmation prompt before applying fixes.
- `--cwd PATH` *(hidden)* — project directory. Defaults to cwd.

**Exit codes:**

- `0` — scan completed; or scan completed and operator declined the apply
  prompt; or `--fix --yes` applied all fixes successfully.
- `1` — `--fix` was passed without `--yes` in non-interactive mode.

**Example:**

```bash
anvil sync                # scan + print report
anvil sync --fix --yes    # scan + auto-apply
```

**See also:** [`anvil sync github`](#sync-github);
[`anvil sync provider`](#sync-provider);
[`docs/sync-providers.md`](sync-providers.md) for the provider contract.

### `anvil sync github` { #sync-github }

**Synopsis:** Sync tasks against GitHub Issues. Convenience alias for
`anvil sync provider github_issues`. Default (neither `--push` nor
`--pull`) runs both directions. Conflict resolution honours each
SyncMapping's `conflict_resolution_strategy`
(`local_wins`, `remote_wins`, `prompt`, `manual_merge`); `--fix` forces
`remote_wins` on every conflict for this run.

**Flags:**

- `--push` *(flag)* — push local tasks to GitHub only (skip pull).
- `--pull` *(flag)* — pull remote issues to local only (skip push).
- `--watch` *(flag)* — long-running poll loop; Ctrl-C to exit. Each iteration
  is isolated (per-task failures do not kill the daemon).
- `--fix` *(flag)* — reconcile remote state into local on conflicts (forces a
  pull for tasks whose `SyncMapping` is in `conflict` state).
- `--task TEXT` *(optional)* — scope sync to a single task id (e.g. `T001`).
- `--yes` *(flag)* — auto-confirm conflict prompts; defaults to `local_wins`
  in non-interactive mode.
- `--health` *(flag)* — probe provider reachability and auth; print status;
  exit. Does not require an initialised project (useful for pre-init
  connectivity sanity checks).
- `--interval INTEGER` *(default: `60`)* — poll interval seconds with
  `--watch`. Use `0` for a single iteration (test seam).
- `--cwd PATH` *(hidden)* — project directory. Defaults to cwd.

**Exit codes:**

- `0` — sync iteration completed successfully.
- `1` — provider cannot be instantiated (e.g. missing `GITHUB_REPOSITORY` or
  `GITHUB_TOKEN`); audit emission catastrophic failure.
- `2` — one or more tasks parked awaiting `manual_merge` resolution. Inspect
  files under `.anvil/.sync-conflicts/<TASK_ID>.md`, resolve, delete,
  re-run sync.

**Example:**

```bash
anvil sync github --health
anvil sync github --push --task T001
anvil sync github --watch --interval 30
```

**See also:** [`how-to/syncing-with-github.md`](how-to/syncing-with-github.md);
[`docs/github-sync.md`](github-sync.md);
[`anvil sync provider`](#sync-provider) for the generic form.

### `anvil sync provider` { #sync-provider }

**Synopsis:** Push/pull against a registered sync provider by id. Same
mechanics as `sync github`, but the provider id is supplied as a positional
argument so contributor-registered providers (Monday, Linear, custom
trackers, etc.) can be invoked without a dedicated alias.

**Positional arguments:**

- `PROVIDER_ID` *(required)* — sync provider id (e.g. `github_issues`,
  `monday`, `linear`). On miss, prints the list of registered providers.

**Flags:**

- `--push` *(flag)* — push local tasks only (skip pull).
- `--pull` *(flag)* — pull remote tasks only (skip push).
- `--watch` *(flag)* — long-running poll loop; Ctrl-C to exit.
- `--fix` *(flag)* — reconcile remote → local on conflicts (forces a pull on
  conflict).
- `--task TEXT` *(optional)* — scope sync to a single task id.
- `--yes` *(flag)* — auto-confirm conflict prompts.
- `--health` *(flag)* — probe provider; print status; exit.
- `--interval INTEGER` *(default: `60`)* — poll interval seconds with
  `--watch`.
- `--cwd PATH` *(hidden)* — project directory. Defaults to cwd.

**Exit codes:**

- `0` — sync iteration completed.
- `1` — unknown `PROVIDER_ID`, provider instantiation failed, or audit
  emission catastrophic failure.
- `2` — one or more tasks parked awaiting `manual_merge` resolution.

**Example:**

```bash
anvil sync provider github_issues --health
anvil sync provider monday --push --task T015
```

**See also:** [`docs/sync-providers.md`](sync-providers.md) for the provider
registration contract; [`anvil sync github`](#sync-github) for the
GitHub-specific alias.

---

## Cross-harness

### `anvil mcp-config` { #mcp-config }

**Synopsis:** Print the paste-ready MCP server config block for a target MCP
client, with the `anvil` server pointed at this checkout's `bin/anvil-mcp` by
**absolute path** (not `${CLAUDE_PLUGIN_ROOT}`), so any MCP-capable harness gets
the full 35-tool surface. Read-only and project-free (mirrors `anvil describe`):
it never opens a backend, runs from any directory, and only *prints* config — it
never mutates the client's own settings file. In text mode the config goes to
stdout (paste-clean) and a one-line `# paste into <file>` hint goes to stderr.

**Argument:**

- `CLIENT` *(required)* — one of `claude-code`, `cursor`, `windsurf`, `cline`,
  `vscode`, `zed`, `codex`, `opencode`, `roo`, `amp`, `continue`, `goose`
  (12 clients). The envelope differs per client: top key `mcpServers` /
  `servers` / `context_servers` / `mcp` / `amp.mcpServers` / `extensions`,
  and the format is JSON for most clients, TOML for `codex`, and YAML for
  `continue` and `goose`. The inner server spec is usually
  `{command, args[, env]}`; `opencode`, `continue`, and `goose` have their
  own client-specific shapes (e.g. `opencode` nests env vars under
  `environment`, not `env`; `goose` uses `cmd`/`envs`).

**Flags:**

- `--uv-run` *(flag)* — emit the explicit `uv run --quiet --project <bin> python -m
  anvil.mcp_server` invocation instead of the `bash <bin>/anvil-mcp` wrapper
  (use on hosts without bash, e.g. Windows).
- `--root PATH` *(option)* — inject `"env": {"ANVIL_ROOT": "<dir>"}` to pin the
  project root. Omitted by default (the client's cwd decides).
- `--json` *(flag)* — emit the standard single-line envelope; `data` carries
  `{client, target_file, format, config_text}` and nothing goes to stderr.

**Exit codes:**

- `0` — config printed.
- `2` — unknown client (under `--json`, `error.code` is `bad_request`).

**Example:**

```bash
anvil mcp-config cursor              # prints the mcpServers JSON block
anvil mcp-config codex               # prints the [mcp_servers.anvil] TOML block
anvil mcp-config continue            # prints the .continue/mcpServers/anvil.yaml block
anvil mcp-config --uv-run vscode     # explicit uv invocation (no bash)
anvil mcp-config --json cursor | jq -r .data.config_text
```

**See also:** [`AGENTS.md`](https://github.com/fakoli/anvil/blob/main/AGENTS.md) for the MCP-tool ⇄ CLI-command table;
[`docs/how-to/using-anvil-on-any-harness.md`](how-to/using-anvil-on-any-harness.md)
for the full cross-harness walkthrough.

---

## Hook subcommands (internal — invoked by `hooks.json`)

These commands are called by the plugin's bash hooks (in `hooks/`) — not by
end users directly. They are documented here because they are the
machine-facing surface of `anvil` and contributors writing custom
hooks need the flag list. Every hook subcommand **always exits 0**: hook
failures must never block the calling tool or session.

### `anvil hook check-claim` { #hook-check-claim }

**Synopsis:** Used by `hooks/check-claim.sh` (PreToolUse on Edit / Write /
NotebookEdit). Checks whether `FILE` is within the scope of an active claim.
If `FILE` is in the `expected_files` of a claim owned by a *different* actor,
warns to stderr. Silent in every other case.

**Flags:**

- `--file TEXT` *(required)* — path of the file about to be modified.
- `--actor TEXT` *(required)* — session actor / `session_id`.
- `--cwd PATH` *(hidden)* — project directory. Defaults to cwd.

**Exit codes:**

- `0` — always. Errors are silently swallowed; hooks must never block the
  tool.

**Example (from `hooks/check-claim.sh`):**

```bash
anvil hook check-claim --file "src/auth/login.py" --actor "$SESSION_ID"
```

**See also:** [`docs/architecture.md`](architecture.md) for the hook
contract; `hooks/check-claim.sh`.

### `anvil hook record-file-change` { #hook-record-file-change }

**Synopsis:** Used by `hooks/record-file-change.sh` (PostToolUse on Edit /
Write / NotebookEdit). Appends a `file_changed` event to both the SQLite
events table and `events.jsonl` so the audit log has a record of every file
mutation made during a session.

**Flags:**

- `--file TEXT` *(required)* — path of the file that was modified.
- `--tool TEXT` *(required)* — tool name (e.g. `Edit`, `Write`,
  `NotebookEdit`).
- `--actor TEXT` *(required)* — session actor / `session_id`.
- `--cwd PATH` *(hidden)* — project directory. Defaults to cwd.

**Exit codes:**

- `0` — always. Errors are silently swallowed.

**Example (from `hooks/record-file-change.sh`):**

```bash
anvil hook record-file-change \
  --file "src/auth/login.py" --tool "Edit" --actor "$SESSION_ID"
```

**See also:** `hooks/record-file-change.sh`.

### `anvil hook capture-evidence` { #hook-capture-evidence }

**Synopsis:** Used by `hooks/capture-evidence.sh` (PostToolUse on Bash).
Appends a JSON record of the bash command (command string, exit code,
stdout excerpt, stderr excerpt, actor, timestamp) to
`.anvil/.evidence-buffer/<CLAIM_ID>.json`. If no active claim is found
for the actor, writes to `.evidence-buffer/orphan.json` with a recovery hint.
Stdout/stderr excerpts are truncated to 4000 chars each.

**Flags:**

- `--command TEXT` *(required)* — full bash command string that was run.
- `--exit-code INTEGER` *(required)* — exit code of the command.
- `--stdout-file PATH` *(optional)* — path to a temp file containing the
  command's stdout.
- `--stderr-file PATH` *(optional)* — path to a temp file containing the
  command's stderr.
- `--actor TEXT` *(required)* — session actor / `session_id`.
- `--cwd PATH` *(hidden)* — project directory. Defaults to cwd.

**Exit codes:**

- `0` — always. Errors are silently swallowed.

**Example (from `hooks/capture-evidence.sh`):**

```bash
anvil hook capture-evidence \
  --command "pytest tests/test_auth.py" \
  --exit-code 0 \
  --stdout-file "$STDOUT_TMP" \
  --stderr-file "$STDERR_TMP" \
  --actor "$SESSION_ID"
```

**See also:** [`docs/evidence-buffer.md`](evidence-buffer.md) for the buffer
format and how `submit --output-file` consumes it;
`hooks/capture-evidence.sh`.

---

## Additional commands (index) { #additional-commands }

The sections above give full Synopsis/Flags/Exit-codes treatment to the core
lifecycle commands. anvil ships 25 more — every one real,
`--help`-documented, and exercised by the test suite — indexed here one line
at a time so this page's single-reference claim holds. Run
`anvil <command> --help` (or `anvil <group> <command> --help`) for the live
flag list; full prose treatment may follow in a later pass.

**Self-description and cross-harness delivery**

- `anvil describe` — Emit a machine-readable manifest of the CLI/MCP command
  surface (engine version, schema version, every command/tool name)
  (`--human`, `--json`); read-only, needs no project.
- `anvil install <harness>` — Deliver anvil's MCP config and instructions to
  a target harness (codex/openclaw drive their own CLI; others get a merged
  MCP block) (`--write`, `--rollback`, `--root`, `--automations`,
  `--cron-recipes`, `--finish-gate`); dry-run by default.

**PRD authoring extras**

- `anvil prd list` — List every PRD in the project (the multi-PRD entry
  point), marking the default with `*` (`--json`).
- `anvil prd find-decisions` — Scan a PRD for `[NEEDS DECISION]` markers,
  open questions, and missing acceptance-criteria/verification fields
  (`--file`, `--json`); read-only, always exits 0.
- `anvil prd resolve-decision DECISION_ID` — Back-propagate a resolved
  decision into the PRD source and record a `prd.decision_resolved` event
  (`--resolution`/`-r`, `--by`, `--file`, `--json`).

**Planning extras**

- `anvil assumptions` — Rank PRD requirements by
  `blast_radius x uncertainty` so the riskiest, least-certain requirements
  surface before planning (`--limit`/`-n`, `--json`); advisory only, never
  mutates state.
- `anvil deps` — Apply a batch of dependency-edge edits (`--add`/`--remove
  SOURCE:TARGET`, repeatable) atomically, rejecting the whole batch on any
  cycle, unknown task, or self-loop.

**Diagnostics and health** (read-only)

- `anvil doctor` — One-shot health diagnosis: schema/db reachability, config
  parse status, active/stale claims, replay integrity, reconciliation drift
  (`--json`); exits non-zero when any finding is ERROR-level. With
  `--preflight [--prd <id>]`, adds PRD-parse, unresolved-decision, and git
  tree-state probes plus a final `PREFLIGHT: GO`/`NO-GO` verdict line
  (JSON: `data.preflight`/`data.go`) — the GO/NO-GO gate to run before a
  long workflow.
- `anvil merge-check <task>` — Pre-merge freshness report for the task's
  claim branch: behind-count vs `origin/<default>` (offline degrades to the
  local base) and a `git merge-tree` textual-conflict probe; with
  `--run-checks`, runs the task's verification commands against the
  would-be merge result in a throwaway worktree (`--json`); exit 1 when
  stale, conflicted, or a merged-tree check fails. See also the
  `merge_check` config knob on [`anvil apply`](#apply).
- `anvil progress <task> <phase>` — Record a structured progress phase
  (`build`, `tests`, …) as a `progress.noted` audit event; task status
  never changes and no claim is required (`--detail`, `--actor`, `--json`).
  `anvil status` shows each active claim's latest phase, elapsed time, and
  lease-expiry countdown.
- `anvil drift` — Report intent/state/filesystem-git divergence (orphan
  branches, orphan worktrees, orphan packets, stale claims, vanished
  expected files) (`--json`); always exits 0 — a report, not a gate.
- `anvil graph` — Emit the task dependency/state graph as Mermaid, JSON, or
  a text summary (`--format text|mermaid|json`, `--scope all|feature|task`
  with `--target`, `--json`).
- `anvil conflicts` — List persisted conflict groups — tasks whose
  `likely_files` overlap (`--format text|json`).
- `anvil notify-digest` — Print a one-line needs-review/blocked/
  leases-expiring-soon summary, staying silent when the queue is clean;
  built for cron `--announce` jobs (`--json`, incl. `expiring_soon`);
  always exits 0.

**Native-harness gates** (read-only, default-open; built for
OpenClaw/Codex-style `before_tool_call` / `before_agent_finalize` hooks)

- `anvil claim-guard` — Check whether an actor holds a claim covering the
  file(s) it is about to edit before a mutating tool runs (`--actor`,
  `--file` repeatable, `-q`/`--quiet`, `--json`; exit 2 = block, no claim
  held).
- `anvil gate-check` — Finish-gate: block an agent from ending its turn
  while any of its claimed tasks has incomplete verification evidence
  (`--actor`, `-q`/`--quiet`, `--json`; exit 2 = block).

**Data lifecycle and maintenance**

- `anvil replay --from-events PATH --into PATH` — Rebuild canonical state
  from an events log into a scratch SQLite database; refuses to target the
  live `state.db`.
- `anvil run-workflow NAME` — Run a declarative
  `.anvil/workflows/<name>.yaml` workflow to completion through anvil's
  governed create → claim → run → submit → apply transitions, then exit.
- `anvil backup` — Push `events.jsonl` (and, with `--include-db`,
  `state.db`) to the configured S3 `durable_store`.
- `anvil restore` — Pull `events.jsonl` from S3 and replay it into
  `state.db` (destructive; `--yes`/`-y` skips the confirmation prompt).
- `anvil migrate-events --to git` — Rewrite `events.jsonl` into hash-chained,
  merge-friendly git-backed storage (dry-run by default; `--yes` applies).
- `anvil migrate state` — Upgrade `.anvil/state.db` to the current engine
  schema version, backing it up first (dry-run by default; `--yes`
  applies; `--json`).
- `anvil migrate-workspace` — One-time copy of legacy in-repo `.anvil/`
  state into the HOME-workspace layout; never clobbers an existing
  workspace, copies rather than moves (dry-run by default; `--yes` applies;
  `--json`).

**Proof verification**

- `anvil proof verify PROOF_FILE` — Verify a signed `AcceptanceProof`
  off-host: detached Ed25519 signature, signer fingerprint, and trust-list
  membership (`--trust`, `--project`, `--json`).

**Additional hook subcommands** (internal — see
[Hook subcommands](#hook-check-claim) above for the contract)

- `anvil hook dispatch NAME` — Shell-free dispatcher for `hooks/hooks.json`
  (`detect-state`, `check-claim`, `record-file-change`, `capture-evidence`,
  `heartbeat`); parses the hook JSON payload from stdin and calls the
  matching subcommand. Always exits 0.
- `anvil hook stop-gate` — Opt-in Stop-hook evidence gate for Codex/Claude
  Code: blocks ending the turn (exit 2, `{"decision":"block",...}` on
  stdout) while a claimed task has no submitted evidence; not wired by
  default (`--actor`).
- `anvil hook heartbeat` — PostToolUse lease heartbeat: renews the actor's
  active claim lease(s) on tool activity so a lazy lease stays fresh
  (`--actor`); always exits 0.
