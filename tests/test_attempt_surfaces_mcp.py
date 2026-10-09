"""FastMCP advisory reads and exact-owner timing share native boundaries."""
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from anvil.attempt_view import read_attempt_view
from anvil.cli._helpers import _open_backend
from anvil.mcp_server import apply_surface_gate, mcp
from tests.test_evidence_preflight import _claim
from tests.test_mcp import _data, _run
from tests.test_profile_cli import _prepared
from tests.test_profile_mcp import _claim as _profile_claim
from tests.test_profile_planning_cli import _approve, _invoke, _project
from tests.test_timing_receipts import _receipt


def _files(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*")
        if p.is_file() and not p.name.endswith(("-wal", "-shm"))}


@pytest.mark.parametrize("prd", ["default", "release"])
@pytest.mark.parametrize("workspace", [False, True])
def test_attempt_named_four_digit_identity_matches_shared_reader(tmp_path, monkeypatch, prd, workspace):
    root, state, source = _project(tmp_path, monkeypatch, prd, workspace=workspace)
    source.write_text(source.read_text().replace("T001", "T1000"))
    extra = [] if prd == "default" else ["--prd", prd]
    _invoke(root, ["prd", "parse", *extra])
    _approve(root, prd)
    _invoke(root, ["plan", "--no-llm", *extra])
    before = _files(state)
    task = "T1000" if prd == "default" else "release:T1000"

    def forbidden(*args, **kwargs):
        pytest.fail("advisory MCP opened mutable backend, reaped or asserted custody")

    monkeypatch.setattr("anvil.mcp_server._open_backend", forbidden)
    monkeypatch.setattr("anvil.mcp_server._reap_stale", forbidden)
    monkeypatch.setattr("anvil.roots.registry.RootSetRegistry", forbidden)

    async def exercise():
        async with Client(mcp) as client:
            result = _data(await client.call_tool("get_attempt_view", {
                "task_id": task, "prd_id": prd, "cwd": str(root),
            }))
        assert result == read_attempt_view(state, task, prd_id=prd)
        assert result["identity"]["stored_task_id"] == task
        assert not result["mutation_authority"]
        assert str(root) not in json.dumps(result)
    _run(exercise())
    assert _files(state) == before


def test_new_read_tools_are_on_default_execution_surface():
    async def exercise():
        apply_surface_gate(mcp, {})
        try:
            async with Client(mcp) as client:
                names = {tool.name for tool in await client.list_tools()}
                assert {"get_attempt_view", "read_evidence_preflight"} <= names
                assert "parse_prd" not in names
        finally:
            apply_surface_gate(mcp, {"ANVIL_MCP_PLANNING": "1"})
    _run(exercise())


def test_bundle_view_has_one_shared_frontier_and_native_members(tmp_path, monkeypatch):
    root, state, _ = _prepared(tmp_path, monkeypatch, bundle=True)
    async def exercise():
        async with Client(mcp) as client:
            await _profile_claim(client, root, True)
            before = _files(state)
            result = _data(await client.call_tool("get_attempt_view", {
                "task_id": "B1", "bundle": True, "cwd": str(root),
            }))
        assert result == read_attempt_view(state, "B1", bundle=True)
        assert result["members"][0]["current"]["claim"]["bundle_claim_id"] is not None
        assert result["members"][0]["current"]["claim"]["proof_attribution"]["repository_id"] is None
        assert _files(state) == before
    _run(exercise())


@pytest.mark.parametrize("tool", ["get_attempt_view", "read_evidence_preflight"])
@pytest.mark.parametrize("changes", [
    {"task_id": "missing"}, {"task_id": "../private-hostile"},
    {"limits": {"max_event_records": 0}}, {"limits": {"max_event_records": True}},
    {"limits": {"max_event_records": 20_001}}, {"limits": {"max_response_bytes": 1}},
    {"observation_at": "2026-10-08T01:00:00"},
])
def test_advisory_refusal_is_fixed_json_without_mutation(tmp_path, monkeypatch, tool, changes):
    task, _ = _claim(tmp_path)
    state, before = tmp_path / ".anvil", _files(tmp_path / ".anvil")
    async def exercise():
        async with Client(mcp) as client:
            with pytest.raises(ToolError) as error:
                await client.call_tool(tool, {"task_id": task, "cwd": str(tmp_path), **changes})
        refusal = json.loads(str(error.value))
        assert "code" in refusal and "schema_id" in refusal
        assert str(tmp_path) not in str(error.value)
        assert "private-hostile" not in str(error.value)
    _run(exercise())
    assert _files(state) == before


