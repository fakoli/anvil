# Backlog generation & management as a first-class anvil capability

> **Date:** 2026-06-19 · **Status:** Research brief (input to a spec) · **Produced by:** a 22-agent deep-research workflow (5-lane competitive landscape → demand sweep → adversarial verification → synthesis). Next step: backlog item **B37** drives a structured Q&A → PRD from this.

A decision-ready research brief for the anvil maintainer. EVIDENCE and RECOMMENDATION are kept separate; competitor claims are marked where a verification pass knocked them down. All source URLs are cited inline and deduped in §8.

---

## 1. The question & why now

**The question.** Should anvil treat *backlog generation and ongoing management* — not just parsed-task execution — as a first-class, governed capability, and if so, what is the defensible shape that avoids the PM-tool graveyard?

**Why now (the gap we hit in our own process).** This session's backlog items (epic E9/B30–B33, E10/B34–B36) were created by an **ad-hoc, manual, orchestrator-driven flow**: a friction signal or a user probe triggered a research fan-out (codebase file:line + web), agents returned briefs, the orchestrator distilled a single insight, structured Q&A pinned the trade-off cut, and each insight became a fixed-shape item (rationale, 2–3 trade-off options with a recommendation, file:line targets, acceptance criteria, Priority/Effort/Type) appended to `docs/backlog/anvil-backlog.md`, optionally ingested into anvil's SQLite state as requirements→features→tasks with hand-authored dependency ordering, and shipped one-PR-per-item under CI + Greptile + Copilot. It works, but it is **slow, re-litigated each time, and invisible to anvil itself**: anvil knows about parsed *tasks*, but is blind to the standing backlog of *remaining ideas/items* across sessions, and the markdown backlog and the state DB are two stores that drift.

