# Concurrent execution journeys

Use a planned execution bundle to authorize a bounded set of independent tasks once, then
let its coordinator submit each member's evidence as work finishes. The browser
and CLI operate on the same native State. Closing the browser does not stop the
coordinator or release its lease.

Execution authorization, evidence submission, independent review and final
acceptance remain separate. A bundle is not a container scheduler: the caller
starts, limits and stops workers. Delegates return their work and proof to the
coordinator; they do not claim bundle members independently.

## Journey map

| ID | User intent | CLI | Web UI | Required result |
| --- | --- | --- | --- | --- |
| J1 | Authorize a bounded batch | `anvil bundle claim B001 --actor lead --expected-scope-digest SHA256_FROM_PLAN` | Review the planned bundle, then confirm its execution preview | One coordinator claim owns the exact members. Changed source or member scope refuses without partial claims. |
| J2 | Keep independent workers busy | `anvil bundle execution-plan B001 --max-workers 8 --json` | Inspect the bundle's execution plan and member counts | A bounded wave contains eligible independent members. Pending review on one member does not exclude unrelated members. |
| J3 | Return individual proof | `anvil submit TASK_ID ... --actor lead` | Refresh the board to see the member in review | Evidence retains task, claim, actor and generation attribution. Submission does not accept the task. |
| J4 | Handle incomplete or refused work | Inspect the command refusal, `anvil evidence-preflight TASK_ID --json`, and the execution plan | Inspect blocked members and their reasons | A failed request remains a refusal; other eligible work can continue. Missing proof cannot satisfy bundle completion. |
| J5 | Review the completed batch | `anvil bundle complete B001 --actor lead`, then independent bundle reviews and `finalize-review` | See completed members awaiting review; use bound task approval when authorized | Completion and review finalization do not implicitly accept members. Final acceptance retains the established human gate. |
| J6 | Continue without the board | `anvil bundle packet B001 --actor lead --format json` | Close the browser, then reopen and refresh later | Native commands continue against the same State and lease. The board reflects their recorded results. |

The execution plan is an advisory snapshot, not a durable dispatch reservation.
Use current coordinator custody and the exact member authorizations from the
work packet. Re-read the plan when work finishes, a lease changes, or an operation
refuses. A nonempty plan does not grant permission to ignore a later engine
refusal.

The current dependency rule still requires `done`. Proof submission alone does
not release dependent tasks. File overlap and conflict groups can also keep
otherwise independent members out of the same wave. Planned capacity is not
measured container capacity.

## Prepare and authorize

Start from reviewed, ready tasks under the current approved PRD. Create a bundle
with an explicit coordinator and the intended member IDs:

```sh
anvil bundle create B001 T001 T002 --prd release --coordinator lead --actor lead
anvil bundle status B001 --json
```

Use the task IDs returned by your project; named PRD tasks may be qualified,
such as `release:T001`. The browser's configured writer actor must match the
persisted coordinator. Its execution confirmation authorizes the displayed
planned bundle, not arbitrary task selection or future task additions.

Choose either the browser confirmation or the native claim command:

```sh
anvil bundle execution-plan B001 --json
anvil bundle claim B001 --actor lead --expected-scope-digest SHA256_FROM_PLAN
anvil bundle packet B001 --actor lead --format json
anvil bundle execution-plan B001 --max-workers 8 --json
```

Copy `scope_digest` from the plan into `SHA256_FROM_PLAN`. This explicit scoped
grant is the same authorization used by the browser. Internal dependencies are
refused before claiming: split them into independent cohorts, letting external
dependencies reach `done` before claiming the next cohort. Existing unscoped
sequential bundles keep their established CLI flow; they are not dispatchable
through this independent-wave projection.

The browser preview binds the project, selected bundle and its source/member scope.
Confirmation checks that binding again through the native claim path. A changed
preview must be reviewed again. Two competing confirmations cannot establish
two owners. After an uncertain response, inspect bundle status before retrying;
a transport failure alone does not prove that no event was committed.

## Execute, submit and review

The coordinator dispatches only the selected wave, using the packet's member
authorizations for proof capture. Once a member's actual verification has run,
submit its evidence using the existing native command. For example:

```sh
anvil submit release:T001 --commands "pytest -q" --files-changed src/first.py --actor lead
anvil bundle execution-plan B001 --max-workers 8 --json
```

This command records supplied evidence; it does not run the test command.
Follow the task's frozen verification contract and typed proof requirements.
Plain command text is not a substitute for a required claim-bound artifact.
See [bundle coordination](coordinating-a-bundle.md) for exact member proof
attribution, lease renewal and recovery.

