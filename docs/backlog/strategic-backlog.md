# anvil Strategic Backlog — moat execution track

> Added 2026-07-02 from a full product / product-market-fit / moat review
> (repo docs + code + external landscape), with every code-level claim below
> verified against shipped source by a five-stream fact-check pass. This doc
> is a **strategic overlay**: it sequences and sharpens existing items from
> [`roadmap.md`](../roadmap.md) (SL/WF waves) and
> [`anvil-backlog.md`](anvil-backlog.md) (E1–E13 / B-items) rather than
> replacing them. Items are numbered `S*` to avoid colliding with either.
> The same review corrected ~25 stale doc claims (docs said several shipped
> features were unbuilt — B46, B47, B48, replay, multi-provider LLM); see the
> PR that introduced this file.

## The one-sentence strategy

**Status is downstream of proof — make that literally true, measure it, then
distribute it.** The commoditized pillars (durable state, orchestration) are
conceded per [`_positioning.md`](../_positioning.md); the defensible position
is the closed verification loop: typed evidence → enforced gate → signed
proof → **independent re-execution** → countersigned, portable, replayable
artifact. Nobody in the researched landscape ships that fusion: agentic-os /
EviBound have hard gates but no portable artifact; AGEF / Proof-of-Insight /
Pipelock have signed artifacts but no task/claim/state binding; Temporal has
leases but no evidence concept; Beads has memory but deliberately no
governance. The moat is the loop, plus two compounding assets: the verified
history accumulating in every `.anvil/` (switching cost) and measured,
honest numbers (credibility).

Where the loop stands today (verified 2026-07-02):

| Link in the loop | Status |
|---|---|
| Typed proofs (`CommandProof` w/ exit code, `DiffProof`, `LinkProof`, `AssertionProof`) | **Shipped** (B48 pt 1) |
| Signed portable `AcceptanceProof`, auto-minted on approve, off-host `anvil proof verify` | **Shipped** (B48 pt 2) |
| Unified actor identity (`ANVIL_ACTOR` resolver, signer-fingerprint fallback) | **Shipped** (B47) |
| Lease hardening (max-claim-age cutoff in `renew()`) | **Shipped** (B46) |
| Replay guarantee proven in CI (`anvil replay` + `tests/test_replay_equivalence.py`) | **Shipped** (SL-1) |
| Evidence gate **enforced by default** | **Open — S2** (advisory: `strict_evidence` defaults `False`) |
| Independent **re-execution** of proof commands | **Open — S1** (nothing re-executes; proofs trust the hook writer) |
| The numbers that prove it works (gaming benchmark, real critic false-pass) | **Open — S5/S6** |

---

## Wave 1 — Make the headline claim true