**The cross-session backlog-awareness gap is real and vendor-acknowledged.** Across Claude Code, Cursor, Codex, and Devin, users describe starting each session from zero and re-explaining architecture/decisions (Claude Code issue [#2954](https://github.com/anthropics/claude-code/issues/2954)), agents "remembering only the last 15 minutes" ([artmnk](https://artmnk.substack.com/p/how-to-vibe-code-as-a-professional)), and repeating themselves every session ([dev.to/sean8](https://dev.to/sean8/memento-give-claude-code-persistent-memory-so-you-stop-repeating-yourself-22je)). Crucially, **every shipped "memory" feature stores stable facts/conventions, not the live list of what-remains**: OpenAI Codex Memories *explicitly disclaims* active backlogs/roadmaps/WIP and tells users to keep that in checked-in docs ([developers.openai.com/codex/memories](https://developers.openai.com/codex/memories)); Devin's Knowledge Base stores conventions and *resets the workspace every session* ([vectorize.io](https://vectorize.io/articles/do-ai-agents-learn-between-sessions)); Cursor *shipped then removed* Memories in v2.1, leaving only Rules ([forum.cursor.com](https://forum.cursor.com/t/custom-modes-and-memories-gone-in-2-1/143744)). They solve "remember how we work"; none solves "generate and manage what's left to do."

---

## 2. Landscape

A tool either (a) **captures/ideates** an idea into a doc, (b) **manages an ongoing backlog**, or (c) does neither (pure note storage / agent plumbing). "Manages ongoing backlog?" is scored strictly: continuous cross-session dedup, re-prioritization, and grooming of a *standing* list — not "updates a score when feedback links in" and not "an agent works items you already created."

### Lane A — AI Product-Management / Roadmap tools

| Tool | Idea → backlog | Manages ongoing backlog? | Gap |
|---|---|---|---|
| **Productboard + Pulse AI** | Pulse clusters feedback into themes; AI auto-links notes→features + impact score; PM verifies & orders | Partial | Auto-links stay **"unverified" until a human confirms**; can't filter to verified-only. No autonomous re-groom/dedup/re-rank. ([support.productboard.com](https://support.productboard.com/hc/en-us/articles/26949590820627-Link-insights-automatically-with-Productboard-AI)) |
| **Aha!** | Auto-captured ideas; AI agent ranks by human-set product-value score, summarizes trade-offs; PM applies in Roadmaps | Partial | Garbage-in: leans on human scorecard; recommends but does not own grooming/dedup/re-rank. Richest pieces gated behind Ideas Advanced tier. ([support.aha.io](https://support.aha.io/aha-software/ai-assistant/ai-prompt-library/ai-agents/feature-prioritization)) |
| **Jira Product Discovery + Rovo** | Human impact ratings + formula fields produce a prioritized view; Rovo only drafts/summarizes text | Partial (manual) | **No AI prioritization, no ranking, no auto-linking** — verified. Agentic roadmapping is "exploring," not GA. ([support.atlassian.com](https://support.atlassian.com/jira-product-discovery/docs/explore-atlassian-intelligence-in-jira-product-discovery/), [atlassian.com/blog](https://www.atlassian.com/blog/company-news/introducing-product-collection)) |
| **Linear** | Conversation→drafted issue, first-pass dedup, route by volume/revenue | No | Engineering tracker, not discovery engine; "with priority" is **overstated** — priority is absent from Triage Intelligence's predicted properties. Intake-only. ([linear.app/docs/triage-intelligence](https://linear.app/docs/triage-intelligence)) |
| **ClickUp Brain** | Extracts themes from pasted feedback; "Prioritize with AI" re-ranks existing tasks | No | Disjoint, human-orchestrated; **no native help-desk ingestion, no impact/risk scoring** — claim knocked down. ([eesel.ai](https://www.eesel.ai/blog/clickup-brain)) |
| **Notion AI / Projects** | Ideas in a DB; AI summarizes/dedupes/autofills; humans own ordering | No | Closest to "stores ideas." Scoring/grooming is template+human; autofill unreliable for subjective priority. ([notion.com/help/autofill](https://www.notion.com/help/autofill)) |
| **Dovetail** | Multi-source voice→AI cluster/sentiment→drafts PRDs→hands off to Productboard/Jira | No (by design) | Insights repository; stops at the insight (though 2025 platform can now create Linear issues). ([dovetail.com/blog](https://dovetail.com/blog/dovetail-launches-customer-intelligence-platform/)) |
| **Cycle** | Autopilot extracts/categorizes feedback→creates Jira/Linear/Productboard items | No — **dead** | **Acquired by Atlassian Sep 2025; standalone sunset Oct 31 2025.** "Learned conventions" is marketing embellishment. |
| **Savio** | Human links feedback (=1 vote); sort by votes + ARR/MRR | No | Vote-tally tracker, **no AI**. Thinnest tool on both axes. ([savio.io](https://www.savio.io/how-savio-works/)) |

### Lane B — Spec-driven-dev & agent-planning

| Tool | Idea → backlog | Manages ongoing backlog? | Gap |
|---|---|---|---|
| **GitHub Spec Kit** | `/specify→/plan→/tasks` decomposes ONE feature into tasks.md on a fresh branch | No | A spec is "the lifetime of a change request, not a feature" (Fowler). **No cross-feature backlog file**, no dedup/re-prioritize. ([github.com/github/spec-kit](https://github.com/github/spec-kit)) |
| **claude-task-master** | One-time `parse-prd` of a hand-written PRD → evolving task list (add/expand/move/next/deps/tags, state in `.taskmaster/state.json`) | **Yes** (closest model) | Manages a *to-do list*, not a curated backlog: **no continuous idea intake, no dedup, no auto re-prioritization**; documented PRD data-loss ([Discussion #864](https://github.com/eyaltoledano/claude-task-master/discussions/864)). |
| **Vercel eve** | n/a — durable backend-agent framework; per-session task graphs are scratch | No | **Wrong layer** — zero backlog concept; durability resumes sessions, not a groomed backlog. Plumbing you'd build a backlog tool *on*. ([vercel.com/docs/eve/concepts](https://vercel.com/docs/eve/concepts)) |
| **Cursor (Plan Mode + to-dos)** | Plan Mode → reviewable plan; agent to-do lists per task | No | To-do list is transient working memory; message queue is FIFO, not a curated backlog. ([forum.cursor.com](https://forum.cursor.com/t/cursor-does-not-use-a-to-do-list/144227)) |
| **Windsurf (Cascade Planning)** | Persistent markdown plan file per goal, survives sessions | Single-goal | Cross-session persistence for **one** plan — not a multi-item prioritized backlog; no dedup/grooming. |
| **Devin** | Editable per-session plan; consumes Linear/Jira tickets | No | **Works** items already in the tracker; backlog of record lives elsewhere. ([vectorize.io](https://vectorize.io/articles/do-ai-agents-learn-between-sessions)) |
| **OpenHands** | Resolver: label a GitHub issue → autonomous PR | No | Backlog of record is GitHub issues; resolves one at a time. |
| **Sourcegraph Amp** | Per-thread TODOs / TODO.md as working memory | No | "One thread per task" — TODOs are scratch, not a groomed backlog. |
| **Tessl** | Spec-first, spec-anchored; drift detection | No | Manages *specs* and code-vs-spec drift — orthogonal to backlog management. |
| **Kiro (AWS)** | Per-spec requirements/design/tasks.md; specs discarded post-task | No | Backlog management **bolted on** via external MCP (Backlog.md) or Jira/Linear. |

### Lane C — Ideation / brainstorming

| Tool | Idea → backlog | Manages ongoing backlog? | Gap |
|---|---|---|---|
| **Superpowers (obra) brainstorming** | ONE idea → one approved design doc (`docs/superpowers/specs/…`) → writing-plans → execute | No | **Feature-linear funnel** with a hard gate against any other action until the single design is approved; **no skill persists/dedups/grooms ideas across sessions.** Precisely "not quite what we're looking for." |
| **Miro AI / FigJam AI + Jambot** | Generate + AI-cluster stickies; manual/shallow push to Jira | No | Structuring stops at affinity clusters; no scored/deduped items, no grooming. |
| **Ideaflow / Mem / Reflect** | Frictionless capture + semantic linking/resurfacing | No | "Second brain" — store and connect, never convert to scored/managed work items. |
| **ChatPRD** | Idea→PRD/stories; "Product Backlog" is a static template; export to Linear/Notion | No | Document-centric, **one-shot**; no cross-session memory, dedup, or re-score. ([prodpad.com](https://www.prodpad.com/prodpad-vs-productboard/)) |
| **Notion AI (brainstorm + /action items)** | Extract tasks from notes into a DB | No | Per-invocation; the DB + humans do the actual backlog management. |

### Lane D — Issue trackers + AI grooming

| Tool | Idea → backlog | Manages ongoing backlog? | Gap |
|---|---|---|---|
| **Linear (Triage Intelligence)** | Semantic dup-detection over existing backlog; auto-merge duplicate customer requests; route | Intake only | Genuinely cross-backlog **dedup** today — but **does not score/re-prioritize/re-groom** existing items; no priority inference. ([linear.app/now](https://linear.app/now/how-we-built-triage-intelligence)) |
| **GitHub Issues + Copilot** | Single-issue authoring assist; deeper triage only via Copilot SDK (IssueCrush) | No | Flagship triage is a **DIY SDK example, not shipped**, and explicitly **not dedup/whole-backlog aware**. |
| **Jira AI / Rovo (+ marketplace agents)** | Epic→stories; 3rd-party "Backlog Grooming Agent" flags stale/dup | No | Native AI has **no whole-backlog awareness or holistic prioritization**; grooming agents *flag*, don't continuously re-groom. |
| **Height** | Marketed full vision: autonomous grooming, dedup, spec maintenance | **Claimed Yes** — **dead** | **Shut down 24 Sep 2025** after ~3.5 yrs and $18.3M raised; its pitch was literally AI bug-triage + backlog pruning + auto-updating specs. The one product that targeted autonomous whole-backlog grooming failed — strongest signal the standalone PM-platform play is hard to monetize. ([alternativeto.net](https://alternativeto.net/news/2025/3/height-project-management-tool-to-shut-down-by-september-2025/), [creativerly.com](https://www.creativerly.com/height-app-is-shutting-down/), [HN](https://news.ycombinator.com/item?id=43454034)) |
| **Shortcut + Korey AI** | Generates stories/specs/sub-tasks + reporting | No | Generation + reporting, **not grooming**; users report backlog mgmt "almost non-existent." |
| **Zenhub** | AI sprint planning from existing issues | No | Sprint *planning*, not backlog *hygiene*; no dup detection. |
| **Taskmaster-style (claude-task-master, Atlas)** | PRD→complexity-scored, dependency-ordered task graph + `next` | No | Build-the-backlog-**once**, not maintain-continuously; no dedup, no re-score on drift, no raw-feedback intake. |

### Lane E — Methodologies + AI-PRD tools (the encoding layer)

| Tool/method | Idea → backlog | Manages ongoing backlog? | Gap |
|---|---|---|---|
| **RICE** | Scoring rubric over captured items | No | Inert spreadsheet math; Effort/Confidence are the optimistic estimates RCF exists to fix. |
| **Opportunity Solution Tree (Torres)** | outcome→opportunity→solution→assumption-test promotion gate | No | Best **conceptual** idea→validated-item model; encodes zero in executable form. |
| **JTBD / story mapping** | Job stories → ordered map | No | Framing + sequencing only; no capture/dedup/scoring/persistence. |
| **Reference-Class Forecasting (Kahneman/Flyvbjerg)** | Debiases the effort/confidence estimate against a distribution of *past* items | No | **Single biggest unencoded lever**; needs a curated corpus of past items no PRD tool maintains. |
| **BuildPad / Bolt / Lovable** | One-shot idea→plan/PRD before code | No | No persistent backlog/stories/tasks; context degrades as the build grows. |
| **ProdPad CoPilot** | Idea→balanced priority score, drafts specs, links feedback | **Yes** (best real match) | Every action (merge/link/re-rank) needs **human confirmation**; no stale-item detection, no RCF. ([prodpad.com](https://www.prodpad.com/prodpad-vs-productboard/)) |
| **Zeda.io** | Aggregates signal, auto-tags themes into Product Areas | Partial | Strong intake, thin back: no explicit DoR gate, weaker dedup/merge, no effort forecasting. |

---

## 3. Demand evidence

**What people say is MISSING (`wants_missing`).**
- *Durable task structure beats model horsepower.* Backlog.md's founder: "untangling [Claude Code's] output was slower than writing from scratch. The fix turned out to be process, not model horsepower" — went from 50% to 95%+ task success only after a CLI that turns a spec into per-task files ([HN 44483530](https://news.ycombinator.com/item?id=44483530)).
- *"Can execute but can't prioritize."* "When everything is possible, deciding what to work on becomes the job. Agents can execute; they can't prioritize." ([MindStudio](https://www.mindstudio.ai/blog/ai-agents-infinite-backlog-5-new-organizational-roles)). The bottleneck "moved upstream from engineering throughput to spec quality" ([Allstacks](https://www.allstacks.com/blog/roadmap-slipping-ai-coding-tools-spec-problem)) — both vendor blogs, so demand-signal not capability-proof.
- *Stale plans even within a session.* Cursor staff confirmed a known bug where context summarization "improperly preserv[es] stale plan state," and a user reports it "more than halves my effective context" ([forum.cursor.com/160672](https://forum.cursor.com/t/agent-stuck-referencing-stale-completed-plans/160672)); another rolled his own `/plan-to-file` because native to-dos are unreliable ([144227](https://forum.cursor.com/t/cursor-does-not-use-a-to-do-list/144227)).
- *Even leading spec tools admit specs go stale.* Kiro's creator: "if you do 'vibe coding' via Kiro it can make code changes without updating the specs at all" ([HN 44560662](https://news.ycombinator.com/item?id=44560662)).
- *Cross-session continuity is the named pain.* "Context persistence across sessions — major workflow disruption" forces devs to "repeatedly re-explain project architecture and decisions" ([claude-code #2954](https://github.com/anthropics/claude-code/issues/2954)); Codex users want it to "remember the current task and where we stopped" ([codex #12567](https://github.com/openai/codex/discussions/12567)).
- *Demand specifically for dependency + dispatch, not storage.* HN users ask for dependency tracking, cross-repo backlog, and how to dispatch tasks "without blowing out their context budget" ([HN 44483530](https://news.ycombinator.com/item?id=44483530)).

**What people PRAISE.** The *beloved, solved* job is **frictionless capture + auto-organization** so ideas don't evaporate — Voiceliner ([HN 29726787](https://news.ycombinator.com/item?id=29726787)), Granola ("I never have to worry about missing anything important," [G2](https://www.g2.com/products/granola/reviews); [zackproser.com](https://zackproser.com/blog/granola-ai-review)), Saner.AI ([Product Hunt](https://www.producthunt.com/products/saner-ai/reviews)). Praise for genuine backlog **generation** appears *only* in dev-PM tools and centers on **structure** (dependencies/IDs/subtasks) and **traceability**: "My rambling spec was turned into a crystal-clear PRD, then exploded into bite-sized, dependency-aware tasks" ([Emelia/Reddit](https://emelia.io/hub/claude-task-master-ai-project-management)); ProdPad praised for "connecting the ideas we were working on with the related customer feedback" ([prodpad.com](https://www.prodpad.com/prodpad-vs-productboard/)); Linear praised because "structured issues … create the conditions where AI can be useful instead of noisy" ([tooljunction.io](https://www.tooljunction.io/ai-tools/linear-app)).

**Skeptics (the bar anvil must clear).** HN dismisses the markdown-memory crowd: "nothing works better than simply keeping my own library of markdown files"; "there's never any evidence or even attempt at measuring any metric"; "this is just prompts" ([HN 46426624](https://news.ycombinator.com/item?id=46426624), [HN 47486287](https://news.ycombinator.com/item?id=47486287)). The 2026 "meeting-notes-to-backlog" wave ([StoriesOnBoard](https://storiesonboard.com/blog/meeting-notes-to-product-backlog-ai)) is mostly transcription + clustering with humans in review gates.

**The Superpowers read.** Superpowers (obra) is the sharpest near-miss in the ideation lane and exactly clarifies the wedge. Its brainstorming skill refines **one** idea via one-question-at-a-time Socratic dialogue into **one** approved design doc, then hands to writing-plans for immediate execution. It is **feature-linear with a hard gate** forbidding any other action until that single design is approved, and the repo has **no skill that persists, dedups, prioritizes, or grooms a list of ideas across sessions**. It is a per-feature funnel, not a portfolio/backlog manager — which is precisely the loop anvil needs (continuous ingestion of many ideas, scoring/dedup against an existing queue, cross-session awareness) and which Superpowers, by design, does not attempt.

**Net takeaway.** The complaint is real, loud, and dual-audience (developers + product/eng leaders), and it is specifically a **backlog/roadmap-durability** gap, not a generic memory gap. The market answered with dozens of tools — itself the strongest demand evidence — but the overwhelming majority are thin markdown/text stores that lean on humans for prioritization and ship **no metric**. The defensible wedge is a backlog the agent **reads from AND writes back to**, with real prioritization, dependency/conflict awareness, durability across sessions, and a benchmark proving it beats "a folder of markdown files."

---

## 4. What actually holds (verification pass)

The verification pass **confirmed** the genuine-backlog-manager claims for Productboard+Pulse, Aha!, JPD, claude-task-master, Spec Kit's intra-feature decomposition, Savio's no-AI manual flow, Notion AI's honestly-scoped "AI assists, human prioritizes," Dovetail's insight boundary, and eve's true-negative. It **knocked down or materially corrected** the following:

- **ClickUp Brain — claim FAILS.** "Brain extracts and ranks → tasks with AI-suggested priority/impact/risk → Sprint Overviews" is disjoint and human-orchestrated, **not** one end-to-end managed flow: "Prioritize with AI" only re-ranks *existing* tasks via prompts you hand-write (no native impact/risk scoring), Brain only extracts themes from feedback already pasted in (**no native help-desk ingestion**), and "Sprint Overviews" isn't the real feature name (it's AI Stand-ups). "Manage the ongoing backlog" is marketing-overstated ([eesel.ai](https://www.eesel.ai/blog/clickup-brain)).
- **Linear — "with priority" is overstated.** Triage Intelligence dedups and routes (team/project/assignee/label) but **does not infer priority** — priority is absent from its predicted properties; "manages the backlog" means it surfaces volume/revenue views for a human to order ([linear.app/docs/triage-intelligence](https://linear.app/docs/triage-intelligence)).
- **Cycle — claim partially false + product dead.** "Autopilot extracts verbatim quotes via *learned conventions*" is unsubstantiated embellishment (the real Autopilot is Canny's summarize/dedupe with manual board config); the prioritized roadmap actually lives in Productboard, not "Jira/Linear items" — that mapping is **inverted**. And Cycle is sunset.
- **JPD — marketing-vs-shipped.** Verified: **no AI prioritization, no idea ranking, no auto-linking**; prioritization is human impact ratings + formula fields; agentic roadmapping is "exploring," not GA.
- **Height — the cautionary tale.** The only product that *marketed* continuous autonomous whole-backlog grooming + dedup **shut down 24 Sep 2025** ([alternativeto.net](https://alternativeto.net/news/2025/3/height-project-management-tool-to-shut-down-by-september-2025/), [creativerly.com](https://www.creativerly.com/height-app-is-shutting-down/), [HN 43454034](https://news.ycombinator.com/item?id=43454034)). Treat "the backlog grooms/ranks itself" as the least-shipped claim in the entire landscape.
- **GitHub "AI issue triage" — not a product.** It is a DIY Copilot-SDK example (IssueCrush), single-issue, explicitly **not** dedup/whole-backlog aware.

Pattern: across every lane the recurring failure is the **verification gap** (Productboard links stay "unverified"; Cycle/Saner.AI are "confirm-when-needed") and the **marketing-vs-shipped gap** (JPD, ClickUp, GitHub triage, Height). No tool autonomously re-grooms, merges duplicates across sessions, or proactively re-ranks a standing backlog — that is the universal, verified white space.

---

## 5. The gap anvil uniquely fills

Six gaps converge, and anvil's existing moat maps onto each. (Schema verified in `bin/src/anvil/state/schema.py`: `projects → prds → requirements → features → tasks → claims → evidence → decisions → events`, `PRAGMA user_version = 5`; deterministic engine in `bin/src/anvil/planning/scoring.py` with `blast_radius`.)

1. **Whole-backlog blindness → cross-session awareness as the *core product*, not a memory add-on.** `anvil next` (and the MCP `get_next_task`) already surfaces the right ready *task* by dependency + score across sessions. Lifting that to the whole backlog — the next ready *item* to research/groom/author/promote — means the agent reads from AND writes back to a durable prioritized backlog. This is exactly the wedge Codex Memories *explicitly disclaims*, Devin's Knowledge Base resets, and Cursor *removed*.
2. **Ungoverned ideas → recorded transitions, not mutable rows.** ProdPad/Productboard/Aha! keep a human in every loop and their backlog is mutable rows you drag. anvil's `events` table makes every state change an **additive, append-only ledger** entry (actor/action/target/seq). No competitor's backlog is an immutable audit log.
3. **Advisory suggestion → evidence-gated promotion.** anvil already gates task *completion* on submitted `evidence` and makes accepted work immutable. Extend that primitive *upstream*: an idea cannot become a `ready` item until it passes a **Definition-of-Ready** gate. This is the governed-gate moat applied to grooming — structurally impossible for tools whose links stay "unverified."
4. **Dropped provenance → end-to-end traceability already in the schema.** task-master [#864](https://github.com/eyaltoledano/claude-task-master/discussions/864) dropped ~80% of a PRD and generated subtasks from the parent only. anvil already carries `requirements→features→tasks→evidence` with `related_tasks`/`related_features` on `decisions`; adding the upstream `backlog_item` node uniquely closes **insight → item → requirement → task → evidence → accepted**, queryable as one chain. Nobody else can answer "which shipped commit traces back to which originating friction signal."
5. **Two-store drift → native markdown↔state-DB duality.** anvil already round-trips `.anvil/prd.md` ↔ parsed state and projects state→GitHub Issues (E8) and Mermaid. The backlog markdown becomes the **same** human-editable face over governed state — the bridge competitors lack because their two stores are owned by different products.
6. **Unproven prioritization → deterministic, explainable scoring + the only corpus for RCF.** The six-dimension rule-based engine (`scoring.py`, blast_radius × uncertainty) computes the score with pure heuristics and uses an LLM only for *explanation*, never the number. anvil is the natural — and arguably only — home for **reference-class forecasting**: score effort against the distribution of *past accepted items in the immutable ledger*, a corpus only anvil accumulates.

**Positioning guardrail:** the wedge is "a durable, prioritized backlog of *remaining work* the agent reads AND writes," **not** generic "persistent memory" (crowded, commoditized — it would look like one more MCP memory tool). Benchmark against the bar of *actively generating / prioritizing / sequencing a governed item graph*, with a metric, because HN explicitly distrusts unmeasured claims.

---

## 6. Proposed feature — backlog as a first-class, governed, AI-tooled surface

**Thesis.** Make anvil aware of the **whole** backlog (not just parsed tasks) by adding a governed `backlog_item` node *above* requirements, plus a reusable ideation→item loop that tools it. anvil stays the **governed substrate UNDER** a human/agent loop — it does **not** reimplement feedback aggregation (Productboard/Dovetail) or autonomous auto-merge (Height, which died). Dedup/re-rank ship as **evidence-backed suggestions** a human or gate accepts, never silent mutations.

**The ideation→item loop (distilled from our actual process).** A reusable, repeatable encoding of what we did by hand for E9/E10:
1. **Capture** — a trigger (friction signal or user probe) opens a `backlog_item` with **provenance** (what surfaced it, where), at status `captured`. One command, cheap.
2. **Research fan-out** — parallel agents gather two evidence kinds: codebase (file:line) and external (competitor/precedent/demand), each returning a concise brief.
3. **Synthesize → single insight** — the orchestrator distills briefs into one named gap/root-cause/opportunity with evidence attached.
4. **Structured Q&A refinement** — for direction forks, multiple-choice questions each with a recommended option pin intent and the trade-off cut (reuse the existing `resolve-decisions` machinery at the *idea* stage, not the PRD stage).
5. **Author the item** in the fixed DoR shape: rationale, an *Implementation trade-offs* block (2–3 options, each cost + recommendation), file:line targets, acceptance criteria, Priority/Effort/Type.
6. **Optional ingest into state** — parse item→requirements→features→tasks with dependency ordering and eval/gating where it matters, so a future session resumes via `anvil next`.
7. **Governance** — one PR per item, gated on CI + Greptile + Copilot; deferred findings → `docs/tech-debt-backlog.md`.

**How anvil becomes aware of the whole backlog.**
- A new **`backlog_items` table** with lifecycle `captured → researched → drafted → ready → promoted → shipped/dropped`, each row carrying provenance (trigger + evidence links), the fixed DoR shape, the six-dimension score, and dedup/conflict links. **Every state change appends to `events`; accepted items are immutable** — the same governance already protecting tasks.
- `docs/backlog/anvil-backlog.md` becomes a **projection** of this table (the markdown↔DB bridge below), so the human file and the executable queue never drift.

**Concrete tooling (commands / skills / MCP).**
- `anvil backlog capture --from-friction "<signal>" | --probe "<directive>"` — the cheap front door; records provenance, status `captured`. *(Must stay one-command-light so nobody routes around anvil back to a scratch file.)*
- `/anvil:ideate <item-id>` skill — drives the full loop: research fan-out (codebase file:line + WebSearch) → synthesize one insight → structured Q&A via the `resolve-decisions` machinery → author the fixed-shape item through the **DoR gate**. Reuses the `deep-research` and resolve-loop patterns already in this repo.
- `anvil backlog next [--phase research|groom|author|promote] [--json]` — extends the cross-session `next` primitive to the *whole* backlog, returning the next item to act on by score+status+dependency. `--json` mirrors E4's machine-readable surface for non-Claude hosts.
- `anvil backlog promote <id>` — **governed bridge**: emit a PRD fragment from a `ready` item, run parse→plan→score so its requirements/features/tasks land in state with the insight→item→task chain intact. **Refuses** on an item that hasn't passed the DoR gate.
- `anvil backlog sync [--check]` — bidirectional markdown↔DB round-trip for `anvil-backlog.md` (epics/B-items ↔ rows), reusing the PRD round-trip and GitHub-projection plumbing; `--check` fails CI on drift. **Conflict-surfacing, not last-writer-wins.**
- `anvil backlog dedup` / `anvil backlog rerank` — produce **evidence-backed** similarity and priority suggestions (six-dimension score + reference-class effort from the ledger's past accepted items) as appended `decisions` rows a human/gate accepts — never silent auto-merge.
- **DoR gate in the engine** (extends the evidence-gate code path) — an item cannot transition to `ready` without rationale + ≥2 trade-off options + acceptance criteria + file targets; enforced in-engine and recorded as a transition.
- **MCP additions** (`backlog_capture`, `backlog_next`, `backlog_promote`) so any harness — Codex, eve, CI — can drive the loop, consistent with E10 breadth-as-proof.

**How it bridges markdown ↔ state.db.** The DB is the system of record; `anvil-backlog.md` is a generated, human-editable *projection*. `anvil backlog sync` round-trips epics/B-items ↔ `backlog_items` rows using the exact PRD-round-trip + GitHub-projection plumbing that already works, with `--check` as a CI drift gate and explicit conflict surfacing — so the two stores that every competitor lets drift stay in lockstep, and the markdown stays the friendly face while governance lives in the ledger.

---

## 7. Risks / open questions

1. **Scope creep into the PM-tool graveyard.** Height built autonomous whole-backlog grooming and **died**. anvil must not own feedback aggregation, auto-merge, or "the backlog ranks itself." Dedup/re-rank ship as `decisions` rows, never silent mutations. Cross this line and anvil becomes one more unmonetizable PM platform.
2. **Schema/migration risk.** A `backlog_item` node *above* requirements (today PRD is the root, `user_version=5`) is a real schema change. Must be **purely additive** (ALTER ADD with defaults, like the v5 `task_type` backfill) and migration-safe per the version-bump-in-lockstep rule, or it breaks every existing project.
3. **Markdown↔DB drift as a new failure class.** A sync that silently loses hand-authored trade-off prose or file:line targets would *reproduce the exact task-master #864 complaint* anvil positions against. Needs `--check`/CI gate + conflict surfacing, not last-writer-wins.
4. **DoR-gate friction.** Too strict, and capturing a quick friction signal becomes heavyweight — people route back to a markdown scratch file (the CLAUDE.md/memory-bank behavior we're displacing). Keep `capture` one-command-cheap; apply the gate **only** at promote-to-`ready`.
5. **RCF needs a corpus.** Reference-class effort forecasting only works once enough items have shipped through the immutable ledger. Early on it has no priors — must **fall back to the deterministic six-dimension heuristic, clearly labeled**, not fabricate confidence.
6. **Over-claiming the cross-session moat.** Position and benchmark against *governed item-graph generation/prioritization/sequencing with a metric* — not against the crowded "persistent memory" bar, where HN distrust is highest. **Open question:** what is the benchmark? (Candidate: a corpus where anvil's generated/groomed backlog measurably beats "a folder of markdown files" on traceability completeness, dedup precision, and re-prioritization stability — the SL-2 critic-harness pattern is a precedent.)
7. **Open product questions.** (a) Is `backlog_item` a true new root, or does it attach beside `prds` under `projects`? (b) Does `promote` create one PRD fragment per item or batch related items? (c) How are dedup links represented (self-referential FK vs. `conflict_groups` reuse)? (d) What's the minimum provenance schema so "which commit traces to which friction signal" is queryable end-to-end?

---

## 8. Sources

Landscape / capability:
- https://support.productboard.com/hc/en-us/articles/26949590820627-Link-insights-automatically-with-Productboard-AI
- https://www.productboard.com/blog/productboard-ai-2/
- https://www.globenewswire.com/news-release/2024/10/29/2970872/0/en/Productboard-Launches-AI-Powered-Productboard-Pulse-to-Integrate-Voice-of-Customer-into-Product-Decisions-at-Scale.html
- https://onehorizon.ai/blog/productboard-in-depth-review
- https://support.aha.io/aha-software/ai-assistant/ai-prompt-library/ai-agents/feature-prioritization
- https://www.aha.io/blog/expand-customer-feedback-collection-from-sales-and-support-tools
- https://www.aha.io/roadmaps/prioritization
- https://www.aha.io/blog/just-launched-identify-duplicate-customer-ideas-with-ai
- https://support.atlassian.com/jira-product-discovery/docs/explore-atlassian-intelligence-in-jira-product-discovery/
- https://www.atlassian.com/software/jira/product-discovery/guides/insights/overview
- https://www.atlassian.com/software/jira/product-discovery/guides/fields/overview
- https://www.atlassian.com/blog/company-news/introducing-product-collection
- https://linear.app/docs/intercom
- https://linear.app/now/how-we-built-triage-intelligence
- https://linear.app/docs/triage-intelligence
- https://linear.app/docs/customer-requests
- https://help.clickup.com/hc/en-us/articles/38334064769687-Automatically-prioritize-tasks-using-AI
- https://www.eesel.ai/blog/clickup-brain
- https://workmanagementhub.com/clickup-brain-ai-complete-guide-2026/
- https://www.notion.com/help/autofill
- https://www.notion.com/use-case/project-management/product-backlog
- https://www.eesel.ai/blog/notion-ai-autofill
- https://dovetail.com/blog/dovetail-launches-customer-intelligence-platform/
- https://docs.dovetail.com/integrations/productboard
- https://www.productboard.com/integrations/jira-better-together/
- https://support.productboard.com/hc/en-us/articles/360056354514-Link-user-feedback-to-related-feature-ideas-using-insights
- https://www.productboard.com/integrations/jira/
- https://help.canny.io/en/articles/8202451-autopilot
- https://canny.io/features/autopilot
- https://www.savio.io/how-savio-works/
- https://www.savio.io/features/see-and-prioritize-top-feature-requests/
- https://github.com/github/spec-kit
- https://raw.githubusercontent.com/github/spec-kit/main/templates/tasks-template.md
- https://raw.githubusercontent.com/github/spec-kit/main/spec-driven.md
- https://github.com/eyaltoledano/claude-task-master/blob/main/docs/command-reference.md
- https://github.com/eyaltoledano/claude-task-master/blob/main/docs/tutorial.md
- https://github.com/eyaltoledano/claude-task-master/blob/main/docs/task-structure.md
- https://github.com/eyaltoledano/claude-task-master/discussions/864
- https://vercel.com/docs/eve/concepts
- https://vercel.com/kb/guide/how-to-use-eve-subagents
- https://github.com/vercel/eve/blob/main/README.md
- https://www.prodpad.com/prodpad-vs-productboard/
- https://www.tooljunction.io/ai-tools/linear-app

Demand / sentiment:
- https://news.ycombinator.com/item?id=44483530
- https://news.ycombinator.com/item?id=46426624
- https://news.ycombinator.com/item?id=47486287
- https://news.ycombinator.com/item?id=44560662
- https://news.ycombinator.com/item?id=29726787
- https://forum.cursor.com/t/agent-stuck-referencing-stale-completed-plans/160672
- https://forum.cursor.com/t/cursor-does-not-use-a-to-do-list/144227
- https://forum.cursor.com/t/persistent-memory-for-cursor-that-survives-every-session-brain-folder-approach/157488
- https://forum.cursor.com/t/custom-modes-and-memories-gone-in-2-1/143744
- https://github.com/anthropics/claude-code/issues/2954
- https://github.com/openai/codex/discussions/12567
- https://developers.openai.com/codex/memories
- https://vectorize.io/articles/do-ai-agents-learn-between-sessions
- https://www.augmentcode.com/blog/what-spec-driven-development-gets-wrong (vendor)
- https://www.mindstudio.ai/blog/ai-agents-infinite-backlog-5-new-organizational-roles (vendor)
- https://www.allstacks.com/blog/roadmap-slipping-ai-coding-tools-spec-problem (vendor)
- https://storiesonboard.com/blog/meeting-notes-to-product-backlog-ai (vendor)
- https://artmnk.substack.com/p/how-to-vibe-code-as-a-professional
- https://dev.to/sean8/memento-give-claude-code-persistent-memory-so-you-stop-repeating-yourself-22je
- https://emelia.io/hub/claude-task-master-ai-project-management
- https://www.taskmaster.one/ (vendor)
- https://www.g2.com/products/granola/reviews
- https://zackproser.com/blog/granola-ai-review
- https://www.producthunt.com/products/saner-ai/reviews
- https://www.saner.ai/blogs/best-ai-for-brain-dump (vendor)
- https://blog.mylifenote.ai/ai-productivity-stack-2026/

Height shutdown (cautionary tale): https://alternativeto.net/news/2025/3/height-project-management-tool-to-shut-down-by-september-2025/ · https://www.creativerly.com/height-app-is-shutting-down/ · https://news.ycombinator.com/item?id=43454034

*(Vendor-authored sources validate demand but should not be cited as neutral proof of capability. Cycle's acquisition/sunset is reported in the landscape inputs without a standalone URL.)*
