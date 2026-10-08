# Issue #246: long-running delivery plan

Prepared 2026-10-08. **Status: implementation authorized as an unattended goal; final project validation remains with the user.** Live historical recovery and infrastructure changes remain outside this source-build scope.

## Execution authorization

The user authorized completion as an unattended goal on 2026-10-08, with final project validation and no interim approval steps. Routine implementation decisions, intermediate source milestones, tests, independent reviews and integration may proceed under that standing authorization. Required review, evidence, custody and CI gates remain in force. The coordinator must not impersonate a human reviewer, weaken a review floor, recover unrelated live tasks or change serving.

Execution is tracked in the existing shared Anvil workspace under the named PRD `issue-246-long-running-delivery`. Eight tasks cover profile contracts, profile planning, claim enforcement, current-attempt reads, timing, correction/preflight integration, shipped guidance, and cross-project qualification. The existing `native-evidence-correction:T001` remains the sole writer for its overlapping evidence/recovery contract. Integration must wait for its reservations to clear and its reviewed source to be available.

The profile design uses a repository-owned `anvil-verification.toml`, explicit name/platform/manifest-digest references in the canonical PRD, and frozen digests of declared runner files. Updating a profile requires an ordinary PRD revision and review. The parser stays pure; planning supplies an explicit repository root. Existing commands/proof requirements remain authoritative, and absent profile fields retain legacy serialization.

The current-attempt read must enter a query-only boundary before ordinary packet generation's lease maintenance or sidecar writes. Provider snapshot composition is a consistency precedent, but its full-history hashing is unsuitable as a bounded scan implementation. Timing must include process rejections that existing acceptance-rate metrics deliberately omit.

