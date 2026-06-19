# Claude Code adapter — drain the Anvil loop with self-paced `/loop`

Drive Anvil's ready queue from a Claude Code session until it is empty. This is
the **drain** mode of the one primitive: `while a task is ready, run the governed
body`. Anvil is the durable state underneath — leases and evidence survive the
session, so the loop is resumable and safe to run alongside other drainers.

## The seam

`anvil next -q` is the branch point. No `--json`/`jq` parsing needed:

| exit | meaning | loop action |
|---|---|---|
| `0` | a task is ready | run the body |
| `3` | queue is empty | stop — this is success |
| other | real error | surface it, do not keep looping |

(Prefer `anvil next -q` for control flow. Use `anvil next --json` only when you
need the task fields; on an empty queue it returns `{"data":{"task":null}}` at
exit 0.)

## Invoke

Self-paced — omit the interval so the model paces itself one task at a time:

```
/loop drain the Anvil queue: run `anvil next -q`; if exit 0, execute the next
governed task with /anvil:execute; if exit 3, stop and report the count.
```

## The loop body (one governed task per pass)

Each pass runs Anvil's governed transitions. `/anvil:claim` then `/anvil:execute`
already wrap this; the raw flow is:

1. `anvil next -q` — ready? (exit 0) continue; empty? (exit 3) **stop**.
2. `anvil claim <task>` — acquire the single-winner lease + file-conflict check.
3. `anvil packet <task>` — fetch the work packet (the contract: acceptance
   criteria, files in scope, verification commands). Read it before editing.
4. Do the work on the claim's branch; run the packet's verification commands.
5. `anvil submit <task> --commands "<cmds>" --files-changed "<paths>"` — the
   evidence is the typed proof; this auto-releases the claim → `needs_review`.
6. `anvil apply <task> --approve` — the gate (refuses approval when required
   evidence is missing; pass `--strict` to enforce). Then loop back to step 1.

## Why drain mode here

A self-paced `/loop` has no fixed interval, so it keeps pulling the next ready
task until the queue drains — the session-length analog of `ci-drain.sh`. Because
Anvil holds the state, an interrupted session loses nothing: re-run the `/loop`
and it resumes from whatever is still `ready`, with no double-claims.

## See also

- `packaging/loops/ci-drain.sh` — the same drain loop as a POSIX shell skeleton.
- `packaging/loops/codex-automation.md` — the one-per-invocation mode.
- `docs/how-to/drive-the-anvil-loop.md` — PRD → ready queue → both modes.
