"""Qualify long-running discovery/help and actual stdio from the built wheel."""

from __future__ import annotations

import json
from pathlib import Path

from anvil.cli.describe import API_VERSION
from tests import test_installed_wheel
from tests.test_release_artifact_contract import _install_wheel, _run

bound_wheel_fixture = test_installed_wheel.built_wheel


def test_installed_long_running_cli_and_stdio_contract(
    bound_wheel_fixture: Path, tmp_path: Path,
) -> None:
    python, anvil, env = _install_wheel(bound_wheel_fixture, tmp_path)
    for name in ("ANVIL_ROOT", "ANVIL_PRD", "ANVIL_ACTOR", "ANVIL_CLAIM_ID"):
        env.pop(name, None)
    env["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"
    described = _run([str(anvil), "describe", "--json"], cwd=tmp_path, env=env)
    assert described.returncode == 0, described.stderr
    described_payload = json.loads(described.stdout)
    assert described_payload["ok"] is True
    manifest = described_payload["data"]
    assert manifest["api_version"] == API_VERSION
    version = _run([str(anvil), "--version"], cwd=tmp_path, env=env)
    assert version.returncode == 0, version.stderr
    assert version.stdout.startswith(f"anvil {manifest['display_version']} (schema ")
    for command, flags in {
        "packet": {"--attempt", "--observation-at"},
        "evidence-preflight": {"--json", "--observation-at"},
        "progress": {"--timing-file"},
        "apply": {"--invalidate-accepted", "--invalidation-preview"},
    }.items():
        assert flags <= set(manifest["cli"]["options"][command])
        help_result = _run(
            [str(anvil), command, "--help"], cwd=tmp_path, env=env,
        )
        assert help_result.returncode == 0, help_result.stderr
        assert all(flag in help_result.stdout for flag in flags)
    qualified = _run(
        [str(python), "-c", _STDIO_CHECK, str(tmp_path),
         str(manifest["mcp"]["count"]), manifest["display_version"]],
        cwd=tmp_path, env=env,
    )
    assert qualified.returncode == 0, (qualified.stdout + qualified.stderr)[-2000:]
    result = json.loads(qualified.stdout)
    assert result == {"default_tools": 26, "planning_tools": 38,
                      "advisory_refusals": 2, "servers_stopped": True}
    assert not (tmp_path / ".anvil").exists()


_STDIO_CHECK = r"""
import asyncio
import json
import os
import sys
from importlib.resources import files
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from fastmcp.exceptions import ToolError

root = Path(sys.argv[1])
instructions = files("anvil._data").joinpath("AGENTS.md").read_text()
assert "Native resume and frozen handoff" in instructions
assert "at least three independent" in instructions
assert "Immutable approval requires explicit user authority" in instructions
assert "final user project-validation gates" in instructions

async def check():
    counts = []
    refused = 0
    for planning in ("0", "1"):
        transport = StdioTransport(
            command=sys.executable, args=["-m", "anvil.mcp_server"],
            cwd=str(root), env={**os.environ, "ANVIL_MCP_PLANNING": planning},
            keep_alive=False, log_file=root / ("stdio-" + planning + ".log"),
        )
        async with Client(transport) as client:
            assert client.initialize_result.serverInfo.version == sys.argv[3]
            tools = await asyncio.wait_for(client.list_tools(), 20)
            names = {tool.name: tool for tool in tools}
            counts.append(len(tools))
            assert {"get_attempt_view", "read_evidence_preflight"} <= names.keys()
            for tool in ("get_attempt_view", "read_evidence_preflight"):
                assert {"task_id", "cwd", "observation_at"} <= set(
                    names[tool].inputSchema["properties"]
                )
            assert "timing" in names["submit_progress"].inputSchema["properties"]
            if planning == "1":
                assert len(tools) == int(sys.argv[2])
                assert "init_project" in names
                continue
            assert "init_project" not in names
            for tool in ("get_attempt_view", "read_evidence_preflight"):
                try:
                    await asyncio.wait_for(client.call_tool(
                        tool, {"task_id": "T001", "cwd": str(root)},
                    ), 20)
                except ToolError as error:
                    assert json.loads(str(error))["code"] == "state_unavailable"
                    assert len(str(error).encode()) <= 4096
                    refused += 1
                else:
                    raise AssertionError("missing-state read unexpectedly succeeded")
        assert not (root / ".anvil").exists()
    return {"default_tools": counts[0], "planning_tools": counts[1],
            "advisory_refusals": refused, "servers_stopped": True}

print(json.dumps(asyncio.run(check())))
"""
