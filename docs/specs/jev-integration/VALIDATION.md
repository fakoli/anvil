# Jev integration: evidence and architecture review

Recorded 2026-09-20. Source delivery and live enablement are separate decisions.
No production workload, provider selection, route, deployment, or Anvil approval
was changed by these experiments. All seven capability defaults remain off.

## What exists

The four PRDs cover 57 requirements, 10 features and 10 proposed tasks. They
are registered as drafts, not approved state. Source implementation includes
the bounded Anvil transport/rubrics, seven explicit CLI commands, selected-item
audits, a stateless consumer bridge, Serving native controls, an authenticated
Workbench panel, and finalized-text voice sideband metadata.

The implementation uses explicit `anvil jev assess`, not an implicit call from
ordinary PRD review. It exports top-level acceptance criteria only. Evidence
advice is requested for selected claim/observation text and never replaces the
canonical proof-status surface. Workbench ranks manually selected optional
snippets; it does not discover private files, rewrite prompts, or enforce a
token budget. Voice integration is server-side metadata with per-connection
consent; a client must present the disclosure before opting in. No live audio
or production voice latency claim follows from text classification tests.

## Frozen live evaluation

The expected labels and corpus were frozen before the live run. The complete
[40-case result](evidence/qualification-2026-09-20.json) retains every question,
input, expected outcome, answer, source/rubric/input digest, timing, and usage.
Corpus SHA-256: `e4d3b39d83f25a93169f356aa72e6dec3d3634e06da322e14ecc13cf7e597810`.

| Group | Cases | Completed | Matched prewritten expectation |
|---|---:|---:|---:|
| Seven core capabilities | 28 | 28 | 28 |
| Stale handoff / scope audit | 4 | 4 | 4 |
| Release-note claim grounding | 4 | 4 | 4 |
| Missing regression observation | 4 | 4 | 4 |
| Total | 40 | 40 | 40 |

Pinned model: `jev-1.13.0`. Input tokens: 20,140; reported output tokens: 2,323.
Serial run: 12.395 seconds. Per-call end-to-end minimum/median/maximum:
266 / 304.5 / 401 ms, including the owned request worker. No retry was used.
This is a small, authored synthetic convenience sample, not independently
sampled production accuracy, a comparative productivity result, or calibrated
confidence. Forty matching examples do not establish prompt-injection safety.

The separate [consumer run](evidence/consumer-qualification-2026-09-20.json)
exercised the real Serving subprocess adapter against an independently
installed Anvil 0.6.12 wheel and TypeSafe: 4/4 completed. Consumer end-to-end
times were 661, 614, 625 and 615 ms for skill/context/incident/voice-text advice.
That includes CLI startup, unlike provider-only timing. It does not prove a
deployed fleet path or a one-second voice SLA under contention.

The [installed CLI audit run](evidence/audit-qualification-2026-09-20.json)
then exercised the three-item example end to end in disposable local state:
`insufficient`, `contradicts`, and `restart_persistence`, matching the three
prewritten expectations. The event log was byte-identical before and after
advice. This is separate from the frozen 40-case denominator.

Earlier exploratory research produced three requests and fourteen questions.
One initial fixture was ambiguous before clarification. Those observations
are separate from this frozen corpus and are not added to its denominator.

## Useful extra applications, using existing capabilities

The [three-item audit example](examples/audit.json) requires no new classifier
or background service. Enable only `evidence_triage` and `proof_contracts`,
then explicitly select/export the file with `anvil jev audit`.

**Handoff overclaim review.** Compare a handoff's exact assertion to the
reported observation. “Backend started” is narrower than “routed API ready.”
This could help a receiving agent notice what remains untested. Jev cannot
determine chronological freshness: timestamps, source digests and current
owner identity must be checked deterministically before relying on a handoff.

**Release-note grounding.** Compare a proposed release claim to its evidence.
Passing source tests does not establish deployment or a speed improvement.
Use the annotation to draw an author's attention to a sentence, not to edit or
publish it automatically. This is useful for evidence-conscious engineering
articles as well as release notes.

