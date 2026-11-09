# Read-only web interface

Anvil's native web interface navigates **machine → repository → campaign/PRD →
task**. It reads the same canonical project snapshots as CLI and MCP consumers.
Enrollment stores presentation settings, not tasks, claims or a second database.
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
options, for example `anvil web --config /path/to/config.json list --json`.

Select a repository before a campaign. Switching repositories immediately clears
the previous task set; aborted or late responses cannot populate another
repository's board. The page renders at most 500 cards at once and reports any clipping; choose a
campaign to narrow a larger task set. The API retains the complete bounded
snapshot. Each card retains its exact canonical status, dependencies,
criteria and verification summary. Source details expose repository root,
project identity, configured reader, observed schema, digest and event frontier.
A task in Review or Closed is a workflow state, not proof of a successful
experiment, deployment or permission to start one.

## Reader identity and compatibility

The default reader is the current installed Python/Anvil module running
`project snapshot --json` in the enrolled repository. Module readers use isolated
Python startup (`-I`) so repository modules and `sitecustomize.py` cannot replace
the installed worker/CLI. It clears inherited
`ANVIL_ROOT`, `ANVIL_PRD`, `ANVIL_STATE_LAYOUT` and `PYTHONPATH` so launching from
another harness cannot silently select a different project.

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
path/argv input, task mutation, arbitrary file server or external asset loading.
A private proxy's access policy is the authentication boundary; public exposure
needs separate design and authorization. Real proxy/service configuration and
lifecycle belong in the operator's private deployment system. The foreground
viewer stops with Ctrl-C; it does not install a daemon or boot service itself.

Reads are serialized, with a 20-second child deadline, 17 MiB output ceiling,
64 MiB per scratch-file ceiling on POSIX, and an isolated temporary directory.
Windows retains the output/time limits without the POSIX file-size limit.
Canonical provider limits also apply. At most 16 repositories and four HTTPS
origins are enrolled in a 64 KiB config. Catalog reads contain metadata only;
`GET /api/repositories/ID/snapshot` reads exactly the selected repository.
Idle HTTP sockets have a five-second deadline. A refusal remains visible; the
viewer does not hide it with a previous successful repository's data. This is a
small single-reader foundation, not a concurrent multi-user capacity claim.

## Provenance

The initial Kanban layout, exact-origin controls and read-only workflow derive
from the user-owned LayerSpeak board deliveries
[PR20](https://github.com/fakoli/layerspeak/pull/20) and
[PR21](https://github.com/fakoli/layerspeak/pull/21). The native Anvil integration
adds persisted independent enrollment, repository isolation, canonical payload
validation and packaged CLI/assets. Anvil's existing MIT license and copyright
apply; original source attribution is retained.
