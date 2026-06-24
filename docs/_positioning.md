# anvil: Positioning (internal reference)

> Internal source of truth for README, architecture.md, design.md, and the
> `plugin.json` description. Not part of user-facing docs nav. Reusable
> sentences are marked **(Q)**.

## What it is

**(Q)** anvil is a local-first, backend-neutral project-state layer for
humans and AI coding agents: the durable record of every requirement, task,
claim, and piece of evidence in your project, stored in SQLite under
`.anvil/` and exposed through a CLI and an MCP server.

## Who it is for

Developers running Claude Code, Codex, Cursor, OpenHands, or Copilot who need
multiple agents, and multiple humans, to coordinate against the same plan
without overwriting each other. Solo builders can preserve PRDs across
sessions. Project leads can audit what was claimed, reviewed, and completed.

## The 5 differentiators (vs CCPM, issue-trackers, chat-driven workflows)

1. **Richer canonical state than issue text.** Pydantic v2 models in SQLite, validated at every transition, not free-form markdown in an issue body.
2. **Explicit claim / lock / lease model.** A `Claim` row with an expiry timestamp and heartbeat; stale leases are detected and released on every CLI or MCP call, not assignment-by-label.
3. **LLM-optimized work packets.** `anvil packet T012` renders the exact intent, acceptance criteria, scope, and non-goals an agent needs, not an entire issue thread the agent must summarize.
4. **Six-dimension task scoring.** Complexity, parallelizability, context load, blast radius, review risk, and agent suitability drive routing and expand recommendations, not single-axis story points.
5. **Runtime-neutral CLI + MCP.** The state engine is not coupled to any one agent runtime; the FastMCP stdio server exposes 24 tools to any MCP-compatible client.

## Terraform analogy

**(Q)** anvil is to agentic software work what Terraform is to
infrastructure: a canonical state file holds the truth, derived views (work
packets, markdown plans, dependency graphs) are projected from it, and the
plan-then-apply rhythm gates execution behind review. The PRD is the
configuration; the SQLite database is the state; `anvil apply` is the
commit point that records evidence and transitions a task to `done`. Drift
(stale claims, orphan branches, sync conflicts) is detected and reconciled,
not papered over.

**(Q)** Stretching the analogy one notch: each PRD is a **scoped stack /
workspace** within the **one canonical state**. A project holds several PRDs the
way a Terraform installation holds several workspaces — separate plans, separate
gates, separate `apply` rhythms — all backed by a single state store
(`.anvil/state.db` + `events.jsonl`), not one database per PRD. A PRD is
a release/milestone-scoped, separately-gated, revisable plan carrying a target
version/tag: it gates the claimability of the tasks *it* owns (a task is
claimable once its owning PRD is reviewed/approved, independent of sibling PRDs),
yet conflict detection deliberately spans every workspace so two tasks in
different PRDs that touch the same file are still single-winner-coordinated. The
single-PRD project is the one-workspace default — a lone `default` PRD owning
every row.

## MCP vs plugin

**(Q)** MCP exposes capabilities; plugins encode operating discipline. The
MCP server ships 24 tools (`claim_task`, `submit_completion_evidence`,
`generate_work_packet`, …) any agent can call, but a tool does not decide *when*
to claim, *which* specialist should execute, *what* evidence is required, or
*how* the critic gate runs. That behaviour lives in skills, subagents, and
hooks: the plugin layer. anvil is plugin-first and MCP-compatible,
not MCP-only.

## Elevator pitch

**(Q)** anvil turns PRDs into reviewed, lockable, evidence-backed work packets
that humans and AI coding agents can execute in parallel without overwriting
each other: the canonical project-state layer for agent teams.

## The fleet thesis: capacity coordination across pools + packet quality (direction)

> Added 2026-06-20, revised the same day after a four-stream adversarial review
> (landscape research, a narrow-waist stress-test, a code-grounded red-team, and
> the fakoli-state lineage). Re-led the same day on capacity coordination +
> packet quality after the economics turned out to be the real driver; the first
> drafts overstated verification, so it is now a supporting differentiator and a
> bet, not the headline. Mechanics in `design.md` § "Why risk-axis eligibility
> now, matching later"; build tracked as epic **E13**.
> anvil = https://github.com/fakoli/anvil.

**(Q)** The next layer of anvil's value is not a better orchestrator and not
durable state: it is *capacity coordination across multiple parallel pools.* The
real constraint is not per-token cost, it is capacity. A heavy user maxes a
flat-rate plan and constantly hits rate limits, so the win is draining several
flat-rate pools (for example one Claude subscription and one Codex subscription)
in parallel and spilling *overflow* to a zero-marginal-cost local GPU box. anvil
is the neutral backlog a heterogeneous fleet (different vendors, different
machines, local and cloud) pulls work from, so the work keeps moving when any one
pool throttles.

**(Q)** Packets are the worldview substitute. A local open model lacks the
frontier model's worldview, so it cannot explore an underspecified task well; but
a tight, fully-specified anvil packet (intent, acceptance criteria, scope,
non-goals, exact context) lets a *fast* local model (measured 200+ tok/s on an
RTX 5090) skip exploration and execute a bounded task directly. Packet quality is
what turns "fast but dumb" into "fast and sufficient," and because a well-scoped
packet cuts steps, it also cuts tokens, which buys back capacity. Packet quality
is a first-class, measured workstream, not a nicety.

