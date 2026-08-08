# Attesting progress from an external writer

Use a progress attestation when work changes a claimed file outside Anvil's
file-change hooks. The artifact binds one changed path to the exact claim,
claim generation, repository, PRD revision, task revision, and claim-start
baseline. Anvil verifies the local bytes or Git object before it records the
artifact.

An attestation is renewal evidence, not completion evidence. It does not change
task status and does not replace `anvil submit`.

## Obtain the immutable claim values

Create the claim before the external writer changes an expected path, then save
the structured responses:

```console
anvil status --json > status.json
anvil claim T007 --actor external-writer --json > claim.json
```

The claim response contains `data.claim.generation` and
`data.claim.attestation_context`. The context records the claim-start Git SHA,
repository identity, PRD/task revisions, and each canonical expected path with
its SHA-256 baseline. A missing file has `baseline_sha256: null`.

If `attestation_context` is `null`, the project was not an accessible Git
repository when it was claimed. External attestations are unavailable for that
claim; Anvil preserves legacy hook-observed renewal behavior instead.

## Version 1 wire schema

The input is one JSON object. Unknown fields, duplicate keys, floats, malformed
Unicode, and alternate spellings are rejected.

```json
{
  "envelope_id": "producer-run-42",
  "payload": {
    "schema_version": 1,
    "kind": "file",
    "project_id": "my-project",
    "claim_id": "C1234ABCD",
    "generation": 2,
    "task_id": "T007",
    "task_revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "prd_id": "default",
    "prd_revision": 4,
    "claimed_by": "external-writer",
    "repository_id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "claim_start_sha": "cccccccccccccccccccccccccccccccccccccccc",
    "commit_sha": "cccccccccccccccccccccccccccccccccccccccc",
    "path": "src/feature.py",
    "prior_sha256": null,
    "file_sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
    "issued_at": "2026-08-08T19:30:00Z"
  }
}
```

The optional signed form adds an `issuer` sibling to `payload`:

```json
{
  "envelope_id": "producer-run-42",
  "payload": {"...": "the complete payload above"},
  "issuer": {
    "algorithm": "ed25519",
    "signer_id": "16-lowercase-hex-characters",
    "public_key": "64-lowercase-hex-characters",
    "signature": "128-lowercase-hex-characters"
  }
}
```

`FileProgressPayload` and `CommitProgressPayload` have the same complete key
set shown above; neither kind permits an omitted or additional field. Their
discriminated contracts are:

```text
FileProgressPayload   = payload with kind="file"
                        and commit_sha=claim_start_sha
CommitProgressPayload = payload with kind="commit"
                        and commit_sha=<full descendant commit object id>
```

For both variants, `path`, `prior_sha256`, and `file_sha256` remain mandatory
(with only `prior_sha256` allowed to be JSON `null`). The distinction is which
repository object Anvil independently reads and hashes.

Field meanings:

| Field | Contract |
|---|---|
| `envelope_id` | Producer-chosen non-empty identifier. It is audited but is not part of semantic identity or the signature. |
| `schema_version` | Integer `1` exactly; booleans are not integers here. |
| `kind` | `file` for current working-tree bytes, or `commit` for one path in a descendant commit. |
| `project_id` | Exact `data.project_id` from `anvil status --json`. |
| `claim_id`, `generation`, `task_id`, `claimed_by` | Exact values from the active claim. A later claim generation cannot replay an older artifact. |
| `task_revision`, `prd_id`, `prd_revision`, `repository_id`, `claim_start_sha` | Copy verbatim from `claim.attestation_context`. |
| `path` | One path in `attestation_context.expected_paths`, using its canonical `/`-separated spelling. Absolute paths, `..`, empty segments, Windows device names, alternate data streams, and unsafe trailing dots/spaces are rejected. |
| `prior_sha256` | Copy that path's `baseline_sha256` verbatim, including `null` for a path absent at claim start. |
| `file_sha256` | Lowercase SHA-256 of the raw current file bytes (`file`) or raw Git blob bytes at `commit_sha:path` (`commit`). It is not a text-normalized digest. |
| `commit_sha` | For `file`, exactly `claim_start_sha`; local `HEAD` must still equal it. For `commit`, the full object ID of a commit descended from `claim_start_sha` in which `path` is a changed regular blob. |
| `issued_at` | Timezone-aware ISO 8601 time at or after claim creation, not in the verifier's future, while the claim is active and unexpired. |

