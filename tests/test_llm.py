"""Tests for anvil.planning.llm — Phase 7 Wave 1 LLM abstraction.

Coverage:
- ``LLMResponse`` Pydantic model validates as expected.
- ``LLMProvider`` Protocol structurally accepts both concrete impls.
- ``RecordedLLMProvider``: hit, miss, and stable ``record_key()``.
- ``AnthropicProvider``: happy path (mocked SDK), cache_control kwargs
  inspection, and SDK-error wrapping into ``LLMProviderError``.

No live API calls are made — the Anthropic SDK is mocked via
``unittest.mock.patch.object`` on the underlying ``messages.create``.
This mirrors the in-process mocking pattern already established by
``test_claims.py`` / ``test_sqlite.py`` for non-HTTP collaborators.
"""

from __future__ import annotations

import importlib.util as _ilu
import os
import unittest.mock as _mock
from typing import Literal

import anthropic
import pytest
from anthropic.types import Message, TextBlock, Usage

from anvil.planning.llm import (
    AnthropicProvider,
    LLMProvider,
    LLMProviderError,
    LLMResponse,
    RecordedLLMProvider,
)

_agent_sdk_available = _ilu.find_spec("claude_agent_sdk") is not None
_skip_no_agent_sdk = pytest.mark.skipif(
    not _agent_sdk_available,
    reason="claude-agent-sdk not installed (it is a core dep; install it)",
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_StopReason = Literal[
    "end_turn", "max_tokens", "stop_sequence", "tool_use", "pause_turn", "refusal"
]


def _make_sdk_message(
    *,
    text: str = "ok",
    input_tokens: int = 100,
    cache_read_input_tokens: int = 0,
    output_tokens: int = 50,
    model: str = "claude-sonnet-4-6",
    stop_reason: _StopReason = "end_turn",
) -> Message:
    """Build a real anthropic.types.Message for happy-path tests."""
    return Message(
        id="msg_test_001",
        type="message",
        role="assistant",
        model=model,
        content=[TextBlock(type="text", text=text, citations=None)],
        stop_reason=stop_reason,
        stop_sequence=None,
        usage=Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
            cache_creation_input_tokens=0,
        ),
    )


def _make_response(text: str = "canned") -> LLMResponse:
    return LLMResponse(
        text=text,
        input_tokens=10,
        cached_input_tokens=0,
        output_tokens=5,
        model="claude-sonnet-4-6",
        finish_reason="end_turn",
    )


# ---------------------------------------------------------------------------
# LLMResponse — Pydantic model
# ---------------------------------------------------------------------------


class TestLLMResponse:
    def test_constructs_with_all_fields(self) -> None:
        resp = LLMResponse(
            text="hello",
            input_tokens=12,
            cached_input_tokens=4,
            output_tokens=7,
            model="claude-sonnet-4-6",
            finish_reason="end_turn",
        )
        assert resp.text == "hello"
        assert resp.input_tokens == 12
        assert resp.cached_input_tokens == 4
        assert resp.output_tokens == 7
        assert resp.model == "claude-sonnet-4-6"
        assert resp.finish_reason == "end_turn"

    def test_rejects_negative_token_counts(self) -> None:
        with pytest.raises(Exception):  # noqa: B017 — pydantic ValidationError
            LLMResponse(
                text="x",
                input_tokens=-1,
                cached_input_tokens=0,
                output_tokens=0,
                model="m",
                finish_reason="r",
            )

    def test_round_trips_via_model_dump(self) -> None:
        """JSON-serialisable: the response can survive a model_dump → reload."""
        r = _make_response("round-trip")
        dumped = r.model_dump()
        rebuilt = LLMResponse(**dumped)
        assert rebuilt == r


# ---------------------------------------------------------------------------
# LLMProvider Protocol — structural typing check
# ---------------------------------------------------------------------------


