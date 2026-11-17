"""A revised PRD must be re-planned before its tasks can be claimed.

``prd parse`` records a new revision but only planning rebuilds feature and task
rows, so without this gate the previous revision's tasks stay claimable (and are
recorded as bound to the new revision) after the operator approves the edit.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from anvil.cli import app
from tests.test_json_output import _FULL_PRD

runner = CliRunner()


def _invoke(tmp_path: Path, cmd: list[str]) -> Any:
    original_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        return runner.invoke(app, cmd, catch_exceptions=False)
    finally:
        os.chdir(original_cwd)


def _ok(tmp_path: Path, cmd: list[str]) -> Any:
    result = _invoke(tmp_path, cmd)
    assert result.exit_code == 0, f"{cmd}: {result.output}"
    return result


def _approve_current_prd(tmp_path: Path) -> None:
    _ok(tmp_path, ["prd", "parse"])
    _ok(tmp_path, ["prd", "review"])
    _ok(tmp_path, ["prd", "review", "--approve"])


def _plan_to_ready(tmp_path: Path) -> None:
    _ok(tmp_path, ["plan", "--no-llm"])
    _ok(tmp_path, ["score"])
    _ok(tmp_path, ["review", "tasks"])


def _planned_project(tmp_path: Path) -> Path:
    _ok(tmp_path, ["init", "--name", "JSON Output Test Project"])
    prd = tmp_path / ".anvil" / "prd.md"
    prd.write_text(_FULL_PRD, encoding="utf-8")
    _approve_current_prd(tmp_path)
    _plan_to_ready(tmp_path)
    return prd


def _revise(prd: Path, old: str, new: str) -> None:
    text = prd.read_text(encoding="utf-8")
    assert old in text
    prd.write_text(text.replace(old, new), encoding="utf-8")


def test_material_revision_blocks_new_claims_until_replanned(tmp_path: Path) -> None:
    prd = _planned_project(tmp_path)
    _revise(
        prd,
        "- `pytest tests/test_converter.py -v`",
        "- `pytest tests/test_converter.py tests/test_roundtrip.py -v`",
    )
    _approve_current_prd(tmp_path)

    refused = _invoke(tmp_path, ["claim", "T001", "--actor", "tester", "--json"])

    assert refused.exit_code != 0, refused.output
    envelope = json.loads(refused.stdout.strip())
    assert envelope["ok"] is False
    assert "anvil plan" in envelope["error"]["message"]
    assert "planned before its current revision" in envelope["error"]["message"]

    _plan_to_ready(tmp_path)
    claimed = _ok(tmp_path, ["claim", "T001", "--actor", "tester", "--json"])
    assert json.loads(claimed.stdout.strip())["data"]["claim"]["task_id"] == "T001"


def test_title_only_revision_keeps_the_graph_claimable(tmp_path: Path) -> None:
    prd = _planned_project(tmp_path)
    _revise(
        prd,
        "# Project: JSON Output Test Project",
        "# Project: JSON Output Test Project Renamed",
    )
    # A title-only revision keeps the material digest and the approval.
    _ok(tmp_path, ["prd", "parse"])

    _ok(tmp_path, ["claim", "T001", "--actor", "tester", "--json"])


def test_existing_claim_survives_a_material_revision(tmp_path: Path) -> None:
    prd = _planned_project(tmp_path)
    claimed = _ok(tmp_path, ["claim", "T001", "--actor", "tester", "--json"])
    claim_id = json.loads(claimed.stdout.strip())["data"]["claim"]["id"]
    _revise(prd, "- R003: Handle errors.", "- R003: Handle errors and report them.")
    _approve_current_prd(tmp_path)

    _ok(tmp_path, ["renew", claim_id, "--actor", "tester", "--json"])
    refused = _invoke(tmp_path, ["claim", "T002", "--actor", "tester", "--json"])
    assert refused.exit_code != 0
    assert "anvil plan" in json.loads(refused.stdout.strip())["error"]["message"]


def test_stale_reason_is_none_for_a_freshly_planned_graph(tmp_path: Path) -> None:
    from anvil.cli._helpers import _open_backend

    _planned_project(tmp_path)
    backend = _open_backend(tmp_path / ".anvil", project_root=tmp_path)
    try:
        assert backend.prd_task_graph_stale_reason("default") is None
        assert backend.prd_task_graph_stale_reason("no-such-prd") is None
    finally:
        backend.close()


def test_mcp_claim_is_refused_for_a_stale_graph(
    tmp_path: Path, monkeypatch: Any
) -> None:
    import asyncio

    import pytest
    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    from anvil.mcp_server import mcp

    prd = _planned_project(tmp_path)
    _revise(prd, "- R003: Handle errors.", "- R003: Handle errors and report them.")
    _approve_current_prd(tmp_path)
    monkeypatch.chdir(tmp_path)

    async def claim() -> Any:
        async with Client(mcp) as client:
            return await client.call_tool(
                "claim_task", {"task_id": "T001", "claimed_by": "agent-x"}
            )

    with pytest.raises(ToolError, match="anvil plan"):
        asyncio.run(claim())
