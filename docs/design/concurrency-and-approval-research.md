# Concurrency, proof and bulk approval research

Status: bounded research complete; not an approved execution policy or scale claim.

## User direction — 2026-11-09

- Target hundreds to thousands of tasks or containers executing concurrently.
- Keep Anvil's rigid, machine-checkable proof and ledger consistency guarantees.
- Human review must not serialize independent execution or interrupt every task.
- Allow humans to approve a large, bounded body of work, with individual proofs
  collected and checked at the appropriate scale afterward.
- Provide a supported agent path that keeps work and the ledger moving when the
  board or a human review is unavailable. This is not permission to fabricate
  evidence, skip protected acceptance, or silently lose ledger updates.
- Measure contention, throughput and consistency; distinguish concurrent active
  tasks, concurrent writers, and concurrent proof-verification workloads.
- Research GitHub Spec Kit and other primary sources critically. Adopt useful
  mechanisms only when they fit Anvil; spec-driven terminology is not a goal.
- Continue the Kanban and native-action implementation while this research runs.

## Working hypotheses to investigate

1. Approve an immutable scope and execution policy once; issue task-level leases
   against that authority. Re-approval should be tied to material scope/policy
   changes, not to ordinary progress inside the approved scope.
2. Separate execution permission, proof validation, delivery review and final
   product acceptance. A slow human decision should block only its protected
   boundary and true dependents, while independent ready work continues.
3. Keep individual proof attribution exact: task, claim generation, source
   revision, command, result and artifact identity. Bulk approval must not turn
   one passing sample into acceptance of every member.
4. Use explicit aggregate states that distinguish fully proven, partially
   proven, failed and unknown outcomes. Preserve exceptions for human review.
5. Define the agent escape path as durable native commands and receipts, with
   visible retry/conflict outcomes. An unavailable UI must not become an
   execution dependency. Offline or queued writes would need an explicit
   durability/reconciliation design before adoption.

These are hypotheses, not implemented guarantees or changes to current human
approval requirements.

## Current implementation boundaries

The Kanban work reuses canonical SQLite snapshots and engine events. Browser
previews bind the selected task and evidence rather than the entire project, so
unrelated task changes do not invalidate a pending decision. Web concurrency is
bounded; the existing engine still owns atomic ledger writes.

The direct `task-action` command provides the same guarded non-approval moves
without a browser preview. Existing claim, progress, submit and apply commands
remain authoritative. No new automatic acceptance or bulk approval policy is
being enabled by the UI change.

## Research and validation questions

- What are the current lock boundaries and repeated work on claim, progress,
  evidence submission and review? Which costs grow with ledger history?
- How many simultaneous actors can retain leases and append proofs without
  starvation, lost updates, duplicate decisions or excessive tail latency?
- Can proof validation occur outside the short writer transaction while exact
  inputs are checked again atomically before acceptance?
- How should bounded bulk authority bind scope, policy, reviewer requirements,
  source changes, expiry, revocation and partial failure?
- Which decisions actually require a human, and which are mechanically decidable
  from already-approved contracts? Existing gates must remain until the user
  explicitly adopts a revised policy.
- What does current Spec Kit implement, rather than merely suggest to agents?
- Which scale can this single-host SQLite design demonstrate, and where would
  worker queues, partitioning or another supported backend be justified?

## Evidence to collect

- Native transitions and snapshots on at least 1,000 synthetic tasks.
- Multiple independent writers, per-operation latency and verified event counts.
- Same-task races, changed evidence, expired claims and proof attribution.
- Bounded web admission and responsive independent requests during a slow read.
- Primary-source comparison and a concrete next qualification envelope.

Local synthetic measurements do not establish fleet or thousand-container
capacity. Record workload, worker count, source revision and limitations with
each result.

## First bounded measurements

Run `uv run --project bin --locked python benchmarks/kanban_actions.py --workers 8`.
The initial Python 3.11 run used 1,000 synthetic tasks and checked exact task and
ledger counts after each arm. Setup was excluded.

| Workload | Operations | Throughput | p95 latency |
| --- | ---: | ---: | ---: |
| Native readiness moves, one writer | 100 | 4,005/s | 0.253 ms |
| Native readiness moves, eight writers | 100 | 307/s | 33.720 ms |
| Fresh CLI process per move | 10 | 1.53/s | 678.8 ms |
| Full snapshot plus action availability | 5 reads of 1,000 tasks | 0.718/s | 1,415.2 ms |

These are lightweight `ready` → `drafted` moves, not claim acquisition, evidence
verification, approval or container execution. The eight-writer arm uses threads
with independent backend connections; cross-process/fleet behavior remains to
qualify. Process startup dominates the simple CLI path. Persistent native
clients avoid that cost but retain the same ledger controls.

Source inspection found that `query_only_transaction` takes the same exclusive
event-log lock as append. The follow-up collision test measured writer p95 of 14.2–17.5 ms without a reader
and 1,621–1,637 ms overlapping one board refresh, across three repetitions with
600 verified transitions total. More HTTP threads alone do not remove this lock.

[Source findings and qualification plan](concurrency-source-findings.md) records
lock boundaries, proof costs, history-dependent Git event replay, human gates and
the next process/claim/proof/container experiments. Snapshot coalescing followed
by immutable capture outside the writer lock is the first proposed performance
work; neither optimization nor bulk acceptance is implemented here.

## External comparison

[Current Spec Kit comparison](spec-kit-concurrency-comparison.md) records the
pinned source, official documentation and limitations. Its useful mechanisms
include an approval gate before concurrent fan-out and aggregate review after
fan-in. Its local thread pool and JSON run state do not demonstrate a
thousand-container coordinator. Separate execution, verifier and human-exception
queues are a proposed direction for Anvil, not an enabled policy.


## Kanban reference and small coordination smoke test

Jira's [column configuration](https://support.atlassian.com/jira-software-cloud/docs/configure-columns/)
maps multiple statuses into a column, while [workflow transitions](https://support.atlassian.com/jira-software-cloud/docs/transition-an-issue/)
control legal moves. Its [resolution field](https://support.atlassian.com/jira-cloud-administration/docs/what-is-a-resolution-in-jira/)
records why work ended separately from status. The Anvil board adopts these
patterns while retaining canonical status labels, engine transition guards,
proof-bound approval and explicit non-success closure metadata.

The existing `overlapping_files --quick` coordination benchmark also passed:
12 tasks, six shared-file pairs, four simulated actors, one trial, seed 42.
Anvil recorded zero collisions, zero duplicate completions and a valid final
state; the Markdown control recorded nine collisions, eight duplicate
completions and invalid final state. The Anvil arm took 31.6 seconds. This is a
small correctness smoke test, not fleet capacity evidence. It was qualified
under a temporary test HOME with inherited project overrides cleared; the
first inherited-home run was refused because the host root registry and
synthetic checkout identity did not match. The harness source was unchanged.
