# Project: Optional Jev incident triage and voice intent assistance

## Summary

Use Jev to label a selected operational observation or finalized utterance,
while preserving Anvil Serving's explicit resource ownership, fixed capability
routing, user confirmations, and voice cancellation behavior. Assistance must
never become an inference-based router or an operations controller.

## Goals

- Help an operator choose the next diagnostic area without declaring an
  unverified root cause or taking an operational action.
- Help a voice consumer distinguish conversational, informational, operational,
  and unclear requests without executing the classification.
- Surface Jev attribution and make each capability independently optional.
- Keep the main gateway and healthy voice path independent of Jev availability.

## Current behavior and reuse

Serving's `workbench_app/service.py` authorizes workload-log reads before
calling the configured log owner. Existing CLI/controller tools own bounded
logs and lifecycle. `voice/pipeline.py:TranscriptionToGenerate` bridges final
transcripts into generation requests; messages retain turn ID, revision, and
cancellation generation. `voice/cancel_scope.py` and the realtime owner manage
cancellation. The router selects one explicitly configured tier and never
classifies requests to choose an alternative. Reuse those boundaries.

## Requirements

- R001: `incident_triage` and `voice_intent` shall be independent opt-ins and
  shall not implicitly enable each other or another Jev capability.
- R002: Incident inputs shall be explicitly selected, bounded, locally
  sanitized observations from an authorized source. Do not scrape a fleet,
  export raw logs, or collect credentials/private topology automatically.
- R003: Triage shall classify observations into a closed diagnostic category
  or `unknown`, without asserting a root cause solely from model output.
- R004: Suggested next checks shall come from local reviewed read-only
  templates keyed by category, not generated commands or model-returned URLs.
- R005: Triage shall not restart services, load/unload models, alter networking,
  change routes, retry production requests, or promote a candidate. Any later
  user action uses the existing preview/confirmation/owner checks.
- R006: Jev shall remain outside the gateway request path, readiness/admission
  decisions, replica selection, model routing, and benchmark pass/fail logic.
- R007: Voice classification shall use only a selected final text transcript,
  never raw audio, partial transcripts, speaker biometrics, or conversation
  history by default. Text export requires explicit capability/data permission.
- R008: A voice result shall be a closed intent label plus provenance and
  optional uncertainty, not a tool call, command, tool arguments, or new route.
- R009: Intent shall not establish speaker identity, permission, target
  resource, consent, safety, or confirmation. Mutating requests shall still
  require the existing action-specific authorization and confirmation flow.
- R010: Stop/cancel/barge-in shall remain deterministic and must not wait for
  Jev. Late results shall be discarded using the original turn ID, revision,
  cancellation generation, and effective enablement.
- R011: Timeout, ambiguity, invalid response, missing credentials, or disabled
  classification shall preserve the original selected voice provider and
  ordinary pipeline. No cloud or local model substitution is permitted.
- R012: Diagnostic UI shall say “Jev triage suggestion”; voice controls shall
  disclose cloud text classification and show attributed intent metadata.
  Collapsed details must not obscure whether a request was sent.
- R013: Server-side authorization shall precede reading/exporting logs or
  transcripts and be rechecked before displaying delayed results. The API
  key shall never reach the browser, recorded transcript, or agent prompt.
- R014: Offline tests shall cover cancellation, timeout, permission revocation,
  malformed output, and no lifecycle/route side effects. Synthetic live cases
  shall retain all failures and must not operate real serving resources.

## Rubrics and illustrative journeys

Incident categories: `authentication`, `authorization_or_license`,
`missing_dependency`, `incompatible_configuration`, `resource_exhaustion`,
`connectivity`, `application_behavior`, and `unknown`.

An operator selects a safe diagnostic excerpt. A 401 observation may suggest
authentication checks; an allocation failure may suggest resource checks.
“Unknown” is preferred when the observation supports several explanations.
The original owner observation remains visible locally, with its time and
identity. Any corroborating log query requires the same permission as before.

Voice categories: `conversation`, `read_only_information`,
`operational_change_request`, `unclear`, and `unsupported`.
“Restart the service” is an operational-change request, not consent to execute.
“Tell me how to restart it” can be informational; ambiguous cases require the
ordinary caller clarification path. Jev cannot parse a safe target from a name
or certify that an utterance really came from the authorized user.

