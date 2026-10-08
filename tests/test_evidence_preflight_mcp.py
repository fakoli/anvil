"""Real hook capture, advisory reads and the final MCP evidence append boundary."""
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from anvil.attempt_view import read_evidence_preflight
from anvil.cli._helpers import _open_backend
from anvil.mcp_server import mcp
from anvil.state.sqlite import SqliteBackend
from tests.test_evidence_preflight import _capture, _claim
from tests.test_mcp import _data, _run
from tests.test_strict_evidence import _PLANNED_VERIFY_CMD


async def _submit(client, root, task):
    return _data(await client.call_tool("submit_completion_evidence", {
        "task_id": task, "actor": "agent-alpha", "commands_run": [_PLANNED_VERIFY_CMD],
        "files_changed": ["src/foo.py"], "cwd": str(root),
    }))


@pytest.mark.parametrize("count,failed", [(0, False), (1, False), (1, True), (16, True), (17, True)])
def test_real_capture_preflight_matches_shared_frontier_without_writes(
    tmp_path, monkeypatch, count, failed,
):
    task, claim = _claim(tmp_path)
    monkeypatch.setenv("ANVIL_CLAIM_ID", claim)
    for _ in range(count):
        _capture(tmp_path, failed=failed)
    state, buffer = tmp_path / ".anvil", tmp_path / ".anvil/.evidence-buffer" / f"{claim}.json"
    before = (state / "events.jsonl").read_bytes()
    original_buffer = buffer.read_bytes() if buffer.exists() else None
    observed = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    async def exercise():
        async with Client(mcp) as client:
            if count == 17:
                with pytest.raises(ToolError, match="could not be read completely"):
                    await client.call_tool("read_evidence_preflight", {
                        "cwd": str(tmp_path), "task_id": task, "observation_at": observed,
                    })
                with pytest.raises(ToolError, match="record limit"):
                    await _submit(client, tmp_path, task)
            else:
                result = _data(await client.call_tool("read_evidence_preflight", {
                    "cwd": str(tmp_path), "task_id": task, "observation_at": observed,
                }))
                assert result == read_evidence_preflight(state, task, observation_at=observed)
                assert result["buffer"]["valid_proof_count"] == count
                assert not result["mutation_authority"] and result["advisory_only"]
                assert all(p["satisfied"] for p in result["required_command_proofs"]) == bool(
                    count and not failed
                )
    _run(exercise())
    assert (state / "events.jsonl").read_bytes() == before
    assert (buffer.read_bytes() if buffer.exists() else None) == original_buffer


@pytest.mark.parametrize("failed", [False, True])
def test_real_submit_keeps_failed_command_proof_and_actual_auto_release(tmp_path, monkeypatch, failed):
    task, claim = _claim(tmp_path)
    monkeypatch.setenv("ANVIL_CLAIM_ID", claim)
    _capture(tmp_path, failed=failed)

    async def exercise():
        async with Client(mcp) as client:
            result = await _submit(client, tmp_path, task)
            assert result["hook_command_proofs"][0]["exit_code"] == int(failed)
            assert result["task_status"] == "needs_review"
            assert bool(result["missing_claim_bound_proofs"]) == failed
    _run(exercise())
    backend = _open_backend(tmp_path / ".anvil", project_root=tmp_path)
    try:
        evidence = backend.get_latest_evidence(task)
        assert evidence.proofs[0].exit_code == int(failed)
        assert backend.get_claim(claim).status.value == "released"
        assert backend.get_task(task).status.value == "needs_review"
    finally:
        backend.close()


