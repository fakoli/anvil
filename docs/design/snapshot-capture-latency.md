# Immutable snapshot capture and bounded latency

Observed 2026-11-09 on the development host, source based on `8f2b750` plus
the capture/checksum changes described here. This implements the first capture
experiment from [the concurrency findings](concurrency-source-findings.md).
Temporary synthetic local State only; no operator State or serving changes.

Run the bounded history probe from the checkout:

```bash
uv run --project bin --locked python benchmarks/snapshot_capture.py
```

The raw receipt at `benchmarks/fixtures/snapshot_capture_2026_11_09.json`
retains every sample. The probe creates exactly 1,000 tasks, grows history
incrementally through 100,000 events, takes five captures per history size,
then removes its temporary State. It resets the WAL prefix cache before every
capture to model a fresh web subprocess. It measures the actual event-file lock
from successful acquisition through release, including schema confirmation and
SQLite connection teardown. These are capture measurements, not writer p95.

| Events | DB / log bytes | Total capture median / max | Event-lock median / max |
| ---: | ---: | ---: | ---: |
| 1,008 | 1,515,520 / 602,725 | 4.12 / 7.07 ms | 1.57 / 2.96 ms |
| 10,000 | 4,038,656 / 3,237,381 | 36.63 / 37.05 ms | 4.92 / 5.30 ms |
| 100,000 | 29,904,896 / 29,607,367 | 49.91 / 53.12 ms | 24.29 / 26.43 ms |

The roughly 4.1 MB WAL contains different lengths of currently valid frames at
these milestones; its allocated size alone does not predict cold probe cost.
Cold verification still costs CPU outside the writer lock. The 100,000-event
case exceeds a 15 ms lock budget because copying the DB and log grows with
history. This is an explicit measured ceiling, not qualification for arbitrary
history, proof intake, strict durability, Git event mode, or container count.
Five samples cannot establish an end-to-end p95 service objective. Competing
process writers and complete board rendering need their separate measurements.

## Capture contract

`project_snapshot.captured_snapshot_transaction(state_dir)` yields a private
query-only SQLite connection and a read-only copied event log. The existing
`query_only_transaction` performs both schema probes and takes the same event
lock as writers. While locked, capture pins a database read transaction, copies
the exact initial event-log length using buffers no larger than 1 MiB, and uses
SQLite backup to copy the pinned projection. Log file identity and length are
checked before and after copying. The live connection and both live locks are
released before the captured pair is yielded.

Canonical event/projection agreement, log material, hierarchy, limits, digests,
and response validation run unchanged against the private pair. A same-size
rewrite with restored mtime cannot pass by metadata alone: the captured bytes
must still equal their captured database events. The copied DB/log remain
unchanged when another writer commits to live State. Temporary files are cleaned
on success and refusal. Existing web worker scratch limits still apply; the CLI
gains no new total-history cap, and copying remains O(DB bytes + log bytes).

Both provider snapshots and board inspection use capture. Preview and execution
continue to read live State under their original locking and compare-and-set
guards; a captured board view grants no mutation or acceptance authority.

## Exact-byte WAL reuse

Schema inspection still reads current database-header and WAL bytes without
opening live SQLite. The checksum loop uses `struct.iter_unpack` instead of
allocating two byte slices per checksum word pair. A single immutable cache entry
retains at most 16 MiB of checksum-verified WAL prefix plus its cumulative checksum
and latest/committed page-one state. The cache is local to a process.

Reuse requires an exact comparison of the entire verified prefix, including
header and salts. Current headers are checked on every call; changed, shortened,
restarted, or replaced bytes invalidate reuse. Appended frames are verified from
the last good checksum. Incomplete, bad-salt, or bad-checksum tails are never
cached as verified frames and are checked again if repaired or completed.
No stat signature, path, timestamp, or digest grants trust. If the prefix exceeds
the cap, normal full validation continues. The current main-database header is
still used when no committed WAL page-one frame supersedes it.

This lets the cold probe before lock acquisition supply exact verified bytes to
the second probe under the lock. A WAL restart between probes can still require
a cold scan under lock. The cache changes neither schema refusal nor proof
validation. Tests cover capture parity and cleanup, concurrent writer completion,
same-size/mtime-restored log changes, WAL rewrite/truncation/header changes,
new commits, invalid-tail repair, cache ceilings, and main-header fallback.

Further improvement at larger histories needs a qualified pinned-WAL capture or
another immutable frontier design; suppressing validation or trusting file stats
would weaken the snapshot contract.
