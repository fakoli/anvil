# Coordinating a milestone bundle

Execution bundles let one coordinator carry a related set of existing Anvil tasks to one
reviewed delivery without turning every task into a separate conversational handoff. Anvil
owns the durable state, leases, evidence lineage, review gate, and delivery checkpoint. It
does not spawn, await, cancel, or monitor subagents for you.

Use a bundle when the coordinator needs to keep integration reasoning in the main loop and
any delegation can be bounded. Keep using ordinary task claims when work is genuinely
independent and separate accepted deliveries are desirable.

## Invariants

- One coordinator owns the bundle claim and all Anvil mutations for its members.
- Claiming the bundle atomically creates internal member authorizations. Do not separately
  run `anvil claim` for those tasks.
- A progress note is an audit heartbeat, not evidence and not a subagent heartbeat.
- Every member still needs fresh completion evidence bound to the current bundle member
  authorization. Historical evidence remains readable but cannot complete a new bundle.
- Reviewers must be distinct from the coordinator and from one another. Required angles,
  review count, and re-review budget come from the bundle policy.

## Coordinator-only workflow

This is the default and simplest mode. The coordinator performs each member in dependency
order and keeps all integration decisions local.

```bash
anvil bundle create B001 T001 T002 --prd release \
  --coordinator lead --actor lead \
  --required-angle correctness --required-angle security \
  --required-angle integration
anvil bundle status B001
anvil bundle claim B001 --actor lead --shared-tree
anvil bundle packet B001 --actor lead

# Work and verify each member, then submit its evidence through the normal task surface.
anvil submit T001 --commands "pytest tests/test_one.py -q" \
  --files-changed src/one.py --actor lead
anvil submit T002 --commands "pytest tests/test_two.py -q" \
  --files-changed src/two.py --actor lead

anvil bundle complete B001 --actor lead
anvil bundle review B001 --round 1 --angle correctness \
  --decision approve --actor reviewer-a
anvil bundle review B001 --round 1 --angle security \
  --decision approve --actor reviewer-b
anvil bundle review B001 --round 1 --angle integration \
  --decision approve --actor reviewer-c
anvil bundle finalize-review B001 --actor lead

anvil bundle checkpoint B001 --commit "$COMMIT_SHA" --actor lead
anvil bundle reconcile B001 --commit "$COMMIT_SHA" --actor lead
```

On MCP, the equivalent operations are `create_bundle`, `claim_bundle`,
`generate_bundle_packet`, ordinary `submit_completion_evidence` for each member,
`submit_bundle_progress(..., phase="implemented", complete=true)`, three
`record_bundle_review` calls, `finalize_bundle_review`, `checkpoint_bundle`, and
`reconcile_bundle`. Renew or release a coordinator lease through `renew_claim` or
`release_task` with `target_kind="bundle"`.

## Bounded delegation

Delegation is optional and remains a harness concern. A useful pattern is:

1. The coordinator claims and reads the aggregate packet.
2. Delegate one bounded, non-overlapping member or one read-only investigation with a
   concrete return contract: patch or findings, commands run, files touched, and deadline.
3. Continue independent coordinator work instead of waiting indefinitely.
4. Record an audit note, for example:

   ```bash
   anvil bundle progress B001 delegated-review \
     --member-task T002 --detail "delegate-a; return patch+tests by 14:30Z" \
     --actor lead
   ```

5. Validate and integrate the returned work in the coordinator loop. The coordinator runs
   the verification and submits the member evidence.

Keep the wave within `--max-tasks` and `--max-serial-stages`. Anvil's
`delegated_agents` field is observational state; it does not gate the lifecycle, and the
public CLI/MCP surface does not manage child-agent processes. If a delegate stalls, take
the member back into the main loop and record the recovery rather than waiting without a
deadline.

## Recovery semantics

| Situation | Safe response |
|---|---|
| A delegate stalls while the coordinator lease is active | Take the member back, record progress, renew the bundle lease, and continue. |
| The coordinator stops while the bundle is `active` | `anvil bundle release B001 --reason "handoff" --actor lead` marks the bundle `replan_required`. Members that still have active authorizations return to `ready`; members already submitted remain `needs_review`. Release is not pause/resume. |
| The coordinator lease expires while the bundle is `active` | The next lease-sensitive mutation reaps active coordinator/member claims, resets only actively authorized members, and marks the bundle `replan_required`. A stale lease cannot be renewed. |
| The coordinator claim is released or expires after `bundle complete` | Do not expect a reset: the bundle and submitted member statuses remain in review state, but finalization requires an active coordinator claim. The public surface cannot reclaim that bundle; create an eligible replacement and supersede the stranded source. |
| `bundle complete` reports `bundle_not_ready` | Fix the per-member blockers. The failed completion is retry-safe and appends no progress event. |
| Review gate is incomplete and the review cap has room | Add distinct reviewers using the still-missing angles. Assign required angles before dispatching reviewers; once the cap is consumed, a missing-angle round has no public repair. |
| A reviewer corrects analysis without implementation changes | A later review round may record the corrected verdict when the gate permits it. Review rounds do not mint fresh member evidence. |
| A review requires implementation or evidence changes | Do not edit in place: member submissions already released their authorizations, and no public reauthorization/resubmit path exists. Revise the plan, create an eligible replacement, and supersede the source. |
| Re-review budget is exhausted | Revise the task plan, create an eligible replacement bundle, then supersede the old bundle. |
| Delivery metadata arrives late or is retried | Re-run `bundle reconcile` with the same commit or PR reference; reconciliation is idempotent. |

