# Evidence buffer

`.anvil/.evidence-buffer/` is a transient, append-only directory used by
the `capture-evidence.sh` hook to record bash-command output between the moment
a verification command runs and the moment `anvil submit` packages that
output into a durable `evidence.submitted` event.

Only a fixed set of verification commands is captured: the hook matches on
`pytest`, `ruff check`, `mypy`, `npm test`, `cargo test`, and `bun test`
(`hooks/capture-evidence.sh`'s `VERIFICATION_PATTERNS`). Any other bash
command exits the hook silently and is never written to a buffer file — it
is not just "not a verification command," it leaves no trace at all. A
future phase may move this list to config.

Documented as part of closing tech-debt-backlog **CL-15** (originally flagged
in PR #41).

## Format

Each file is JSON: one *record* per line in append-only `*.json` files, keyed
by the active claim ID. The hook writes one file per claim:

```text
.anvil/.evidence-buffer/
├── 4F2A.json        # claim 4F2A's captured commands
├── 7B91.json        # claim 7B91's captured commands
└── orphan.json      # commands captured while no active claim matched the actor
```

Each line in a file is one JSON object:

```jsonc
{
  "kind": "command",
  "timestamp": "2026-05-25T14:23:00+00:00",
  "command": "pytest tests/ -v",
  "exit_code": 0,
  "output_sha256": "9f3c...a1",
  "stdout_excerpt": "...up to 4000 chars...",
  "stderr_excerpt": "...up to 4000 chars...",
  "actor": "agent-x"
}
```

`output_sha256` is the SHA-256 of the *full* (untruncated) stdout+stderr,
computed by `anvil hook capture-evidence` before truncation — it lets the
`CommandProof` attest to output that was never fully persisted. `kind` is
written by the hook but not currently read back; `timestamp`, `command`,
`exit_code`, and `output_sha256` are the fields the submit-side reconciler
(`_read_command_proofs` in `packet_apply.py`) actually reads, and it
silently **skips** any record missing one of them (e.g. a pre-SL-3 line with
no `output_sha256`) rather than failing `submit`. A record missing
`output_sha256` would simply be dropped, not embedded as evidence.

`stdout_excerpt` and `stderr_excerpt` are truncated to 4000 characters each
to keep buffer files small and JSONL-friendly. Truncated outputs are still
useful for the sentinel — full output should be saved separately if the
agent's flow needs the long form.

## Lifecycle

| Step | Who | Effect |
|---|---|---|
| 1. Agent runs `pytest` (or other verification command) | Bash tool | `PostToolUse` hook fires |
| 2. `hooks/capture-evidence.sh` shells to `anvil hook capture-evidence` | Hook | One JSON line appended to `<claim-id>.json` (or `orphan.json` if no matching active claim) |
| 3. Agent runs `anvil submit T012 --commands "pytest" --files-changed ...` | CLI | Reads `<claim-id>.json`, parses each well-formed line into a `CommandProof`, and embeds them in the `evidence.submitted` event's `proofs` field |
| 4. `submit --output-file` provided directly | CLI | The buffer is bypassed; output is taken from the file the agent supplied |

Submit is **read-only** with respect to the buffer: it turns the transient
buffer into the durable `evidence.submitted` JSONL event but does **not**
delete, truncate, or rotate the buffer file afterward. `<claim-id>.json`
persists on disk exactly as it was — submitted lines and all — until the
user manually removes it (or the whole `.evidence-buffer/` directory).

`submit` also auto-releases the active claim (the CLI's human-readable output
prints `Claim ID:  <claim-id> (auto-released)`), so there is no such thing as
a second `submit` reading the same claim's buffer file — a task with no
active claim fails `submit` outright (`no active claim found for task '...'.
Run \`anvil claim ...\` first.`, exit 1). Re-claiming the task afterward mints
a brand-new claim ID, so any further hook-captured commands land in a new
`<new-claim-id>.json`; the original claim's buffer file is never re-read by a
later submit.

## `orphan.json` accumulation

When a bash command runs and **no active claim matches the actor**, the
record goes to `orphan.json`. This commonly happens when:

- An agent runs verification commands before claiming a task.
- An agent runs commands after the claim has been released or has gone stale.
- Multiple agents run concurrently and the hook's actor identity doesn't
  match any owner.

`orphan.json` is currently **never auto-cleaned**. It accumulates indefinitely
until the user deletes it manually:

```bash
rm .anvil/.evidence-buffer/orphan.json
```

This is a known limitation. The recovery path is `submit --output-file`,
which lets an agent point at a specific orphan record (or any file) and
attach it as evidence without going through the buffer. A future
`anvil evidence prune` command could rotate `orphan.json` on a TTL
basis; tracked separately.

## Sentinel interaction

The `sentinel` agent reads the per-claim buffer files when validating evidence completeness. After
`submit` runs, the sentinel sees the durable `evidence.submitted` event in
`state.db` and `events.jsonl` — the buffer file itself is still there on
disk, unchanged, but the sentinel's evidence-completeness checks work off the
durable event, not the buffer.

## Cleanup policy

| Trigger | What happens |
|---|---|
| `anvil submit T012` succeeds | `<claim-id>.json` for T012's claim is read and embedded in `evidence.submitted`; the file itself is **not** deleted |
| `anvil release T012` | Buffer file for the released claim is **not** auto-deleted; it remains on disk indefinitely until a manual clean. Because buffer files are keyed by claim ID and claim IDs are never reused, no future submit reads it again — re-claiming the task writes to a new `<new-claim-id>.json` instead |
| `anvil init --force` | The entire `.evidence-buffer/` directory is preserved (it's user data) |
| Process crash mid-write | Append-only JSONL means a torn line is the worst case; subsequent reads skip malformed lines |

## When to manually clean

- After a hard reset of project state (`rm -rf .anvil/.evidence-buffer/`).
- After resolving an orphan-accumulation issue (e.g., a stuck claim was force-released and never resubmitted).
- Before sharing a project state snapshot — the buffer is transient and not part of the canonical audit log.

## See also

- `hooks/capture-evidence.sh` — the bash hook that writes to the buffer.
- `bin/src/anvil/cli/hooks.py::capture-evidence` — the CLI subcommand the hook calls.
- `bin/src/anvil/cli/packet_apply.py::submit` — the read side that reconciles the buffer into `evidence.submitted`.
- [`docs/hooks-reference.md`](hooks-reference.md) — the broader hook lifecycle.
