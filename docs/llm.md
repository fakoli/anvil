# anvil LLM augmentation

## What it is

Planning in anvil is deterministic by default: a rule-based PRD parser, a six-dimension
scoring engine, and a subset-overlap dependency inferencer turn `prd.md` into reviewed tasks
without ever calling out to a model. The LLM layer is **strictly additive** — when enabled it
enriches the deterministic output (longer task descriptions, trade-off summaries on score
explanations, sub-task proposals for high-complexity work) but never replaces or overrides
a deterministic value. Every operation succeeds without an API key; the LLM is opt-in.

---

## Configuration

The **default provider is the Claude Agent SDK** over your logged-in Claude
subscription — no API key. If Claude Code is installed and logged in (`claude`
on PATH), `--use-llm` just works. anvil scrubs `ANTHROPIC_API_KEY` /
`CLAUDE_API_KEY` for the duration of the call so a quota-capped key cannot
hijack the run.

To use a metered API key, AWS Bedrock, or a custom OpenAI-compatible endpoint
instead, pin `llm_provider:` in `.anvil/config.yaml` (or set `llm_fallback:
true` to restore env auto-detection). See
[`docs/llm-providers.md`](llm-providers.md) for the full setup and precedence.

Model selection: leave `llm_tier` / `llm_model` blank to use the subscription's
default model on `agent-sdk`, or the `sonnet` tier default on the API
providers. Set `llm_tier` (`opus`/`sonnet`/`haiku`) or an explicit `llm_model`
to pin one.

**Prompt caching** is enabled on the direct-API and Bedrock paths: every
Anthropic-family call sends the system block with
`cache_control: {"type": "ephemeral"}` so repeated runs against the same task
batch hit the 5-minute ephemeral cache. The `agent-sdk` and custom paths do not
set this field (the subscription CLI and OpenAI-compatible servers handle
caching themselves).

---

## Usage

Three CLI commands accept the `--use-llm` flag. The deterministic baseline always runs first;
LLM enrichment is layered on top.

### `anvil plan --use-llm`

Re-parses `prd.md` and emits `feature.created` / `task.created` events as usual. With
`--use-llm`, short task descriptions (under 50 characters) are extended by the LLM after the
deterministic parse. The structural fields (id, dependencies, conflict groups, status
transitions) are never touched by the model.

```text
$ anvil plan --use-llm
Planned 4 features, 17 tasks.
Detected 2 conflict group(s).
```

### `anvil score [TASK_ID] --use-llm`

Computes the six numeric scores deterministically, then asks the LLM for a 1–3 sentence
trade-off summary appended to the rule-based explanation. **The numeric scores themselves
are never modified by the LLM.**

```text
$ anvil score T012 --use-llm
TaskID      Complexity Parallel CtxLoad Blast Review Agent
------------------------------------------------------------
T012                 4        2       3     2      3     4

Scored 1 task(s).
```

`anvil show T012` then displays the appended trade-off paragraph under `Explanation`.

### `anvil expand TASK_ID --use-llm`

Unlike `plan` and `score`, `expand` **requires** `--use-llm` — the deterministic engine
never invents sub-tasks (manual authoring as `T001.1`, `T001.2` blocks in `prd.md` is the
deterministic path). With `--use-llm` and a task of `complexity >= 4`, the LLM proposes
2–5 independently-claimable sub-tasks. The command prints proposals for a human to paste
into `prd.md`; **it does not mutate state.**

```text
$ anvil expand T012 --use-llm
Proposed 3 sub-task(s) for T012. Paste into prd.md as ### TXxx blocks under the same ## Tasks section.

--- Sub-task 1 ---
Title: Extract JWT validation into middleware
Description: ...
Likely files: src/auth/jwt.py, src/auth/middleware.py
Acceptance criteria:
  - All requests with malformed JWT return 401
  - Validation logic is unit-tested in isolation
```

Tasks with `complexity < 4` return no proposals — they are deemed simple enough to ship
as-is.

#### `--format prd` (v1.9.0)

The default `--format text` mode (above) emits human-readable per-subtask
blocks. The new `--format prd` mode emits markdown blocks matching
[`docs/prd-template.md`](prd-template.md) — paste-ready into the
`## Tasks` section of `.anvil/prd.md`:

```text
$ anvil expand T012 --use-llm --format prd
# 3 sub-task block(s) for T012 — paste into the ## Tasks section of .anvil/prd.md:

### T012.1: Extract JWT validation into middleware

**Feature:** F003
**Priority:** high
**Likely files:** src/auth/jwt.py, src/auth/middleware.py

Pull JWT validation out of the route handlers and into a reusable middleware
layer so future routes inherit the guard for free.

**Acceptance criteria:**

- All requests with malformed JWT return 401
- Validation logic is unit-tested in isolation

**Verification:**

- TODO: add verification command
```

The `**Feature:**` and `**Priority:**` fields are populated from the
parent task's metadata (Phase 9 critic CONSIDER fix — eliminates the
manual-edit step in the paste-into-`prd.md` workflow). The
`**Verification:**` line is left as `- TODO: add verification command`
on purpose so `git diff` shows the user where to paste in the real
verification command before `prd parse`.

The emitted blocks round-trip cleanly through `prd parse` — see
`tests/test_cli_plan.py::test_prd_format_output_round_trips_to_prd_parser`
for the canonical proof.

---

## Provider interface

The LLM layer lives behind a single Protocol so callers never import the Anthropic SDK
directly. Power users and contributors swap implementations by injecting a different
provider into the planning engine.

```python
from typing import Protocol

class LLMProvider(Protocol):
    def generate(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> LLMResponse: ...
```

