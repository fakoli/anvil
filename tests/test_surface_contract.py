"""Qualify discovery against the registered CLI and actual FastMCP transport."""

import json
from importlib.resources import files

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from jsonschema import Draft202012Validator
from typer.testing import CliRunner

from anvil.cli import app
from anvil.mcp_server import apply_surface_gate, mcp
from tests.test_attempt_surfaces_mcp import _files
from tests.test_cli import _expected_cli_command_options, _expected_cli_contracts
from tests.test_evidence_preflight import _capture, _claim
from tests.test_mcp import _ALL_TOOLS, _EXECUTION_TOOLS, _PLANNING_TOOLS, _data, _run

runner = CliRunner()


def test_discovery_matches_registered_commands_and_both_wire_surfaces(tmp_path, monkeypatch):
    monkeypatch.setenv("ANVIL_ROOT", str(tmp_path))
    before = _files(tmp_path)
    result = runner.invoke(app, ["describe", "--json"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    manifest = json.loads(result.output)["data"]
    contracts = _expected_cli_contracts()
    options = _expected_cli_command_options()
    assert manifest["api_version"] == "19"
    assert manifest["cli"]["contracts"] == contracts
    assert manifest["cli"]["options"] == options
    assert manifest["cli"]["commands"] == sorted(options)
    assert manifest["cli"]["count"] == len(options) == 88
    assert manifest["cli"]["contract_count"] == len(contracts) == 100
    assert {"--attempt", "--bundle", "--observation-at", "--format"} <= set(options["packet"])
    assert {"--json", "--observation-at"} <= set(options["evidence-preflight"])
    assert "--timing-file" in options["progress"]
    assert set(manifest["mcp"]["tools"]) == _ALL_TOOLS
    assert manifest["mcp"]["count"] == len(_ALL_TOOLS) == 38

    async def exercise():
        for planning in (False, True):
            with monkeypatch.context() as patch:
                patch.setenv("ANVIL_MCP_PLANNING", str(int(planning)))
                apply_surface_gate()
                try:
                    async with Client(mcp) as client:
                        tools = {tool.name: tool for tool in await client.list_tools()}
                        assert set(tools) == (_ALL_TOOLS if planning else _EXECUTION_TOOLS)
                        assert len(tools) == (38 if planning else 26)
                        assert set(tools) & _PLANNING_TOOLS == (_PLANNING_TOOLS if planning else set())
                        assert "observation_at" in tools["get_attempt_view"].inputSchema["properties"]
                        assert "timing" in tools["submit_progress"].inputSchema["properties"]
                        if planning:
                            assert _data(await client.call_tool("describe_surface", {})) == manifest
                finally:
                    patch.undo()
                    apply_surface_gate()

    _run(exercise())
    assert _files(tmp_path) == before


def test_public_advisory_reads_share_identity_and_do_not_open_mutators(tmp_path, monkeypatch):
    task, claim = _claim(tmp_path)
    monkeypatch.setenv("ANVIL_ROOT", str(tmp_path))
    monkeypatch.setenv("ANVIL_CLAIM_ID", claim)
    _capture(tmp_path, failed=True)
    before = _files(tmp_path / ".anvil")

    def forbidden(*args, **kwargs):
        pytest.fail("public advisory read opened a backend or reaped claims")

    for module in ("anvil.cli.packet_apply", "anvil.mcp_server"):
        monkeypatch.setattr(module + "._open_backend", forbidden)
    monkeypatch.setattr("anvil.cli.packet_apply._reap_stale_claims", forbidden)
    monkeypatch.setattr("anvil.state.sqlite.SqliteBackend.initialize", forbidden)
    attempt_result = runner.invoke(app, ["packet", task, "--attempt", "--format", "json"])
    preflight_result = runner.invoke(app, ["evidence-preflight", task, "--json"])
    assert attempt_result.exit_code == preflight_result.exit_code == 0
    attempt = json.loads(attempt_result.output)
    preflight = json.loads(preflight_result.output)["data"]
    manifest = json.loads(runner.invoke(app, ["describe", "--json"]).output)["data"]
    for operation in manifest["operation_catalog"]["operations"]:
        command = operation["transport"]["command"].split()
        if command == ["prd", "show"]:
            command.append("default")
        result = runner.invoke(app, [*command, "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        schema = json.loads(files("anvil._data").joinpath(operation["schema_resources"]["output"]).read_text())
        Draft202012Validator(schema).validate(data)
        assert data["operation_id"] == operation["operation_id"]
        assert data["operation_version"] == 1
        assert schema["$id"] != attempt["schema_id"]
    assert attempt["schema_id"] == "anvil.state.attempt-view.v1"
    assert preflight["schema_id"] == "anvil.state.evidence-preflight.v1"
    assert attempt["event_cursor"] == preflight["event_cursor"]
    assert attempt["current"]["claim"]["id"] == preflight["current_claim"]["id"] == claim
    assert preflight["hard_proof_limits"] == {"max_items": 16, "max_bytes": 1_048_576}
    assert preflight["buffer"]["valid_proof_count"] == 1
    assert not all(item["satisfied"] for item in preflight["required_command_proofs"])
    assert attempt["applied_limits"]["max_response_bytes"] == 65_536
    assert len(attempt_result.output.encode()) <= 65_536

    async def exercise():
        async with Client(mcp) as client:
            assert _data(await client.call_tool("get_attempt_view", {"task_id": task})) == attempt
            assert _data(await client.call_tool("read_evidence_preflight", {"task_id": task})) == preflight

    _run(exercise())
    assert _files(tmp_path / ".anvil") == before


@pytest.mark.parametrize("overflow", ["items", "bytes"])
def test_public_preflight_limits_refuse_without_trimming_or_mutation(tmp_path, monkeypatch, overflow):
    task, claim = _claim(tmp_path)
    monkeypatch.setenv("ANVIL_ROOT", str(tmp_path))
    monkeypatch.setenv("ANVIL_CLAIM_ID", claim)
    for _ in range(17 if overflow == "items" else 1):
        _capture(tmp_path, failed=True)
    buffer = tmp_path / ".anvil" / ".evidence-buffer" / f"{claim}.json"
    if overflow == "bytes":
        with buffer.open("ab") as stream:
            stream.write(b" " * 1_048_576)
    before = _files(tmp_path / ".anvil")
    result = runner.invoke(app, ["evidence-preflight", task, "--json"])
    assert result.exit_code == 1
    diagnostic = json.loads(result.output)["error"]
    assert diagnostic["schema_id"] == "anvil.state.attempt-view-error.v1"
    assert str(tmp_path) not in result.output

    async def exercise():
        async with Client(mcp) as client:
            with pytest.raises(ToolError) as refused:
                await client.call_tool("read_evidence_preflight", {"task_id": task})
            error = json.loads(str(refused.value))
            assert error == {key: diagnostic[key] for key in error}

    _run(exercise())
    assert _files(tmp_path / ".anvil") == before


def test_advisory_errors_and_provider_catalog_remain_separate_without_initialization(tmp_path, monkeypatch):
    monkeypatch.setenv("ANVIL_ROOT", str(tmp_path))
    monkeypatch.setattr("anvil.state.sqlite.SqliteBackend.initialize",
                        lambda *args: pytest.fail("read initialized absent State"))
    manifest = json.loads(runner.invoke(app, ["describe", "--json"]).output)["data"]
    before = _files(tmp_path)
    for operation in manifest["operation_catalog"]["operations"]:
        assert operation["operation_version"] == 1 and operation["effect"] == "read"
        assert operation["transport"]["kind"] == "cli"
        command = operation["transport"]["command"].split()
        if command == ["prd", "show"]:
            command.append("default")
        result = runner.invoke(app, [*command, "--json"])
        assert result.exit_code == 1
        error = json.loads(result.output)["error"]
        schema = json.loads(files("anvil._data").joinpath(operation["schema_resources"]["error"]).read_text())
        Draft202012Validator(schema).validate(error)
        assert error.get("schema_id") != "anvil.state.attempt-view-error.v1"
    for command in (["packet", "T001", "--attempt", "--format", "json"],
                    ["evidence-preflight", "T001", "--json"]):
        result = runner.invoke(app, command)
        assert result.exit_code == 1
        error = json.loads(result.output)["error"]
        assert error["code"] == "state_unavailable"
        assert str(tmp_path) not in result.output
    async def exercise():
        async with Client(mcp) as client:
            for tool in ("get_attempt_view", "read_evidence_preflight"):
                with pytest.raises(ToolError) as refused:
                    await client.call_tool(tool, {"task_id": "T001"})
                error = json.loads(str(refused.value))
                assert error["schema_id"] == "anvil.state.attempt-view-error.v1"
                assert error["code"] == "state_unavailable"
                assert str(tmp_path) not in str(refused.value)
    _run(exercise())
    assert _files(tmp_path) == before