@pytest.mark.parametrize("bundle", [1, "true", None])
def test_bundle_switch_rejects_non_boolean_on_wire(tmp_path, bundle):
    async def exercise():
        async with Client(mcp) as client:
            with pytest.raises(ToolError):
                await client.call_tool("get_attempt_view", {
                    "task_id": "B1", "cwd": str(tmp_path), "bundle": bundle,
                })
    _run(exercise())


def _claimed_receipt(tmp_path, monkeypatch):
    root, state, _ = _prepared(tmp_path, monkeypatch)
    async def exercise():
        async with Client(mcp) as client:
            return await _profile_claim(client, root, False)
    claim = _run(exercise())
    backend = _open_backend(state, project_root=root)
    try:
        attribution = {
            **{key: claim["attestation_context"][key] for key in (
                "repository_id", "claim_start_sha", "prd_id", "prd_revision", "task_revision",
            )}, "project_id": backend.get_project().id,
            "claim_id": claim["id"], "generation": claim["generation"],
            "claimed_by": "author", "task_id": "T001",
        }
        at = backend.get_claim(claim["id"]).created_at.isoformat().replace("+00:00", "Z")
        timing = _receipt(attribution=attribution, started_at=at, ended_at=at)
        return root, state, claim["id"], timing
    finally:
        backend.close()


@pytest.mark.parametrize("outcome", ["succeeded", "failed", "interrupted"])
def test_real_timing_audit_cannot_renew_or_mutate_task(tmp_path, monkeypatch, outcome):
    root, state, claim_id, timing = _claimed_receipt(tmp_path, monkeypatch)
    if outcome == "failed":
        timing.update(outcome="failed", exit_code=1)
    elif outcome == "interrupted":
        timing.update(outcome="interrupted", ended_at=None, exit_code=None)
    backend = _open_backend(state, project_root=root)
    try:
        task, claim = backend.get_task("T001"), backend.get_claim(claim_id)
        async def exercise():
            async with Client(mcp) as client:
                for _ in range(2):
                    result = _data(await client.call_tool("submit_progress", {
                        "task_id": "T001", "actor": "author", "timing": timing,
                        "phase": "tests", "detail": "observed", "cwd": str(root),
                    }))
                    assert result["recorded"] and result["event_action"] == "progress.noted"
        _run(exercise())
        assert backend.get_task("T001") == task
        assert backend.get_claim(claim_id) == claim
        events = [json.loads(x) for x in (state / "events.jsonl").read_text().splitlines()]
        notes = [e["payload_json"] for e in events if e["action"] == "progress.noted"]
        assert len(notes) == 2 and notes[0]["notes"] == "Command timing observation"
        assert notes[0]["timing"]["outcome"] == outcome
        assert notes[0]["phase"] == "tests" and notes[0]["detail"] == "observed"
        assert backend.get_latest_evidence("T001") is None
    finally:
        backend.close()


@pytest.mark.parametrize("change", ["generation", "actor", "project", "future", "expired", "malformed", "attestation"])
def test_timing_invalid_or_ineligible_is_safe_and_does_not_reap(tmp_path, monkeypatch, change):
    root, state, claim_id, timing = _claimed_receipt(tmp_path, monkeypatch)
    kwargs = {}
    if change in {"generation", "actor", "project"}:
        field, value = {"generation": ("generation", 2), "actor": ("claimed_by", "other"),
                        "project": ("project_id", "other")}[change]
        timing["attribution"][field] = value
    elif change == "future":
        timing["started_at"] = "2099-01-01T00:00:00Z"
    elif change == "expired":
        from anvil.clock import FrozenClock
        monkeypatch.setattr("anvil.clock.SystemClock", lambda: FrozenClock(datetime.now(UTC) + timedelta(hours=1)))
    elif change == "malformed":
        timing["output"] = "private receipt text"
    else:
        kwargs["attestation_base64"] = ""
    before = _files(state)
    before.pop("audit.jsonl", None)
    async def exercise():
        async with Client(mcp) as client:
            with pytest.raises(ToolError, match="timing_receipt_") as error:
                await client.call_tool("submit_progress", {
                    "task_id": "T001", "actor": "author", "cwd": str(root),
                    "timing": timing, **kwargs,
                })
        assert "private receipt text" not in str(error.value)
    _run(exercise())
    after = _files(state)
    after.pop("audit.jsonl", None)  # Native rejection audit is not a domain event.
    assert after == before


