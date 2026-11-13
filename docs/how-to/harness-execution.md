# Run an Anvil task with a configured harness

Anvil can dispatch one ordinary ready task to the registered Codex–Pi
coordination v2 adapter, integrate verified returned files in an isolated native
claim checkout, run the frozen verification commands there, and submit their
real claim-bound proofs. Successful execution ends at `needs_review`; independent
review and explicit final approval remain separate.

Install the coordination plugin and configure its private OpenShell profile
first. Register its installed files once with Anvil (preview by omitting
`--write`):

```sh
anvil run-configure --script /installed/coordination/scripts/coordinate.py --interpreter /absolute/python3 --profile /private/profile.json --write
```

The registration lives only in the owner's global Anvil YAML configuration.
Project configuration cannot replace executable or profile paths. Registration
pins the interpreter, script, extension and profile bytes; it does not qualify
the model or runtime. Changed registered bytes require an intentional reviewed
registration update.
On POSIX, registered inputs and the canonical global configuration target are
read through trusted root/current-owner, non-symlink ancestry. Script and profile
paths cannot contain symbolic links. The owner-selected global configuration and
interpreter keep their existing canonical-target resolution; their resolved
targets receive the same ancestry checks. Group-writable installed files
are supported beneath a protected owner-only directory, provided writable files
have no additional hardlinks. A private directory beneath a reachable writable
ancestor does not make that path safe. These checks also cover missing config
parents before registration writes; they do not change installed permissions or
attest privileged or same-owner writers. Actual Run remains Linux-only; Windows
registration checks aliases and bounded files without claiming POSIX permissions
or Windows ACL qualification.
This initial command refuses replacement and does not yet provide an upgrade
command; a supported expected-registration update is follow-up work. Do not
edit private registration files as a normal upgrade procedure.

In the intended repository:

```sh
anvil run feature:T001 --preview
anvil run feature:T001 --confirm PREVIEW_ID
anvil run-status RUN_ID
```

The preview expires after 300 seconds and displays the exact task, owner,
source, scope, literal commands, default or explicit options and budget in normal
text output. `--json` also includes the registered profile identity. Confirm
uses those saved options and rechecks current material scope. Ready tasks get a
native isolated claim. To use an existing claim, run from its isolated checkout
as its exact actor. Dependencies must be `done`. Bundle members, root sets,
unbounded paths, verification profiles and dirty source refuse in this slice.
New claim actors use Anvil's native Unicode normalization; existing owners must
still match their recorded identity exactly. Initial Run requires shared
workspace State. `ANVIL_STATE_LAYOUT=local` refuses before reservation with
`unsupported_local_isolated_mode`; this command never creates replacement State
for an isolated checkout.

Preview validates the concrete assignment, writable files and source export
before reserving an execution slot or creating a claim. A ready task's preview
source must match the native claim planner's selected source; for an existing
branch, create its normal isolated owner claim first. Preview and confirmation
refresh the native packet sidecar and may perform the packet command's ordinary
stale-claim maintenance. They recheck task material and ownership afterward;
preview is not a purely read-only operation.

Omit model options to use the configured default. Set per-run options only on
the preview:

```sh
anvil run feature:T001 --preview --provider anvil --model llm.primary --thinking low
```

The adapter validates the actual model and thinking capabilities before any
prompt. Unsupported or clamped selections fail closed. `auto` and `dynamic`
are not currently supported. `anvil continue RUN_ID --preview` explicitly
refuses until qualified saved-session support is available; it never silently
starts a fresh conversation.

Initial Run requires Linux subreaper custody and `/proc`; other platforms refuse
before creating a claim. Commands use `/bin/sh -c` in the isolated checkout.
Keep the frozen command set small enough for the remaining native lease and
run budget. Existing verification profiles are refused, not silently ignored.

A known stopped failed verification ends at `failed_stopped`: preserve its
edits and logs, correct the assignment or source through the normal native
workflow, then create a fresh preview. Its old preview remains consumed.
Unknown stop, uncertain submission and partial integration remain held for
owner reconciliation; they are never automatically retired or replayed.

The worker runs independently of the CLI and does not hold an execution-wide
State lock. Each task has an exclusive State-local operational slot. Duplicate
confirmation cannot launch again. Receipts distinguish the Anvil run, native
claim, adapter run and owned processes; adapter model statements are never
native verification proofs. Logs are bounded; overflow is a failed check.
Each capture uses an owned supervisor to stop and reap local descendants,
including processes that start another session. A parent-death signal also
requests cleanup if the capturing process disappears. Command PID and raw exit
status remain separate from the supervisor's identity and cleanup status. Missing
or failed containment evidence blocks proofs and safe slot retirement; process
group absence alone does not establish descendant custody.

Failure preserves edits, output and custody records. An interrupted admission,
partial integration or uncertain local/remote stop is `reconciliation_required`.
Do not delete the slot or retry blindly: inspect retained execution receipts and
reconcile the exact owner first. Killing a local process does not prove OpenShell
cleanup. No automatic reset, commit, push, merge or final approval occurs.

This source journey is separate from installed default-model qualification,
web Run controls and official Continue support. It makes no claim that the
15 ms latency target or large-scale coding-agent execution has been qualified.

For advanced selectors, inspect verified capability metadata separately:

```sh
anvil run-capabilities --json
anvil run-capabilities --refresh
anvil run-status RUN_ID
```

The first command never starts a runtime or sends a prompt. The explicit refresh
runs in the background using the registered adapter's existing exclusive owner
and no-prompt metadata path. It refuses unresolved local custody; cross-project
owner conflicts also remain guarded by the adapter. Inspect its run ID for the
actual outcome. Default Run never depends on this metadata cache.

A verified catalog is fresh for 300 seconds and pins the adapter/profile bytes.
Only selectable model IDs, the effective selection and supported thinking levels
for that exact observed model are exposed. The cache counts omitted catalog entries that
exceed Anvil’s portable selection bounds. An unrelated omitted model or optional
cache storage failure cannot block a valid task return; Run status reports an
unavailable cache. Selected options remain strict. Metadata refresh verifies the
exact registered sandbox and stopped main instance before publishing its cache.
Stale or changed-profile entries do not provide active choices. Other models' thinking levels remain unknown until matching
verified evidence exists; the current metadata command selects the actual idle
model only. Model-specific metadata selection and saved-session Continue remain
follow-up work, not implied capability of this initial slice.

Each Run retains a strict `anvil.execution-request.v1` and
`anvil.adapter-result.v1` envelope around the concrete adapter's task and receipts.
These bind the native claim, source, exact preimages and actual host permission
bits, assignment, selected options, artifact hashes and stopped owner custody.
Contract digests use Anvil's canonical UTF-8 JSON. Local interpreter pinning and
operational status remain separate; neither an adapter result nor sandbox checks
grant native acceptance. Hidden Git index flags and special file-mode bits
refuse rather than hiding source changes or normalizing permissions.
The full native packet output is retained byte-for-byte with its hash. Model
instructions preserve its work content while omitting only active ownership,
update-protocol instructions and the current task's status/update timestamp;
the outer request binds those separately. The same assignment is validated
before and after claiming, allowing only the native checkout path to change.
Oversized instructions fail before reservation rather than being truncated.