**Missing regression experiment.** Ask which observation category a claim
lacks: restart persistence, routed integration, authorization boundary, or
deployment identity. A category can seed a human-authored test plan. It cannot
produce proof, invent a successful execution, or revise an approved contract.

Each extra application matched 4/4 frozen synthetic cases. Real reviewer time
saved, false-positive burden, and relevant-context retention remain unmeasured.

## Review findings and repair history

The model being integrated did not review or approve its own implementation.
Independent software reviewers used deterministic tests and code inspection.

| Reviewer / angle | Finding and disposition |
|---|---|
| Privacy reviewer (Sol), Anvil export/configuration | Found lost concurrent config writes and per-operation rather than total HTTP timeouts. Fixed with the existing capture/verify atomic publisher and a killed/reaped per-request worker. |
| Privacy reviewer, revocation | Found disable/re-enable accepting late advice when effective settings were equal. Added project/global file-generation binding and pre/post-dispatch checks. |
| Separate Serving author reviewing Anvil proof boundary | No blocking finding in closed rubrics, state non-mutation, immutable approval boundary, or source binding; 185 focused checks passed at that review revision. |
| Separate client author reviewing Serving lifecycle/output | Found dispatch after local consent revocation, advisory cleanup delaying core stage shutdown, and digest syntax without input equality. Author added request-bound gates, reordered stop signaling, and exact sanitized-input digest checks. |
| Privacy reviewer, Serving configuration | Found concurrent disable lost at policy publication. Existing owner locking now covers every supported writer; opened-file generation rejects stale policy. Final security/privacy re-review passed in both repositories (Anvil 297 checks; Serving 104). |
| GitHub Copilot | Required independent current-head PR review before merge; final disposition belongs in the PR record. |

Initial full Anvil run: 5,316 passed, 85 skipped, two failures. The failures were
the CLI documentation count and frozen release contract. Updating the roster
and staging candidate 0.6.12 with a clean-wheel-derived contract repaired those
gates; no existing frozen release snapshot was rewritten. The subsequent full
run passed 5,323 tests with 85 skips; a later focused run including all Jev and
clean-wheel artifact checks passed 189. Ruff, strict module typing and strict
docs build passed. Final cross-platform CI and Copilot disposition belong to
the associated PR. Serving's independent lifecycle re-review passed all 52
focused tests after repairs; the author's wider regression gate passed 461
with one skip.

Serving's configured absolute Anvil executable is a trusted local boundary,
not a sandbox or hash-pinned binary. Supported config writers serialize under
the owner lock; arbitrary same-user file edits bypassing that lock are outside
that serialization contract. Secret filtering is heuristic, not DLP. The
current public Serving snapshot semantic scan found zero findings; history and
live credential rotation were outside this source integration's scope.

Chrome verification used loopback-only isolated Workbench fixtures. Observed:
default-off disabled request control; unchecked export consent; refusal before
consent; explicit “Ranked with Jev” attribution; exact original-order restore;
and clearing advice/consent with the local off switch. Enabled UI responses
were synthetic, not live provider calls. Separate Node DOM tests exercise late
result suppression; HTTP tests cover authentication, resource ACL and CSRF.

## How this helps future systems

The useful separation is **interpretation, then observation, then acceptance**.
Jev can cheaply narrow attention within text the owner already permits. Anvil
still owns claim identity, required observations, proof validation and human
acceptance. Serving still owns resources, sessions, cancellation and actions.

That separation makes more judgment calls possible without expanding what the
model may do. A future review queue can highlight likely scope gaps; an agent
can choose among already eligible skills; a diagnostic view can suggest which
existing read-only check to inspect next. Each should retain the baseline,
abstention, input identity, attribution, and an operation-local off switch.

Do not promote semantic labels into authorization, health gates, model routing,
automatic remediation, or proof truth. Do not build an always-on collector or
cache before workload evidence justifies the added privacy/identity complexity.
The next useful measurement is a blinded, independently labeled sample of
realistic reviewer mistakes, with approved source classes and baseline human
effort recorded. Until then, claims of saved time, reduced tokens, reliability
improvement, or production-grade accuracy are hypotheses.

Source merge does not certify standard-service data retention as appropriate
for private project text. Review current TypeSafe terms before operational use.
