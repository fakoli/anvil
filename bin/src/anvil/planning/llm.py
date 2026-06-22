"""LLM provider abstraction for planning augmentation.

This module is the *only* place anvil talks to a Large Language Model.
Callers (planning.scoring, planning.template, planning.inference, …) take an
``LLMProvider`` and call ``generate()`` — they never import the Anthropic SDK
directly. Everything else in Phase 7+ layers on top of this Protocol.

Public surface
--------------
- :class:`LLMResponse` — Pydantic model for a single completion result.
- :class:`LLMProvider` — typing.Protocol; one method, ``generate``.
- :class:`ClaudeAgentSDKProvider` — **the default provider.** Drives the
  bundled Claude Code CLI via ``claude_agent_sdk.query()`` over the logged-in
  Claude *subscription* (no per-token API key). Needs the ``claude`` CLI on
  PATH at call time; both the SDK import and a missing CLI surface as
  :class:`LLMProviderError`. Scrubs ``ANTHROPIC_API_KEY`` / ``CLAUDE_API_KEY``
  for the duration of the call so a quota-capped key cannot hijack the run
  (mirrors ``evals/harness.py``).
- :class:`AnthropicProvider` — direct Anthropic API via the ``anthropic`` SDK.
- :class:`BedrockProvider` — Anthropic-on-Bedrock via ``anthropic[bedrock]``
  (the SDK's first-party ``AnthropicBedrock`` client). Optional dep; the
  import is lazy so users who never set ``llm_provider: bedrock`` do not need
  boto3 installed.
- :class:`CustomEndpointProvider` — any OpenAI-compatible endpoint (vLLM,
  LiteLLM proxy, Together, OpenRouter, Groq, …) via the ``openai`` SDK
  with ``base_url=``. Optional dep; lazy import for the same reason.
- :class:`RecordedLLMProvider` — deterministic test double; canned responses
  keyed by a length-prefixed sha256 over ``(system, user, max_tokens,
  temperature)``.  Tuning args participate in the key — two recordings
  under different ``max_tokens`` / ``temperature`` do NOT collide.
- :class:`LLMProviderError` — wraps SDK / network / lookup failures so CLI
  callers can ``except LLMProviderError`` once and emit a clean error.
- :data:`MODEL_TIERS`, :data:`BEDROCK_MODEL_TIERS`, :data:`DEFAULT_TIER` —
  the tier ↔ model-id mapping used when callers pass ``tier="opus"``
  instead of an explicit model id. See :func:`resolve_model_for_tier`.

Design notes
------------
* Single-shot completion: ``system`` + ``user`` only.  No multi-turn history
  yet — anvil uses the LLM for augmenting *structured* planning
  output, not freeform chat.  When/if a use case appears, add ``history=[…]``
  as a keyword-only arg with a default — never a positional break.
* Temperature defaults to ``0.0`` (deterministic).  Augmentation of plans
  should be repeatable, not creative.
* **Prompt caching** (superpowers:claude-api skill rule): every Anthropic-
  family request (direct API or Bedrock) sets
  ``cache_control: {"type": "ephemeral"}`` on the system block, so repeated
  calls with the same system prompt hit the cache and pay only for the new
  user tokens. The OpenAI-compatible path does not set this — most non-
  Anthropic endpoints either ignore the field or auto-cache server-side.
* **Multi-provider precedence** (v1.17.0): the new
  :func:`anvil.planning.llm_planner.resolve_planner_provider`
  consumes :class:`anvil.config.Config` to pick exactly one provider
  per process — explicit ``llm_provider`` config wins, falling back to
  env-based auto-detect, then failing loudly. We deliberately do NOT silent-
  fail across providers; community consensus is that silent fallback breaks
  cost predictability and surprises ops teams during incidents.
* **Tier-aware defaults** (v1.17.0): callers pass ``tier="opus"|"sonnet"|
  "haiku"`` instead of model IDs. The provider maps the tier to its own
  model-id namespace (different on Bedrock vs direct API). Default tier is
  Sonnet — Anthropic's own routing issue #27665 documents that defaulting
  every agent to Opus is the dominant cost anti-pattern in 2026 setups.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Protocol, cast

# The anthropic SDK is a hard runtime dep (declared in pyproject.toml);
# import at module load is fine. The Bedrock and OpenAI-compatible client
# imports are LAZY inside their respective provider classes so users who do
# not configure those providers do not need to install boto3 or openai.
import anthropic
from anthropic.types import TextBlockParam
from pydantic import BaseModel, Field, ValidationError

if TYPE_CHECKING:
    # These types are only needed for annotations on the optional providers.
    # Importing here under TYPE_CHECKING keeps the runtime cost zero for the
    # default Anthropic-API path.
    # Pyright flags `AnthropicBedrock` as a private import because it is not
    # in the SDK's top-level `__all__`, but it IS the documented public
    # entry point per the anthropic-sdk-python README — keep the public name
    # and silence the false positive.
    from anthropic import AnthropicBedrock  # pyright: ignore[reportPrivateImportUsage]
    from openai import OpenAI

__all__ = [
    "LLMResponse",
    "LLMProvider",
    "ClaudeAgentSDKProvider",
    "AnthropicProvider",
    "BedrockProvider",
    "CustomEndpointProvider",
    "RecordedLLMProvider",
    "LLMProviderError",
    "MODEL_TIERS",
    "BEDROCK_MODEL_TIERS",
    "DEFAULT_TIER",
    "resolve_model_for_tier",
]


# ---------------------------------------------------------------------------
# Response model
# ---------------------------------------------------------------------------


class LLMResponse(BaseModel):
    """Result of a single LLM completion.

    All fields are required so callers can rely on them without ``.get()``-
    style defensive coding.  Token counts are integers; providers that do
    not report a particular count (e.g. non-Anthropic providers, or a cache
    miss) MUST set the field to ``0`` rather than ``None``.
    """

    text: str = Field(description="The completion text (joined first text block).")
    input_tokens: int = Field(
        ge=0,
        description=(
            "Non-cached input tokens for this completion. The Anthropic SDK "
            "reports input_tokens as the non-cached portion separately from "
            "cache_read_input_tokens, so this field is the SDK's "
            "Usage.input_tokens passed through unchanged."
        ),
    )
    cached_input_tokens: int = Field(
        ge=0,
        description=(
            "Tokens served from the prompt cache. ``0`` for non-Anthropic "
            "providers, or for the first call where the cache is cold."
        ),
    )
    output_tokens: int = Field(ge=0, description="Tokens generated by the model.")
    model: str = Field(description="Model id that produced the response.")
    finish_reason: str = Field(
        description="Provider-native stop reason, e.g. 'end_turn', 'max_tokens'."
    )


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LLMProviderError(Exception):
    """Raised when an LLM provider call fails.

    Wraps lower-level SDK / network / lookup errors so callers (CLI, MCP
    tools) can catch a single exception type and surface a clean user-facing
    error.  The original exception is chained via ``raise … from exc``.
    """


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class LLMProvider(Protocol):
    """Minimal completion API used by planning augmentation.

    Implementations MUST be safe to call repeatedly with the same arguments.
    Implementations SHOULD enable prompt caching where the backend supports
    it, so callers can reuse a long system prompt cheaply.
    """

    def generate(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> LLMResponse:
        """Run a single-shot completion.

        Parameters
        ----------
        system:
            The system prompt.  Should be stable across calls so prompt
            caching (where supported) can kick in.
        user:
            The user prompt — the actual planning question.
        max_tokens:
            Hard cap on output tokens.  Default 4096 is enough for the
            largest planning-augmentation outputs we currently emit.
        temperature:
            ``0.0`` (default) for deterministic augmentation.  Set higher
            only if a caller explicitly wants variability.

        Raises
        ------
        LLMProviderError
            If the upstream call fails for any reason.
        """
        ...  # pragma: no cover — Protocol


# ---------------------------------------------------------------------------
# Tier ↔ model-id mapping (v1.17.0)
# ---------------------------------------------------------------------------
#
# We expose a small "tier" vocabulary (``opus`` / ``sonnet`` / ``haiku``) so
# agent definitions, config files, and CLI flags can stay stable across
# Anthropic model refreshes. The provider classes translate the tier into
# their own model-id namespace at call time.
#
# Last verified against the Anthropic API and Bedrock catalog: 2026-05-26.
# When Anthropic ships a newer model in a tier (Sonnet 4.7, Haiku 5, …),
# update the right-hand side here AND bump ``CHANGELOG.md`` with a note
# explaining the floor model change — agents that pinned a logical tier will
# auto-upgrade, which is normally what users want but is worth flagging.

# Tier defaults for the **direct Anthropic API**. Model IDs are bare
# (no region prefix). See https://docs.anthropic.com/en/docs/about-claude/models
MODEL_TIERS: dict[str, str] = {
    "opus": "claude-opus-4-7",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5",
}

# Tier defaults for **Bedrock**. Bedrock requires cross-region inference
# profile prefixes (``us.`` / ``eu.`` / ``global.``) for current-generation
# Claude models. We default to the ``us.`` prefix because (a) it covers the
# largest number of AWS users, (b) the SDK error is clear if the region is
# wrong, and (c) users can override per call via ``model=`` if they want a
# different prefix. The Anthropic+Bedrock model IDs occasionally include a
# date suffix; if you pin to a specific dated ID, pass it explicitly via the
# ``model=`` constructor arg.
#
# Reference: AWS Bedrock model card pages (2026-05-26 snapshot) and Claude
# Code's own ``modelOverrides`` pattern.
BEDROCK_MODEL_TIERS: dict[str, str] = {
    "opus": "us.anthropic.claude-opus-4-7",
    "sonnet": "us.anthropic.claude-sonnet-4-6",
    "haiku": "us.anthropic.claude-haiku-4-5",
}

# Default tier when nothing is specified. **Sonnet, not Opus.** This is the
# critical cost-optimization decision: Anthropic's own routing telemetry
# (issue #27665, May 2026) showed that defaulting every agent to Opus is the
# headline cost anti-pattern. Sonnet handles the vast majority of agent work
# at ~5× lower per-token cost; opt up to Opus per agent when reasoning depth
# justifies it.
DEFAULT_TIER: str = "sonnet"

def resolve_model_for_tier(
    tier: str,
    *,
    bedrock: bool = False,
) -> str:
    """Translate a logical tier (``opus``/``sonnet``/``haiku``) to a model ID.

    The two namespaces ship slightly different IDs (Bedrock prepends an
    inference-profile prefix); this helper picks the right table.

    Raises:
        ValueError: ``tier`` is not one of the known tiers.
    """
    table = BEDROCK_MODEL_TIERS if bedrock else MODEL_TIERS
    try:
        return table[tier]
    except KeyError as exc:
        valid = ", ".join(sorted(table))
        raise ValueError(
            f"Unknown model tier {tier!r}. Valid tiers: {valid}."
        ) from exc


# ---------------------------------------------------------------------------
# ClaudeAgentSDKProvider — subscription auth via the Claude Agent SDK (DEFAULT)
# ---------------------------------------------------------------------------


def _run_blocking_until_complete(drive: Callable[[], Awaitable[None]]) -> None:
    """Run an async zero-arg function to completion from synchronous code.

    ``anyio.run()`` raises ``RuntimeError`` if an event loop is already running
    in the calling thread. Every current caller is loop-free — the sync CLI,
    and the MCP server's tools (which are sync ``def``\\ s that fastmcp runs in
    a worker thread) — so the common path is a direct ``anyio.run``. If a
    future caller invokes ``generate()`` from *inside* a running loop, we
    offload to a worker thread whose ``anyio.run`` creates its own loop, so the
    provider stays usable from async code too.
    """
    import anyio

    try:
        import asyncio

        asyncio.get_running_loop()
    except RuntimeError:
        # No running loop in this thread — drive directly.
        anyio.run(drive)
        return

    # A loop is already running here. Offload to a worker thread; its
    # ``anyio.run`` gets a fresh loop. The closure's captured state is read
    # only after the thread joins (``.result()``), so there is no data race.
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(lambda: anyio.run(drive)).result()


class ClaudeAgentSDKProvider:
    """LLMProvider backed by the Claude Agent SDK — **the default provider**.

    Unlike :class:`AnthropicProvider` / :class:`BedrockProvider`, this does NOT
    call the Anthropic API with a per-token key. It drives the bundled Claude
    Code CLI through ``claude_agent_sdk.query()`` and authenticates with the
    logged-in Claude *subscription*. This is the economically-aligned default:
    anvil is capacity-bound (it rides flat-rate plans), not per-token-cost
    bound, so the subscription path is preferred over metered API spend.

    Single-shot, no tools: ``max_turns=1``, ``allowed_tools=[]``,
    ``setting_sources=[]`` (so ambient Claude Code hooks/settings do not leak
    into the subprocess). The ``system`` prompt *replaces* the CLI's default
    system prompt entirely — exactly what planning augmentation wants.

    ``ANTHROPIC_API_KEY`` / ``CLAUDE_API_KEY`` are scrubbed from the process
    environment for the duration of the call, then restored. The SDK's
    subprocess transport inherits ``os.environ`` and can only ADD keys, never
    remove an inherited one — so a quota-capped key present in the env would
    otherwise hijack the run and fail it with a 400 usage-limit error. This
    mirrors ``evals/harness.py``.

    Requirements (surfaced as :class:`LLMProviderError` at call time, not at
    construction — so :func:`resolve_planner_provider` can hand back this
    provider without importing the SDK or needing the CLI):

      * the ``claude-agent-sdk`` package importable, and
      * the ``claude`` CLI on PATH with an active subscription session.

    Model resolution order:
      1. Explicit ``model=`` constructor arg.
      2. ``tier=`` constructor arg, resolved via :data:`MODEL_TIERS`.
      3. ``None`` → the CLI / subscription default model. (We deliberately do
         NOT force :data:`DEFAULT_TIER` here: an unconfigured project should
         get whatever model the subscription defaults to.)

    Note on ``max_tokens`` / ``temperature``: accepted for
    :class:`LLMProvider` compatibility but NOT forwarded — the Agent SDK / CLI
    does not expose per-call equivalents the way the Messages API does. Output
    length and sampling are governed by the CLI / subscription. Planning
    augmentation runs at ``temperature=0.0`` by convention regardless.
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        tier: str | None = None,
    ) -> None:
        """Construct a ClaudeAgentSDKProvider.

        Construction never imports ``claude_agent_sdk`` or touches the CLI —
        the import is deferred to :meth:`generate`. This keeps provider
        resolution cheap and lets a project hand back the default provider even
        on a box where the CLI is not yet installed (the error then fires only
        if augmentation is actually invoked).
        """
        if model is not None:
            self._model: str | None = model
        elif tier is not None:
            self._model = resolve_model_for_tier(tier)
        else:
            self._model = None

    def generate(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> LLMResponse:
        """Run a single-shot completion through the Claude Agent SDK.

        See :meth:`LLMProvider.generate`. ``max_tokens`` / ``temperature`` are
        accepted but not forwarded (see the class docstring).
        """
        try:
            from claude_agent_sdk import (
                AssistantMessage,
                ClaudeAgentOptions,
                ClaudeSDKError,
                ResultMessage,
                TextBlock,
                query,
            )
        except ImportError as exc:
            raise LLMProviderError(
                "ClaudeAgentSDKProvider needs the `claude-agent-sdk` package "
                "AND the `claude` CLI on PATH (logged in to a Claude "
                "subscription). Install/verify with:\n"
                "    pip install claude-agent-sdk\n"
                "    claude --version   # the CLI must be installed + logged in\n"
                "Or pin a different provider via `llm_provider:` in "
                ".anvil/config.yaml (anthropic | bedrock | custom)."
            ) from exc

        options = ClaudeAgentOptions(
            system_prompt=system,
            model=self._model,
            max_turns=1,
            allowed_tools=[],
            permission_mode="bypassPermissions",
            # Keep ambient Claude Code hooks/settings out of the subprocess —
            # they flood the run with hook events and can break it.
            setting_sources=[],
        )

        collected_text: list[str] = []
        # Captured-by-closure result fields. A dict avoids `nonlocal` churn for
        # the nested async driver.
        captured: dict[str, Any] = {
            "got_result": False,
            "is_error": False,
            "result_text": None,
            "usage": None,
            "model": None,
            "stop_reason": None,
        }

        async def _drive() -> None:
            try:
                async for message in query(prompt=user, options=options):
                    if isinstance(message, AssistantMessage):
                        collected_text.append(
                            "".join(
                                b.text
                                for b in message.content
                                if isinstance(b, TextBlock)
                            )
                        )
                        # The per-turn assistant frame carries the RELIABLE model
                        # id (required) and stop_reason. The ResultMessage frame
                        # often leaves both None (stop_reason is `str | None`,
                        # model lives only in the optional model_usage map), so
                        # prefer these and use the result frame only as fallback.
                        if message.model:
                            captured["model"] = message.model
                        if message.stop_reason:
                            captured["stop_reason"] = message.stop_reason
                    elif isinstance(message, ResultMessage):
                        captured["got_result"] = True
                        captured["is_error"] = bool(message.is_error)
                        captured["result_text"] = message.result
                        captured["usage"] = message.usage
                        # Fallbacks only — the assistant frame (captured above,
                        # and it arrives first) is authoritative when present.
                        if not captured["stop_reason"]:
                            captured["stop_reason"] = message.stop_reason
                        if not captured["model"]:
                            # model_usage is a dict keyed by model id; first key
                            # is the model that produced this single-turn answer.
                            model_usage = message.model_usage or {}
                            captured["model"] = next(iter(model_usage), None)
            except Exception:  # noqa: BLE001
                # The SDK can emit a trailing terminal control-frame error AFTER
                # it has already delivered the ResultMessage. If we have the
                # result, that frame is noise — keep it. Otherwise it is a real
                # failure: re-raise to be wrapped below.
                if not captured["got_result"]:
                    raise

        # Scrub the API-key vars so the inherited subprocess env does not carry
        # a quota-capped key, then restore.
        saved = {
            var: os.environ.pop(var, None)
            for var in ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY")
        }
        try:
            _run_blocking_until_complete(_drive)
        except ClaudeSDKError as exc:
            raise LLMProviderError(
                f"Claude Agent SDK call failed: {type(exc).__name__}: {exc}"
            ) from exc
        except Exception as exc:  # noqa: BLE001
            # Transport / subprocess errors that are not ClaudeSDKError
            # subclasses (e.g. an anyio cancellation surfaced as a generic
            # exception) still get the uniform LLMProviderError wrap.
            raise LLMProviderError(
                f"Claude Agent SDK call failed: {type(exc).__name__}: {exc}"
            ) from exc
        finally:
            for var, val in saved.items():
                if val is not None:
                    os.environ[var] = val

        if not captured["got_result"]:
            raise LLMProviderError(
                "Claude Agent SDK returned no ResultMessage (no completion)."
            )
        if captured["is_error"]:
            raise LLMProviderError(
                f"Claude Agent SDK reported an error result: "
                f"{captured['result_text']!r}"
            )

        # Prefer the accumulated assistant text; fall back to the ResultMessage
        # ``result`` string (set on a success subtype).
        text = "".join(collected_text).strip() or (captured["result_text"] or "")

        usage = captured["usage"] or {}
        input_tokens = int(usage.get("input_tokens", 0) or 0)
        cached = int(usage.get("cache_read_input_tokens", 0) or 0)
        output_tokens = int(usage.get("output_tokens", 0) or 0)

        try:
            return LLMResponse(
                text=text,
                input_tokens=input_tokens,
                cached_input_tokens=cached,
                output_tokens=output_tokens,
                model=captured["model"] or self._model or "claude-agent-sdk",
                finish_reason=captured["stop_reason"] or "end_turn",
            )
        except ValidationError as exc:
            raise LLMProviderError(
                f"Claude Agent SDK response failed schema validation: {exc}"
            ) from exc


# ---------------------------------------------------------------------------
# AnthropicProvider — direct Anthropic API
# ---------------------------------------------------------------------------


class AnthropicProvider:
    """LLMProvider backed by the Anthropic Python SDK (direct API).

    Prompt caching is enabled by default: the ``system`` block is sent with
    ``cache_control: {"type": "ephemeral"}`` so repeated calls with the same
    system prompt hit the 5-minute ephemeral cache.  This is required by
    the ``superpowers:claude-api`` skill rule.

    Model resolution order (highest priority first):
      1. Explicit ``model=`` constructor arg.
      2. ``tier=`` constructor arg, resolved via :data:`MODEL_TIERS`.
      3. The module default (:data:`DEFAULT_TIER` → Sonnet).

    API key resolution order:
      1. Explicit ``api_key=`` constructor arg.
      2. ``ANTHROPIC_API_KEY`` environment variable.
      3. SDK default (which itself reads ``ANTHROPIC_API_KEY``) — included
         so the SDK can surface its own helpful error if the env var is
         unset and no override was passed.
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        tier: str | None = None,
        api_key: str | None = None,
        client: anthropic.Anthropic | None = None,
    ) -> None:
        """Construct an AnthropicProvider.

        Parameters
        ----------
        model:
            Explicit model id.  Overrides ``tier`` when both are passed.
            ``None`` (the default) falls back to ``tier`` → :data:`DEFAULT_TIER`.
        tier:
            Logical tier (``opus``/``sonnet``/``haiku``).  Translated via
            :data:`MODEL_TIERS`.  Ignored when ``model`` is set.
        api_key:
            Override for the API key.  If omitted, the SDK pulls from
            ``ANTHROPIC_API_KEY`` (also resolved explicitly here for clarity).
        client:
            Pre-built ``anthropic.Anthropic`` instance.  Mainly for tests;
            production callers should pass ``model``/``tier``/``api_key`` and
            let the constructor build the client.
        """
        if model is not None:
            self._model = model
        else:
            self._model = resolve_model_for_tier(tier or DEFAULT_TIER)
        if client is not None:
            self._client = client
        else:
            resolved_key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY")
            # If still None, let the SDK raise its own auth error on first call.
            self._client = anthropic.Anthropic(api_key=resolved_key)

    def generate(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> LLMResponse:
        """Run a single-shot completion against Anthropic.

        See :meth:`LLMProvider.generate`.
        """
        # Prompt caching: ephemeral breakpoint on the system block.
        # The SDK accepts ``system`` either as a string OR as a list of
        # typed text blocks.  We use the list form so we can attach
        # cache_control without enabling the broader top-level beta header
        # (cache_control on system blocks is GA on supported models).
        system_blocks: list[TextBlockParam] = [
            cast(
                TextBlockParam,
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                },
            )
        ]

        try:
            msg = self._client.messages.create(
                model=self._model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system_blocks,
                messages=[{"role": "user", "content": user}],
            )
        except anthropic.AnthropicError as exc:
            # AnthropicError is the common ancestor of APIError, APIConnectionError,
            # APIStatusError, AuthenticationError, RateLimitError, etc.
            raise LLMProviderError(
                f"Anthropic API call failed: {type(exc).__name__}: {exc}"
            ) from exc

        # Extract the first text block.  The SDK returns ``content`` as a list
        # of typed blocks (text, tool_use, etc.); planning augmentation never
        # uses tools, so we expect exactly one text block.
        text = ""
        for block in msg.content:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                text = getattr(block, "text", "")
                break

        cached = getattr(msg.usage, "cache_read_input_tokens", 0) or 0
        # Anthropic reports cache_read_input_tokens separately from input_tokens;
        # input_tokens is already the non-cached portion. Trust it directly.
        non_cached_input = msg.usage.input_tokens or 0

        try:
            return LLMResponse(
                text=text,
                input_tokens=non_cached_input,
                cached_input_tokens=cached,
                output_tokens=msg.usage.output_tokens or 0,
                model=msg.model,
                finish_reason=msg.stop_reason or "unknown",
            )
        except ValidationError as exc:
            # Wrap pydantic schema-validation errors so callers only have to
            # catch LLMProviderError (Phase 7 contract per docstring).
            raise LLMProviderError(
                f"Anthropic response failed schema validation: {exc}"
            ) from exc


# ---------------------------------------------------------------------------
# BedrockProvider — Anthropic-on-Bedrock via anthropic[bedrock]
# ---------------------------------------------------------------------------


class BedrockProvider:
    """LLMProvider backed by ``anthropic.AnthropicBedrock``.

    The Anthropic SDK ships an ``AnthropicBedrock`` client that wraps the
    same ``messages.create`` interface around AWS Bedrock's
    ``InvokeModel`` / ``Converse`` APIs. Using this client (rather than
    talking to ``boto3.client("bedrock-runtime")`` directly) means:

    * the same prompt-caching, system-block, and tool-use shapes work
      verbatim (cache_control on system blocks is the same wire format);
    * model IDs use Bedrock's namespace, including inference-profile
      prefixes (``us.anthropic.claude-opus-4-7``);
    * AWS auth flows through the standard boto3 credential chain
      (env vars → ``~/.aws/credentials`` → IAM role for EC2/ECS/EKS).

    Optional install: ``pip install 'anvil[bedrock]'`` pulls in
    ``anthropic[bedrock]`` which adds boto3. The import is deferred so
    users on the Anthropic-API-only path do not pay for boto3 at startup.

    Credential resolution order (full boto3 chain via ``AnthropicBedrock``):
      1. Explicit ``aws_access_key`` / ``aws_secret_key`` /
         ``aws_session_token`` constructor args (passed through).
      2. ``aws_profile`` constructor arg (passed through).
      3. Environment: ``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY`` /
         ``AWS_SESSION_TOKEN``.
      4. ``~/.aws/credentials`` and ``~/.aws/config``.
      5. IAM instance/task/IRSA role.

    Region resolution: explicit ``aws_region=`` constructor arg, falling
    back to ``AWS_REGION`` and ``AWS_DEFAULT_REGION``. We do NOT pick a
    default region — if neither the constructor nor the env supplies one,
    the SDK raises a clear error on first call. Silent defaults to
    ``us-east-1`` hide latency / billing surprises.

    Model resolution order (highest priority first):
      1. Explicit ``model=`` constructor arg.
      2. ``tier=`` constructor arg, resolved via :data:`BEDROCK_MODEL_TIERS`.
      3. The module default (:data:`DEFAULT_TIER` → Sonnet inference profile).
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        tier: str | None = None,
        aws_region: str | None = None,
        aws_profile: str | None = None,
        aws_access_key: str | None = None,
        aws_secret_key: str | None = None,
        aws_session_token: str | None = None,
        client: AnthropicBedrock | None = None,
    ) -> None:
        """Construct a BedrockProvider.

        Parameters mirror :class:`AnthropicProvider` for the model/tier pair
        and add the AWS-specific knobs. All AWS auth args are pass-through
        to ``AnthropicBedrock``; supply only what you cannot get from the
        env / instance profile.

        Raises:
            LLMProviderError: ``anthropic[bedrock]`` is not installed.
        """
        if model is not None:
            self._model = model
        else:
            self._model = resolve_model_for_tier(
                tier or DEFAULT_TIER, bedrock=True
            )

        if client is not None:
            self._client = client
            return

        # Lazy import — only Bedrock users pay the import cost.
        # `AnthropicBedrock` is the documented public entry point per the
        # anthropic-sdk-python README, even though Pyright's
        # reportPrivateImportUsage rule flags it as private.
        try:
            from anthropic import (
                AnthropicBedrock as _AnthropicBedrock,  # pyright: ignore[reportPrivateImportUsage]
            )
        except ImportError as exc:
            raise LLMProviderError(
                "BedrockProvider requires the bedrock extras. Install with:\n"
                "    pip install 'anthropic[bedrock]'\n"
                "or, if you installed anvil from PyPI:\n"
                "    pip install 'anvil[bedrock]'\n"
                "then re-run."
            ) from exc

        # Pass each AWS knob explicitly (not via `**dict`) so Pyright can
        # bind each value to its typed slot — `AnthropicBedrock.__init__`
        # has many other parameters (timeout, max_retries, http_client) that
        # a `**dict[str, str]` splat would falsely shadow.
        # Passing `None` for an unset arg is safe: it matches the SDK's
        # default and lets the boto3 credential chain run normally.
        self._client = _AnthropicBedrock(
            aws_region=aws_region,
            aws_profile=aws_profile,
            aws_access_key=aws_access_key,
            aws_secret_key=aws_secret_key,
            aws_session_token=aws_session_token,
        )

    def generate(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> LLMResponse:
        """Run a single-shot completion against Bedrock.

        Wire shape matches :meth:`AnthropicProvider.generate` because the
        Bedrock client exposes the same ``messages.create`` interface.
        Prompt caching is enabled on the system block — Bedrock honors
        ``cache_control: {"type": "ephemeral"}`` for models that support
        it (Claude 4.x family).
        """
        system_blocks: list[TextBlockParam] = [
            cast(
                TextBlockParam,
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                },
            )
        ]

        try:
            msg = self._client.messages.create(
                model=self._model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system_blocks,
                messages=[{"role": "user", "content": user}],
            )
        except anthropic.AnthropicError as exc:
            raise LLMProviderError(
                f"Bedrock API call failed: {type(exc).__name__}: {exc}"
            ) from exc

        text = ""
        for block in msg.content:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                text = getattr(block, "text", "")
                break

        cached = getattr(msg.usage, "cache_read_input_tokens", 0) or 0
        non_cached_input = msg.usage.input_tokens or 0

        try:
            return LLMResponse(
                text=text,
                input_tokens=non_cached_input,
                cached_input_tokens=cached,
                output_tokens=msg.usage.output_tokens or 0,
                model=msg.model,
                finish_reason=msg.stop_reason or "unknown",
            )
        except ValidationError as exc:
            raise LLMProviderError(
                f"Bedrock response failed schema validation: {exc}"
            ) from exc


