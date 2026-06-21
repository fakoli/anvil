# Two-week bake-off runbook (B50) — measure before scaling

**Why.** Every further fleet abstraction (capability `type`/`tier`, profiles, a
shared backend, many-runners) must be pulled into existence by *measured*
mis-routing, not the market metaphor. The economic premise — that **capacity**
(not per-token cost) is the binding constraint — only holds if you frequently hit
rate limits, which is currently **unmeasured**. So: ship the MVP (B45–B49, B51,
B48), run it on the real backlog for two weeks across the flat-rate pools plus the
local box, and let the data decide. **Capacity-pools-as-a-first-class concept is
DEFERRED pending this bake-off.**

> This is the gate for everything bigger. Do not build `type`/`tier`/profiles/a
> shared backend until the numbers below justify them.

## Day 0 — a one-day sanity check first

Before the full run, get ~80% of "pull, self-select, verify" for free:

- label-driven pull via `anvil next -q` (the loop seam) on the real backlog,
- a self-hosted runner for the local box,
- **green CI as the gate** (the cheapest verifier), with the B48 strict-evidence
  + signed-proof layer on.

If even this 1-day check shows the pools never throttle, the capacity premise is
already in doubt — note it and shorten the run.

## What to measure (daily)

### In-engine half — `benchmarks/bakeoff_snapshot.py`

Run once a day and append the JSON to a log:

```bash
python benchmarks/bakeoff_snapshot.py <state_dir> >> bakeoff-log.jsonl
```

It captures: **`needs_review_depth`** (the binding constraint — does review debt
build up?), task status counts, **per-runner accept-rate** (B49 — is local work
accepted or reworked?), and **packet right-sizing savings** (B51
`as_routed_savings_pct`).

### Out-of-engine half — capture by hand / from harness logs

| Metric | How |
|---|---|
| **Per-pool throttle frequency** | Count rate-limit/429s per flat-rate pool per day (Claude $100, Codex $100). *Does the capacity case even exist?* |
| **Spillover frequency to the local box** | How often naive overflow routing fires (a cloud pool throttles → work runs local). *Does naive spillover suffice without a pool concept?* |
| **Local false-pass + rework rate** | Run the SL-2 corpus (`python benchmarks/critic_falsepass.py`) with the **local models** as the critic backend, graded **vs a Sonnet baseline** (`docs/critic-false-pass-baseline.md`). The gap is the local-quality tax. |
| **Review-minutes per task** | Wall-clock spent by the human reviewing each accepted/rejected task. |
| **Cloud-tokens before/after** | Total cloud tokens for the same work with vs without local spillover. |

## Results note

Land a short write-up in `docs/research/` (stub:
[`2026-06-21-bake-off-results.md`](../research/2026-06-21-bake-off-results.md))
answering: did the pools throttle often enough for the capacity case to hold? did
naive spillover suffice (no pool concept needed)? what was the local false-pass /
rework tax? did review debt stay bounded under B49's cap?

## Kill / pivot trigger (record explicitly)

> If a platform ships a portable, exportable, **vendor-neutral proof + state
> format** that off-cloud, non-blessed runtimes can read and write, collapse
> Anvil into (i) the schema spec + (ii) emit/ingest adapters — the durable-state
> and signed-proof layers would then be commodity, and Anvil's remaining value is
> the format + the routing glue.

## Decision gate (what the numbers unlock)

- Pools throttle often **and** spillover suffices → keep the lean spillover model;
  still no pool concept.
- Pools throttle **and** naive spillover mis-routes (measured) → *now* a capacity
  -pool concept is justified — build the minimum the data demands.
- Pools rarely throttle → the capacity premise is weak; refocus on packet quality
  + verification, not pools.