class TestLLMProviderProtocol:
    def test_recorded_provider_satisfies_protocol(self) -> None:
        """RecordedLLMProvider is structurally an LLMProvider."""
        rec = RecordedLLMProvider({})
        # Mypy/pyright check at type time; runtime check via attribute presence
        # since Protocol is not @runtime_checkable here (intentionally — we
        # don't want isinstance() on Protocols, the type-check is the spec).
        assert hasattr(rec, "generate")
        assert callable(rec.generate)
        # Assignment to LLMProvider-typed variable: caught by static analysis.
        provider: LLMProvider = rec
        assert provider is rec

    def test_anthropic_provider_satisfies_protocol(self) -> None:
        """AnthropicProvider also satisfies the LLMProvider Protocol."""
        # Build with an inert client so no env var is required.
        prov = AnthropicProvider(client=_mock.MagicMock(spec=anthropic.Anthropic))
        assert hasattr(prov, "generate")
        provider: LLMProvider = prov
        assert provider is prov


# ---------------------------------------------------------------------------
# RecordedLLMProvider — hit, miss, key stability
# ---------------------------------------------------------------------------


class TestRecordedLLMProviderHits:
    def test_returns_canned_response_on_hit(self) -> None:
        system = "You are a planning helper."
        user = "Score task T001."
        key = RecordedLLMProvider.record_key(system, user)
        canned = _make_response("scored: 7/10")

        prov = RecordedLLMProvider({key: canned})
        got = prov.generate(system=system, user=user)
        assert got == canned

    def test_distinguishes_max_tokens_and_temperature(self) -> None:
        """Tuning args are part of the canonical key (Phase 9 C2 fix).

        Inverted from the Phase 7 ``test_ignores_max_tokens_and_temperature``:
        under the new contract, a recording made under one (max_tokens,
        temperature) pair must NOT satisfy a lookup made under a different
        pair.  The recorded provider raises ``LLMProviderError`` on the
        mismatched lookup so silent collisions become loud test failures.
        """
        system = "S"
        user = "U"
        # Record under (max_tokens=1, temperature=0.0).
        key = RecordedLLMProvider.record_key(
            system, user, max_tokens=1, temperature=0.0
        )
        canned = _make_response()
        prov = RecordedLLMProvider({key: canned})

        # Matching tuning args → hit.
        hit = prov.generate(system=system, user=user, max_tokens=1, temperature=0.0)
        assert hit == canned

        # Mismatched max_tokens → miss.
        with pytest.raises(LLMProviderError, match="no recording for prompt hash"):
            prov.generate(system=system, user=user, max_tokens=9999, temperature=0.0)

        # Mismatched temperature → miss.
        with pytest.raises(LLMProviderError, match="no recording for prompt hash"):
            prov.generate(system=system, user=user, max_tokens=1, temperature=0.9)

    def test_constructor_copies_recordings(self) -> None:
        """Mutating the source dict after construction must not leak in."""
        system = "S"
        user = "U"
        key = RecordedLLMProvider.record_key(system, user)
        canned = _make_response()
        src = {key: canned}

        prov = RecordedLLMProvider(src)
        src.clear()  # mutate source

        # Provider still has the recording.
        got = prov.generate(system=system, user=user)
        assert got == canned


class TestRecordedLLMProviderMisses:
    def test_miss_raises_llm_provider_error(self) -> None:
        prov = RecordedLLMProvider({})
        with pytest.raises(LLMProviderError, match="no recording for prompt hash"):
            prov.generate(system="S", user="U")

    def test_miss_error_includes_hash_prefix(self) -> None:
        prov = RecordedLLMProvider({})
        expected_prefix = RecordedLLMProvider.record_key("S", "U")[:8]
        with pytest.raises(LLMProviderError) as exc_info:
            prov.generate(system="S", user="U")
        assert expected_prefix in str(exc_info.value)


