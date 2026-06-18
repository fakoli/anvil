# Hooks reference

> anvil ships 4 hooks that detect project state, enforce claim
> discipline, record file changes, and buffer verification-command output as
> evidence. All hooks are **non-blocking** by design — warnings only, never
> errors. Hooks are wired via [`hooks/hooks.json`](../hooks/hooks.json) and
> shell out to `anvil hook ...` subcommands implemented in
> [`bin/src/anvil/cli/hooks.py`](../bin/src/anvil/cli/hooks.py).

---

## The non-blocking contract

All 4 hooks follow these invariants. They are enforced by code review and by
the hook test suite — any new hook script that violates them will be
rejected.

1. **No `set -e`, no `set -u`, no `set -o pipefail`.** A single failed
   command must never abort the script. A hook that aborts mid-run leaves
   partial files on disk (a half-written `orphan.json`, a missing audit
   row) and trains the agent to mistrust the hook layer.
2. **Always `exit 0`.** Every script ends with an unconditional `exit 0`
   regardless of whether the work succeeded. Failures are logged to stderr
   or silently swallowed — they are never propagated as a non-zero status
   to Claude Code.
3. **Wrap CLI calls with `|| true`.** When the script shells out to
   `anvil hook ...`, the call is followed by `|| true` (or the exit
   code is captured into a variable that the script then ignores). The
   Python sub-app itself follows the same discipline: every command in
   `cli/hooks.py` wraps its body in `try/except Exception: pass` and ends
   with `raise typer.Exit(code=0)`.
4. **<200ms internal target; 5s hard timeout.** Each entry in `hooks.json`
   declares `"timeout": 5` — the upper bound the Claude Code runtime will
   wait. The scripts themselves aim for well under 200ms on the hot path,
   per comments in each script header. Hooks fire on every matching tool
   call, so the budget is tight.
5. **Warnings go to stderr; the success path is silent.** Claude Code
   surfaces hook stderr as a user-visible warning; stdout becomes part of
   the model's input context (which would pollute the conversation with
   bookkeeping noise). The success path produces no output at all.

### Why non-blocking

A blocking hook on `PreToolUse: Edit` would freeze the agent on every
edit — eventually the agent would learn to route around the hook by using
`Bash + sed` instead, defeating the discipline. By contrast a warning + an
audit-log entry captures the same signal without breaking flow: humans see
the warning during review, the `events.jsonl` row supports post-hoc
conflict detection, and the agent keeps moving.

The same principle applies to the CLI degradation path. When the
`anvil` binary is missing or its `hook` sub-app returns non-zero
(database locked, subcommand not yet implemented during a phased
rollout), the script falls back to a direct file write or silent skip —
the session is never broken because a backing service is unavailable.

---

## `hooks.json` mapping

The four entries are declared in [`hooks/hooks.json`](../hooks/hooks.json):

| Event | Matcher | Script | Timeout |
|---|---|---|---|
| `SessionStart` | (all) | [`detect-state.sh`](../hooks/detect-state.sh) | 5s |
| `PreToolUse` | `Edit\|Write\|NotebookEdit` | [`check-claim.sh`](../hooks/check-claim.sh) | 5s |
| `PostToolUse` | `Edit\|Write\|NotebookEdit` | [`record-file-change.sh`](../hooks/record-file-change.sh) | 5s |
| `PostToolUse` | `Bash` | [`capture-evidence.sh`](../hooks/capture-evidence.sh) | 5s |

Each script receives the Claude Code hook payload as JSON on stdin and
addresses the project state at `${CLAUDE_PROJECT_DIR:-$PWD}/.anvil/`.

---

## Per-hook reference

### `detect-state.sh` (SessionStart)

**Purpose.** On session start, detect the project language (Rust, Python,
TypeScript, or unknown) by inspecting marker files (`Cargo.toml`,
`pyproject.toml`, `setup.py`, `package.json`, `tsconfig.json`) and print a
one-line state banner to stderr. The banner becomes visible to the agent
as part of the session-start context.

**Banner format (stderr).**
- If `.anvil/` is absent:
  > `[anvil] not initialized in this project — run \`anvil init\` to start`
- If `.anvil/` exists and the CLI is available:
  > `[anvil] Language: Python | active-claims:2 ready-tasks:7 blockers:0 prd-status:approved`
- If `.anvil/` exists but the CLI is missing or returns non-zero:
  > `[anvil] Language: Python | state present, CLI not available — install anvil bin to enable status`

**Side effects.** None. Read-only banner.

**Performance.** The script header targets <1s. In practice the SessionStart
event fires once per session, so this is the loosest of the four budgets.

**CLI call.** `anvil status --hook-format` — emits a single line in
the form `active-claims:N ready-tasks:N blockers:N prd-status:STATUS`.

**Source.** [`hooks/detect-state.sh`](../hooks/detect-state.sh).

