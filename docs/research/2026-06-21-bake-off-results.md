# Two-week bake-off results (B50) — STUB, to be filled after the run

**Status:** not yet run. This is the destination for the bake-off note; the
protocol is [`docs/how-to/bake-off.md`](../how-to/bake-off.md). Fill it in after
two weeks on the real repo across the $100 Claude + $100 Codex pools + the local
box.

## Headline questions (answer with data)

1. **Does the capacity case exist?** How often did each flat-rate pool throttle?
   _(fill: throttles/day per pool)_
2. **Does naive spillover suffice?** How often did overflow route to the local
   box, and did it work without any pool concept? _(fill: spillover events/day;
   mis-routes)_
3. **What is the local-quality tax?** Local false-pass + rework rate vs the
   Sonnet baseline (SL-2 corpus). _(fill: local false-pass %, Sonnet baseline %)_
4. **Did review debt stay bounded?** `needs_review` depth over time under B49's
   cap; review-minutes per task. _(fill: from bakeoff-log.jsonl)_
5. **Did spillover save cloud tokens?** Cloud-tokens before/after. _(fill)_

## Raw log

Daily in-engine snapshots: `bakeoff-log.jsonl`
(`benchmarks/bakeoff_snapshot.py`). Out-of-engine metrics: table per
`docs/how-to/bake-off.md`.

## Decision

_(fill: which of the three decision-gate branches the data selected, and what —
if anything — to build next. Record whether the kill/pivot trigger fired.)_
