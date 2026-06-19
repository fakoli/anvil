# SL-7 — Workflow adapter spike (`anvil workflow-step` + projection posture)

**Date:** 2026-06-19
**Status:** Draft — needs approval before implementation
**Plugin:** `anvil`
**Tracks:** roadmap integrity-track `SL-7` (Wave 3: "earn the reframe")
**Depends on:** SL-3 (typed `CommandProof` — the governed step captures one). SL-3 should ship first; the spike can stub the proof type if it does not.

> **⚠️ THIS IS A SPIKE, NOT A PRODUCT.** The goal is a *recorded run* that proves
> two postures are mechanically possible, not a hardened, configurable, generally
> available feature. Everything here is built to be **thrown away or rewritten**
> once it has answered its question. No version bump, no `registry/` entry, no
> stability promise. It lives behind a hidden/experimental command. Acceptance is
> a recorded artifact (§4), not a passing production gate.

---

## 1. Goal

Demonstrate, with one worked example each, that `anvil`'s governed lifecycle can
wrap an *external* unit of work, and that an external dynamic-workflow script's
otherwise-discarded intermediate state can be persisted as canonical `Evidence` /
`Decision` rows.

Two postures (the roadmap's "postures 2 and 3", `docs/roadmap.md:150-155`):

- **Posture 2 — the governed step.** A single CLI wrapper,
  `anvil workflow-step`, that runs *one* external command (or script) through the
  full `anvil` lifecycle: **claim → run → capture typed proof → submit → apply**,
  with the SL-3 evidence gate enforced. The external tool gets `anvil`'s
  exclusivity (claim lease), audit (event log), and proof gate "for free" without
  knowing `anvil` exists.
- **Posture 3 — the projection.** A dynamic-workflow script (the kind that
  computes a chain of intermediate script-variables and then throws all but the
  final answer away) is instrumented so each intermediate script-variable is
  persisted as an `Evidence` or `Decision` row. After the session ends, that
  discarded intermediate state is queryable in `events.jsonl`.

The single acceptance bar: **a recorded run where a workflow script's discarded
intermediate state is queryable in `events.jsonl` after the session ends.**

## 2. Context & root cause

`anvil`'s lifecycle today assumes a *Claude agent* is the actor: it claims a task
(`task_ready_to_claimed`, `transitions.py:442-467`), works, submits evidence
(`evidence.submitted`, `cli/packet_apply.py:372-380`), and a reviewer applies
(`task.applied`). Each step is a separate CLI/MCP call an agent makes by hand.

External, deterministic, dynamic workflows — shell pipelines, Makefile targets,
data-processing scripts, an orchestration DAG — do not speak that protocol. They
run start-to-finish in one process and keep their working state in local
variables that vanish when the process exits. None of it reaches `events.jsonl`,
so:

- there is no exclusivity (two runs of the same workflow can collide);
- there is no proof gate (the workflow's success is never checked against a typed
  `CommandProof`);
- the workflow's intermediate reasoning — the `Decision`-shaped "I chose strategy
  X because metric Y was Z" facts — is lost, even though it is exactly the
  audit/context material `anvil` exists to keep.

The root question the spike answers: *can the governed lifecycle be driven by a
thin wrapper around an arbitrary command, and can a script's transient variables
be projected into canonical state, without changing the engine's contracts?*

## 3. Proposed design (spike)

### 3.1 Posture 2 — `anvil workflow-step` (the governed-step wrapper)

A new hidden CLI command (experimental; not registered in help). It composes
existing, already-shipped seams — it adds **no new engine method**:

```
anvil workflow-step T042 \
    --actor ci-runner \
    --run "pytest -q && ruff check ." \
    [--expected-files src/foo.py] \
    [--proof-command "pytest -q"]
```

Sequence (each line is an existing call site):

1. **Claim.** Claim `T042` for `--actor` with `--expected-files` — the same path
   `anvil claim` uses (`ClaimManager.claim`, reached via `mcp_server.py:757`).
   The lease gives the workflow exclusivity. If the task is unclaimable (PRD not
   reviewed — `_can_claim_task`, `transitions.py:121-137`), the wrapper fails
   loudly *before* running anything.
2. **Run.** Execute `--run` in a subprocess, capturing `stdout`, `stderr`, and
   the real `exit_code`. This is the same observation
   `hooks/capture-evidence.sh` makes (`capture-evidence.sh:95-104`) — the wrapper
   does it inline instead of via the PostToolUse hook.
3. **Capture typed proof.** Build a `CommandProof` (SL-3): `command=--proof-command
   or --run`, `exit_code=<observed>`, `output_sha256=sha256(stdout+stderr)`,
   `captured_at=now`. (Pre-SL-3, the spike may stub this as the legacy
   `commands_run` string — but the spike's *point* is to land on the typed
   proof, so SL-3-first is strongly preferred.)
4. **Submit.** Append an `evidence.submitted` event carrying the proof
   (`EvidenceSubmittedPayload`, `payloads.py:309-324`; with SL-3's `proofs`
   field). This auto-releases the claim and moves `T042 → needs_review`
   (`task_in_progress_to_needs_review`, `transitions.py:532-553`).
5. **Apply (gate enforced).** Run the evidence gate
   (`review.gates.evidence_complete`, the SL-3 typed predicate). If it passes,
   `apply --approve` → `accepted → done` (`task_needs_review_to_accepted`,
   `transitions.py:556-586`, which calls `_evidence_complete`,
   `transitions.py:164-207`). **If the gate fails, the wrapper exits non-zero and
   the task stays `needs_review`** — the external workflow is now governed by the
   same non-gameable gate as a Claude agent. This is the load-bearing claim of
   posture 2.

The wrapper is ~one Typer command in a new `cli/workflow.py`, calling the claim /
submit / apply functions that `cli/claim.py` and `cli/packet_apply.py` already
expose. No engine change.

### 3.2 Posture 3 — the projection (`anvil decide` / `anvil note-evidence`)

A dynamic-workflow script computes intermediate script-variables and discards
them. Posture 3 instruments the script to *project* each one into canonical
state as it is computed. Two tiny, additive CLI surfaces — both thin wrappers
over existing event appends:

- `anvil note-evidence` — appends an `evidence.submitted`-shaped record (or, for
  the spike, a lighter `progress.noted` event — `ProgressNotedPayload`,
  `payloads.py:361`) capturing an intermediate value: `{"variable": "candidate_k",
  "value": 7, "step": "grid_search"}`.
- `anvil decide` — appends a `Decision` row (`Decision` model,
  `models.py:443-460`; there is no `decision.recorded` event action yet, so the
  spike adds a minimal `decision.recorded` action following SL1-RR-1's
  `_check_*`/`_write_*` dispatch contract). Captures "I chose X because Y":
  `title`, `context`, `decision`, `consequences`, `related_tasks=[T042]`.

The script calls these at each interesting intermediate point. The values that
would have vanished when the process exited are now `events.jsonl` facts,
queryable by `anvil describe` / `find-decisions`
(`mcp__plugin_fakoli-state__find_decisions`-equivalent) after the session ends.

### 3.3 What the spike deliberately does NOT build

- No long-running daemon, scheduler, or DAG engine — `anvil workflow-step` runs
  one step and exits (consistent with SL1-RR-1 §6 "not a background process").
- No retry / resume / partial-failure recovery.
- No generic adapter SDK or plugin API — two concrete commands, one worked
  example, then evaluate.
- No config surface beyond flags.

## 4. Acceptance (recorded artifact, not a green gate)

A committed recorded run under `docs/spikes/sl7/` (or an asciinema/transcript)
showing:

1. `anvil workflow-step T042 --run "<cmd that passes>"` driving
   `T042` from `ready` → `done` with a `CommandProof` in its evidence and the
   evidence gate enforced (a second run with a *failing* `--run` leaves the task
   at `needs_review` and exits non-zero — proving the gate bites).
2. A dynamic-workflow example script (one file) whose intermediate
   script-variables are projected via `anvil decide` / `anvil note-evidence`,
   such that **after the script process exits**, those intermediate values are
   recoverable by reading `events.jsonl` (shown via `anvil describe` or a raw
   `grep` of the log). This is the literal roadmap acceptance line
   (`docs/roadmap.md:154-155`).
3. A short written finding: did the existing seams compose cleanly, or did the
   spike reveal a missing engine primitive? (This is the spike's real output.)

## 5. Worked example (posture 3)

A "pick the cheapest provider" dynamic workflow. Without `anvil`, its
intermediate scoring vanishes; with the projection, every intermediate decision
is logged.

```bash
#!/usr/bin/env bash
# docs/spikes/sl7/pick-provider.sh — a dynamic workflow whose intermediate
# script-variables are projected into anvil canonical state.
set -euo pipefail
TASK=T042
ACTOR=provider-picker

# Governed step wraps the whole script run for exclusivity + a final proof.
anvil claim "$TASK" --actor "$ACTOR" --expected-files config/providers.yaml >/dev/null

best_provider=""
best_cost=999999
for p in openai anthropic local; do
    cost=$(python3 score_provider.py "$p")          # <- intermediate script-variable
    # Project the intermediate value (would otherwise be discarded):
    anvil note-evidence "$TASK" --actor "$ACTOR" \
        --variable "cost_$p" --value "$cost" --step "scoring" >/dev/null
    if [ "$cost" -lt "$best_cost" ]; then
        best_cost=$cost; best_provider=$p
    fi
done

# Project the decision (the WHY, not just the final value):
anvil decide --actor "$ACTOR" --related-task "$TASK" \
    --title "Selected provider: $best_provider" \
    --context "Scored openai/anthropic/local by score_provider.py" \
    --decision "Use $best_provider (cost=$best_cost)" \
    --consequences "Other providers' scores recorded as Evidence for audit" >/dev/null

# Capture the final typed proof + submit + apply (the governed close-out):
anvil workflow-step "$TASK" --actor "$ACTOR" \
    --run "python3 apply_provider.py $best_provider" \
    --proof-command "python3 apply_provider.py $best_provider"
```

After this script exits, the per-provider `cost_*` intermediates and the
selection rationale are facts in `events.jsonl`:

```
$ anvil describe T042   # or: grep '"action":"decision.recorded"' .anvil/events.jsonl
... progress.noted   cost_openai=...   step=scoring
... progress.noted   cost_anthropic=... step=scoring
... progress.noted   cost_local=...    step=scoring
... decision.recorded "Selected provider: local" ...
... evidence.submitted  CommandProof(apply_provider.py local, exit=0, sha256=...)
... task.applied  T042 accepted -> done
```

The discarded intermediate state is queryable post-session — acceptance met.

## 6. Risks (spike-appropriate — surface, don't solve)

- **Engine-primitive gap.** The spike may discover that `decision.recorded`
  (posture 3's WHY-projection) needs to be a first-class event action rather than
  a spike stub. Recording *that finding* is a valid spike outcome; the spike adds
  the minimal action and flags it for a real spec.
- **Gate strictness vs. external tools.** An external workflow that legitimately
  exits non-zero on a soft-failure (e.g. a linter warning) will be blocked by the
  enforced gate. The spike notes this tension; production would need
  per-step `passing_exit_codes` (SL-3's `ProofRequirement.passing_exit_codes`).
- **Throwaway code rotting into product.** The single biggest spike risk. Mitigated
  by: hidden command, `docs/spikes/sl7/` location, no version bump, no registry
  entry, and an explicit "rewrite before productionizing" note in the finding.
- **Claim/lease semantics under a long script.** A multi-minute script holds a
  lease that may expire (`lease_expires_at`, `models.py:392`); the spike either
  renews (`anvil renew`, `transitions` heartbeat) or notes the gap. Not solved
  here.

## 7. Implementation steps (spike)

1. Add `cli/workflow.py` with a hidden `workflow-step` command composing the
   existing claim / run-subprocess / submit / apply calls. No engine change.
2. Add `anvil note-evidence` (thin wrapper over `progress.noted` /
   `evidence.submitted` append) and `anvil decide` (minimal `decision.recorded`
   action via the SL1-RR-1 dispatch contract).
3. Write `docs/spikes/sl7/pick-provider.sh` + the two trivial helper scripts.
4. Record the run (transcript / asciinema) into `docs/spikes/sl7/`.
5. Write the finding doc: what composed, what was missing, what a real
   product spec would need.

## 8. Test plan (spike — minimal, illustrative)

| Test | Asserts |
|---|---|
| **Governed pass** | `workflow-step` with a passing `--run` drives `ready → done`; evidence carries a `CommandProof` |
| **Gate bites** | `workflow-step` with a failing `--run` leaves the task at `needs_review`, exits non-zero |
| **Projection survives exit** | After the example script's process exits, `cost_*` intermediates + the `decision.recorded` row are present in `events.jsonl` |
| **Replay (P4)** | The recorded run's `events.jsonl` replays without error (the projection events are ordinary additive facts) |

No CI gate is added for the spike beyond the existing suite continuing to pass —
the spike must not regress production paths.

## 9. Out of scope

- Productionizing either posture (a real spec follows the spike's finding).
- DAG/orchestration engines, retries, resumption, scheduling.
- A generic external-tool adapter SDK.
- Any version bump, `registry/` entry, or marketplace surface.

## 10. References

- `docs/roadmap.md:150-155` (SL-7 postures 2 and 3, acceptance line)
- `bin/src/anvil/state/transitions.py:121-137` (`_can_claim_task`),
  `:442-467` (claim), `:532-586` (submit → needs_review → accepted),
  `:164-207` (`_evidence_complete`)
- `bin/src/anvil/cli/packet_apply.py:372-417` (submit/evidence path)
- `bin/src/anvil/cli/claim.py`; `bin/src/anvil/mcp_server.py:757` (claim seam)
- `hooks/capture-evidence.sh:95-104` (the observation the wrapper inlines)
- `bin/src/anvil/state/models.py:392` (`Claim.lease_expires_at`),
  `:443-460` (`Decision`)
- `bin/src/anvil/state/payloads.py:309-324` (`EvidenceSubmittedPayload`),
  `:361` (`ProgressNotedPayload`)
- `docs/specs/2026-06-19-sl3-proofartifact.md` (`CommandProof`,
  `ProofRequirement.passing_exit_codes`)
- `docs/specs/2026-06-01-sl1-rr-1-event-sourcing-write-path.md` (dispatch
  `_check_*`/`_write_*` contract for the new `decision.recorded` action)
