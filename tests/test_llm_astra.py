"""Astra provider compatibility, using the actual SDK over mock HTTP only."""

import json

import httpx
import pytest

from anvil.config import Config, load_config
from anvil.planning.llm import (
    CodexSubscriptionProvider,
    LLMProviderError,
    OpenAIResponsesProvider,
)
from anvil.planning.llm_planner import PlannerProviderUnavailable, resolve_planner_provider


def payload(**changes):
    data = {
        "id": "resp_test", "object": "response", "created_at": 0,
        "status": "completed", "model": "gpt-6-astra", "error": None,
        "incomplete_details": None, "output": [{
            "id": "msg_test", "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": "## Tasks\n\n### T001: Test", "annotations": []}],
        }],
        "usage": {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 40, "cache_write_tokens": 10},
                  "output_tokens": 30, "output_tokens_details": {"reasoning_tokens": 20}, "total_tokens": 130},
    }
    data.update(changes)
    return data


@pytest.fixture
def sdk_client():
    openai = pytest.importorskip("openai")
    clients = []
    def make(response=None, status=200):
        requests = []
        def handler(request):
            requests.append(json.loads(request.content))
            assert request.url.path == "/v1/responses"
            return httpx.Response(status, json=response if response is not None else payload())
        client = openai.OpenAI(
            api_key="synthetic-test-key", base_url="https://api.openai.test/v1", max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        clients.append(client)
        return client, requests
    yield make
    for client in clients:
        client.close()


def test_responses_wire_contract_and_usage(sdk_client):
    client, requests = sdk_client()
    provider = OpenAIResponsesProvider(client=client, reasoning_effort="max", reasoning_budget=2000)
    result = provider.generate(system="contract", user="input", max_tokens=8000)
    assert requests == [{"model": "gpt-6-astra", "instructions": "contract", "input": "input",
                         "reasoning": {"effort": "max"}, "max_output_tokens": 10000, "store": False}]
    assert result.input_tokens == 60 and result.cached_input_tokens == 40
    assert result.output_tokens == 30 and result.reasoning_tokens == 20
    assert result.cache_write_input_tokens == 10


@pytest.mark.parametrize("status", ["incomplete", "failed", "in_progress", "cancelled"])
def test_partial_output_never_reaches_planner(sdk_client, status):
    client, _ = sdk_client(payload(status=status))
    with pytest.raises(LLMProviderError, match="incomplete or failed"):
        OpenAIResponsesProvider(client=client).generate(system="s", user="u")


@pytest.mark.parametrize("content", [[], [{"type": "refusal", "refusal": "no"}]])
def test_empty_or_refusal_is_an_error(sdk_client, content):
    data = payload()
    data["output"][0]["content"] = content
    client, _ = sdk_client(data)
    with pytest.raises(LLMProviderError):
        OpenAIResponsesProvider(client=client).generate(system="s", user="u")


def test_bad_sampling_and_budget_fail_before_http(sdk_client):
    client, requests = sdk_client()
    provider = OpenAIResponsesProvider(client=client)
    for kwargs in ({"temperature": 0.1}, {"max_tokens": 0}, {"max_tokens": 128001}):
        with pytest.raises(LLMProviderError):
            provider.generate(system="s", user="u", **kwargs)
    assert requests == []


@pytest.mark.parametrize("status", [401, 429, 500])
def test_request_errors_never_trigger_provider_fallback(sdk_client, status):
    client, requests = sdk_client({"error": {"message": "failure"}}, status)
    with pytest.raises(LLMProviderError, match="request failed"):
        OpenAIResponsesProvider(client=client).generate(system="s", user="u")
    assert len(requests) == 1


@pytest.mark.parametrize("name", ["openai", "anthropic", "bedrock", "custom"])
def test_api_provider_requires_separate_explicit_permission(name):
    with pytest.raises(PlannerProviderUnavailable, match="llm_allow_api"):
        resolve_planner_provider(Config(project_name="test", project_id="test", llm_provider=name))


def test_legacy_autodetect_requires_api_permission(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-test-key")
    with pytest.raises(PlannerProviderUnavailable, match="llm_allow_api"):
        resolve_planner_provider(Config(project_name="test", project_id="test", llm_fallback=True))


def test_codex_is_explicit_and_does_not_require_api(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test-key")
    cfg = Config(project_name="test", project_id="test", llm_provider="codex")
    provider, name = resolve_planner_provider(cfg)
    assert isinstance(provider, CodexSubscriptionProvider) and name == "codex"
    assert provider._model == "gpt-6-astra"
    assert resolve_planner_provider()[1] == "agent-sdk"
    assert resolve_planner_provider(cfg, model_override="explicit-pin")[0]._model == "explicit-pin"


def test_harness_mode_prevents_nested_provider_selection(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test-key")
    cfg = Config(project_name="test", project_id="test", llm_provider="harness")
    with pytest.raises(PlannerProviderUnavailable, match="current harness"):
        resolve_planner_provider(cfg)


@pytest.mark.parametrize("extra", ["llm_allow_api: 'false'", "llm_reasoning_effort: none",
                                  "openai_reasoning_budget: -1", "openai_reasoning_budget: true"])
def test_config_rejects_invalid_astra_controls(tmp_path, extra):
    path = tmp_path / "config.yaml"
    path.write_text("project_name: t\nproject_id: t\n" + extra)
    with pytest.raises(ValueError):
        load_config(path)


def test_config_accepts_subscription_defaults(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("project_name: t\nproject_id: t\nllm_provider: codex\n")
    cfg = load_config(path)
    assert cfg.llm_allow_api is False and cfg.llm_reasoning_effort == "medium"


@pytest.mark.parametrize("output", [None, [{"type": "message", "status": "completed", "content": None}],
    [{"type": "message", "status": "completed", "content": [
        {"type": "output_text", "text": None}]}],
    [{"type": "message", "status": "completed", "content": [
        {"type": "output_text", "text": "hello"}, {"type": "unknown"}]}],
])
def test_malformed_response_stays_inside_provider_error_contract(sdk_client, output):
    client, _requests = sdk_client({**payload(), "output": output})
    with pytest.raises(LLMProviderError):
        OpenAIResponsesProvider(client=client).generate(system="s", user="u")


def test_reasoning_allowance_cannot_make_negative_output_budget_valid(sdk_client):
    client, requests = sdk_client(payload())
    with pytest.raises(LLMProviderError):
        OpenAIResponsesProvider(client=client, reasoning_budget=100).generate(
            system="s", user="u", max_tokens=-1,
        )
    assert not requests
