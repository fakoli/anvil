# Bounded current-attempt reads

`anvil.attempt_view.read_attempt_view(state_dir, task_id, *, prd_id=None, limits=None)`
is an observational Python read core. CLI, MCP, bundle packet rendering, and timing
composition are separate integration work. Pass the state directory already
resolved by Anvil; do not assume that a checkout contains its own `.anvil` directory.

The returned JSON-compatible dictionary uses schema
`anvil.state.attempt-view.v1`. It contains exact project/PRD/task identity,
complete task acceptance criteria, Verification and named claims, feature
requirements, dependency statuses, and the current claim/evidence/reviews.
Evidence is selected by its submitted event's causal projection order, never by
timestamp or evidence-ID sorting. A newer claim generation cannot inherit an
older claim's evidence. Reviews include process rejections excluded from acceptance
metrics. Legacy reviews without an exact attempt binding remain explicitly unknown.

`history` separately labels earlier claims, evidence, and reviews. Its counts are
totals including current records; `overflow: false` means the read is complete.
There is no truncated-success result and no historical-reference retrieval API in
this first slice. If mandatory facts or associated history cannot fit, the entire
read refuses. `events` retains exact scoped event IDs, causal order, actor, native
timestamps, and allowlisted identity/status facts for timing composition.

`event_cursor` identifies the single locked event frontier. `view_digest` is
`sha256:` followed by SHA-256 of `b"anvil.state.attempt-view.v1\0"` and canonical
JSON for the entire response except `view_digest`. Identical reads and limits
produce identical digests. A frontier change changes the digest even when the
task itself is unchanged.

The reader acquires the existing event-first, query-only SQLite transaction and
reuses the provider snapshot's event/projection consistency checks. It does not
initialize, migrate, repair, reap or renew leases, create packet sidecars, or
contact root owners. SQLite may update its existing transient shared-memory read
marks; durable database and event material are not changed.

`AttemptViewLimits` defaults are fixed hard ceilings. A mapping or an instance
may lower them:

| Limit | Ceiling |
| --- | ---: |
| `max_event_log_bytes` | 32 MiB |
| `max_event_records` | 20,000 |
| `max_event_bytes` | 1 MiB, excluding the newline |
| `max_cell_bytes` | 256 KiB per projected cell, excluding event payloads |
| `max_response_bytes` | 64 KiB, including digest and limits |

Every associated row collection is also bounded by the record ceiling, and each
selected collection's aggregate cells are bounded by the log-byte ceiling. The
whole-history validation scan is deliberate. These ceilings accommodate the
measured 17 MiB / 9,543-event project history used for qualification; larger
projects require an engine-maintained incremental frontier before another increase.

Catch `ProjectSnapshotError` for all refusals. `AttemptViewError` is its
execution-specific subclass, with an `AttemptViewRefusal` in `.error` carrying
`code`, `field`, `actual`, and `limit`. The shared frontier checker can return
the existing provider read error type. Neither exposes hostile record contents.

This execution view allows requirement text and command literals. It excludes
raw command output, proof `output_base64`, worktree/session/host fields, and
command working-directory fields. It does not widen the generic provider
snapshot schema. Retained proof metadata includes exact attribution, semantic
digests, capture times, and measured command intervals when native evidence has
them. Capture-only duration remains unknown.

Local acceptance and bundle/claim status do not prove source delivery or external
custody release. `mutation_authority` is false; `source_delivery`, runner stop,
external owner state, and unsupported acceptance invalidation state are explicitly
unknown. The returned root binding identifies the retained reservation and
digests; only the resource owner's current state can authorize its use.
