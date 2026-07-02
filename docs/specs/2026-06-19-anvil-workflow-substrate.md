# Anvil as the Workflow Substrate — the second front door (WF-1 / WF-2 / WF-3)

**Date:** 2026-06-19
**Status:** Draft — WF-1/WF-2 building now (this PR); WF-3 deferred
**Plugin:** `anvil`
**Tracks:** roadmap integrity-track `WF-1`/`WF-2`/`WF-3` (the "runtime-neutral workflow" axis, [`docs/roadmap.md` § "Theme: Workflow substrate"](../roadmap.md)); extends `SL-7` (workflow-step spike); sits on `SL-3` (typed proof); closes `SL-4` (status-file → events)
**Depends on:** `SL-7` spike (the governed-step posture) for WF-3; `SL-3` (`ProofArtifact`) for WF-3's typed per-step gate. WF-1/WF-2 depend on nothing new — they expose loop seams over the lifecycle that already ships.
**Breaking:** NO. WF-1 adds one optional flag; WF-2 adds committed docs/adapters; WF-3 is additive and deferred.

---

## 1. Goal

Give Anvil a **second front door**. Today the only way in is the PRD —
`PRD → parse → review → plan/score → tasks → claim → packet → submit evidence →
apply`. That is the greenfield path, and it is excellent at what it covers. But
the way work actually gets *launched* now is harness-native orchestration: loops,
fan-outs, scheduled sweeps. Those primitives are ephemeral and harness-specific.
Anvil is the durable, governed state + audit layer underneath them.

The second front door is the **workflow/loop itself**. The job is not to author a
workflow file from scratch for the common case — Anvil already turns the PRD into
a ready queue. The job is to **transfer/drive that ready queue into whatever loop
or automation a runtime offers**, so each step runs Anvil's governed transitions
(single-winner lease, file-conflict check, typed evidence gate). One primitive —
"one governed task per invocation" — drives every runtime; durable state makes it
resumable and safe under concurrency.

This spec formalizes three backlog items:

- **WF-1** — the `anvil next -q` exit-code seam (the single missing bit). **Built
  in this PR.**
- **WF-2** — committed loop adapters (Claude `/loop`, Codex automation, CI/cron
  drain) + a how-to. **Built in this PR.**
- **WF-3** — `anvil run-workflow` + a `.anvil/workflows/*.yaml` declarative path,
  for *ad-hoc loops not derived from a PRD*. **Deferred / spec-first.**

## 2. Context — the shift, and why now

### 2.1 The shift: the PRD is one front door, the loop is the other

Anvil's PRD path is the greenfield story: a human authors requirements, the
planner expands and scores them, agents claim and ship. It is the right tool when
the work *starts* from a written spec. But two things are true:

1. **The PRD IS the spec.** For the common case you do not write a separate
   workflow file — the PRD already produced a ready queue. A workflow file would
   duplicate it. So the second front door's job is to *drive the existing ready
   queue*, not to re-declare the work.
2. **Most real work is not greenfield.** "For each failing test, claim a fix, run
   it, gate on green"; "sweep the brownfield repo for TODOs and resolve each one";
   "every weekday morning, advance one task." These are *loops over a queue*, and
   they are roughly the ~75% of work the PRD front door does not naturally launch.

The second front door covers that ~75% without abandoning the governed lifecycle:
the loop drives the same `claim → packet → work → submit → apply` transitions the
PRD path drives.

### 2.2 The runtime landscape (why the substrate must be runtime-neutral)

Harness-native orchestration primitives are powerful but ephemeral and
harness-specific. The three that matter
([`docs/research/agent-workflow-formats.md`](../research/agent-workflow-formats.md)):

