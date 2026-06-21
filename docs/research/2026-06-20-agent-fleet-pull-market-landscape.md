# Landscape: the pull-based agent labor market (grounding the Anvil fleet thesis)

> A reference map of where Anvil's "agent fleet / pull-based work queue" thesis sits
> in the 2026 landscape: durable agent state, orchestration frameworks, the
> job-queue/scheduler lineage, local model serving, and the economics.
>
> **Status:** living reference. Compiled 2026-06-20 from a multi-agent research pass
> (5 parallel landscape angles + a 2-reviewer adversarial novelty check + synthesis).
> Sources are cited inline; the adversarial check is folded into "Where Anvil is
> genuinely differentiated." Companion to [`_positioning.md`](../_positioning.md)
> (the fleet thesis) and [`design.md`](../design.md) (capability-matched pull).
> Build tracked as backlog epic **E13**.

## What we learned

- **The thesis is validated but crowded at the edges, with a genuine unoccupied center.** Every individual pillar Anvil claims — durable governed state, claim/lease/heartbeat, evidence-gated completion, capability-matched pull, shared multi-host backend — already ships in mature tools, and most pairs and triples co-exist in one product. The defensible claim is the *conjunction of all five as a packaged pull-based labor market for heterogeneous coding-agent runtimes*, not any single primitive.
- **The honest novelty is integration + framing, not invention.** The adversarial check refutes the marketing-style "no tool combines 2+ of these" framing decisively: Temporal alone hits 4 of 5 pillars in production. The strong-form claim that survives is narrower — "no *shipping product* combines all five," and even that is *approached* (not matched) by an OpenClaw-family research architecture and explicitly *proposed* (not shipped) by the AEX research design.
- **Beads (~18.7k stars, Steve Yegge) already won the argument that a durable canonical work record — separate from chat AND from GitHub Issues — should exist.** Anvil should treat Beads as its credibility anchor and primary comparable, then differentiate *within* the layer on governance + evidence-gating, which base Beads deliberately omits.
- **The orchestration field is overwhelmingly PUSH + ephemeral + in-process** (CrewAI, OpenAI Agents SDK, Magentic/Microsoft Agent Framework); LangGraph is the durability outlier but is still push and is checkpoint-snapshot, not guaranteed durable execution. Anvil's "harnesses, not sub-agents" pull framing is a clean wedge against this field — but *not* against the durable-execution tier (Temporal/DBOS/Inngest), where durable + cross-host + pull is table stakes.
- **Anvil's narrowest genuinely-unclaimed differentiator is the fusion of evidence-gated completion (P3) with capability-matched pull by heterogeneous agent runtimes (P4), welded onto a governed project backlog.** No durable-execution engine has an evidence gate (they run *your* code, not arbitrary agent CLIs); no agent-team orchestrator has leases/heartbeats or a shared multi-host backend; no marketplace has durable governed *project* state.
- **Local open coders crossed from "fast but dumb" to "genuinely useful for real agentic work" in 2026 — but a measurable, benchmark-dependent gap to frontier remains** (open ~70.6% SWE-bench Verified vs Sonnet ~79.6%; the gap *widens* on contamination-resistant SWE-bench Pro: open ~38–44% vs frontier ~59% on the same harness). A single 96GB RTX PRO 6000 workstation now runs a legitimately useful coder; it is not Opus/GPT-5-class on the hardest standardized tasks.

---

## Angle 1 — Prior art for durable, governed, multi-agent project state

The space is real and contested but fragmented across distinct layers; no single widely-adopted product unifies all four of {durable canonical record + multi-agent claim/lease + governance + evidence-gated completion}.

### Evidence (sourced)

