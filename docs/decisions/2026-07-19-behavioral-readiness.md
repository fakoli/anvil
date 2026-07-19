# Behaviour-first PRD readiness

**Date:** 2026-07-19
**Status:** Accepted
**Scope:** PRD authoring, planning context, and work packets

---

## Context

Anvil turns a PRD into reviewed, lockable work packets and evidence-backed task
outcomes. That makes the quality of the PRD unusually consequential: technical
detail alone is not enough if the intended user, behaviour, boundaries, and
observable outcome are still implicit when design begins.

[Productboard’s PRD versus product-spec comparison](https://www.productboard.com/glossary/prd-vs-product-spec/)
draws a useful distinction: a PRD centers the product problem, goals, scope,
and success, while a product specification describes the implementation in
more technical detail. [OpenSpec’s concepts](https://github.com/Fission-AI/OpenSpec/blob/main/docs/concepts.md)
make the complementary case for behavioural specifications and incremental
change deltas as living context. Both reinforce the need to preserve the
product behaviour before implementation detail proliferates.

## Decision

Strengthen Anvil’s existing PRD as an executable, behaviour-first contract:

```text
PRD intent → user behaviour → technical design → task proof → outcome evidence
```

The engine adds an optional typed `## Assumptions` section and a deterministic,
read-only `prd assess` / `assess_prd` advisory. Findings explain a readiness
signal, identify its PRD location, and suggest one focused challenge question.
They never block a lifecycle transition or silently grant authority.

Assumptions have stable IDs, statements, rationales, and optional requirement
references. They are persisted in parse/revision events, replay safely from old
logs with an empty default, reach the planner as context, and appear in a work
packet only when global or relevant to the task’s feature requirements.

Skills own the optional interaction. In challenge mode they ask the most useful
question one at a time. When a user explicitly delegates an autonomous run,
the skill records only bounded, reversible defaults in `## Assumptions`,
re-parses and re-assesses, then continues with remaining advisories reported.
It still stops for new external authority, a conflict with declared scope or
non-goals, or an inference with no bounded safe default.

## Consequences

- Better traceability from user behaviour to task verification without adding a
  new approval gate.
- Repeatable, testable guidance that can be returned identically by CLI and
  MCP rather than depending on a model’s judgement.
- Autonomous planning remains available, but inferred constraints become
  inspectable context instead of hidden prompt state.
- This release intentionally does not add requirement-to-evidence coverage
  dashboards or post-release outcome measurement.

## Rejected alternatives

### Mandatory interview gates

They would make a drafting aid a hard blocker and remove the useful ability to
delegate the rest of a bounded workflow autonomously. The existing approval and
authority policies are the appropriate gates.

### LLM-only assessment

It would make results non-repeatable and difficult to audit or test. An LLM may
help an opted-in skill phrase a question, but the engine’s baseline assessment
must remain deterministic.

### A second competing specification format

OpenSpec’s living-spec and delta model is valuable context, but cloning it here
would split the source of truth. Anvil keeps PRD markdown as the front door and
adds structured assumptions to the existing contract instead.
