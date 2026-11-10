# Ledger latency validation

The implemented target is **native ledger mutation p95 below 15 ms at a declared
load**, with correctness checked by exact task/event readback. This is not a
15 ms deadline for every operation, a CLI/HTTP response target, or qualification
for thousands of simultaneous ledger writers. Observation: 2026-11-09, candidate
0.6.20, schema25; all State was temporary and synthetic.

Reproduce the eight-process workload from the checkout:

```sh
uv run --project bin --locked python benchmarks/concurrent_ledger.py
```

The default creates 1,000 tasks and 1,008 initial events, then measures 1,000
ready-to-backlog transitions per arm. Each process has its own connection. The
reader arm intentionally starts the writers while a separate board-reader
process holds the ledger lock. Setup and process startup are excluded; full
board computation is reported separately. Each of three repetitions starts from
the same seed. Source hashes and every measurement are in
`benchmarks/fixtures/ledger_contention_2026_11_09.json`.

| Workload | p95 range | p99 range | Worst observed | Result |
| --- | ---: | ---: | ---: | --- |
| 8 writers | 12.01–13.65 ms | 18.84–22.43 ms | 42.50 ms | Meets declared p95 target |
| 8 writers + overlapping board | 13.42–14.41 ms | 22.22–24.85 ms | 46.95 ms | Meets declared p95 target |
| 64 writers | 149.94 ms | 224.79 ms | 383.00 ms | Does not meet target |
| 64 writers + overlapping board | 95.89 ms | 143.20 ms | 178.02 ms | Does not meet target |

The 64-process arm has one repetition per condition. It demonstrates an unmet
target; the difference between its two conditions is not evidence that readers
improve throughput. Host load was not isolated. All 8,000 candidate transitions
matched their intended tasks and exact event counts, with no missing or duplicate
transitions. The candidate's eight-writer arms consumed 0.40–0.49 aggregate
worker CPU seconds per 1,000 moves.

The same process fixture on baseline `8f2b750` had eight-writer worst operation latencies of
321–625 ms without the board and 2,143–2,662 ms with it. Its p95 was lower
(0.62–1.37 ms) because a process could perform many writes while others slept.
The new schedule trades that distribution for much shorter worst operation latency; it is
not an across-the-board percentile improvement. Intermediate 500 ms and 8 ms
retry caps are retained in the local review packet.

## What changed

Snapshot computation now validates a consistent private DB/log capture after
releasing live locks. Exact-byte WAL prefix reuse reduces repeated checksum work
without trusting timestamps or skipping validation. The separate
[snapshot measurements](snapshot-capture-latency.md) show actual lock holds below
6 ms at 10,000 events, but above 24 ms at 100,000 events. The copying cost remains
proportional to history. Cold verification and complete board projection remain
separate, slower work.

Ledger lock retries start at 1 ms and cap at 2 ms with jitter, retaining the
five-second deadline. This increases retry frequency during a long-held lock;
it is a measured local tradeoff, not a claim of cheap polling at unlimited
contention. The process benchmark reports CPU cost and exposes the 64-writer
ceiling. No ledger write is silently dropped or deferred outside its canonical
transaction.

Overlapping web refreshes share one in-flight read. Four read requests, including
coalesced waiters, can wait at once, preserving HTTP-thread availability. The
existing four subprocess slots remain shared: a saturated board can still
return `busy` for a web mutation. There is no hidden freshness cache and no
automatic retry of uncertain mutations. Agents can continue through the native
CLI/MCP; the browser is not an execution dependency.

## Scale and proof boundaries

Independent execution cohorts keep human decisions off unrelated work paths.
A scope-bound claim authorizes its exact members once; each member retains its
own claim and proof. A member awaiting review does not consume dispatch capacity
for another independent member. Final acceptance remains separate and scoped
cohorts require finalized independent bundle review. Internal dependency chains
must be split into successive cohorts; `done` dependency semantics are preserved.

The [journey guide](../how-to/concurrent-execution-journeys.md) records CLI and
browser checks plus 12/100/500-member plan/claim timings. A 12-member plan measured
about 0.94 ms and its single measured atomic claim about 5.97 ms. The 100/500-member
atomic claims took about 33/233 ms and do not meet the target. The 500-member
limit is a native bound, not a recommended latency cohort size.

Thousands of executing containers do not require thousands of simultaneous
ledger writers, but this work does not yet qualify that workload. The next
measured step is sustained, bounded coordinator intake with real claim-bound
proofs and declared offered load. A persistent writer service or immutable
frontier design should be added only if that test establishes the need. Any
such path must retain idempotency, per-task custody, durable receipts and explicit
unknown-outcome handling; an escape hatch must not become a proof bypass.
