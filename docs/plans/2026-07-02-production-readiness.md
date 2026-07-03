# Production-Readiness Plan — 2026-07-02

> Synthesized from three evidence sources, triangulated on 2026-07-02:
> **(a)** six real-session retrospectives in
> [fakoli/post-session-findings](https://github.com/fakoli/post-session-findings)
> (four anvil-loop sessions on Claude Code, one on Codex, 16–38 h each);
> **(b)** a five-agent hands-on reproduction of every onboarding path against
> the **published** `anvil-state 0.3.0` (README quickstart both variants,
> getting-started walkthrough, `anvil install`/`mcp-config`/`install.sh`,
> MCP stdio smoke); **(c)** the repo's own recorded evidence
> (benchmarks, evals, audits, postmortems). Strategy context:
> [`../backlog/strategic-backlog.md`](../backlog/strategic-backlog.md) (S1–S13).
>
> Items marked **SHIPPED (this change)** landed in the same PR as this plan.

## 1. Strengths — proven, and to be put at the forefront

These are not aspirations; each has a recorded run behind it.

1. **The full loop closes, at production scale, repeatedly.** A 23.7 h
   autonomous session shipped **32 tasks / 21 PRs / 1 release** through the
   anvil loop (multiprd session, 2026-06-23); two concurrent loops completed
   **18/18 tasks of a PRD in 16.7 h**; the loop ported to Codex. The
   reproduction confirmed both README quickstart variants close end-to-end on
   the published package, ending in a signed Ed25519 proof on disk.
2. **Claims deconflict real concurrent sessions with zero negotiation.**
   "anvil's exclusive claim deconflicted two sessions with zero explicit
   negotiation" (harness-router-build). This is the single-winner moat working
   in the field, not just in the benchmark (which itself measured collisions
   3.0 → 0.0 and found two engine races before users ever could).
3. **Gates catch real defects every time they run.** Across every session:
   fail-open deny gates, log injection, semantic merge breaks, a CHANGELOG
   that omitted its own headline feature. The strict PRD validator refused a
   malformed requirement ID **before** it could ship a broken plan.
4. **The lean-orchestrator shape holds.** 90/10 delegation kept a 28M-token
   run coherent; the operator steered 1,224 turns with 9 messages.
5. **Context frugality is measured**: ~2.4k always-on tokens
   (`benchmarks/CONTEXT_AUDIT.md`) against an 18.6k-token competitor tax.

**Forefront actions:** a "Proven in real sessions" section in the README with
these numbers (SHIPPED, this change); the remaining items are S5/S6/S9 in the
strategic backlog (commit the gaming benchmark + critic numbers, then quote
them).

## 2. Weaknesses — ranked by production impact

### W-A. Onboarding says one thing, the tool does another *(the reported issue — confirmed)*

Every first-run surface taught the legacy `./.anvil` layout while the default
is a HOME workspace (`~/.anvil/workspaces/<key>/.anvil`): a doc-literal user
authors a PRD the parser never reads and fails at step 3. Worse, it was not
just docs: **`anvil claim` ran its git ops in the workspace dir**, so the
promised `agent/<task>-<slug>` branch was *never* created in the default
layout; `anvil install`'s dry-run printed **"(wrote)"** while writing nothing;
the shipped `AGENTS.md` told harnesses to run `bin/anvil`, a path that doesn't
exist for installed users; `docs/mcp.md` never mentioned that 10 of its 24
documented tools are off the wire by default.

**SHIPPED (this change):** project-dir git resolver for claim (+ regression
test), honest dry-run output + trailer, AGENTS.md bare entrypoints (all three
synced copies), status showing claimed/needs_review/done buckets, README
Quick Start / getting-started / prd-template / mcp.md rewritten around the
real layout with regenerated example output.

**Remaining:**
- **A1. PRD pre-flight surface.** Real sessions lost a rework cycle
  discovering the strict `R0NN` requirement-ID rule by failing. Docs now
  state it (this change); follow with `anvil prd rules` (or `prd parse
  --explain`) printing the authoring contract, and referencing it from the
  init hint. *(S: small)*
- **A2. CLI-satisfiable evidence gate.** A plain-CLI user following the
  README sees "Evidence gate: INCOMPLETE" with no way to satisfy it — typed
  exit-code proofs only come from run hooks, so the signed proof carries
  empty `command_results`. Add `anvil submit --run` (execute the declared
  commands, capture exit codes into `CommandProof`s). This is also the
  natural seam for strategic S1 (`anvil verify` re-execution). *(M)*
- **A3. Cross-harness self-description.** The Codex session burned five
  human turns discovering/porting Claude-specific tooling. Ship
  `anvil describe --json` prominently in AGENTS.md and per-harness install
  guides (E10/E12 territory). *(M)*

### W-B. Silent-failure defaults *(top production risk from real sessions)*

The three worst field incidents were all silent:

- **B1. Lease expiry.** >15-min workflows silently lost their lease; tasks
  reverted to `ready`; the standing workaround was `--lease 240`.
  **SHIPPED (this change): default lease is now 240 min** (max-claim-age 4×
  still caps wedged agents at 16 h). Follow-up: `next`/`claim` should print a
  one-line notice when the reaper released a stale claim, and `renew` refusal
  should say *why* (expired vs max-age). *(S)*
- **B2. Shared-actor co-claims.** Two loops under the default actor silently
  defeated mutual exclusion (both claimed T001). Fix in two steps:
  (i) `claim`/`next` warn when the resolved actor already holds an active
  claim on a *different* task and no explicit `--actor` was given;
  (ii) document distinct `--actor` as a hard multi-loop precondition in the
  new production-ops guide (D3). Longer-term: per-process actor suffix
  opt-in (`ANVIL_ACTOR_UNIQUE=1`). *(M)*
- **B3. Skippable `submit`.** Skipping `anvil submit` surfaces only later as
  an `apply` error. Make `apply` name the missing step explicitly and make
  `status` show `claimed`-but-unsubmitted tasks (partially SHIPPED via the
  new claimed bucket); the strict-gate default flip (strategic S2) closes it
  fully. *(S, then S2)*
- **B4. Windows cp1252 crashes** (issue #106): `submit`/`apply` crashed on
  `→`/`↳` glyphs. **SHIPPED (this change): stdout/stderr auto-reconfigure to
  UTF-8** with lossless fallback. Follow-up: a Windows job in the CI matrix
  so glyph regressions can't land. *(S)*

### W-C. Trust boundary and unmeasured verification *(the moat gap — strategic backlog owns it)*

The evidence gate is advisory by default (S2), proofs attest hook-recorded
exit codes an agent could fabricate (S1 — re-execution + countersignature),
`get_next_task` bypasses the governed pull seam (tech-debt E13-2 — mechanical
fix, do first), the real critic false-pass rate is a placeholder (S6), and the
`evidence_gaming` benchmark has no committed numbers (S5). The bake-off (S7)
gates E13. Sequencing unchanged from the strategic backlog; this plan adds
**E13-2 as the immediate next code item** since it is small and closes a
real bypass.

### W-D. Production-operations gaps *(what "true production environment" needs)*

- **D1. Review-cost tiering.** 78% of a 31M-token run went to uniformly-max
  code review; two hard budget walls were hit. anvil already scores six
  dimensions per task — surface a **suggested review tier** (complexity ×
  blast_radius × review_risk → light/standard/max) in `packet` and `next
  --json`, so harness loops can right-size review spend at claim time. This
  turns an existing differentiator into the fix for a measured cost problem.
  *(M — highest-value new feature in this plan)*
- **D2. Progress visibility.** 4 of 6 sessions had "is it stuck?" check-ins.
  The one session without them used anvil task-status posting — the fix
  already exists in the product. Ship `anvil status --watch` (or a
  lightweight `notify-digest` cron recipe, which exists for OpenClaw) and
  document the pattern. *(S–M)*
- **D3. A "Running anvil in production" guide**: multi-loop preconditions
  (distinct actors, worktree isolation, fresh-base fetch before branch),
  lease/heartbeat tuning, backup/restore (`anvil backup`/`restore` ship
  today), replay recovery, Windows notes. Content exists across six retros;
  it needs one page. *(S)*
- **D4. Claim-time freshness.** Stale-base branches cost 2 merge cycles and
  a 2.3M-token wasted review in the field. Offer `claim --fetch` (or a
  claim-time hook) running `git fetch` before branching, warning when the
  base is behind origin. *(S)*
- **D5. Windows CI + packaging checks** (see B4) and a nightly live-sync
  workflow (docs/live-tests.md currently references a workflow file that
  does not exist — reframed in docs this change; commit the secret-gated
  workflow next). *(S)*

## 3. Sequencing

1. **Now (this change):** everything marked SHIPPED above + docs overhaul +
   version bump 0.3.1 so `/plugin marketplace update` and PyPI users pick it
   up.
2. **Next code batch (small, silent-failure class):** E13-2 pull-seam
   routing → B1 reap notice → B2 actor warning → B3 apply hint → A1 prd
   rules.
3. **Feature batch:** A2 `submit --run` → strategic S1 `anvil verify`
   (shares the execution seam) → S2 strict-auto default → D1 review-tier
   surfacing.
4. **Measurement batch (before any new marketing claim):** S5 gaming
   benchmark (advisory vs strict vs verify arms) + refreshed 4×3 coordination
   run → S6 critic false-pass → S7 bake-off decision on E13.
5. **Ops batch:** D2 visibility, D3 production guide, D4 claim-time fetch,
   D5 Windows CI + live-sync workflow.

## 4. Success measures

- A new user on a clean machine reaches `anvil next` → merged branch →
  signed proof following only the README, no source-diving. (Reproduction
  agents re-run green on the released package.)
- Zero silent state transitions: every lease expiry, stale reap, actor
  collision, and skipped step prints its cause at the moment it happens.
- The README quotes only measured numbers, each linking to a committed
  artifact (RESULTS.md, CONTEXT_AUDIT.md, critic baseline, replay check).
- A two-loop production run completes a PRD with review spend ≤⅓ of the
  uniform-max baseline (D1), no budget-wall interventions, and no "is it
  stuck?" messages (D2).
