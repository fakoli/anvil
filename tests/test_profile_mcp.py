"""Actual MCP claims preserve selected Git targets and transactional custody."""
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from anvil.cli._helpers import _open_backend
from anvil.mcp_server import mcp
from anvil.state.sqlite import SqliteBackend
from tests.test_mcp import _data, _run
from tests.test_profile_cli import _git, _git_identity, _prepared


async def _claim(client, root, bundle, **kwargs):
    return _data(await client.call_tool(
        "claim_bundle" if bundle else "claim_task",
        {"cwd": str(root), **({"bundle_id": "B1", "actor": "author"} if bundle
            else {"task_id": "T001", "claimed_by": "author"}), **kwargs},
    ))


@pytest.mark.parametrize("bundle", [False, True])
@pytest.mark.parametrize("workspace", [False, True])
@pytest.mark.parametrize("explicit", [False, True])
def test_profile_actual_target_is_prepared_before_native_claim(
    tmp_path, monkeypatch, bundle, workspace, explicit,
):
    root, state, _ = _prepared(tmp_path, monkeypatch, bundle=bundle, workspace=workspace)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    monkeypatch.setenv("ANVIL_ROOT", str(unrelated if explicit else root))
    if workspace and not explicit:
        # ANVIL_ROOT is deliberately literal; implicit HOME resolution uses cwd.
        monkeypatch.delenv("ANVIL_ROOT")
        monkeypatch.chdir(root)
    import anvil.mcp_server as module
    original_apply = __import__("anvil.git_ops", fromlist=["apply_claim_plan"]).apply_claim_plan
    seen = []
    opened = []
    original_backend = module._open_backend

    def open_backend(*args, **kwargs):
        backend = original_backend(*args, **kwargs)
        opened.append(backend)
        return backend

    monkeypatch.setattr(module, "_open_backend", open_backend)

    def prepared(plan, **kwargs):
        assert not seen
        assert opened[-1].list_active_claims() == []
        original_apply(plan, **kwargs)
        seen.append(True)

    monkeypatch.setattr("anvil.git_ops.apply_claim_plan", prepared)

    async def exercise():
        async with Client(mcp) as client:
            selected = root if explicit else None
            result = await _claim(client, selected, bundle) if explicit else _data(
                await client.call_tool("claim_bundle" if bundle else "claim_task",
                    {"bundle_id": "B1", "actor": "author"} if bundle else
                    {"task_id": "T001", "claimed_by": "author"}))
        claim = result["claim"] if bundle else result
        assert seen == [True]
        assert Path(claim["git_metadata"]["target_path"]) == root
        assert claim["git_metadata"]["claim_start_sha"] == _git(root, "rev-parse", "HEAD")
        backend = module._open_backend(state, project_root=root)
        try:
            assert len(backend.list_active_claims()) == 1
            assert backend.get_task("T001").status.value == "claimed"
        finally:
            backend.close()

    _run(exercise())


@pytest.mark.parametrize("bundle", [False, True])
@pytest.mark.parametrize("profiled", [False, True])
def test_mcp_preserves_legacy_state_first_order(tmp_path, monkeypatch, bundle, profiled):
    root, state, _ = _prepared(tmp_path, monkeypatch, bundle=bundle, profiled=profiled)
    from anvil.git_ops import apply_claim_plan
    observed = []
    import anvil.mcp_server as module
    opened = []
    original_backend = module._open_backend

    def open_backend(*args, **kwargs):
        backend = original_backend(*args, **kwargs)
        opened.append(backend)
        return backend

    monkeypatch.setattr(module, "_open_backend", open_backend)

    def witness(plan, **kwargs):
        observed.append(bool(opened[-1].list_active_claims()))
        return apply_claim_plan(plan, **kwargs)

    monkeypatch.setattr("anvil.git_ops.apply_claim_plan", witness)

    async def exercise():
        async with Client(mcp) as client:
            await _claim(client, root, bundle)
    _run(exercise())
    assert observed == [not profiled]