**(Q)** Agents *pull and self-select*; anvil never *places* work. A runtime polls,
finds a task it is eligible for (its risk within the runner's ceilings), claims it
under a lease, executes, and submits evidence. anvil owns *eligibility*; the
harness owns *assignment*; anvil never names a model. That pull model is what lets
the agency layer and the substrate layer evolve independently: you can swap the
whole runtime fleet without touching the state contract, and a heterogeneous fleet
self-selects by capability and degrades gracefully when a pool is throttled.

**(Q)** Why a substrate and not orchestration: orchestration is a *churn layer*.
Inside one ~8-month window OpenAI, Anthropic, GitHub, and Cursor each absorbed
multi-agent orchestration into their first-party product; the author's own
fakoli-flow (https://github.com/fakoli/fakoli-plugins) was obviated by Claude Code
Dynamic Workflows, the rule, not bad luck. Build the layer the platforms keep
eating and your work evaporates; build the layer beneath it and you have a
riverbed, not sand. **The name is the thesis: anvil is the fixed surface the work
is done *on*; orchestration is the hammer, the agents the hand; both change, the
anvil stays.**

**(Q)** Be honest about the weak pillars. Durable state is no longer a moat: the
platforms ship it now (OpenAI Symphony, GitHub Agent HQ, Claude Code Tasks, MCP
Tasks), and on the pillar itself anvil is *behind*, not ahead. Temporal's
append-only Event History with replay, Microsoft's cross-host checkpointing,
LangGraph Platform's multi-host workers, and Beads' Dolt-versioned store all exceed
anvil's single-host SQLite + JSONL. Orchestration is likewise commoditized and
being absorbed (Anthropic Dynamic Workflows, CrewAI, LangGraph). So the strategy is
mixed: *concede* durable execution (interoperate, stay local-first SQLite, do not
try to out-engineer Temporal) while *hardening* the lease/claim semantics, the
single-winner coordination that is cheap to keep ahead on.

**(Q, supporting differentiator: a bet to execute)** And the work is verifiable.
The genuinely unoccupied position is a specific *fusion*: a portable, signed,
replayable proof artifact bound to task identity + claim/lease + pull, local-first.
Verifying the work product instead of trusting the exit code is what would make it
safe to let weak or unsupervised local runners pull. This is a *contested* wedge,
not a won one: portable proof formats (AGEF, Proof of Insight, Pipelock) and
enforced evidence-gates (agentic-os, EviBound, CrewAI guardrails) already exist
*separately*, and platforms are absorbing verification (Cursor Cloud Agents
auto-record proof onto PRs). anvil does *not* yet hold the fusion: its evidence
gate is advisory-by-default, it emits no typed/signed/portable artifact, and it is
single-host SQLite. So present this as a bet anvil must still execute, never as a
moat it occupies. (Replay-in-CI, when it lands, must check *logical* equivalence
via a canonical row-ordered dump or per-table content hash, not byte-identical
SQLite.)

**Load-bearing, not a mode:** multi-vendor + local-first + no-account is the
*headline*, not an option. The moment the value also works fine
single-vendor-inside-GitHub, the platform wins it. A *self-hosted* shared backend
(your own Postgres/MySQL on your network) is in-bounds; a hosted cloud backend
with accounts is the exact terrain that gets absorbed.

**Scope discipline (anti-overstretch):** ship the *minimum* (risk-axis eligibility
on `anvil next` + autonomous pull loops + the verifiable-proof bet) and let
*measured* mis-routing pull richer machinery (`type`, `tier`, profiles, shared
backend) into existence. Capacity-pools as a first-class concept stay deferred
until a two-loop bake-off measures how often pools throttle and whether naive
spillover already suffices. **Kill/pivot trigger:** if a platform ships a portable,
exportable, vendor-neutral proof+state format that off-cloud, non-blessed runtimes
can read and write, the standalone-tool bet is over: collapse into (i) the schema
spec and (ii) emit/ingest adapters. For a solo author the win condition is
*personal infrastructure that survives churn*, not market share.

## What it is NOT

- **Not a SaaS.** State lives in your repo under `.anvil/`; no hosted backend, no account, no telemetry.
- **Not a chat memory layer.** Chat history is not a database. State survives session resets, model swaps, and agent runtime changes.
- **Not an issue tracker.** GitHub Issues is an opt-in *sync target* via the bidirectional Phase 8 sync engine, not the source of truth.
- **Not a coding agent.** It is the coordination layer around coding agents: work packets in, evidence out.
- **Not (becoming) a SaaS, even in fleet mode.** The opt-in shared backend that lets a multi-host fleet pull from one job board is a database *you* run, not a hosted control plane with accounts or telemetry. The local-first, single-host default is unchanged.
- **Not a model router.** anvil scores and labels work and matches *eligibility*; it never selects or names a model. Which model runs a job is the runner's choice, made from its own capability profile.
