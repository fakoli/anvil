# Evidence buffer

`.anvil/.evidence-buffer/` is a transient, append-only directory used by
the `capture-evidence.sh` hook to record bash-command output between the moment
a verification command runs and the moment `anvil submit` packages that
output into a durable `evidence.submitted` event.

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
  "timestamp": "2026-05-25T14:23:00+00:00",
  "command": "pytest tests/ -v",
  "exit_code": 0,
  "stdout_excerpt": "...up to 4000 chars...",
  "stderr_excerpt": "...up to 4000 chars...",
  "actor": "agent-x"
}
```

`stdout_excerpt` and `stderr_excerpt` are truncated to 4000 characters each
to keep buffer files small and JSONL-friendly. Truncated outputs are still
useful for the sentinel — full output should be saved separately if the
agent's flow needs the long form.

## Lifecycle

| Step | Who | Effect |
|---|---|---|
| 1. Agent runs `pytest` (or other verification command) | Bash tool | `PostToolUse` hook fires |
| 2. `hooks/capture-evidence.sh` shells to `anvil hook capture-evidence` | Hook | One JSON line appended to `<claim-id>.json` (or `orphan.json` if no matching active claim) |
| 3. Agent runs `anvil submit T012 --commands "pytest" --files-changed ...` | CLI | Reads matching buffer file, embeds outputs in `evidence.submitted` event, then **deletes** the buffer file |
| 4. `submit --output-file` provided directly | CLI | The buffer is bypassed; output is taken from the file the agent supplied |

The submit step is the consume-and-rotate operation: it turns the transient
buffer into the durable `evidence.submitted` JSONL event and clears the
buffer file. Any unconsumed buffer files persist on disk until either (a)
the next `submit` against that claim consumes them or (b) the user manually
clears them.

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
`submit` consumes a buffer, the sentinel sees the durable
`evidence.submitted` event in `state.db` and `events.jsonl` — the
intermediate buffer file is gone.

## Cleanup policy

| Trigger | What happens |
|---|---|
| `anvil submit T012` succeeds | `<claim-id>.json` for T012's claim is deleted |
| `anvil release T012` | Buffer file for the released claim is **not** auto-deleted; remains until next submit consumes it or a manual clean |
| `anvil init --force` | The entire `.evidence-buffer/` directory is preserved (it's user data) |
| Process crash mid-write | Append-only JSONL means a torn line is the worst case; subsequent reads skip malformed lines |

## When to manually clean

- After a hard reset of project state (`rm -rf .anvil/.evidence-buffer/`).
- After resolving an orphan-accumulation issue (e.g., a stuck claim was force-released and never resubmitted).
- Before sharing a project state snapshot — the buffer is transient and not part of the canonical audit log.

## See also

- `hooks/capture-evidence.sh` — the bash hook that writes to the buffer.
- `bin/src/anvil/cli/hooks.py::capture-evidence` — the CLI subcommand the hook calls.
- `bin/src/anvil/cli/packet_apply.py::submit` — the consume side that drains the buffer.
- `docs/hooks.md` — the broader hook lifecycle.
