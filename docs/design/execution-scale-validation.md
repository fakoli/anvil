# Bounded execution and proof qualification

Run the committed-source harness with a fresh receipt destination:

```sh
uv run --project bin --locked python benchmarks/execution_scale.py --tasks 1000 --workers 8 --output /tmp/anvil-execution-scale.json
```

It creates a disposable committed Git repository and native event-sourced State
next to the receipt, in `anvil-execution-scale.work`. Existing receipts and
work directories are never overwritten. The harness explicitly selects its own
owner registry; it neither reads nor changes the operator's project State.

## Workload and controls

The fixture creates independent tasks with exact command-proof requirements and
claims them through native, scope-bound execution bundles. Cohorts default to
12 members. All member claims exist before execution starts, so a 1,000-task
run checks 1,000 simultaneous active member claims. The executor admits at most
`--workers` real command subprocesses; one coordinator records all progress and
evidence. This is a qualification harness, not a new product scheduler.

Each subprocess actually executes a deterministic Python assertion for its
assigned task. The harness captures exact command, output bytes, timestamps and
exit status, binds the artifact to the native immutable member claim context,
and passes it through the existing proof loader/verifier and native evidence
event guard. It does not replace a required proof with a successful-looking
string. Captures use the explicit `claim_owner_self_attested` trust mode, not a
claim of independent issuer attestation or application correctness.

The final oracle requires exact member custody and evidence counts, every
successful task in `needs_review`, zero accepted tasks, and byte-equivalent
canonical State after replay. Failed captures remain on disk and do not become
completion evidence. A separate focused regression executes a command that
prints a success-looking message but exits nonzero, checks refusal, and proves
unrelated members still complete.

## Measurement boundaries

The receipt separates setup, scope projection, scoped claims, guarded claim
append, progress append,
executor admission queue, actual command duration, proof-result queue, proof
validation, evidence append, command-end-to-durable-evidence latency, bundle
readiness and replay. Each stage records p50/p95/p99/max and sample count. Command
duration includes interpreter startup and a deliberate 20 ms fixture wait.
Queue latency is included in the execution throughput and separately exposed;
it is never hidden inside a claim that the whole operation meets 15 ms.
`claim_append` measures the actual native append, including its live checks and
reducer. `scoped_claim` also includes Git identity, immutable member context
capture and manager preflight. The 15 ms native-stage gate uses `claim_append`,
progress append, proof validation and evidence append. Full cohort preparation
and end-to-end acknowledgement remain separate measured costs; passing the
native-stage gate does not mean either is below 15 ms.

`peak_active_claims`, executor limit, observed overlapping command intervals,
ledger writers and actual containers are separate fields. This harness reports
zero containers. A thousand active claims do not demonstrate a thousand
executing containers, a thousand writers, or a real application workload.

Use `--durability strict` for log fsync and stronger SQLite synchronization;
the default `relaxed` mode has different crash guarantees. `--events-storage
git` is a separate cell because its content-validation cost differs from local
event storage. Compare only matched modes, source hashes and fixture sizes.

The CLI requires POSIX process-group cleanup, limits tasks to 2,000, executor
slots to 128, cohorts to 12, each command to 30 seconds, retained fixture storage
to 256 MiB, and the overall deadline to at most one hour (15 minutes by default).
On timeout it terminates the owned process group and retains a failed receipt
and the available ledger/captures. Source hashes are checked before and after
the run; changing source cannot produce a validated receipt.

## Qualification results

Scale cells must be recorded with the source commit, file hashes, runtime and
SQLite versions, event/durability mode, resource limits, workload sizes, exact
errors and the retained receipt. No large-scale result is claimed by this guide
until those cells have actually run. The initial small harness regression is
correctness evidence only. Pi/GLM review and OpenShell execution receipts remain
separate from host measurements, browser visual checks and physical-container
qualification.

The harness also runs the canonical snapshot through the existing isolated web
reader worker, retaining its response and size. That read keeps the default
provider limits, 64 MiB scratch-file ceiling, 20-second timeout and 17 MiB output
ceiling. Its installed reader source must match the measured source files.
The `web_snapshot` stage measures this full read separately from replay-oracle
serialization; a failed read makes the qualification fail rather than lifting a
limit. This is provider-read evidence, not a browser rendering test.

Executor adapters may supply `prepare_executor(project)` after the fixture's Git
commit. It returns the exact per-task argv builder used for both the frozen
proof requirement and execution; a mismatch is refused. Preparation time is
reported separately. Commands preserve the caller's existing `HOME` and optional
`XDG_CONFIG_HOME` paths for native CLI configuration without copying credentials.
The snapshot worker instead receives only an allowlisted environment with local
State layout and the explicit fixture path, discarding inherited Anvil context.

The deadline regression uses the public command's minimum ten-second deadline
and a real spawned coordinator with a SIGTERM-ignoring command descendant. It
requires the coordinator to stop, the descendant to stop executing, and a failed
timeout receipt to remain. Escalation targets the entire owned process group
even when its leader already exited on SIGTERM.

## OpenShell workers

`benchmarks/openshell_execution_scale.py` runs the same proof fixture through
actual OpenShell sandbox commands on a local Docker gateway. Supply the existing
image name and its immutable Docker image ID; use `--help` for bounded resource
options. It does not configure or restart the gateway or attach model providers.

Start with two workers. The supported step sequence is 2, 8, 32, then 128;
subsequent runs require the prior successful receipt through
`--previous-receipt`, matching source and image, complete cleanup, available
capacity and the declared native latency gate. Each sandbox has both a workload
and supervisor container, so these counts are reported separately. Both receive
explicit CPU and memory limits; fresh supervisor containers receive restrictive
limits only after their exact owned identity is verified. Host capacity reserves
remain in force even when a requested wave fits its explicit budget.

The committed fixture is uploaded alone and its bytes are verified remotely.
Required proofs bind the exact outer OpenShell command run from the host fixture
checkout. A native file lock serializes commands assigned to each sandbox; outer
command time includes that wait, CLI/network overhead and remote execution. It
does not pretend the remote directory has the host repository identity.

A durable ownership journal records each attempted creation before the RPC and
each observed sandbox/container identity afterward. Cleanup uses only matching
IDs, run labels and images. An interrupted create with no observed identity
remains unresolved even if the current inventory is empty: absence cannot prove
that the gateway will not complete the request later. Deadline or interruption
stops the owned local process group before the parent reconciles that journal.
Failed or uncertain cleanup makes the receipt fail.
