# Drive the Anvil loop

Anvil has two front doors. The first is the **PRD**: author requirements, and
Anvil turns them into a governed, ready-to-execute task queue. The second is the
**loop**: drive that queue from whatever automation a runtime gives you — a shell
`while`, a Claude Code `/loop`, a scheduled Codex automation — so each step runs
Anvil's governed transitions instead of ad-hoc script state.

The PRD **is** the spec. You do not author a workflow file from scratch for the
common case — Anvil already produced the ready queue. The loop's only job is to
**transfer that queue into a runtime** and run the body once per task.

New to terms like the loop, packet, or claim? See the [glossary](../glossary.md).

## 1. PRD → ready queue

Get a queue of `ready` tasks before any loop runs:

```bash
anvil init                  # one-time: create the project's state
anvil prd parse             # read the PRD you wrote at the path init printed
anvil prd assess            # optional advisory behavior/testability feedback
anvil prd review            # draft -> reviewed
anvil prd review --approve  # reviewed -> approved; nothing can be claimed before this
anvil plan                  # generate the task graph (deps, conflict groups)
anvil score                 # score each task on the six dimensions
anvil review tasks          # promote tasks that pass the readiness gate to ready
```

After this, `anvil list --status ready` shows claimable work. (See
[Authoring a PRD](authoring-a-prd.md) for the PRD step and
[Claiming and shipping a task](claiming-and-shipping-a-task.md) for the manual
single-task flow.)

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

One governed task = the same six steps everywhere. `/anvil:execute` wraps them.

```
claim  ->  packet  ->  do the work  ->  capture the proof  ->  submit  ->  apply
```

```bash
# single-winner lease + file-conflict check; on a refusal, show it and stop
claimed="$(anvil claim T001 --actor loop --json)" || { printf '%s\n' "$claimed" >&2; exit 1; }
claim="$(printf '%s' "$claimed" | jq -r '.data.claim.id')"
export ANVIL_ACTOR=loop ANVIL_CLAIM_ID="$claim"    # binds the proof below to this claim
anvil packet T001                     # the contract: criteria, files, verify cmds
# ... implement against the packet ...
uv run pytest -x > pytest.log 2>&1; code=$?        # the packet's verification command
anvil hook capture-evidence --command "uv run pytest -x" \
  --exit-code "$code" --stdout-file pytest.log --actor loop   # record the command proof
[ "$code" -eq 0 ] || exit "$code"                  # a failed check stops before submit
anvil submit T001 --actor loop \
  --commands "uv run pytest -x" \
  --files-changed "src/app/foo.py"    # hand in the evidence; -> needs_review
anvil apply T001 --approve --strict   # the gate; --strict refuses unverified work
```

`submit` hands in what you ran and what you changed. The command proof is the
record `capture-evidence` wrote: the exact command, its exit code and a hash of
its output, bound to the claim named by `ANVIL_CLAIM_ID`. That id is new for
every claim, so read it from the claim rather than typing one in. Run by hand,
plain `anvil claim` prints the same `export` line for you to copy. Without the
variable, or with an id that is not your claim's, the capture still exits 0 but
is bound to nothing, and the gate reports the check as missing. A task with
several verification commands needs one capture for each. If a check fails,
stop before `submit`: `submit` releases the claim, and the task would sit in
`needs_review` with a failing proof.

The capture exits 0 even when it records nothing, so check before you hand in:
[`anvil evidence-preflight T001 --json`](../cli-reference.md#evidence-preflight)
lists each required command proof with `satisfied` true or false.

Anvil's hooks make this capture for you only when the command contains
`pytest`, `ruff check`, `mypy`, `npm test`, `cargo test` or `bun test`
([the matcher](../hooks-reference.md#capture-evidence-dispatcher-posttooluse-bash)).
For any other verification command, such as `go test ./...` or `make test`, and
for every command in a bare shell, run the capture step yourself.

`apply` is the gate: with `--strict` it refuses `--approve` until every required
check has a matching, passing command proof. Leasing + evidence gating are why
parallel loops cannot double-claim or fake "done".

This loop approves its own work. The CLI allows that, and the approval records
who gave it. To keep a person in the loop, leave out the `apply` line: tasks
then stop at `needs_review` until someone approves them.

## 4. Two modes, one primitive

Both modes run the **same body**; they differ only in cadence.

### One-per-invocation

Run the body once for `anvil next`'s task, then exit. The cursor is Anvil's
durable state, so the next invocation resumes from the next ready task. Fits a
scheduled fire (Codex automation), a cron job, or a single CI step.

```bash
# exit 3 = empty (nothing to do, clean); any other non-zero = real error -> propagate
anvil next -q || { rc=$?; [ "$rc" -eq 3 ] && exit 0; exit "$rc"; }
task="$(anvil next --json | jq -r '.data.task.id')"  # read the id, then run the body once
```

### Drain until empty

Loop the body until the queue empties. Fits a self-paced Claude `/loop` or any
shell:

```sh
while true; do
	# exit 3 = drained (clean stop); other non-zero = real error -> propagate
	anvil next -q || { rc=$?; [ "$rc" -eq 3 ] && break; exit "$rc"; }
	# run the body for the recommended task
done
# queue drained
```

The committed [`packaging/loops/ci-drain.sh`](https://github.com/fakoli/anvil/blob/main/packaging/loops/ci-drain.sh)
is the full version. It also skips a lost lease so concurrent drainers don't
abort, stops with the refusal when a claim is refused for any other reason
(such as a PRD that is not approved), and runs the evidence preflight so a task
is never submitted without its proofs.

Durable, leased state makes **both** resumable and safe to run concurrently:
single-winner leases mean two runners never claim the same task, and a crashed
run loses no progress — re-run and it continues from whatever is still `ready`.

### Milestone-sized work

When several related tasks should produce one reviewed delivery, use a coordinator-first
execution bundle instead of forcing one conversational handoff per task. The coordinator
keeps integration in the main loop and may delegate bounded work without transferring
Anvil ownership. See [Coordinating a milestone bundle](coordinating-a-bundle.md).

## 5. Per-runtime adapters

Committed, copy-ready adapters for each mode:

| Runtime | Mode | File |
|---|---|---|
| POSIX shell / CI / cron | drain | `packaging/loops/ci-drain.sh` |
| Claude Code `/loop` | drain | `packaging/loops/claude-loop.md` |
| Codex automation | one-per-invocation | `packaging/loops/codex-automation.md` |

Each adapter references this seam and this body — they only change the cadence
and the per-runtime wiring. For declarative loops **not** derived from a PRD,
`anvil run-workflow <name>` loads `.anvil/workflows/<name>.yaml` and drives each
step through the same governed transitions (create → claim → run → submit
evidence → apply) to completion, then exits — no background process.