# ---------------------------------------------------------------------------
# CustomEndpointProvider — OpenAI-compatible /v1/chat/completions
# ---------------------------------------------------------------------------


class CustomEndpointProvider:
    """LLMProvider for any OpenAI-compatible ``/v1/chat/completions`` endpoint.

    Targets vLLM, LiteLLM proxy, Together, OpenRouter, Groq, local llama.cpp
    servers, Azure OpenAI deployments, and similar. The 2026 community
    consensus is that the OpenAI SDK with ``base_url=`` is the right shim;
    importing LiteLLM into a library is an anti-pattern (pulls 200+ deps and
    replaces the client surface).

    What does NOT carry over from the Anthropic path:

    * **No prompt-cache cache_control field.** OpenAI's API does not have
      one; servers that auto-cache do so based on prompt prefix matching.
      We send a plain ``system`` message — that is the most-portable shape
      and lets servers that DO have their own cache header still work.
    * **No ``cached_input_tokens`` accounting.** OpenAI usage objects report
      a single ``prompt_tokens`` field; we map it to ``input_tokens`` and
      set ``cached_input_tokens=0``. Servers that expose finer accounting
      (some vLLM builds) can be supported later via a kwarg.

    Optional install: ``pip install 'anvil[custom]'`` pulls in
    ``openai>=1.0``. Lazy import — only custom-endpoint users pay for it.

    Auth resolution order:
      1. Explicit ``api_key=`` constructor arg.
      2. ``CUSTOM_LLM_API_KEY`` env (the conventional anvil name).
      3. ``OPENAI_API_KEY`` env (so the SDK's default also works).

    base_url resolution order:
      1. Explicit ``base_url=`` constructor arg.
      2. ``CUSTOM_LLM_BASE_URL`` env.
      3. **Error**: there is no sensible default — fail rather than silently
         hit api.openai.com when the user clearly meant their local server.

    Model: required. There is no portable default model name across every
    OpenAI-compatible endpoint (``gpt-4o`` is OpenAI-specific, vLLM exposes
    whatever was loaded, OpenRouter uses route names like
    ``anthropic/claude-sonnet-4-6``). Callers MUST pass an explicit model.
    """

    def __init__(
        self,
        *,
        model: str,
        base_url: str | None = None,
        api_key: str | None = None,
        client: OpenAI | None = None,
    ) -> None:
        """Construct a CustomEndpointProvider.

        Raises:
            LLMProviderError: ``openai`` is not installed.
            ValueError: ``base_url`` is missing (neither arg nor env) when
                ``client`` is also not supplied.
        """
        if not model:
            raise ValueError(
                "CustomEndpointProvider requires an explicit `model` argument. "
                "There is no portable default for OpenAI-compatible endpoints."
            )
        self._model = model

        if client is not None:
            self._client = client
            return

        # Lazy import — only custom-endpoint users pay the import cost.
        try:
            from openai import OpenAI as _OpenAI
        except ImportError as exc:
            raise LLMProviderError(
                "CustomEndpointProvider requires the openai SDK. Install with:\n"
                "    pip install openai\n"
                "or, if you installed anvil from PyPI:\n"
                "    pip install 'anvil[custom]'\n"
                "then re-run."
            ) from exc

        resolved_base = base_url or os.environ.get("CUSTOM_LLM_BASE_URL")
        if not resolved_base:
            raise ValueError(
                "CustomEndpointProvider requires `base_url=` or "
                "CUSTOM_LLM_BASE_URL env var. There is no default — set the "
                "full /v1 root of your OpenAI-compatible endpoint, e.g. "
                "http://localhost:8000/v1 (vLLM) or https://openrouter.ai/api/v1."
            )

        resolved_key = (
            api_key
            or os.environ.get("CUSTOM_LLM_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            # The openai SDK requires a non-empty key, even for endpoints that
            # do not check it (local vLLM). Use a sentinel; servers ignore it.
            or "EMPTY"
        )

        self._client = _OpenAI(base_url=resolved_base, api_key=resolved_key)

    def generate(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> LLMResponse:
        """Run a single-shot completion against an OpenAI-compatible endpoint.

        Lazy-imports ``openai`` errors so we can wrap them in
        :class:`LLMProviderError` without an unconditional top-level import.
        """
        try:
            from openai import OpenAIError
        except ImportError as exc:  # pragma: no cover — covered in __init__
            raise LLMProviderError("openai SDK not installed") from exc

        try:
            resp = self._client.chat.completions.create(
                model=self._model,
                max_tokens=max_tokens,
                temperature=temperature,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
        except OpenAIError as exc:
            raise LLMProviderError(
                f"Custom endpoint call failed: {type(exc).__name__}: {exc}"
            ) from exc

        # OpenAI's response shape: choices[0].message.content + .usage.
        # Defensive: some endpoints (vLLM 0.4.x) return choices=[] on error
        # without raising — surface that as an LLMProviderError rather than
        # an IndexError.
        if not resp.choices:
            raise LLMProviderError(
                "Custom endpoint returned no choices (empty completion)."
            )

        choice = resp.choices[0]
        text = choice.message.content or ""
        finish = choice.finish_reason or "unknown"

        usage = resp.usage
        prompt_tokens = (usage.prompt_tokens if usage else 0) or 0
        completion_tokens = (usage.completion_tokens if usage else 0) or 0

        try:
            return LLMResponse(
                text=text,
                input_tokens=prompt_tokens,
                # OpenAI-style endpoints do not report cache hits separately.
                cached_input_tokens=0,
                output_tokens=completion_tokens,
                model=resp.model,
                finish_reason=finish,
            )
        except ValidationError as exc:
            raise LLMProviderError(
                f"Custom endpoint response failed schema validation: {exc}"
            ) from exc


# ---------------------------------------------------------------------------
# RecordedLLMProvider — deterministic test double
# ---------------------------------------------------------------------------


class RecordedLLMProvider:
    """Deterministic ``LLMProvider`` for tests.

    Tests build a ``{key: LLMResponse}`` map where the key is a sha256
    of length-prefixed (``system``, ``user``, ``max_tokens``, ``temperature``)
    byte strings, then inject the provider.  On a key miss the provider raises
    ``LLMProviderError`` so tests fail loudly rather than silently calling out
    to a real API.

    Tuning args (``max_tokens``, ``temperature``) are part of the canonical
    key as of Phase 9 (was Phase 7 C2 deferral): two recordings produced
    under different tuning args MUST yield different keys.  Tests that
    pre-compute a key with :meth:`record_key` MUST pass the same tuning
    values the engine will pass at lookup time.
    """

    def __init__(self, recordings: dict[str, LLMResponse]) -> None:
        # Copy to insulate the provider from caller-side mutation.
        self._recordings: dict[str, LLMResponse] = dict(recordings)

    @classmethod
    def record_key(
        cls,
        system: str,
        user: str,
        *,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> str:
        """Stable lookup key for a (system, user, max_tokens, temperature) tuple.

        Length-prefixed encoding is collision-free across any byte boundary —
        a separator-based scheme could collide if ``system`` ended with the
        separator-suffix and ``user`` began with its prefix.  Two calls with
        the same inputs always return the same hex digest, regardless of
        process / interpreter / hash randomization.

        Defaults for ``max_tokens`` and ``temperature`` mirror the
        :meth:`LLMProvider.generate` defaults so callers can omit them in the
        common case where the engine also relies on defaults.  Callers (or
        tests) MUST pass the same explicit values the engine uses when the
        engine overrides the defaults — e.g. ``_SCORE_EXPLAIN_MAX_TOKENS``
        (300), ``_DESCRIPTION_ENRICH_MAX_TOKENS`` (400), or
        ``_EXPAND_MAX_TOKENS`` (2000) — otherwise the recorded key will not
        match the engine's lookup key and the test will see a
        ``LLMProviderError`` for a missing recording.

        ``temperature`` is normalised via ``repr(float(...))`` so ``0``,
        ``0.0``, and ``0.00`` collapse to the same canonical encoding.
        """
        h = hashlib.sha256()
        sys_bytes = system.encode("utf-8")
        usr_bytes = user.encode("utf-8")
        # Canonical int repr — str(int) is round-trip stable across platforms.
        mt_bytes = str(int(max_tokens)).encode("utf-8")
        # Canonical float repr — repr(float(...)) is the spec-conformant
        # round-tripping representation in Python 3.
        temp_bytes = repr(float(temperature)).encode("utf-8")
        for chunk in (sys_bytes, usr_bytes, mt_bytes, temp_bytes):
            h.update(len(chunk).to_bytes(8, "big"))
            h.update(chunk)
        return h.hexdigest()

    def generate(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> LLMResponse:
        """Return the canned response for the (system, user, max_tokens,
        temperature) tuple.

        Tuning args are part of the canonical key (Phase 9 contract): two
        recordings produced under different tuning args do NOT collide.  This
        replaces the Phase 7 behavior where ``max_tokens`` and ``temperature``
        were intentionally ignored.
        """
        key = self.record_key(
            system, user, max_tokens=max_tokens, temperature=temperature
        )
        if key not in self._recordings:
            raise LLMProviderError(
                f"no recording for prompt hash {key[:8]}... "
                f"(have {len(self._recordings)} recording(s))"
            )
        return self._recordings[key]
