# Packet quality (measuring right-sizing) — B51

A work packet is the exact context an agent needs to execute one task (intent,
acceptance criteria, scope, non-goals, verification). **Packet quality is a
first-class, measured workstream**, not an afterthought: a tight, right-sized
packet lets a fast local model skip exploration *and* cuts tokens-to-completion,
which directly buys capacity (the binding constraint on a flat-rate/local fleet).

## Right-sizing

The renderer already routes each task to a **lightweight** or **full** packet by
score (`anvil.context.packets.is_lightweight` — a task is lightweight when both
its complexity and blast_radius are low). The lightweight variant trims the
non-essential sections (e.g. the evidence checklist is sliced to the essential
item) while keeping the load-bearing ones (goal, acceptance criteria, scope,
constraints/non-goals, verification). The full packet keeps everything for
riskier or more complex tasks.

## Measuring the reduction

`anvil.context.packet_metrics` quantifies the token reduction deterministically:

```bash
# Aggregate report over a project's backlog (JSON to stdout):
python -m anvil.context.packet_metrics <state_dir>
```

It reports, per task and in aggregate: the full vs lightweight token counts
(a whitespace-split proxy — absolute numbers approximate, the *relative*
reduction stable), which variant each task is routed to, and the
`as_routed_savings_pct` the fast-lane already delivers vs sending everything
full.

From Python:

```python
from anvil.context.packet_metrics import measure_backlog
report = measure_backlog(backend.list_tasks())
```

## Scope: token reduction here, success-rate lift in the bake-off

This harness measures the **token reduction** half of packet quality — the part
that is measurable without a live model. The complementary **local-model
success-rate lift** (does the right-sized packet let the local model complete the
task correctly more often?) requires a live run and is measured in the **B50
two-week bake-off**, which feeds these results into its note. Together they turn
"fast dumb work" into "fast and sufficient."