class TestRecordedLLMProviderKey:
    def test_same_inputs_same_key(self) -> None:
        k1 = RecordedLLMProvider.record_key("system prompt", "user prompt")
        k2 = RecordedLLMProvider.record_key("system prompt", "user prompt")
        assert k1 == k2

    def test_different_system_different_key(self) -> None:
        k1 = RecordedLLMProvider.record_key("A", "U")
        k2 = RecordedLLMProvider.record_key("B", "U")
        assert k1 != k2

    def test_different_user_different_key(self) -> None:
        k1 = RecordedLLMProvider.record_key("S", "A")
        k2 = RecordedLLMProvider.record_key("S", "B")
        assert k1 != k2

    def test_separator_prevents_collision(self) -> None:
        """Length-prefixed encoding prevents concat-collisions across any byte boundary.

        The Phase 7 implementation used a ``"\\n---\\n"`` literal separator;
        Phase 7 follow-up replaced that with length-prefixed encoding (each
        chunk preceded by its 8-byte big-endian length).  Either scheme
        defeats the naive concat collision — ``("ab", "c")`` and
        ``("a", "bc")`` would hash the same under raw concatenation but the
        length-prefix makes them distinguishable.  The test name is kept for
        ``git blame`` continuity; the assertion is the canonical no-collision
        proof regardless of which encoding shipped.
        """
        k1 = RecordedLLMProvider.record_key("ab", "c")
        k2 = RecordedLLMProvider.record_key("a", "bc")
        assert k1 != k2

    def test_key_is_hex_sha256(self) -> None:
        k = RecordedLLMProvider.record_key("S", "U")
        # sha256 hex = 64 chars
        assert len(k) == 64
        assert all(c in "0123456789abcdef" for c in k)

    def test_different_max_tokens_different_key(self) -> None:
        """Phase 9 C2 regression: tuning args participate in the key.

        Two recordings made with the same (system, user) but different
        ``max_tokens`` MUST yield different hashes so they do not silently
        collide in the recordings map.
        """
        k1 = RecordedLLMProvider.record_key("S", "U", max_tokens=100)
        k2 = RecordedLLMProvider.record_key("S", "U", max_tokens=200)
        assert k1 != k2

    def test_different_temperature_different_key(self) -> None:
        """Phase 9 C2 regression: temperature participates in the key."""
        k1 = RecordedLLMProvider.record_key("S", "U", temperature=0.0)
        k2 = RecordedLLMProvider.record_key("S", "U", temperature=0.7)
        assert k1 != k2

    def test_default_tuning_args_match_explicit_defaults(self) -> None:
        """Omitting tuning args yields the same key as passing the defaults.

        Guarantees back-compat for the no-kwargs call style: tests that
        pre-compute keys via ``record_key(system, user)`` continue to match a
        ``generate(system=..., user=...)`` call that also uses the defaults.
        """
        k_implicit = RecordedLLMProvider.record_key("S", "U")
        k_explicit = RecordedLLMProvider.record_key(
            "S", "U", max_tokens=4096, temperature=0.0
        )
        assert k_implicit == k_explicit

    def test_temperature_int_and_float_zero_collapse(self) -> None:
        """``temperature=0`` and ``temperature=0.0`` produce the same key.

        ``record_key`` normalises temperature via ``repr(float(...))`` so
        callers can pass an int without changing the hash output.
        """
        k_int = RecordedLLMProvider.record_key("S", "U", temperature=0)
        k_float = RecordedLLMProvider.record_key("S", "U", temperature=0.0)
        assert k_int == k_float


# ---------------------------------------------------------------------------
# AnthropicProvider — happy path (mocked SDK)
# ---------------------------------------------------------------------------