@pytest.mark.parametrize("bundle", [False, True])
def test_final_native_profile_callback_refuses_and_compensates(tmp_path, monkeypatch, bundle):
    root, state, _ = _prepared(tmp_path, monkeypatch, bundle=bundle)
    before = (state / "events.jsonl").read_bytes()
    identity = _git_identity(root)
    original = SqliteBackend.append
    touched = []

    def append(self, draft, **kwargs):
        if draft.action in {"claim.created", "bundle.claimed"}:
            callback = kwargs.get("pre_log_check")
            def drift():
                if callback is not None:
                    callback()
                (root / "verify.py").write_text("unapproved runner\n")
                touched.append(True)
            kwargs["pre_log_check"] = drift
        return original(self, draft, **kwargs)

    monkeypatch.setattr(SqliteBackend, "append", append)

    async def exercise():
        async with Client(mcp) as client:
            with pytest.raises(ToolError, match="verification profile"):
                await _claim(client, root, bundle)
    _run(exercise())
    assert touched
    assert (state / "events.jsonl").read_bytes() == before
    # Dirty caller content is preserved while newly owned refs are removed.
    assert _git_identity(root)[:3] == identity[:3]
    assert (root / "verify.py").read_text() == "unapproved runner\n"


@pytest.mark.parametrize("bundle", [False, True])
def test_legacy_git_failure_releases_actual_claim(tmp_path, monkeypatch, bundle):
    root, state, _ = _prepared(tmp_path, monkeypatch, bundle=bundle, profiled=False)
    from anvil.git_ops import ClaimPlanError

    def failed(*args, **kwargs):
        raise ClaimPlanError("fixture_failure", "Git refused")

    monkeypatch.setattr("anvil.git_ops.apply_claim_plan", failed)

    async def exercise():
        async with Client(mcp) as client:
            with pytest.raises(ToolError, match="Git refused"):
                await _claim(client, root, bundle)
    _run(exercise())
    backend = _open_backend(state, project_root=root)
    try:
        assert backend.list_active_claims() == []
        assert backend.get_task("T001").status.value == "ready"
        assert backend.list_claims()[0].status.value == "released"
    finally:
        backend.close()


@pytest.mark.parametrize("bundle", [False, True])
def test_mcp_claim_keeps_registered_root_policy(tmp_path, monkeypatch, bundle):
    from anvil.roots.registry import RootSetRegistry
    root, state, _ = _prepared(tmp_path, monkeypatch, bundle=bundle)
    RootSetRegistry().enroll(repository_id="fixture", path=str(root), origin="local:fixture")
    before, identity = (state / "events.jsonl").read_bytes(), _git_identity(root)

    async def exercise():
        async with Client(mcp) as client:
            with pytest.raises(ToolError, match="root_set_registered"):
                await _claim(client, root, bundle)
    _run(exercise())
    assert (state / "events.jsonl").read_bytes() == before
    assert _git_identity(root) == identity


@pytest.mark.parametrize("bundle", [False, True])
def test_mcp_isolation_required_refuses_before_git(tmp_path, monkeypatch, bundle):
    root, state, _ = _prepared(tmp_path, monkeypatch, bundle=bundle)
    config = state / "config.yaml"
    config.write_text(config.read_text() + "\nworktree_isolation: require\n")
    before, identity = (state / "events.jsonl").read_bytes(), _git_identity(root)

    async def exercise():
        async with Client(mcp) as client:
            with pytest.raises(ToolError, match="worktree_isolation"):
                await _claim(client, root, bundle)
    _run(exercise())
    assert (state / "events.jsonl").read_bytes() == before
    assert _git_identity(root) == identity


