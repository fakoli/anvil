# WF-3 format & lifecycle decisions

**Date:** 2026-06-19
**Status:** Locked (gates T002–T006)
**Source spec:** [`docs/specs/2026-06-19-anvil-workflow-substrate.md`](../specs/2026-06-19-anvil-workflow-substrate.md) §11
**Task:** T001 — Lock WF-3 spec-format and lifecycle decisions

This note resolves the five §11 open questions before any runner code is written,
so T002 (schema/parser), T003 (runner), T004 (proof gate), T005 (`fan_out`), and
T006 (concurrency proof) build against a fixed target.

---

## Decision: spec format — minimal YAML, scoped to non-PRD loops

WF-3 authors ad-hoc loops in a small `.anvil/workflows/*.yaml` surface. PRD-markdown
remains the front door for greenfield work. Spec §11 cautions against a second
authoring surface "unless non-PRD loops clearly need it" — WF-3 *is* exactly that
case (the ~75% brownfield/ad-hoc work the PRD front door does not launch), so a
deliberately tiny YAML is justified. The format stays minimal (see the control-flow
ceiling below); it is not a general programming surface.

## Decision: task lifecycle — CREATE ephemeral, run-scoped tasks

A workflow run CREATES and claims its tasks; `fan_out` derives N run-scoped tasks
from a step's typed output (spec §9). The common WF-3 case ("one task per flaky
test") is inherently create-on-the-fly and cannot be expressed by binding to
pre-authored PRD task IDs. Bind-to-existing is deferred — added only if a concrete
non-PRD loop earns it. For WF-1/WF-2 this is moot (they drive the PRD's existing
ready queue).

## Decision: trigger ownership — delegated to the harness

Triggering (cron, schedule, `on:`) is delegated to the harness, not owned by the
spec or a WF-3 daemon. This is already the WF-2 posture (§6: Codex automation
schedule, GitHub Action `on:`) and is consistent with the out-of-scope line (§12:
"a long-running daemon, scheduler, or DAG engine"). `anvil run-workflow` runs steps
and exits; whatever fires it owns the cadence. Spec-declared cron is reconsidered
only if a runtime cannot supply its own schedule.

## Decision: control-flow ceiling — minimal primitives + code escape-hatch

The declarative format supports exactly `run`, `fan_out`, `needs`, and
`on_fail: reopen`. Anything richer (conditionals, general loops, map-reduce DSL)
is out — a step that needs it drops to a code workflow via the escape-hatch. This
holds the line spec §11 and §3.3 / SL1-RR-1 §6 draw: "resist growing a DAG engine."
The ceiling is the test for "just code": if a workflow wants `if/when` or `while`,
it has crossed the line and should be code, not YAML.

## Decision: concurrency acceptance bar — mirror the existing suite

T006 proves `fan_out` stresses the wedge with the same rigor as
[`tests/test_claims_concurrency.py`](../../bin/tests/test_claims_concurrency.py):
N parallel `fan_out` claims over overlapping `expected_files` must yield exactly
one winner per task and zero lost evidence, across ≥200 iterations, ≥8 threads, and
multiple contention shapes. A single-pass smoke test is insufficient — the rare
races this wedge guards against only surface under iteration.

---

## Locked primitive set (for T002's schema)

| Primitive | Purpose |
|---|---|
| `run` | a single governed step (claim → run → capture proof → submit → apply) |
| `fan_out` | derive + create + claim N run-scoped tasks from a step's typed output |
| `needs` | ordering dependency between steps |
| `proof` | typed per-step gate — a passing `CommandProof` must exist (SL-3 `ProofRequirement`) |
| `on_fail: reopen` | per-step loop-until policy |
| `uses_code` | escape-hatch: a step delegates to a code workflow when declarative is insufficient |

Explicitly **excluded**: `if`/`when` conditionals, general `while`/`loop` constructs,
and any map-reduce/DAG DSL. These are the "just code" boundary.