class TestAnthropicProviderHappyPath:
    def test_returns_llm_response_from_sdk_message(self) -> None:
        sdk_client = _mock.MagicMock(spec=anthropic.Anthropic)
        sdk_client.messages.create.return_value = _make_sdk_message(
            text="planning advice",
            input_tokens=120,
            cache_read_input_tokens=80,
            output_tokens=30,
        )

        prov = AnthropicProvider(client=sdk_client)
        resp = prov.generate(system="planner system", user="please help")

        assert isinstance(resp, LLMResponse)
        assert resp.text == "planning advice"
        assert resp.cached_input_tokens == 80
        # SDK reports input_tokens=120 already excludes the cached portion in
        # the current contract — provider returns it as-is (via max() guard).
        assert resp.input_tokens == 120
        assert resp.output_tokens == 30
        assert resp.model == "claude-sonnet-4-6"
        assert resp.finish_reason == "end_turn"

    def test_uses_configured_model(self) -> None:
        sdk_client = _mock.MagicMock(spec=anthropic.Anthropic)
        sdk_client.messages.create.return_value = _make_sdk_message(model="claude-opus-4")

        prov = AnthropicProvider(model="claude-opus-4", client=sdk_client)
        prov.generate(system="s", user="u")

        kwargs = sdk_client.messages.create.call_args.kwargs
        assert kwargs["model"] == "claude-opus-4"

    def test_passes_max_tokens_and_temperature(self) -> None:
        sdk_client = _mock.MagicMock(spec=anthropic.Anthropic)
        sdk_client.messages.create.return_value = _make_sdk_message()

        prov = AnthropicProvider(client=sdk_client)
        prov.generate(system="s", user="u", max_tokens=1234, temperature=0.5)

        kwargs = sdk_client.messages.create.call_args.kwargs
        assert kwargs["max_tokens"] == 1234
        assert kwargs["temperature"] == 0.5

    def test_default_temperature_is_zero(self) -> None:
        """Augmentation should be deterministic by default."""
        sdk_client = _mock.MagicMock(spec=anthropic.Anthropic)
        sdk_client.messages.create.return_value = _make_sdk_message()

        prov = AnthropicProvider(client=sdk_client)
        prov.generate(system="s", user="u")

        kwargs = sdk_client.messages.create.call_args.kwargs
        assert kwargs["temperature"] == 0.0

    def test_extracts_text_from_first_text_block(self) -> None:
        """The SDK returns content as a list; we extract the first text block."""
        sdk_client = _mock.MagicMock(spec=anthropic.Anthropic)
        sdk_client.messages.create.return_value = _make_sdk_message(text="first block")

        prov = AnthropicProvider(client=sdk_client)
        resp = prov.generate(system="s", user="u")
        assert resp.text == "first block"

    def test_zero_cache_read_when_cold(self) -> None:
        sdk_client = _mock.MagicMock(spec=anthropic.Anthropic)
        sdk_client.messages.create.return_value = _make_sdk_message(
            cache_read_input_tokens=0,
        )

        prov = AnthropicProvider(client=sdk_client)
        resp = prov.generate(system="s", user="u")
        assert resp.cached_input_tokens == 0


# ---------------------------------------------------------------------------
# AnthropicProvider — prompt-cache breakpoint on system block
# ---------------------------------------------------------------------------


class TestAnthropicProviderPromptCache:
    """superpowers:claude-api skill rule: cache_control on system block."""

    def test_system_block_has_ephemeral_cache_control(self) -> None:
        sdk_client = _mock.MagicMock(spec=anthropic.Anthropic)
        sdk_client.messages.create.return_value = _make_sdk_message()

        prov = AnthropicProvider(client=sdk_client)
        prov.generate(system="planner system prompt", user="u")

        kwargs = sdk_client.messages.create.call_args.kwargs
        system = kwargs["system"]

        # System is sent as a list of typed blocks so cache_control attaches.
        assert isinstance(system, list)
        assert len(system) == 1
        block = system[0]
        assert block["type"] == "text"
        assert block["text"] == "planner system prompt"
        assert block["cache_control"] == {"type": "ephemeral"}

    def test_user_prompt_passed_as_messages_array(self) -> None:
        sdk_client = _mock.MagicMock(spec=anthropic.Anthropic)
        sdk_client.messages.create.return_value = _make_sdk_message()

        prov = AnthropicProvider(client=sdk_client)
        prov.generate(system="s", user="what is the next task?")

        kwargs = sdk_client.messages.create.call_args.kwargs
        messages = kwargs["messages"]
        assert messages == [{"role": "user", "content": "what is the next task?"}]


# ---------------------------------------------------------------------------
# AnthropicProvider — error wrapping
# ---------------------------------------------------------------------------


