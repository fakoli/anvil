# Issue #246: research and review register

Reviewed 2026-10-08 against fetched Anvil main `f7d9f265fd2061da3e1b50f8e80b0e76de237298`. Companion: [delivery plan](../plans/2026-10-08-issue-246-long-running-delivery.md).

**Scope:** issue bodies and comments for the parent, six workstreams, direct historical references, and relevant linked background issues; current source tracing; a disposable evidence-reader reproduction; the cited OpenAI guidance. No live proof repair or state mutation was performed. Incident timings and historical delivery claims below are source-reported unless explicitly identified as reproduced.

## Issue inventory

GitHub state observed during this review:

| Issue | State | Reviewed role / disposition |
|---|---|---|
| [#246](https://github.com/fakoli/anvil/issues/246) | Open | Parent outcome and cross-project pilot acceptance. Keep the existing six-way scope. |
| [#247](https://github.com/fakoli/anvil/issues/247) | Open | High-priority complete-import defect. Reproduced in the pinned code. |
| [#248](https://github.com/fakoli/anvil/issues/248) | Open | High-risk accepted-attempt invalidation. Depends on #247; no ordinary terminal-task rejection workaround. |
| [#249](https://github.com/fakoli/anvil/issues/249) | Open | Frozen repository verification profiles. Contract/materialization work, with runner ownership retained by the repository. |
| [#250](https://github.com/fakoli/anvil/issues/250) | Open | Attempt-aware bounded composition, extending existing reads. |
| [#251](https://github.com/fakoli/anvil/issues/251) | Open | Shipped-command cleanup first, later integration with delivered features. |
| [#252](https://github.com/fakoli/anvil/issues/252) | Open | Derived timing with explicit measurement gaps. Not a prerequisite for correctness. |
| [#108](https://github.com/fakoli/anvil/issues/108) | Closed | Historical branch naming, literal proof matching, and workspace ergonomics. Do not reopen those concerns as import completeness. |
| [#153](https://github.com/fakoli/anvil/issues/153) | Closed | Semantic evidence contracts; closeout reports 11 tasks delivered in v0.5.0. Reuse current assertions, categories, and claim bindings. |
| [#171](https://github.com/fakoli/anvil/issues/171) | Closed | Execution bundles and throughput policy. Existing implementation is the foundation, not a new orchestrator proposal. |
| [#178](https://github.com/fakoli/anvil/issues/178) | Closed | Bounded versioned provider reads. Closeout records merged PR #204; reusable contracts and privacy restrictions. |
| [#177](https://github.com/fakoli/anvil/issues/177) | Closed | Background linked by #178: persisted PRD titles, explicit reparse for legacy rows. Reads must not silently repair source/state. |
| [#242](https://github.com/fakoli/anvil/issues/242) | Open | Optional Jev measurement. Its September pilot comment reports a missed benefit target and disabled automatic consumption. Keep independent. |
| [anvil-serving#191](https://github.com/fakoli/anvil-serving/issues/191) | Closed | Historical #171 motivating regression: advertised confirmation flags disagreed with real parsers. Supports testing installed commands, not changing Serving here. |

The parent and six new workstreams had no comments when reviewed. Their latest edits were `2026-10-08T07:00:07Z` through `2026-10-08T07:00:26Z`; #242's latest comment was `2026-09-26T20:42:13Z`. Historical PRs and external incident artifacts are background references, not additional implementation workstreams or independently revalidated releases. This is a bounded linked-issue review, not a recursive audit of every historical PR.

## Findings grounded in current source

### F1: the reader returns an incomplete success-shaped prefix

[The shared reader][reader] stops when `len(proofs) == 16`, without establishing EOF. Existing oversized, unreadable, symlink, or nonregular input returns `[]`. Its return type cannot distinguish a missing optional buffer from a failed import. Malformed, historical unattributed, and cross-claim records are skipped. Attribution and semantic digests are checked; output hashes remain trusted hook observations rather than reread output verification.

All three submission consumers call that reader: [ordinary CLI][submit], [MCP][mcp-submit], and [root-set evidence][roots-submit]. A correction only in one caller would leave the other two affected. The existing [bounded-reader regression][reader-test] explicitly expects 17 input records to produce 16 proofs and an oversized file to produce an empty list, so passing today's tests does not demonstrate completeness.

**Review conclusion:** #247 accurately identifies a shared defect. Change the common result/error contract and its callers together; replace tests that codify silent truncation. Keep permissive historical-record skipping distinct from inability to inspect the complete bounded input.

### F2: recovery must be an explicit guarded operation

[SQLite apply validation][apply-check] checks current review/evidence binding and rejects live rejection outside `needs_review`; [workflow apply][workflow-apply] also enters the same `task.applied` boundary. A new CLI flag alone cannot establish safe recovery. Append locking, typed payload validation, replay, latest-attempt identity, idempotency, and dependent/custody checks all matter.

[Acceptance proof export][proof-export] signs a historical receipt. Append-only invalidation can remove prospective reliance in native state without making that old signature cryptographically invalid. The operator/read-view contract must explain both facts.

**Review conclusion:** keep #248 separate in behavior and validation even if the existing coordinator implements it alongside #247. Do not broaden ordinary rejection or edit signed history. Resolve offline-consumer and dependent-enumeration limits before describing invalidation as universally effective.

### F3: profile materialization has existing seams, but the runner is not interchangeable

[Verification][verification] already holds commands, required proofs, and artifact assertions. [Packet rendering][packets] emits those contracts. Project config has no named verification-profile contract at this revision. [The workflow executor][workflow-executor] runs trusted proof strings with `shell=True` and uses workflow-specific proof types; it is not automatically a portable, shell-free verification-profile runner.

**Review conclusion:** materialize into existing verification fields, adding only necessary profile/source bindings. Test runner portability and command identity explicitly; do not document `run-workflow` as a drop-in profile implementation. Environment preflight, evidence preflight, and successful tests are three different results.

### F4: lightweight presentation is not proof of complete resume context

[Lightweight packet rendering][packet-trim] truncates free-text `required_evidence` for display, while leaving the authoritative verification object unchanged. Reusing that mode wholesale could hide a requirement from an agent even though enforcement still requires it. [Provider snapshots][snapshots] already offer a query-only consistent read and bounded contracts, providing a better foundation for freshness and overflow semantics.

[Packet metrics][packet-metrics] use whitespace splitting. Those counts are a proxy and cannot be reported as billed-token savings. “Thousands of historical references” also requires a bounded scan/retrieval design, not merely a short serialized answer after an unbounded history read.

**Review conclusion:** #250 needs a required-fact coverage test as well as a size test. Carry mandatory facts or stable retrievable references, explicit overflow, and a state frontier. Avoid a second summary database.

### F5: instruction contradictions are concrete

[Claim guidance][claim-skill] asks for acknowledgement before ordinary submission, then says immutable approval is the only mandatory confirmation in that lifecycle leg. Its blanket auto-release wording also needs qualification for retained root custody. [Finish guidance][finish-skill] offers an advisory light tier, while [repository policy][agents] requires three independent adversarial reviews before acceptance.

**Review conclusion:** #251 should align guidance with authorization and the applicable repository policy. Preserve the three-angle floor here, capability checks, reviewer independence, and the human acceptance gate. A generic skill's lighter example must not override project rules. Current-command cleanup can precede any new API.

### F6: timing data is incomplete in an asymmetric way

[Hook `CommandProof`][hook-proof] records `captured_at` but has no start/end duration. [Explicit claim-command evidence][claim-proof] has UTC `started_at`/`ended_at`, but validates `exit_code == 0`. Thus explicit proofs can describe successful command intervals, while that artifact type alone cannot represent timed nonzero failures. [Progress notes][progress] can be free-form and do not require an active claim; they are not automatically trustworthy per-generation interval boundaries. Existing [metrics][metrics] cover acceptance/review debt rather than all proposed phases.

**Review conclusion:** #252 must expose unknown historical durations and separate observed from inferred intervals. Research the smallest attributable failure/timing receipt only if existing retained records cannot satisfy the pilot. Do not weaken completion-proof success rules to store failures, or reinterpret notes as qualifying renewal progress.

### F7: current native work overlaps the critical path

Read-only status and `anvil show native-evidence-correction:T001 --json` resolved an existing claimed implementation task covering bounded import refusal and exact acceptance invalidation. It reserves the shared reader, root/MCP entry points, state/payload/workflow paths, tests, and CLI reference. The task's frozen description is narrower than the full epic; in particular, preflight discovery and later packet/profile/metrics integration still need coverage mapping.

**Review conclusion:** preserve that task's owner and active contract. Do not create a second #247/#248 implementation claim from this research. “Claimed” is native state, not proof that its runner is currently computing or its implementation is complete.

## Disposable reproduction

At the pinned source, ran the existing focused test command recorded in the [plan](../plans/2026-10-08-issue-246-long-running-delivery.md#validation-and-acceptance): **6 passed, 32 deselected, 5.31 seconds** on Linux/Python 3.14.7. These are baseline behavior tests, not future acceptance tests.

A separate temporary-directory probe reused `tests/test_proof_gate.py` helpers `_command_record` and `_write_buffer` to construct distinct valid attributed records. Record zero had exit code 1; subsequent records had exit code 0. It called `_read_command_proofs` for each size and checked the buffer SHA-256 before/after.

| Valid records | Returned proofs | Last input record included | Original bytes unchanged |
|---:|---:|---|---|
| 0 | 0 | Not applicable | Yes |
| 15 | 15 | Yes | Yes |
| 16 | 16 | Yes | Yes |
| 17 | 16 | No | Yes |
| 39 | 16 | No | Yes |

The initial failed result remained visible in every nonempty returned prefix. This reproduces truncation without dropping failures or modifying a real project. It does **not** reproduce a live acceptance incident, establish full caller atomicity, or test all #247 boundary cases. Those remain implementation acceptance work.

Documentation validation also passed: `git diff --check`, the strict MkDocs build with the repository's declared documentation dependencies, and checks that pinned source links/line ranges exist and the new documents contain no local home/workspace or private network paths. Full product, Windows, replay, and end-to-end acceptance suites were not run for this documentation-only review.

## Research items and decision gates

All items below are **open**. Owners are proposed roles, not claims or assignments. Update this register with the selected decision, pinned evidence, and validation result when resolved; native task state remains authoritative for execution.

| ID / issue | Question and current recommendation | Bounded investigation and required output | Owner / blocks |
|---|---|---|---|
| R1 / #247–#248 | What does the already claimed correction task deliver? Reuse its ownership; do not amend its frozen contract implicitly. | Compare its current packet, implementation/PR if available, and final evidence with both issue checklists. Record covered behavior and residual preflight/discovery work. | Existing correction coordinator; before another claim or overlapping edit. |
| R2 / #247 | What exactly constitutes complete input at item/byte limits and under concurrent append/replace? Prefer one typed shared result/refusal and bounded diagnostics. | Specify absent/complete/skipped/incomplete states; EOF at 16, malformed suffixes, byte boundary, replacement/append race, no-follow behavior, and generation/source drift. Trace hook writer synchronization and submission append boundary; demonstrate no success from an advisory preflight after drift. | Evidence owner; blocks reader/preflight contract. |
| R3 / #247 | What supported action follows overflow without losing old captures? Safe refusal may ship first, but remediation must be honest. | Exercise existing stop/release/readiness/fresh-generation path in a disposable project. Determine where it is available and where custody requires owner action. If insufficient, record a separate bounded design decision. No trimming, last-16 selection, passing-only filtering, limit increase, or historical relabeling. | Evidence and lifecycle owners; blocks claims of end-to-end overflow recovery. |
| R4 / #248 | How are exact invalidation retries, dependent consumers, and historical proof authenticity represented? Prefer narrow append-only semantics and fail-closed unknowns. | Design CAS tuple and idempotency identity; inspect task/bundle/root/cross-project consumer visibility. Test duplicate and changed requests, later accepted generation, concurrent dependent claim, active custody, unauthorized reviewer, missing confirmation, crash/replay. State what offline proof verification cannot determine. | State/review owner; blocks recovery mutation design. |
| R5 / #249 | Where is profile/source identity frozen, and how does it map to exact proof commands across platforms? Prefer expanding existing `Verification`. | Prototype schema/materialization on Python and shell-free executable fixtures. Test config/profile edits mid-claim, path escape/root mismatch, unsupported platform, separate author/reviewer fixtures, resource refusal, preflight failure and interrupted execution. Produce a short runner command and non-secret config example. | Contract owner plus consuming repository runner owner; blocks profile implementation. |
| R6 / #250 | What is the smallest versioned attempt view with complete required-fact coverage? Prefer existing packet/read entry points. | Freeze DTO, identity/frontier, size and scan limits, stable artifact references/retrieval, unknowns and overflow behavior. Compare full/lite/resume views with many historical attempts and same-numbered cross-PRD tasks. Record mandatory-fact recall and bytes; label whitespace proxy correctly. | Read-contract owner; blocks compact surface. |
| R7 / #251 | Which confirmations and repeated checks are redundant, and which are mandatory? Prefer one shared policy explanation. | Audit four skills plus installed/package copies, AGENTS and command manifests. Separate routine authorization from immutable acceptance, retained custody and forced-ownership cases. Test actual CLI/MCP and built artifacts, ordinary/bundle paths, and an interrupted/rejected/reworked scenario. Keep model choices optional and harness-owned. | Plugin/harness owner; blocks claiming workflow consistency. |
| R8 / #252 | Which phase and failed-run intervals are actually derivable? Prefer explicit unknowns over fabricated history. | Map each requested duration/count to event/proof fields and attribution. Cover hook single timestamps, explicit success-only proof intervals, free-form notes, overlapping commands and clock skew. Add a minimal typed observational receipt only for a demonstrated gap; preserve completion and renewal semantics. | Metrics/state owner; blocks complete timing claims. |
| R9 / #246 | What makes the pilot comparable and worth shipping? Keep the same required checks and review policy. | Predeclare two comparable future cycles, source/environment/runner and reviewer configuration, packet/evidence budgets, cold/warm conditions, retries and external waits. Record raw sanitized receipts, required-fact coverage, handoff size, redundant reads, dispatch delay, failures and total elapsed time. Report limitations; no invented percentage target or model speedup. | Pilot coordinator and independent outcome reviewer; blocks efficiency claims/epic closeout. |

## External guidance and limits

These are supporting principles, not additional authority to alter Anvil governance:

- [OpenAI: long-horizon Codex work](https://developers.openai.com/blog/run-long-horizon-tasks-with-codex) supports durable plans with milestones, validation, decisions, and retained status. Application here: linked repository documents for research and existing Anvil records for task execution.
- [OpenAI: memory and compaction](https://developers.openai.com/cookbook/examples/agents_sdk/building_reliable_agents_memory_compaction) separates continuing a run from carrying reusable lessons across runs; cited facts remain in reviewable artifacts. Application here: compact attempt views reference authoritative evidence rather than replacing it with prose memory.
- [OpenAI: skills/instruction audit](https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra) recommends narrow skill triggers, progressive disclosure, and removing stale/redundant instruction scaffolding. Application here: reduce repetitive orientation without deleting project-specific evidence, ownership, and review requirements.
- [OpenAI: model prompting and verification](https://developers.openai.com/api/docs/guides/latest-model) recommends checks proportional to the change and repetition when changes, failures, or unresolved concerns justify it. Application here: meet declared checks once at final source, preserve independently required reviewer runs, and investigate repeated failures.
- [The deployment checklist](https://developers.openai.com/api/docs/guides/deployment-checklist) linked by #251 is broader production guidance, not evidence that a particular model assignment or delegation policy improves Anvil throughput. This review makes no model comparison; capability examples should be refreshed when implemented.

The [#242 pilot report](https://github.com/fakoli/anvil/issues/242#issuecomment-5849714600) reports that semantic selection missed its target and retained the deterministic baseline. No new Jev/model call is needed to settle deterministic completeness, identity, arithmetic, or ownership questions in this plan.

## Evidence boundaries

- Main revision and clean checkout equality were verified by fetch plus `rev-parse` / `rev-list`; the review did not inspect another runner's private working edits.
- The reported 39-capture incident, roughly 192 KB handoff, and environment-failure timings in the issues were not independently reconstructed. Only the synthetic reader behavior was reproduced here.
- Future schemas, names, APIs, recovery operations, and timing receipts are proposals until implemented and qualified. Existing tests passing does not make them shipped.
- This register stores only product-level conclusions and identifiers. Raw issue exports containing incidental operational details and local state snapshots are not added to product documentation.

[reader]: https://github.com/fakoli/anvil/blob/f7d9f265fd2061da3e1b50f8e80b0e76de237298/bin/src/anvil/cli/packet_apply.py#L90-L196
[submit]: https://github.com/fakoli/anvil/blob/f7d9f265fd2061da3e1b50f8e80b0e76de237298/bin/src/anvil/cli/packet_apply.py#L1061-L1064
[mcp-submit]: https://github.com/fakoli/anvil/blob/f7d9f265fd2061da3e1b50f8e80b0e76de237298/bin/src/anvil/mcp_server.py#L2059-L2062
[roots-submit]: https://github.com/fakoli/anvil/blob/f7d9f265fd2061da3e1b50f8e80b0e76de237298/bin/src/anvil/cli/roots.py#L730-L749
[reader-test]: https://github.com/fakoli/anvil/blob/f7d9f265fd2061da3e1b50f8e80b0e76de237298/tests/test_strict_evidence.py#L978-L999
[apply-check]: https://github.com/fakoli/anvil/blob/f7d9f265fd2061da3e1b50f8e80b0e76de237298/bin/src/anvil/state/sqlite.py#L12966-L13056
[workflow-apply]: https://github.com/fakoli/anvil/blob/f7d9f265fd2061da3e1b50f8e80b0e76de237298/bin/src/anvil/workflows/tasks.py#L171-L209
[proof-export]: https://github.com/fakoli/anvil/blob/f7d9f265fd2061da3e1b50f8e80b0e76de237298/bin/src/anvil/cli/packet_apply.py#L239-L284
[verification]: https://github.com/fakoli/anvil/blob/f7d9f265fd2061da3e1b50f8e80b0e76de237298/bin/src/anvil/state/models.py#L984-L1012
[packets]: https://github.com/fakoli/anvil/blob/f7d9f265fd2061da3e1b50f8e80b0e76de237298/bin/src/anvil/context/packets.py
[workflow-executor]: https://github.com/fakoli/anvil/blob/f7d9f265fd2061da3e1b50f8e80b0e76de237298/bin/src/anvil/cli/run_workflow.py#L24-L46
[packet-trim]: https://github.com/fakoli/anvil/blob/f7d9f265fd2061da3e1b50f8e80b0e76de237298/bin/src/anvil/context/packets.py#L487-L508
[snapshots]: https://github.com/fakoli/anvil/blob/f7d9f265fd2061da3e1b50f8e80b0e76de237298/bin/src/anvil/project_snapshot.py
[packet-metrics]: https://github.com/fakoli/anvil/blob/f7d9f265fd2061da3e1b50f8e80b0e76de237298/bin/src/anvil/context/packet_metrics.py#L32-L34
[claim-skill]: https://github.com/fakoli/anvil/blob/f7d9f265fd2061da3e1b50f8e80b0e76de237298/skills/claim/SKILL.md#L212-L240
[finish-skill]: https://github.com/fakoli/anvil/blob/f7d9f265fd2061da3e1b50f8e80b0e76de237298/skills/finish/SKILL.md#L250-L277
[agents]: https://github.com/fakoli/anvil/blob/f7d9f265fd2061da3e1b50f8e80b0e76de237298/AGENTS.md
[hook-proof]: https://github.com/fakoli/anvil/blob/f7d9f265fd2061da3e1b50f8e80b0e76de237298/bin/src/anvil/state/models.py#L581-L635
[claim-proof]: https://github.com/fakoli/anvil/blob/f7d9f265fd2061da3e1b50f8e80b0e76de237298/bin/src/anvil/state/models.py#L642-L719
[progress]: https://github.com/fakoli/anvil/blob/f7d9f265fd2061da3e1b50f8e80b0e76de237298/bin/src/anvil/cli/progress.py
[metrics]: https://github.com/fakoli/anvil/blob/f7d9f265fd2061da3e1b50f8e80b0e76de237298/bin/src/anvil/claims/metrics.py
