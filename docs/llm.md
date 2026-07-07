# anvil LLM augmentation

> **Audience:** users and operators running `--use-llm`, configuring a
> provider, or reading LLM cost/behavior. For the engineering rationale
> behind the tier defaults, see [`model-strategy.md`](model-strategy.md).

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
true` to restore env auto-detection). See [Providers](#providers) below for
the full setup and precedence.

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

## Providers

anvil's planning features (`--use-llm`, the LLM-driven task-generation
backstop, `expand --use-llm`, `score --use-llm`) can be backed by four
different LLM provider families.

### Provider matrix

| Provider | When to use | Extras | Config key |
| --- | --- | --- | --- |
| **Claude Agent SDK** | **Default.** Rides your Claude *subscription* (no per-token key). anvil is capacity-bound, not per-token-cost bound, so this is the default. | None (`claude-agent-sdk` is a core dep); needs the `claude` CLI on PATH. | `llm_provider: agent-sdk` |
| **Direct Anthropic API** | You want metered per-token billing against an `ANTHROPIC_API_KEY` (CI without a subscription session, etc.). | None (`anthropic` is a core dep). | `llm_provider: anthropic` |
| **Amazon Bedrock** | Your org pins LLM calls to AWS for compliance, billing, or data-residency reasons. | `pip install 'anvil-state[bedrock]'` (adds `anthropic[bedrock]` + boto3). | `llm_provider: bedrock` |
| **Custom OpenAI-compatible** | You're on vLLM, LiteLLM proxy, OpenRouter, Together, Groq, Azure OpenAI, or a self-hosted endpoint that speaks `/v1/chat/completions`. | `pip install 'anvil-state[custom]'` (adds `openai`). | `llm_provider: custom` |

### Precedence — who picks the provider

`anvil plan` (and every other LLM-touching CLI / MCP tool) picks **exactly one** provider per process:

1. **Explicit `llm_provider` in `.anvil/config.yaml`** — always wins (`agent-sdk` / `anthropic` / `bedrock` / `custom`).
2. **Default → `agent-sdk`.** With no explicit provider, anvil uses the Claude Agent SDK over the subscription. It does **not** consult `ANTHROPIC_API_KEY` / `AWS_REGION` / `CUSTOM_LLM_BASE_URL` by default.
3. **Opt-in env fallback.** Set `llm_fallback: true` to restore the legacy env auto-detect chain *before* falling through to `agent-sdk`:
   - `ANTHROPIC_API_KEY` set → **anthropic**.
   - `AWS_REGION` (or `AWS_DEFAULT_REGION`) set **and** `anthropic[bedrock]` extras installed → **bedrock**. The direct API still wins when both are present because direct is cheaper per token; pin Bedrock in config to override.
   - `CUSTOM_LLM_BASE_URL` set → **custom**.
   - nothing matched → **agent-sdk**.

anvil never silently falls through to a *different* provider once one is chosen; silent fallback breaks billing predictability and can surprise operators during incidents. Because `agent-sdk` is the guaranteed final default, resolution never fails with "no provider configured".

### Claude Agent SDK (default)

```bash
# Just works, given Claude Code is installed and logged in:
anvil plan --use-llm
```

The default install includes `claude-agent-sdk`. At call time anvil drives the bundled `claude` CLI via `claude_agent_sdk.query()` and authenticates with your logged-in Claude **subscription** — there is no `ANTHROPIC_API_KEY` to set. anvil scrubs `ANTHROPIC_API_KEY` / `CLAUDE_API_KEY` from the environment for the duration of the call so a quota-capped key cannot hijack the run.

Requirements (surfaced as a clean error at call time if missing):

- the `claude` CLI on PATH, logged in to an active subscription session (`claude --version` to verify).

To pin a model (otherwise the subscription's own default model is used):

```yaml
# .anvil/config.yaml
llm_provider: agent-sdk     # optional — this is already the default
llm_tier: sonnet            # opus | sonnet | haiku (maps to a model id)
# or:
llm_model: claude-opus-4-7  # explicit id (overrides tier)
```

Leaving both `llm_tier` and `llm_model` blank lets the subscription pick its default model.

### Direct Anthropic API

This is the metered per-token path. It is **no longer the default** — you must
pin it (or enable `llm_fallback`), because the keyless `agent-sdk` default does
not consult `ANTHROPIC_API_KEY`.

```yaml
# .anvil/config.yaml
llm_provider: anthropic
llm_tier: sonnet      # opus | sonnet | haiku (blank = sonnet)
```

```bash
export ANTHROPIC_API_KEY=sk-ant-...
anvil plan --use-llm
```

The `anthropic` SDK ships in the default install, so no extra is needed. (Or,
to keep config untouched, set `llm_fallback: true` and an `ANTHROPIC_API_KEY`
in env — see Precedence above.)

To pin an explicit model id (overrides tier):

```yaml
llm_provider: anthropic
llm_model: claude-opus-4-7-20260124
```

### Amazon Bedrock

**Install:**

```bash
uv tool install 'anvil-state[bedrock]'
```

This adds `anthropic[bedrock]` (which pulls boto3) on top of the base install.

**Configure:** the Bedrock client uses the **standard boto3 credential chain**, so any auth that works for `aws s3 ls` works here:

- env vars (`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN`)
- `~/.aws/credentials` profile (default or named)
- IAM instance/task/IRSA role (EC2, ECS, EKS)

Region resolves from `aws_region` constructor arg → `AWS_REGION` → `AWS_DEFAULT_REGION`. anvil does **not** silently default to `us-east-1`; the SDK will raise a clear error if none of these are set.

Minimal config:

```yaml
# .anvil/config.yaml
llm_provider: bedrock
bedrock_region: us-east-1
bedrock_profile: my-profile     # optional; reads ~/.aws/credentials
llm_tier: sonnet
```

Bedrock uses **cross-region inference profile prefixes** on current-generation Claude models; anvil's tier defaults bake in the `us.` prefix (see the [Cost-tier defaults](#cost-tier-defaults) table below). If your AWS region needs `eu.` or `global.` profiles, set `llm_model` explicitly:

```yaml
llm_provider: bedrock
llm_model: eu.anthropic.claude-sonnet-4-6
bedrock_region: eu-west-1
```

### Custom OpenAI-compatible endpoint

**Install:**

```bash
uv tool install 'anvil-state[custom]'
```

This adds the `openai` SDK; anvil uses it with `base_url=` to target any endpoint that speaks `/v1/chat/completions`.

**Configure:** `base_url` is **required** for the custom path. No portable default exists, and falling back to `api.openai.com` when a local server was intended would create unexpected billing and data-routing behavior. Set it in env OR config:

```bash
# via env
export CUSTOM_LLM_BASE_URL=http://localhost:8000/v1
export CUSTOM_LLM_API_KEY=...   # if your endpoint requires a key
```

```yaml
# via config
llm_provider: custom
custom_base_url: http://localhost:8000/v1
custom_api_key_env: OPENROUTER_API_KEY   # name of env var to read the key from
llm_model: anthropic/claude-sonnet-4-6   # REQUIRED for custom — no portable default
```

**Worked examples:**

Local vLLM (no auth):

```yaml
llm_provider: custom
custom_base_url: http://localhost:8000/v1
llm_model: meta-llama/Llama-3.1-70B-Instruct
```

OpenRouter (routes to Anthropic):

```yaml
llm_provider: custom
custom_base_url: https://openrouter.ai/api/v1
custom_api_key_env: OPENROUTER_API_KEY
llm_model: anthropic/claude-sonnet-4-6
```

LiteLLM proxy (unified gateway in front of multiple providers):

```yaml
llm_provider: custom
custom_base_url: http://litellm-proxy.internal:4000/v1
custom_api_key_env: LITELLM_API_KEY
llm_model: claude-sonnet-4-6
```

**Caveats:**

- No `cache_control` field — OpenAI's API has no prompt-caching equivalent, so the caching behavior described above only applies to the direct-API and Bedrock paths. Servers that auto-cache (vLLM with prefix caching enabled, OpenRouter's transparent caching) still work, but anvil does not control it per-call the way it does on the Anthropic path.
- No `cached_input_tokens` accounting — OpenAI's usage objects report a single `prompt_tokens`, mapped to `input_tokens` with `cached_input_tokens=0`.
- Model name is pass-through. anvil does not translate tier names for custom endpoints — your `llm_model` value goes to the server verbatim. Different proxies use different naming conventions (`gpt-4o` for OpenAI, `meta-llama/Llama-3-70b-instruct` for OpenRouter, `claude-sonnet-4-6` for Anthropic-via-LiteLLM); set it to whatever your proxy expects.

### Tier vs explicit model id

The `llm_tier` field accepts a logical name (`opus` / `sonnet` / `haiku`) and the provider translates it to the right model id for its namespace. This is the recommended way to set a project-wide default because it survives Anthropic model refreshes — when a newer Sonnet ships, agents pinned to `tier: sonnet` auto-upgrade and you don't need to touch every config file.

Use `llm_model` (explicit id) only when:

- You need to pin to a specific dated model id (`claude-sonnet-4-6-20260518`) for reproducibility.
- You're on a custom endpoint that requires a non-standard model name (OpenRouter routes, vLLM-served local models).
- You want a model outside the Opus/Sonnet/Haiku trio.

Precedence within a provider: `llm_model` > `llm_tier` > the provider's default. For the API providers that default is `DEFAULT_TIER` (Sonnet); for `agent-sdk` it is the subscription's own default model (no tier is forced). See [`model-strategy.md`](model-strategy.md) for *why* Sonnet is the default tier.

### Cost-tier defaults

The tier table is published in `bin/src/anvil/planning/llm.py` as `MODEL_TIERS` and `BEDROCK_MODEL_TIERS`. When Anthropic ships a newer model in a tier, those constants get bumped and the CHANGELOG notes the floor change. Agents pinned to a logical tier auto-upgrade.

| Tier | Direct API id | Bedrock id (`us.` profile) | Direct API price (in / out, per M tokens) | Recommended for |
| --- | --- | --- | --- | --- |
| `opus` | `claude-opus-4-7` | `us.anthropic.claude-opus-4-7` | $15 / $75 | Multi-file architecture, hard debugging, deep code review, planning synthesis. |
| `sonnet` (default) | `claude-sonnet-4-6` | `us.anthropic.claude-sonnet-4-6` | $3 / $15 | Daily coding, structured generation, pattern matching, most agent work. |
| `haiku` | `claude-haiku-4-5` | `us.anthropic.claude-haiku-4-5` | $1 / $5 | File enumeration, regex/glob search, simple validation, mechanical regen. |

Prices are Direct API list prices (2026 snapshot); Bedrock pricing varies by region and inference profile. Custom endpoints (vLLM self-hosted) carry hosting cost only; OpenRouter / Together pass through provider rates with a margin. See [`docs/model-strategy.md`](model-strategy.md) for the per-agent tier rationale.

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

#### `--format prd`

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
- Prompt caching (see [Configuration](#configuration) above) makes repeated runs cheap. A
  typical `score --use-llm` run against a 20-task batch hits the cache on tasks 2–20 and
  pays for one cold system block plus 20 small user blocks plus 20 small output blocks.
- `expand` is the heaviest call (sub-task JSON, up to ~800 output tokens) but is invoked
  once per high-complexity task and gated by `complexity >= 4`.
- See [Cost-tier defaults](#cost-tier-defaults) for per-tier pricing and
  [`model-strategy.md`](model-strategy.md) for the per-agent tier rationale.

---

## See also

- [`mcp.md`](mcp.md) — MCP server (`plan_tasks` calls the LLM task-generation backstop by
  default; other LLM augmentation such as `score --use-llm` and `expand` is CLI-only).
- [`model-strategy.md`](model-strategy.md) — why agents default to specific tiers, the
  per-agent mapping, and override precedence.
- [`prd-template.md`](prd-template.md) — the deterministic PRD format the parser expects.
- `specs/2026-05-24-anvil-v0.md` — canonical design spec including the LLM
  augmentation contract.