Tracking: [#246](https://github.com/fakoli/anvil/issues/246). Evidence, source inventory, and unresolved decisions: [research register](../research/2026-10-08-issue-246-long-running-delivery.md).

## Recommendation

Keep the six existing issues. Deliver complete-import refusal (#247) before accepted-attempt recovery (#248). Develop verification profiles (#249), compact read views (#250), and shipped-command skill cleanup (#251) independently where reservations permit. Derive timing (#252) from recorded facts; it must not delay the correctness fixes.

The decomposition is sound, but execution needs these clarifications:

1. **Reconcile existing ownership first.** Native state already contains a claimed task covering the core of #247 and #248. Do not create a competing task or writer.
2. **Separate safe refusal from usable recovery.** Refusing a 17th proof fixes silent incompleteness; it does not itself give a long claim a supported path back to verification. Resolve that operator path without deleting captures, selecting passing results, or raising limits.
3. **Freeze shared identities once.** Evidence preflight, resume packets, profiles, and timing must agree on project/PRD/task, claim/generation, evidence/review attempt, source identity, and observation frontier. Reuse existing types rather than inventing parallel identities.
4. **Keep missing facts visible.** A lightweight packet can currently omit declared evidence text. A captured-at timestamp does not establish command duration. Neither should become a false completeness claim.
5. **Keep governance explicit.** Skill cleanup must preserve this repository's three independent adversarial review angles and human confirmation before immutable acceptance. Advisory review tiers cannot lower that floor.

## Verified baseline and ownership

- `git fetch origin` completed. The initial clean checkout and `origin/main` both resolved to `f7d9f265fd2061da3e1b50f8e80b0e76de237298`, with zero commits ahead or behind. This is also the revision pinned in #246–#252.
- The installed CLI passed `anvil prd source-name --help`. MCP project status resolved the existing shared `anvil` workspace; no initialization was needed.
- Native state reported `native-evidence-correction:T001`, **claimed**, titled “Implement bounded import refusal and exact acceptance invalidation.” Its claim was `C1E4D4DD8`, generation 1. This is a dated coordination observation, not permission to assume ownership or evidence of implementation completion.
- That task reserves the evidence reader, roots/MCP submission, SQLite/payload/workflow mutation paths, affected tests, and CLI reference. Those overlap #247/#248 and later integration work.
- The existing `execution-scheduler-v0` bundle also reports `replan_required`. It is not a resumable container for this epic; #246 does not authorize migrating or resetting it.
- No open Anvil PR was returned by the GitHub query during this review. Fetch and inspect native ownership again before implementation; unpublished work may exist.

The original review changed documentation only. The subsequent execution authorization created the named native PRD; task/claim/evidence status belongs in Anvil. GitHub remains the issue tracker. These documents retain design and research evidence, not a second lifecycle ledger.

## Delivery sequence

| Slice | Owner boundary and work | Dependency / research gate | Exit evidence |
|---|---|---|---|
| P0: reconcile | Existing correction coordinator maps its frozen task contract to #247/#248; identify missing preflight, discovery, or documentation work explicitly. | Refresh native task, claim, source, reservations, and any PR. Research R1. | Coverage map and one authorized writer; no duplicate claim or silently amended active contract. |
| P1: complete imports, #247 | Shared reader returns complete bounded results or a typed refusal; all three callers use it. Add read-only preflight through the smallest existing surface. | R2 and R3. Correctness first. | 0/15/16/17/39 records, exact EOF/byte boundaries, invalid records, source drift, and all caller refusal checks. No evidence/task/claim/root mutation on refusal. |
| P2: exact recovery, #248 | Narrow append-only invalidation using the existing review/state service; fresh ordinary generation and verification afterward. | P1; R4. Existing coordinator owns overlapping mutations. | Exact target/CAS checks, independence and confirmation, custody/dependent refusal, idempotency, crash/replay equivalence, immutable historical hashes, fresh acceptance proof. |
| P3: profiles, #249 | Resolve repository-owned profile into existing `Verification`; freeze digest/source before claim. Runner owns environment setup and actual execution. | R5. Independent of P2; integrate P1 preflight only after it ships. | Two structurally different disposable projects, author/reviewer isolation, failure and drift cases, legacy compatibility. Preflight never counts as test completion. |
| P4: compact context, #250 | Extend packet/read composition with a bounded current-attempt view and authoritative references. | R6. Initial version uses shipped facts; integrate P1/P2/P3 later. | Consistent frontier, stable payload/digest, explicit unknowns/overflow, complete required-fact coverage, cross-PRD identity and large-history tests. |
| P5a: existing-command guidance, #251 | Remove contradictory routine confirmation examples and repeated orientation; clarify actual custody and review rules. | R7. May ship independently on unreserved files. | CLI/skill/install contract checks and ordinary-task/bundle resume scenarios. No future command described as available. |
| P5b: integrated guidance, #251 | Add delivered preflight, profile, compact-packet, and recovery procedures with capability/version checks. | Relevant P1/P2/P3/P4 surfaces actually delivered. | Built-wheel and plugin agreement; interrupted/rejected/reworked scenario retains required gates and actual stop/release evidence. |
| P6: timing, #252 | Extend native read projections with attributable attempts, known intervals, blockers, and unknowns. | R8; can begin on existing events. Integrate with P4/P5. | Multi-attempt, overlapping, missing, skewed-clock, replay, and custody fixtures; wall time distinct from summed execution time. |
| P7: pilot and closeout, #246 | Reuse all slices in disposable end-to-end projects and measured future cycles. | R9; required slices qualified. | Lifecycle acceptance below and a comparison that includes failures and waits. |

Logical independence does not imply safe simultaneous edits. #247/#248 share `packet_apply.py`, roots, MCP, and state paths. #249/#250 share models and packet composition; #250/#252 share read contracts. Freeze contracts and integrate sequentially where those files overlap. Keep one coordinator for any execution bundle and preserve per-task evidence and disposition.

## Frozen implementation requirements

### Evidence and recovery

- Distinguish absent optional buffer, complete input with skipped invalid records, and unreadable/incomplete input. Report bounded counts/reasons without returning a success-shaped prefix or reflecting raw hostile content.
- Preserve the 16-item / 1,048,576-byte bounds, attribution/digest validation, file-type and no-follow checks, all failures, and original buffer bytes. Do not choose the latest 16, deduplicate, or filter for passing commands.
- Prove EOF at the limit and refuse uncertainty or mutation during inspection. Preflight is time-specific advice. Submission must revalidate current input and bindings at the authoritative mutation boundary.
- Bind invalidation to the exact accepted event, evidence/review attempt and digest, revision, original actor/claim/generation, and applicable root-set identity. Refuse stale targets, conflicting custody, and reliant active/completed consumers; no cascade reset.
- Repeat identical requests safely, including after a newer successful generation. Never reopen that newer generation. Preserve old signed bytes and distinguish authenticity from current acceptance authority.
- Continue recovery through supported readiness, new claim, current verification, submission, independent review, human gate, and a new ordinary acceptance proof. Source delivery is separate from permission to recover a live task.

### Profiles and read views

- Keep the runner and short user-facing verification command in the consuming repository. Reuse conventional non-secret config. No generic supervisor, automatic interpreter installer, or product-specific machine paths.
- Pin expanded commands and profile/source identity before execution. Preserve literal matching and required full-suite checks. Keep author and reviewer runs separately attributable with separate fixtures.
- Carry exact mandatory requirements/findings or supported stable references. An overflow cannot silently omit a blocker. Preserve full packet access and history.
- Resolve current versus historical attempt facts from one consistent observation. Unknown stop/release, missing evidence, pending human decisions, and external waits remain explicit. Advice never grants mutation authority.
- Retain privacy boundaries of each surface; do not copy raw logs, host identities, secrets, or executable source text into generic provider snapshots.

### Skills and timing

- Reuse valid authorization for routine work while retaining independent review and human-only gates. A coordinator validates verdict identity and release evidence; it need not repeat the reviewers' full source assessment.
- Run required checks at final source. Repeat for changed code, failed checks, or an unresolved concrete risk. Repeated same-class failure needs a different, falsifiable diagnostic step.
- Include every attempt and failure. Distinguish observed execution time, inferred intervals, running intervals, and unknown measurements. Use interval unions for elapsed overlap; report summed execution separately.
- Phase notes and timing reads must not renew claims, clear blockers, imply runner stop, change scheduling, or equate accepted source with merged/deployed state.
- Keep Jev #242 outside the dependency chain. Its recorded pilot missed the benefit target; this plan has no basis for automatic semantic selection or a speedup claim.

## Validation and acceptance

For this documentation review, the evidence-reader baseline check was run:

```sh
uv run --project bin --frozen pytest tests/test_proof_gate.py tests/test_strict_evidence.py -k 'read_command_proofs or hook_buffer_reader_is_bounded' -q
```

Result: **6 passed, 32 deselected**. This confirms existing behavior, including the truncation defect; it is not evidence that #247 is fixed. The [research register](../research/2026-10-08-issue-246-long-running-delivery.md#disposable-reproduction) records the separate 0/15/16/17/39-record probe.

Implementation should extend the existing relevant suites, with these responsibilities:

| Work | Existing validation homes |
|---|---|
| #247 | `test_proof_gate.py`, `test_hooks.py`, `test_strict_evidence.py`, `test_command_proof_artifact.py`, `test_root_set_evidence.py`, `test_mcp.py`; include all three submission callers. |
| #248 | `test_review.py`, `test_sqlite.py`, `test_replay_equivalence.py`, root-set tests; the existing correction task also names its proposed `test_native_evidence_correction.py`. |
| #249 | `test_config.py`, packet, model, CLI/MCP and runner fixtures; prove no-profile compatibility. |
| #250 | `test_packet_quality.py`, `test_project_snapshot.py`, CLI/MCP/read-contract coverage. |
| #251 | `test_skill_cli_contract.py`, `test_agent_plugin_manifest.py`, `test_install_manifests.py`, installed-artifact compatibility and disposable workflows. |
| #252 | `test_progress_cli.py`, `test_bundle_status.py`, snapshot/metrics fixtures. |

Follow repository lint, packaging, documentation, and CI requirements for each implementation change. Review the final source with at least three independent adversarial angles before presenting an Anvil task for acceptance: evidence/caller behavior, state/replay/ownership, and surface compatibility/privacy. Resolve blocking findings and repeat affected checks. Human confirmation remains required before immutable `anvil apply --approve`; review completion alone does not grant it.

Epic closeout requires:

- [ ] One disposable lifecycle includes a real failed attempt, independent rejection, normal rework, fresh evidence, acceptance, and actual custody release.
- [ ] A second structurally different project uses the same native contracts without Serving-specific assumptions.
- [ ] Exact accepted-attempt recovery is demonstrated separately with all refusal guards and byte-identical historical proof/evidence.
- [ ] Both ordinary and bundle paths preserve their respective ownership and review rules.
- [ ] Bounded current context retains all required facts or retrieves them through supported references; raw history remains available.
- [ ] At least two comparable future cycles report environment retries, evidence processing, handoff, review, rework, stop/release, next dispatch, and external waits, with unknowns and failures included.
- [ ] Installed CLI/MCP/plugin schemas and examples match delivered commands; CI, replay, and required platform checks pass.

No implementation outcome or performance improvement is claimed by the baseline review. Source mapping and independent review now resolve the remaining research items during the authorized build; outcomes must be pinned to final source and verification evidence.