@pytest.mark.parametrize("workspace", [False, True])
def test_mcp_backend_implicit_pairing_does_not_invent_other_root(tmp_path, monkeypatch, workspace):
    import anvil.mcp_server as module
    root, state, _ = _prepared(tmp_path, monkeypatch, workspace=workspace)
    monkeypatch.setenv("ANVIL_ROOT", str(root))
    if workspace:
        monkeypatch.delenv("ANVIL_ROOT")
        monkeypatch.chdir(root)
    backend = module._open_backend(state)
    try:
        assert backend._project_root == root
    finally:
        backend.close()
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("ANVIL_ROOT", str(other))
    backend = module._open_backend(state)
    try:
        assert backend._project_root is None
    finally:
        backend.close()


@pytest.mark.parametrize("bundle", [False, True])
def test_profile_failure_preserves_preexisting_unowned_branch(tmp_path, monkeypatch, bundle):
    from anvil.bundles.manager import BundleError, BundleManager
    from anvil.claims.manager import ClaimError, ClaimManager
    from anvil.git_ops import resolve_claim_plan

    root, state, _ = _prepared(tmp_path, monkeypatch, bundle=bundle)
    backend = _open_backend(state, project_root=root)
    try:
        title = "Bundle B1" if bundle else backend.get_task("T001").title
    finally:
        backend.close()
    plan = resolve_claim_plan("B1" if bundle else "T001", title, cwd=root,
        branch_prefix="agent", shared_tree=False, ignored_worktree_paths=(state,))
    _git(root, "branch", plan.branch)
    before, identity = (state / "events.jsonl").read_bytes(), _git_identity(root)
    def denied(*args, **kwargs):
        raise (BundleError if bundle else ClaimError)("fixture native refusal")
    monkeypatch.setattr(BundleManager if bundle else ClaimManager, "claim", denied)
    async def exercise():
        async with Client(mcp) as client:
            with pytest.raises(ToolError, match="fixture native refusal"):
                await _claim(client, root, bundle)
    _run(exercise())
    assert (state / "events.jsonl").read_bytes() == before
    assert _git_identity(root) == identity


@pytest.mark.parametrize("workspace", [False, True])
@pytest.mark.parametrize("drift", ["missing", "runner", "manifest", "final_callback"])
def test_profile_evidence_selected_target_refuses_without_release(tmp_path, monkeypatch, workspace, drift):
    root, state, _ = _prepared(tmp_path, monkeypatch, workspace=workspace)
    async def acquire():
        async with Client(mcp) as client:
            return await _claim(client, root, False)
    claim = _run(acquire())
    target = root / ("anvil-verification.toml" if drift == "manifest" else "verify.py")
    if drift == "missing":
        target.unlink()
    elif drift != "final_callback":
        target.write_text("changed source\n")
    else:
        original = SqliteBackend.append
        def append(self, draft, **kwargs):
            if draft.action == "evidence.submitted":
                callback = kwargs.get("pre_log_check")
                def change():
                    if callback:
                        callback()
                    target.write_text("changed at final callback\n")
                kwargs["pre_log_check"] = change
            return original(self, draft, **kwargs)
        monkeypatch.setattr(SqliteBackend, "append", append)
    unrelated = tmp_path / "elsewhere"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    monkeypatch.setenv("ANVIL_ROOT", str(unrelated))
    before = (state / "events.jsonl").read_bytes()
    async def exercise():
        async with Client(mcp) as client:
            with pytest.raises(ToolError, match="verification profile"):
                await client.call_tool("submit_completion_evidence", {
                    "task_id": "T001", "actor": "author", "commands_run": ["pytest -q"],
                    "files_changed": ["src/feature.txt"], "cwd": str(root),
                })
    _run(exercise())
    assert (state / "events.jsonl").read_bytes() == before
    backend = _open_backend(state, project_root=root)
    try:
        assert backend.get_claim(claim["id"]).status.value == "active"
        assert backend.get_latest_evidence("T001") is None
    finally:
        backend.close()
