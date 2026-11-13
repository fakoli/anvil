# Kanban web interface

Anvil's native web interface navigates **machine → repository → campaign/PRD →
task**. The board reads canonical snapshots from Anvil’s SQLite state through
the state engine, the same source used by CLI and MCP consumers. It does not
scan task markdown files or keep a second task database. Enrollment stores only
presentation settings.
Only the local connected machine is implemented; remote federation is not inferred
from a label, path or Tailscale connection.

## Start once, use from any directory

In an installed build containing this interface:

```sh
anvil web enroll /path/to/repository --label "My repository"
anvil web
```

Open `http://127.0.0.1:8780/`. The dedicated `anvil-web` executable invokes the
same implementation and can be installed in a scoped environment while an older
global `anvil` remains in use. Neither launch command depends on the current
project or creates `.anvil` in the directory where it is launched.

Enroll each existing project explicitly. `anvil web list --json` shows the
persisted catalog; `anvil web remove REPOSITORY_ID` removes enrollment only.
Enrollment refuses inaccessible/uninitialized state rather than initializing or
migrating it. Its owner-private configuration is `~/.anvil/web/config.json`,
written atomically with owner-only POSIX file permissions. Windows uses the
owner profile's file ACLs instead of interpreting POSIX write bits. Run enrollment changes
serially and reload the page afterward. `--config PATH` selects another catalog;
`--port NUMBER` overrides the configured loopback port. These are web-group
options, for example `anvil web --config /path/to/config.json list --json`. Custom catalogs and every containing path must be trusted and protected against replacement: reader argv is executable operator configuration, and final-file mode checks do not establish owner/parent custody.

Select a repository before a campaign. Switching repositories immediately clears
the previous task set; aborted or late responses cannot populate another
repository's board. The page renders at most 500 cards at once. **Previous** and **Next** reach
every task in larger campaigns; the page indicator shows the visible range and
total. Column counts describe the current page. Choosing a repository or campaign
returns to the first page; refreshing keeps the current page when it still exists. The API retains the complete bounded
snapshot. Each card retains its exact canonical status, dependencies,
criteria and verification summary. Source details expose repository root,
project identity, configured reader, observed schema, digest and event frontier.
A task in In review or Closed is a workflow state, not proof of a successful
experiment, deployment or permission to start one. Closed tasks show their
resolution separately from their exact status, including in read-only sessions.

## Move work through the board

The default session is read-only. To enable human workflow actions, launch with
an explicit actor:

```sh
anvil web --allow-writes --actor human
```

Only repositories using this installation’s native reader can be changed. A
separate compatible reader remains read-only; selecting it never grants writer
authority. The actor is recorded attribution, not an authentication mechanism.
Everyone who can use this enabled session acts as that configured actor.

| Column | Canonical task statuses |
| --- | --- |
| Backlog | `proposed`, `drafted`, `reviewed`, `rejected` |
| Ready | `ready` |
| In progress | `claimed`, `in_progress` |
| Blocked | `blocked` |
| In review | `needs_review` |
| Closed | `accepted`, `done`, `closed` |

Drag a card to a highlighted destination, or use its **Move…** button. The
board offers named actions from the engine, not arbitrary status assignments.
Dropping a card opens a preview; the card stays in place until you confirm and
Anvil accepts the change. Escape cancels. Keyboard and touch users can choose
the same moves through buttons.

Available actions depend on the task and its current custody. Drafted or
reviewed work can become Ready when its readiness requirements pass. Ready
work can return to Backlog. Starting, blocking and resuming work require a
current claim owned by the configured actor. Evidence submission, detailed
bundle coordination and protected root operations retain their CLI/MCP paths. A drag does not acquire a lease, run work, fabricate
evidence or skip directly from Backlog to successful completion.

Open **Execution batches** to inspect current bundles, 50 per page. **Previous
batches** and **Next batches** navigate every batch in the selected campaign;
**All campaigns** includes the whole repository. Pages use stable bundle IDs,
so earlier batches leaving the current list cannot skip later batches. Each page
reads current State; **Refresh** starts from the first page to include earlier
additions. A planned bundle
owned by the session actor offers **Preview batch claim**. Confirmation calls the
native claim path with an exact project/source/member scope digest; changed scope refuses
without partial custody. Members must be independent; split dependency chains
into successive cohorts. This authorizes execution under one coordinator lease;
it does not start workers or accept completion. Member counts and the next
advisory wave refresh from the same State used by the CLI. See the
[concurrent execution journeys](concurrent-execution-journeys.md).

