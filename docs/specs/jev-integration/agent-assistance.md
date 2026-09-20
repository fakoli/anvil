# Project: Optional Jev skill and context assistance

## Summary

Help an agent or Workbench user find relevant skills and optional context
without giving a classifier control over instructions, permissions, providers,
or execution. Ranking is advisory and occurs only within an already authorized,
bounded candidate set. Users can see and disable the source of the ranking.

## Goals

- Reduce irrelevant optional context while preserving all required context.
- Suggest existing eligible skills, including a clear “none appropriate” result.
- Preserve host-session versus task-session authority and existing project ACLs.
- Make recommendation provenance and the original ordering inspectable.

## Current behavior and reuse

Serving's `workbench_app/projects.py` performs project authorization before
Anvil reads; `service.py` owns authenticated application operations.
`pi_sessions.py` and `pi_rpc.py` own managed Pi command dispatch. `pi_web.py`
manages a separate host service, not an interchangeable task runner. Reuse the
current session and command owners; never write Pi session files directly.
The workspace-renewal package is a proposed design, not evidence that its
multi-root or aggregation interfaces are already implemented.

## Requirements

- R001: `skill_suggestion` and `context_ranking` shall be independent opt-ins
  under the foundation policy, with unchanged behavior when disabled.
- R002: Candidate discovery, authorization, explicit skill requests, required
  instructions, and source exclusions shall run deterministically before Jev.
- R003: Skill candidates shall contain stable IDs and bounded public/safe
  descriptions of already available eligible skills. No credential, arbitrary
  full skill body, private path, or inaccessible skill may enter the request.
- R004: Skill selection shall permit a closed candidate ID or `none`; returned
  unknown IDs are invalid. Explicitly user-requested or policy-required skills
  shall never be vetoed by a model recommendation.
- R005: A recommendation shall not install or invoke a skill, execute a tool,
  change the provider, grant egress, or bypass the harness's skill-reading rules.
- R006: Context ranking shall operate only on optional, already authorized
  snippets with opaque IDs and local source digests. ACL checks precede counts,
  truncation, ranking, and network transmission.
- R007: System/developer instructions, repository guidance, current user
  requirements, active work packet, proof requirements, and explicit attachments
  shall be pinned independently of relevance scores. If pinned content exceeds
  budget, report that condition rather than silently dropping it.
- R008: Jev shall return relevance scores, not rewrite or summarize source
  content. Local stable sorting and deterministic budget accounting shall decide
  ordering; ties retain original order and uncertain items remain available.
- R009: A consumer shall retain the baseline candidate list and a restore action.
  Optional ranking shall not silently make lower-ranked content undiscoverable.
- R010: All results shall bind to candidate-set digest, request intent digest,
  project, principal, and session/turn generation locally. Reauthorization is
  required before use; no cross-user cache or shared session result is allowed.
- R011: Empty, denied, oversized, stale, unavailable, and ambiguous candidate
  sets shall return an explicit unavailable/none/unchanged result rather than
  expanding discovery scope or inventing candidates.
- R012: Consumer presentation shall say “Suggested/ranked with Jev” and expose
  model, source scope, experimental confidence, and the applicable off switch.
  Collapsing details shall not erase machine-readable provenance.
- R013: A host Pi session shall not acquire a managed task's roots, credentials,
  or write authority through a recommendation; task-bound sessions keep their
  immutable bindings. No Pi provider-allowlist expansion is implicit.

## Proposed interaction

For skill suggestions, the caller supplies a short user intent and a bounded
set of candidate ID/description pairs. One closed Choice question selects an
eligible candidate or none. Start with one suggestion; additional candidates
can be requested in an explicit later operation, not an unbounded ranking loop.
Skill invocation remains a user/harness action outside this adapter.

For context ranking, the caller supplies intent plus selected optional snippets.
Independent three-level Score questions assess irrelevant, partially relevant,
or directly useful context. Local code uses the scores only to order these
snippets and keeps the original order for recovery. Model confidence is shown,
not treated as a validated cutoff. Do not infer token counts from Jev; retain
the consumer's deterministic accounting and budget.