- **Spec layer (stateless intent→artifacts).** GitHub spec-kit generates four markdown artifacts (constitution.md, spec.md, plan.md, tasks.md) via Spec→Plan→Tasks→Implement, version-controlled in `.specify/`/`specs/`. It is explicitly "scaffolding and template generator… does not track: Task claims or agent assignments; Multi-agent coordination; Runtime completion evidence; Persistent execution state… stateless at runtime." Source: https://github.com/github/spec-kit
- spec-kit's `tasks.md` checkbox model is documented as unreliable for stateless agents to self-mark done (v1.x checked off only ~50% of the time); multi-agent flows require a bolted-on external `agent-coordination.json`. Source: https://github.com/github/spec-kit/discussions/1077 *(confidence: medium)*
- **Canonical task/state layer (closest direct prior art).** Beads (Steve Yegge, ~18.7k stars) is a git-backed (JSONL + SQLite cache) dependency-DAG issue tracker positioned as "coding agent memory" — explicitly separate from chat AND deliberately distinct from GitHub Issues (deeper hierarchy, ready-work queries, four dependency-link kinds incl. provenance, survives context compaction). Source: https://steve-yegge.medium.com/the-beads-revolution-how-i-built-the-todo-system-that-ai-agents-actually-want-to-use-228a5f9be2a9
- Base Beads is a task/memory store and does **not** itself enforce evidence-gating or governance; those appear only when Beads is wrapped in a playbook (Agent Flywheel: "completion means tests pass and review rounds come back clean, not just code committed"; beads "encode acceptance criteria and test obligations"). Source: https://agent-flywheel.com/complete-guide
- **Coordination/messaging layer.** `mcp_agent_mail` (FastMCP + Git + SQLite) does identities, inboxes, threads, and *advisory* file leases with a commit-blocking git hook, but is explicitly **not** a task authority — it cites Beads as the task owner ("does not hold canonical task state, requirements tracking, or evidence-gated completion gates"). Source: https://github.com/dicklesworthstone/mcp_agent_mail
- **Evidence-gated completion (the differentiator's strongest prior art).** An arXiv paper describes almost exactly Anvil's model: "verify-gated completion as admission control" with packetized durable state (claim/evidence/common-ground/recovery packets), an append-only audit trace, a read-only admission verifier, an evidence floor, and an 11-condition fail-closed acceptance predicate (φ1–φ11) separating execution from claim from acceptance. Source: https://arxiv.org/html/2605.17998v2
- A documented 2026 industry consensus that AI coding agents falsely claim completion ("evidence before claims, always"), realized today as skills/gate-functions and research — *not* a governed state-of-record product. Source: https://dev.to/moonrunnerkc/ai-coding-agents-lie-about-their-work-outcome-based-verification-catches-it-12b4
- **Issue-tracker-as-coordination-plane (live trend).** Linear's Agent API makes agents first-class workspace teammates with profiles; "issue trackers encode state, ownership, permissions, and history." But trackers do human-oversight gating, not automated proof-of-work, and concede "a raw database is more flexible and performant for high-frequency state changes." Source: https://www.mindstudio.ai/blog/issue-trackers-ai-agent-infrastructure-jira-linear
- GitHub Issues/API is documented as structurally unsuited to machine-speed multi-agent load: a single secondary-rate-limit hit cascaded into a system-wide outage; agent-opened PRs surged ~4M (Sep 2025) → 17M+ (Mar 2026). Source: https://www.tamirdresher.com/blog/2026/03/21/rate-limiting-multi-agent *(confidence: medium)*
- **Workspace/orchestration layer.** Augment Intent, Vibe Kanban (now sunset), Conductor, CrewAI/LangGraph coordinate *execution* via git worktrees + a "living spec" + review-as-PR, but the canonical artifact is per-run/ephemeral, not a durable governed record of claims+evidence. Source: https://www.augmentcode.com/blog/intent-a-workspace-for-agent-orchestration *(confidence: medium)*

### Inference

- The "separate from chat AND from GitHub Issues, durable canonical record" claim is already proven viable by Beads, so Anvil should not argue this layer needs to exist — it should differentiate *within* it. Beads is the credibility anchor, not a threat to the premise.
- Anvil's real wedge is the combination Beads-class tools deliberately omit: **governance + evidence-gated completion baked into the state record itself**. That combination currently exists only as a research architecture (arXiv 2605.17998) and as scattered playbooks (Agent Flywheel bolting gating onto Beads + Agent Mail + git hooks). The defensible line: *"the canonical record that won't let an agent mark a task done without admitted evidence."*
- Anti-positioning to pre-empt: do **not** pitch Anvil as "another kanban for agents" (Vibe Kanban already sunset) or as "memory" alone (Beads + mem0/cognee own that word). The term-of-art to try to define is the *durable, governed, evidence-gated claims+evidence record*.

---

## Angle 2 — Orchestration frameworks on three axes (push/pull, ephemeral/durable, in-process/cross-host)

The landscape splits into two layers, and the "agent framework" conversation clusters on one corner: PUSH, EPHEMERAL-by-default, IN-PROCESS.

### Evidence (sourced)

- **CrewAI** is push-coordinated and in-process; state is ephemeral by default, made durable per-flow via `@persist` (default SQLite). "No task queue, no worker pool, no placement logic." At scale (~2B agentic executions/year, 60%+ Fortune 500) but durability is opt-in app-level persistence. Source: https://docs.crewai.com/en/concepts/flows
- **OpenAI Agents SDK** is the production successor to Swarm (archived); push via handoffs + a central Runner loop, single-process Python-first, ephemeral by default with durable Sessions backends. The Apr 15 2026 release added a subagent primitive (beta), long-horizon harness, native sandbox — deepening *parent-directed* orchestration, not pull or cross-host. Sources: https://openai.github.io/openai-agents-python/ · https://www.openlinksw.com/data/html/openai-agents-sdk-next-evolution-infographic.html *(latter: medium)*
- **Microsoft Magentic / Magentic-One** is push par excellence: a "Magentic manager"/Orchestrator plans (task ledger), tracks progress (progress ledger), selects the next agent, loops. In the Microsoft Agent Framework (unifying AutoGen + Semantic Kernel, designated primary production platform early 2026) it runs in-process with optional in-memory checkpointing. Sources: https://learn.microsoft.com/en-us/agent-framework/workflows/orchestrations/magentic · https://cloudsummit.eu/blog/microsoft-agent-framework-production-ready-convergence-autogen-semantic-kernel/
- **LangGraph** is the most durable of the framework group — checkpoints thread/graph state per super-step to Postgres/Redis/DynamoDB, enabling resume/time-travel/HITL — but remains push (a graph/supervisor drives nodes). Source: https://docs.langchain.com/oss/python/langgraph/persistence
- Multiple 2026 analyses argue LangGraph/CrewAI/Google ADK provide **checkpoints, not durable execution**: no automatic failure detection, no automatic recovery, no duplicate-execution prevention, no distributed coordination — all left to the developer. Source: https://www.diagrid.io/blog/checkpoints-are-not-durable-execution-why-langgraph-crewai-google-adk-and-others-fall-short-for-production-agent-workflows
- **Temporal** is the canonical durable + pull + cross-host engine: workflow state lives in a cluster/Cloud independent of any worker; polyglot workers across hosts/regions/clouds pull from task queues; Replay 2026 added serverless (AWS Lambda) workers, workflow streams, task-queue priority/fairness, multi-cloud replication GA. Source: https://temporal.io/blog/replay-2026-product-announcements
- Temporal et al. coordinate an activity/function/step, **not a whole agent harness** — you author the agentic loop as a Workflow and LLM/tool calls as Activities. Source: https://callsphere.ai/blog/temporal-ai-agent-workflows-durable-execution-workflow-as-code
- **DBOS** is a lightweight durable-execution library inside your app on Postgres (no separate broker/control plane); **Inngest** is event/HTTP-push durable execution against existing servers with managed state. Sources: https://www.dbos.dev/dbos-transact
- A 2026 reference architecture pairs LangGraph-style checkpointers *with* a durable engine (Temporal/Step Functions/Restate/DBOS/Inngest) — confirming the two-layer split. Source: https://appscale.blog/en/blog/durable-execution-llm-agents-temporal-langgraph-checkpointing-2026 *(confidence: medium)*
- **Blackboard pattern** is the literature's name for pull/claim semantics — agents monitor a shared store, self-select work by preconditions, write back, optimistic-lock on conflict — but it is a *design pattern*, not a productized cross-host substrate, and is a minority pattern (supervisor/sequential dominate production). Source: https://callsphere.ai/blog/blackboard-architecture-multi-agent-systems-shared-knowledge-spaces

### Inference

- Anvil should position **orthogonally to the named frameworks**, not as a competitor: the whole CrewAI / OpenAI Agents SDK / Magentic family occupies the push/ephemeral/in-process corner and treats sub-agents as objects inside one process. "Anvil is the substrate the harnesses themselves coordinate through" is clean and defensible against them.
- The honest competitive nuance is the **durable-execution tier**. Temporal already nails durable + pull + cross-host, so Anvil cannot win on those three alone — Temporal reads them as table stakes. The differentiator must be the *unit of coordination* (a whole heterogeneous agent harness, not a function) plus agent-native safety primitives (leases, file-conflict checks, work packets, completion evidence, claim-guards).
- Framed precisely: **Anvil = blackboard-pattern pull/claim semantics realized on a durable, cross-host, cross-harness runtime.** Recommended posture: concede and embrace the durable-execution heritage — Anvil could sit *above/alongside* a durable engine rather than reinventing a Temporal cluster.

---

## Angle 3 — Job-market / pull-based-labor prior art

Best described as a **novel recombination of well-proven parts**, not a novel primitive.

### Evidence (sourced)

- **GitHub Actions self-hosted runners are the literal pattern:** capability labels (`runs-on`) + idle-runner pull/assignment + queue-until-match. "GitHub looks for a runner that matches the job's `runs-on` labels… if found online and idle, the job is assigned… otherwise the job remains queued." Source: https://docs.github.com/actions/hosting-your-own-runners/using-labels-with-self-hosted-runners
- The "agents on runners" variant already ships: GitHub Copilot coding agent runs the LLM agent inside a GitHub Actions environment and can target self-hosted ARC scale sets via `runs-on` labels. Source: https://github.com/orgs/community/discussions/177903
- **GitHub Agentic Workflows (`gh aw`)** compiles plain-language agent tasks to Actions workflows running Claude Code / Codex / Gemini / Copilot as the engine — but is push/event-triggered, not a durable pull-market across multi-cloud fleets. Source: https://githubnext.com/projects/agentic-workflows/
- Durable-execution engines already provide the entire queue+lease+heartbeat+capability-routing substrate (Temporal: workers pull from task queues; heartbeating; dedicated GPU/capability queues). Source: https://www.spheron.network/blog/ai-agent-workflow-orchestration-temporal-inngest-restate-gpu-cloud/
- Market/auction allocation to heterogeneous workers is **40+ years of prior art** — Contract Net Protocol (1980), MURDOCH, market-based MRTA surveys. "The 'labor market for agents' framing is a re-skin of CNP." Source: https://link.springer.com/article/10.1007/s10846-022-01803-0
- Capability advertisement/discovery is now standardized (A2A Agent Cards, Linux Foundation v1.0, 150+ orgs) — but A2A is **push delegation** with task lifecycle states, no pull-queue/claim/lease. Source: https://www.digitalapplied.com/blog/ai-agent-protocol-ecosystem-map-2026-mcp-a2a-acp-ucp
- A live economic agent labor market exists (dealwork.ai, opentask.ai, execution.market, ugig.net; Coinbase x402 ~69k agents / 165M+ tx / ~$50M volume by Apr 2026; Circle marketplace) — but these emphasize **payment, escrow, reputation, discovery**, not runner-fleet leasing. Source: https://dev.to/kirothebot/the-agent-economy-is-real-12-platforms-where-ai-agents-actually-earn-money-may-2026-5bm2 *(confidence: medium)*
- The dominant 2026 multi-agent pattern is **push/wave-dispatch**: SPOQ computes dependency waves and the orchestrator *spawns* specialist agents per task (model-tier-matched), "no lease or claim mechanism… orchestrator-assigned rather than agent-claimed." Source: https://arxiv.org/html/2606.03115v1
- Commercial "agent fleet/runner" products (Netlify Agent Runners, Okteto Agent Fleets) are about ephemeral isolated *environments*, UI/CLI-triggered (push/on-demand), not durable pull queues with capability-labeled claiming. Confirms heterogeneous-runtime demand (Claude Code/Codex/Gemini/local). Source: https://docs.netlify.com/build/build-with-ai/agent-runners/overview/ *(confidence: medium)*
- Distributed capability directories (AGNTCY/OASF, Linux Foundation; Cisco-originated) provide content-addressed, signed, taxonomy-based discovery across heterogeneous MAS — the registry half — but again discovery/provenance, not a work queue with leasing. Source: https://arxiv.org/html/2509.18787v1 *(confidence: medium)*

### Inference

- Position the pull-market not as a new mechanism but as the **missing integration layer** and the right abstraction: *"the agent runtime is the unit of capability."* Lean into the GitHub Actions analogy explicitly — it is literal prior art users already understand; the credible novel claim is "self-hosted runners, but the runner is a heterogeneous agent runtime and the labels describe agent capabilities + repo/file scope, not just OS/arch."
- Do **not** reinvent queue/lease/heartbeat — those are solved (Temporal/Inngest/Restate, and Anvil's own `claim_task`/`renew_claim`/`heartbeat`). The contribution is wiring durable queue + capability matching + lease semantics specifically to coding-agent runtimes across local + multi-cloud, which no surveyed product packages together.
- The articulable architectural advantage of pull over the push-dominated field (A2A, SPOQ, Netlify/Okteto): **pull decouples task supply from runner supply**, lets a heterogeneous fleet self-select by capability, and degrades gracefully. Interoperate with A2A Agent Cards / AGNTCY directories (the emerging discovery standard) while owning the durable pull-queue + lease + conflict-aware claiming layer they lack.
- Honest caveat: components are all proven, so "novel" must mean integration + framing; CNP/MRTA is 40-year prior art (cite it, claim the agent-runtime instantiation).

---

## Angle 4 — Where Anvil is genuinely differentiated vs. where prior art overlaps (incorporating the adversarial check)

This section deliberately does **not** overclaim novelty. The adversarial check returned **partially_refuted** on both runs, and we adopt its honest framing.

### What the adversarial check refutes

- **The marketing-style claim "no existing tool combines 2+ of the five pillars" is decisively REFUTED.** Multiple shipping tools combine 3–4 pillars. This framing should be **dropped**.
- **Temporal hits 4 of 5 pillars in production** (P1 durable governed state via append-only Event History; P2 lease + activity heartbeat with reclaim-on-timeout — "functionally identical to Anvil's `lease_expires_at` + `last_heartbeat_at` + stale-reap"; P4 capability-matched pull via task queues / Worker Versioning / build-ID routing; P5 shared multi-host server). The only pillar it does not make first-class is **P3 evidence-gated completion** — completion is just an activity's return value. Sources: https://docs.temporal.io/task-routing · https://docs.temporal.io/encyclopedia/detecting-activity-failures · https://docs.temporal.io/workflow-execution
- **Lease+heartbeat (P2) is a commodity primitive**, present in AWS SQS visibility-timeout + `ChangeMessageVisibility` heartbeat, and Celery `visibility_timeout` + `acks_late`. Sources: https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-visibility-timeout.html · https://docs.celeryq.dev/en/stable/userguide/configuration.html
- **AI-agent job marketplaces** combine P3 (verifiable completion via vetting/sandbox/escrow) + P4 (pull/bid) + P5 (shared backend) — 3 of 5. Gap: no durable governed *project* state (P1), no lease/heartbeat (P2). Source: https://dev.to/kirothebot/the-agent-economy-is-real-12-platforms-where-ai-agents-actually-earn-money-may-2026-5bm2 *(confidence: medium)*
- **Heterogeneous coding-agent orchestrators** (Claude Code Agent Teams, Emdash ~22 CLI providers, Intent BYOA) are the closest match to Anvil's signature P4 — heterogeneous runtimes pulling from a shared task list with claim/assignment + file-locking ("each agent marks tasks as in-progress before starting, preventing other agents from claiming the same work"). Gaps: no lease/heartbeat (P2), no evidence gate (P3), single-project not cross-machine (P5), only partial durable state. Source: https://www.mindstudio.ai/blog/claude-code-agent-teams-parallel-workflows
- **OpenClaw/Moltbook (arXiv 2602.19810)** is the single closest thing to all five at once: pull dispatch loop (agents poll on a 45–90s cycle), heartbeats every 5 min, a `run_evidence_gate` rejecting evidence whose witness is not allow-listed (overlaps P3+P4), shared Supabase/Postgres backend (P1/P5). But it is a research/social-science agent swarm, not a governed software-task labor market with PRD-gated state and six-dimension capability scoring. Source: https://arxiv.org/pdf/2602.19810 *(confidence: medium)*
- **AEX / Agent Exchange (arXiv 2507.03904)** explicitly *proposes* the "pull-based labor market for AI agents" vision — central auction, capability matching, real-time bidding, agent hubs, value attribution/settlement — i.e. the exact end-state Anvil's marketing claims, **as a research design, not a shipped tool.** Source: https://arxiv.org/abs/2507.03904 *(confidence: medium — mapped from abstract/snippets, not the full implementation sections)*
- **RLVR env-hubs (Prime Intellect / OpenReward)** already pair P3 (verifiable completion — "a compiler ran the output and returned pass or fail") with P5 (shared multi-host backend serving 330+ environments / 4.5M+ tasks agents pull). Gap: no governed project lifecycle (P1), no lease/heartbeat (P2). Source: https://www.primeintellect.ai/blog/environments
- **LangGraph / DBOS / Inngest** cover P1 (+ partial P3 via human-in-the-loop, + partial P5 via shared Postgres) but lack the pull-market + lease/heartbeat across competing heterogeneous runtimes. Sources: https://docs.langchain.com/oss/python/langgraph/persistence · https://www.dbos.dev/blog/dbos-new-features-march-2026

### Where Anvil is genuinely differentiated (the surviving, narrow claim)

- The **full five-pillar conjunction packaged as a pull-based labor market for AI *coding* agents** was not found in any single shipping product. This is an **integration/packaging novelty, not a primitive novelty.**
- The decisive, narrowest unclaimed gap is the **fusion of P3 (a declarative `required_evidence` / `verification.commands` gate that blocks `needs_review → accepted`) with P4 (capability-matched pull by heterogeneous coding-agent runtimes — Claude Code / Codex / Gemini), welded onto a governed software-PROJECT backlog with lease/heartbeat claims.** Temporal nails the infra pillars but has no evidence gate and runs *your* code, not arbitrary agent CLIs; the agent-team orchestrators nail heterogeneous-runtime pull but lack leases/heartbeats, an evidence gate, and a shared multi-host backend; the marketplaces nail verifiable completion + pull + shared backend but have no durable governed *project* state.
- Anvil's **six-dimension `agent_suitability` capability scoring** of coding agents to coding tasks is a real (if narrow) differentiator — no found tool does rich semantic capability-matching at this granularity; existing systems do label/version/role routing.

### Honest caveats to carry into any external claim

- **Anvil's own P5 is partly aspirational.** Per the adversarial check against Anvil's `architecture.md`, Anvil is currently local-first SQLite single-host — so "shared multi-host backend" is roadmap for Anvil too. Any comparison that pits Anvil's roadmap against competitors' *shipped* systems must say so.
- The right external framing is **"no shipping product combines all five,"** *not* "no tool combines 2+." The architecture is an integration of well-established primitives, with one runtime (Temporal) at 4/5 and an active research proposal (AEX) targeting the identical end-state.
- Confidence: **high** that 3–4-pillar overlaps exist (primary vendor docs for Temporal/GitHub/SQS/Celery); **medium** on the precise pillar-count for marketplaces, OpenClaw, and AEX (secondary reporting / preprints, fast-moving).

---

## Angle 5 — Are local agents still "fast but dumb"? (2026 reality)

This grounds the blog's central tension. The honest answer: **no longer "dumb," but the size of the closed gap is benchmark- and harness-dependent.**

### Evidence (sourced)

- **gpt-oss-120b**: ~116.8B total / 5.1B active, MoE (128 experts, top-4), 131,072 ctx, natively MXFP4 (4.25 bits/param) so it fits one 80GB GPU; **62.4% SWE-bench Verified** per OpenAI's model card. Fits the 96GB RTX PRO 6000 comfortably and can run on a 32GB RTX 5090 in MXFP4 (only ~5B params activate; KV cache is the squeeze). Source: https://arxiv.org/html/2508.10925v1
- **Qwen3-Coder-Next** (strongest single-card-fittable open coder): 80B total / **3B active** (512 experts, 10+1 active; hybrid Gated DeltaNet + Gated Attention), 262,144 native ctx, **70.6% SWE-bench Verified, 44.3% SWE-bench Pro, 36.2% Terminal-Bench 2.0** per its model card. The 3B active count is what lets an 80B model run at 4-bit on a 32GB card. Source: https://huggingface.co/Qwen/Qwen3-Coder-Next
- **GLM-4.5-Air** (106B / 12B active, 128k ctx, MIT) runs as AWQ-4bit on a single RTX PRO 6000 96GB (otherwise needs 2× 48GB L40S). GLM-5 (744B MoE, ~40B active, 1M ctx) is data-center scale, not one-card. Source: https://apxml.com/models/glm-45-air
- **SGLang RadixAttention** (radix-tree prefix caching) + **FP8 KV cache** is the decisive serving lever for agentic coding where a large system-prompt/skills/tool-schema prefix replays every turn: ~3× requests/sec over older vLLM for a coding agent reusing ~100K-token prefixes. Source: https://www.runpod.io/blog/sglang-vs-vllm-kv-cache *(confidence: medium)*
- **FP8 KV cache** roughly halves KV storage vs BF16 → higher concurrency / longer context; near-full accuracy when implemented carefully (AUC recovery 94–98%+), but *naive* FP8 attention collapsed a 128k needle task to 13% until two-level FP32-accumulation restored it to 89%. Break-even context ~7k tokens. Source: https://vllm-project.github.io/2026/04/22/fp8-kvcache.html
- **The "system prompt eats my context + compaction" problem is real and partly defeats prefix caching:** agentic loops break the cache via (1) context growth shrinking the cached fraction, (2) any system-prompt/tool-schema edit invalidating the *entire* prefix, (3) compaction rewriting history. Cache hit rate degrades over a long task, eroding the theoretical saving. Source: https://arxiv.org/pdf/2601.06007
- **The benchmark nuance that the blog must land:** open models look near-frontier on SWE-bench *Verified* (70–80%+) but these are largely vendor-harness numbers; on Scale's contamination-resistant **SWE-bench Pro under one standardized harness, absolute scores collapse and the gap reopens** — frontier ~59% (GPT-5.4 xHigh 59.1%, Opus 4.6 thinking 51.9%) vs open weights ~38–44% on the same SEAL scaffold (Qwen3-Coder-480B 38.7%; Qwen3-Coder-Next 44.3% self-reported). Vendor-reported open Pro scores (~58% for GLM-5.1/MiniMax) are **not** comparable to SEAL's standardized numbers. Source: https://www.morphllm.com/swe-bench-pro *(confidence: medium)*
- **Hardware tiers:** RTX 5090 32GB (1,792 GB/s) is the throughput/consumer tier for small-active MoE coders at 4-bit (and gpt-oss-120b MXFP4 if KV is managed); RTX PRO 6000 96GB is the capability tier — runs 106–120B-class coders at usable quant with long-context headroom on one card, avoiding multi-GPU PCIe overhead. Source: https://www.hardware-corner.net/gpu-llm-benchmarks/rtx-pro-6000-blackwell/ *(confidence: medium)*
- Data-center-tier open coders to contrast against (NOT one-card): DeepSeek V4 (V4-Pro 1.6T/49B active; V4-Flash 284B/13B active; 1M ctx), GLM-5.x (744B MoE / ~40B active / 1M ctx). Gemma 4 (Apache 2.0, up to 31B dense / 26B MoE, 256K ctx) is the cleanest-license fully-local pick but trails dedicated coders on agentic tasks. Source: https://www.mindstudio.ai/blog/deepseek-v4-launch-specs-open-weight-2026 *(confidence: medium)*

### Inference

- The blog's central tension resolves to a **two-part, defensible claim**: (1) open models have genuinely crossed from "fast but dumb" to "useful for real multi-step agentic work," AND (2) a measurable gap to Opus/GPT-5 remains on contamination-resistant standardized SWE-bench Pro (open ~38–44% vs frontier ~59%). Stating *both* is what makes the post credible to skeptical engineers. Do **not** repeat the lazy "70–80% SWE-bench, basically frontier" line — that's a vendor-harness number.
- The "3B active" MoE trick is the real story: it **decouples capability (total params) from speed/VRAM (active params)**, which is why an 80B model runs on a consumer card.
- The KV/prefix-cache section is where Anvil-style tooling differentiates, but with an honest caveat: the actionable product angle is **keep system prompts and tool schemas stable and append-only, and treat compaction as a cache-buster to be minimized**, not a free optimization.
- A skeptic's hygiene note: many 2026 leaderboard/aggregator sites are SEO content with inconsistent or fabricated future-model names — anchor every cited number to a primary source (model cards, Scale SEAL methodology, vLLM/SGLang docs).

---

## Angle 6 — Economics: when does the local box pay off?

### Evidence (sourced)

- **Claude Max** is $100/mo (5x) / $200/mo (20x) on a 5-hour rolling + weekly bucket *pooled across Claude Code, Claude.ai, and Cowork*; Anthropic publishes only relative multipliers, plus two weekly caps (all-models + Sonnet-only). Source: https://support.claude.com/en/articles/11049741-what-is-the-max-plan
- Usage-limit pain is documented: Anthropic was hit with a lawsuit over Max usage limits; an enterprise outlier ran a ~$500M Claude bill in 30 days from uncapped access. Source: https://www.engadget.com/2194626/anthropic-hit-with-lawsuit-over-its-claude-max-usage-limits/ *(anecdotes/outliers; confidence: medium)*
- **OpenAI Codex** is a separate parallel spend (Free/Go $8/Plus $20/Pro $100/$200), billing on API-style token rates via credits since Apr 2, 2026; a typical Codex session ~$0.50–$2.40; ~$100–$200/dev/month. Source: https://developers.openai.com/codex/pricing
- **Cloud per-token rates to beat:** Opus 4.8 $5/$25, Sonnet 4.6 $3/$15, Haiku 4.5 $1/$5 per MTok; cache-read 0.1× input; Batch 50% off. A single agentic task can push 400K–2M cumulative input tokens; heavy agentic devs spend $400–$2,000/mo (outliers $4,000+). Source: https://platform.claude.com/docs/en/about-claude/pricing
- **The box:** a single RTX PRO 6000 Blackwell (96GB GDDR7, 1,792 GB/s, 600W) retails ~$8,500–$9,200 (MSRP $8,565, Mar 2025; one marketplace listing hit $13,250); a full single-GPU workstation lands near ~$11.5k. 96GB fits 70B-class or large-MoE coders entirely on one card. Source: https://www.thundercompute.com/blog/nvidia-rtx-pro-6000-pricing
- **Marginal local cost ≈ electricity:** ~$0.02/M tokens at batched throughput on a 30B MoE (vs Sonnet $3/$15, Opus $5/$25); ~$630–$840/yr power at 450–600W sustained. Source: https://www.spheron.network/blog/rent-nvidia-rtx-pro-6000/ *(the ~6–12mo break-even below is arithmetic on these inputs, not a vendor figure; confidence: medium)*
- **Speed is split (state honestly):** local wins on latency (p99 TTFT 10–50ms vs cloud 200–800ms) and on *batched/parallel* throughput (single 6000 Pro ~8,400 tok/s aggregate on a 30B MoE at 400 concurrent), but a *single-stream* long generation is often *slower* locally (~15–25 tok/s consumer vs 60–80 tok/s Claude). Source: https://www.kunalganglani.com/blog/llm-api-latency-benchmarks-2026
- **Quality still trails frontier:** best open coders mid-2026 — GLM-4.7 ~74.2, Qwen3-Coder-Next ~70.6, DeepSeek-V3.2 ~70.2, Qwen3.6-35B-A3B ~73.4 SWE-bench Verified — vs Claude Sonnet 4.6 ~77–79.6. A ~5–9-point trade for sovereignty/cost. Source: https://www.softwareseni.com/qwen3-coder-next-deepseek-v3-2-and-glm-4-7-which-open-weight-model-wins-for-coding-agents/
- **Non-cost drivers are the strongest local case:** privacy/data sovereignty (code never leaves the machine — decisive for proprietary/regulated codebases), provider independence (no outages, no rate caps, no bill shock), and an on-prem shift (cited 55% of enterprise inference on-prem/edge in 2026, up from 12% in 2023). Source: https://renewator.com/the-rise-of-local-llms-privacy-and-sovereignty-in-2026/ *(the 55% stat is a secondary-blog figure; cite with caution; confidence: medium)*

### Inference

- Frame the local box as a **complement, not a wholesale replacement**: keep Claude Max for peak-quality interactive reasoning (where Opus/Sonnet lead by ~5–9 SWE-bench points and single-stream output is faster), and move the **sustained, high-volume, structured, parallel** agentic load onto the box — precisely where you hit weekly caps and burn tokens in two places (Claude + Codex).
- The economics win on the three axes that map to that load: (1) marginal cost collapses to ~electricity, so ~$11.5k amortizes against a $1k–$2k/mo heavy habit in **roughly 6–12 months — but only if you saturate the box** (condition the break-even on utilization, never promise it flatly); (2) sell speed correctly — lead with low latency + massive parallel throughput, do **not** claim a single long generation beats cloud; (3) privacy/sovereignty + provider independence is the cleanest, least-contestable selling point.
- Net verdict: **YES** for someone running continuous parallel agentic loops, already maxing subscriptions and paying API overages, on privacy-sensitive code; **NO** for an intermittent single-session user who would leave the box idle.

---

## Open risks & unknowns

- **The strong-form novelty claim rests on absence of evidence.** "No shipping product combines all five pillars" survived the adversarial check only because none was *found* — and OpenClaw/Moltbook (arXiv 2602.19810) and AEX (arXiv 2507.03904) approach or explicitly target the same end-state. A shipped product could close this gap quickly; the moat is integration + framing + execution, not a defensible primitive. *(confidence: medium)*
- **Anvil's own P5 (shared multi-host backend) is aspirational, not shipped** — currently local-first SQLite single-host per `architecture.md`. External comparisons must not pit Anvil's roadmap against competitors' shipped systems.
- **Temporal is the real competitive overhang.** It ships 4/5 pillars and could add an evidence/verification gate or first-class agent-CLI workers; Anvil should plan to interoperate with or sit above durable engines rather than out-engineer their clusters.
- **The marketplaces are the wildcard on the economic side.** x402/Circle/dealwork optimize payment+discovery+reputation, a different axis than Anvil's intra-project conflict-aware execution — but a marketplace adding governed project state + leases would converge on Anvil's space.
- **Benchmark integrity is fragile.** SWE-bench Verified numbers are largely vendor-harness; SWE-bench Pro standardized numbers tell a less flattering story; many 2026 leaderboard sites are SEO content with possibly-fabricated model names. Any quantitative claim must anchor to a primary source, and several model-name/version strings in the source material (e.g. "GLM-5.1", future Claude/GPT versions) should be treated as low-confidence until verified against primary docs.
- **Local-serving cost claims are utilization-sensitive and quant-dependent.** The ~6–12-month break-even is arithmetic on cited marginal-cost inputs, not a measured figure; GPU prices swing $8.5k–$13.25k by channel; and the prefix-cache economics erode under real agentic loops (compaction/system-prompt edits). Treat the economic case as conditional, not guaranteed. *(confidence: medium)*
- **The "agents lie about completion" pain is documented mostly in blogs/research, not quantified at product scale** — the evidence-gate value proposition is well-motivated but its measured impact on real coding-agent reliability remains an open question. *(confidence: medium)*
- **Discovery-standard lock-in risk:** A2A Agent Cards and AGNTCY/OASF are consolidating the capability-advertisement layer with 150+ orgs. If Anvil does not interoperate, it risks being routed around; if it does, it cedes that layer and must defend purely on the pull-queue + lease + evidence-gate layer.