class TestAnthropicProviderErrorWrapping:
    def test_api_connection_error_wraps_in_llm_provider_error(self) -> None:
        sdk_client = _mock.MagicMock(spec=anthropic.Anthropic)
        sdk_client.messages.create.side_effect = anthropic.APIConnectionError(
            request=_mock.MagicMock()
        )

        prov = AnthropicProvider(client=sdk_client)
        with pytest.raises(LLMProviderError, match="Anthropic API call failed"):
            prov.generate(system="s", user="u")

    def test_anthropic_error_subclass_wraps(self) -> None:
        """Any AnthropicError subclass should be wrapped, not just APIError."""
        sdk_client = _mock.MagicMock(spec=anthropic.Anthropic)
        # APIError is the broadest practical class — covers status errors.
        sdk_client.messages.create.side_effect = anthropic.APIError(
            message="boom",
            request=_mock.MagicMock(),
            body=None,
        )

        prov = AnthropicProvider(client=sdk_client)
        with pytest.raises(LLMProviderError) as exc_info:
            prov.generate(system="s", user="u")

        # Original exception is chained for debugging.
        assert exc_info.value.__cause__ is not None
        assert isinstance(exc_info.value.__cause__, anthropic.AnthropicError)

    def test_error_message_includes_original_type_name(self) -> None:
        sdk_client = _mock.MagicMock(spec=anthropic.Anthropic)
        sdk_client.messages.create.side_effect = anthropic.APIConnectionError(
            request=_mock.MagicMock()
        )

        prov = AnthropicProvider(client=sdk_client)
        with pytest.raises(LLMProviderError, match="APIConnectionError"):
            prov.generate(system="s", user="u")


# ---------------------------------------------------------------------------
# AnthropicProvider — API key resolution
# ---------------------------------------------------------------------------


class TestAnthropicProviderApiKeyResolution:
    def test_explicit_api_key_used_when_provided(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Explicit api_key= overrides the env var."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")
        with _mock.patch("anvil.planning.llm.anthropic.Anthropic") as mock_ctor:
            AnthropicProvider(api_key="explicit-key")
            mock_ctor.assert_called_once_with(api_key="explicit-key")

    def test_env_var_used_when_no_explicit_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")
        with _mock.patch("anvil.planning.llm.anthropic.Anthropic") as mock_ctor:
            AnthropicProvider()
            mock_ctor.assert_called_once_with(api_key="env-key")

    def test_none_passed_when_no_explicit_and_no_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SDK gets None so it can surface its own helpful auth error."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with _mock.patch("anvil.planning.llm.anthropic.Anthropic") as mock_ctor:
            AnthropicProvider()
            mock_ctor.assert_called_once_with(api_key=None)


# ---------------------------------------------------------------------------
# ClaudeAgentSDKProvider — subscription path (mocked claude_agent_sdk.query)
# ---------------------------------------------------------------------------
#
# We patch ``claude_agent_sdk.query`` with a fake async generator yielding real
# SDK message objects. No CLI, no subprocess, no subscription — the provider's
# import-deferral means construction never touches the CLI, and the async-drive
# + usage-mapping logic is exercised against constructed messages.


