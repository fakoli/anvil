# SL-3 — Typed `ProofArtifact` (typed evidence, non-gameable gate)

**Date:** 2026-06-19
**Status:** Draft — needs approval before implementation
**Plugin:** `anvil`
**Tracks:** roadmap integrity-track `SL-3` (Wave 2: "make governance non-gameable"); fakoli-style principle **P4** `open_work` (replay-equivalence must stay green)
**Depends on:** SL-0 (shipped — the advisory→preview gate consolidation that already routes the enforcing and advisory gate through one predicate)
**Breaking:** YES. The free-text `required_evidence` substring path is **deleted, not deprecated**. The event schema for `evidence.submitted` changes shape. Schema version bumps `5 → 6`.

---

## 1. Goal

Stop the substrate from trusting strings. Today an agent satisfies the evidence
gate by writing the right *English* into a free-text field; the gate is a
case-insensitive `in` check (`bin/src/anvil/review/gates.py:228-262`). Replace
free-text `Verification.required_evidence: list[str]`
(`bin/src/anvil/state/models.py:270`) with a typed `list[ProofRequirement]`, and
replace the substring gate with a predicate evaluator that asks a verifiable
question — *"does a passing `CommandProof` for this command exist?"* — against a
typed `list[ProofArtifact]` carried on the `Evidence` record.

The `hooks/capture-evidence.sh` PostToolUse hook already captures real exit codes
and stdout/stderr; today it dumps them into an untyped buffer record. SL-3 makes
it emit a typed `CommandProof` carrying the command, its real `exit_code`, and a
`sha256` of the captured output — so a proof is something the engine *produced
from observation*, not something the agent *asserted in prose*.

The `test_replay_equivalence` guarantee (fakoli-style **P4**) must remain green:
old `events.jsonl` logs that carry string `required_evidence` and untyped
evidence must still replay into the new typed schema deterministically (see §6).

## 2. Context & root cause

### The gameable surface today

`evidence_complete(task, evidence)` (`review/gates.py:190-262`) iterates
`task.verification.required_evidence` — a `list[str]` of human phrases like
`"test output"`, `"PR link"`, `"screenshots"` — and routes each string by
keyword:

- a string containing `"test"` / `"pytest"` checks `evidence.commands_run` for a
  test-runner substring (`_contains_test_keyword`, `review/gates.py:279-309`);
- a string matching `\bpr\b` checks `evidence.pr_url` is truthy
  (`_is_pr_related`, `review/gates.py:312-324`);
- `"screenshot"` checks `evidence.screenshots` is non-empty;
- `"files changed"` checks `evidence.files_changed` is non-empty;
- **anything else** falls through to a substring `in` test against
  `evidence.output_excerpt` and `evidence.known_limitations`
  (`review/gates.py:251-257`).

The fallback branch is the hole. A required item `"benchmarks pass"` is satisfied
by an agent putting the literal text `"benchmarks pass"` anywhere in
`output_excerpt` or `known_limitations`. No command ran; no exit code was
observed; the gate passes. The docstring on `transitions._evidence_complete`
(`state/transitions.py:175-186`) already admits the prior raw-corpus version was
"trivially satisfiable by free text" — SL-3 closes the same class of hole that
the heuristic routing only narrowed.

Even the "good" branches are weak: `_contains_test_keyword` matches the *string*
`"pytest"` in `commands_run` but never sees the command's **exit code**. A
recorded command `pytest tests/` that *failed* (exit 5) passes the gate, because
`commands_run` is a `list[str]` (`models.py:427`) with no result attached.

### Root cause

`Evidence` records what the agent *claims it did* as strings
(`commands_run: list[str]`, `output_excerpt: str | None`); `required_evidence`
records what is wanted as strings. The gate is therefore string-against-string.
There is no typed object that says "this exact command was observed to exit 0 and
here is the hash of what it printed." The hook
(`hooks/capture-evidence.sh:95-104`) already *has* the exit code and output — it
just throws away the typing by writing a flat dict to a buffer that
`anvil submit` never reconciles into the gate.

## 3. Proposed design

### 3.1 The typed proof model (`state/models.py`)

Mirror the existing embedded-value-object style (`Score`, `Verification`:
`models.py:249-270`): Pydantic `BaseModel`, `model_config = _MODEL_CONFIG`
(`frozen=False, validate_assignment=True, extra="forbid"`, `models.py:225-229`),
StrEnum for the discriminator, UTC validator reused from `_require_utc`
(`models.py:232`).

