# Provider read contracts, version 1

Anvil exposes two bounded, read-only provider operations for consumers that need
project planning state without opening Anvil's SQLite database or event log:

- `state.project.snapshot` version 1: one atomic, allowlisted project hierarchy.
- `state.prd.content` version 1: exact persisted PRD content, optionally narrowed
  to selected heading sections.

Both operations are JSON-only, append no event, and never initialize, migrate,
repair, or catch up state. A refusal returns one JSON envelope, a non-zero exit,
no partial content, and `truncated: false`.

## Discover and pin the contract

Run this before accessing project state:

```bash
anvil describe --json
```

The response must contain all of the following before a version-1 consumer
continues:

- `api_version == "11"`;
- `operation_catalog.catalog_version == 1`;
- the required `operation_id` with `operation_version == 1`;
- the exact version-1 input, output, and error schema resource paths published
  by that operation.

If the describe API is absent, or any pinned API, catalog, operation, or schema
version differs, refuse **before** accessing state. Do not guess compatibility
from the engine release number and do not fall back to database inspection.

The same manifest is returned by the planning-gated MCP `describe_surface`
tool. Provider reads themselves use the CLI transports recorded in the catalog:
`project snapshot` and `prd show`.

## Packaged schemas and fixtures

The wheel includes the cataloged resources below the Python package
`anvil._data`:

```text
contracts/provider-reads/v1/
  project-snapshot-input.schema.json
  project-snapshot-output.schema.json
  prd-content-input.schema.json
  prd-content-output.schema.json
  read-error.schema.json
  project-snapshot-input.canonical.json
  project-snapshot-output.canonical.json
  prd-content-input.canonical.json
  prd-content-output.canonical.json
  read-error.canonical.json
  provider-read-digests.v1.json
```

Load a resource without depending on an installation path:

```python
from importlib import resources

root = resources.files("anvil._data")
schema = (root / "contracts/provider-reads/v1/project-snapshot-output.schema.json").read_text(
    encoding="utf-8"
)
```

The JSON Schemas use draft 2020-12. The canonical fixtures validate against
them, and `provider-read-digests.v1.json` pins independent byte/digest vectors.

## Atomic project snapshot

```bash
anvil project snapshot --json
anvil project snapshot --json --limit max_tasks=1000 --limit max_dependency_edges=5000
```

The success envelope's `data` is schema
`anvil.state.project-snapshot.v1`. It contains the operation/API/state-schema
versions, applied limits, event frontier, project/PRD/feature/task hierarchy,
and `snapshot_digest`. It excludes full PRD Markdown, source paths, raw
verification commands and manual steps, proof/evidence bodies, claims, and
provider/configuration secrets.

Caller limits may only lower these immutable provider ceilings:

| Limit | Version-1 ceiling |
|---|---:|
| PRDs | 128 |
| features | 4,096 |
| tasks | 50,000 |
| dependencies per task | 512 |
| dependency edges | 200,000 |
| acceptance criteria per task | 256 |
| verification summaries per task | 256 |
| string bytes | 65,536 |
| verification-summary label bytes | 4,096 |
| canonical JSON depth | 128 |
| canonical snapshot bytes | 16,777,216 |
| complete response bytes | 16,842,752 |
| diagnostic bytes | 4,096 |

An unknown limit, malformed value, or attempted increase refuses atomically.
For example, this is a refusal rather than a raised ceiling:

```bash
anvil project snapshot --json --limit max_tasks=50001
```

The snapshot digest is lowercase SHA-256 over:

```text
b"anvil.project-snapshot.v1\0" + canonical_json_bytes(payload)
```

The event cursor hashes the complete converged committed event frontier. An
excluded claim/evidence event can therefore change the cursor without changing
the allowlisted snapshot digest.

### Verification summaries are inert

Each task may expose only grouped `{kind, label, count}` summaries:

| `kind` | Fixed `label` | `count` means |
|---|---|---|
| `command` | `Automated checks` | command records |
| `manual_step` | `Manual checks` | manual-step records |
| `required_evidence` | `Required evidence` | required-evidence records |
| `typed_proof` | `Typed proofs` | typed-proof requirements |

Labels are provider-owned readable data, not executable instructions. Consumers
must render them as inert text. Raw commands, manual instructions, proof
payloads, evidence output, paths, and secrets are deliberately excluded. The
summary-record ceiling is 256 per task, each label is capped at 4,096 UTF-8
bytes, and the overall snapshot/response ceilings still apply.

## Revision-bound PRD content

```bash
anvil prd show default --json
anvil prd show default --json --section Summary
anvil prd show default --json --section Summary --section Goals --max-bytes 65536
```

The read uses the exact source bytes persisted with the reported PRD revision;
it never rereads the mutable authoring file. Full reads preserve LF/CRLF,
NFC/NFD, and non-BMP UTF-8 bytes. Re-encoding the returned JSON `content` as
UTF-8 reproduces `returned_size_bytes` and the exact selected bytes.

Use `--expected-digest` for optimistic freshness:

```bash
anvil prd show default --json --expected-digest 2b1b3fab185e4627f05131d808c1844c1a0b1ac908b5c2495a9a4f0afe323b49
```

A different persisted source returns `stale_digest` with no `content` field.
Legacy PRDs without revision-bound provenance return `content_unavailable`;
Anvil does not fabricate content from a current filesystem file.

The immutable returned-content ceiling is 2,097,152 bytes and the response
ceiling is 16,842,752 bytes. `--limit`/`--max-bytes` may lower, never raise, the
content ceiling. Over-limit reads return no prefix and `truncated: false`.

### Section selectors

`--section` is repeatable. A selector is an exact, case-sensitive,
slash-delimited ATX heading path, excluding the root `# Project:` heading.
Escape a literal `~` as `~0` and `/` as `~1` in a path segment. Headings inside
backtick or tilde fences are ignored. Requested sections must be known, unique,
and non-overlapping; output is concatenated in source order, not request order.

For a full read the canonical selector is
`{"kind":"full","paths":[]}`. For selected sections it is
`{"kind":"sections","paths":[...source-order paths...]}`. The content digest
is lowercase SHA-256 over this unambiguous preimage:

```text
b"anvil.prd-content.v1\0"
  + source_digest.encode("ascii")
  + b"\0"
  + canonical_json_bytes(selector)
  + b"\0"
  + exact_returned_bytes
```

The packaged digest fixture pins both full-document and selected `Summary`
vectors.

## Refusals and consumer behavior

Core error fields validate against `anvil.state.read-error.v1` and use cataloged
stable codes. The CLI envelope also adds `truncated: false` to prove that no
prefix was returned. Treat unrecognized codes as failures. Never display
`message` as trusted markup or execute author-controlled payload fields. A
typical error envelope is:

```json
{"ok":false,"command":"prd show","error":{"code":"stale_digest","message":"The expected PRD digest is stale.","field":"expected_digest","actual":null,"limit":null,"schema_id":"anvil.state.read-error.v1","truncated":false}}
```

No refusal contains a partial snapshot, content prefix, resolved path, raw
exception, or source text.

## Workbench mapping fixture

[`workbench-provider-v1.json`](workbench-provider-v1.json) is the
provider-owned compatibility fixture. It maps every version-1 PRD, feature, and
task hierarchy field to its provider path, explicitly marks runtime `run` data
as consumer-owned/unavailable in provider read v1, and records fail-closed
examples for absent or incompatible API, operation, and schema versions. A
Workbench implementation may consume this fixture without requiring a change
to the Workbench repository.
