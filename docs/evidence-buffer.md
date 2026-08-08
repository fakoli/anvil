# Evidence buffer

> **Audience:** users and operators inspecting or troubleshooting captured verification evidence.

`.anvil/.evidence-buffer/` is a transient, append-only directory used by
the `capture-evidence.sh` hook to record bash-command output between the moment
a verification command runs and the moment `anvil submit` packages that
output into a durable `evidence.submitted` event.

Only a fixed set of verification commands is captured: the hook matches on
`pytest`, `ruff check`, `mypy`, `npm test`, `cargo test`, and `bun test`
(`hooks/capture-evidence.sh`'s `VERIFICATION_PATTERNS`). Any other bash
command exits the hook silently and is never written to a buffer file — it
is not just "not a verification command," it leaves no trace at all. A
future phase may move this list to config.

Documented as part of closing tech-debt-backlog **CL-15** (originally flagged
in PR #41).

## Format

Each file is JSON: one *record* per line in append-only `*.json` files, keyed
by the active claim ID. The hook writes one file per claim:

```text
.anvil/.evidence-buffer/
├── 4F2A.json        # claim 4F2A's captured commands
├── 7B91.json        # claim 7B91's captured commands
└── orphan.json      # commands captured while no active claim matched the actor
```

Each line in a file is one JSON object:

```jsonc
{
  "kind": "command",
  "timestamp": "2026-05-25T14:23:00+00:00",
  "command": "pytest tests/ -v",
  "exit_code": 0,
  "output_sha256": "9f3c...a1",
  "stdout_excerpt": "...up to 4000 chars...",
  "stderr_excerpt": "...up to 4000 chars...",
  "actor": "agent-x"
}
```

`output_sha256` is the SHA-256 of the *full* (untruncated) stdout+stderr,
computed by `anvil hook capture-evidence` before truncation — it lets the
`CommandProof` attest to output that was never fully persisted. `kind` is
written by the hook but not currently read back; `timestamp`, `command`,
`exit_code`, and `output_sha256` are the fields the submit-side reconciler
(`_read_command_proofs` in `packet_apply.py`) actually reads, and it
silently **skips** any record missing one of them (e.g. a pre-SL-3 line with
no `output_sha256`) rather than failing `submit`. A record missing
`output_sha256` would simply be dropped, not embedded as evidence.

`stdout_excerpt` and `stderr_excerpt` are truncated to 4000 characters each
to keep buffer files small and JSONL-friendly. Truncated outputs are still
useful for the sentinel — full output should be saved separately if the
agent's flow needs the long form.

## Lifecycle

| Step | Who | Effect |
|---|---|---|
| 1. Agent runs `pytest` (or other verification command) | Bash tool | `PostToolUse` hook fires |
| 2. `hooks/capture-evidence.sh` shells to `anvil hook capture-evidence` | Hook | One JSON line appended to `<claim-id>.json` (or `orphan.json` if no matching active claim) |
| 3. Agent runs `anvil submit T012 --commands "pytest" --files-changed ...` | CLI | Reads `<claim-id>.json`, parses each well-formed line into a `CommandProof`, and embeds them in the `evidence.submitted` event's `proofs` field |
| 4. `submit --output-file` provided directly | CLI | The buffer is bypassed; up to 8000 characters become a descriptive output excerpt. This never creates a typed proof or satisfies `required_proofs`. |
| 5. `submit --command-proof-file ARTIFACT` provided | CLI | The bounded claim-bound artifact is validated against the explicit active claim, actor, generation, task/PRD revision, repository, cwd, command, timestamps, exit code, and output digest before it is imported as a typed proof. Repeat the flag for a batch. |

Submit is **read-only** with respect to the buffer: it turns the transient
buffer into the durable `evidence.submitted` JSONL event but does **not**
delete, truncate, or rotate the buffer file afterward. `<claim-id>.json`
persists on disk exactly as it was — submitted lines and all — until the
user manually removes it (or the whole `.evidence-buffer/` directory).

`submit` also auto-releases the active claim (the CLI's human-readable output
prints `Claim ID:  <claim-id> (auto-released)`), so there is no such thing as
a second `submit` reading the same claim's buffer file — a task with no
active claim fails `submit` outright (`no active claim found for task '...'.
Run \`anvil claim ...\` first.`, exit 1). Re-claiming the task afterward mints
a brand-new claim ID, so any further hook-captured commands land in a new
`<new-claim-id>.json`; the original claim's buffer file is never re-read by a
later submit.

## External claim-bound command-proof artifact

External and subagent runners can satisfy an exact typed command requirement
without hook instrumentation by constructing one canonical JSON envelope:

```json
{"envelope_id":"RUN-1","payload":{"schema_version":1,"project_id":"P1","claim_id":"C123","generation":1,"claimed_by":"agent-a","task_id":"T001","task_revision":"<64 hex>","prd_id":"default","prd_revision":1,"repository_id":"<64 hex>","claim_start_sha":"<40 or 64 hex>","cwd_relative":".","cwd_identity":"<64 hex>","command_base64":"cHl0ZXN0IC1x","started_at":"2026-08-08T18:00:00Z","ended_at":"2026-08-08T18:00:01Z","exit_code":0,"output_base64":"MSBwYXNzZWQK","output_sha256":"a92b7fdcb45e1d22fc2af4c80adc6e7fc1389ff8a694010cf5e6ff0b5ffbf1f6"}}
```

The root contains exactly `envelope_id`, `payload`, and optional `issuer`.
The payload contains exactly the fields shown. Take the claim/task/PRD and
repository values from the explicit claim response and current project status;
do not infer an owner or use another active claim. `command_base64` is standard
canonical base64 of the exact UTF-8 command bytes and must decode to one exact
task `required_proofs` command and one exact submitted `--commands` value.
`output_base64` is the exact reported combined output and `output_sha256` is
lowercase SHA-256 over those decoded bytes. `cwd_relative` is `.` or a canonical
repository-relative POSIX path. Compute `cwd_identity` with the public
`domain_separated_sha256` helper, domain `anvil.command-cwd.v1\0`, and object
`{"repository_id": REPOSITORY_ID, "cwd_relative": CWD_RELATIVE}`.

Serialize with `anvil.state.hashing.canonical_json_bytes`: sorted keys, UTF-8,
no BOM, whitespace, trailing newline, duplicate keys, floats, or noncanonical
base64. Times use canonical UTC `...Z` spelling and must fall between claim
creation and both verification time and lease expiry. Limits are 262,144 bytes
per canonical envelope, 16,384 decoded command bytes, 131,072 decoded output
bytes, 16 artifacts per batch, and 1 MiB aggregate encoded command/output. The
semantic digest uses domain `anvil.command-proof.v1\0` over the typed payload.

An optional configured issuer has exact shape
`{"algorithm":"ed25519","signer_id":"<16 hex>","public_key":"<64 hex>","signature":"<128 hex>"}`.
The signature covers the canonical payload bytes. The public key or fingerprint
must be present in `ANVIL_TRUST_LIST` or `~/.anvil/trust.txt`; otherwise import
fails. Without this issuer, the receipt truthfully reports
`claim_owner_self_attested`, not independent execution.

Import canonical JSON files with repeated `--command-proof-file` flags. MCP
clients base64-encode each entire canonical envelope and pass the resulting
strings in `command_proof_artifacts_base64` together with `cwd`. Every artifact
is prevalidated before the one durable `evidence.submitted` append, so one bad
item imports nothing and leaves the claim active.

## `orphan.json` accumulation

When a bash command runs and **no active claim matches the actor**, the
record goes to `orphan.json`. This commonly happens when:

- An agent runs verification commands before claiming a task.
- An agent runs commands after the claim has been released or has gone stale.
- Multiple agents run concurrently and the hook's actor identity doesn't
  match any owner.

`orphan.json` is currently **never auto-cleaned**. It accumulates indefinitely
until the user deletes it manually:

```bash
rm .anvil/.evidence-buffer/orphan.json
```

This is a known limitation. `submit --output-file` can preserve an orphan
record as a descriptive excerpt, but it **cannot** turn that record into a
typed command proof or satisfy `required_proofs`. To satisfy a typed command
requirement, rerun the command while the explicit claim is active or import a
valid claim-bound artifact with `submit --command-proof-file`. A future
`anvil evidence prune` command could rotate `orphan.json` on a TTL
basis; tracked separately.

## Sentinel interaction

The `sentinel` agent reads the per-claim buffer files when validating evidence completeness. After
`submit` runs, the sentinel sees the durable `evidence.submitted` event in
`state.db` and `events.jsonl` — the buffer file itself is still there on
disk, unchanged, but the sentinel's evidence-completeness checks work off the
durable event, not the buffer.

## Cleanup policy

| Trigger | What happens |
|---|---|
| `anvil submit T012` succeeds | `<claim-id>.json` for T012's claim is read and embedded in `evidence.submitted`; the file itself is **not** deleted |
| `anvil release T012` | Buffer file for the released claim is **not** auto-deleted; it remains on disk indefinitely until a manual clean. Because buffer files are keyed by claim ID and claim IDs are never reused, no future submit reads it again — re-claiming the task writes to a new `<new-claim-id>.json` instead |
| `anvil init --force` | The entire `.evidence-buffer/` directory is preserved (it's user data) |
| Process crash mid-write | Append-only JSONL means a torn line is the worst case; subsequent reads skip malformed lines |

## When to manually clean

- After a hard reset of project state (`rm -rf .anvil/.evidence-buffer/`).
- After resolving an orphan-accumulation issue (e.g., a stuck claim was force-released and never resubmitted).
- Before sharing a project state snapshot — the buffer is transient and not part of the canonical audit log.

## See also

- `hooks/capture-evidence.sh` — the bash hook that writes to the buffer.
- `bin/src/anvil/cli/hooks.py::capture-evidence` — the CLI subcommand the hook calls.
- `bin/src/anvil/cli/packet_apply.py::submit` — the read side that reconciles the buffer into `evidence.submitted`.
- [`docs/hooks-reference.md`](hooks-reference.md) — the broader hook lifecycle.