| Runtime | Orchestration unit | Authoring | Control flow | Committed artifact | Triggers |
|---|---|---|---|---|---|
| **Claude Code dynamic workflows** | a JS program orchestrating subagents | code (session-authored `.js`) | first-class (`parallel`/`pipeline`/loops) | the `.js`, but session-authored, harness-specific | in-session / background |
| **Codex automations** | one prompt per scheduled fire | UI form / NL; no committed file | none author-controlled (single agent run) | none for automations (the Codex GitHub Action YAML is a *separate* committed surface) | schedule only (cron) |
| **OpenAI Agents SDK** | your code calling `Runner.run` on sub-agents | code, runs in your infra | first-class (`asyncio.gather`) | none (it *is* code) | you wire them |

The common thread: each keeps intermediate state in script variables (or in the
model's own context) and **throws it away when the session ends**. None is a
durable, governed, cross-tool state layer. That is the whitespace Anvil already
occupies for *state*; this spec extends it to *workflow*. The substrate is
runtime-neutral by construction — the runtime drives, Anvil governs.

### 2.3 The loop body already exists

The per-task body is not new. It is the lifecycle the `execute` skill
([`skills/execute/SKILL.md`](https://github.com/fakoli/anvil/blob/main/skills/execute/SKILL.md)) already wraps:

```
claim   → single-winner lease + file-conflict check   (cli/claim.py:25)
packet  → the contract that teaches the steps          (cli/packet_apply.py:74)
work    → implement against the packet's acceptance criteria
submit  → evidence is the typed proof; auto-releases the claim → needs_review
                                                       (cli/packet_apply.py:216)
apply   → the gate; refuses --approve when evidence is missing
                                                       (cli/packet_apply.py:475)
```

Everything the second front door needs is here *except one thing*: a way for a
plain shell or a non-Claude automation to ask "is there work?" and branch on the
answer **without parsing JSON**. That is WF-1.

## 3. THE SEAM — `anvil next -q`

### 3.1 What was already there

`anvil next` ([`bin/src/anvil/cli/claim.py:500`](https://github.com/fakoli/anvil/blob/main/bin/src/anvil/cli/claim.py))
recommends the highest-priority claimable task without claiming it. It already
behaves correctly on an empty queue: with `--json` it emits
`{"ok": true, "command": "next", "data": {"task": null}}` and **exits 0** — an
empty queue is not an error. That is the right contract for a JSON consumer.

The gap: a jq-less shell or a Codex automation prompt cannot branch on `task:
null` without parsing JSON. The loop condition wants an **exit code**, not a
payload.

### 3.2 The change (WF-1 — built in this PR)

Add a `-q` / `--quiet` flag to `anvil next`
([`bin/src/anvil/cli/claim.py:513-562`](https://github.com/fakoli/anvil/blob/main/bin/src/anvil/cli/claim.py)) that
prints nothing and uses the exit code as the signal:

| exit | meaning | loop action |
|---|---|---|
| `0` | a task is ready | run the body |
| `3` | the queue is empty | stop — this is **success**, not failure |
| other | a real error (state dir missing, backend broken) | propagate |

The implementation is one branch, deliberately minimal (the seam is the exit
code, nothing else):

```python
# bin/src/anvil/cli/claim.py
quiet: bool = typer.Option(
    False, "-q", "--quiet",
    help="Print nothing; exit 0 if a task is claimable, 3 if the queue "
         "is empty. Loop seam for jq-less shells.",
)
...
if quiet:
    # ponytail: the exit code is the loop seam (`while anvil next -q`).
    raise typer.Exit(0 if task is not None else 3)
```

Exit `3` is chosen as a non-`1` sentinel so "empty queue" is distinguishable from
"real failure" (`1`) — a `while anvil next -q; do …; done` loop stops cleanly on
`3` under `set -e` without masking a genuine error. Tests assert both arms
([`tests/test_cli.py:2275-2285`](https://github.com/fakoli/anvil/blob/main/tests/test_cli.py)): `-q` exits 3 and
prints nothing on an empty queue; exits 0 and prints nothing when a task is ready.

This is the whole of WF-1. It is the single missing bit that turns the existing
`anvil next` into a loop seam.

## 4. The loop body (the governed per-task flow)

One pass of the loop runs Anvil's governed transitions over the task `anvil next`
recommended. Each line is an existing call site — the body composes seams that
already ship; it adds no new engine method.

1. **`anvil next -q`** — ready? (exit 0) continue; empty? (exit 3) **stop**.
   ([`cli/claim.py:500`](https://github.com/fakoli/anvil/blob/main/bin/src/anvil/cli/claim.py))
2. **`anvil claim <task>`** — acquire the single-winner lease + file-conflict
   check. If another runner won it, the claim fails loudly; the next pass/fire
   retries. ([`cli/claim.py:25`](https://github.com/fakoli/anvil/blob/main/bin/src/anvil/cli/claim.py))
3. **`anvil packet <task>`** — fetch the work packet: the contract that teaches
   the steps (acceptance criteria, files in scope, verification commands, prior
   decisions, output contract). Read it before editing.
   ([`cli/packet_apply.py:74`](https://github.com/fakoli/anvil/blob/main/bin/src/anvil/cli/packet_apply.py))
4. **Do the work** on the claim's branch; run the packet's verification commands.
   The `capture-evidence.sh` PostToolUse hook records exit codes and output into
   the claim's pending-evidence buffer automatically.
5. **`anvil submit <task> --commands "<cmds>" --files-changed "<paths>"`** — the
   evidence is the typed proof; this writes the `Evidence` row, auto-releases the
   claim, and moves the task to `needs_review`.
   ([`cli/packet_apply.py:216`](https://github.com/fakoli/anvil/blob/main/bin/src/anvil/cli/packet_apply.py))
6. **`anvil apply <task> --approve [--strict]`** — the gate. `--strict` refuses
   `--approve` when required evidence is missing (exit 1), so a step cannot ship
   unverified. ([`cli/packet_apply.py:475`](https://github.com/fakoli/anvil/blob/main/bin/src/anvil/cli/packet_apply.py))
   Then loop back to step 1.

The `execute` skill ([`skills/execute/SKILL.md`](https://github.com/fakoli/anvil/blob/main/skills/execute/SKILL.md))
already wraps steps 3–5 for a Claude session; the adapters in §6 drive the same
body from other runtimes.

## 5. Both execution modes are the SAME primitive

"One governed task per invocation" is the only primitive. The two modes differ
only in **cadence**, not in mechanism:

### 5.1 Drain until empty

```sh
while anvil next -q; do
    <body>   # claim → packet → work → submit → apply
done
# anvil next -q exited 3: the queue is drained.
```

A Claude self-paced `/loop`, or any POSIX shell. The loop keeps pulling the next
ready task until the queue drains.

### 5.2 Fire once

Run the body exactly once for `anvil next`'s task, then exit. A Codex automation
fire, a CI job, a cron tick. There is no in-prompt loop — **the queue itself is
the cursor**: each fire advances one task, the next fire resumes from durable
state.

### 5.3 Why they are the same

Durable, leased state collapses the distinction. Drain mode is just fire-once
called repeatedly with the queue as a persistent cursor. Both are:

- **Resumable.** An interrupted drain (or a missed cron fire) loses nothing —
  re-running picks up whatever is still `ready`.
- **Safe under concurrency.** Single-winner leases mean two drainers (or a CI
  drain + a Codex fire) never claim the same task; evidence gating means a step
  cannot fake "done."

This is the load-bearing property: the same primitive serves a tight interactive
loop and a sparse scheduled cadence because Anvil — not the runtime — holds the
state between invocations.

## 6. Per-runtime adapters (WF-2 — built in this PR)

Because governance lives in Anvil, each adapter is thin: it drives the WF-1 seam
and the existing body. The adapters are committed under
[`packaging/loops/`](https://github.com/fakoli/anvil/tree/main/packaging/loops) so they are reviewable, copyable
skeletons, not prose.

### 6.1 CI / cron drain — `packaging/loops/ci-drain.sh`

A POSIX-shell skeleton: `while anvil next -q; do <body>; done`. The loop
*condition* is the seam; the loop *body* is the governed per-task flow with a
`REPLACE_WITH_…` executor line the adopter fills in. Runs the gate with
`anvil apply <task> --approve --strict` so CI cannot ship unverified work. Safe to
run as cron/CI and safe to run concurrently with other drainers.

### 6.2 Claude Code — `packaging/loops/claude-loop.md` (drain mode)

Drive the queue from a self-paced `/loop`:

```
/loop drain the Anvil queue: run `anvil next -q`; if exit 0, execute the next
governed task with /anvil:execute; if exit 3, stop and report the count.
```

The model paces itself one task at a time; `/anvil:claim` + `/anvil:execute`
already wrap the body. Because Anvil holds the state, an interrupted session
resumes on re-run with no double-claims — the session-length analog of
`ci-drain.sh`.

### 6.3 Codex — `packaging/loops/codex-automation.md` (fire-once mode)

A Codex automation is a scheduled NL prompt that runs one agent per fire with no
author-controlled fan-out — which matches "one governed task per invocation"
exactly. The committed prompt advances exactly one task and stops, branching on
the seam (exit 3 → "no ready tasks," nothing to report; non-3 non-zero → report to
Triage; exit 0 → run the body). The next fire resumes from durable state. Codex's
approval mode gates the `apply` step (report-only stops at `submit`; allow-writes
runs `--approve --strict`).

### 6.4 The how-to (WF-2)

`docs/how-to/drive-the-anvil-loop.md` — referenced by all three adapters; **shipped
in this PR (WF-2).** It documents the one-primitive / two-modes story end to
end: PRD → ready queue → "fire once" vs "drain until empty," with the seam table
and the body, pointing at the three adapters as the runtime-specific instances. It
slots beside the existing
[`docs/how-to/using-anvil-on-any-harness.md`](../how-to/using-anvil-on-any-harness.md)
(MCP/CLI install) as the *driving* counterpart to that *installing* guide.

## 7. Why this strengthens Anvil

This is not feature width — it is making the central claim more true.

1. **Exercises the wedge under real load.** Single-winner leases, file-conflict
   detection, and the evidence gate are proven today only by the concurrency suite
   ([`tests/test_claims_concurrency.py`](https://github.com/fakoli/anvil/blob/main/tests/test_claims_concurrency.py))
   and the benchmark. Driving them from real parallel loops proves them *in situ*:
   a regression becomes visible the instant a loop double-claims or ships
   unverified.
2. **A second entry point.** A loop drives (and WF-3 can create) tasks, covering
   the ad-hoc / brownfield ~75% the PRD front door misses — "for each failing
   test, claim a fix, run, gate on green."
3. **Collapses the fakoli-flow / fakoli-crew trinity.** fakoli-flow's phases
   become loop passes; fakoli-crew's agents become per-step executors; and
   coordination becomes Anvil **events** instead of grep-parsed markdown status
   files — the concrete payoff of `SL-4`
   ([`docs/roadmap.md:131-138`](../roadmap.md)). One substrate, not three.
4. **Correct, not just fast.** Every step is leased + evidence-gated, so parallel
   loops *cannot* double-claim or fake "done." Anvil makes the loop trustworthy —
   which is the performance that matters (fewer redone tasks, no silent
   conflicts), not raw throughput.
5. **Projection survives the session.** A runtime's discarded intermediate state
   persists as `Evidence` / `Decision` rows (the `SL-7` posture-3 projection),
   so the audit trail outlives the loop that produced it.

## 8. Where it sits on the roadmap

"Runtime-neutral **workflow**" — the natural next axis after "runtime-neutral
**state**." Concretely:

- It **grows `SL-7`** (the workflow-step spike,
  [`docs/specs/2026-06-19-sl7-workflow-adapter.md`](2026-06-19-sl7-workflow-adapter.md))
  from a recorded spike into a product axis. WF-1/WF-2 are the "beside" and
  "governed step" postures made routine; WF-3 is the productionized governed
  runner the spike's finding feeds.
- It **sits on `SL-3`** (`ProofArtifact`,
  [`docs/specs/2026-06-19-sl3-proofartifact.md`](2026-06-19-sl3-proofartifact.md)):
  WF-3's per-step `proof` gate is a typed `ProofRequirement`, so a loop step's
  success asks "does a passing `CommandProof` exist" rather than trusting prose.
- It **closes `SL-4`** (status-file → events): coordination across loop steps is
  Anvil events, retiring the grep-parsed markdown status protocol.

## 9. Backlog items

### WF-1 — `anvil next -q` exit-code seam — **BUILDING NOW (this PR)**

The single missing bit (§3). Add `-q`/`--quiet` to `anvil next`: exit 0 if a task
is ready, exit 3 if the queue is empty, print nothing. Turns the existing command
into a loop seam usable from any plain shell. Already implemented at
[`bin/src/anvil/cli/claim.py:513-562`](https://github.com/fakoli/anvil/blob/main/bin/src/anvil/cli/claim.py) with tests
at [`tests/test_cli.py:2275-2285`](https://github.com/fakoli/anvil/blob/main/tests/test_cli.py).

### WF-2 — committed loop adapters + how-to — **BUILDING NOW (this PR)**

Worked, committed adapters driving the WF-1 seam from each runtime (§6), plus the
how-to. Adapters live under
[`packaging/loops/`](https://github.com/fakoli/anvil/tree/main/packaging/loops): `ci-drain.sh`, `claude-loop.md`,
`codex-automation.md`, plus the how-to (`docs/how-to/drive-the-anvil-loop.md`) —
all shipped in this PR. No engine change — these document and skeletonize the
existing body over the WF-1 seam.

### WF-3 — `anvil run-workflow` + `.anvil/workflows/*.yaml` — **DEFERRED / SPEC-FIRST**

A committed, harness-neutral declarative workflow format and a governed runner —
for **ad-hoc loops not derived from a PRD only**. The common case (a PRD's ready
queue) is fully covered by WF-1/WF-2 driving the existing queue, so a declarative
file would duplicate the PRD. WF-3 is built only if non-PRD loops earn it.

Sketch of the format (deliberately small primitives; `run`/`fan_out`/`claim`/
`proof`/`needs`/`on_fail`):

```yaml
# .anvil/workflows/fix-flaky-tests.yaml
name: fix-flaky-tests
trigger: { schedule: "0 9 * * 1-5" }          # manual | schedule(cron) | on-event
description: Find flaky tests, fix each in parallel, gate on green.
steps:
  - id: find
    run: "List flaky tests (retry markers in CI logs)"
    output: { schema: { type: array, items: { type: string } } }   # typed
  - id: fix
    fan_out: ${{ steps.find.output }}          # one governed task per item (parallel)
    claim: { expected_files_from: item }       # exercises lease + file-conflict
    run: "Fix the flaky test: ${{ item }}"
    proof:                                       # typed gate (SL-3 ProofRequirement)
      - command: "uv run pytest ${{ item }}"
        passing_exit_codes: [0]
    on_fail: reopen                              # loop-until policy
  - id: report
    needs: [fix]
    run: "Summarize the fixes applied"
```

The runner drives each step through Anvil's transitions
(`claim → run → capture typed proof → submit evidence → apply`); `fan_out`
creates + claims N tasks in parallel (the real parallel-claim stress on the
wedge); the whole run is event-sourced. An escape hatch lets a step point at a
code workflow when declarative is not enough. WF-3 extends the `SL-7` governed
runner and depends on `SL-3` for the typed `proof` gate.

## 10. Phased path

| Phase | Item | What ships | Status |
|---|---|---|---|
| **P0** | `SL-7` | One governed step (`anvil workflow-step`) + projection postures, as a recorded spike | spec'd (separate spec) |
| **P1** | **WF-1** | `anvil next -q` exit-code seam | **this PR** |
| **P2** | **WF-2** | Committed adapters (CI drain, Claude `/loop`, Codex automation) + how-to | **this PR** |
| **P3** | **WF-3** | `.anvil/workflows/*.yaml` + `anvil run-workflow` (sequential), then `fan_out` + `loop-until` | deferred / spec-first |

WF-3's `fan_out` + `loop-until` is the real proof of the wedge under parallel
claim load, and is gated on `SL-3` for the typed-proof step. WF-1/WF-2 ship first
because they expose loop seams over the lifecycle that already ships, with no
engine change and no new format to maintain.

## 11. Open questions (decide before building WF-3)

- **Spec format.** YAML (above), or stay in the PRD-as-markdown family for one
  mental model? The PRD-as-spec refinement argues against introducing a second
  authoring surface unless non-PRD loops clearly need it.
- **Task lifecycle.** Does a WF-3 workflow CREATE tasks, bind to existing PRD
  tasks, or both? (For WF-1/WF-2 this is moot — they drive the PRD's existing
  ready queue.)
- **Triggers for non-Claude runtimes.** Cron declared in the spec, or delegated
  to the harness (Codex automation schedule, GitHub Action `on:`)? WF-2 delegates
  to the harness today; WF-3 must decide whether to own the schedule.
- **Control-flow ceiling.** How much (`fan_out` + `loop-until` + `needs`) before
  the declarative format is "just code"? Keep it declarative with a code
  escape-hatch; resist growing a DAG engine (consistent with `SL-7` §3.3 and
  SL1-RR-1 §6 "not a background process").
- **Concurrency proof.** What is the acceptance bar that `fan_out` actually
  stresses the wedge — a test where N parallel claims over overlapping
  `expected_files` produce exactly one winner and zero lost evidence, mirroring
  the existing concurrency suite?

## 12. Out of scope

- A long-running daemon, scheduler, or DAG engine. WF-1/WF-2 add no background
  process; WF-3's runner runs steps and exits (consistent with `SL-7` §3.3 and
  SL1-RR-1 §6).
- Re-running or re-verifying a step's commands independently of the runtime's
  observation (the gate trusts the hook/`CommandProof`; independent re-execution
  is a future `SL-3` hardening).
- A generic adapter SDK or plugin API. WF-2 ships three concrete, copyable
  adapters; a fourth runtime copies the pattern, it does not register against an
  API.
- WF-3 itself in this PR — it is deferred, spec-first, and built only if non-PRD
  loops earn it.

## 13. References

- `bin/src/anvil/cli/claim.py:500` (`next`), `:513-562` (the `-q`/`--quiet` seam),
  `:25` (`claim`)
- `bin/src/anvil/cli/packet_apply.py:74` (`packet`), `:216` (`submit`),
  `:475` (`apply`, incl. `--strict/--no-strict`)
- `bin/src/anvil/claims/manager.py:135` (`next_claimable`)
- `tests/test_cli.py:2275-2285` (the `-q` exit-code tests)
- `packaging/loops/ci-drain.sh`, `packaging/loops/claude-loop.md`,
  `packaging/loops/codex-automation.md` (the WF-2 adapters)
- `skills/execute/SKILL.md` (the `execute` skill wrapping the body)
- `docs/how-to/using-anvil-on-any-harness.md` (install counterpart to the driving
  how-to)
- `docs/research/agent-workflow-formats.md` (the runtime landscape; the substrate
  framing)
- `docs/roadmap.md` § "Theme: Workflow substrate" (WF-1/WF-2/WF-3 entries), `:34-68` (the reframe and
  the three integration postures), `:131-138` (SL-4)
- `docs/specs/2026-06-19-sl7-workflow-adapter.md` (the spike this grows from)
- `docs/specs/2026-06-19-sl3-proofartifact.md` (`ProofRequirement` for WF-3's gate)
- `docs/specs/2026-06-01-sl1-rr-1-event-sourcing-write-path.md` (event-sourced
  log the projection/audit trail rides on; "not a background process")