```python
class ProofKind(enum.StrEnum):          # grep-able, str-serialisable (house rule)
    command   = "command"
    diff      = "diff"
    link      = "link"
    assertion = "assertion"


class CommandProof(BaseModel):
    """A command the engine observed run: real exit code + output hash."""
    model_config = _MODEL_CONFIG
    kind: Literal[ProofKind.command] = ProofKind.command
    command: str
    exit_code: int
    output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    captured_at: datetime.datetime
    # @field_validator("captured_at") -> _require_utc(...)


class DiffProof(BaseModel):
    """A unified diff the engine observed (SL-5 keys its drift check on this)."""
    model_config = _MODEL_CONFIG
    kind: Literal[ProofKind.diff] = ProofKind.diff
    files_changed: list[str] = Field(default_factory=list)
    diff_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    insertions: int = Field(default=0, ge=0)
    deletions: int = Field(default=0, ge=0)


class LinkProof(BaseModel):
    """An external artifact reference (PR, CI run, screenshot URL)."""
    model_config = _MODEL_CONFIG
    kind: Literal[ProofKind.link] = ProofKind.link
    url: str
    label: str | None = None


class AssertionProof(BaseModel):
    """A human/agent attestation. The ONLY honour-system proof — and it is
    typed as such so the gate can refuse to let it satisfy a CommandProof
    requirement. Replaces the old free-text fallback, but cannot impersonate
    an observed command."""
    model_config = _MODEL_CONFIG
    kind: Literal[ProofKind.assertion] = ProofKind.assertion
    statement: str
    attested_by: str


ProofArtifact = Annotated[
    CommandProof | DiffProof | LinkProof | AssertionProof,
    Field(discriminator="kind"),
]
```

`ProofArtifact` is a discriminated union keyed on `kind` (the same StrEnum
discipline used everywhere in this file). A serialized proof always carries its
`kind`, so the SQLite JSON column and the `events.jsonl` payload round-trip
through `TypeAdapter(list[ProofArtifact])` deterministically.

### 3.2 Typed requirements on the Task

Replace `Verification.required_evidence: list[str]` (`models.py:270`) with a
typed predicate list:

```python
class ProofRequirement(BaseModel):
    """One typed thing a Task demands before it can be accepted."""
    model_config = _MODEL_CONFIG
    kind: ProofKind                 # which proof kind satisfies this
    # command requirements pin the exact command and the passing exit set:
    command: str | None = None
    passing_exit_codes: list[int] = Field(default_factory=lambda: [0])
    # link requirements can pin a required URL scheme/host substring (optional):
    link_contains: str | None = None
    label: str                       # human description for packets / errors

    @model_validator(mode="after")
    def _command_requirements_pin_a_command(self) -> "ProofRequirement":
        # A kind=command requirement with command=None can never be satisfied
        # (CommandProof.command is always a str, so `p.command == None` is always
        # False) — reject it at construction instead of failing the gate silently.
        if self.kind is ProofKind.command and self.command is None:
            raise ValueError("command-kind ProofRequirement requires `command`")
        return self


class Verification(BaseModel):
    model_config = _MODEL_CONFIG
    commands: list[str] = Field(default_factory=list)
    manual_steps: list[str] = Field(default_factory=list)
    required_proofs: list[ProofRequirement] = Field(default_factory=list)  # was required_evidence: list[str]
```

`Evidence` (`models.py:419-440`) gains one typed field; the legacy string fields
(`commands_run`, `output_excerpt`, `files_changed`, `pr_url`, `commit_sha`,
`screenshots`) stay for one release as **descriptive metadata only** — the gate
no longer reads them:

```python
    proofs: list[ProofArtifact] = Field(default_factory=list)   # NEW — the only thing the gate reads
```

### 3.3 The gate rewrite (`review/gates.py`)

`evidence_complete` is rewritten to evaluate typed predicates. The signature is
unchanged — `(task, evidence) -> tuple[bool, list[str]]` — so its single source
of truth (it is called from `transitions._evidence_complete` at
`transitions.py:194`, `cli/packet_apply.py:400/585/686`, and `mcp_server.py:2593`)
needs no caller changes:

