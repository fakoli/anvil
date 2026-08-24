# Hooks reference

> **Audience:** users configuring or debugging hook behavior; sections on hook internals (dispatch, wrappers) are contributor detail.

> anvil ships 5 hooks that detect project state, enforce claim
> discipline, record file changes, renew claim leases, and buffer
> verification-command output as evidence. All hooks are **non-blocking** by
> design — warnings only, never errors. Hooks are wired via [`hooks/hooks.json`](https://github.com/fakoli/anvil/blob/main/hooks/hooks.json).
> The default manifest launches the shell-free `anvil hook dispatch ...` path
> through `uv run`; the legacy `.sh` wrappers call the same subcommands in
> [`bin/src/anvil/cli/hooks.py`](https://github.com/fakoli/anvil/blob/main/bin/src/anvil/cli/hooks.py)
> and remain as compatibility/test wrappers.
>
> Paths written as `.anvil/...` below are shorthand for the resolved state
> directory reported by `anvil status`; they are in-repo only in local layout.

---

## The non-blocking contract

All five active dispatcher paths follow these invariants, enforced by tests:

1. **Always `exit 0`.** Every dispatcher path and shell wrapper exits 0
   regardless of whether the work succeeded. Failures are logged to stderr
   or silently swallowed — they are never propagated as a non-zero status
   to Claude Code.
2. **5s hard timeout.** Each entry in `hooks.json`
   declares `"timeout": 5` — the upper bound the Claude Code runtime will
   wait. The active dispatcher has no separately claimed or measured <200ms
   contract.
3. **Warnings go to stderr; tool hooks stay quiet on stdout.** Claude Code
   surfaces hook stderr as a user-visible warning; stdout becomes part of
   the model's input context (which would pollute the conversation with
   bookkeeping noise). The hot-path tool hooks produce no stdout on success.
   The SessionStart dispatcher is the exception: it emits a Codex/Claude
   `hookSpecificOutput` JSON object whose `additionalContext` contains the
   one-line state banner.

The retained legacy shell wrappers additionally avoid `set -e`, `set -u`, and
`set -o pipefail`; wrap CLI calls with `|| true`; and carry a header-level
<200ms target. Those wrapper-only constraints are compatibility guidance, not
the active dispatcher's measured performance contract.

### Why non-blocking

A blocking hook on `PreToolUse: Edit` would freeze the agent on every
edit — eventually the agent would learn to route around the hook by using
`Bash + sed` instead, defeating the discipline. By contrast a warning + an
audit-log entry captures the same signal without breaking flow: humans see
the warning during review, the `events.jsonl` row supports post-hoc
conflict detection, and the agent keeps moving.

The same principle applies to the CLI degradation path. When the dispatcher
cannot resolve state, it silently skips. When a legacy wrapper cannot find the
`anvil` binary or its `hook` sub-app returns non-zero (database locked,
subcommand not yet implemented during a phased rollout), the wrapper falls
back to a direct file write or silent skip — the session is never broken
because a backing service is unavailable.

---

## `hooks.json` mapping

The six entries in [`hooks/hooks.json`](https://github.com/fakoli/anvil/blob/main/hooks/hooks.json)
call the shell-free dispatcher. `heartbeat` runs on both `PostToolUse`
matchers. The `.sh` files listed here are the legacy wrappers for the same
behavior:

| Event | Matcher | Manifest command | Legacy wrapper | Timeout |
|---|---|---|---|---|
| `SessionStart` | (all) | `anvil hook dispatch detect-state` | [`detect-state.sh`](https://github.com/fakoli/anvil/blob/main/hooks/detect-state.sh) | 5s |
| `PreToolUse` | `Edit\|Write\|NotebookEdit` | `anvil hook dispatch check-claim` | [`check-claim.sh`](https://github.com/fakoli/anvil/blob/main/hooks/check-claim.sh) | 5s |
| `PostToolUse` | `Edit\|Write\|NotebookEdit` | `anvil hook dispatch record-file-change` | [`record-file-change.sh`](https://github.com/fakoli/anvil/blob/main/hooks/record-file-change.sh) | 5s |
| `PostToolUse` | `Edit\|Write\|NotebookEdit` | `anvil hook dispatch heartbeat` | [`heartbeat.sh`](https://github.com/fakoli/anvil/blob/main/hooks/heartbeat.sh) | 5s |
| `PostToolUse` | `Bash` | `anvil hook dispatch capture-evidence` | [`capture-evidence.sh`](https://github.com/fakoli/anvil/blob/main/hooks/capture-evidence.sh) | 5s |
| `PostToolUse` | `Bash` | `anvil hook dispatch heartbeat` | [`heartbeat.sh`](https://github.com/fakoli/anvil/blob/main/hooks/heartbeat.sh) | 5s |

Each default manifest command receives the Claude Code/Codex hook payload as
JSON on stdin. The dispatcher parses it and calls `anvil hook ...`, which
resolves project state exactly as every other command does — the HOME workspace
by default, or `ANVIL_STATE_LAYOUT=local` / `ANVIL_ROOT` when set. The hooks do
not assume an in-repo `./.anvil`.

---

## Per-hook reference

### `detect-state` dispatcher (SessionStart)

**Purpose.** On session start, detect the project language (Rust, Python,
TypeScript, or unknown) by inspecting marker files (`Cargo.toml`,
`pyproject.toml`, `setup.py`, `package.json`, `tsconfig.json`) and inject a
one-line state banner into the session-start context. The shell-free dispatcher
prints a JSON object with `hookSpecificOutput.hookEventName="SessionStart"` and
`hookSpecificOutput.additionalContext=<banner>`; plain-text stdout is invalid
for Codex SessionStart hooks.

**Banner format (`additionalContext`).**
- If `.anvil/` is absent:
  > `[anvil] not initialized in this project — run \`anvil init\` to start`
- If `.anvil/` exists and the CLI is available:
  > `[anvil] Language: Python | active-claims:2 ready-tasks:7 blockers:0 prd-status:approved`
- If the loaded hook, packaged plugin manifest, and PATH installation differ,
  the banner adds their versions and schemas plus an action for only the stale
  component. A schema mismatch also reports the database schema and never
  recommends deleting state.
- If `.anvil/` exists but the CLI is missing or returns non-zero:
  > `[anvil] Language: Python | state present, CLI not available — install anvil bin to enable status`

**Side effects.** None. Read-only banner.

**Performance.** The contained `anvil --version` response has a one-second
budget after operating-system process setup. The manifest's five-second hard
timeout bounds the complete hook, including synchronous process creation and
the stdin handshake. Closing the hook process closes its Windows kill job; on
POSIX, the worker verifies and monitors its expected parent PID and kills the
contained process group if that parent exits.
In practice SessionStart fires once per session, so this is the loosest of the
five budgets.

**PATH trust boundary.** To compare the loaded plugin with the independently
installed CLI, SessionStart resolves `anvil` from PATH and executes its
`--version` command inside a time- and output-bounded process job. PATH lookup
is executable authorization, exactly as it is when a user runs `anvil` in a
terminal; containment is not a filesystem or network sandbox. Do not place an
untrusted or project-writable directory ahead of the intended Anvil install on
PATH. Remove the SessionStart entry from `hooks.json` if that trust contract is
not appropriate for the host.

**CLI call.** `anvil status --hook-format` — emits a single line in
the form `active-claims:N ready-tasks:N blockers:N prd-status:STATUS`.

**Source.** [`bin/src/anvil/cli/hooks.py`](https://github.com/fakoli/anvil/blob/main/bin/src/anvil/cli/hooks.py)
for the default dispatcher path, and
[`hooks/detect-state.sh`](https://github.com/fakoli/anvil/blob/main/hooks/detect-state.sh)
for the legacy shell wrapper.

---

### `check-claim` dispatcher (PreToolUse: Edit, Write, NotebookEdit)

**Purpose.** Before any file edit, look up active claims and warn (on
stderr) when the file being modified is in the `expected_files` scope of
**another actor's** claim. Files in the current actor's own claim are
silent.

**Payload extraction.** The dispatcher parses stdin JSON for:
- `.tool_input.path` (Edit, Write) or `.tool_input.notebook_path`
  (NotebookEdit) — the file being modified.
- `.session_id` — used as the actor proxy only when neither `ANVIL_ACTOR` nor
  legacy `ANVIL_GATE_ACTOR` is pinned. A pinned lifecycle actor wins.

**Skip conditions (silent).** The dispatcher exits 0 with no output when any of
the following hold:
- No state resolves for the selected project.
- The payload contains no file path.
- The file is an absolute path outside the project tree (not under `pwd`).

**Warning format (stderr).** When a conflict is detected:
> `[anvil:check-claim] WARNING: file 'src/foo.py' is in the scope of claim 'C00042' owned by 'session-bbb', not 'session-aaa'.`

**Side effects.** None — the CLI subcommand is read-only. It does **not**
append an event to `events.jsonl`; the signal lives only in the agent's
terminal output for this check-claim path.

**Performance.** The manifest launches one shell-free Python dispatcher, which
opens SQLite, calls `list_active_claims()`, and closes. The 5-second manifest
timeout is the hard ceiling; historical Theme 3 subprocess findings apply only
to the unwired legacy wrapper.

**CLI call.** `anvil hook check-claim --file PATH --actor ACTOR`
(defined in
[`bin/src/anvil/cli/hooks.py`](https://github.com/fakoli/anvil/blob/main/bin/src/anvil/cli/hooks.py)).

**Source.** [`bin/src/anvil/cli/hooks.py`](https://github.com/fakoli/anvil/blob/main/bin/src/anvil/cli/hooks.py)
for the active dispatcher, and
[`hooks/check-claim.sh`](https://github.com/fakoli/anvil/blob/main/hooks/check-claim.sh)
for the legacy wrapper.

---

### `record-file-change` dispatcher (PostToolUse: Edit, Write, NotebookEdit)

**Purpose.** After every file edit, append a `file_changed` event to both
the SQLite `events` table and `events.jsonl`. This feeds the
conflict-detection and audit layers with real per-file write data.

**Payload extraction.** The dispatcher parses stdin for `.tool_input.path` (or
`.notebook_path`), `.tool_name`, and `.session_id`.

**Skip conditions (silent).** It exits 0 with no output when:
- No state resolves for the selected project.
- No file path can be extracted from the payload.

**Write strategy.** The dispatcher calls `anvil hook record-file-change --file
PATH --tool TOOL --actor ACTOR` in-process. The subcommand opens a
`SqliteBackend`, builds `action="file_changed"`, and uses the normal locked,
log-first append path before applying the SQLite projection. The legacy shell
wrapper delegates to that subcommand and retains a direct-JSONL fallback for
compatibility when the CLI is unavailable.

**Output.** Silent on success and on every failure path.

**Performance.** The active manifest uses one shell-free dispatcher process.
The backend serializes cooperating JSONL writers; historical wrapper-only
performance findings remain in roadmap Theme 3.

**Source.** [`bin/src/anvil/cli/hooks.py`](https://github.com/fakoli/anvil/blob/main/bin/src/anvil/cli/hooks.py)
for the active dispatcher, and
[`hooks/record-file-change.sh`](https://github.com/fakoli/anvil/blob/main/hooks/record-file-change.sh)
for the legacy wrapper.

---

### `capture-evidence` dispatcher (PostToolUse: Bash)

**Purpose.** After every Bash tool call, check whether the command matches
a verification pattern (substring match against a hardcoded set). If yes,
capture `stdout` / `stderr` / `exit_code` into the explicitly pinned claim's
evidence buffer at `.anvil/.evidence-buffer/<claim-id>.json`. The work packet
supplies `ANVIL_CLAIM_ID` and `ANVIL_ACTOR`; the active claim, exact owner, and
persisted session (when present) must all match. Otherwise the record lands in
`orphan.json`. It can be preserved
later with `anvil submit TASK_ID --output-file <FILE>` only as a descriptive
excerpt; that flag cannot satisfy typed `required_proofs`.

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

**Default dispatcher.** The shipped manifest calls the shell-free Python
dispatcher directly. It consumes the hook payload, preserves full output for
the digest, and refuses to infer a passing result when `exit_code` is missing
or is any JSON type other than an integer. The remaining details in this
section describe the legacy shell
wrapper retained for existing installations.

**Legacy-wrapper payload extraction.** A single `python3` round-trip parses
`.tool_input.command`, `.tool_response.exit_code`, `.tool_response.stdout`,
`.tool_response.stderr`, and `.session_id`. The script previously spawned
seven python processes for this; the consolidation to one was a
hook-perf-budget fix flagged by the hook-critic agent (see the script
header comments).

**Truncation.** Both `stdout` and `stderr` are truncated to 4000
characters in the captured record. The legacy wrapper computes the digest over
the full strings first, then transports only the bounded excerpts and digest to
the CLI. See
[`docs/evidence-buffer.md`](evidence-buffer.md) for the full record schema,
the descriptive `submit --output-file` path, and claim-bound proof import.

**Two-tier write strategy.**
1. **Preferred path.** Shell out to `anvil hook capture-evidence
   --command CMD --exit-code N --stdout-file F --stderr-file F --actor
   ACTOR` (the wrapper also supplies its hidden full-output digest). The
   subcommand resolves only the exact active claim named by the
   inherited `ANVIL_CLAIM_ID`, then checks the exact owner and persisted
   session before writing an attributed record. It never chooses a sole or
   actor-only active claim. Non-Git claims bind the current task and owning PRD
   snapshot; Git claims retain their immutable repository binding.
2. **Direct-write fallback.** If the CLI is absent, returns non-zero, or
   `mktemp` fails, a second `python3` call writes the record directly to
   `orphan.json`. The fallback cannot reach `state.db` from shell cheaply
   enough to honour the <200ms budget, so it always writes to orphan. The
   user may attach it later via `submit --output-file` as descriptive output,
   but must rerun under a claim or import a valid claim-bound artifact to
   satisfy typed proof requirements.

**Side effects.** Appends one line to `.anvil/.evidence-buffer/<claim-id>.json`
or `.anvil/.evidence-buffer/orphan.json`.

**Performance.** Header targets <200ms.

**Source.** [`bin/src/anvil/cli/hooks.py`](https://github.com/fakoli/anvil/blob/main/bin/src/anvil/cli/hooks.py)
for the active dispatcher, and
[`hooks/capture-evidence.sh`](https://github.com/fakoli/anvil/blob/main/hooks/capture-evidence.sh)
for the legacy wrapper.

---

### `heartbeat` dispatcher (PostToolUse: Edit, Write, NotebookEdit, Bash)

**Purpose.** On every matching tool call, renew the acting session's active
claim lease(s) so a lazy lease stays fresh while real work is happening. This
is the only hook wired to **two** `hooks.json` entries — one on the
`Edit|Write|NotebookEdit` matcher, one on the `Bash` matcher — so a heartbeat
fires regardless of which kind of tool the agent is using.

**Actor resolution.** No `--actor` is passed. The CLI resolves the same
identity `anvil claim` used (`resolve_actor`: explicit arg > `$ANVIL_ACTOR` >
`$ANVIL_GATE_ACTOR` > derived `$USER`/fingerprint/`"agent"` + session
discriminator) so the heartbeat renews the lease the current session actually
holds instead of a different actor's.

All hook actor values are local coordination/audit attribution, not
cryptographic authentication. Lease and gate continuation messages carry the
exact owner through safely quoted `--actor` guidance or structured MCP fields.

**Skip conditions (silent).** Exits 0 with no output when:
- No state resolves for the selected project.
- The resolved actor holds no active claims (nothing to renew).
- Renewing a given claim raises (e.g. an already-expired lease) — that claim
  is skipped, not fatal; the next claim/reclaim handles it.

**Side effects.** For each active claim held by the resolved actor and current
session, the normal renewal gate extends the lease only after new hook-observed
file progress or a pending verified claim-bound attestation. A successful
renewal appends `claim.renewed`; without qualifying progress it is a no-op.

**Performance.** The active manifest uses one shell-free dispatcher process
per entry and a 5-second hard timeout.

**CLI call.** `anvil hook heartbeat` (defined in
[`bin/src/anvil/cli/hooks.py`](https://github.com/fakoli/anvil/blob/main/bin/src/anvil/cli/hooks.py)).

**Source.** [`bin/src/anvil/cli/hooks.py`](https://github.com/fakoli/anvil/blob/main/bin/src/anvil/cli/hooks.py)
for the active dispatcher, and
[`hooks/heartbeat.sh`](https://github.com/fakoli/anvil/blob/main/hooks/heartbeat.sh)
for the legacy wrapper.

---

## Troubleshooting

### My hook is not running

- Confirm the plugin is loaded by your Claude Code or Codex session — the
  SessionStart dispatcher fires on every session and will inject either a
  "not initialized" notice or the project state line. If you see neither,
  the plugin may be unloaded or untrusted.
- Inspect `hooks/hooks.json` and confirm its `uv run ... anvil.cli hook dispatch`
  command matches the installed plugin location.
- Run `anvil status` from the same project and confirm it reports an initialized
  resolved state directory. The active hooks use that resolver; an in-repo
  `.anvil/` is neither required nor assumed.

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

The capture-evidence dispatcher only captures commands that match its **fixed**
substring matcher: `pytest`, `ruff check`, `mypy`, `npm test`,
`cargo test`, `bun test`. The matcher is independent of the active task's
`verification.commands` field — adding a new command to a task does not
add it to the matcher.

Checks:
- Run a command whose string contains one of the matcher substrings.
  `uv run pytest` matches (`pytest` substring). `make test` does not.
- Apply the packet's structured `hook_environment` in the tool process. A
  missing/stale `ANVIL_CLAIM_ID`, wrong `ANVIL_ACTOR`, or sibling session sends
  the record to `.anvil/.evidence-buffer/orphan.json`.
- Inspect `.anvil/.evidence-buffer/` for any `*.json` files.
- For recovery from `orphan.json`, see
  [`docs/evidence-buffer.md`](evidence-buffer.md). `--output-file` preserves
  the text only; it does not create a typed proof.

### A hook is too slow

The active path launches one shell-free Python dispatcher process. Its main
cost is Python startup plus the bounded SQLite operation. Each manifest entry
has a 5-second hard timeout. If a hook reaches that ceiling, inspect the
resolved `events.jsonl` or `.evidence-buffer/` rather than assuming a write
completed; rerun a verification command under the packet environment when its
capture record is absent. Historical wrapper subprocess findings are retained
in roadmap Theme 3 but do not describe the active manifest.

### Temporarily disable a hook

There is no per-hook config toggle today. Remove the specific dispatcher entry
from `hooks/hooks.json`, then restart the session so the harness reloads the
manifest. This is an installation-local change and may be replaced by a plugin
upgrade. Renaming a legacy `.sh` wrapper does not disable the active shell-free
entry, and removing project state is not a hook-control mechanism.

---

## See also

- [`architecture.md` → Hooks](architecture.md#hooks-5) — architectural placement of the hook layer.
- [`evidence-buffer.md`](evidence-buffer.md) — the record schema, lifecycle,
  descriptive `submit --output-file` behavior, and claim-bound proof import.
- [`hooks/hooks.json`](https://github.com/fakoli/anvil/blob/main/hooks/hooks.json) — the source of truth for
  event-to-script wiring.
- [`bin/src/anvil/cli/hooks.py`](https://github.com/fakoli/anvil/blob/main/bin/src/anvil/cli/hooks.py) —
  the six `anvil hook ...` subcommands: `dispatch`, `check-claim`,
  `record-file-change`, `capture-evidence`, `heartbeat`, and the opt-in,
  blocking `stop-gate` (not wired by default — see
  [`docs/reference/codex.md`](reference/codex.md) for how to enable it).