To approve submitted work, move an In review card to Closed and inspect the
preview’s task, evidence, submitting actor, commit and commands. **Approve and
close** records acceptance through Anvil’s existing strict `apply` path. Required
independent reviews and your project validation must be complete before you
confirm; the displayed evidence summary alone does not establish those reviews.
**Request changes** returns work to Backlog with a recorded reason.

**Close without completion** records a non-success disposition: Duplicate,
Cancelled, Won’t do, Obsolete or Cannot reproduce. A reason is required;
Duplicate also requires an existing task. An unqualified ID such as `T002`
means the current campaign; `campaign:T002` selects another campaign and
`default:T002` selects the default campaign. This closure does not satisfy
dependent tasks’ successful-completion requirements.

Previews expire after five minutes and are single-use. Confirmation checks the
selected task and evidence again, so another task can advance without
invalidating your preview. A change to the selected task, evidence or enrollment
requires a fresh preview. If a response is uncertain, refresh before deciding
what to do next; the board never retries a mutation automatically.

The web UI is optional for agent throughput. Agents keep using native
claim/progress/submit/apply operations and may use `anvil task-action` for
supported direct moves without a browser preview round trip. The same engine
custody and transition checks apply.

## Run and continue work

With a native adapter already registered, choose **Run…** on an eligible card,
review the preview, and select **Confirm run**. **Default** inherits the adapter's
configuration without sending model or thinking overrides. The server checks
current task scope, source and claim ownership before dispatch. Card availability
is only a hint; a preview can refuse work that is no longer eligible.

Confirmation returns after dispatch, closes the dialog and leaves the board usable.
The selected run's status appears above the board. Successful proof submission
ends in **Awaiting review**, without accepting the work. Independent tasks can
continue during that human review; dependencies and exclusive adapter ownership
still apply. **Make ready**, **Start claimed work**, drag and drop, and approval
never launch a harness themselves.

Expand **Advanced options** to choose an exact provider/model pair and thinking
level. Choices come from verified metadata for that model. **Refresh model
choices…** or **Refresh this model…** opens its own confirmation and performs a
bounded metadata request, not a task run. Reopen Run after metadata is ready.
Unknown or stale metadata disables advanced choices while Default remains
available. Changing a choice invalidates the previous preview. Observed thinking
settings are not defaults, and unsupported levels are never invented or substituted.

To continue saved work, select an inspected run under **Runs in this repository**,
then choose **Continue…** on an eligible task. A saved conversation must be
positively reported by the native engine; the browser never guesses a latest
session. For the same task, explicitly **Request changes**, prepare the revised
scope and **Make ready** before a new Continue preview. An independent approved
follow-up may instead continue the saved conversation under its own fresh claim.
Work awaiting review or already closed does not relaunch itself. Inspect and
commit returned edits through the normal workflow before continuing when native
source checks require it.

**Inspect a known run ID** accepts the exact run ID from the CLI and reads that
one run. It does not scan history, dispatch work or grant authority to Continue.
The configured project writer has the same status access as the native CLI;
actor names are audit attribution, not a separate authentication system. Fresh
Run/Continue admission still checks the current configured actor and claim.

If confirmation loses its response, use **Inspect status**. The page retains a
separate opaque reference to that exact preview after its single-use confirmation
token expires or is consumed. Unconfirmed or incomplete outcomes remain unknown;
there is no automatic retry. Inspection references are bounded to 128 on the
server and in the page; an evicted reference cannot prove whether dispatch
occurred. The CLI remains the durable inspection path after a page or server restart.

Only one visible selected run is polled, with at most one request in flight.
Polling pauses in a hidden tab, stops at a terminal result, and pauses after ten
minutes until **Inspect status** is used again. **Running or interrupted** does
not establish that the worker is alive; **Reconciliation required** stays unresolved.
Hiding status or closing the browser never cancels, restarts or takes custody of
a worker. These controls use the installed native worker and are unavailable for
read-only or custom-reader enrollments.

## Reader identity and compatibility

The default reader is the current installed Python/Anvil module running
`project snapshot --json` in the enrolled repository. Module readers use isolated
Python startup (`-I`) so repository modules and `sitecustomize.py` cannot replace
the installed worker/CLI. It clears inherited
`ANVIL_ROOT`, `ANVIL_PRD`, `ANVIL_STATE_LAYOUT` and `PYTHONPATH` so launching from
another harness cannot silently select a different project. The default uses Anvil’s HOME-workspace layout; a local-only legacy `.anvil` project needs an explicit trusted reader that sets `ANVIL_STATE_LAYOUT=local` itself. Merely selecting another Anvil executable does not select the local layout. If both layouts exist, the default reads HOME state; native per-repository layout selection is deferred.