Both kinds require a real change from `prior_sha256`. For `file`, the verifier
reads the contained regular file in the current working tree. For `commit`, it
resolves the exact full commit ID, proves claim-start ancestry, and hashes the
regular Git blob without checking out the commit.

## Canonical bytes, digest, and signature

The file must be exactly Anvil canonical JSON bytes:

- UTF-8 without a BOM or trailing newline;
- object keys sorted lexicographically;
- no whitespace between tokens (`separators=(",", ":")`);
- Unicode emitted as UTF-8 rather than ASCII escapes;
- only JSON null, booleans, signed 64-bit integers, strings, arrays, and
  string-keyed objects; floats are forbidden;
- at most 262,144 decoded bytes.

Use `anvil.state.hashing.canonical_json_bytes`; ordinary pretty-printed JSON is
not accepted. Let `evidence_core` be the complete payload with only `issued_at`
removed. Semantic identity is:

```text
hex_sha256(b"anvil.progress-attestation.v1\0" + canonical_json_bytes(evidence_core))
```

`issued_at`, `envelope_id`, and `issuer` therefore cannot change the semantic
digest. This makes two reports of the same claim-bound file/commit evidence the
same semantic evidence even when their wrapper or reporting time differs.

For a signed envelope, the Ed25519 signature preimage is exactly
`canonical_json_bytes(payload)`. There is no additional signature-domain
prefix. `signer_id` is the first 16 lowercase hex characters of SHA-256 over
the raw 32-byte public key. The embedded public key or fingerprint must appear
in `$ANVIL_TRUST_LIST`, or in `~/.anvil/trust.txt` when that variable is unset,
one value per line. A signed artifact that is valid but not configured as
trusted is rejected.

That trust configuration is also an operational replay dependency. Live append
and every later state replay revalidate the embedded fingerprint, signature,
and current issuer membership using `$ANVIL_TRUST_LIST`, or
`~/.anvil/trust.txt` when the variable is unset. If that file is missing, moved,
or changed so the recorded issuer is no longer trusted, replay fails closed.
Back up the applicable trust-list file with the Anvil state and restore it at
the same configured path before rebuilding or moving the workspace. Retain at
least the recorded issuer's full public key or fingerprint; never back up or
distribute the private signing key as verifier state.

An envelope without `issuer` is recorded as
`claim_owner_self_attested`. A trusted valid signed envelope is recorded as
`configured_issuer_verified`. Actor identity remains local audit attribution;
the unsigned mode does not turn the actor string into authentication. Replay of
self-attested evidence is deterministic and does not depend on the trust list.

## Reproducible generator

Save this as `make_progress_attestation.py`. It uses Anvil's public canonicalizer
and signing helpers, so its output is byte-for-byte acceptable to the loader.