```python
def evidence_complete(task: Task, evidence: Evidence) -> tuple[bool, list[str]]:
    required = task.verification.required_proofs
    if not required:
        return True, []
    missing: list[str] = []
    for req in required:
        if not _proof_satisfies(req, evidence.proofs):
            missing.append(req.label)
    return len(missing) == 0, missing


def _proof_satisfies(req: ProofRequirement, proofs: list[ProofArtifact]) -> bool:
    if req.kind is ProofKind.command:
        return any(
            isinstance(p, CommandProof)
            and p.command == req.command
            and p.exit_code in req.passing_exit_codes
            for p in proofs
        )
    if req.kind is ProofKind.diff:
        return any(isinstance(p, DiffProof) for p in proofs)
    if req.kind is ProofKind.link:
        return any(
            isinstance(p, LinkProof)
            and (req.link_contains is None or req.link_contains in p.url)
            for p in proofs
        )
    if req.kind is ProofKind.assertion:
        return any(isinstance(p, AssertionProof) for p in proofs)
    return False
```

Key property: a `ProofKind.command` requirement can **only** be satisfied by an
actual `CommandProof` whose `exit_code` is in the passing set. There is no
substring branch and no field-flattening fallback — an `AssertionProof` cannot
impersonate a command. The exit-code check is now first-class, which closes the
"recorded a failed `pytest`" hole the string path missed entirely.

**Deletions** (this is the breaking part; not a deprecation):

- `evidence_complete`'s entire substring body (`review/gates.py:222-262`).
- `_is_test_related` (`gates.py:270-273`).
- `_COLLECT_ONLY_RE` and `_contains_test_keyword` (`gates.py:276-309`) — the
  `--collect-only` guard is no longer relevant: a `CommandProof` carries the real
  exit code, so "collected zero tests, exit 0" is now a *passing* command and the
  requirement author must pin the command they actually mean.
- `_is_pr_related` (`gates.py:312-324`) — superseded by `LinkProof` +
  `link_contains`.

`DeferredFinding` and the `deferred_findings*` functions (`gates.py:54-187`) are
untouched — they key on `likely_files` / `files_changed`, not on
`required_evidence`.

### 3.4 The hook emits `CommandProof` (`hooks/capture-evidence.sh` + `cli/hooks.py`)

`hooks/capture-evidence.sh` already extracts `command`, `exit_code`,
`stdout_excerpt`, `stderr_excerpt` in one `python3` pass
(`capture-evidence.sh:59-121`). Change the pre-built `record` dict
(`capture-evidence.sh:95-104`) so the buffer line is a serialized `CommandProof`
shape: add `kind: "command"` and `output_sha256` (computed as
`hashlib.sha256((stdout_raw + stderr_raw).encode()).hexdigest()` — over the
**full** output, before the 4000-char excerpt truncation, so the hash is of what
actually ran, not of the excerpt).

The CLI path `anvil hook capture-evidence` (`cli/hooks.py:149-254`) is the
primary writer when the binary is present (`capture-evidence.sh:156-184`). It
gains a `--output-sha256` option (the shell computes the hash; the CLI does not
re-read the truncated temp files for hashing) and writes a typed `CommandProof`
record (`kind`, `command`, `exit_code`, `output_sha256`, `captured_at`) into the
per-claim buffer at `.anvil/.evidence-buffer/<claim-id>.json`
(`cli/hooks.py:238-254`).

`anvil submit` (`cli/packet_apply.py`, the `evidence.submitted` path at
`packet_apply.py:372-380`) reads the claim's buffer, parses each line into a
`ProofArtifact` via the discriminated `TypeAdapter`, and writes them into the new
`Evidence.proofs` field carried on the `EvidenceSubmittedPayload` (§3.5). A
`--proof` repeatable flag lets a human attach `LinkProof` / `AssertionProof`
explicitly.

### 3.5 Event payload + storage

`EvidenceSubmittedPayload` (`state/payloads.py:309-324`) gains
`proofs: list[ProofArtifact] = Field(default_factory=list)` — the **typed**
artifacts, validated at write time like every other embedded value object in the
payload suite (do NOT use `list[dict[str, Any]]`, which would defer validation
and let a malformed proof through until replay). `default_factory=list` keeps
pre-SL-3 logs replayable. `extra="forbid"` stays. The `_write_evidence_submitted` handler and the evidence
SELECT/INSERT (`state/sqlite.py:3450-3576`, columns enumerated at
`sqlite.py:3565`) gain a `proofs TEXT NOT NULL DEFAULT '[]'` JSON column on the
`evidence` table (`schema.py:152-165`).

