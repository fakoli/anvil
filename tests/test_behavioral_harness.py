"""Offline state-loop qualification: the current harness owns model execution."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

EVALS = Path(__file__).resolve().parents[1] / "evals"
sys.path.insert(0, str(EVALS))
from harness import IsolatedEnv, run_agent, run_assertion, run_codex_agent  # noqa: E402
from run import DEFAULT_CASE, main, run_case  # noqa: E402


def test_live_entry_points_require_explicit_gate(monkeypatch, tmp_path):
    monkeypatch.delenv("RUN_BEHAVIORAL_EVALS", raising=False)
    assert main(["run.py", "--provider", "codex"]) == 2
    for invoke in (
        lambda: run_case(DEFAULT_CASE),
        lambda: run_codex_agent("unused", cwd=tmp_path),
        lambda: run_agent("unused", cwd=tmp_path, allowed_tools=[]),
    ):
        with pytest.raises(RuntimeError, match="RUN_BEHAVIORAL_EVALS"):
            invoke()


@pytest.mark.parametrize("bundle_mode", [False, True])
def test_harness_executes_cli_work_with_real_claim_bound_proof(monkeypatch, bundle_mode):
    case = yaml.safe_load((EVALS / "cases" / "execute.yaml").read_text())
    # Ambient fake keys are not permission, even for a harness using a local model.
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-never-used")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-never-used")
    with IsolatedEnv() as env:
        env.init(case["project_name"])
        path = env.state_dir / "config.yaml"
        config = yaml.safe_load(path.read_text())
        config.update(case["config"])
        path.write_text(yaml.safe_dump(config))
        (env.state_dir / "prd.md").write_text(case["prd_source"])
        for args in case["setup_commands"]:
            env.run_anvil(*args)
        if bundle_mode:
            env.run_anvil("bundle", "create", "B001", "T001", "--prd", "default",
                          "--coordinator", "eval-harness")
            bundle_claim = json.loads(env.run_anvil(
                "bundle", "claim", "B001", "--actor", "eval-harness", "--shared-tree", "--json",
            ).stdout)["data"]["claim"]
            claim = {"id": bundle_claim["member_claim_ids"]["T001"]}
        else:
            claim = json.loads(env.run_anvil(
                "claim", "T001", "--actor", "eval-harness", "--json",
            ).stdout)["data"]["claim"]
        env.run_anvil("packet", "T001")
        (env.project_dir / "echo.py").write_text('print("ANVIL")\n')
        completed = subprocess.run(
            [sys.executable, "echo.py"], cwd=env.project_dir,
            capture_output=True, text=True, check=True,
        )
        stdout = env.project_dir / "stdout.txt"
        stderr = env.project_dir / "stderr.txt"
        stdout.write_text(completed.stdout)
        stderr.write_text(completed.stderr)
        monkeypatch.setenv("ANVIL_CLAIM_ID", claim["id"])
        env.run_anvil(
            "hook", "capture-evidence", "--command", "python echo.py",
            "--exit-code", str(completed.returncode), "--stdout-file", str(stdout),
            "--stderr-file", str(stderr), "--actor", "eval-harness",
        )
        submission = json.loads(env.run_anvil(
            "submit", "T001", "--commands", "python echo.py", "--files-changed", "echo.py",
            "--actor", "eval-harness", "--json",
        ).stdout)["data"]
        assert submission["missing_claim_bound_proofs"] == []
        assert submission["hook_command_proofs"][0]["exit_code"] == 0
        for spec in case["assertions"]:
            result = run_assertion(env, spec)
            assert result.passed, result.detail
        if bundle_mode:
            env.run_anvil("bundle", "complete", "B001", "--actor", "eval-harness")
            # Event replay must accept the same typed bundle evidence at its
            # historical claim state, even after the bundle enters review.
            from anvil.clock import SystemClock
            from anvil.state.sqlite import SqliteBackend

            replay = SqliteBackend(db_path=str(env.project_dir / "replay.db"),
                                   events_path=str(env.project_dir / "replay.jsonl"),
                                   clock=SystemClock())
            replay.initialize()
            try:
                replay.replay_from_empty(str(env.state_dir / "events.jsonl"))
                assert replay.get_task("T001").status.value == "needs_review"
                assert replay.get_latest_evidence("T001").proofs
                assert replay.get_bundle("B001").status.value == "implemented_unreviewed"
            finally:
                replay.close()
        # Augmentation fails closed in this mode without selecting a cloud provider.
        denied = env.run_anvil("score", "T001", "--use-llm", check=False)
        assert denied.returncode != 0
        assert "current harness" in denied.stdout + denied.stderr



def test_claude_driver_cancels_on_timeout(monkeypatch, tmp_path):
    import anyio
    import claude_agent_sdk

    from anvil.planning import subscription

    closed = []
    async def stalled_query(**kwargs):
        try:
            await anyio.sleep_forever()
            yield None
        finally:
            closed.append(True)

    monkeypatch.setenv("RUN_BEHAVIORAL_EVALS", "1")
    monkeypatch.setattr(subscription, "require_subscription", lambda *a, **k: "fake-claude")
    monkeypatch.setattr(claude_agent_sdk, "query", stalled_query)
    trace = run_agent("input", cwd=tmp_path, allowed_tools=[], timeout=0.01)
    assert trace.is_error and "timed out" in trace.result
    assert closed == [True]
