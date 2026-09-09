"""Subscription-only CLI execution shared by planning and behavioral evals.

Authentication remains owned by each CLI. Never read auth files or env files.
An API credential in the shell must not change the billing path of a subscription
provider; API providers are selected separately, with explicit permission.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import signal
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

ASTRA_MODEL = "gpt-6-astra"
ReasoningEffort = Literal["low", "medium", "high", "xhigh", "max"]
REASONING_EFFORTS = ("low", "medium", "high", "xhigh", "max")

# Provider/transport overrides can bypass subscription login even without a key.
_API_ENV_VARS = (
    "OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE",
    "ANTHROPIC_API_KEY", "CLAUDE_API_KEY", "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS", "CUSTOM_LLM_API_KEY",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_API_KEY_HELPER_TTL_MS",
)


class SubscriptionError(RuntimeError):
    """The selected subscription CLI could not complete the request."""


def subscription_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build a child environment without modifying the parent's credentials."""
    env = dict(os.environ)
    env.update(extra or {})
    for name in _API_ENV_VARS:
        env.pop(name, None)
    return env


def claude_subscription_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Mask inherited overrides in the SDK's additive child environment.

    The SDK merges os.environ with options.env. Empty values disable credential
    and boolean transport overrides without temporarily mutating process state.
    """
    return {**subscription_env(extra), **dict.fromkeys(_API_ENV_VARS, "")}


def require_subscription(
    provider: Literal["codex", "claude"], *, env: Mapping[str, str], cwd: Path
) -> str:
    """Check the CLI's public login status without exposing account metadata."""
    executable = shutil.which(provider, path=env.get("PATH"))
    if executable is None:
        raise SubscriptionError(f"Install the {provider} CLI and sign in to its subscription.")
    command = (
        [executable, "login", "status"] if provider == "codex"
        else [executable, "--setting-sources", "", "auth", "status"]
    )
    try:
        result = subprocess.run(
            command, env=dict(env), cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", timeout=30,
        )
        if provider == "codex":
            valid = "Logged in using ChatGPT" in result.stdout + result.stderr
        else:
            status = json.loads(result.stdout)
            valid = (
                isinstance(status, dict) and status.get("loggedIn") is True
                and status.get("authMethod") == "claude.ai"
                and status.get("apiProvider") == "firstParty"
                and not status.get("apiKeySource")
                and status.get("subscriptionType") in ("pro", "max", "team", "enterprise")
            )
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        raise SubscriptionError(f"Could not verify {provider} subscription login.") from exc
    if result.returncode != 0 or not valid:
        raise SubscriptionError(
            f"{provider} requires subscription login; API authentication is not allowed "
            "on this provider. Sign in to ChatGPT/Claude and retry."
        )
    return executable


@dataclass
class CodexResult:
    text: str
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    num_turns: int = 0
    session_id: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)


def parse_codex_events(output: str, *, allow_tools: bool = False) -> CodexResult:
    """Only a completed turn with a final message is a usable completion."""
    result = CodexResult(text="")
    completed = False
    active = False
    try:
        for line in output.splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError("event must be an object")
            kind = event["type"]
            result.events.append(event)
            if kind in ("error", "turn.failed"):
                raise SubscriptionError("Codex reported an unsuccessful turn; no output accepted.")
            if kind == "thread.started":
                result.session_id = event.get("thread_id")
            elif kind == "turn.started":
                if active:
                    raise ValueError("nested turn")
                active = True
                completed = False
                result.text = ""
            elif kind in ("item.started", "item.updated", "item.completed"):
                if not active:
                    raise ValueError("item outside active turn")
                item = event["item"]
                if not isinstance(item, dict):
                    raise ValueError("item must be an object")
                if item["type"] == "agent_message":
                    if (kind == "item.completed"
                            and item.get("phase", "final_answer") == "final_answer"):
                        if not isinstance(item["text"], str):
                            raise ValueError("message must be text")
                        result.text = item["text"]
                elif not allow_tools and item["type"] != "reasoning":
                    raise SubscriptionError("Codex used a tool during a text-only completion.")
            elif kind == "turn.completed":
                if not active or not result.text.strip():
                    raise ValueError("completion without active turn and final text")
                active = False
                completed = True
                result.num_turns += 1
                usage = event.get("usage") or {}
                if not isinstance(usage, dict):
                    raise ValueError("usage must be an object")
                total = usage.get("input_tokens", 0)
                cached = usage.get("cached_input_tokens", 0)
                output_tokens = usage.get("output_tokens", 0)
                if any(type(n) is not int or n < 0 for n in (total, cached, output_tokens)):
                    raise ValueError("invalid token counts")
                if cached > total:
                    raise ValueError("cached tokens exceed input")
                result.input_tokens += total - cached
                result.cached_input_tokens += cached
                result.output_tokens += output_tokens
    except (ValueError, KeyError, TypeError) as exc:
        raise SubscriptionError("Codex returned malformed completion events.") from exc
    if not completed or not result.text.strip():
        raise SubscriptionError("Codex returned no completed final answer.")
    return result


