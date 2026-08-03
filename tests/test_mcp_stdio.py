"""Real stdio contracts for MCP schema mismatch handling (issue #180)."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from fastmcp.exceptions import ToolError

from anvil import __version__
from anvil.clock import SystemClock
from anvil.state.schema import SCHEMA_VERSION
from anvil.state.sqlite import SqliteBackend


def _future_schema_project(root: Path) -> Path:
    state_dir = root / ".anvil"
    state_dir.mkdir()
    events_path = state_dir / "events.jsonl"
    events_path.touch()
    backend = SqliteBackend(
        db_path=str(state_dir / "state.db"),
        events_path=str(events_path),
        clock=SystemClock(),
    )
    backend.initialize()
    backend.close()
    connection = sqlite3.connect(state_dir / "state.db")
    try:
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        connection.commit()
    finally:
        connection.close()
    return state_dir


def test_real_stdio_schema_mismatch_is_bounded_stable_and_responsive(
    tmp_path: Path,
) -> None:
    state_dir = _future_schema_project(tmp_path)
    log_path = tmp_path / "mcp-stderr.log"
    repo_bin = Path(__file__).resolve().parents[1] / "bin"
    transport = StdioTransport(
        command=sys.executable,
        args=["-m", "anvil.mcp_server"],
        env={**os.environ, "ANVIL_ROOT": str(tmp_path)},
        cwd=str(repo_bin),
        keep_alive=False,
        log_file=log_path,
    )

    async def run() -> tuple[str, list[str]]:
        errors: list[str] = []
        async with Client(transport) as client:
            version = client.initialize_result.serverInfo.version
            for _ in range(3):
                with pytest.raises(ToolError) as raised:
                    await asyncio.wait_for(
                        client.call_tool("list_tasks", {}), timeout=2
                    )
                errors.append(str(raised.value))
        return version, errors

    version, errors = asyncio.run(run())
    assert version == __version__
    assert len(set(errors)) == 1
    payload = json.loads(errors[0])
    assert payload["error"]["code"] == "schema_mismatch"
    assert payload["error"]["database_schema"] == SCHEMA_VERSION + 1
    assert payload["error"]["supported_schema"] == SCHEMA_VERSION
    assert len(errors[0].encode("utf-8")) <= 4_096
    logs = log_path.read_text(encoding="utf-8")
    assert str(state_dir.resolve()) not in errors[0]
    assert str(state_dir.resolve()) not in logs
    assert "Traceback" not in logs


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO exercises a stalled read")
def test_real_stdio_schema_mismatch_stalled_probe_times_out_and_recovers(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / ".anvil"
    state_dir.mkdir()
    (state_dir / "events.jsonl").touch()
    os.mkfifo(state_dir / "state.db")
    log_path = tmp_path / "mcp-stalled-stderr.log"
    repo_bin = Path(__file__).resolve().parents[1] / "bin"
    transport = StdioTransport(
        command=sys.executable,
        args=["-m", "anvil.mcp_server"],
        env={**os.environ, "ANVIL_ROOT": str(tmp_path)},
        cwd=str(repo_bin),
        keep_alive=False,
        log_file=log_path,
    )

    async def run() -> tuple[str, int]:
        async with Client(transport) as client:
            with pytest.raises(ToolError) as raised:
                await asyncio.wait_for(
                    client.call_tool("list_tasks", {}), timeout=4
                )
            tools = await asyncio.wait_for(client.list_tools(), timeout=2)
            return str(raised.value), len(tools)

    error, tool_count = asyncio.run(run())
    payload = json.loads(error)
    assert payload["error"]["code"] == "schema_probe_failed"
    assert len(error.encode("utf-8")) <= 4_096
    assert tool_count == 24
    logs = log_path.read_text(encoding="utf-8")
    assert str(state_dir.resolve()) not in error
    assert str(state_dir.resolve()) not in logs
    assert "Traceback" not in logs
