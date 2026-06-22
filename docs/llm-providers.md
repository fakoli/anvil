# LLM providers

anvil's planning features (`--use-llm`, the LLM-driven task-generation backstop, `expand --use-llm`, `score --use-llm`) can be backed by four different LLM provider families. This guide covers how to set each one up and how the precedence rule picks between them.

> **TL;DR.** Do nothing — the default is the **Claude Agent SDK** over your logged-in Claude subscription (no API key). It just needs the `claude` CLI on PATH. Pin `anthropic` / `bedrock` / `custom` in `.anvil/config.yaml` when your org needs a metered API, AWS, or a local endpoint instead.

---

## Provider matrix

| Provider | When to use | Extras | Config key |
| --- | --- | --- | --- |
| **Claude Agent SDK** | **Default.** Rides your Claude *subscription* (no per-token key). anvil is capacity-bound, not per-token-cost bound, so this is the default. | None (`claude-agent-sdk` is a core dep); needs the `claude` CLI on PATH. | `llm_provider: agent-sdk` |
| **Direct Anthropic API** | You want metered per-token billing against an `ANTHROPIC_API_KEY` (CI without a subscription session, etc.). | None (`anthropic` is a core dep). | `llm_provider: anthropic` |
| **Amazon Bedrock** | Your org pins LLM calls to AWS for compliance, billing, or data-residency reasons. | `pip install 'anvil-state[bedrock]'` (adds `anthropic[bedrock]` + boto3). | `llm_provider: bedrock` |
| **Custom OpenAI-compatible** | You're on vLLM, LiteLLM proxy, OpenRouter, Together, Groq, Azure OpenAI, or a self-hosted endpoint that speaks `/v1/chat/completions`. | `pip install 'anvil-state[custom]'` (adds `openai`). | `llm_provider: custom` |

---

## Precedence — who picks the provider

`anvil plan` (and every other LLM-touching CLI / MCP tool) picks **exactly one** provider per process:

1. **Explicit `llm_provider` in `.anvil/config.yaml`** — always wins (`agent-sdk` / `anthropic` / `bedrock` / `custom`).
2. **Default → `agent-sdk`.** With no explicit provider, anvil uses the Claude Agent SDK over the subscription. It does **not** consult `ANTHROPIC_API_KEY` / `AWS_REGION` / `CUSTOM_LLM_BASE_URL` by default.
3. **Opt-in env fallback.** Set `llm_fallback: true` to restore the legacy env auto-detect chain *before* falling through to `agent-sdk`:
   - `ANTHROPIC_API_KEY` set → **anthropic**.
   - `AWS_REGION` (or `AWS_DEFAULT_REGION`) set **and** `anthropic[bedrock]` extras installed → **bedrock**. The direct API still wins when both are present because direct is cheaper per token; pin Bedrock in config to override.
   - `CUSTOM_LLM_BASE_URL` set → **custom**.
   - nothing matched → **agent-sdk**.

anvil never silently falls through to a *different* provider once one is chosen; silent fallback breaks billing predictability and can surprise operators during incidents. Because `agent-sdk` is the guaranteed final default, resolution never fails with "no provider configured".

---

## Claude Agent SDK (default)

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

---

## Direct Anthropic API

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

---

## Amazon Bedrock

### Install

```bash
pip install 'anvil[bedrock]'
```

This adds `anthropic[bedrock]` (which pulls boto3) on top of the base install.

### Configure

The Bedrock client uses the **standard boto3 credential chain**, so any auth that works for `aws s3 ls` works here:

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

### Model IDs

Bedrock uses **cross-region inference profile prefixes** on current-generation Claude models. anvil's tier defaults bake in the `us.` prefix:

| Tier | Bedrock model id |
| --- | --- |
| `opus` | `us.anthropic.claude-opus-4-7` |
| `sonnet` | `us.anthropic.claude-sonnet-4-6` |
| `haiku` | `us.anthropic.claude-haiku-4-5` |

If your AWS region needs `eu.` or `global.` profiles, set `llm_model` explicitly:

```yaml
llm_provider: bedrock
llm_model: eu.anthropic.claude-sonnet-4-6
bedrock_region: eu-west-1
```

---

## Custom OpenAI-compatible endpoint

### Install

```bash
pip install 'anvil[custom]'
```

This adds the `openai` SDK; anvil uses it with `base_url=` to target any endpoint that speaks `/v1/chat/completions`.

### Configure

`base_url` is **required** for the custom path. No portable default exists, and falling back to `api.openai.com` when a local server was intended would create unexpected billing and data-routing behavior. Set it in env OR config:

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

### Worked examples

**Local vLLM (no auth):**

```yaml
llm_provider: custom
custom_base_url: http://localhost:8000/v1
llm_model: meta-llama/Llama-3.1-70B-Instruct
```

**OpenRouter (routes to Anthropic):**

```yaml
llm_provider: custom
custom_base_url: https://openrouter.ai/api/v1
custom_api_key_env: OPENROUTER_API_KEY
llm_model: anthropic/claude-sonnet-4-6
```

**LiteLLM proxy (unified gateway in front of multiple providers):**

```yaml
llm_provider: custom
custom_base_url: http://litellm-proxy.internal:4000/v1
custom_api_key_env: LITELLM_API_KEY
llm_model: claude-sonnet-4-6
```

### Caveats

- **No prompt-cache `cache_control` field** — OpenAI's API does not have one. Servers that auto-cache (vLLM with prefix caching enabled, OpenRouter's transparent caching) still work, but you lose the per-call control anvil exercises on the Anthropic path.
- **No `cached_input_tokens` accounting** — OpenAI's usage objects report a single `prompt_tokens`, mapped to `input_tokens` with `cached_input_tokens=0`.
- **Model name is pass-through.** anvil does not translate tier names for custom endpoints — your `llm_model` value goes to the server verbatim. Different proxies use different naming conventions (`gpt-4o` for OpenAI, `meta-llama/Llama-3-70b-instruct` for OpenRouter, `claude-sonnet-4-6` for Anthropic-via-LiteLLM); set it to whatever your proxy expects.

---

## Tier vs explicit model id

The `llm_tier` field accepts a logical name (`opus` / `sonnet` / `haiku`) and the provider translates it to the right model id for its namespace. This is the recommended way to set a project-wide default because it survives Anthropic model refreshes — when Sonnet 4.7 ships, agents pinned to `tier: sonnet` auto-upgrade and you don't need to touch every config file.

Use `llm_model` (explicit id) only when:

- You need to pin to a specific dated model id (`claude-sonnet-4-6-20260518`) for reproducibility.
- You're on a custom endpoint that requires a non-standard model name (OpenRouter routes, vLLM-served local models).
- You want a model outside the Opus/Sonnet/Haiku trio.

Precedence within a provider: `llm_model` > `llm_tier` > the provider's default. For the API providers that default is `DEFAULT_TIER` (Sonnet); for `agent-sdk` it is the subscription's own default model (no tier is forced).

---

## Cost-tier defaults (refreshed 2026-05-26)

The tier table is published in `bin/src/anvil/planning/llm.py` as `MODEL_TIERS` and `BEDROCK_MODEL_TIERS`. When Anthropic ships a newer model in a tier, those constants get bumped and the CHANGELOG notes the floor change. Agents pinned to a logical tier auto-upgrade.

| Tier | Direct API id | Bedrock id (us. profile) | Recommended for |
| --- | --- | --- | --- |
| `opus` | `claude-opus-4-7` | `us.anthropic.claude-opus-4-7` | Multi-file architecture, hard debugging, deep code review, planning synthesis. |
| `sonnet` (default) | `claude-sonnet-4-6` | `us.anthropic.claude-sonnet-4-6` | Daily coding, structured generation, pattern matching, most agent work. |
| `haiku` | `claude-haiku-4-5` | `us.anthropic.claude-haiku-4-5` | File enumeration, regex/glob search, simple validation, mechanical regen. |

See [`docs/model-strategy.md`](model-strategy.md) for the per-agent tier rationale and the 2026 cost figures.