There is no public transition from `replan_required` back to `planned`. Recovery therefore
uses revised/new task IDs and a replacement bundle:

```bash
anvil bundle create B002 T101 T102 --prd release \
  --coordinator lead --actor lead
anvil bundle supersede B001 --replacement B002 --actor lead
```

The replacement must already exist, be nonterminal, and belong to the same PRD. Current
creation rules also prevent a new nonterminal bundle from reusing tasks still owned by the
old nonterminal bundle. Supersession is therefore a history-preserving redirect to a
revised/disjoint plan, not an in-place regrouping mechanism. Never edit SQLite or rewrite
`events.jsonl` to work around this constraint.

These limits are intentionally explicit: the current public contract has no same-bundle
member reauthorization, no in-place regrouping, and no way to reacquire a coordinator
claim after completion. Treat any recovery that needs new code or new evidence as a new
bundle generation.

## Checkpoints and reconciliation

A checkpoint records delivery metadata; it is not task evidence. The recommended order is
to checkpoint after the review gate passes, when a commit or PR exists. At least one of
`--commit` or `--pr-url` is required, including on `reconcile`:

```bash
anvil bundle checkpoint B001 --commit "$COMMIT_SHA" --pr-url "$PR_URL" --actor lead
anvil bundle reconcile B001 --commit "$COMMIT_SHA" --pr-url "$PR_URL" --actor lead
anvil bundle reconcile B001 --commit "$COMMIT_SHA" --pr-url "$PR_URL" --merged --actor lead
```

Reconciliation advances `reviewed_unintegrated → integrated → merged`. It is safe to retry
with the same references. The public surface does not infer a delivery reference and does
not expose a separate `merged → completed` command.

## Adopting existing tasks without losing history

Bundle adoption is additive. Existing task IDs, dependencies, status history, claims, and
evidence rows stay canonical; `bundle create` adds bundle metadata and ordered membership.

1. Back up state before any schema migration and run the normal migration command if the
   installed version requires it. See [Migrations](../migrations.md).
2. Select unclaimed `ready` tasks from one PRD. Preserve their IDs, order, and dependency
   graph.
3. Create the bundle and inspect `anvil bundle status B001` before claiming it.
4. Claim only after preflight succeeds. Claim validates external dependencies, internal
   acyclicity, conflict groups, and serial-stage limits.
5. Produce fresh evidence for the new member authorizations. Old evidence is preserved for
   audit, but its claim ID belongs to an earlier generation.

Do not rerun `plan`, recreate task IDs, edit dependencies, delete rows, or rewrite events
merely to adopt tasks. A failed create is atomic. Creating a bundle rejects missing or
cross-PRD tasks, actively claimed tasks, and tasks already owned by another nonterminal
bundle.

## Model-neutral comparison protocol

The committed fixture compares `task_per_agent` and `coordinator_first` as execution
policies, not models or vendors. Its shared workload pins the task-graph hash, initial
commit, ordered target tasks, and acceptance commands. Each paired trial must use the same
integer seed and opaque execution-profile ID, and each arm retains a provenance reference.
It then records:

- time to an accepted commit;
- coordinator tokens and delegate tokens;
- accepted tasks per 1,000 total tokens;
- review findings and re-reviews;
- wait time; and
- human interventions.

Run the deterministic protocol with:

```bash
uv run --project bin python benchmarks/bundle_workflow_fixture.py
```

The output contains descriptive summaries and signed deltas only—no weighted score and no
declared winner. Values are fail-closed: impossible metrics, mismatched pairs, and an
accepted result without the full ordered target plus a commit SHA are rejected. If either
policy has an incomplete trial, every cross-policy delta is suppressed. The comparator
hash-checks referenced raw logs and structurally binds acceptance-command results to the
accepted commit; it does not execute those commands or prove that the Git object exists.
The fixture validates metric plumbing; it is explicitly synthetic and is not an empirical
performance claim. See [the benchmark README](https://github.com/fakoli/anvil/blob/main/benchmarks/README.md#coordinator-policy-comparison-fixture) for the capture rules.