`LLMResponse` is a Pydantic v2 model with `text`, `input_tokens`, `cached_input_tokens`,
`output_tokens`, `model`, and `finish_reason`. All fields are required; non-Anthropic
providers MUST report `cached_input_tokens=0` rather than `None`.

### Injecting a provider in tests

`RecordedLLMProvider` is a deterministic test double. Build a `{key: LLMResponse}` map
where the key is the length-prefixed sha256 over `(system, user, max_tokens, temperature)`,
then inject it into any function that takes a `provider` keyword. On a key miss the
provider raises `LLMProviderError` so the test fails loudly rather than silently hitting
the real API.

The canonical signature (Phase 9 C2):

```python
@classmethod
def record_key(
    cls,
    system: str,
    user: str,
    *,
    max_tokens: int = 4096,
    temperature: float = 0.0,
) -> str: ...
```

```python
from anvil.planning.llm import RecordedLLMProvider, LLMResponse
from anvil.planning.scoring import (
    score_task,
    _SCORE_EXPLAIN_MAX_TOKENS,
)

system = "You are a senior planning assistant..."
user = "Task T012: Implement auth middleware\n..."

# IMPORTANT: pass the same max_tokens the engine will use at lookup
# time, or the key will not match and the test will see
# LLMProviderError("no recording for prompt hash ...").
key = RecordedLLMProvider.record_key(
    system, user, max_tokens=_SCORE_EXPLAIN_MAX_TOKENS,
)
provider = RecordedLLMProvider({
    key: LLMResponse(
        text="Trade-off: middleware is reusable but blast radius is wider.",
        input_tokens=120,
        cached_input_tokens=0,
        output_tokens=18,
        model="claude-sonnet-4-6",
        finish_reason="end_turn",
    ),
})

result = score_task(task, provider=provider)
```

The Phase 7 contract documented `max_tokens` and `temperature` as "accepted
but intentionally ignored"; **Phase 9 C2 reversed that** — tuning args now
participate in the canonical key. Two recordings under different
`max_tokens` or `temperature` no longer collide; tests that pre-compute
keys MUST pass the matching values the engine will use at lookup time.
The engine's per-call-site constants are `_SCORE_EXPLAIN_MAX_TOKENS`
(300), `_DESCRIPTION_ENRICH_MAX_TOKENS` (400), and `_EXPAND_MAX_TOKENS`
(2000) — import them from `planning.scoring` / `planning.template` /
`planning.inference` respectively to keep tests in sync if the constants
ever change.

### Engine entry points

Three functions take a `provider: LLMProvider | None = None` keyword-only argument:

- `planning.scoring.score_task(task, *, provider=None)`
- `planning.scoring.score_all(tasks, *, provider=None)`
- `planning.template.parse_prd(markdown, *, prd_id, provider=None, clock=None)`

A fourth is LLM-only:

- `planning.inference.expand_task(task, *, provider=None) -> list[SubtaskProposal]`

`expand_task` returns `[]` deterministically (no provider, or `complexity < 4`); with a
provider and `complexity >= 4` it asks the LLM for 2–5 sub-task proposals. Malformed JSON
responses fall back to `[]` with a stderr warning.

---

## Failure mode

**Provider not usable.** The default `agent-sdk` provider always *resolves*
(no key required), so `--use-llm` no longer exits 1 for a missing
`ANTHROPIC_API_KEY`. Resolution fails with code 1 only when an explicitly
pinned provider can't be built — e.g. `llm_provider: bedrock` without the
`anthropic[bedrock]` extra, or a `custom` endpoint missing its `base_url` /
model. The message names the fix. If the `claude` CLI is absent at call time,
the `agent-sdk` provider raises a clear `LLMProviderError` telling you to
install/login to Claude Code or pin a different provider.

**Mid-operation LLM error.** If the LLM call fails after the deterministic baseline has
already produced a valid result (network error, rate limit, malformed model response), the
engine **falls back to deterministic-only output** and emits a warning to stderr. The
operation does not abort. This applies to all four engine entry points: a `score` run that
loses the LLM mid-batch still writes every numeric score; an `expand` that errors returns
`[]` with the warning visible on stderr.

`LLMProviderError` is the single exception type to catch in custom callers — it wraps
`anthropic.AnthropicError` and any other SDK / network / lookup failure. The engine's
augmentation sites widen this guard further: any non-conforming custom provider that
raises a different exception type is also caught and logged, so the deterministic
baseline always survives.

**Mid-batch interrupt.** `score --use-llm` and `plan --use-llm` commit per-task events
inside their own `BEGIN IMMEDIATE` transactions, so a SIGINT (Ctrl-C) after 10 of 50
tasks leaves 10 task.scored events durably committed and 40 untouched. The committed
rows reflect whatever the LLM produced at the time (some may have full LLM-augmented
explanations, some may have deterministic-only if the LLM was already failing). Re-run
the command without arguments to resume; tasks that already have explanations are
re-scored idempotently.

---

## Cost notes

- `temperature=0.0` by default — augmentation should be repeatable, not creative.
- Prompt caching makes repeated runs cheap. The system block is the largest, most stable
  part of every call; caching it eliminates the dominant cost on the second and subsequent
  calls within the 5-minute ephemeral window.
- A typical `score --use-llm` run against a 20-task batch hits the cache on tasks 2–20 and
  pays for one cold system block plus 20 small user blocks plus 20 small output blocks.
- `expand` is the heaviest call (sub-task JSON, up to ~800 output tokens) but is invoked
  once per high-complexity task and gated by `complexity >= 4`.

---

## See also

- [`mcp.md`](mcp.md) — MCP server (does not currently expose LLM augmentation; agents that
  want it call the CLI directly).
- [`prd-template.md`](prd-template.md) — the deterministic PRD format the parser expects.
- `specs/2026-05-24-anvil-v0.md` — canonical design spec including the LLM
  augmentation contract.