The first useful consumer is an explicit CLI/JSON contract usable by harnesses.
The Workbench consumer must additionally show an attributed recommendation and
restore/disable controls. A generic evaluator alone does not prove the UI is
integrated. Browser requests are authenticated server-side; no TypeSafe key is
ever exposed to JavaScript.

## Acceptance Criteria

- Given an explicit user skill selection, an irrelevant Jev suggestion cannot
  suppress the selected skill or skip its required instructions.
- Given no relevant skill, `none` is a successful advisory answer and no skill
  is invoked. A candidate ID not in the authorized set is rejected.
- Given an unauthorized project/snippet, it is absent from request bodies,
  candidate counts, digests shown to other principals, and results.
- Given optional context below pinned instructions, Jev can change only the
  optional order; all mandatory instruction/contract content is preserved.
- Given equal scores, ordering is stable; given provider failure, baseline
  ordering and ordinary skill discovery remain available.
- Given a permission or session change during evaluation, the stale result is
  discarded and cannot be attached to the new session.
- Given disabled Jev, no candidate content or credential is read for Jev and
  the ordinary Workbench/Pi workflow remains usable.

## Qualification

Freeze cases for direct matches, paraphrases, misleading overlapping names,
irrelevant candidates, no candidates, ambiguous intent, multilingual intent,
required skill conflicts, duplicate IDs, prompt injection, long snippets,
cross-project ACL isolation, stale results, and budget overflow.

Measure top-choice agreement and none accuracy for skills; use independently
labeled relevant-snippet retention and baseline comparison for context. A
smaller prompt is not automatically a better prompt. Demonstrate relevant
context preservation before claiming token or outcome improvements.

## Features

### F001: Bounded skill suggestion
**Requirements:** R001, R002, R003, R004, R005, R010, R011, R012, R013

### F002: Optional context ranking
**Requirements:** R001, R002, R006, R007, R008, R009, R010, R011, R012, R013

## Tasks

### T001: Implement skill and context rubric contracts
**Feature:** F001
**Priority:** medium
**Likely files:** bin/src/anvil/jev_questions.py, tests/test_jev_questions.py
**Acceptance criteria:**
- Bounded authorized candidate inputs support typed skill/none selection and
  relevance scores, with no install, execution, or instruction authority.
**Verification:**
- `uv run --project bin pytest tests/test_jev_questions.py`

### T002: Integrate attributed consumer recommendations
**Feature:** F002
**Priority:** medium
**Likely files:** anvil_serving/workbench_app/service.py, anvil_serving/workbench_app/projects.py, tests/workbench
**Acceptance criteria:**
- Current authenticated consumer surfaces preserve pinned context, immutable
  session bindings, baseline ordering, and per-capability disable behavior.
**Verification:**
- `python scripts/run_tests.py tests/workbench/test_service.py tests/workbench/test_projects.py tests/workbench/test_pi_sessions.py -q`

T001 depends on the foundation. T002 runs in the Serving repository after its
consumer seam is inspected; the command above uses that repository's runner.
Add focused new tests to the same verification gate when implementing.

## Non-Goals

No new skill marketplace, autonomous installation, hidden skill invocation,
embedding database, retrieval crawler, agent scheduler, generated context,
provider replacement, Pi Web fork, or implementation of the entire workspace
renewal redesign.

## Risks

Descriptions can contain hostile instructions; code must constrain outputs.
Ranking can hide useful evidence, hence baseline restoration and pinned content.
An apparently harmless snippet can still be confidential; user read permission
and export permission are different. Short context can harm reasoning quality.

## Open Questions

The first Workbench insertion point must use a currently supported owner; if
the requested UX depends on unshipped workspace-renewal work, record that
dependency instead of asserting it exists. Wider candidate retrieval and
automatic context removal require separate outcome evidence and approval.
