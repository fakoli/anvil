"""Behavioral-eval harness for anvil skills (Layer 3, opt-in / costed).

This is the COMPLEMENT to the deterministic static contract evals
(``tests/test_layout_assumptions.py``, ``tests/test_skill_cli_contract.py``).
Those check the *static* contract: that skills cite real ``anvil`` commands /
flags and never hard-code an in-repo ``.anvil/`` path. They cannot tell you
whether a real agent, handed the skill, actually drives anvil to the promised
*state*. That is what this harness does:

    isolate a throwaway anvil project
        -> drive a real Claude Code agent through one skill flow (claude-agent-sdk)
            -> assert anvil's OWN state matches the skill's promise
               (`anvil status`, the workspace prd.md, events.jsonl)

This catches semantic / instruction drift the static evals structurally cannot
(e.g. a skill whose wording leads the agent to write the PRD to the wrong place,
or never parse it, even though every command it cites is spelled correctly).

Design (slimmed from fakoli/agent-eval):
  - per-run mkdtemp isolation + context-manager cleanup (``IsolatedEnv``);
  - a subprocess agent driver returning a structured ``ExecutionTrace``
    (here: ``claude-agent-sdk`` over its bundled CLI);
  - a small set of deterministic code assertions over the resulting filesystem
    + CLI state (``Assertion`` / ``check_*``);
  - dict/YAML case definitions (``cases/*.yaml``), pass/fail (no scoring weights
    in the prototype).

Isolation strategy: we set ``ANVIL_ROOT=<scratch>`` for every anvil invocation.
ANVIL_ROOT always wins and is literal (``<ANVIL_ROOT>/.anvil``), in either state
layout (see ``anvil/cli/_helpers.py``), so all state lives under the scratch dir
and a single rmtree cleans it up. Nothing touches the real ``~/.anvil``.

COST WARNING: the driver spends real Claude subscription capacity (one agent
run per case). This is NOT part of the fast CI gate. Gate it behind
``RUN_BEHAVIORAL_EVALS=1`` and run it deliberately. See ``evals/README.md``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Repo root is evals/'s parent; the anvil CLI runs via `uv run anvil` from bin/.
REPO_ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"

# claude-agent-sdk drives the underlying CLI; if these are set the CLI prefers
# the (quota-capped, in this environment) API key over the subscription session
# and every run fails on a 400 usage-limit error. ALWAYS scrub them.
_API_KEY_VARS = ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY")


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


class IsolatedEnv:
    """A throwaway anvil project dir, cleaned up on exit.

    Use as a context manager. On enter it makes a fresh ``mkdtemp`` scratch
    directory; on exit it rmtree's it. All anvil state lands under
    ``<scratch>/.anvil`` because we run the CLI with ``ANVIL_ROOT=<scratch>``,
    so there is exactly one tree to remove and the real ``~/.anvil`` is never
    touched.
    """

    def __init__(self, prefix: str = "anvil_eval_") -> None:
        self.prefix = prefix
        self.root: Path | None = None

    def __enter__(self) -> IsolatedEnv:
        self.root = Path(tempfile.mkdtemp(prefix=self.prefix)).resolve()
        return self

    def __exit__(self, *exc: object) -> None:
        if self.root is not None and self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)
        self.root = None

    # -- anvil CLI plumbing --------------------------------------------------

    @property
    def project_dir(self) -> Path:
        assert self.root is not None, "IsolatedEnv used outside its context"
        return self.root

    @property
    def state_dir(self) -> Path:
        """Where anvil keeps state for this run (``<scratch>/.anvil``)."""
        return self.project_dir / ".anvil"

    def cli_env(self) -> dict[str, str]:
        """Environment for `anvil` invocations: ANVIL_ROOT-pinned, key-scrubbed."""
        env = dict(os.environ)
        env["ANVIL_ROOT"] = str(self.project_dir)
        for var in _API_KEY_VARS:
            env.pop(var, None)
        return env

    def run_anvil(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        """Run ``anvil <args>`` against this scratch project via ``uv run``.

        We shell to ``uv run anvil`` from ``bin/`` so the eval exercises the
        exact CLI the skill tells the agent to use, no import shortcuts.
        """
        proc = subprocess.run(
            ["uv", "run", "anvil", *args],
            cwd=str(BIN_DIR),
            env=self.cli_env(),
            capture_output=True,
            text=True,
            timeout=120,
        )
        if check and proc.returncode != 0:
            raise RuntimeError(
                f"anvil {' '.join(args)} failed ({proc.returncode}):\n"
                f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
            )
        return proc

    def status_json(self) -> dict[str, Any]:
        """Parsed ``anvil status --json`` envelope for this scratch project."""
        proc = self.run_anvil("status", "--json", check=False)
        return json.loads(proc.stdout)

    def init(self, name: str) -> None:
        """Scaffold the throwaway project (``anvil init``)."""
        self.run_anvil("init", "--name", name)


# ---------------------------------------------------------------------------
# Agent driver (claude-agent-sdk over its bundled CLI)
# ---------------------------------------------------------------------------


@dataclass
class ExecutionTrace:
    """Structured result of one agent run."""

    is_error: bool
    result: str
    num_turns: int
    session_id: str | None = None
    raw: list[dict[str, Any]] = field(default_factory=list)


def run_agent(
    prompt: str,
    *,
    cwd: Path,
    allowed_tools: list[str],
    max_turns: int = 20,
    extra_env: dict[str, str] | None = None,
) -> ExecutionTrace:
    """Drive a real agent through ``claude-agent-sdk`` and return a trace.

    The SDK is a thin wrapper over the bundled Claude Code CLI (subprocess
    transport); it does NOT call the Anthropic API directly. With the API-key
    vars scrubbed it authenticates via the logged-in subscription session,
    exactly like interactive Claude Code.

    Raises ``RuntimeError`` if claude-agent-sdk is not installed (it is an
    eval-only dependency; see ``evals/README.md``).
    """
    try:
        import anyio
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ResultMessage,
            TextBlock,
            query,
        )
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise RuntimeError(
            "claude-agent-sdk is not installed. It is an eval-only dependency; "
            "install it into a throwaway venv, e.g.\n"
            "  uv venv /tmp/anvil-evals && "
            "uv pip install --python /tmp/anvil-evals/bin/python "
            "claude-agent-sdk anyio pyyaml\n"
            "then run with that interpreter. See evals/README.md."
        ) from exc

    # CRITICAL: the SDK transport builds the subprocess env as
    # ``{**os.environ, **options.env}`` (see subprocess_cli.py). options.env can
    # only ADD/override keys, never REMOVE an inherited one. So to stop the CLI
    # picking up the quota-capped ANTHROPIC_API_KEY (which fails every run with a
    # 400 usage-limit), we must scrub it from THIS process's os.environ for the
    # duration of the run, then restore. We do NOT pass options.env at all (the
    # transport already inherits the now-scrubbed os.environ) except for the
    # caller's extra_env (e.g. ANVIL_ROOT for the agent's Bash anvil calls).
    options = ClaudeAgentOptions(
        cwd=str(cwd),
        max_turns=max_turns,
        permission_mode="bypassPermissions",
        allowed_tools=allowed_tools,
        # Stop ambient Claude Code hooks/settings leaking into the subprocess
        # (they flood the run with hook events and can break it).
        setting_sources=[],
        env=dict(extra_env or {}),
    )

    collected: list[dict[str, Any]] = []
    trace: dict[str, Any] = {
        "is_error": True,
        "result": "(no ResultMessage received)",
        "num_turns": 0,
        "session_id": None,
    }
    state = {"got_result": False}

    async def _drive() -> None:
        try:
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, AssistantMessage):
                    text = "".join(
                        b.text for b in message.content if isinstance(b, TextBlock)
                    )
                    collected.append({"type": "assistant", "text": text})
                elif isinstance(message, ResultMessage):
                    state["got_result"] = True
                    trace["is_error"] = bool(message.is_error)
                    trace["result"] = str(message.result)
                    trace["num_turns"] = int(getattr(message, "num_turns", 0))
                    trace["session_id"] = getattr(message, "session_id", None)
                    collected.append({"type": "result", **trace})
        except Exception as exc:  # noqa: BLE001
            # The SDK can raise a terminal control-frame error AFTER it has
            # already delivered the ResultMessage (e.g. when the run stops on
            # max_turns, it emits a trailing {"type":"error"} frame). If we have
            # the result, that frame is noise — keep the trace. If we never got a
            # result, the failure is real: surface it.
            if not state["got_result"]:
                raise
            collected.append({"type": "sdk_terminal_noise", "text": str(exc)})

    # Scrub the API-key vars from the real process env for the duration of the
    # run so the inherited subprocess env does not carry the quota-capped key,
    # then restore them so the eval process is left as we found it.
    saved = {var: os.environ.pop(var, None) for var in _API_KEY_VARS}
    try:
        anyio.run(_drive)
    finally:
        for var, val in saved.items():
            if val is not None:
                os.environ[var] = val
    return ExecutionTrace(
        is_error=bool(trace["is_error"]),
        result=str(trace["result"]),
        num_turns=int(trace["num_turns"]),
        session_id=trace["session_id"],
        raw=collected,
    )


# ---------------------------------------------------------------------------
# Deterministic assertions over anvil's own state
# ---------------------------------------------------------------------------


@dataclass
class AssertResult:
    name: str
    passed: bool
    detail: str


def _events(state_dir: Path) -> list[dict[str, Any]]:
    """Read events.jsonl (append-only NDJSON) as a list of dicts."""
    path = state_dir / "events.jsonl"
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def check_status_field(env: IsolatedEnv, field_path: str, expected: Any) -> AssertResult:
    """Assert a dotted field under ``anvil status --json`` ``data`` equals expected.

    e.g. ``check_status_field(env, "prd_status", "draft")``.
    """
    data = env.status_json().get("data", {})
    cur: Any = data
    for part in field_path.split("."):
        cur = cur.get(part) if isinstance(cur, dict) else None
    ok = cur == expected
    return AssertResult(
        name=f"status.data.{field_path} == {expected!r}",
        passed=ok,
        detail=f"got {cur!r}",
    )


def check_file_exists(env: IsolatedEnv, rel: str) -> AssertResult:
    """Assert a file exists under the resolved state dir (``<scratch>/.anvil``)."""
    path = env.state_dir / rel
    return AssertResult(
        name=f"file exists: .anvil/{rel}",
        passed=path.is_file(),
        detail=str(path),
    )


def check_file_contains(env: IsolatedEnv, rel: str, needles: list[str]) -> AssertResult:
    """Assert a state-dir file contains every substring in ``needles``."""
    path = env.state_dir / rel
    if not path.is_file():
        return AssertResult(f"file contains: .anvil/{rel}", False, "file missing")
    text = path.read_text(encoding="utf-8")
    missing = [n for n in needles if n not in text]
    return AssertResult(
        name=f"file contains ({len(needles)} needles): .anvil/{rel}",
        passed=not missing,
        detail="all present" if not missing else f"missing: {missing}",
    )


def check_events_contains_action(env: IsolatedEnv, action: str) -> AssertResult:
    """Assert events.jsonl carries at least one event with the given action."""
    actions = [e.get("action") for e in _events(env.state_dir)]
    return AssertResult(
        name=f"events.jsonl has action {action!r}",
        passed=action in actions,
        detail=f"actions={actions}",
    )


# Dispatch table for YAML-defined assertions: {check: name, ...args}.
def run_assertion(env: IsolatedEnv, spec: dict[str, Any]) -> AssertResult:
    check = spec["check"]
    if check == "status_field":
        return check_status_field(env, spec["field"], spec["equals"])
    if check == "file_exists":
        return check_file_exists(env, spec["file"])
    if check == "file_contains":
        return check_file_contains(env, spec["file"], list(spec["needles"]))
    if check == "events_contains_action":
        return check_events_contains_action(env, spec["action"])
    raise ValueError(f"unknown assertion check: {check!r}")