def test_malformed_timing_refuses_before_backend_open(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid timing opened backend")
    monkeypatch.setattr("anvil.mcp_server._open_backend", forbidden)
    async def exercise():
        async with Client(mcp) as client:
            with pytest.raises(ToolError, match="timing_receipt_invalid"):
                await client.call_tool("submit_progress", {
                    "task_id": "T001", "actor": "author", "timing": {"output": "x" * 20_000},
                    "cwd": str(tmp_path),
                })
    _run(exercise())


def test_plain_notes_preserve_absent_timing_shape(tmp_path, monkeypatch):
    root, state, claim_id, _ = _claimed_receipt(tmp_path, monkeypatch)
    async def exercise():
        async with Client(mcp) as client:
            await client.call_tool("submit_progress", {
                "task_id": "T001", "actor": "author", "notes": "old", "cwd": str(root),
            })
            with pytest.raises(ToolError, match="notes is required"):
                await client.call_tool("submit_progress", {
                    "task_id": "T001", "actor": "author", "cwd": str(root),
                })
    _run(exercise())
    event = json.loads((state / "events.jsonl").read_text().splitlines()[-1])
    assert set(event["payload_json"]) == {"task_id", "actor", "notes", "noted_at"}


@pytest.mark.parametrize("bundle", [False, True])
def test_timing_requires_ordinary_frozen_claim_context(tmp_path, monkeypatch, bundle):
    if bundle:
        root, state, _ = _prepared(tmp_path, monkeypatch, bundle=True)
        async def acquire():
            async with Client(mcp) as client:
                await _profile_claim(client, root, True)
        _run(acquire())
    else:
        root, state = tmp_path, tmp_path / ".anvil"
        _claim(root)
    backend = _open_backend(state, project_root=root)
    try:
        claim = backend.list_active_claims()[0]
        task = backend.get_task(claim.task_id)
        from anvil.state.models import task_snapshot_revision
        attribution = {
            "project_id": backend.get_project().id, "task_id": task.id,
            "claim_id": claim.id, "claimed_by": claim.claimed_by,
            "generation": claim.generation, "prd_id": task.prd_id,
            "prd_revision": backend.get_prd(task.prd_id).revision,
            "task_revision": task_snapshot_revision(task), "repository_id": "a" * 64,
            "claim_start_sha": "b" * 40,
        }
        at = claim.created_at.isoformat().replace("+00:00", "Z")
        timing = _receipt(attribution=attribution, started_at=at, ended_at=at)
    finally:
        backend.close()
    before = _files(state)
    before.pop("audit.jsonl", None)
    async def exercise():
        async with Client(mcp) as client:
            with pytest.raises(ToolError, match="timing_receipt_refused"):
                await client.call_tool("submit_progress", {
                    "task_id": task.id, "actor": claim.claimed_by,
                    "timing": timing, "cwd": str(root),
                })
    _run(exercise())
    after = _files(state)
    after.pop("audit.jsonl", None)  # Native rejection audit is not a domain event.
    assert after == before


def test_mcp_help_uses_generic_surface_counts():
    from anvil.mcp_server import _help_text
    text = _help_text()
    assert "full tool surface" in text
    assert "36-tool" not in text and "24 execution tools" not in text


@pytest.mark.parametrize("tool", ["get_attempt_view", "read_evidence_preflight"])
def test_advisory_invalid_configured_root_has_fixed_path_free_error(tmp_path, monkeypatch, tool):
    private = tmp_path / "private-root-name"
    private.mkdir()
    monkeypatch.setenv("ANVIL_ROOT", str(private))
    before = _files(private)
    async def exercise():
        async with Client(mcp) as client:
            with pytest.raises(ToolError) as refused:
                await client.call_tool(tool, {"task_id": "T001"})
        assert json.loads(str(refused.value)) == {
            "schema_id": "anvil.state.attempt-view-error.v1", "code": "state_unavailable",
            "field": "state", "actual": None, "limit": None,
            "message": "The attempt view could not be read completely.",
        }
        assert str(private) not in str(refused.value)
    _run(exercise())
    assert _files(private) == before
