# Spec-Driven Development — Research Synthesis

> The merged, cited output of a deep-research pass on the intellectual lineage and
> 2025–2026 convergence of intent-/spec-driven AI development. The automated run
> fanned out 5 search angles → 21 sources → 103 raw claims → 25 adversarially
> verified → **24 confirmed, 1 refuted**; its final synthesis step failed mid-response,
> so this document performs that merge by hand from the verified evidence.
>
> **How to read confidence:** each finding is backed by claims that survived 3-reviewer
> adversarial verification (a claim was killed only if ≥2 voted to refute).
> **HIGH** = all backing claims 3-0. **MEDIUM** = at least one backing claim 2-1.
> Convergence/gap analysis is explicitly labelled *inference* — it reasons *from* the
> confirmed claims rather than being directly quoted.
>
> Compiled 2026-06-17. Companion to [`intent-driven-development-landscape.md`](./intent-driven-development-landscape.md)
> (the strategic map); this file is the evidence base.

---

## Part A — Findings (verified claims, merged & deduped)

Forty-five raw claims collapsed into the findings below; semantic duplicates across
search angles are merged, and the strongest verbatim quote is kept for each.

### A1. The paradigm has deep formal roots — "spec before code" is decades old

**Finding (HIGH).** The "specification/model as primary artifact, code as derived output"
thesis predates LLMs by decades and appears independently in formal methods and
enterprise modelling.

- **Model-Driven Architecture (MDA).** The OMG launched MDA in **2001**. It "treats
  models as the primary development artifact, with implementation code derived through
  automated transformation processes," emphasizing *forward engineering* — "producing
  code from abstract, human-elaborated modeling diagrams (e.g. class diagrams)."
  → *3 claims merged, all 3-0.* [MDA]
- **TLA+.** Leslie Lamport's formal specification language for "designing, modelling,
  documentation, and verification of programs, especially concurrent systems and
  distributed systems." Specs are "written in a formal language of logic and
  mathematics … intended to uncover design flaws before system implementation is
  underway." → *2 claims merged, both 3-0.* [TLA+]

**Why it matters:** the modern wave's "spec is the source of truth / spec before code"
is a re-expression of MDA's transformation model and TLA+'s verify-before-build
discipline — in English, aimed at an LLM instead of a code generator or model checker.

### A2. The natural-language and behavioral ancestors

**Finding (HIGH).** Two further ancestors moved the spec from formal notation toward
*plain language* and *executable behavior* — the two properties the LLM era depends on.

- **Behavior-Driven Development.** Created by Daniel Terhorst-North in the early 2000s
  ("Introducing BDD," 2006) "as a response to test-driven development." The
  `Given/When/Then` template "to capture acceptance criteria in executable form" drew on
  Eric Evans' Domain-Driven Design and Rachel Davies' Connextra user-story template.
  → *2 claims merged, both 3-0.* [BDD]
- **Readme-Driven Development.** Tom Preston-Werner, 2010-08-23: "Write your Readme
  first. First. As in, before you write any code or tests or behaviors or stories or
  ANYTHING." The authority principle: **"A perfect implementation of the wrong
  specification is worthless."** → *2 claims merged, both 3-0.* [RDD]

### A3. Karpathy's "Software 2.0" is the conceptual bridge to LLM-era intent

**Finding (HIGH).** Karpathy (Nov 2017) framed a regime where you "specify some goal on
the behavior of a desirable program … and use the computational resources at our
disposal to search this space for a program that works," and where "the process of
training the neural network compiles the dataset into the binary." This is the cleanest
pre-statement of *intent as source of truth, the artifact as compiled/derived.*
→ *3 claims merged, all 3-0.* [SW2.0]

### A4. GitHub Spec Kit is the flagship — and is, by design, stateless scaffolding

**Finding (HIGH, with one MEDIUM sub-claim).** Spec Kit operationalizes Spec-Driven
Development but carries no durable engine of its own.

- It is a toolkit for SDD in which "specifications become executable, directly
  generating working implementations rather than just guiding them." → *2-1, MEDIUM.* [spec-kit]
- It explicitly inverts the code-centric model: "code has been king — specifications
  were just scaffolding we built and discarded … Spec-Driven Development changes this."
  → *3-0.* [spec-kit]