The README's headline promise ("no completed work without proof") holds only
under opt-in strict mode, and a proof's authenticity rests on a trusted hook
writer (`state/models.py` TRUST BOUNDARY; `review/gates.py`
`_proof_satisfies` docstring: *"A harness in which the agent can write the
evidence buffer can still fabricate a passing CommandProof"*). Wave 1 closes
those two gaps. Everything else in this doc is downstream of them.

### S1 — `anvil verify <task>`: independent re-execution + countersigned AcceptanceProof

- **Priority:** P0  **Effort:** L  **Type:** feature  **Status:** OPEN
- **Rationale:** The single highest-leverage product move. A signed proof of
  an agent-recorded result is laundered trust: `CommandProof.output_sha256`
  is recorded, never re-verified, and no code path anywhere re-executes a
  submitted command (the only fresh command execution in the tree is
  `run-workflow`'s executor, which is not re-verification). Re-execution
  converts *recorded* proof into *reproduced* proof — the one link no
  competitor in `docs/research/2026-06-20-competitive-analysis-agent-state-tools.md`
  ships (hard gates without artifacts, artifacts without binding, neither
  with re-execution). It is also the safety prerequisite E13's fleet thesis
  names for letting weak/unsupervised local runners pull work, and the
  literal implementation of "status is downstream of proof."
- **Acceptance:** `anvil verify <task-id> [--json]` (1) resolves the task's
  accepted evidence and its `CommandProof`s; (2) re-runs each pinned command
  in a clean checkout of the claim's branch (fresh worktree by default;
  `--in-place` escape hatch), honoring a configurable timeout; (3) compares
  exit codes against `passing_exit_codes` (output hashes compared when
  present, reported as informational — command output need not be
  deterministic); (4) on success appends a verifier **countersignature**
  block (separate keypair identity via the existing `signing` module) to the
  proof JSON under `<state>/proofs/`, and `anvil proof verify` validates
  worker + verifier signatures and reports both; (5) on mismatch exits
  non-zero with a per-command report and appends a `task.verification_failed`
  event (additive; replay stays green — proof files themselves remain outside
  replayable state, as today). Works headless so CI can run it. Docs: a
  "verify" section in `cli-reference.md` + the trust-boundary paragraph in
  `design.md` updated to point at it.
- **Likely files:** `bin/src/anvil/cli/verify.py` (new), `bin/src/anvil/signing.py`,
  `bin/src/anvil/state/models.py` (`AcceptanceProof.countersignatures`),
  `bin/src/anvil/git_ops/`, `bin/src/anvil/cli/proof.py`, `tests/`
- **Depends on:** — (B48 both parts shipped)

### S2 — Enforce the evidence gate by default for tasks that declare typed proofs

- **Priority:** P0  **Effort:** M  **Type:** modify  **Status:** OPEN
- **Rationale:** As shipped, `strict_evidence` defaults `False`
  (`config.py`), the default `apply --approve` path approves with a failing
  or absent gate, the SQLite `task.applied` write path performs
  needs_review→accepted→done with no evidence check at all, and the enforcing
  `transitions.task_needs_review_to_accepted()` exists but has no caller.
  Meanwhile the direct competitors for the verification wedge hard-block
  (agentic-os unbypassable CI gate; EviBound's measured 100%→0% hallucinated
  completion). Flipping the default *only where the planner emitted typed
  `required_proofs`* keeps quick-fix/legacy flows unbroken while making the
  README headline true for every engine-created task.
- **Acceptance:** `strict_evidence` gains an `auto` mode and it becomes the
  default: strict iff `task.verification.required_proofs` is non-empty;
  explicit `true`/`false`/`--strict`/`--no-strict`/`$ANVIL_STRICT_EVIDENCE`
  still win (existing precedence preserved). Applies identically to CLI
  `apply` and MCP `apply_review`. MCP `submit_completion_evidence` (which
  today runs no gate at all) returns the advisory gate verdict in its
  envelope so agents see missing items at submit time, before review.
  Belt-and-braces: the apply write path routes through (or asserts the same
  predicate as) `task_needs_review_to_accepted` so the gate cannot be
  bypassed by a front door that forgets to check. CHANGELOG + migration note;
  `design.md` §gate updated; the README headline sentence restored to its
  strong form once this lands.
- **Likely files:** `bin/src/anvil/config.py`, `bin/src/anvil/cli/packet_apply.py`,
  `bin/src/anvil/mcp_server.py`, `bin/src/anvil/state/transitions.py`,
  `bin/src/anvil/state/sqlite.py`, `tests/`
- **Depends on:** — (pairs naturally with S1: verify-then-accept flows)

### S3 — Name the replay-equivalence CI check and surface the audit guarantee

- **Priority:** P2  **Effort:** S  **Type:** infra  **Status:** OPEN
- **Rationale:** SL-1 shipped (v1.19.0) but runs anonymously inside the main
  pytest job, so the strongest credibility artifact anvil has — "replaying
  the log reconstructs the database, proven on every PR" — is invisible to an
  evaluating user. A named status check + one README sentence converts an
  existing engineering fact into a marketing fact at near-zero cost.
- **Acceptance:** CI exposes a separately-named `replay-equivalence` check
  (job or step-level reporting) running `tests/test_replay_equivalence.py`;
  README's audit-guarantee paragraph links to it. No engine changes.
- **Likely files:** `.github/workflows/ci.yml`, `README.md`
- **Depends on:** —

### S4 — Proof-format interop: export `AcceptanceProof` to an existing portable envelope

- **Priority:** P2  **Effort:** M  **Type:** feature  **Status:** OPEN
- **Rationale:** The competitive analysis recommends *adopt, don't invent*:
  AGEF / Proof-of-Insight / Pipelock already define portable proof
  envelopes with ecosystems forming around them. anvil's differentiator is
  the **binding** (task + claim + actor + event range), not the envelope
  format. An `anvil proof export --format <fmt>` adapter keeps the moat
  (binding + verify loop) while neutralizing "yet another proprietary proof
  file" as an adoption objection — and positions anvil to *ingest* others'
  proofs later. Also the hedge the positioning doc's kill-trigger names: if a
  platform standardizes a proof format, anvil collapses into schema + adapters.
- **Acceptance:** Pick one target format after a short spike (spike doc in
  `docs/research/`); `anvil proof export` emits it losslessly enough that a
  third-party verifier of that format accepts the signature; round-trip test.
- **Likely files:** `bin/src/anvil/cli/proof.py`, `bin/src/anvil/state/models.py`, `tests/`
- **Depends on:** S1 (export is most valuable once proofs can carry countersignatures)

---

## Wave 2 — Make it measurable

A verification product with unmeasured verification is an unverified claim.
Today the only committed numbers are the coordination benchmark's single
scenario (`overlapping_files`, 8 actors, 1 trial, seed 42, **measured on
v0.0.8** — two-plus releases old) and the mock critic's 0.25 false-pass
baseline. The scenario that IS the thesis — `evidence_gaming` — has a harness
but **no committed numbers**, and the real-LLM critic number is an explicit
placeholder.

### S5 — Commit `evidence_gaming` benchmark numbers across gate modes; refresh the headline on current code

- **Priority:** P1  **Effort:** M  **Type:** infra  **Status:** OPEN
- **Rationale:** EviBound markets "hallucinated completion 100%→0% at ~8.3%
  overhead." anvil needs its own honestly-caveated sentence of that shape,
  and the harness already exists (`benchmarks/harness/scenarios.py` defines
  `evidence_gaming`; RESULTS.md just never populated it). Run it in three
  arms — advisory (today's default), strict (S2), strict+verify (S1) — and
  the resulting table is simultaneously the release gate for S1/S2 and the
  centerpiece of the launch post. Re-run `overlapping_files` on current code
  at the same time so the committed headline stops citing v0.0.8.
- **Acceptance:** `benchmarks/RESULTS.md` gains a populated
  `evidence_gaming` section (gamed-submission catch/refuse rates per arm,
  seeds/trials stated) generated on the current version, plus a refreshed
  `overlapping_files` run; README/status quotes the strongest defensible
  sentence with its caveats. Multi-trial (not 1 trial) so the number
  survives scrutiny.
- **Likely files:** `benchmarks/harness/`, `benchmarks/RESULTS.md`, `README.md`
- **Depends on:** S2 (strict arm); S1 (verify arm — can land as a follow-up arm)

### S6 — Measure the real critic false-pass rate (retire the placeholder)

- **Priority:** P1  **Effort:** M  **Type:** infra  **Status:** OPEN
- **Rationale:** `docs/critic-false-pass-baseline.md` ships a committed
  number only for the mock backend (false-pass 1/4 = 0.25); the real-LLM
  table is marked "PLACEHOLDER — not yet measured" and the `api` backend
  raises `NotImplementedError`. The critic gate is a marketed differentiator;
  its central quality number cannot stay TBD. Maps to roadmap SL-2.
- **Acceptance:** The `api` critic backend is wired (default via the Agent
  SDK subscription path, consistent with `--use-llm`); the fault-injection
  suite runs against it (opt-in/costed, like `evals/`); the baseline doc's
  placeholder table is replaced with measured false-pass / false-fail rates
  and the measurement recipe; a CI-adjacent (manual-trigger) workflow keeps
  it re-runnable.
- **Likely files:** `bin/src/anvil/planning/critic_falsepass.py` (or current home),
  `docs/critic-false-pass-baseline.md`, `evals/`
- **Depends on:** —

### S7 — Run the B50 capacity bake-off before building more of E13

- **Priority:** P1  **Effort:** M  **Type:** research  **Status:** OPEN
- **Rationale:** E13 (fleet/capacity coordination) is the boldest strategic
  bet and its economic premise — pools throttle often enough that draining
  several flat-rate pools + local spillover matters — rests on
  `docs/research/2026-06-21-bake-off-results.md`, which is a stub. The
  positioning doc already defines kill/pivot triggers; they can only fire if
  the experiment runs. Two loops, two weeks, publish whatever it says —
  either it funds E13 or it saves a quarter of misdirected work.
- **Acceptance:** The bake-off doc is populated with the measured throttle
  frequency, spillover behavior, and packet-quality observations from ≥2
  concurrent pools on real work; an explicit go / narrow / kill call on E13
  scope is recorded in `anvil-backlog.md` E13 header.
- **Likely files:** `docs/research/2026-06-21-bake-off-results.md`,
  `docs/backlog/anvil-backlog.md`
- **Depends on:** — (S1/S2 strengthen the "safe to let weak runners pull" arm but aren't blockers)

---

## Wave 3 — Distribution: be the proof layer, not another tracker

### S8 — Beads/Gas Town interop: "Beads remembers; anvil proves"

- **Priority:** P1  **Effort:** M  **Type:** feature  **Status:** OPEN
- **Rationale:** Beads (~18.7k stars) owns backlog/memory mindshare and
  *deliberately* omits governance — close is a manual one-liner, `--claim` is
  one-shot with no lease/heartbeat, and there is no evidence concept. Gas
  Town runs 20–30 parallel agents whose merge queue trusts unverified worker
  output. That is anvil's exact wedge, adjacent to the category's largest
  audience. Interop turns the mindshare leader into a distribution channel
  instead of a competitor: a bd↔anvil sync provider (the
  `docs/sync-providers.md` contributor path exists for exactly this) or, at
  minimum, a documented recipe wiring anvil's gate + proofs under a
  Beads-managed backlog. Positioning sentence: **"Beads remembers what your
  agents did; anvil proves they did it."**
- **Acceptance:** Either (a) a `beads` sync provider mapping bd issues ↔
  anvil tasks (evidence/proofs stay anvil-side; status projects both ways,
  same shape as the GitHub provider), or (b) a how-to +
  glue commands demonstrating a Gas Town/Beads loop where completion requires
  `anvil apply` strict-gate acceptance — chosen after a 1-day spike on bd's
  current CLI/JSONL surface. A short post/README section announces the
  integration.
- **Likely files:** `bin/src/anvil/sync/` (provider), `docs/how-to/`,
  `docs/sync-providers.md`
- **Depends on:** S2 (the recipe's value is the enforced gate)

### S9 — Surface the measured claims where evaluators look

- **Priority:** P2  **Effort:** S  **Type:** docs  **Status:** PARTLY DONE (this PR)
- **Rationale:** anvil's strongest evaluator-facing facts were buried:
  measured always-on context cost (~2,433 tokens, execution surface, vs
  spec-kit's community-reported ~18.6k always-on tax — the loudest complaint
  class in `competitor-issue-analysis.md`) lived only in
  `benchmarks/CONTEXT_AUDIT.md`; the replay guarantee ran unnamed in CI
  (S3); several shipped verification features were still described as
  unbuilt in anvil's own docs. The PR introducing this file fixed the
  context-cost README row and ~25 stale claims; the rest lands with S3/S5.
- **Acceptance:** README comparison table carries the context-cost row
  (done); benchmark + false-pass numbers quoted with caveats once S5/S6
  land; a "what's measured" index section in README Status linking
  RESULTS.md / CONTEXT_AUDIT.md / critic baseline / replay check.
- **Likely files:** `README.md`, `docs/_positioning.md`
- **Depends on:** S3, S5, S6

### S10 — Ride MCP Tasks (SEP-1686) as a transport for claims/packets

- **Priority:** P2  **Effort:** M  **Type:** feature  **Status:** OPEN
- **Rationale:** Already the competitive analysis' recommendation: MCP Tasks
  is the transport the platforms are converging on; anvil should be the
  durable, governed backend behind it rather than a parallel vocabulary.
  Neutral-transport interop is also the best defense against the
  platform-absorption failure mode the positioning doc documents.
- **Acceptance:** Spike doc mapping anvil's claim/packet/evidence lifecycle
  onto the MCP Tasks spec as it stabilizes; implement the mapping behind a
  flag if the spec is stable enough; otherwise record the go/no-go and
  revisit trigger.
- **Likely files:** `bin/src/anvil/mcp_server.py`, `docs/research/`
- **Depends on:** —

### S11 — Sequencing guard: brownfield ingest (E5) starts only after S1/S2/S5

- **Priority:** P2  **Effort:** —  **Type:** decision  **Status:** OPEN
- **Rationale:** E5 (scan/ingest an existing repo; non-feature task types) is
  the biggest TAM expansion — the "underserved 75% of real work" — and the
  most tempting next build. But it widens the product before the central
  claim is true and measured; every new surface built on an advisory gate
  compounds the say/do gap that Wave 1 exists to close. This item exists to
  make the deferral explicit and reviewable rather than accidental.
- **Acceptance:** E5 work items stay unstarted until S1, S2, and S5 are DONE
  or this item is consciously overridden with a note here.
- **Depends on:** S1, S2, S5

---

## Process items

### S12 — One canonical planning surface (and it should be anvil)

- **Priority:** P2  **Effort:** M  **Type:** process  **Status:** OPEN
- **Rationale:** Two unreconciled planning artifacts (`roadmap.md`'s SL/WF
  waves with legacy v1.x/v2.x buckets; `anvil-backlog.md`'s E/B items) plus
  this overlay is one too many. Agents plan from these files; divergence is
  compounding. anvil now supports multi-PRD projects — dogfooding the
  roadmap *as anvil state* (a `planning` PRD whose tasks are the S/B items)
  is both the fix and a product demo. Also: this review found ~25 doc claims
  contradicted by shipped code, all of one class — prose restating facts the
  code owns. Where cheap, assert doc-stated counts in tests (hook count, tool
  count, provider count, schema version) the way `test_version_sync.py`
  already pins versions.
- **Acceptance:** One doc (or anvil project) is declared canonical for
  sequencing, the others reduced to reference/index; a small
  `tests/test_doc_sync.py` asserts the highest-drift counts; stale-claim
  class tracked in `tech-debt-backlog.md` if not closed.
- **Likely files:** `docs/roadmap.md`, `docs/backlog/*.md`, `tests/test_doc_sync.py` (new)
- **Depends on:** —

### S13 — Skill-vs-CLI drift eval (pull forward postmortem action (a))

- **Priority:** P1  **Effort:** M  **Type:** infra  **Status:** OPEN
- **Rationale:** The 2026-06-22 init-loop postmortem's deep cause — SKILL.md
  prose duplicating operational knowledge the CLI owns, with nothing keeping
  them in sync — has already produced one high-severity agent-facing outage
  and names sibling risks in `claim`, `plan`, `execute`,
  `resolve-decisions`. Agents are anvil's primary users today; this is the
  highest-recurrence-risk gap in the product's actual UX. The postmortem
  lists the eval as "being built next" — schedule it, don't intend it.
- **Acceptance:** An `evals/` (or plain pytest, where offline-checkable) case
  per skill exercising its documented flow against the default workspace
  layout, failing on path/command drift like the 06-22 incident; wired to CI
  where offline, `evals/` where costed.
- **Likely files:** `evals/`, `tests/`, `skills/`
- **Depends on:** —

---

## Explicitly deprioritized (until Waves 1–2 land)

Per the roadmap's own principle — *"makes the product wider, not the central
claim more true"*:

- **Sync-provider expansion** (Linear P9B-1, Monday P9B-2, Jira P9B-3,
  GitHub Projects P9B-4, webhook sync P9B-5) — except the Beads provider
  (S8), which is distribution for the moat itself, not breadth.
- **The 56-item `P11-*` audit batch** — hardening that matters, but none of
  it changes what anvil *is*; drain opportunistically.
- **WF-3 declarative workflow runner** — stays spec-first per roadmap;
  orchestration is the churn layer anvil deliberately sits beneath.
- **E11 backlog-platform build-out** — S12 dogfoods the need first; the
  research's own warning ("stay the governed substrate under the loop, not a
  PM platform") stands.

## Kill / pivot triggers (unchanged, restated for this track)

From `_positioning.md`: if a platform ships a portable, exportable,
vendor-neutral proof+state format that off-cloud runtimes can read and
write, collapse into (i) the schema spec and (ii) emit/ingest adapters — S4
is the pre-positioning for exactly that outcome. S7's bake-off carries E13's
kill trigger. For a solo author the win condition remains *personal
infrastructure that survives churn*, not market share.
