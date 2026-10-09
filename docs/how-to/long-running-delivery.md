# Long-running delivery

Start in the intended checkout. `anvil status --json` reports its resolved State;
an uninitialized replacement checkout does not establish that project history
is absent. Inspect existing ownership and recorded attempts before claiming:

```bash
anvil status --json
anvil packet T001 --attempt --format json
anvil evidence-preflight T001 --json
```

For a bundle use `anvil packet B001 --attempt --bundle --format json`. The
coordinator owns every member mutation; see [coordinating a bundle](coordinating-a-bundle.md).
For a named PRD, retain its identity, for example
`anvil prd parse --prd release` and `anvil packet release:T001 --attempt --format json`.

Attempt reads and evidence preflight never initialize State, reap claims,
write packets, renew custody, or authorize submission. Preflight reports the
current claim and generation, complete buffer digest and bytes, record/skipped
counts, and literal required-command coverage. It preserves failed captures and
refuses overflow instead of selecting a passing suffix. Its proof ceilings are
16 items and 1 MiB. Attempt responses are bounded to 64 KiB; bundle members share
one State frontier and cumulative bounds. Default `anvil packet T001` still
writes the ordinary packet.

If complete inspection reports overflow, observe that the repository runner has
stopped before releasing the rejected generation with `anvil release T001`.
Preserve the original buffer and historical evidence. Inspect readiness and
claim a fresh eligible generation; capture its literal required checks again.
Do not trim, choose passing records, raise limits or relabel old captures. Bundle
custody and replacement-generation rules remain coordinator-owned.

The repository owns its verification runner, prerequisites, environment checks,
timeouts and process cleanup. [Verification profiles](verification-profiles.md)
freeze metadata and declared source bytes; Anvil does not execute a runner or
install dependencies. Run the repository's supported preflight and verification
commands, then submit actual captured evidence using the existing workflow:

```bash
anvil evidence-preflight T001 --json
anvil submit T001 --commands "pytest -q" --files-changed src/feature.py
```

Replace the example command and file with the task's literal requirements and
actual changes. Submission re-inspects the buffer at the native mutation
boundary; an earlier passing preflight cannot waive later drift. Profile guards
check the actual prepared target at claim creation and the originating claim at
submission and acceptance. Legitimate commits may advance that target after
creation. Three independent adversarial reviews and the required immutable
approval authority remain separate gates.

For an actual runner observation, write the schema-1 timing receipt with exact
claim attribution and observed UTC/monotonic measurements, then ingest it:

```bash
anvil progress T001 tests --timing-file timing.json
anvil packet T001 --attempt --format json
```

The receipt must be a complete regular UTF-8 JSON file of at most 16 KiB. Timing
requires the exact active ordinary owner and cannot combine with
`--attestation-file` or `--bundle`. It is audit data: success, failure or
interruption grants no proof, renewal, acceptance or ownership. Complete
monotonic execution and interrupted observations remain distinct; overlapping
UTC intervals use a union rather than summed execution time. Current age needs
an explicit `--observation-at` UTC timestamp. Unknown runner stop, reservation
release and external waits stay unknown; the view supplies no forecast.

Accepted-attempt correction records exact invalidation provenance while retaining
historical acceptance and evidence bytes. An old signed proof may still verify
cryptographically after native acceptance has been invalidated; consult the
current State for acceptance validity. Exact retries cannot reopen a newer
accepted generation. Replay does not consult current profile
files. A new claim generation inherits no old proof coverage. Integration,
PR/merge and deployment facts require recorded delivery evidence. Installed
artifact checks, native platform checks, actual project pilots and final user
validation must be completed separately; source tests do not establish them.
