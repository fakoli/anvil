# LLM providers

fakoli-state's planning features (`--use-llm`, the LLM-driven task-generation backstop, `expand --use-llm`, `score --use-llm`) can be backed by three different LLM provider families. This guide covers how to set each one up and how the precedence rule picks between them.

> **TL;DR.** Set `ANTHROPIC_API_KEY` in your env and everything works. Move to Bedrock or a custom OpenAI-compatible endpoint when your org needs it.

---

## Provider matrix

| Provider | When to use | Optional extras | Config key |
| --- | --- | --- | --- |
| **Direct Anthropic API** | Default. Cheapest per-token path. Works inside Claude Code, Cursor, Codex, or any shell with the key. | None (`anthropic` is a hard dep). | `llm_provider: anthropic` |
| **Amazon Bedrock** | Your org pins LLM calls to AWS for compliance, billing, or data-residency reasons. | `pip install 'fakoli-state[bedrock]'` (adds `anthropic[bedrock]` + boto3). | `llm_provider: bedrock` |
| **Custom OpenAI-compatible** | You're on vLLM, LiteLLM proxy, OpenRouter, Together, Groq, Azure OpenAI, or a self-hosted endpoint that speaks `/v1/chat/completions`. | `pip install 'fakoli-state[custom]'` (adds `openai`). | `llm_provider: custom` |

---

## Precedence — who picks the provider

`fakoli-state plan` (and every other LLM-touching CLI / MCP tool) walks this order to pick **exactly one** provider per process:

1. **Explicit `llm_provider` in `.fakoli-state/config.yaml`** — always wins.
2. **Env auto-detect** (only if config is silent):
   - `ANTHROPIC_API_KEY` set → **anthropic**.
   - `AWS_REGION` (or `AWS_DEFAULT_REGION`) set **and** `anthropic[bedrock]` extras installed → **bedrock**. The direct API still wins when both are present because direct is cheaper per token; pin Bedrock in config to override.
   - `CUSTOM_LLM_BASE_URL` set → **custom**.
3. **Fail loudly** with a multi-line message naming every supported path. fakoli-state never silently falls through to a different provider mid-process — community consensus (research, May 2026) is that silent fallback breaks billing predictability and surprises ops teams during incidents.

---

## Direct Anthropic API (default)

```bash
export ANTHROPIC_API_KEY=sk-ant-...
fakoli-state plan
```

That's all. The default install includes the `anthropic` SDK.

To pin a tier:

```yaml
# .fakoli-state/config.yaml
llm_provider: anthropic
llm_tier: sonnet      # opus | sonnet | haiku (blank = sonnet)
```

To pin an explicit model id (overrides tier):

```yaml
llm_provider: anthropic
llm_model: claude-opus-4-7-20260124
```

---

## Amazon Bedrock

### Install

```bash
pip install 'fakoli-state[bedrock]'
```

This adds `anthropic[bedrock]` (which pulls boto3) on top of the base install.

### Configure

The Bedrock client uses the **standard boto3 credential chain**, so any auth that works for `aws s3 ls` works here:

- env vars (`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN`)
- `~/.aws/credentials` profile (default or named)
- IAM instance/task/IRSA role (EC2, ECS, EKS)

Region resolves from `aws_region` constructor arg → `AWS_REGION` → `AWS_DEFAULT_REGION`. fakoli-state does **not** silently default to `us-east-1`; the SDK will raise a clear error if none of these are set.

Minimal config:

```yaml
# .fakoli-state/config.yaml
llm_provider: bedrock
bedrock_region: us-east-1
bedrock_profile: my-profile     # optional; reads ~/.aws/credentials
llm_tier: sonnet
```

### Model IDs

Bedrock uses **cross-region inference profile prefixes** on current-generation Claude models. fakoli-state's tier defaults bake in the `us.` prefix:

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
pip install 'fakoli-state[custom]'
```

This adds the `openai` SDK; fakoli-state uses it with `base_url=` to target any endpoint that speaks `/v1/chat/completions`.

### Configure

`base_url` is **required** for the custom path (no sensible default exists — silently falling back to `api.openai.com` when you meant your local server would be a billing surprise). Set it in env OR config:

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

- **No prompt-cache `cache_control` field** — OpenAI's API does not have one. Servers that auto-cache (vLLM with prefix caching enabled, OpenRouter's transparent caching) still work, but you lose the per-call control fakoli-state exercises on the Anthropic path.
- **No `cached_input_tokens` accounting** — OpenAI's usage objects report a single `prompt_tokens`, mapped to `input_tokens` with `cached_input_tokens=0`.
- **Model name is pass-through.** fakoli-state does not translate tier names for custom endpoints — your `llm_model` value goes to the server verbatim. Different proxies use different naming conventions (`gpt-4o` for OpenAI, `meta-llama/Llama-3-70b-instruct` for OpenRouter, `claude-sonnet-4-6` for Anthropic-via-LiteLLM); set it to whatever your proxy expects.

---

## Tier vs explicit model id

The `llm_tier` field accepts a logical name (`opus` / `sonnet` / `haiku`) and the provider translates it to the right model id for its namespace. This is the recommended way to set a project-wide default because it survives Anthropic model refreshes — when Sonnet 4.7 ships, agents pinned to `tier: sonnet` auto-upgrade and you don't need to touch every config file.

Use `llm_model` (explicit id) only when:

- You need to pin to a specific dated model id (`claude-sonnet-4-6-20260518`) for reproducibility.
- You're on a custom endpoint that requires a non-standard model name (OpenRouter routes, vLLM-served local models).
- You want a model outside the Opus/Sonnet/Haiku trio.

Precedence within a provider: `llm_model` > `llm_tier` > the provider's `DEFAULT_TIER` (Sonnet).

---

## Cost-tier defaults (refreshed 2026-05-26)

The tier table is published in `bin/src/fakoli_state/planning/llm.py` as `MODEL_TIERS` and `BEDROCK_MODEL_TIERS`. When Anthropic ships a newer model in a tier, those constants get bumped and the CHANGELOG notes the floor change. Agents pinned to a logical tier auto-upgrade.

| Tier | Direct API id | Bedrock id (us. profile) | Recommended for |
| --- | --- | --- | --- |
| `opus` | `claude-opus-4-7` | `us.anthropic.claude-opus-4-7` | Multi-file architecture, hard debugging, deep code review, planning synthesis. |
| `sonnet` (default) | `claude-sonnet-4-6` | `us.anthropic.claude-sonnet-4-6` | Daily coding, structured generation, pattern matching, most agent work. |
| `haiku` | `claude-haiku-4-5` | `us.anthropic.claude-haiku-4-5` | File enumeration, regex/glob search, simple validation, mechanical regen. |

See [`docs/model-strategy.md`](model-strategy.md) for the per-agent tier rationale and the 2026 cost figures.