```python
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from anvil import signing
from anvil.state.hashing import canonical_json_bytes

parser = argparse.ArgumentParser()
parser.add_argument("--claim", type=Path, required=True)
parser.add_argument("--status", type=Path, required=True)
parser.add_argument("--root", type=Path, default=Path.cwd())
parser.add_argument("--path", required=True)
parser.add_argument("--kind", choices=("file", "commit"), required=True)
parser.add_argument("--commit")
parser.add_argument("--envelope-id", required=True)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--sign", action="store_true")
args = parser.parse_args()

claim = json.loads(args.claim.read_text(encoding="utf-8"))["data"]["claim"]
project_id = json.loads(args.status.read_text(encoding="utf-8"))["data"][
    "project_id"
]
context = claim["attestation_context"]
if context is None:
    raise SystemExit("claim has no attestation_context")
baselines = {item["path"]: item["baseline_sha256"] for item in context["expected_paths"]}
if args.path not in baselines:
    raise SystemExit("--path is not in the claim's canonical expected_paths")

if args.kind == "file":
    commit_sha = context["claim_start_sha"]
    content = (args.root / args.path).read_bytes()
else:
    if not args.commit:
        raise SystemExit("--commit is required for kind=commit")
    commit_sha = args.commit
    content = subprocess.check_output(
        ["git", "-C", str(args.root), "show", f"{commit_sha}:{args.path}"]
    )

payload = {
    "schema_version": 1,
    "kind": args.kind,
    "project_id": project_id,
    "claim_id": claim["id"],
    "generation": claim["generation"],
    "task_id": claim["task_id"],
    "task_revision": context["task_revision"],
    "prd_id": context["prd_id"],
    "prd_revision": context["prd_revision"],
    "claimed_by": claim["claimed_by"],
    "repository_id": context["repository_id"],
    "claim_start_sha": context["claim_start_sha"],
    "commit_sha": commit_sha,
    "path": args.path,
    "prior_sha256": baselines[args.path],
    "file_sha256": hashlib.sha256(content).hexdigest(),
    "issued_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
}
envelope = {"envelope_id": args.envelope_id, "payload": payload}
if args.sign:
    private_key, public_key, signer_id = signing.load_or_create_signer()
    envelope["issuer"] = {
        "algorithm": "ed25519",
        "signer_id": signer_id,
        "public_key": public_key,
        "signature": signing.sign(private_key, canonical_json_bytes(payload)),
    }
args.output.write_bytes(canonical_json_bytes(envelope))
```

File-mode example after changing `src/feature.py`:

```console
uv run --project bin python make_progress_attestation.py --claim claim.json --status status.json --path src/feature.py --kind file --envelope-id producer-run-42 --output progress.json
anvil progress T007 external-write --attestation-file progress.json --actor external-writer --json
```

When Anvil is installed in the active Python environment, run the script with
`python` directly; `uv run --project bin` is the source-checkout form.

Commit-mode example after committing the expected path on a descendant of the
claim-start commit:

```console
uv run --project bin python make_progress_attestation.py --claim claim.json --status status.json --path src/feature.py --kind commit --commit 0123456789abcdef0123456789abcdef01234567 --envelope-id producer-run-43 --output progress.json
```

Use the full commit object ID from `git rev-parse HEAD`; the synthetic value
above is only representative. Add `--sign` to either generator command to use
Anvil's configured Ed25519 runner key, then add the emitted envelope's
`issuer.signer_id` or `issuer.public_key` to the configured trust list before
submitting it.

For MCP, standard-base64 encode the exact file bytes, including required `=`
padding and without whitespace:

```python
import base64
from pathlib import Path

attestation_base64 = base64.b64encode(Path("progress.json").read_bytes()).decode("ascii")
```

Call `submit_progress` with `task_id`, the exact claim `actor`, that
`attestation_base64`, and the repository `cwd`. Noncanonical base64, URL-safe
spellings, omitted padding, and whitespace are rejected.

## Renewal and replay behavior

Acceptance records the semantic digest, claim generation, kind, issuer, and
trust mode as `progress.attested`; it does not renew immediately. The next
successful `anvil renew` or MCP `renew_claim` atomically consumes that pending
artifact and reports:

```json
{
  "source": "attestation",
  "digest": "semantic-digest",
  "generation": 2,
  "trust_mode": "configured_issuer_verified"
}
```

The same artifact cannot authorize a second renewal. A release, stale claim,
different generation, different owner, changed PRD/task revision, moved file
baseline, unrelated repository, or expired lease also prevents reuse. Ordinary
`progress.noted` text is audit-only and never authorizes renewal.

Only one unconsumed attestation may be pending for a claim generation. A second
submission is refused; if independently appended branches replay two pending
facts for the same generation, Anvil quarantines the collision rather than
choosing one as renewal authority.