## 4. Acceptance

1. `Verification.required_evidence` is removed; `Verification.required_proofs:
   list[ProofRequirement]` replaces it. `Evidence.proofs: list[ProofArtifact]` is
   added.
2. `evidence_complete` evaluates typed predicates only. The substring body,
   `_is_test_related`, `_contains_test_keyword`, `_COLLECT_ONLY_RE`, and
   `_is_pr_related` are **deleted** from `review/gates.py` (verified by grep
   returning zero hits, outside this spec).
3. A `ProofKind.command` requirement is satisfiable **only** by a `CommandProof`
   with an exit code in `passing_exit_codes`; an `AssertionProof` carrying the
   command text in `statement` does **not** satisfy it (regression test for the
   gameability hole).
4. A recorded command that exited non-zero does **not** satisfy a matching
   command requirement (closes the "failed pytest passes" hole).
5. `hooks/capture-evidence.sh` emits a `CommandProof`-shaped buffer line with
   `kind: "command"`, the real `exit_code`, and a 64-hex `output_sha256`.
6. The one-time migration (§5) converts every existing string `required_evidence`
   item to a typed `ProofRequirement`.
7. `test_replay_equivalence` (P4) stays green against a regenerated golden that
   includes a pre-SL-3 (string `required_evidence`) event tail (§6).
8. Schema version `5 → 6`; `migrations.md` documents the v5→v6 auto-upgrade.
9. `plugins/anvil` version bumped (minor — breaking model/event change) and
   `registry/` regenerated.

## 5. Migration (one-way, string → typed)

This is a **data migration of declared requirements**, separate from the SQLite
schema bump. It runs once per project, converting the historical free-text
intent into the closest typed predicate so nothing silently loosens.

### 5.1 Requirement string → `ProofRequirement` mapping

Reuse the *old* routing heuristics one final time, inside the migration only, to
classify each legacy string (then the heuristics are deleted from the live gate):

| Legacy `required_evidence` string | Migrated `ProofRequirement` |
|---|---|
| contains `test` / `pytest` / `cargo test` | `kind=command, command=<first matching Verification.commands entry>, passing_exit_codes=[0], label=<original string>` |
| matches `\bpr\b` or `"pull request"` | `kind=link, link_contains="/pull/", label=<original>` |
| contains `screenshot` | `kind=link, label=<original>` |
| contains `files changed` | `kind=diff, label=<original>` |
| anything else (the old free-text fallback) | `kind=assertion, label=<original>` |

The "anything else → assertion" row is deliberate and **honest**: items that the
old gate could only check by substring become explicit honour-system assertions,
visibly typed as such, instead of masquerading as verified. A planner re-running
`anvil plan` can tighten an `assertion` into a `command` requirement afterward.

For the `test → command` row, the migration pairs the requirement with the
*first* `Verification.commands` entry that contains a test runner; if the task
declares no commands, it falls back to `kind=assertion` (a test requirement with
no command to run is, by definition, unverifiable and must be flagged as such).

### 5.2 Mechanism