Native bundle claims capture each member's immutable repository, starting commit,
task/PRD revision, generation and expected-path context. The bundle packet's
`members[].member_claim` carries that context for proof capture. Submit an actual
capture with `anvil submit TASK_ID --commands COMMAND --files-changed FILES
--command-proof-file CAPTURE.json --actor lead`. Use an empty `FILES` value for a
verification-only command. Both the member authorization and coordinator lease
must still be active; a sibling's capture or a released cohort cannot authorize
submission. Historical claims without context need a fresh claim before using
this imported-proof path; their old evidence is not retargeted.

A malformed artifact refuses before successful evidence intake. Preserve the
failed capture and its error. Do not fabricate a successful receipt, rewrite old
evidence for a new claim, or use a second owner to bypass the refusal. The
coordinator can continue independent eligible members while investigating it.

After all members have sufficient completion evidence:

```sh
anvil bundle complete B001 --actor lead
```

Members stay `needs_review`. Scoped grants enforce finalized bundle review before
member acceptance, including replay; omitted acceptance guard fields cannot
bypass this gate. Obtain the required independent reviews and follow
the bundle review/finalization flow. Explicitly authorized member acceptance and
final human project validation remain separate gates. There is no automatic
bulk acceptance or proof-only dependency release in this flow.

New scoped-grant acceptance attempts also require the native finalized review gate
for the submitted member's bundle lineage. Calling task approval directly before
that gate refuses with `bundle_review_required`; closing the browser does not
weaken the rule. Historical acceptance records retain their existing replay
semantics.

## Exercise the journeys

Run the deterministic CLI/HTTP journeys from the source checkout:

```sh
uv run --project bin --locked pytest tests/test_execution_journeys.py -q
```

The fixtures use a disposable Git repository and isolated native State. They
seed through engine events, exercise real CLI commands and local HTTP workers,
and inspect ledger outcomes. They cover browser-to-CLI continuation, independent
proof submission during pending review, stale preview refusal, competing claims,
malformed proof refusal and incomplete bundle completion. They do not launch
containers, qualify a thousand-worker fleet or perform production acceptance.

The integration run also checks read-only access, coordinator identity,
task/bundle confirmation-token separation, and task-approval refusal before the
bundle review gate. A disposable Chrome check exercised batch preview and claim,
CLI submission followed by board refresh, one member awaiting review while its
independent sibling remains dispatchable, keyboard confirmation, a valid drag
preview, and a 390-pixel viewport without document-level horizontal overflow.

For an interactive browser check, use a disposable enrolled repository and
exercise J1–J6 above. Confirm that only valid drag destinations highlight; the
preview names the affected task or bundle; keyboard actions expose the same
confirmation; errors remain visible; and refreshing shows CLI changes. Check
pagination with more than 500 tasks, a narrow viewport, and an open review dialog
while independent workers update the ledger. Record the source revision,
fixture size, browser, observed outcome and any remaining gap.

The native ledger latency target is p95 below 15 milliseconds for the explicitly
measured operation and workload. Report lock wait, mutation duration, offered
load, refusals and correctness checks alongside it. CLI startup, HTTP delivery,
canonical snapshots, browser rendering and model inference are separate latency
measurements. A passing journey is correctness evidence, not a latency or fleet
capacity qualification. See the [concurrency findings](../design/concurrency-source-findings.md)
for the measured baseline and qualification boundaries, and the
[ledger validation](../design/ledger-latency-validation.md) for the implemented
latency results and remaining scale limits. The [execution qualification guide](../design/execution-scale-validation.md)
adds real claim-bound command captures, replay and canonical-reader checks, plus
bounded OpenShell worker qualification.

One bounded run on 2026-11-09 measured the native execution-plan projection and
atomic bundle claim on synthetic State:

| Members | Plan median | Plan p95 | One atomic claim |
| --- | ---: | ---: | ---: |
| 12 | 0.937 ms | 0.967 ms | 5.965 ms |
| 100 | 6.413 ms | 14.135 ms | 33.088 ms |
| 500 | 31.059 ms | 69.413 ms | 232.729 ms |

Each plan cell used 25 in-process reads; each claim value is one observation,
not a percentile. Host load was uncontrolled, and these cells exclude CLI
startup, concurrent writers, proof verification and actual containers. Keep
initial execution cohorts near the existing 12-member default and measure the
real workload before raising limits. Larger atomic claims have not met the
15-millisecond target in this run. Reproduce these bounded cells with:

```sh
uv run --project bin --locked pytest tests/test_bundle_execution_plan.py -q -s -k bounded_large_cohort
```
