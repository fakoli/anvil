# Project: Optional Jev foundation and accountable consumer contract

## Summary

Give Anvil and its consumers an explicit, bounded TypeSafe/Jev decision aid.
Installation alone must not send data to TypeSafe. Every consumer can work
without Jev, and every displayed Jev recommendation identifies its origin,
limits, and availability. This PRD owns the shared behavior; the remaining
PRDs own the questions and consumer experience.

## Goals

- Make cloud assistance an informed opt-in with one effective off switch and
  independent capability selection.
- Preserve local-first behavior, deterministic proof gates, current providers,
  serving routes, and existing authorization boundaries.
- Return machine-readable judgments whose model, input, and rubric identities
  can be checked without storing private input text.
- Make errors, disabled operation, and uncertainty distinguishable from a
  completed judgment.

## Users and journeys

An operator configures a protected credential reference and enables only a
selected capability. A developer asks for assistance on bounded, deliberately
selected text. A consumer sees an advisory label with model/provenance, can
dismiss it, and can continue when the provider is unavailable. An administrator
turns the integration off without uninstalling Anvil or changing its LLM.

## Requirements

- R001: Jev shall default to disabled, with no enabled capabilities, and the
  disabled path shall perform no credential lookup, input-file read for Jev,
  DNS resolution, HTTP call, subprocess request, or Jev background work.
- R002: The effective policy shall require global enablement, the requested
  capability's enablement, existing API/egress permission, and source-export
  permission. An explicit per-operation disable shall override enablement.
- R003: Enabling Jev shall not select an Anvil planner provider, replace a
  user's Codex/Claude provider, change a Serving route, or enable unrelated
  capabilities. Existing `llm_allow_api` denial shall not be bypassed in Anvil.
- R004: Supported configuration shall use existing product configuration and
  protected secret references. Routine enable, disable, status, and evaluation
  shall have short supported CLI workflows; no shell sourcing of shared env
  files, pasted keys, or user-authored wrapper scripts shall be required.
- R005: Credential lookup shall happen only at the trusted execution owner,
  after authorization. The default environment reference is
  `TYPESAFE_API_KEY`; browsers, agent prompts, reports, fixtures, and Git shall
  never contain the value. Do not automatically scan home env files.
- R006: Requests shall use verified HTTPS to the fixed TypeSafe System One
  endpoint with a pinned model, bounded input/output, a finite deadline, and
  no automatic retries, redirects, alternate endpoints, or model fallback.
- R007: Both request and response schemas shall be validated, including exact
  question IDs, permitted choice labels, probability keys, finite numeric
  values, model identity, and per-type ranges. Booleans are not numbers.
- R008: Results shall identify whether a request was attempted and whether a
  usable answer completed; disabled, policy-blocked, unavailable, malformed,
  and completed outcomes shall not be conflated.
- R009: Completed results shall carry the model, capability, rubric version
  or digest, input digest, schema version, elapsed time, validated usage, and
  typed answers. A digest proves correspondence, not truth or authenticity.
- R010: Every consumer that displays or applies an advisory ranking shall show
  a persistent compact Jev attribution. Expanded details shall explain cloud
  processing, model identity, uncertainty, and non-authoritative status.
- R011: Source text, raw provider error bodies, authorization headers, and
  arbitrary provider fields shall be excluded from normal telemetry and
  exceptions. Record only bounded reason codes and safe metadata.
- R012: Local access permission shall not imply permission to export content.
  Callers shall use explicit allowed fields and selected sources; redact
  known secrets locally and refuse known secret/configuration sources.
- R013: A consumer shall reject a result after its source digest, authorization,
  operation generation, or effective enablement changes. Switching off stops
  new calls; bytes already sent cannot be recalled and late results are ignored.
- R014: Jev results shall remain outside canonical proof truth, task acceptance,
  status transitions, review approvals, capability routing, and lifecycle.
  No outage or high confidence shall relax an existing gate.
- R015: Offline tests shall require no credentials or external requests. Live
  tests shall use only synthetic or separately approved inputs and shall keep
  model/rubric versions, expected labels, raw answers, timings, and failures.
- R016: Documentation shall distinguish implemented interfaces from proposals,
  test results from targets, source merge from deployment, and smoke tests
  from held-out qualification. All capabilities remain off after source merge.

## Proposed interface contract

Use one `jev` settings block, not another provider registry. Fields are
`enabled`, an explicit set of capability names, pinned `model`, credential
environment reference, and bounded `timeout_seconds`. Product-level API and
source-export policy remain additional restrictions, not replaceable settings.
Unknown capabilities, invalid booleans, non-finite timeouts, and moving model
aliases fail configuration validation. No automatic environment detection.

Capability names are defined in the [suite index](README.md). Anvil's new
`jev` command group should expose status/configuration and explicit evaluation;
native consumers reuse that implementation rather than make their own
question-specific HTTP calls. A Serving bridge may consume its versioned JSON
contract through the existing bounded Anvil CLI pattern where available;
missing optional tooling reports unavailable, never installs tools implicitly.

Result `schema`: `anvil.jev.annotation.v1`, with `status`, `reason`,
`requested`, `request_started`, `used`, `capability`, `model`, `input_digest`,
`rubric_digest`, `elapsed_ms`, `usage`, and `answers`. Only `completed` sets
`used=true`. An unavailable response after sending sets
`request_started=true`, so privacy disclosures do not falsely claim no
egress. Inputs need not be logged to establish an input digest.