---

### `check-claim.sh` (PreToolUse: Edit, Write, NotebookEdit)

**Purpose.** Before any file edit, look up active claims and warn (on
stderr) when the file being modified is in the `expected_files` scope of
**another actor's** claim. Files in the current actor's own claim are
silent.

**Payload extraction.** The script parses stdin JSON for:
- `.tool_input.path` (Edit, Write) or `.tool_input.notebook_path`
  (NotebookEdit) — the file being modified.
- `.session_id` — used as the actor proxy.

**Skip conditions (silent).** The script exits 0 with no output when any of
the following hold:
- `.anvil/` does not exist in the cwd.
- The payload contains no file path.
- The file is an absolute path outside the project tree (not under `pwd`).
- The CLI binary is missing or not executable.

**Warning format (stderr).** When a conflict is detected:
> `[anvil:check-claim] WARNING: file 'src/foo.py' is in the scope of claim 'C00042' owned by 'session-bbb', not 'session-aaa'.`

**Side effects.** None — the CLI subcommand is read-only. It does **not**
append an event to `events.jsonl`; the audit signal lives only in the
agent's terminal output for this PR's check-claim path.

**Performance.** Header targets <200ms. The hot path is the shell-out to
`anvil hook check-claim`, which opens SQLite, calls
`list_active_claims()`, and closes. Phase 11 backlog item P11-HK-S1 tracks
consolidating these per-call sqlite spawns.

**CLI call.** `anvil hook check-claim --file PATH --actor ACTOR`
(defined in
[`bin/src/anvil/cli/hooks.py`](../bin/src/anvil/cli/hooks.py)).

**Source.** [`hooks/check-claim.sh`](../hooks/check-claim.sh).

---

### `record-file-change.sh` (PostToolUse: Edit, Write, NotebookEdit)

**Purpose.** After every file edit, append a `file_changed` event to both
the SQLite `events` table and `events.jsonl`. This feeds the
conflict-detection and audit layers with real per-file write data.

**Payload extraction.** Parses stdin for `.tool_input.path` (or
`.notebook_path`), `.tool_name`, and `.session_id`.

**Skip conditions (silent).** Exits 0 with no output when:
- `.anvil/` does not exist.
- No file path can be extracted from the payload.

**Two-tier write strategy.**
1. **Preferred path.** Shell out to `anvil hook record-file-change
   --file PATH --tool TOOL --actor ACTOR`. The subcommand opens a
   `SqliteBackend`, builds an `Event` with `action="file_changed"`,
   `target_kind="file"`, `target_id=<path>`, calls `backend.apply_event`
   (which writes to both `state.db` and `events.jsonl` inside one
   `BEGIN IMMEDIATE` transaction), and closes.
2. **Direct-append fallback.** If the CLI is absent or returns non-zero
   (DB locked, subcommand not yet wired), the script appends a hand-built
   JSONL line directly to `events.jsonl`. The line uses the same field
   names the replay engine expects (`action`, `entity_type`, `entity_id`,
   `actor`, `tool`, `timestamp`, `source: "hook"`).

**Output.** Silent on success and on every failure path.

**Performance.** Header targets <200ms. Phase 11 backlog item P11-HK-S2
tracks adding `flock` to harden concurrent appends.

**Source.** [`hooks/record-file-change.sh`](../hooks/record-file-change.sh).

---

### `capture-evidence.sh` (PostToolUse: Bash)

**Purpose.** After every Bash tool call, check whether the command matches
a verification pattern (substring match against a hardcoded set). If yes,
capture `stdout` / `stderr` / `exit_code` into the active claim's evidence
buffer at `.anvil/.evidence-buffer/<claim-id>.json`. If no claim is
held by the actor, the record lands in `orphan.json` and can be re-attached
later via `anvil submit TASK_ID --output-file <FILE>`.

**Verification matcher (hardcoded, substring match).**
- `pytest`
- `ruff check`
- `mypy`
- `npm test`
- `cargo test`
- `bun test`

This is the Phase 5 hardcoded set. The matcher is **not** sourced from any
active task's `verification.commands` field — Phase 6+ moves the matcher
to config. Commands that don't match any pattern are silently dropped (the
hook exits 0 without writing).

**Payload extraction.** A single `python3` round-trip parses
`.tool_input.command`, `.tool_response.exit_code`, `.tool_response.stdout`,
`.tool_response.stderr`, and `.session_id`. The script previously spawned
seven python processes for this; the consolidation to one was a
hook-perf-budget fix flagged by the hook-critic agent (see the script
header comments).

**Truncation.** Both `stdout` and `stderr` are truncated to 4000
characters in the captured record. See
[`docs/evidence-buffer.md`](evidence-buffer.md) for the full record
schema and the `submit --output-file` recovery path.

