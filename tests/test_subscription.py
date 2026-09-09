"""Subscription billing boundaries and fail-closed Codex event parsing."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from anvil.planning import subscription as sub


def events(*items):
    return "\n".join(json.dumps(item) for item in ({"type": "turn.started"}, *items))


FINAL = {"type": "item.completed", "item": {"type": "agent_message", "text": "complete"}}
DONE = {"type": "turn.completed", "usage": {
    "input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 20,
}}


def test_subscription_environment_scrubs_overrides_without_mutating_parent(monkeypatch):
    for name in sub._API_ENV_VARS:
        monkeypatch.setenv(name, "synthetic-test-value")
    env = sub.subscription_env({"OPENAI_API_KEY": "synthetic-extra", "ANVIL_ROOT": "/scratch"})
    assert all(name not in env for name in sub._API_ENV_VARS)
    assert env["ANVIL_ROOT"] == "/scratch"
    assert os.environ["OPENAI_API_KEY"] == "synthetic-test-value"


def test_sdk_environment_masks_overrides_without_mutating_parent(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "synthetic-test-value")
    env = sub.claude_subscription_env({"OPENAI_API_KEY": "synthetic-test-value"})
    assert all(env[name] == "" for name in sub._API_ENV_VARS)
    assert os.environ["ANTHROPIC_AUTH_TOKEN"] == "synthetic-test-value"


@pytest.mark.parametrize("provider,stdout,stderr,accepted", [
    ("codex", "", "Logged in using ChatGPT", True),
    ("codex", "", "Logged in using an API key", False),
    ("codex", "", "Not logged in", False),
    ("claude", '{"loggedIn":true,"authMethod":"claude.ai","apiProvider":"firstParty","subscriptionType":"pro"}', "", True),
    ("claude", '{"loggedIn":true,"authMethod":"api_key","apiProvider":"firstParty","subscriptionType":"pro"}', "", False),
    ("claude", '{"loggedIn":true,"authMethod":"claude.ai","apiProvider":"bedrock"}', "", False),
    ("claude", '{"loggedIn":true,"authMethod":"claude.ai","apiProvider":"firstParty",'
     '"subscriptionType":null,"apiKeySource":"/login managed key"}', "", False),
    ("claude", '{"loggedIn":true,"authMethod":"claude.ai","apiProvider":"firstParty",'
     '"subscriptionType":"pro","apiKeySource":"apiKeyHelper"}', "", False),
    ("claude", "not-json", "", False),
])
def test_auth_status_gate(monkeypatch, tmp_path, provider, stdout, stderr, accepted):
    monkeypatch.setattr(sub.shutil, "which", lambda *a, **k: "/fake/cli")
    monkeypatch.setattr(sub.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        [], 0, stdout, stderr,
    ))
    if accepted:
        assert sub.require_subscription(provider, env={}, cwd=tmp_path) == "/fake/cli"
    else:
        with pytest.raises(sub.SubscriptionError):
            sub.require_subscription(provider, env={}, cwd=tmp_path)


def test_codex_usage_and_final_phase():
    commentary = {"type": "item.completed", "item": {
        "type": "agent_message", "phase": "commentary", "text": "working",
    }}
    result = sub.parse_codex_events(events(commentary, FINAL, DONE))
    assert result.text == "complete"
    assert (result.input_tokens, result.cached_input_tokens, result.output_tokens) == (60, 40, 20)


@pytest.mark.parametrize("stream", [
    "", "garbage", "[]", events(FINAL), events(DONE),
    events(DONE, FINAL), events(FINAL, DONE, FINAL), events(FINAL, DONE, DONE),
    events({"type": "item.completed", "item": None}),
    events({"type": "item.completed", "item": {"type": "agent_message", "text": None}}),
    events(FINAL, {"type": "turn.failed"}), events(FINAL, DONE, {"type": "error"}),
    events(FINAL, DONE, {"type": "turn.started"}),
    events(FINAL, {"type": "turn.completed", "usage": {"input_tokens": 2, "cached_input_tokens": 3}}),
    events(FINAL, {"type": "turn.completed", "usage": {"input_tokens": -1}}),
    events({"type": "item.completed", "item": {"type": "command_execution"}}, FINAL, DONE),
])
def test_rejects_partial_failed_malformed_and_tool_output(stream):
    with pytest.raises(sub.SubscriptionError):
        sub.parse_codex_events(stream)


def test_runner_pins_subscription_model_and_scrubs_child_environment(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setenv("CODEX_API_KEY", "synthetic-key")
    monkeypatch.setattr(sub, "require_subscription", lambda *a, **k: "/fake/codex")

    class Process:
        returncode = 0
        stdin = None
        def __init__(self, argv, **kwargs):
            captured.update(argv=argv, **kwargs)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def wait(self, timeout):
            captured.update(prompt=captured["stdin"].read(), timeout=timeout)
            captured["stdout"].write(events(FINAL, DONE))
            captured["stdout"].flush()
            return None, None

    monkeypatch.setattr(sub.subprocess, "Popen", Process)
    assert sub.run_codex("input", cwd=tmp_path, system="contract").text == "complete"
    argv = captured["argv"]
    assert argv[argv.index("--model") + 1] == "gpt-6-astra"
    assert 'forced_login_method="chatgpt"' in argv
    assert "--ignore-user-config" in argv and "--ephemeral" in argv
    assert "features.shell_tool=false" in argv
    assert "features.apply_patch_freeform=false" in argv
    assert "CODEX_API_KEY" not in captured["env"]
    assert captured["prompt"] == "input"
    assert argv[-1] == "-"
    assert captured["cwd"] == Path(tmp_path)


@pytest.mark.parametrize("windows", [False, True])
def test_timeout_terminates_tree_and_bounds_wait(monkeypatch, windows):
    from unittest.mock import Mock
    process = Mock(pid=123)
    process.poll.return_value = None
    terminate = Mock()
    monkeypatch.setattr(sub.subprocess, "run", terminate)
    killpg = Mock()
    monkeypatch.setattr(sub.os, "killpg", killpg, raising=False)
    sub._terminate_process_tree(process, windows=windows)
    process.kill.assert_called_once()
    process.wait.assert_called_once_with(timeout=5)
    if windows:
        assert terminate.call_args.args[0] == ["taskkill", "/PID", "123", "/T", "/F"]
        assert terminate.call_args.kwargs["timeout"] == 5
    else:
        killpg.assert_called_once_with(123, sub.signal.SIGKILL)


def test_runner_timeout_has_no_unbounded_pipe_drain(monkeypatch, tmp_path):
    from unittest.mock import Mock
    process = Mock(stdin=None)
    process.wait.side_effect = subprocess.TimeoutExpired("codex", 1)
    monkeypatch.setattr(sub, "require_subscription", lambda *a, **k: "/fake/codex")
    monkeypatch.setattr(sub.subprocess, "Popen", Mock(return_value=process))
    terminate = Mock()
    monkeypatch.setattr(sub, "_terminate_process_tree", terminate)
    with pytest.raises(sub.SubscriptionError, match="timed out"):
        sub.run_codex("input", cwd=tmp_path, timeout=1)
    terminate.assert_called_once()
    assert process.wait.call_count == 1
    process.communicate.assert_not_called()
