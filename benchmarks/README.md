# anvil coordination benchmark

**The claim this measures:** when multiple agents work one project in parallel,
coordinating through anvil's durable state engine produces *fewer collisions,
fewer duplicates, correct ordering, and auditable evidence* than coordinating through
naive shared-markdown state — and it does so on the real engine, not a model of it.

This is the asset competitors structurally can't produce: spec-kit, task-master, BMAD
and the rest are stateless, so there is no coordination state to measure. anvil
has one, so we can put a number on it.

## Run it

```bash
cd benchmarks
uv run python run_benchmark.py                       # all 4 scenarios, 3 trials -> RESULTS.md
uv run python run_benchmark.py --quick               # fast smoke (1 trial, fewer actors)
uv run python run_benchmark.py --scenarios overlapping_files,evidence_gaming
uv run python run_benchmark.py --trials 5 --seed 7   # more trials / different seed
uv run python run_benchmark.py --live                # (phase-2 stub) real subagents
```

No third-party dependencies. The first run does a one-time `uv sync` of the
`anvil` CLI into `bin/.venv`; thereafter it calls that console script directly.
Results are written to `RESULTS.md` (regenerated each run).

## How it's built — and why you can trust the number

```
harness/
  engine.py        own the real anvil binary; render a PRD from a TaskSpec list;
                   run the real setup pipeline (init->parse->review->plan->score->ready)
  coordinators.py  the ONLY thing that differs between arms:
                     MarkdownCoordinator     — naive shared TODO.md, non-atomic
                     AnvilCoordinator  — real `next`/`claim`/`submit`/`apply`
  scenarios.py     4 scenarios (task set + actor count + failure injection)
  metrics.py       the oracle: pure functions over the recorded facts
  runner.py        run each scenario through both arms over N seeded trials; aggregate
```

The control discipline that makes this an argument rather than a demo: **both arms run
the identical actor loop over the identical task set and the identical "work" function.
The only variable is the coordinator.** Any difference in the metrics is therefore
attributable solely to the coordination layer.

The anvil arm shells out to the *actual* CLI a user runs — `next`, `claim`,
`submit`, `apply` — against a live SQLite state engine. We are not simulating
`BEGIN IMMEDIATE`; we are contending on it.

## The four scenarios

| Scenario | Injection | What it proves |
|---|---|---|
| `overlapping_files` | 12 tasks in 6 file-sharing pairs, 8 actors | Exclusive leasing serializes writes to a shared file (no concurrent clobber) |
| `dependency_ordering` | 3 chains of 3 dependent tasks, 6 actors | The readiness gate stops a task starting before its dependency is done |
| `crash_recovery` | a dead actor locks T001 and vanishes | An abandoned exclusive lease is reaped and the task reclaimed — with zero duplicates |
| `evidence_gaming` | half of all completions skip real verification | Every completion carries a durable evidence record; gamed work is auditable |

## Metrics (the oracle)

Computed after each trial from an append-only instrumentation log + the canonical
SQLite state:

- **file collisions** — a workspace file with two writes from *different actors whose
  time intervals overlap* (a real read-modify-write race; sequential writes by
  different actors, correctly serialized by a lease, are **not** counted).
- **duplicate completions** — a task whose work was performed by more than one actor.
- **ordering violations** — a dependent task whose first write preceded a dependency's
  completion.
- **recovered after crash** — the abandoned task eventually reached `done`.
- **auditable evidence records / gamed detected %** — completions carrying a structured
  evidence record, and the share of gamed completions that record makes visible.
- **final state valid** — the run ended correct on all of the above.

## Engine findings (surfaced by building this)

Running real concurrency against the real engine turned up two genuine bugs. That a
benchmark found them is itself the argument for having one. **Both are fixed in
v1.23.3** (see CHANGELOG); they are documented here because surfacing them is the point.

### Finding #1 — TOCTOU race in the file-overlap claim check

Two file-overlapping tasks can *both* be claimed concurrently (~8% of head-to-head
attempts) because the "do any active claims overlap my files?" read happens **outside**
the `BEGIN IMMEDIATE` transaction that protects the claim insert. The *same-task* claim
is atomic and safe; the *cross-task file-overlap* guard is not.

Repro:
```bash
# two threads, each claiming one of two tasks that share a file; count double-wins
python3 - <<'PY'
# (see git history of this dir for the probe; ~1/12 attempts both succeed)
PY
```
This is why anvil's `overlapping_files` collisions were low but not zero.
**Fixed in v1.23.3:** the file-overlap *and* conflict-group re-checks now run inside the
`BEGIN IMMEDIATE` claim transaction; `overlapping_files` collisions are now **0 across
all trials**, guarded by `tests/test_claim_concurrency.py` (single-winner under 8 threads).

### Finding #2 — CLI `claim` ignores `default_lease_minutes`

`cli/claim.py` constructs `ClaimManager(backend, clock, actor=...)` without passing
`default_lease_minutes`, so the CLI always uses the hardcoded 60-minute default and
silently ignores `config.yaml`. (The MCP server path *does* wire it.) The config loader
also coerces the value via `int(str(...))`, which rejects fractional minutes outright.

**Fixed in v1.23.3:** the CLI `claim`/`renew` now thread `config.default_lease_minutes`
into `ClaimManager`, and the config accepts fractional (sub-minute) leases via `float`
coercion. (The crash scenario still fast-forwards the lease by backdating
`lease_expires_at` — that keeps the run fast regardless, and exercises the real reaper.)

## Caveats (also printed in RESULTS.md)

- **Reproducible-aggregate, not bit-identical.** Real OS-thread concurrency is
  nondeterministic; numbers are means over seeded trials. Re-running reproduces the
  *conclusion* and close numbers, not identical decimals.
- **The evidence gate is advisory.** anvil *flags* unverified work; it does not
  refuse it. The metric is detectability, which markdown structurally lacks.
- **Actors are simulated.** They model the coordination contract (acquire → work →
  complete), not an LLM's reasoning. That isolates the coordination mechanism, which is
  what the claim is about.

## Roadmap — `--live`

`--live` is a phase-2 stub today. It will replace the simulated actor loop with real
Claude subagents (Agent SDK) running against the same scenarios and the same oracle —
trading reproducibility and zero cost for visceral credibility. The metric definitions
do not change; only the actor implementation does.
