"""Actual MCP claims preserve selected Git targets and transactional custody."""
import json
import sqlite3
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from anvil.cli._helpers import _open_backend
from anvil.mcp_server import mcp
from anvil.state.backend import EventRejected, TransactionAborted
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
@pytest.mark.parametrize("write_error,audit_memory", [
    (sqlite3.OperationalError, False), (EventRejected, False), (EventRejected, True),
])
def test_mcp_postlog_failure_retains_recoverable_git(
    tmp_path, monkeypatch, bundle, write_error, audit_memory,
):
    root, state, _ = _prepared(tmp_path, monkeypatch, bundle=bundle)
    before = (state / "events.jsonl").read_bytes()
    action = "bundle.claimed" if bundle else "claim.created"
    writer = "_write_bundle_claimed" if bundle else "_write_claim_created"
    observed = []
    audit_failures = []
    dumps = json.dumps
    audit_path = state / "audit.jsonl"
    before_audit = audit_path.read_bytes() if audit_path.exists() else b""

    def fail_audit_serialization(value, *args, **kwargs):
        if isinstance(value, dict) and value.get("kind") == "write_failed_after_log":
            assert value["action"] == action
            error = MemoryError("injected MCP audit serialization allocation failure")
            audit_failures.append((value["event_id"], error))
            raise error
        return dumps(value, *args, **kwargs)

    def fail_after_log(backend, conn, payload, event):
        events = [json.loads(line) for line in
            (state / "events.jsonl").read_bytes()[len(before):].splitlines()]
        assert len(events) == 1 and events[0]["id"] == event.id
        assert events[0]["action"] == action
        assert payload.git_metadata.target_path == str(root)
        assert root.is_dir()
        assert _git(root, "symbolic-ref", "--short", "HEAD") == payload.branch
        assert _git(root, "rev-parse", payload.branch) == _git(root, "rev-parse", "HEAD")
        observed.append((payload.id, payload.branch, event.id))
        raise write_error("injected MCP post-log projection failure")

    async def exercise():
        async with Client(mcp) as client:
            with pytest.raises(ToolError, match=("audit serialization allocation failure"
                if audit_memory else "log line remains")):
                await _claim(client, root, bundle)

    with monkeypatch.context() as patch:
        patch.setattr(SqliteBackend, writer, fail_after_log)
        if audit_memory:
            patch.setattr(json, "dumps", fail_audit_serialization)
        _run(exercise())
    assert len(observed) == 1
    if audit_memory:
        assert len(audit_failures) == 1
        assert audit_failures[0][0] == observed[0][2]
        assert isinstance(audit_failures[0][1].__context__, EventRejected)
        assert (audit_path.read_bytes() if audit_path.exists() else b"") == before_audit
    else:
        assert audit_failures == []
    published_log = (state / "events.jsonl").read_bytes()
    backend = _open_backend(state, project_root=root)
    try:
        claims = backend.list_active_claims()
        assert len(claims) == 1 and claims[0].status.value == "active"
        assert backend.get_task("T001").status.value == "claimed"
        recorded = backend.get_bundle_claim("B1") if bundle else claims[0]
        assert recorded.id == observed[0][0] and recorded.status.value == "active"
        if bundle:
            assert claims[0].bundle_claim_id == recorded.id
        assert recorded.branch == observed[0][1]
        assert recorded.git_metadata.target_path == str(root)
        assert claims[0].worktree_path is None
        assert (state / "events.jsonl").read_bytes() == published_log
        assert _git(root, "symbolic-ref", "--short", "HEAD") == recorded.branch
        assert _git(root, "rev-parse", recorded.branch) == _git(root, "rev-parse", "HEAD")
    finally:
        backend.close()


@pytest.mark.parametrize("bundle", [False, True])
@pytest.mark.parametrize("failure", [
    "unknown", "interruption_context", "aborted_context", "rejected_context", "profile",
])
def test_mcp_postcallback_failure_respects_publication_certainty(
    tmp_path, monkeypatch, bundle, failure,
):
    root, state, _ = _prepared(tmp_path, monkeypatch, bundle=bundle)
    before, identity = (state / "events.jsonl").read_bytes(), _git_identity(root)
    check = SqliteBackend._check_live_profiles
    observed = []

    def fail_after_callback(backend, conn, action, payload, prepared):
        check(backend, conn, action, payload, prepared)
        if action not in {"claim.created", "bundle.claimed"}:
            return
        branch = payload.git_metadata.branch
        assert _git(root, "symbolic-ref", "--short", "HEAD") == branch
        observed.append(branch)
        if failure == "profile":
            source = root / "verify.py"
            original = source.read_bytes()
            try:
                source.write_text("assert False\n")
                check(backend, conn, action, payload, prepared)
                pytest.fail("native final profile guard did not reject drift")
            finally:
                source.write_bytes(original)
        error = RuntimeError("injected MCP post-callback uncertainty")
        refusal = EventRejected("earlier prepublication refusal")
        if failure == "interruption_context":
            interrupted = KeyboardInterrupt("injected interruption context")
            interrupted.__context__ = refusal
            error.__cause__ = interrupted
        elif failure == "aborted_context":
            aborted = TransactionAborted("injected durable publication outcome")
            aborted.__context__ = refusal
            error.__cause__ = aborted
        elif failure == "rejected_context":
            error.__cause__ = refusal
        raise error

    monkeypatch.setattr(SqliteBackend, "_check_live_profiles", fail_after_callback)

    async def exercise():
        async with Client(mcp) as client:
            with pytest.raises(ToolError, match=("verification profile refused"
                if failure == "profile" else "injected MCP post-callback uncertainty")):
                await _claim(client, root, bundle)

    _run(exercise())
    assert len(observed) == 1
    assert (state / "events.jsonl").read_bytes() == before
    backend = _open_backend(state, project_root=root)
    try:
        assert backend.list_active_claims() == []
        assert backend.get_task("T001").status.value == "ready"
    finally:
        backend.close()
    if failure == "profile":
        assert _git_identity(root) == identity
    else:
        assert _git(root, "symbolic-ref", "--short", "HEAD") == observed[0]
        assert _git(root, "rev-parse", observed[0]) == _git(root, "rev-parse", "HEAD")


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