@pytest.mark.parametrize("race", ["between_prepare_append", "same_content_after_open"])
def test_mcp_observed_instability_refuses_without_evidence_append(tmp_path, monkeypatch, race):
    task, claim = _claim(tmp_path)
    monkeypatch.setenv("ANVIL_CLAIM_ID", claim)
    _capture(tmp_path)
    state = tmp_path / ".anvil"
    buffer = state / ".evidence-buffer" / f"{claim}.json"
    before, original_buffer = (state / "events.jsonl").read_bytes(), buffer.read_bytes()
    observed = []
    if race == "between_prepare_append":
        original = SqliteBackend.append
        def appended(self, draft, **kwargs):
            if draft.action == "evidence.submitted":
                observed.append(True)
                buffer.write_bytes(original_buffer + original_buffer)
            return original(self, draft, **kwargs)
        monkeypatch.setattr(SqliteBackend, "append", appended)
    else:
        import anvil.claims.evidence_import as module
        original_open = module.os.open
        def opened(candidate, *args, **kwargs):
            descriptor = original_open(candidate, *args, **kwargs)
            if Path(candidate) == buffer and not observed:
                replacement = buffer.with_suffix(".replacement")
                replacement.write_bytes(original_buffer)
                replacement.replace(buffer)
                observed.append(True)
            return descriptor
        monkeypatch.setattr(module.os, "open", opened)

    async def exercise():
        async with Client(mcp) as client:
            with pytest.raises(ToolError, match="command.*buffer.*changed"):
                await _submit(client, tmp_path, task)
    _run(exercise())
    assert observed == [True]
    assert (state / "events.jsonl").read_bytes() == before
    assert buffer.read_bytes() == original_buffer * (2 if race == "between_prepare_append" else 1)
    backend = _open_backend(state, project_root=tmp_path)
    try:
        assert backend.get_latest_evidence(task) is None
        assert backend.get_claim(claim).status.value == "active"
    finally:
        backend.close()


@pytest.mark.parametrize("problem", ["duplicates", "malformed", "wrong_actor", "future_capture"])
def test_mcp_preflight_native_proof_problem_is_observational(tmp_path, monkeypatch, problem):
    task, claim = _claim(tmp_path)
    monkeypatch.setenv("ANVIL_CLAIM_ID", claim)
    _capture(tmp_path)
    state = tmp_path / ".anvil"
    buffer = state / ".evidence-buffer" / f"{claim}.json"
    record = json.loads(buffer.read_text())
    if problem == "duplicates":
        buffer.write_bytes(buffer.read_bytes() * 2)
    elif problem == "malformed":
        buffer.write_bytes(buffer.read_bytes() + b"{\n")
    else:
        # Recompute a valid proof digest so attribution/lifetime checking owns refusal.
        from anvil.state.models import HookCommandAttribution, hook_command_semantic_digest
        if problem == "wrong_actor":
            record["attribution"]["claimed_by"] = "foreign"
        else:
            record["timestamp"] = "2099-01-01T00:00:00Z"
        record["semantic_digest"] = hook_command_semantic_digest(
            command=record["command"], exit_code=record["exit_code"],
            output_sha256=record["output_sha256"],
            captured_at=datetime.fromisoformat(record["timestamp"]),
            attribution=HookCommandAttribution.model_validate(record["attribution"]),
        )
        buffer.write_text(json.dumps(record) + "\n")
    before, original_buffer = (state / "events.jsonl").read_bytes(), buffer.read_bytes()

    async def exercise():
        async with Client(mcp) as client:
            result = _data(await client.call_tool("read_evidence_preflight", {
                "task_id": task, "cwd": str(tmp_path),
                "observation_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            }))
        assert result["problems"] or result["buffer"]["skipped"]
        assert str(tmp_path) not in json.dumps(result)
    _run(exercise())
    assert (state / "events.jsonl").read_bytes() == before
    assert buffer.read_bytes() == original_buffer


@pytest.mark.parametrize("unsafe", ["directory", "symlink", "oversized"])
def test_unsafe_buffer_refuses_through_both_real_mcp_adapters(tmp_path, monkeypatch, unsafe):
    task, claim = _claim(tmp_path)
    monkeypatch.setenv("ANVIL_CLAIM_ID", claim)
    _capture(tmp_path)
    state = tmp_path / ".anvil"
    buffer = state / ".evidence-buffer" / f"{claim}.json"
    protected = tmp_path / "private-target"
    protected.write_bytes(b"preserved foreign bytes")
    if unsafe == "directory":
        buffer.unlink()
        buffer.mkdir()
    elif unsafe == "symlink":
        buffer.unlink()
        try:
            buffer.symlink_to(protected)
        except OSError:
            pytest.skip("native symlink creation unavailable")
    else:
        buffer.write_bytes(b"x" * 1_048_577)
    before = (state / "events.jsonl").read_bytes()
    async def exercise():
        async with Client(mcp) as client:
            with pytest.raises(ToolError, match="could not be read completely") as error:
                await client.call_tool("read_evidence_preflight", {"cwd": str(tmp_path), "task_id": task})
            assert str(protected) not in str(error.value)
            with pytest.raises(ToolError):
                await _submit(client, tmp_path, task)
    _run(exercise())
    assert protected.read_bytes() == b"preserved foreign bytes"
    assert (state / "events.jsonl").read_bytes() == before