**Two-tier write strategy.**
1. **Preferred path.** Shell out to `anvil hook capture-evidence
   --command CMD --exit-code N --stdout-file F --stderr-file F --actor
   ACTOR`. The subcommand looks up the actor's active claim in `state.db`
   and writes the record to `<claim-id>.json` (or `orphan.json` if no
   active claim exists for that actor).
2. **Direct-write fallback.** If the CLI is absent, returns non-zero, or
   `mktemp` fails, a second `python3` call writes the record directly to
   `orphan.json`. The fallback cannot reach `state.db` from shell cheaply
   enough to honour the <200ms budget, so it always writes to orphan; the
   user re-attaches it later via `submit --output-file`.

**Side effects.** Appends one line to `.anvil/.evidence-buffer/<claim-id>.json`
or `.anvil/.evidence-buffer/orphan.json`.

**Performance.** Header targets <200ms.

**Source.** [`hooks/capture-evidence.sh`](../hooks/capture-evidence.sh).

---

## Troubleshooting

### My hook is not running

- Confirm the plugin is loaded by your Claude Code session — the
  `detect-state.sh` banner fires on every SessionStart and will print
  either a "not initialized" notice or the project state line. If you
  see neither, the plugin is not loaded.
- Check the script is executable:
  `ls -la $CLAUDE_PLUGIN_ROOT/hooks/`.
- Run the script manually to see its output and exit status:
  `bash $CLAUDE_PLUGIN_ROOT/hooks/<script>.sh < /dev/null`.
- Confirm `.anvil/` exists in your cwd — the
  `PreToolUse` / `PostToolUse` hooks fast-path-exit 0 when it is absent.

### I am getting noisy claim warnings

The `check-claim` warning fires when you edit a file that is in another
actor's claim `expected_files`. Two fixes:

- Update the conflicting claim's scope so it no longer covers the file
  (release and re-claim with the right files, or update `expected_files`
  on the task).
- Temporarily disable the hook (see the disable section below) — the
  warning is non-blocking, so this is a comfort fix, not a correctness
  fix.

### My test output is not being captured

`capture-evidence.sh` only captures commands that match its **hardcoded**
substring matcher: `pytest`, `ruff check`, `mypy`, `npm test`,
`cargo test`, `bun test`. The matcher is independent of the active task's
`verification.commands` field — adding a new command to a task does not
add it to the matcher.

Checks:
- Run a command whose string contains one of the matcher substrings.
  `uv run pytest` matches (`pytest` substring). `make test` does not.
- Confirm the actor has an active claim. Without one, the record lands in
  `.anvil/.evidence-buffer/orphan.json` rather than the
  per-claim file.
- Inspect `.anvil/.evidence-buffer/` for any `*.json` files.
- For recovery from `orphan.json`, see
  [`docs/evidence-buffer.md`](evidence-buffer.md) and use
  `anvil submit TASK_ID --output-file
  .anvil/.evidence-buffer/orphan.json`.

### A hook is too slow

Per the non-blocking contract, scripts target <200ms each. The dominant
cost on `check-claim` and `record-file-change` is the python sqlite
connection open inside `anvil hook ...`. Two backlog items track
this:

- **P11-HK-S1** — consolidate sqlite spawns across the hook sub-app.
- **P11-HK-S2** — add `flock` around `events.jsonl` appends.

If the script consistently exceeds the 5s declared timeout in
`hooks.json`, Claude Code aborts it. The hook then partially-wrote a
buffer file or appended a JSONL line; both states are tolerant — the
replay engine ignores malformed JSONL lines and the evidence buffer is
consumed-and-cleared by `submit`.

### Temporarily disable a hook

The hook scripts do not currently read any `ANVIL_*` env-var
override. To disable a hook:

- **Comment its entry out of `hooks.json`** (and restart the session so
  Claude Code re-reads the manifest).
- **Rename the script** — for example,
  `mv hooks/check-claim.sh hooks/check-claim.sh.disabled`. The bash
  invocation in `hooks.json` then fails to find the script and the
  Claude Code runtime treats the entry as a no-op.
- **Or remove `.anvil/`** from the project root entirely. Every
  hook fast-paths to `exit 0` when the state directory is missing, so
  this turns the whole plugin into a no-op.

A config-driven disable mechanism is a candidate roadmap item; the
existing levers above are the supported workflow today.

---

## See also

- [`architecture.md` → Hooks](architecture.md#hooks-4) — architectural placement of the hook layer.
- [`evidence-buffer.md`](evidence-buffer.md) — the record schema, the
  consume-and-rotate lifecycle, and the `submit --output-file` recovery
  path used by `capture-evidence.sh`.
- [`hooks/hooks.json`](../hooks/hooks.json) — the source of truth for
  event-to-script wiring.
- [`bin/src/anvil/cli/hooks.py`](../bin/src/anvil/cli/hooks.py) —
  the three `anvil hook ...` subcommands the scripts shell into.