A repository with newer existing state needs an explicitly selected compatible
reader; this interface never downgrades state, performs a migration, merges an
unrelated feature or silently falls back to another engine. Supply a private
JSON argv once with `enroll --reader-argv` and a descriptive `--reader-label`.
For example, an operator may enroll a repository using an independently pinned
Anvil executable followed by `project snapshot --json`. The executable must be
absolute; argv contains no shell syntax or secrets. Explicit reader argv is
trusted operator configuration: Python module readers should include `-I`, and
executables must come from a reviewed installed environment, not the repository. The public source contains
no private executable, repository, machine or tailnet paths.

Enrollment binds the observed project ID and schema. Later identity/schema
changes refuse until deliberate reenrollment. Canonical payload models and
payload digests are validated; incomplete, malformed or unsupported operation
snapshots refuse. The configured reader label is an operator declaration;
observed schema/API/digest are separate evidence, not a source-signature claim.
A compatibility override does not upgrade the interface's own state engine.

## Access and resource boundaries

The viewer binds only loopback. A trusted private TLS proxy may preserve an
explicitly allowlisted HTTPS origin in the private config:

```json
{
  "schema_id": "anvil.web.config.v1",
  "port": 8780,
  "allowed_https_origins": ["https://board.example.test:8452"],
  "repositories": []
}
```

Merge origin settings into the existing file rather than replacing its enrolled
repositories. Host and Origin checks remain exact; HTTP variants, wildcards,
foreign origins and cross-site API requests refuse. There is no CORS, remote
path/argv input, arbitrary file server or external asset loading. Writes require
the explicit session flag, exact same-origin requests, a session CSRF token and
a validated action preview. Browser input cannot supply executable commands.
A private proxy's access policy is the authentication boundary; public exposure
needs separate design and authorization. Real proxy/service configuration and
lifecycle belong in the operator's private deployment system. The foreground
viewer stops with Ctrl-C; it does not install a daemon or boot service itself.

The server allows eight request threads and at most four isolated reader workers,
with a 20-second child deadline, 17 MiB output ceiling and an isolated temporary
directory. Reader workers also have a 64 MiB per-file ceiling on POSIX; Windows
retains their output/time limits without that file-size limit. Writer workers
retain the output/time bounds but have no per-file limit, so existing state
databases and event logs can grow beyond 64 MiB.
Canonical provider limits also apply. At most 16 repositories and four HTTPS
origins are enrolled in a 64 KiB config. Catalog reads contain metadata only;
`GET /api/repositories/ID/snapshot` reads exactly the selected repository.
Idle HTTP sockets have a five-second deadline. A refusal remains visible; the
viewer does not hide it with a previous successful repository's data. This is a
bounded local board, not a qualified multi-user service. Excess request or
reader capacity returns a visible busy response; it does not create an unbounded
queue. The state engine serializes mutations under its existing writer lock.

## Provenance

The initial Kanban layout, exact-origin controls and read-only workflow derive
from the user-owned LayerSpeak board deliveries
[PR20](https://github.com/fakoli/layerspeak/pull/20) and
[PR21](https://github.com/fakoli/layerspeak/pull/21). The native Anvil integration
adds persisted independent enrollment, repository isolation, canonical payload
validation and packaged CLI/assets. Anvil's existing MIT license and copyright
apply; original source attribution is retained.

Automatic reads run every 30 seconds while the page is idle. Keyboard focus, an
open details panel, dragging, a confirmation dialog or a hidden tab pauses
automatic reads. Turn off **Auto-refresh** to pause explicitly, or use **Refresh**
for an immediate read. Rendering stays bounded at 500 cards per page; campaign filtering and pagination
use the current snapshot without extra network reads or per-card requests.

The interaction follows Jira’s [status-to-column mapping](https://support.atlassian.com/jira-software-cloud/docs/configure-columns/),
[workflow-controlled transitions](https://support.atlassian.com/jira-software-cloud/docs/transition-an-issue/)
and separate [closure resolutions](https://support.atlassian.com/jira-cloud-administration/docs/what-is-a-resolution-in-jira/).
Anvil’s own state and evidence rules remain authoritative.
