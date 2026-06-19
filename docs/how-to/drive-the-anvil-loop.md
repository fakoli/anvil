# Drive the Anvil loop

Anvil has two front doors. The first is the **PRD**: author requirements, and
Anvil turns them into a governed, ready-to-execute task queue. The second is the
**loop**: drive that queue from whatever automation a runtime gives you — a shell
`while`, a Claude Code `/loop`, a scheduled Codex automation — so each step runs
Anvil's governed transitions instead of ad-hoc script state.

The PRD **is** the spec. You do not author a workflow file from scratch for the
common case — Anvil already produced the ready queue. The loop's only job is to
**transfer that queue into a runtime** and run the body once per task.

## 1. PRD → ready queue

Get a queue of `ready` tasks before any loop runs:

```bash
anvil init                 # one-time: create .anvil/ state
anvil prd parse            # PRD markdown -> features + tasks
anvil plan                 # generate the task graph (deps, conflict groups)
anvil score                # score each task on the six dimensions
```

After this, `anvil list --status ready` shows claimable work. (See
`docs/how-to/authoring-a-prd.md` for the PRD step and
`docs/how-to/claiming-and-shipping-a-task.md` for the manual single-task flow.)

## 2. The seam — `anvil next -q`

`anvil next` picks the highest-priority claimable task (dependency-, claim-,
conflict-group- and file-overlap-aware) **without** claiming it. The `-q/--quiet`
flag turns that into a branchable exit code so any shell or automation can loop
without parsing JSON:

| exit | meaning |
|---|---|
| `0` | a task is ready |
| `3` | the queue is empty (success — not an error) |
| other | a real error (no state dir, broken backend) |

Need the task fields too? `anvil next --json` returns
`{"ok":true,"command":"next","data":{"task":{…}}}`, or `{"data":{"task":null}}`
on an empty queue (exit 0). Use `-q` for control flow, `--json` for the id.

## 3. The loop body (already exists)

One governed task = the same five steps everywhere. `/anvil:execute` wraps them.

```
claim  ->  packet  ->  do the work  ->  submit --evidence  ->  apply
```

```bash
anvil claim T001                      # single-winner lease + file-conflict check
anvil packet T001                     # the contract: criteria, files, verify cmds
# ... implement against the packet, run its verification commands ...
anvil submit T001 \
  --commands "uv run pytest -x" \
  --files-changed "src/anvil/foo.py"  # the evidence; auto-releases -> needs_review
anvil apply T001 --approve --strict   # the gate; --strict refuses unverified work
```

`submit`'s evidence (`--commands`, `--files-changed`, optional `--output-file`,
`--pr-url`) is the typed proof. `apply` is the gate: with `--strict` it refuses
`--approve` when required evidence is missing. Leasing + evidence gating are why
parallel loops cannot double-claim or fake "done".

## 4. Two modes, one primitive

Both modes run the **same body**; they differ only in cadence.

### One-per-invocation

Run the body once for `anvil next`'s task, then exit. The cursor is Anvil's
durable state, so the next invocation resumes from the next ready task. Fits a
scheduled fire (Codex automation), a cron job, or a single CI step.

```bash
anvil next -q || exit 0          # exit 3 (empty) -> nothing to do this run
task="$(anvil next --json | …)"  # read the id, then run the body once
```

### Drain until empty

Loop the body until the queue empties. Fits a self-paced Claude `/loop` or any
shell:

```sh
while anvil next -q; do
	# run the body for the recommended task
done
# anvil next -q exited 3: queue drained.
```

Durable, leased state makes **both** resumable and safe to run concurrently:
single-winner leases mean two runners never claim the same task, and a crashed
run loses no progress — re-run and it continues from whatever is still `ready`.

## 5. Per-runtime adapters

Committed, copy-ready adapters for each mode:

| Runtime | Mode | File |
|---|---|---|
| POSIX shell / CI / cron | drain | `packaging/loops/ci-drain.sh` |
| Claude Code `/loop` | drain | `packaging/loops/claude-loop.md` |
| Codex automation | one-per-invocation | `packaging/loops/codex-automation.md` |

Each adapter references this seam and this body — they only change the cadence
and the per-runtime wiring. For declarative loops **not** derived from a PRD, an
`anvil run-workflow` + `.anvil/workflows/*.yaml` path is spec'd but deferred.
