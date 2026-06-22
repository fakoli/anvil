"""Opt-in pytest wrapper for the anvil behavioral eval.

This test is NOT part of the fast CI gate. The repo's pytest config pins
``testpaths = ["../tests"]`` (bin/pyproject.toml), so ``cd bin && uv run
pytest`` never collects this file. Run it deliberately, from a venv that has
claude-agent-sdk installed:

    RUN_BEHAVIORAL_EVALS=1 pytest evals/test_behavioral_eval.py -s

It is double-gated so an accidental collection never spends capacity:
  - skips unless ``RUN_BEHAVIORAL_EVALS=1`` is set (it costs real subscription
    capacity and is latency-nondeterministic);
  - skips if ``claude-agent-sdk`` is not importable (eval-only dependency).
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

EVALS_DIR = Path(__file__).resolve().parent
if str(EVALS_DIR) not in sys.path:
    sys.path.insert(0, str(EVALS_DIR))

_GATE = os.environ.get("RUN_BEHAVIORAL_EVALS") == "1"
_HAS_SDK = importlib.util.find_spec("claude_agent_sdk") is not None


@pytest.mark.skipif(not _GATE, reason="set RUN_BEHAVIORAL_EVALS=1 (costed: spends capacity)")
@pytest.mark.skipif(not _HAS_SDK, reason="claude-agent-sdk not installed (eval-only dep)")
def test_start_prd_reaches_promised_state() -> None:
    """The start-prd skill, driven by a real agent, advances anvil to its promise."""
    from run import DEFAULT_CASE, run_case

    assert run_case(DEFAULT_CASE), (
        "start-prd behavioral eval failed: the agent did not drive anvil to the "
        "promised state (parsed PRD, prd_status=draft). See the printed report."
    )