@_skip_no_agent_sdk
class TestClaudeAgentSDKProvider:
    """Default provider: maps the Agent SDK message stream to LLMResponse,
    scrubs the API key for the duration of the call, and wraps failures."""

    def _messages(
        self,
        *,
        text: str = "hello",
        is_error: bool = False,
        with_result: bool = True,
        usage: dict | None = None,
    ):  # type: ignore[no-untyped-def]
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

        msgs: list = [
            AssistantMessage(
                content=[TextBlock(text=text)], model="claude-sonnet-4-6"
            )
        ]
        if with_result:
            msgs.append(
                ResultMessage(
                    subtype="error" if is_error else "success",
                    duration_ms=1,
                    duration_api_ms=1,
                    is_error=is_error,
                    num_turns=1,
                    session_id="sess-1",
                    usage=usage
                    if usage is not None
                    else {
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "cache_read_input_tokens": 2,
                    },
                    result=text,
                    stop_reason="end_turn",
                    model_usage={"claude-sonnet-4-6": {}},
                )
            )
        return msgs

    def _patch_query(self, monkeypatch, messages, capture=None):  # type: ignore[no-untyped-def]
        import claude_agent_sdk

        async def fake_query(*, prompt, options):  # type: ignore[no-untyped-def]
            if capture is not None:
                capture["api_key_present"] = "ANTHROPIC_API_KEY" in os.environ
                capture["system_prompt"] = options.system_prompt
                capture["model"] = options.model
                capture["max_turns"] = options.max_turns
                capture["allowed_tools"] = options.allowed_tools
            for m in messages:
                yield m

        monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    def test_satisfies_protocol(self) -> None:
        from anvil.planning.llm import ClaudeAgentSDKProvider

        prov: LLMProvider = ClaudeAgentSDKProvider()
        assert hasattr(prov, "generate")

    def test_model_resolution(self) -> None:
        from anvil.planning.llm import MODEL_TIERS, ClaudeAgentSDKProvider

        assert ClaudeAgentSDKProvider(tier="opus")._model == MODEL_TIERS["opus"]
        assert ClaudeAgentSDKProvider(model="custom-id")._model == "custom-id"
        # Neither model nor tier → None (use the subscription default model).
        assert ClaudeAgentSDKProvider()._model is None

    def test_generate_maps_result_to_llmresponse(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from anvil.planning.llm import ClaudeAgentSDKProvider

        self._patch_query(monkeypatch, self._messages(text="hi there"))
        resp = ClaudeAgentSDKProvider(model="claude-sonnet-4-6").generate(
            system="S", user="U"
        )
        assert resp.text == "hi there"
        assert resp.input_tokens == 10
        assert resp.cached_input_tokens == 2
        assert resp.output_tokens == 5
        assert resp.model == "claude-sonnet-4-6"
        assert resp.finish_reason == "end_turn"

    def test_generate_scrubs_key_during_call_and_restores(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from anvil.planning.llm import ClaudeAgentSDKProvider

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")
        cap: dict = {}
        self._patch_query(monkeypatch, self._messages(), capture=cap)
        ClaudeAgentSDKProvider().generate(system="SYS", user="U")
        # Scrubbed for the duration of the SDK call …
        assert cap["api_key_present"] is False
        # … and restored afterward.
        assert os.environ.get("ANTHROPIC_API_KEY") == "sk-test-fake"
        # The system prompt replaces the CLI default; single-shot, no tools.
        assert cap["system_prompt"] == "SYS"
        assert cap["max_turns"] == 1
        assert cap["allowed_tools"] == []

    def test_error_result_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from anvil.planning.llm import ClaudeAgentSDKProvider

        self._patch_query(monkeypatch, self._messages(is_error=True))
        with pytest.raises(LLMProviderError):
            ClaudeAgentSDKProvider().generate(system="S", user="U")

    def test_no_result_message_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from anvil.planning.llm import ClaudeAgentSDKProvider

        self._patch_query(monkeypatch, self._messages(with_result=False))
        with pytest.raises(LLMProviderError):
            ClaudeAgentSDKProvider().generate(system="S", user="U")

    def test_generate_works_inside_running_event_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """generate() is sync but uses anyio.run(); when called from INSIDE a
        running event loop it must offload to a worker thread rather than
        raising 'anyio.run() ... already running'. Proves the
        _run_blocking_until_complete fallback path."""
        import anyio

        from anvil.planning.llm import ClaudeAgentSDKProvider

        self._patch_query(monkeypatch, self._messages(text="loop-safe"))

        async def _call() -> LLMResponse:
            # We are now inside a running loop (anyio.run drives this coro).
            # Calling the sync generate() here must not raise.
            return ClaudeAgentSDKProvider().generate(system="S", user="U")

        resp = anyio.run(_call)
        assert resp.text == "loop-safe"

    def test_prefers_assistant_frame_model_and_stop_reason(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reliable per-turn AssistantMessage (model required, stop_reason
        populated) wins over the ResultMessage frame, which routinely leaves
        stop_reason=None and model_usage empty. Without this the response would
        degrade to finish_reason='end_turn' and model='claude-agent-sdk'."""
        import claude_agent_sdk
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

        from anvil.planning.llm import ClaudeAgentSDKProvider

        async def fake_query(*, prompt, options):  # type: ignore[no-untyped-def]
            yield AssistantMessage(
                content=[TextBlock(text="hi")],
                model="claude-opus-4-8",
                stop_reason="max_tokens",
            )
            # Result frame degraded: no stop_reason, empty model_usage.
            yield ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="s",
                usage={"input_tokens": 1, "output_tokens": 1},
                result="hi",
                stop_reason=None,
                model_usage={},
            )

        monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
        resp = ClaudeAgentSDKProvider().generate(system="S", user="U")
        assert resp.model == "claude-opus-4-8"
        assert resp.finish_reason == "max_tokens"