The voice classifier is advisory metadata after final transcription, not a
serial dependency that delays audio cancellation or selects the LLM. Consumer
code must bound worker/concurrency lifetime and suppress stale results. It
must not create an unbounded thread per utterance.

## Acceptance Criteria

- Given a selected diagnostic with a secret sentinel, no request/log/result
  contains the sentinel; denied export sends no network request.
- Given a Jev resource-exhaustion label, zero lifecycle calls, route changes,
  benchmark verdict changes, or new production requests occur.
- Given a 401, license denial, OOM, timeout, ambiguous symptom, and injected
  command, results remain closed categories with local read-only suggestions.
- Given “restart the model,” classification never calls restart, authorizes a
  tool, changes an alias, or bypasses a confirmation gate.
- Given cancellation during classification, audio cancellation happens using
  existing deterministic logic and the later intent result is discarded.
- Given slow/unavailable Jev, ordinary voice generation continues with the
  explicitly selected provider and does not accumulate outstanding workers.
- Given voice assistance disabled, no transcript is exported and no key is read.
- Given an authenticated consumer without log/transcript access, no snippet,
  count, metadata, or recommendation leaks from another resource or principal.

## Qualification and performance

Use synthetic incident cases spanning observed failures, insufficient context,
multiple possible causes, stale reports, embedded instructions, misleading
numbers, and denial vs authentication. Label “unknown” cases before testing.

Use voice text cases spanning information vs command, negation, quoting,
conditional statements, corrections, multilingual text, ambiguous targets,
and cancellation. Live audio/model/lifecycle testing is not needed to classify
text fixtures, and must not be claimed as performed by those fixtures.

Record class agreement, confusion matrix, unknown/unclear rate, elapsed time,
timeout rate, and cancel/stale suppression. Added blocking latency on the
ordinary voice path must be zero; classifier completion latency is a separate
measurement. No hard latency or accuracy SLA is established by the initial
exploratory API calls.

## Features

### F001: Safe incident classification
**Requirements:** R001, R002, R003, R004, R005, R006, R012, R013, R014

### F002: Optional voice intent metadata
**Requirements:** R001, R006, R007, R008, R009, R010, R011, R012, R013, R014

## Tasks

### T001: Implement incident rubric and explicit consumer
**Feature:** F001
**Priority:** medium
**Likely files:** bin/src/anvil/jev_questions.py, anvil_serving/workbench_app/service.py, tests/workbench
**Acceptance criteria:**
- Selected authorized excerpts yield attributed closed-category advice with
  no operational authority, automatic collection, or secret leakage.
**Verification:**
- `uv run --project bin pytest tests/test_jev_questions.py`
- `python scripts/run_tests.py tests/workbench/test_workload_logs.py tests/workbench/test_service.py -q`

### T002: Integrate cancel-safe optional voice intent
**Feature:** F002
**Priority:** medium
**Likely files:** anvil_serving/voice/pipeline.py, anvil_serving/voice/messages.py, anvil_serving/voice/cancel_scope.py, tests/voice
**Acceptance criteria:**
- Final text can receive attributed sideband intent; cancellation is never
  delayed, stale results are discarded, and ordinary provider selection stays fixed.
**Verification:**
- `python scripts/run_tests.py tests/voice/test_pipeline_spine.py tests/voice/test_vad_bargein.py tests/voice/test_realtime_service.py -q`

Both tasks depend on the foundation contract. Commands beginning `python
scripts/run_tests.py` run in the Serving repository; the Anvil command runs in
Anvil. Add implementation-specific tests before claiming task completion.

## Non-Goals

No intent-driven model router, automatic remediation, security certification,
voice biometrics, autonomous command execution, hidden fallback, new service,
unbounded log ingestion, new runtime dependency, or live fleet enablement.

## Risks

Incident descriptions may omit the actual cause. Transcription errors can
reverse intent. Cancellation races may attach a valid answer to the wrong
turn unless identity is checked. Audio users may not see a visual badge, so
settings/onboarding must explain the enabled export before the first request.

## Open Questions

Product-specific retention and allowed transcript export need owner decisions
before production activation. Keep defaults off while building/testing source.
Any future executable intent path requires a separate threat model and PRD;
this advisory implementation does not authorize it.