Start with maximum serialized request 32 KiB, response 256 KiB, 32 questions,
and five-second total deadline. These are conservative product limits below
the provider's advertised context limits, not claimed tokenizer equivalence.
Voice consumers use a shorter local deadline and cannot hold the base pipeline
past it. A timed-out worker must not accumulate indefinitely. No cross-user
result cache or persistent background queue is needed for the first version.

The provider supports Choice, Score, and Noul. Scores may be fractional.
Probability vectors tolerate documented rounding (sum within 0.02 of one);
each number still must be finite and in range. Unknown labels and mismatched
question sets invalidate the response instead of being partially accepted.

## Acceptance Criteria

- Given disabled Jev and a missing/hostile key source, every existing workflow
  succeeds exactly as before and spies observe zero credential/network reads.
- Given one capability enabled, another remains disabled and sends no data.
- Given enabled Jev and API permission denied, evaluation reports blocked
  before credential lookup and leaves the selected provider unchanged.
- Given a timeout, 401, 429, redirect, malformed JSON, oversized response,
  non-finite score, unknown choice, or model mismatch, the original operation
  remains available and no semantic conclusion is invented.
- Given a successful answer, human output visibly says Jev/advisory and JSON
  retains provenance even when the user collapses explanatory details.
- Given an off switch or permission revocation during a call, a late answer
  cannot change consumer state; the report does not deny already attempted egress.
- Given synthetic sentinel secrets in provider exceptions, normal logs and
  CLI JSON contain none of those sentinels.
- Given every Jev label replaced with an adversarial value, deterministic
  acceptance, authorization, and serving-route results remain unchanged.

## Qualification and metrics

Safety gates are exact regression assertions, not probabilistic targets.
Track calls, completed/unavailable counts, input tokens, latency percentiles,
abstentions, and user dismissals locally where an existing owner supports it.
Do not add analytics infrastructure. No content in telemetry. Report sample
size, denominator, concurrency, and environment for timing claims.

A question rubric is experimental until tested against frozen, independently
labeled cases with expected abstentions and adversarial cases. Keep tuning and
evaluation cases separate; freeze thresholds before evaluating holdouts.
Confidence alone is never an approval threshold. Do not claim universal
reliability from a small synthetic suite.

## Features

### F001: Explicit optional client
**Requirements:** R001, R002, R003, R005, R006, R007, R008, R009, R011

### F002: Consumer controls and privacy boundary
**Requirements:** R004, R010, R012, R013, R014

### F003: Qualification and delivery evidence
**Requirements:** R015, R016

## Tasks

### T001: Implement and test the bounded typed adapter
**Feature:** F001
**Priority:** high
**Likely files:** bin/src/anvil/jev.py, tests/test_jev.py
**Acceptance criteria:**
- Disabled behavior, strict typed validation, bounded transport, and safe
  errors satisfy R001–R009 without a new runtime dependency.
**Verification:**
- `uv run --project bin pytest tests/test_jev.py`

### T002: Add supported configuration, CLI, and disclosure
**Feature:** F002
**Priority:** high
**Likely files:** bin/src/anvil/config.py, bin/src/anvil/cli/jev.py, bin/src/anvil/cli/__init__.py, tests/test_jev_cli.py
**Acceptance criteria:**
- Status and per-capability controls work without secrets; evaluation is
  explicit, attributed, and never mutates a task or proof gate.
- A bounded audit of up to 16 explicitly selected items preserves every
  independent answer, refuses source/policy drift, and reports attempted calls.
**Verification:**
- `uv run --project bin pytest tests/test_jev_cli.py tests/test_config.py`

### T003: Record synthetic qualification and independent review
**Feature:** F003
**Priority:** high
**Likely files:** tests/fixtures/jev, docs/specs/jev-integration
**Acceptance criteria:**
- All shipped capabilities have frozen fixtures, result provenance, failure
  cases, and three distinct adversarial reviews before acceptance.
**Verification:**
- `uv run --project bin pytest tests/test_jev.py tests/test_jev_cli.py`

Verification commands naming new files become runnable with their task, not
evidence that implementation already exists.

## Non-Goals

No cloud processing by default; no Jev planner, proof authority, service,
autonomous agent, general plugin framework, SDK dependency, database migration,
automatic promotion, retraining, automatic retries, or provider switch.

## Risks

Secret-pattern redaction cannot establish safe export of arbitrary documents.
Prefer narrow selected fields and explicit source approval. Jev is susceptible
to adversarial text; isolation is enforced in code, not prompt wording. Even
correct semantic interpretation cannot establish that a submitted report is
authentic, recent, or attached to the right runtime.

## Open Questions

- Serving's existing agent-model policy requires the Claude Agent SDK for
  model-calling code. Before adding a direct vendor call there, record the
  narrow TypeSafe typed-assistance exception or use the existing Anvil process
  boundary; do not silently generalize this into direct Anthropic access.
- Production data retention terms and approved export classes must be decided
  before live enablement. Source delivery and synthetic tests need neither.

## Rollout and rollback

Ship source disabled. Run offline tests, then explicit synthetic checks.
Enable one capability only after its evidence is reviewed. Disable globally
to stop new calls; existing annotations remain labeled historical and cannot
be replayed into a gate. Rollback removes the optional consumer setting; base
Anvil/Serving operations require no provider cleanup or data migration.
