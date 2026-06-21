# Competitive analysis: who does what in the agent-state space, and why durability and orchestration are not the story

> Citation-grounded competitive analysis of the agent-state / coordination space vs Anvil,
> framed on "durability and orchestration are commoditized; neutrality + verification is the
> contested wedge." Compiled 2026-06-20 from a multi-agent pass (5 product clusters profiled
> with primary sources + synthesis). Sources are inline. Companion to
> [`2026-06-20-agent-fleet-pull-market-landscape.md`](2026-06-20-agent-fleet-pull-market-landscape.md),
> [`../_positioning.md`](../_positioning.md), and [`../design.md`](../design.md). Honest finding:
> the verification wedge is **contested, not won**, and Anvil does **not** yet occupy it.

## TL;DR

- **Durability is commoditized.** Append-only, replayable durable agent/task state ships today from durable-execution engines (Temporal's Event History; DBOS/Inngest step-checkpointing) and from the agent-state prior art itself (Beads now masters issues in an embedded Dolt DB). Platform control planes inherit it for free (GitHub's substrate + enterprise audit log; Linear's auto-managed Agent Sessions). A durable store is table stakes, not a wedge.
- **Orchestration is commoditized and being absorbed.** OpenAI Symphony, GitHub Agent HQ, Anthropic Claude Code Dynamic Workflows, and the framework cluster (LangGraph, CrewAI, Microsoft Agent Framework, OpenAI Agents SDK) all ship multi-agent placement/routing. The thin-OSS-SDK-as-funnel pattern (OpenAI Agents SDK) and the OSS-core-to-paid-platform pattern (LangGraph/CrewAI) show orchestration converging to a giveaway that pulls you toward a hosted tier.
- **The remaining wedge is neutrality + verification — and it is contested, not won.** The honest finding: the verification cluster shows portable proof artifacts *already exist* (AGEF, Proof of Insight, Pipelock Receipts, TessPay), evidence-gated completion is *already shipped* (agentic-os hard-blocks in CI; EviBound drives hallucinated completion 100%→0%), and platforms are *absorbing verification natively* (Cursor Cloud Agents auto-record demo video + screenshots + logs onto PRs).
- **The genuinely unoccupied space is narrow:** the *fusion* of a durable, governed, neutral task-STATE substrate (P1) + claim/lease/heartbeat coordination (P2) + capability-matched pull (P4) **with** a portable, replayable, evidence-gated completion contract (P3) — all local-first, no-account, cross-vendor (P5). No competitor binds a portable proof artifact to governed task identity + lease coordination + pull. That intersection is open.
- **But Anvil does not yet hold it.** Anvil's P3 is advisory-by-default and emits no typed/signed/replayable artifact (AGEF/PoI/Receipts already do). Anvil's enforcement is weaker than agentic-os/EviBound. Anvil's durability is single-host SQLite+JSONL while Temporal/Microsoft/LangGraph Platform ship multi-host. So the wedge is a *bet Anvil must still execute*, not a position it occupies.
- **Closest approachers to the wedge:** Beads (durable record + pull + neutrality, but no gate, one-shot claim), Temporal (P1/P2/P4 production-grade, but no work-product gate, server+DB not local-first), mcp_agent_mail (neutral git-audit + advanced leases, but no work record, no gate), and agentic-os (P3+P5 fusion shipped and hard-enforced, but no lease/pull/typed artifact). Each owns part of the wedge; none owns the whole.

## The comparison matrix

Legend: ✓ = ships / strong · ◐ = partial / qualified · ✗ = absent. P1 durable state · P2 claim/lease/heartbeat · P3 verification / evidence-gate · P4 capability-matched pull · P5 neutrality (cross-vendor) + local-first / no-account.

| Product | P1 durable state | P2 claim / lease | P3 verification / evidence-gate | P4 capability pull | P5 neutral + local / no-account |
|---|---|---|---|---|---|
| **Platform control planes** | | | | | |
| OpenAI Symphony | ◐ tracker+FS, no orch. DB | ✓ claim+timeout, single-host | ✗ explicitly out of scope | ◐ pushes/places, Codex-only | ◐ self-host, but Linear+Codex-bound |
| GitHub Agent HQ | ✓ GitHub substrate + audit | ◐ branch controls, no leases | ◐ CI/review gates, not portable | ◐ human push to fleet | ◐ 6 vendors, but cloud+sub-bound |
| Linear agent API | ✓ auto-managed sessions | ◐ delegation + stale, no lease | ✗ response = done, no proof | ◐ delegate push, state handoffs | ◐ open API, but cloud-only SaaS |
| Claude Code Dynamic Workflows | ◐ resume checkpoint only | ✗ no claim/lease | ◐ adversarial refute, ephemeral | ✗ Claude places subagents | ✗ Claude-only, account-gated |
| **Durable-execution engines** | | | | | |
| Temporal | ✓ append-only Event History | ✓ pull+heartbeat+orphan recovery | ✗ "done"=returned, no work proof | ✓ pull, Build-ID routing | ◐ MIT OSS, but server+ext DB |
| Inngest | ✓ step memoization | ◐ concurrency/singleton keys | ✗ persistence ≠ validation | ✗ push HTTP invocation | ◐ SSPL core, mandatory keys |
| DBOS | ✓ Postgres checkpoints | ◐ single-winner queues | ✗ checkpointed ≠ verified | ◐ queue pull, no capability decl | ◐ MIT embedded, needs Postgres |
| Windmill | ◐ Postgres job state, coarse | ◐ DB-queue pull, static tags | ✗ returned ≠ verified | ◐ tag/worker-group routing | ◐ AGPL+proprietary EE gating |
| **Orchestration frameworks** | | | | | |
| LangGraph / Platform | ◐ snapshot ckpt (✓ hosted tier) | ◐ BLPOP infra pickup only | ✗ code-it-yourself, no gate | ✗ push graph routing | ◐ OSS local; durable tier hosted+telemetry |
| CrewAI | ◐ SQLite snapshot/fork | ✗ no claim/lease | ◐ task guardrails pre-completion | ✗ push decorator routing | ◐ OSS local; prod → Enterprise |
| Microsoft Agent Framework | ◐ checkpoint/resume, cross-host | ✗ actor msg routing, no lease | ✗ middleware-only, no proof | ✗ push, manager-assigns | ◐ model-neutral but Azure gravity + telemetry |
| OpenAI Agents SDK | ◐ session conversation memory | ✗ none, single-process | ◐ I/O guardrails, fail-fast | ✗ push handoffs | ◐ OSS local; OpenAI-centric tracing |
| **Work-record & coordination protocols** | | | | | |
| Beads (bd) | ✓ Dolt-mastered graph record | ◐ one-shot atomic claim | ✗ close is manual one-liner | ✓ pure pull (`bd ready`) | ✓ non-git VCS, offline, no account |
| MCP Tasks (SEP-1686) | ✗ ephemeral keepAlive window | ✗ IDs for idempotency, not lock | ✗ completed=request finished | ✗ not work distribution | ✓ open standard, host-dependent local |
| GitHub spec-kit | ◐ markdown files in git | ✗ single-flow, no coordination | ◐ /analyze gates plan coherence | ✗ single configured agent | ✓ 30+ agents, no-lock-in, local |
| mcp_agent_mail | ✓ git append-only (messages) | ✓ TTL leases + stale reclaim + hook | ✗ records intent, not proof | ✗ comms, not dispatch | ✓ self-host, offline, no account |
| **Verification / proof-of-work** | | | | | |
| agentic-os | ◐ git-as-event-log (markdown) | ◐ single-writer lock, no lease | ✓ CI hard-block, unbypassable | ✗ fixed phase workflow | ✓ cross-agent, local, no account |
| AGEF (evidence format) | ◐ frozen replayable bundle | ✗ none | ✓ portable signed verifiable artifact | ✗ none | ✓ multi-runtime spec, offline verify |
| Proof of Insight (PoI) | ◐ content-addressed signed DAG | ✗ none | ✓ typed, replay-classified, signed | ✗ none | ✓ cross-vendor profiles, no account |
| Pipelock Receipts | ◐ hash-chained action trail | ✗ records principals, no lock | ◐ signed receipts, audit-not-gate | ✗ none | ✓ cross-vendor proxy, offline verify |
| EviBound | ◐ MLflow run store | ✗ sequential gates, no lease | ✓ hard dual-gate, measured 0% hallucinated completion | ✗ none | ✗ MLflow-locked, not neutral |
| Qodo | ◐ org-context engine | ✗ none | ◐ deep review verdict, not artifact | ✗ none | ◐ reviews any agent, but proprietary SaaS |
| Autonoma AI | ✗ test suites, not task state | ✗ none | ◐ E2E blocks merge, platform-bound | ✗ none | ✗ cloud/account-based |
| Cursor Cloud Agents | ◐ artifacts on PR, not neutral | ✗ internal only | ◐ auto video+screenshot+log proof | ✗ Cursor places work | ✗ Cursor-only, cloud, account |
| CodeRabbit CLI | ✗ ephemeral review | ✗ none | ◐ per-loop CI gate, review text only | ✗ none | ◐ cross-vendor local CLI; account/limits |
| Agent-eval / CI-regression | ◐ trace/eval stores | ✗ none | ◐ regression gate, not per-task proof | ✗ none | ◐ OSS members local; SaaS members account |
| TessPay | ◐ attestations/VDCs | ✗ none | ✓ TEE-attested, gates payment | ✗ none | ◐ transferable proof, but TEE hardware |
| SmartSnap / DriftGuard | ◐ replayable traces | ✗ none | ✓ trained self-evidence / faithfulness | ✗ none | ◐ drop-in OSS, research-grade |
| **Anvil** | ◐ SQLite + append-only JSONL (single-host today; multi-host = **roadmap**) | ✓ DB row + lease + heartbeat + stale detection (single-host advisory) | ◐ evidence gate, **advisory-by-default**; typed/signed/replay artifact = **roadmap** | ◐ capability-matched pull (never names a model) — design intent, **early reach** | ◐ runtime-neutral CLI+MCP, local-first, no-account, no-telemetry (neutral but **unproven reach**) |

Anvil's aspirational pillars are marked inline: multi-host P1, typed/signed/replayable P3, and hard (non-advisory) enforcement are roadmap, not shipped. P5's local-first/no-account half is shipped; the cross-vendor *reach* is early.

## Durability is not the story

Durable, replayable agent/task state ships, in mature form, from multiple independent directions. None of this is novel anymore.

**Durable-execution engines treat it as the core primitive.** Temporal persists an append-only Event History per workflow and replays from a checkpoint to reconstruct state — event sourcing, server-backed (Cassandra/MySQL/PostgreSQL) [temporal.io/blog/what-is-durable-execution; docs.temporal.io/workflow-execution/event; docs.temporal.io/temporal-service/persistence]. Inngest persists each successful step's output and skips completed steps on resume [inngest.com/docs/learn/how-functions-are-executed; inngest.com/blog/principles-of-durable-execution]. DBOS checkpoints each step's outcome to your own Postgres and resumes from the last completed step [docs.dbos.dev/architecture; dbos.dev/dbos-transact]. Windmill persists job and flow state to Postgres, surviving restarts [github.com/windmill-labs/windmill; windmill.dev].

**The agent-state prior art ships it too — and has moved past the design Anvil uses.** Beads now masters issues in an embedded Dolt database (`.beads/embeddeddolt/`); its JSONL is "an export for viewers and interchange, not the source of truth or a full database backup," with history/merge from Dolt's Prolly trees [github.com/gastownhall/beads/blob/main/docs/DOLT.md; dolthub.com/blog/2026-04-15-common-beads-workflows/; steve-yegge.medium.com/introducing-beads-a-coding-agent-memory-system-637d7d92514a]. mcp_agent_mail keeps a git-backed append-only audit where "No deletions or mutations—only new state appended," fully recoverable and diffable via git log [github.com/Dicklesworthstone/mcp_agent_mail].

**Platforms inherit durability for free.** Linear tracks Agent Session lifecycle automatically across six states ("You don't need to manage agent session state manually") [linear.app/developers/agents; linear.app/developers/agent-interaction]. GitHub Agent HQ rests on GitHub's own substrate plus an enterprise control plane with audit logging [github.blog/news-insights/company-news/welcome-home-agents/; visualstudiomagazine.com/articles/2025/10/28/github-introduces-agent-hq-to-orchestrate-any-agent-any-way-you-work.aspx]. Microsoft Agent Framework ships session state + workflow checkpointing across all orchestration patterns, with cross-host execution [learn.microsoft.com/en-us/agent-framework/overview/; devblogs.microsoft.com/agent-framework/microsoft-agent-framework-version-1-0/].

*Inference:* When append-only-replayable durable state ships from durable-execution engines, the canonical agent-state tracker, and every major platform control plane — and several exceed Anvil's single-host SQLite+JSONL with multi-host, server-backed, or Dolt-versioned stores — a durable store cannot be the differentiator. Anvil's on-disk design (SQLite + append-only `events.jsonl`) is now closer to *historical* Beads than to current Beads.

## Orchestration is not the story

Orchestration is converging on platforms and being given away, then used as a funnel. The churn/absorption pattern is visible across three clusters.

**Platforms ship live orchestration and explicitly punt the hard parts.** OpenAI Symphony turns a Linear board into a control plane, auto-restarting agents and shepherding PRs — yet its SPEC.md deliberately runs *without* a durable orchestrator database and declares proof-of-work out of scope ("Symphony is a scheduler/runner and tracker reader") [openai.com/index/open-source-codex-orchestration-symphony/; raw.githubusercontent.com/openai/symphony/main/SPEC.md; infoworld.com/article/4164173/openais-symphony-spec-pushes-coding-agents-from-prompts-to-orchestration.html]. GitHub Agent HQ orchestrates agents from six vendors in parallel from one command center [github.blog/news-insights/company-news/welcome-home-agents/; developers.slashdot.org/story/25/11/02/2337254/github-announces-agent-hq-letting-copilot-subscribers-run-and-manage-coding-agents-from-multiple-vendors]. Anthropic's Dynamic Workflows fan out across hundreds-to-thousands of subagents that cross-check each other [claude.com/blog/introducing-dynamic-workflows-in-claude-code; infoq.com/news/2026/06/dynamic-workflows-claude-code/].

**Frameworks commoditize it down to a thin SDK or an OSS-to-paid funnel.** OpenAI Agents SDK is a deliberately thin single-process loop with handoffs, leaving durability and distribution to the user — a giveaway that pulls toward the hosted Responses API and OpenAI tracing [openai.github.io/openai-agents-python/; openai.github.io/openai-agents-python/handoffs/]. LangGraph's OSS tier runs locally with SqliteSaver, but durable execution at scale (queue, workers, failover) is the hosted LangGraph Platform with LangSmith telemetry — "Neutrality erodes exactly where durability/orchestration become real" [docs.langchain.com/oss/python/langgraph/durable-execution; docs.langchain.com/langsmith/agent-server]. CrewAI is moving "beyond orchestration" to an enterprise platform for the production story [blog.crewai.com/how-crewai-is-evolving-beyond-orchestration-to-create-the-most-powerful-agentic-ai-platform/; docs.crewai.com/en/concepts/flows]. Microsoft converged AutoGen + Semantic Kernel into a GA SDK with five orchestration patterns and a distributed actor runtime — a hyperscaler shipping cross-host orchestration for free [learn.microsoft.com/en-us/agent-framework/overview/; microsoft.github.io/autogen/stable//user-guide/core-user-guide/framework/distributed-agent-runtime.html].

**The protocol layer is standardizing the plumbing beneath all of it.** MCP's Tasks primitive (SEP-1686) is Final/Standards-Track: it absorbs the call-now/fetch-later async-execution plumbing that tools used to hand-roll, and its roadmap (nested tasks, push notifications) keeps climbing [modelcontextprotocol.io/seps/1686-tasks; blog.modelcontextprotocol.io/posts/2026-mcp-roadmap/]. spec-kit absorbs the spec→plan→tasks front-end across 30+ agents with "no lock-in" [github.com/github/spec-kit; github.github.com/spec-kit/].

*Inference:* Multi-host execution is now table stakes a hyperscaler ships for free (Microsoft, LangGraph Platform, GitHub). Anvil — single-host today, with multi-host on the roadmap — cannot win on orchestration or distribution. Symphony is the single sharpest data point: a major lab shipping orchestration while explicitly punting *both* durable state *and* verification.

## The real wedge: neutrality + verification

The thesis predicts the open space is neutral + verified. The evidence is more uncomfortable than the thesis is flattering, so be precise about what is and is not unclaimed.

**Who is closest.**

- **Beads** owns durable record (P1) + pure pull (P4) + the broadest neutrality (non-git VCS, offline, stealth, ~18.7k stars, matching the figure in the companion landscape doc). It is Anvil's credibility anchor *and* its sharpest commodity-layer competitor. But it deliberately stops short of governance: close is a manual one-liner (`bd close <id> "Fixed"`), and `--claim` is a one-shot atomic field-set, not a lease+heartbeat+stale-reap. Yegge frames it as "orchestration for what you're working on today," explicitly not a planner or verifier [github.com/steveyegge/beads; steve-yegge.medium.com/introducing-beads-a-coding-agent-memory-system-637d7d92514a; github.com/steveyegge/beads/blob/main/CHANGELOG.md]. Beads leaves P3 genuinely open.
- **Temporal** owns P1 + P2 + P4 at production grade and never names a model. But durable execution defines "done" as an activity returning without throwing — it does not verify *correctness* of arbitrary work; human approval via Signals is something you build, not a typed evidence contract [temporal.io/blog/what-is-durable-execution; docs.temporal.io/ai-cookbook/human-in-the-loop-python; docs.temporal.io/production-deployment/worker-deployments/worker-versioning]. And it needs a server + external DB, so it is not local-first single-file. The P3 gap plus single-file local-first is the only durable wedge left versus Temporal.
- **mcp_agent_mail** owns neutral git-backed append-only audit (P1 spirit) and lease mechanics that *exceed* Anvil's (path-pattern advisory leases, TTL, stale reclaim, single-winner exclusivity, an optional commit-blocking pre-commit hook). But by design it records messages + reservations, not a work record, and "does not cryptographically prove task completion" [github.com/Dicklesworthstone/mcp_agent_mail; mcpagentmail.com/]. No P3, no P4.
- **agentic-os** is the most direct competitor to Anvil's *claimed* wedge: it ships P3+P5 fusion *and enforces it harder* than Anvil does — a mandatory CI pipeline that hard-blocks ("The agent can still cut a corner; it just can't get that corner past the checks it doesn't control"), cross-vendor governance files, local-first, no account [github.com/KbWen/agentic-os]. What it lacks: a lease/heartbeat (P2), capability-matched pull (P4), and a typed/signed/replayable proof *artifact* — its evidence is human-readable Markdown.

**What is genuinely unclaimed.** Two things, neither of which is "a portable proof artifact" or "an evidence gate" on its own — both already exist:

1. *Portable, replayable proof artifacts already ship* in at least four independent forms: AGEF (signed CBOR session bundle, deterministic verifier, Rust/Go/Python/JS impls) [github.com/radotsvetkov/agef; dev.to/radotsvetkov/agef-explained-a-portable-evidence-format-for-ai-agent-sessions-40fn]; Proof of Insight (content-addressed signed DAG with R1/R2/R3 replay classes, Sigstore/Rekor signing) [proofofinsight.org/]; Pipelock Agent Action Receipts (Ed25519 hash-chained JSON, 4-language open verifier, conformance corpus) [pipelab.org/learn/agent-action-receipts/]; and TessPay's TEE attestations gating payment [arxiv.org/pdf/2602.00213].
2. *Hard evidence-gated completion already ships*: EviBound's dual machine-checkable gates drove hallucinated completion from 100% to 0% at ~8.3% overhead [arxiv.org/abs/2511.05524], and platforms absorb verification natively — Cursor Cloud Agents self-test in a VM and attach demo video + screenshots + logs to the PR before shipping [cursor.com/blog/security-agents; nxcode.io/resources/news/cursor-cloud-agents-virtual-machines-autonomous-coding-guide-2026]. Funded products gate per-domain (Qodo review [techcrunch.com/2026/03/30/qodo-bets-on-code-verification-as-ai-coding-scales-raises-70m/], Autonoma E2E merge-block [getautonoma.com/blog/ai-coding-agent], CI-eval regression gates [confident-ai.com/knowledge-base/compare/best-ci-cd-tools-testing-ai-agents-before-production-2026]).

The specific combination still unclaimed is the **binding**: a *single neutral, local-first, no-account substrate that fuses governed durable task STATE + claim/lease/heartbeat coordination + capability-matched pull with a portable, replayable, evidence-gated completion contract.* None of the pure verifiers carry task identity, leases, or a pull queue; none of the state engines carry a work-product proof gate; the platforms that fuse some of these are vendor-locked. mcp_agent_mail's own positioning maps the gap precisely: Beads = work graph, agent_mail = coordination + leases, spec-kit = spec authoring, MCP Tasks = async transport — *and none close the verification loop.*

*Inference (honest):* Anvil cannot claim novelty on "proof gates done," on "portable proof artifact," or on "neutral cross-vendor." All three exist. Its only defensible claim is the *integration* — and even that is a bet, because the integration's verification half is advisory-by-default and emits no artifact today. The wedge is real and unoccupied at the *intersection*, but Anvil has not yet built the parts that would occupy it.

## Where competitors match or exceed Anvil

This is the unflattering ledger. On individual pillars, Anvil is behind in more places than it is ahead.

- **Durable state (P1) — exceeded by many.** Temporal's append-only Event History + replay, Microsoft's cross-host checkpointing, and LangGraph Platform's Postgres+Redis multi-host workers are all more mature than Anvil's single-host SQLite+JSONL [docs.temporal.io/workflow-execution/event; devblogs.microsoft.com/agent-framework/microsoft-agent-framework-version-1-0/; docs.langchain.com/langsmith/agent-server]. Beads/Dolt now ships richer durable-state-with-history than Anvil [github.com/gastownhall/beads/blob/main/docs/DOLT.md]. GitHub's substrate + enterprise audit log is more battle-tested [github.blog/news-insights/company-news/welcome-home-agents/].
- **Claim/lease/heartbeat (P2) — matched or exceeded.** Temporal's heartbeat + orphan recovery + distributed-lock recipe exceeds Anvil's single-host advisory leases [docs.temporal.io/workers; temporal.io/blog/coordinate-access-to-shared-resources-with-a-distributed-lock-built-on-temporal-workflows]. Symphony ships a cleaner single-host claim+timeout than Anvil's advisory SQLite leases [raw.githubusercontent.com/openai/symphony/main/SPEC.md]. mcp_agent_mail's path-pattern leases with TTL, stale reclaim, and a commit-blocking hook are arguably ahead of Anvil's task-granular claims for file-conflict prevention [github.com/Dicklesworthstone/mcp_agent_mail].
- **Verification / evidence-gate (P3) — Anvil is behind on both enforcement and artifact.** agentic-os hard-blocks in CI *unbypassably* while Anvil's gate is advisory-by-default [github.com/KbWen/agentic-os]. EviBound demonstrably enforces and *measured* 0% hallucinated completion [arxiv.org/abs/2511.05524]. CrewAI's task guardrails are a working pre-completion validation gate with retry, shipped today [docs.crewai.com/en/enterprise/features/hallucination-guardrail; analyticsvidhya.com/blog/2025/11/introduction-to-task-guardrails-in-crewai/]. And the *artifact* Anvil names as its wedge already exists, signed and verifiable, in AGEF, PoI, Pipelock Receipts, and TessPay — none of which Anvil yet emits [github.com/radotsvetkov/agef; proofofinsight.org/; pipelab.org/learn/agent-action-receipts/; arxiv.org/pdf/2602.00213]. The hard *science* (faithfulness adjudication, trained self-evidencing) is ahead of Anvil too: DriftGuard found artifact-faithfulness failures in 46–68% of reward-0 trajectories as a ~900-LoC drop-in guard [openreview.net/forum?id=40wuXQMQRU; arxiv.org/abs/2512.22322].
- **Capability pull (P4) — matched by Beads and Temporal.** Beads is pure pull (`bd ready`) and never names a model — the same stance as Anvil, already adopted across Claude Code, Sourcegraph Amp, and others [github.com/steveyegge/beads]. Temporal workers self-select by polling queues, with Build-ID/version routing [docs.temporal.io/production-deployment/worker-deployments/worker-versioning].
- **Neutrality + local/no-account (P5) — matched or exceeded on reach.** Beads supports non-git VCS (Sapling, Jujutsu, Piper) — broader VCS neutrality than Anvil [github.com/gastownhall/beads]. spec-kit ships across 30+ agents with explicit no-lock-in [github.com/github/spec-kit]. Linear's open, no-pre-approval, no-cost agent API has a broad live ecosystem (Codex, Cursor, Copilot, Devin, Sentry, Factory, Warp) [linear.app/developers/agents; linear.app/agents]. DBOS matches Anvil's embedded, no-server, no-account ethos and arguably exceeds it on "no separate process" simplicity [docs.dbos.dev/why-dbos; dbos.dev/blog/durable-execution-by-default]. agentic-os and mcp_agent_mail match Anvil's local-first + no-account exactly [github.com/KbWen/agentic-os; github.com/Dicklesworthstone/mcp_agent_mail].

*Net:* Anvil leads on no single pillar in isolation. Its only edge is the fusion — and the fusion's verification half (typed proof + replay-in-CI + hard enforcement) is unbuilt. Strategically, Anvil should *adopt* an existing artifact format (AGEF/PoI/Receipts), *ride* MCP Tasks as transport for long-running evidence/replay jobs, integrate an existing faithfulness verifier (DriftGuard), and make its gate enforcing rather than advisory — then differentiate purely on binding that proof to governed, neutral, leased, pull-based task state.

## Sources

- https://openai.com/index/open-source-codex-orchestration-symphony/
- https://github.com/openai/symphony
- https://raw.githubusercontent.com/openai/symphony/main/SPEC.md
- https://www.infoworld.com/article/4164173/openais-symphony-spec-pushes-coding-agents-from-prompts-to-orchestration.html
- https://aiagentsfirst.com/openai-symphony-linear-codex-agent-control-plane
- https://www.helpnetsecurity.com/2026/04/28/openai-symphony-codex-orchestration-linear/
- https://github.blog/news-insights/company-news/welcome-home-agents/
- https://developers.slashdot.org/story/25/11/02/2337254/github-announces-agent-hq-letting-copilot-subscribers-run-and-manage-coding-agents-from-multiple-vendors
- https://visualstudiomagazine.com/articles/2025/10/28/github-introduces-agent-hq-to-orchestrate-any-agent-any-way-you-work.aspx
- https://venturebeat.com/ai/githubs-agent-hq-aims-to-solve-enterprises-biggest-ai-coding-problem-too
- https://bittalks.org/blog/github-agent-hq-2026/
- https://linear.app/developers/agents
- https://linear.app/developers/agent-interaction
- https://linear.app/agents
- https://linear.app/docs/agents-in-linear
- https://www.theregister.com/2026/03/26/linear_agent/
- https://claude.com/blog/introducing-dynamic-workflows-in-claude-code
- https://www.infoq.com/news/2026/06/dynamic-workflows-claude-code/
- https://pasqualepillitteri.it/en/news/3663/claude-code-dynamic-workflows-anthropic-research-preview
- https://www.testingcatalog.com/anthropic-launches-dynamic-workflows-for-claude-code/
- https://temporal.io/blog/what-is-durable-execution
- https://docs.temporal.io/workflow-execution/event
- https://docs.temporal.io/workers
- https://temporal.io/blog/replay-2026-product-announcements
- https://docs.temporal.io/production-deployment/worker-deployments/worker-versioning
- https://docs.temporal.io/temporal-service/persistence
- https://github.com/temporalio/temporal
- https://docs.temporal.io/ai-cookbook/human-in-the-loop-python
- https://temporal.io/blog/coordinate-access-to-shared-resources-with-a-distributed-lock-built-on-temporal-workflows
- https://www.inngest.com/docs/learn/how-functions-are-executed
- https://www.inngest.com/blog/principles-of-durable-execution
- https://github.com/inngest/inngest
- https://www.inngest.com/docs/self-hosting
- https://www.inngest.com/docs/learn/inngest-functions
- https://www.inngest.com/changelog
- https://docs.dbos.dev/architecture
- https://www.dbos.dev/dbos-transact
- https://github.com/dbos-inc/dbos-transact-py
- https://docs.dbos.dev/why-dbos
- https://www.dbos.dev/blog/durable-execution-by-default
- https://pydantic.dev/articles/pydantic-ai-dbos
- https://github.com/windmill-labs/windmill
- https://www.windmill.dev/
- https://automationatlas.io/answers/windmill-pricing-explained-2026/
- https://hossted.com/knowledge-base/osspedia/devops/developer-tools/streamlining-workflow-automation-with-windmill-open-source-self-hosted-and-developer-friendly-orchestration/
- https://docs.langchain.com/oss/python/langgraph/durable-execution
- https://reference.langchain.com/python/langgraph/checkpoints
- https://docs.langchain.com/langsmith/agent-server
- https://neuralware.github.io/posts/langgraph-redis/
- https://www.diagrid.io/blog/checkpoints-are-not-durable-execution-why-langgraph-crewai-google-adk-and-others-fall-short-for-production-agent-workflows
- https://github.com/langchain-ai/langgraph
- https://docs.crewai.com/en/concepts/flows
- https://docs.crewai.com/en/enterprise/features/hallucination-guardrail
- https://www.analyticsvidhya.com/blog/2025/11/introduction-to-task-guardrails-in-crewai/
- https://blog.crewai.com/how-crewai-is-evolving-beyond-orchestration-to-create-the-most-powerful-agentic-ai-platform/
- https://learn.microsoft.com/en-us/agent-framework/overview/
- https://microsoft.github.io/autogen/stable//user-guide/core-user-guide/framework/distributed-agent-runtime.html
- https://devblogs.microsoft.com/agent-framework/microsoft-agent-framework-version-1-0/
- https://visualstudiomagazine.com/articles/2026/04/06/microsoft-ships-production-ready-agent-framework-1-0-for-net-and-python.aspx
- https://github.com/microsoft/agent-framework
- https://openai.github.io/openai-agents-python/
- https://openai.github.io/openai-agents-python/handoffs/
- https://openai.github.io/openai-agents-python/sessions/
- https://openai.github.io/openai-agents-python/sessions/advanced_sqlite_session/
- https://cookbook.openai.com/examples/agents_sdk/session_memory
- https://steve-yegge.medium.com/introducing-beads-a-coding-agent-memory-system-637d7d92514a
- https://github.com/gastownhall/beads
- https://github.com/steveyegge/beads
- https://www.dolthub.com/blog/2026-04-15-common-beads-workflows/
- https://github.com/gastownhall/beads/blob/main/docs/DOLT.md
- https://github.com/steveyegge/beads/blob/main/CHANGELOG.md
- https://modelcontextprotocol.io/seps/1686-tasks
- https://github.com/modelcontextprotocol/modelcontextprotocol/issues/1686
- https://github.com/modelcontextprotocol/modelcontextprotocol/pull/1686
- https://blog.modelcontextprotocol.io/posts/2026-mcp-roadmap/
- https://github.com/github/spec-kit
- https://github.github.com/spec-kit/
- https://github.com/github/spec-kit/blob/main/templates/commands/analyze.md
- https://github.com/github/spec-kit/blob/main/AGENTS.md
- https://github.com/github/spec-kit/issues/1323
- https://github.blog/ai-and-ml/generative-ai/spec-driven-development-with-ai-get-started-with-a-new-open-source-toolkit/
- https://github.com/Dicklesworthstone/mcp_agent_mail
- https://www.jeffreyemanuel.com/projects/mcp-agent-mail
- https://mcpagentmail.com/
- https://github.com/Dicklesworthstone/mcp_agent_mail_rust
- https://pypi.org/project/mcp-agent-mail
- https://www.pulsemcp.com/servers/dicklesworthstone-agent-mail
- https://github.com/KbWen/agentic-os
- https://dev.to/radotsvetkov/agef-explained-a-portable-evidence-format-for-ai-agent-sessions-40fn
- https://github.com/radotsvetkov/agef
- https://proofofinsight.org/
- https://pipelab.org/learn/agent-action-receipts/
- https://arxiv.org/abs/2511.05524
- https://arxiv.org/pdf/2511.05524
- https://techcrunch.com/2026/03/30/qodo-bets-on-code-verification-as-ai-coding-scales-raises-70m/
- https://www.techbuzz.ai/articles/qodo-raises-70m-as-ai-code-verification-becomes-critical
- https://en.wikipedia.org/wiki/Qodo
- https://getautonoma.com/blog/ai-coding-agent
- https://getautonoma.com/blog/what-an-ai-qa-agent-actually-does
- https://getautonoma.com/blog/autonomous-testing-platform
- https://www.nxcode.io/resources/news/cursor-cloud-agents-virtual-machines-autonomous-coding-guide-2026
- https://cursor.com/blog/security-agents
- https://www.coderabbit.ai/cli
- https://docs.coderabbit.ai/cli/claude-code-integration
- https://www.confident-ai.com/knowledge-base/compare/best-ci-cd-tools-testing-ai-agents-before-production-2026
- https://genai.qa/ai-agent-trajectory-testing-2026/
- https://www.braintrust.dev/articles/agent-observability-complete-guide-2026
- https://galileo.ai/blog/best-ai-agent-evaluation-platforms
- https://arxiv.org/pdf/2602.00213
- https://arxiv.org/abs/2512.22322
- https://github.com/TencentYoutuResearch/SmartSnap
- https://openreview.net/forum?id=40wuXQMQRU