def _terminate_process_tree(process: subprocess.Popen[str], *, windows: bool) -> None:
    """Bound teardown even if a launcher or tool descendant holds output open."""
    try:
        if windows:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=5, check=True,
            )
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except (OSError, subprocess.SubprocessError) as exc:
        raise SubscriptionError("Could not terminate the Codex process tree.") from exc
    finally:
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired as exc:
            raise SubscriptionError("Codex process did not stop after termination.") from exc


def run_codex(
    prompt: str, *, cwd: Path, model: str = ASTRA_MODEL,
    reasoning_effort: ReasoningEffort = "medium", timeout: float = 300,
    system: str | None = None, allow_tools: bool = False,
    extra_env: Mapping[str, str] | None = None,
) -> CodexResult:
    """Run Codex using its existing ChatGPT login, never an API key.

    Text generation runs in an empty temporary directory supplied by the caller,
    with execution/patch tools disabled. Evals explicitly enable workspace tools.
    CLI token ceilings differ from API max_output_tokens; timeout bounds the run.
    """
    if (not model.strip() or reasoning_effort not in REASONING_EFFORTS
            or not math.isfinite(timeout) or timeout <= 0):
        raise SubscriptionError("Invalid Codex model, reasoning effort, or timeout.")
    env = subscription_env(extra_env)
    executable = require_subscription("codex", env=env, cwd=cwd)
    command = [
        executable, "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral",
        "--skip-git-repo-check", "--json", "--color", "never", "--cd", str(cwd),
        "--model", model, "--sandbox", "workspace-write" if allow_tools else "read-only",
        "-c", 'model_provider="openai"', "-c", 'forced_login_method="chatgpt"',
        "-c", 'approval_policy="never"', "-c", 'web_search="disabled"',
        "-c", f'model_reasoning_effort="{reasoning_effort}"',
        "-c", "project_doc_max_bytes=0",
    ]
    if not allow_tools:
        command += ["-c", "features.shell_tool=false", "-c", "features.apply_patch_freeform=false"]
    if system is not None:
        command += ["-c", "developer_instructions=" + json.dumps(system)]
    command.append("-")
    try:
        # File-backed input/output avoids inherited pipes blocking teardown or
        # a large stdin write bypassing communicate's Windows timeout.
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stdout_file, \
                tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr_file, \
                tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stdin_file:
            stdin_file.write(prompt)
            stdin_file.seek(0)
            process = subprocess.Popen(
                command, env=env, cwd=cwd, stdin=stdin_file, stdout=stdout_file,
                stderr=stderr_file, text=True, encoding="utf-8",
                start_new_session=os.name != "nt",
            )
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _terminate_process_tree(process, windows=os.name == "nt")
                raise SubscriptionError("Codex subscription request timed out.") from None
            except BaseException:
                _terminate_process_tree(process, windows=os.name == "nt")
                raise
            if process.returncode != 0:
                raise SubscriptionError(
                    f"Codex subscription request failed (exit {process.returncode}); "
                    "verify subscription access and model availability."
                )
            stdout_file.seek(0)
            stdout = stdout_file.read()
    except OSError as exc:
        raise SubscriptionError("Could not run the Codex subscription CLI.") from exc
    return parse_codex_events(stdout, allow_tools=allow_tools)