- It "creates markdown artifacts (constitution.md, spec.md, plan.md, tasks.md) … No
  persistent execution engine; relies on AI agent interpretation of documents." A
  corroborating teardown: the CLI "downloads an existing package … that contains the
  pre-baked prompts … and some Spec Kit-specific metadata and templates."
  → *2 claims merged (spec-kit + den.dev), both 3-0, HIGH.* [spec-kit][den.dev]
- **Lineage:** "heavily influenced by and based on the work and research of John Lam,"
  specifically his research into making LLM development "just a tiny bit more
  deterministic." → *2 claims merged, both 3-0.* [spec-kit][den.dev]
- **Positioning:** an explicit backlash against vibe-coding ("vibe coding into
  production is just a monumentally shortsighted approach"); verification stays
  human-in-the-loop. → *2-1, MEDIUM.* [den.dev]

**Refuted (killed 0-3):** the claim that Spec Kit defines a *fixed eight-command*
workflow where the spec is literally *the executable artifact / compiled specification.*
The command set is not a rigid eight, and code remains agent-generated and
human-reviewed — not compiled from the spec. *This correction matters:* it's the
boundary between Spec Kit's actual (scaffolding) and aspirational (spec-as-source)
positioning.

### A5. The durable-state design exists — and was reached independently

**Finding (HIGH).** Praetorian's "Deterministic AI Orchestration" writeup describes,
from the security-engineering side, the exact machinery the popular tier lacks:

- **Thesis:** "The primary bottleneck in autonomous software development is not model
  intelligence, but context management and architectural determinism." → *3-0.* [praetorian]
- **Durable state:** a dual-state model (ephemeral JSON + persistent YAML) where
  "persistent state … survives session restarts, ensuring the workflow can still be
  resumed from the last checkpoint." → *3-0.* [praetorian]
- **Task leasing:** parallel `developer` agents "utilize a lockfile mechanism
  (`.claude/locks/{agent}.lock`) to prevent race conditions on shared source files."
  → *3-0.* [praetorian]
- **Evidence-based verification:** the system "enforces that code _cannot_ be marked
  complete until independent Reviewer and Tester agents have passed it." → *3-0.* [praetorian]

**Why it matters:** durable state + leasing + evidence is not a fakoli-only idiosyncrasy.
An unrelated team converged on all three. This is the strongest external evidence that
the gap (Part B) is real and that the layer is the defensible one.

### A6. There is a recognized maturity spectrum

**Finding (HIGH).** The `spec-compare` project defines three SDD stages: **Spec-First**
(spec discarded after use), **Spec-Anchored** (spec persists and evolves), and
**Spec-as-Source** (code auto-generates from specs only). → *3-0.* [spec-compare]

This is the cleanest taxonomy for placing every project — but note it measures the
durability of *the spec*, not of *execution state* (claims/evidence/audit), which is a
separate axis (see Part B).

---

## Part B — Synthesis (inference from the confirmed findings)

### B1. Lineage timeline

```
1984  Literate Programming (Knuth)        intent-first authoring        [domain]
1999  TLA+ (Lamport)                       spec verified before code     [V: A1]
2001  Model-Driven Architecture (OMG)      model = source, code derived  [V: A1]
2006  Behavior-Driven Development (North)  executable acceptance specs   [V: A2]
2010  Readme-Driven Development (TPW)       spec-as-authority, in English [V: A2]
~2014 Declarative IaC / Terraform          durable "desired state"       [domain]
2017  Software 2.0 (Karpathy)              intent → searched/compiled    [V: A3]
2025  GitHub Spec Kit, Kiro, Tessl, …      the agentic convergence       [V: A4]
2025  Praetorian / fakoli-state            + durable state, leases, ev.  [V: A5]
```

The 2025 wave is a *recombination* of verified ancestors: MDA's transformation thesis +
RDD's natural language + BDD's executability + (in the serious tier) IaC's durable
state — aimed at an LLM.

### B2. Where the projects land on the verified axes

Sorting by the three capabilities that the evidence treats as the real differentiators:

| Capability | Confirmed to have it | Confirmed / strongly-evidenced to lack it |
|---|---|---|
| Durable canonical **state** | Praetorian (dual-state, resumable) [A5]; fakoli-state (SQLite) | Spec Kit (markdown, no persistent engine) [A4] |
| Exclusive task **leasing** | Praetorian (lockfiles) [A5]; fakoli-state (lease+heartbeat) | Spec Kit and the scaffolding tier (none surfaced) |
| Evidence-based **verification** | Praetorian (Reviewer+Tester gate) [A5]; fakoli-state (evidence contract) | Spec Kit (human-in-the-loop / self-grade) [A4] |

*(Kiro, Tessl, task-master, BMAD, Agent OS, OpenSpec were catalogued from the source
set but did not produce individually-verified architectural claims in this run; the
landscape doc places them from domain knowledge and flags that as lower confidence.)*

### B3. The convergence thesis (inference)

Why so many independent arrivals at "spec/intent as source of truth" in one window? The
confirmed claims point at three forces directly, and two more by reasonable inference:

1. **The bottleneck moved off code generation.** Verified: the bottleneck is "context
   management and architectural determinism, not model intelligence" [A5]. Once the
   model can code, steering becomes scarce — and a spec is the steering handle.
2. **Determinism / governability pressure.** Verified: Spec Kit was born from research
   into making LLM dev "more deterministic" [A4]. A durable spec is the determinism
   handle enterprises require.
3. **Vibe-coding backlash.** Verified (MEDIUM): Spec Kit is explicitly positioned
   against "vibe coding into production" [A4].
4. **Context-window limits** *(inference):* long builds overflow the window, forcing
   state outside the conversation — first the spec, then (in the serious tier)
   execution state.
5. **Multi-agent coordination** *(inference, strongly supported by A5):* the moment >1
   agent runs, you need shared truth + anti-collision — the pressure that forces
   lockfiles and evidence gates.

### B4. The gap (inference, anchored in A4–A6)

The maturity spectrum [A6] measures the spec's durability and stops there. The verified
contrast between Spec Kit [A4] and Praetorian [A5] exposes a *second, orthogonal* axis
the spectrum omits: the durability of **execution state** — who holds which task, what
was actually done, and whether it can be proven later. The popular tier is
spec-durable but execution-ephemeral. The two systems that close the execution axis
(Praetorian, fakoli-state) reached it independently. That independent convergence is
the load-bearing evidence that durable, lease-coordinated, evidence-bearing state is the
defensible layer — not the spec format, which is now commoditized.

---

## Sources

| Tag | Quality | URL |
|-----|---------|-----|
| [RDD] | primary | https://tom.preston-werner.com/2010/08/23/readme-driven-development |
| [SW2.0] | primary | https://karpathy.medium.com/software-2-0-a64152b37c35 |
| [BDD] | primary | https://cucumber.io/docs/bdd/history/ |
| [MDA] | secondary | https://en.wikipedia.org/wiki/Model-driven_architecture |
| [TLA+] | secondary | https://en.wikipedia.org/wiki/TLA+ |
| [spec-kit] | primary | https://github.com/github/spec-kit |
| [den.dev] | primary | https://den.dev/blog/github-spec-kit/ |
| [praetorian] | primary | https://www.praetorian.com/blog/deterministic-ai-orchestration-a-platform-architecture-for-autonomous-development/ |
| [spec-compare] | secondary | https://github.com/cameronsjo/spec-compare |

Catalog / commentary (used for the field guide, not individually claim-verified):
Martin Fowler *SDD with 3 tools*; ThoughtWorks *SDD: unpacking 2025*; Reenbit *BMAD vs
Spec-Kit vs OpenSpec*; Tim Wang *Spec-Kit, BMAD, Agent OS*; Daniliants *Kiro, Spec Kit,
Tessl*; Tessl blog; Marmelab *Waterfall Strikes Back*; Augment Code *multi-agent
production requirements*; BCMS *SDD*; HN threads 45935763 & 47197595.

---

## Run metadata

- Angles: 5 (origins/prior-art · current-wave catalog · state/execution/verification ·
  convergence forces · practitioner critique)
- Sources fetched: 21 · Raw claims: 103 · Verified: 25 · **Confirmed: 24 · Refuted: 1**
- Automated synthesis: **failed** (API connection closed mid-response) → merged by hand
  here from the verified claim set.
