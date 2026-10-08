"""Real MCP consumers preserve selected-checkout profile contracts."""
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from anvil.cli._helpers import _open_backend
from anvil.mcp_server import mcp
from anvil.verification_profiles import MANIFEST
from tests.test_mcp import _data, _run
from tests.test_profile_planning import _MANIFEST
from tests.test_profile_planning_cli import _project


async def _call(client, tool, root, prd="default", **kwargs):
    return _data(await client.call_tool(tool, {"cwd": str(root), "prd_id": prd, **kwargs}))


async def _approve(client, root, prd):
    await _call(client, "review_prd", root, prd, reviewer="reviewer")
    await _call(client, "review_prd", root, prd, reviewer="reviewer", approve=True)


@pytest.mark.parametrize("prd", ["default", "release"])
@pytest.mark.parametrize("workspace", [False, True])
def test_parse_approve_plan_preserves_frozen_verification(tmp_path, monkeypatch, prd, workspace):
    root, state, source = _project(tmp_path, monkeypatch, prd, workspace=workspace)

    async def exercise():
        async with Client(mcp) as client:
            await _call(client, "parse_prd", root, prd)
            backend = _open_backend(state)
            try:
                task_id = "T001" if prd == "default" else "release:T001"
                binding = backend.get_prd(prd).profile_bindings[task_id]
                assert backend.get_task(task_id) is None
            finally:
                backend.close()
            await _approve(client, root, prd)
            await _call(client, "plan_tasks", root, prd, use_llm=False)
        backend = _open_backend(state)
        try:
            task = backend.get_task(task_id)
            assert task.verification.profile_binding == binding
            assert task.verification.commands == ["pytest -q", "python verify.py full"]
            assert [proof.command for proof in task.verification.required_proofs] == task.verification.commands
            assert backend.get_prd(prd).status.value == "approved"
        finally:
            backend.close()

    _run(exercise())


@pytest.mark.parametrize("consumer", ["parse_prd", "plan_tasks"])
@pytest.mark.parametrize("drift", ["manifest", "runner", "missing_runner"])
def test_profile_drift_refuses_without_events(tmp_path, monkeypatch, consumer, drift):
    root, state, source = _project(tmp_path, monkeypatch, "default")

    async def exercise():
        async with Client(mcp) as client:
            await _call(client, "parse_prd", root)
            await _approve(client, root, "default")
            before = (state / "events.jsonl").read_bytes()
            if drift == "manifest":
                (root / MANIFEST).write_text(_MANIFEST + "\n# Changed source.\n")
            elif drift == "runner":
                (root / "verify.py").write_text("assert False\n")
            else:
                (root / "verify.py").unlink()
            with pytest.raises(ToolError, match="verification profile|could not be bound"):
                await _call(client, consumer, root, **({"use_llm": False} if consumer == "plan_tasks" else {}))
            assert (state / "events.jsonl").read_bytes() == before
            backend = _open_backend(state)
            try:
                assert backend.get_prd().status.value == "approved"
                assert backend.get_task("T001") is None
            finally:
                backend.close()

    _run(exercise())


@pytest.mark.parametrize("explicit", [False, True])
def test_selected_root_wins_over_unrelated_cwd_and_env(tmp_path, monkeypatch, explicit):
    root, state, source = _project(tmp_path, monkeypatch, "default")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("ANVIL_ROOT", str(elsewhere if explicit else root))

    async def exercise():
        async with Client(mcp) as client:
            kwargs = {"cwd": str(root)} if explicit else {}
            await client.call_tool("parse_prd", kwargs)
            await client.call_tool("plan_tasks", {**kwargs, "use_llm": False})
        backend = _open_backend(state)
        try:
            assert backend.get_task("T001").verification.profile_binding is not None
        finally:
            backend.close()

    _run(exercise())


def test_legacy_mcp_never_resolves_profile_files(tmp_path, monkeypatch):
    root, state, source = _project(tmp_path, monkeypatch, "default", profiled=False)
    (root / MANIFEST).unlink()
    (root / "verify.py").unlink()

    def forbidden(*args, **kwargs):
        pytest.fail("legacy MCP touched profile files")

    monkeypatch.setattr("anvil.verification_profiles.resolve_profile", forbidden)

    async def exercise():
        async with Client(mcp) as client:
            await _call(client, "parse_prd", root)
            await _call(client, "plan_tasks", root, use_llm=False)
        backend = _open_backend(state)
        try:
            assert backend.get_task("T001").verification.commands == ["pytest -q"]
            assert backend.get_prd().profile_bindings is None
        finally:
            backend.close()

    _run(exercise())