A `task.verification_migrated_v6` event per task (additive, replayable) rewrites
the task's `verification` blob. This keeps the migration **inside the event log**
so it replays — it is not a side-channel mutation. The CLI entry point is
`anvil migrate proofs` (a new subcommand under the existing `cli/migrate.py`
surface, mirroring its event-emitting pattern). Running it twice is idempotent:
a task whose `verification.required_proofs` is already populated emits an
`IdempotentNoOp` (SL1-RR-1's no-op contract).

### 5.3 SQLite schema (`schema.py`, `_check_schema_version`)

Bump `SCHEMA_VERSION = 6` (`schema.py:39`) and add the `evidence.proofs` column
to the DDL (`schema.py:152-165`). Extend `_check_schema_version`
(`sqlite.py:1195-1307`) with a `v5 → v6` branch that calls a new
`_ensure_evidence_proofs_column` helper, identical in shape to
`_ensure_task_type_column` (`sqlite.py:1322-1339`): a duplicate-column-tolerant
`ALTER TABLE evidence ADD COLUMN proofs TEXT NOT NULL DEFAULT '[]'`. Every prior
upgrade branch (`v0/1 → 5`, `v2 → 5`, `v3 → 5`, `v4 → 5`) becomes `→ 6` and
chains the new helper. Purely additive at the SQLite level — the JSON default
backfills every existing evidence row to "no typed proofs," which is the correct
pre-SL-3 meaning.

## 6. Backward-compat / replay implications (fakoli-style P4)

The hard constraint: an `events.jsonl` written before SL-3 — whose
`task.created` / `task.scored` payloads carry string `required_evidence`, and
whose `evidence.submitted` payloads carry no `proofs` — **must still
`replay_from_empty` deterministically** into the v6 schema. Three moves:

1. **Lenient payload parsing on replay.** `EvidenceSubmittedPayload.proofs`
   defaults to `[]`, and the new `Verification` model accepts a *legacy* shape:
   a `model_validator(mode="before")` on `Verification` maps a bare
   `required_evidence: list[str]` key (if present and `required_proofs` absent)
   onto an empty `required_proofs`, while preserving the original strings under a
   **declared, excluded field** — `_legacy_required_evidence: list[str] =
   Field(default_factory=list, exclude=True)` — that the
   `task.verification_migrated_v6` event (§5.2) later consumes. It must be a real
   typed field (NOT a Pydantic "extra"): `_MODEL_CONFIG` sets `extra="forbid"`
   (`models.py:225-229`), so an undeclared key would raise `ValidationError` on
   the first pre-SL-3 log. `exclude=True` keeps it out of serialized output so it
   never re-enters the event stream. This makes old logs replay without raising.
2. **The migration is itself a replayable event.** Because §5.2 records the
   string→typed conversion as `task.verification_migrated_v6` events appended to
   the log, a full replay of a *migrated* project reconstructs the typed state
   exactly — the conversion is a fact in the log, not a one-shot script that
   replay cannot see.
3. **Golden regeneration.** Regenerate `tests/fixtures/replay/sample-project/`
   so the golden `events.jsonl` includes both a pre-SL-3 evidence event (string
   `required_evidence`, no `proofs`) **and** a post-migration
   `task.verification_migrated_v6` event. `test_replay_equivalence`'s byte-equal
   `serialize_state(normal) == serialize_state(replay) == golden` assertion
   stays green — it is the P4 proof.

This is the same P4 discipline SL1-RR-1 followed
(`docs/specs/2026-06-01-sl1-rr-1-event-sourcing-write-path.md` §8C): update the
golden to the new shape; never weaken the equivalence assertion.

## 7. Risks

- **Over-classifying to `assertion`.** The migration's free-text fallback row
  produces honour-system `AssertionProof` requirements. Mitigation: `anvil plan`
  surfaces tasks whose `required_proofs` are all `assertion` so a human can
  tighten them; the *visibility* of the weakness is itself the improvement over
  the old invisible substring pass.
- **Output hash instability.** Hashing full stdout+stderr means non-deterministic
  command output (timestamps, temp paths) yields a different `output_sha256` each
  run. This is fine: the gate checks `exit_code`, not hash equality — the hash is
  a tamper-evident record of *what was observed*, used by SL-5's drift check and
  for audit, never for gate pass/fail. Document this so no future caller asserts
  hash stability.
- **Discriminated-union serialization drift.** A proof missing its `kind` key
  fails `TypeAdapter` validation. Mitigation: the hook and CLI both write `kind`
  explicitly; replay tolerance (§6.1) only covers the *requirement* side, not
  proofs — a malformed interior proof line is corruption and should raise
  (consistent with SL1-RR-1 §7's "interior malformed line is corruption").

## 8. Implementation steps

1. Add `ProofKind`, `CommandProof`, `DiffProof`, `LinkProof`, `AssertionProof`,
   `ProofArtifact`, `ProofRequirement` to `state/models.py`; change `Verification`
   and `Evidence`; export from `__all__` (`models.py:26-66`).
2. Add the `Verification` legacy-shape `model_validator(mode="before")` (§6.1).
3. Bump `SCHEMA_VERSION = 6`; add `evidence.proofs` to DDL; add
   `_ensure_evidence_proofs_column` and the `v5→v6` migration branch in
   `_check_schema_version`; update every existing `→5` branch to `→6`.
4. Add `proofs` to `EvidenceSubmittedPayload`; thread it through
   `_write_evidence_submitted` and the evidence INSERT/SELECT in `sqlite.py`.
5. Rewrite `evidence_complete`; delete `_is_test_related`,
   `_contains_test_keyword`, `_COLLECT_ONLY_RE`, `_is_pr_related`.
6. Update `hooks/capture-evidence.sh` to emit `kind` + `output_sha256`; add
   `--output-sha256` to `anvil hook capture-evidence`; write typed `CommandProof`
   buffer lines.
7. Update `anvil submit` to read the buffer into `Evidence.proofs`; add a
   `--proof` flag for manual `LinkProof` / `AssertionProof`.
8. Update the work-packet renderer (`context/packets.py:160-181`, `306-324`,
   `424-457`) to render `required_proofs` labels instead of the old strings; the
   fast-lane trim (`FAST_LANE_REQUIRED_EVIDENCE_MAX`) now slices
   `required_proofs`.
9. Add `anvil migrate proofs`; emit `task.verification_migrated_v6` events.
10. Regenerate the replay golden; bump plugin version; regen `registry/`.

## 9. Test plan

| Test | Asserts |
|---|---|
| **Gameability regression** | An `AssertionProof{statement: "pytest passed"}` does NOT satisfy a `ProofRequirement{kind=command, command="pytest", passing_exit_codes=[0]}` |
| **Failed-command rejection** | A `CommandProof{command="pytest", exit_code=1}` does NOT satisfy a `[0]`-passing command requirement |
| **Happy path** | A `CommandProof{command="pytest", exit_code=0, output_sha256=...}` satisfies the matching requirement; `evidence_complete` returns `(True, [])` |
| **Hook output** | A simulated PostToolUse payload yields a buffer line with `kind="command"`, the injected `exit_code`, and a 64-hex `output_sha256` over full output |
| **Migration mapping** | Each legacy string row in §5.1 maps to the expected `ProofRequirement` kind; a `test`-string task with no commands maps to `assertion` |
| **Migration idempotence** | Running `anvil migrate proofs` twice emits `IdempotentNoOp` on the second pass |
| **Replay equivalence (P4)** | Pre-SL-3 golden tail (string `required_evidence`, no `proofs`) + a `task.verification_migrated_v6` event replays byte-equal to normal-path state |
| **Schema upgrade** | A v5 db auto-upgrades to v6 with an `evidence.proofs` column defaulting `'[]'`; a v3 db chains through to v6 |
| **No-substring guarantee** | `grep` for `_contains_test_keyword` / `_is_pr_related` / substring `in` in `gates.py` returns zero hits |

CI: full suite via `.github/workflows/anvil.yml`
(`uv run --project plugins/anvil/bin --extra all-providers --with pytest pytest`).

## 10. Out of scope

- Re-hashing or verifying `output_sha256` at gate time (the gate checks exit
  codes; the hash is audit/drift material — SL-5 consumes `DiffProof`).
- A sandbox that *re-runs* commands to independently produce `CommandProof`s
  (this spec trusts the hook's observation; independent re-execution is a future
  hardening).
- Removing the legacy descriptive `Evidence` string fields (`commands_run`,
  `output_excerpt`, etc.) — kept for one release as metadata; their removal is a
  follow-up once nothing reads them.

## 11. References

- `bin/src/anvil/state/models.py:249-270` (`Score`, `Verification`,
  `required_evidence`), `:419-440` (`Evidence`)
- `bin/src/anvil/review/gates.py:190-324` (substring gate + helpers to delete)
- `bin/src/anvil/state/transitions.py:164-207` (`_evidence_complete`, single
  source of truth)
- `bin/src/anvil/cli/packet_apply.py:372-417` (submit/evidence path),
  `:400/585/686` (gate callers)
- `hooks/capture-evidence.sh:59-121` (extraction), `:95-104` (record dict)
- `bin/src/anvil/cli/hooks.py:149-254` (`anvil hook capture-evidence`)
- `bin/src/anvil/state/payloads.py:309-324` (`EvidenceSubmittedPayload`)
- `bin/src/anvil/state/schema.py:39, 152-165` and
  `bin/src/anvil/state/sqlite.py:1195-1339` (schema version + migration pattern)
- `docs/specs/2026-06-01-sl1-rr-1-event-sourcing-write-path.md` §6-§8 (P4 replay
  discipline, IdempotentNoOp contract)
