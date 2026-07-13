"""Integration tests for the anvil MCP server (13 tools).

All tests use the FastMCP in-process Client — no HTTP, no mocking.
Each test runs against a real SqliteBackend in a per-test tmp_path.

The server resolves state via Path.cwd() / ".anvil", so every
test uses monkeypatch.chdir(tmp_path) to isolate cwd.

FastMCP 3.3.1 in-memory transport: Client(mcp) passes the server directly.

Return-value access:
  - Pydantic model returns:  result.structured_content  → dict
  - None returns:             result.data               → None
  - list returns:             result.data               → list
  - dict returns:             result.data / result.structured_content → dict

We use a unified _data() helper that covers all four cases cleanly.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from anvil.mcp_server import mcp

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_UTC = UTC
_T0 = datetime(2026, 5, 24, 18, 0, 0, tzinfo=_UTC)

# ---------------------------------------------------------------------------
# Result accessor
# ---------------------------------------------------------------------------


def _data(result: Any) -> Any:
    """Unified accessor for FastMCP call_tool() results.

    FastMCP 3.3.1 behavior:
    - Pydantic model return → result.data is a Root object (not subscriptable);
      result.structured_content is a plain dict.
    - list return           → result.data is a Python list (subscriptable).
    - dict return           → result.data is a Python dict (subscriptable).
    - None return           → result.data is None; result.content is [].

    This helper normalises everything to a plain Python value.
    """
    if result.data is None:
        return None
    d = result.data
    # Root objects are not dicts/lists; fall back to structured_content
    if not isinstance(d, (dict, list, str, int, float, bool)):
        return result.structured_content
    return d


# ---------------------------------------------------------------------------
# State-setup helpers (no mocking — real SQLite)
# ---------------------------------------------------------------------------


def _init_state_dir(tmp_path: Path, project_name: str = "Test Project") -> Path:
    """Create .anvil/ in tmp_path with project + events initialised.

    Mirrors what `anvil init` does; reuses SqliteBackend + event
    factories from the sqlite test layer so we don't duplicate CLI coupling.
    """
    from anvil.clock import SystemClock
    from anvil.state.models import EventDraft
    from anvil.state.sqlite import SqliteBackend

    state_dir = tmp_path / ".anvil"
    state_dir.mkdir()
    (state_dir / "packets").mkdir()
    # PS-2: snapshots/ is no longer pre-created; the `anvil snapshot`
    # command will create it on first use when implemented.
    (state_dir / "events.jsonl").touch()

    clock = SystemClock()
    now = clock.now()

    b = SqliteBackend(
        db_path=str(state_dir / "state.db"),
        events_path=str(state_dir / "events.jsonl"),
        clock=clock,
    )
    b.initialize()

    project_id = "proj-test"
    b.append(EventDraft(
        timestamp=now,
        actor="test",
        action="project.created",
        target_kind="project",
        target_id=project_id,
        payload_json={
            "id": project_id,
            "name": project_name,
            "description": "A test project.",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        },
    ))
    b.append(EventDraft(
        timestamp=now,
        actor="test",
        action="state.initialized",
        target_kind="project",
        target_id=project_id,
        payload_json={},
    ))
    b.close()
    return state_dir


def _add_prd(
    state_dir: Path,
    status: str = "reviewed",
    *,
    prd_id: str = "default",
    is_default: int = 1,
) -> None:
    """Insert a PRD row directly via SQLite.

    status options: 'draft', 'reviewed', 'approved'

    Defaults insert a v7-correct DEFAULT PRD (``id='default'``, ``is_default=1``)
    that the no-arg ``get_prd()`` resolves. Pass ``prd_id`` + ``is_default=0`` to
    seed an additional NON-default PRD (e.g. a multi-PRD per-PRD-gate test).
    """
    db_path = str(state_dir / "state.db")
    iso = "2026-05-24T18:00:00+00:00"
    conn = sqlite3.connect(db_path)
    conn.execute("""
        INSERT OR REPLACE INTO prds
        (id, project_id, status, summary, goals, non_goals, requirements,
         acceptance_criteria, risks, open_questions,
         is_default, created_at, updated_at)
        VALUES (?, 'proj-test', ?, 'Test summary.', '[]', '[]', '[]',
                '[]', '[]', '[]', ?, ?, ?)
    """, (prd_id, status, is_default, iso, iso))
    conn.commit()
    conn.close()


def _add_feature(state_dir: Path, feat_id: str = "F001", title: str = "Test Feature") -> None:
    """Insert a feature row directly via SQLite."""
    db_path = str(state_dir / "state.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT OR IGNORE INTO features "
        "(id, title, description, status, requirements, tasks) "
        "VALUES (?, ?, 'desc', 'proposed', '[]', '[]')",
        (feat_id, title),
    )
    conn.commit()
    conn.close()


def _add_task(
    state_dir: Path,
    *,
    task_id: str = "T001",
    feature_id: str = "F001",
    prd_id: str = "default",
    title: str = "Test Task",
    status: str = "ready",
    priority: str = "medium",
    task_type: str = "feature",
    dependencies: list[str] | None = None,
    conflict_groups: list[str] | None = None,
    scores: dict[str, Any] | None = None,
    likely_files: list[str] | None = None,
    parent_task_id: str | None = None,
) -> None:
    """Insert a task row directly via SQLite."""
    db_path = str(state_dir / "state.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT OR REPLACE INTO tasks
        (id, feature_id, prd_id, title, description, status, priority, task_type,
         dependencies, conflict_groups, scores, acceptance_criteria,
         implementation_notes, verification, likely_files,
         parent_task_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'A test task.', ?, ?, ?,
         ?, ?, ?, '["Tests pass."]', '[]', '{}', ?,
         ?, ?, ?)""",
        (
            task_id,
            feature_id,
            prd_id,
            title,
            status,
            priority,
            task_type,
            json.dumps(dependencies or []),
            json.dumps(conflict_groups or []),
            json.dumps(scores or {}),
            json.dumps(likely_files or []),
            parent_task_id,
            _T0.isoformat(),
            _T0.isoformat(),
        ),
    )
    conn.commit()
    conn.close()


def _add_active_claim(
    state_dir: Path,
    *,
    claim_id: str = "C001",
    task_id: str = "T001",
    claimed_by: str = "agent-x",
    expected_files: list[str] | None = None,
    minutes_until_expiry: int = 30,
) -> None:
    """Insert an active claim row directly via SQLite."""
    db_path = str(state_dir / "state.db")
    now = datetime.now(UTC)
    expires = (now + timedelta(minutes=minutes_until_expiry)).isoformat()
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT OR REPLACE INTO claims
        (id, task_id, claimed_by, claim_type, status, expected_files,
         created_at, lease_expires_at, last_heartbeat_at)
        VALUES (?, ?, ?, 'task', 'active', ?, ?, ?, ?)""",
        (
            claim_id,
            task_id,
            claimed_by,
            json.dumps(expected_files or []),
            now.isoformat(),
            expires,
            now.isoformat(),
        ),
    )
    conn.commit()
    conn.close()


def _set_required_evidence(
    state_dir: Path, task_id: str, required: list[str]
) -> None:
    """Inject verification.required_evidence into a task row via SQLite.

    Mirrors the direct-DB mutation used in tests/test_strict_evidence.py — the
    planner does not surface required_evidence today, so tests set it directly.
    """
    db_path = str(state_dir / "state.db")
    conn = sqlite3.connect(db_path)
    verification_json = json.dumps(
        {
            "commands": ["pytest tests/ -v"],
            "manual_steps": [],
            "required_evidence": required,
        }
    )
    conn.execute(
        "UPDATE tasks SET verification = ? WHERE id = ?",
        (verification_json, task_id),
    )
    conn.commit()
    conn.close()


def _add_evidence(
    state_dir: Path,
    *,
    evidence_id: str = "EV0001",
    task_id: str = "T001",
    claim_id: str = "C001",
    commands_run: list[str] | None = None,
    files_changed: list[str] | None = None,
    screenshots: list[str] | None = None,
    pr_url: str | None = None,
    commit_sha: str | None = None,
    output_excerpt: str | None = None,
    known_limitations: str | None = None,
) -> None:
    """Insert an evidence row directly via SQLite (mirrors submit's INSERT)."""
    db_path = str(state_dir / "state.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT OR REPLACE INTO evidence
        (id, task_id, claim_id, commands_run, output_excerpt,
         files_changed, pr_url, commit_sha, screenshots,
         known_limitations, submitted_at, submitted_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            evidence_id,
            task_id,
            claim_id,
            json.dumps(commands_run or []),
            output_excerpt,
            json.dumps(files_changed or []),
            pr_url,
            commit_sha,
            json.dumps(screenshots or []),
            known_limitations,
            _T0.isoformat(),
            "agent-x",
        ),
    )
    conn.commit()
    conn.close()


def _task_status(state_dir: Path, task_id: str) -> str | None:
    """Read a task's current status directly from SQLite."""
    db_path = str(state_dir / "state.db")
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Sync runner — bridges pytest sync to async FastMCP client
# ---------------------------------------------------------------------------


def _run(coro: Any) -> Any:
    """Run a coroutine synchronously (pytest without pytest-asyncio)."""
    return asyncio.run(coro)


# ===========================================================================
# Test: list_tools — all 35 registered (full surface, planning gate off)
# ===========================================================================

# The 11 one-shot planning tools, tagged ``planning`` and hidden from the live
# wire surface by default (L2). The remaining 24 are the always-on execution
# surface.
_PLANNING_TOOLS = {
    "init_project", "parse_prd", "review_prd", "plan_tasks", "score_tasks",
    "review_tasks", "apply_review_decision", "edit_dependencies",
    "find_decisions", "describe_surface",
    "create_bundle",
}
_EXECUTION_TOOLS = {
    "get_project_summary", "list_tasks", "get_task", "get_next_task",
    "claim_task", "release_task", "renew_claim", "generate_work_packet",
    "submit_progress", "submit_completion_evidence", "check_conflicts",
    "get_dependency_graph", "update_task_status", "get_project_status",
    "list_bundles", "get_bundle", "claim_bundle", "generate_bundle_packet",
    "submit_bundle_progress", "record_bundle_review", "finalize_bundle_review",
    "checkpoint_bundle", "reconcile_bundle", "supersede_bundle",
}
_ALL_TOOLS = _PLANNING_TOOLS | _EXECUTION_TOOLS


class TestListTools:
    def test_list_tools_returns_all_thirty_five(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # The autouse _full_mcp_surface fixture guarantees the planning surface
        # is enabled, so the in-process client sees every registered tool.
        _init_state_dir(tmp_path)
        monkeypatch.chdir(tmp_path)

        async def run() -> set[str]:
            async with Client(mcp) as c:
                tools = await c.list_tools()
                return {t.name for t in tools}

        names = _run(run())
        assert _ALL_TOOLS <= names, f"Missing tools: {_ALL_TOOLS - names}"
        assert len(_ALL_TOOLS) == 35


# ===========================================================================
# Test: planning-surface gate (L2) — execution vs planning split
# ===========================================================================


class TestPlanningSurfaceGate:
    """The planning surface is hidden from the live wire by default and exposed
    only when ANVIL_MCP_PLANNING is truthy — without removing any tool from the
    registry. Calls re-apply the gate via ``apply_surface_gate``; the autouse
    fixture restores the full surface afterward."""

    def _wire_names(self) -> set[str]:
        async def run() -> set[str]:
            async with Client(mcp) as c:
                return {t.name for t in await c.list_tools()}

        return _run(run())

    def test_default_hides_planning_tools(self) -> None:
        from anvil.mcp_server import apply_surface_gate

        exposed = apply_surface_gate(mcp, env={})
        assert exposed is False
        names = self._wire_names()
        assert _EXECUTION_TOOLS <= names, f"Missing exec tools: {_EXECUTION_TOOLS - names}"
        assert names.isdisjoint(_PLANNING_TOOLS), (
            f"Planning tools leaked into default surface: {names & _PLANNING_TOOLS}"
        )
        assert len(names) == 24

    def test_env_flag_exposes_full_surface(self) -> None:
        from anvil.mcp_server import apply_surface_gate

        exposed = apply_surface_gate(mcp, env={"ANVIL_MCP_PLANNING": "1"})
        assert exposed is True
        names = self._wire_names()
        assert _ALL_TOOLS <= names
        assert len(names & _ALL_TOOLS) == 35

    @pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on", "On"])
    def test_truthy_values_enable(self, val: str) -> None:
        from anvil.mcp_server import _planning_surface_enabled

        assert _planning_surface_enabled({"ANVIL_MCP_PLANNING": val}) is True

    @pytest.mark.parametrize("val", ["", "0", "false", "no", "off", "  ", "nope"])
    def test_falsy_or_unset_values_disable(self, val: str) -> None:
        from anvil.mcp_server import _planning_surface_enabled

        assert _planning_surface_enabled({"ANVIL_MCP_PLANNING": val}) is False
        assert _planning_surface_enabled({}) is False

    def test_gate_is_idempotent_and_reversible(self) -> None:
        from anvil.mcp_server import apply_surface_gate

        # Disable twice, then enable, then disable — converges each time.
        apply_surface_gate(mcp, env={})
        apply_surface_gate(mcp, env={})
        assert len(self._wire_names()) == 24
        apply_surface_gate(mcp, env={"ANVIL_MCP_PLANNING": "1"})
        assert len(self._wire_names() & _ALL_TOOLS) == 35
        apply_surface_gate(mcp, env={})
        assert len(self._wire_names()) == 24

    def test_gated_planning_tool_is_uncallable_but_returns_when_enabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from anvil.mcp_server import apply_surface_gate

        _init_state_dir(tmp_path)
        monkeypatch.chdir(tmp_path)

        # Gated: describe_surface (a planning tool) must be unreachable.
        apply_surface_gate(mcp, env={})

        async def call_gated() -> None:
            async with Client(mcp) as c:
                await c.call_tool("describe_surface", {})

        with pytest.raises(ToolError):
            _run(call_gated())

        # Enabled: it returns the full 35-tool manifest (introspection is
        # registry-based, so it reports the whole engine surface).
        apply_surface_gate(mcp, env={"ANVIL_MCP_PLANNING": "1"})

        async def call_enabled() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("describe_surface", {}))

        manifest = _run(call_enabled())
        assert manifest["mcp"]["count"] == 35

    def test_registry_still_reports_all_35_when_gated(self) -> None:
        # describe/mcp_tool_names introspect the registry, NOT the wire surface,
        # so the documented "full engine surface" never shrinks under the gate.
        from anvil.cli.describe import mcp_tool_names
        from anvil.mcp_server import apply_surface_gate

        apply_surface_gate(mcp, env={})
        names = set(mcp_tool_names())
        assert _ALL_TOOLS <= names
        assert len(names) == 35


class TestBundleTools:
    def _seed(self, tmp_path: Path) -> None:
        state_dir = _init_state_dir(tmp_path)
        _add_prd(state_dir, status="approved")
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", likely_files=["src/one.py"])
        _add_task(state_dir, task_id="T002", likely_files=["src/two.py"])
        _add_task(state_dir, task_id="T003", likely_files=["src/three.py"])

    def test_create_read_claim_packet_and_progress(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._seed(tmp_path)
        monkeypatch.chdir(tmp_path)

        async def run() -> tuple[Any, ...]:
            async with Client(mcp) as client:
                created = _data(
                    await client.call_tool(
                        "create_bundle",
                        {
                            "bundle_id": "B001",
                            "prd_id": "default",
                            "task_ids": ["T001", "T002"],
                            "coordinator": "coordinator",
                            "actor": "planner",
                        },
                    )
                )
                await client.call_tool(
                    "create_bundle",
                    {
                        "bundle_id": "B002",
                        "prd_id": "default",
                        "task_ids": ["T003"],
                        "coordinator": "coordinator",
                        "actor": "planner",
                    },
                )
                listed = _data(await client.call_tool("list_bundles", {}))
                fetched = _data(
                    await client.call_tool("get_bundle", {"bundle_id": "B001"})
                )
                status = _data(await client.call_tool("get_project_status", {}))
                checkpoint = _data(
                    await client.call_tool(
                        "checkpoint_bundle",
                        {
                            "bundle_id": "B001",
                            "actor": "coordinator",
                            "commit_sha": "abc123",
                        },
                    )
                )
                reconciled = _data(
                    await client.call_tool(
                        "reconcile_bundle",
                        {
                            "bundle_id": "B001",
                            "actor": "coordinator",
                            "commit_sha": "abc123",
                        },
                    )
                )
                superseded = _data(
                    await client.call_tool(
                        "supersede_bundle",
                        {
                            "bundle_id": "B002",
                            "replacement_bundle_id": "B001",
                            "actor": "coordinator",
                        },
                    )
                )
                claimed = _data(
                    await client.call_tool(
                        "claim_bundle",
                        {"bundle_id": "B001", "actor": "coordinator"},
                    )
                )
                packet = _data(
                    await client.call_tool(
                        "generate_bundle_packet",
                        {
                            "bundle_id": "B001",
                            "actor": "coordinator",
                            "format": "json",
                        },
                    )
                )
                progress = _data(
                    await client.call_tool(
                        "submit_bundle_progress",
                        {
                            "bundle_id": "B001",
                            "actor": "coordinator",
                            "phase": "implementing",
                        },
                    )
                )
                for task_id in ("T001", "T002"):
                    await client.call_tool(
                        "submit_completion_evidence",
                        {
                            "task_id": task_id,
                            "actor": "coordinator",
                            "commands_run": ["pytest -q"],
                            "files_changed": [f"src/{task_id.lower()}.py"],
                        },
                    )
                completed = _data(
                    await client.call_tool(
                        "submit_bundle_progress",
                        {
                            "bundle_id": "B001",
                            "actor": "coordinator",
                            "phase": "implemented",
                            "complete": True,
                        },
                    )
                )
                completed_retry = _data(
                    await client.call_tool(
                        "submit_bundle_progress",
                        {
                            "bundle_id": "B001",
                            "actor": "coordinator",
                            "phase": "implemented",
                            "complete": True,
                        },
                    )
                )
                reviews = []
                for reviewer, angle in (
                    ("reviewer-a", "correctness"),
                    ("reviewer-b", "security"),
                    ("reviewer-c", "integration"),
                ):
                    reviews.append(
                        _data(
                            await client.call_tool(
                                "record_bundle_review",
                                {
                                    "bundle_id": "B001",
                                    "actor": reviewer,
                                    "review_round": 1,
                                    "angle": angle,
                                    "decision": "approve",
                                },
                            )
                        )
                    )
                finalized = _data(
                    await client.call_tool(
                        "finalize_bundle_review",
                        {"bundle_id": "B001", "actor": "coordinator"},
                    )
                )
                reconciled = _data(
                    await client.call_tool(
                        "reconcile_bundle",
                        {
                            "bundle_id": "B001",
                            "actor": "coordinator",
                            "commit_sha": "abc123",
                        },
                    )
                )
                terminal = _data(
                    await client.call_tool(
                        "reconcile_bundle",
                        {
                            "bundle_id": "B001",
                            "actor": "coordinator",
                            "commit_sha": "abc123",
                            "merged": True,
                        },
                    )
                )
                terminal_status = _data(
                    await client.call_tool("get_project_status", {})
                )
                return (
                    created,
                    listed,
                    fetched,
                    status,
                    checkpoint,
                    claimed,
                    (
                        packet,
                        progress,
                        completed,
                        completed_retry,
                        reviews,
                        finalized,
                        reconciled,
                        terminal,
                        terminal_status,
                        superseded,
                    ),
                )

        created, listed, fetched, status, checkpoint, claimed, tail = _run(run())
        (
            packet,
            progress,
            completed,
            completed_retry,
            reviews,
            finalized,
            reconciled,
            terminal,
            terminal_status,
            superseded,
        ) = tail
        assert created["bundle"]["id"] == "B001"
        assert [entry["id"] for entry in listed["bundles"]] == ["B001", "B002"]
        assert fetched["claim"] is None
        assert status["bundles"][0]["bundle_id"] == "B001"
        assert checkpoint["checkpoint"]["commit_sha"] == "abc123"
        assert completed["bundle"]["status"] == "implemented_unreviewed"
        assert completed_retry["bundle"]["status"] == "implemented_unreviewed"
        assert reviews[-1]["gate"]["passed"] is True
        assert finalized["bundle"]["status"] == "reviewed_unintegrated"
        assert reconciled["bundle"]["status"] == "integrated"
        assert terminal["bundle"]["status"] == "merged"
        assert terminal_status["active_claim_count"] == 0
        terminal_rollup = next(
            entry
            for entry in terminal_status["bundles"]
            if entry["bundle_id"] == "B001"
        )
        assert terminal_rollup["status"] == "merged"
        assert terminal_rollup["coordinator_claim"]["status"] == "released"
        assert "already_claimed" not in {
            refusal["code"] for refusal in terminal_rollup["refusals"]
        }
        assert superseded["bundle"]["superseded_by"] == "B001"
        assert claimed["bundle"]["status"] == "active"
        assert set(claimed["claim"]["member_claim_ids"]) == {"T001", "T002"}
        assert packet["format"] == "json"
        assert packet["content"]["bundle"]["id"] == "B001"
        assert progress["recorded"] is True
        assert progress["bundle"]["status"] == "active"

    def test_existing_renew_release_tools_dispatch_bundle_ids(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._seed(tmp_path)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        cwd = str(tmp_path)

        async def run() -> tuple[Any, Any]:
            async with Client(mcp) as client:
                await client.call_tool(
                    "create_bundle",
                    {
                        "bundle_id": "B001",
                        "prd_id": "default",
                        "task_ids": ["T001"],
                        "coordinator": "coordinator",
                        "actor": "planner",
                        "cwd": cwd,
                    },
                )
                await client.call_tool(
                    "claim_bundle",
                    {"bundle_id": "B001", "actor": "coordinator", "cwd": cwd},
                )
                await client.call_tool(
                    "submit_bundle_progress",
                    {
                        "bundle_id": "B001",
                        "actor": "coordinator",
                        "phase": "working",
                        "cwd": cwd,
                    },
                )
                renewed = _data(
                    await client.call_tool(
                        "renew_claim",
                        {
                            "task_id": "B001",
                            "actor": "coordinator",
                            "target_kind": "bundle",
                            "cwd": cwd,
                        },
                    )
                )
                released = _data(
                    await client.call_tool(
                        "release_task",
                        {
                            "task_id": "B001",
                            "actor": "coordinator",
                            "reason": "handoff",
                            "target_kind": "bundle",
                            "cwd": cwd,
                        },
                    )
                )
                return renewed, released

        renewed, released = _run(run())
        assert renewed["renewed"] is True
        assert released["released"] is True
        assert released["claim_id"].startswith("BC")

    def test_failed_bundle_completion_does_not_append_progress(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._seed(tmp_path)
        monkeypatch.chdir(tmp_path)

        async def setup_and_fail() -> None:
            async with Client(mcp) as client:
                await client.call_tool(
                    "create_bundle",
                    {
                        "bundle_id": "B001",
                        "prd_id": "default",
                        "task_ids": ["T001"],
                        "coordinator": "coordinator",
                        "actor": "planner",
                    },
                )
                await client.call_tool(
                    "claim_bundle",
                    {"bundle_id": "B001", "actor": "coordinator"},
                )
                before = len(
                    (tmp_path / ".anvil" / "events.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()
                )
                with pytest.raises(ToolError, match="bundle_not_ready"):
                    await client.call_tool(
                        "submit_bundle_progress",
                        {
                            "bundle_id": "B001",
                            "actor": "coordinator",
                            "phase": "implemented",
                            "complete": True,
                        },
                    )
                after = len(
                    (tmp_path / ".anvil" / "events.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()
                )
                assert after == before

        _run(setup_and_fail())

    def test_lease_target_kind_disambiguates_colliding_ids(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._seed(tmp_path)
        _add_task(tmp_path / ".anvil", task_id="B001")
        monkeypatch.chdir(tmp_path)

        async def run() -> tuple[Any, Any]:
            async with Client(mcp) as client:
                task_claim = _data(
                    await client.call_tool(
                        "claim_task",
                        {"task_id": "B001", "claimed_by": "task-worker"},
                    )
                )
                await client.call_tool(
                    "create_bundle",
                    {
                        "bundle_id": "B001",
                        "prd_id": "default",
                        "task_ids": ["T001"],
                        "coordinator": "coordinator",
                        "actor": "planner",
                    },
                )
                bundle_claim = _data(
                    await client.call_tool(
                        "claim_bundle",
                        {"bundle_id": "B001", "actor": "coordinator"},
                    )
                )
                released_task = _data(
                    await client.call_tool(
                        "release_task",
                        {"task_id": "B001", "actor": "task-worker"},
                    )
                )
                still_active = _data(
                    await client.call_tool("get_bundle", {"bundle_id": "B001"})
                )
                return task_claim, (
                    bundle_claim,
                    released_task,
                    still_active,
                )

        task_claim, tail = _run(run())
        bundle_claim, released_task, still_active = tail
        assert released_task["claim_id"] == task_claim["id"]
        assert released_task["claim_id"] != bundle_claim["claim"]["id"]
        assert still_active["claim"]["status"] == "active"

    def test_registered_bundle_output_schema_names_core_fields(self) -> None:
        tools = _run(mcp.local_provider.list_tools())
        schema = next(tool.output_schema for tool in tools if tool.name == "get_bundle")
        bundle_schema = schema["properties"]["bundle"]
        if "$ref" in bundle_schema:
            bundle_schema = schema["$defs"][bundle_schema["$ref"].split("/")[-1]]
        assert {"id", "status", "task_ids", "coordinator"} <= set(
            bundle_schema["properties"]
        )
        agents_schema = bundle_schema["properties"]["delegated_agents"]["items"]
        if "$ref" in agents_schema:
            agents_schema = schema["$defs"][agents_schema["$ref"].split("/")[-1]]
        assert {"id", "status", "task_ids", "observed_at"} <= set(
            agents_schema["properties"]
        )

    def test_bundle_manager_uses_per_call_checkout_for_artifact_root(
        self, tmp_path: Path
    ) -> None:
        """Explicit MCP cwd must win over a detached HOME-workspace state dir."""
        from anvil.mcp_server import _bundle_manager, _open_backend

        workspace = tmp_path / "home" / ".anvil" / "workspaces" / "project"
        workspace.mkdir(parents=True)
        state_dir = _init_state_dir(workspace)
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        backend = _open_backend(state_dir)
        try:
            manager = _bundle_manager(
                backend, state_dir, "coordinator", cwd=str(checkout)
            )
            assert manager._project_root == checkout.resolve()
            assert manager._project_root != state_dir.parent
        finally:
            backend.close()

    @pytest.mark.parametrize(
        ("tool_name", "arguments"),
        [
            ("claim_bundle", {"bundle_id": "missing", "actor": "coordinator"}),
            (
                "generate_bundle_packet",
                {"bundle_id": "missing", "actor": "coordinator"},
            ),
            (
                "submit_bundle_progress",
                {"bundle_id": "missing", "actor": "coordinator", "phase": "x"},
            ),
            (
                "record_bundle_review",
                {
                    "bundle_id": "missing",
                    "actor": "reviewer",
                    "review_round": 1,
                    "angle": "correctness",
                    "decision": "approve",
                },
            ),
            (
                "finalize_bundle_review",
                {"bundle_id": "missing", "actor": "coordinator"},
            ),
            (
                "checkpoint_bundle",
                {
                    "bundle_id": "missing",
                    "actor": "coordinator",
                    "commit_sha": "abc123",
                },
            ),
            (
                "reconcile_bundle",
                {"bundle_id": "missing", "actor": "coordinator"},
            ),
            (
                "supersede_bundle",
                {
                    "bundle_id": "missing",
                    "replacement_bundle_id": "also-missing",
                    "actor": "coordinator",
                },
            ),
        ],
    )
    def test_bundle_errors_have_matching_code_prefix(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> None:
        self._seed(tmp_path)
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as client:
                await client.call_tool(tool_name, arguments)

        with pytest.raises(ToolError, match="bundle_error"):
            _run(run())


# ===========================================================================
# Tool 1: get_project_summary
# ===========================================================================

class TestGetProjectSummary:
    def test_happy_path_returns_project_fields(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        state_dir = _init_state_dir(tmp_path, "My Project")
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="ready")
        _add_task(state_dir, task_id="T002", status="blocked")
        _add_prd(state_dir, status="reviewed")
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("get_project_summary", {}))

        data = _run(run())
        assert data["project_name"] == "My Project"
        assert data["project_id"] == "proj-test"
        assert data["prd_status"] == "reviewed"
        assert data["ready_task_count"] == 1
        assert data["blocked_task_count"] == 1
        assert "task_counts" in data

    def test_error_when_not_initialized(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """ToolError raised when .anvil/ is absent."""
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("get_project_summary", {})

        with pytest.raises(ToolError, match="not initialized|anvil"):
            _run(run())

    def test_task_counts_all_statuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        for i, status in enumerate(["ready", "blocked", "done", "proposed"]):
            _add_task(state_dir, task_id=f"T{i+1:03}", status=status)
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("get_project_summary", {}))

        data = _run(run())
        counts = data["task_counts"]
        assert counts["ready"] == 1
        assert counts["blocked"] == 1
        assert counts["done"] == 1
        assert counts["proposed"] == 1

    def test_prds_rollup_additive_with_flat_totals(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """T020: get_project_summary grows a per-PRD ``prds`` list while keeping
        the flat fields as the project total. Default PRD (approved) owns 2 tasks
        + 1 claim; a second 'v0.2' PRD (draft) owns 1 ready task."""
        state_dir = _init_state_dir(tmp_path)
        _add_prd(state_dir, status="approved", prd_id="default", is_default=1)
        _add_prd(state_dir, status="draft", prd_id="v0.2", is_default=0)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="ready", prd_id="default")
        _add_task(state_dir, task_id="T002", status="claimed", prd_id="default")
        _add_task(state_dir, task_id="T900", status="ready", prd_id="v0.2")
        _add_active_claim(state_dir, claim_id="C001", task_id="T002")
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("get_project_summary", {}))

        data = _run(run())
        # Flat project totals retained.
        assert data["ready_task_count"] == 2
        assert data["active_claim_count"] == 1
        # Additive per-PRD rollup.
        by_id = {e["prd_id"]: e for e in data["prds"]}
        assert set(by_id) == {"default", "v0.2"}
        assert by_id["default"]["total_tasks"] == 2
        assert by_id["default"]["ready_task_count"] == 1
        assert by_id["default"]["active_claim_count"] == 1
        assert by_id["default"]["status"] == "approved"
        assert by_id["v0.2"]["total_tasks"] == 1
        assert by_id["v0.2"]["active_claim_count"] == 0
        assert by_id["v0.2"]["status"] == "draft"


# ===========================================================================
# Tool 2: list_tasks
# ===========================================================================

class TestListTasks:
    def test_happy_path_returns_all_tasks(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="ready")
        _add_task(state_dir, task_id="T002", status="blocked")
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("list_tasks", {}))

        tasks = _run(run())
        assert len(tasks) == 2
        ids = {t["id"] for t in tasks}
        assert ids == {"T001", "T002"}

    def test_filter_by_status(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="ready")
        _add_task(state_dir, task_id="T002", status="blocked")
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("list_tasks", {"status": "ready"}))

        tasks = _run(run())
        assert len(tasks) == 1
        assert tasks[0]["id"] == "T001"

    def test_filter_by_claimed_by(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="claimed")
        _add_task(state_dir, task_id="T002", status="claimed")
        _add_active_claim(state_dir, claim_id="C001", task_id="T001", claimed_by="agent-a")
        _add_active_claim(state_dir, claim_id="C002", task_id="T002", claimed_by="agent-b")
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("list_tasks", {"claimed_by": "agent-a"}))

        tasks = _run(run())
        assert len(tasks) == 1
        assert tasks[0]["id"] == "T001"

    def test_returns_empty_when_no_tasks(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _init_state_dir(tmp_path)
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("list_tasks", {}))

        tasks = _run(run())
        assert tasks == []

    def test_filter_by_task_type(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """list_tasks(task_type=...) scopes to that kind (T015)."""
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", task_type="feature")
        _add_task(state_dir, task_id="T002", task_type="bugfix")
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(
                    await c.call_tool("list_tasks", {"task_type": "bugfix"})
                )

        tasks = _run(run())
        assert len(tasks) == 1
        assert tasks[0]["id"] == "T002"
        assert tasks[0]["task_type"] == "bugfix"

    def test_cwd_param_resolves_other_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GAP-01: list_tasks(cwd=PROJECT) targets that project even when the
        server process cwd is a different (uninitialized) directory.

        Without the cwd param the tool resolved to Path.cwd() and broke across
        projects; this proves the param threads into _resolve_state_dir(cwd).
        """
        # Real project under tmp_path/project; tasks live here.
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        state_dir = _init_state_dir(project_dir)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="ready")
        _add_task(state_dir, task_id="T002", status="blocked")

        # Process cwd is an unrelated, uninitialized directory.
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(
                    await c.call_tool(
                        "list_tasks", {"cwd": str(project_dir)}
                    )
                )

        tasks = _run(run())
        ids = {t["id"] for t in tasks}
        assert ids == {"T001", "T002"}

    def test_cwd_param_combines_with_status_filter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GAP-01: cwd composes with the existing status filter."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        state_dir = _init_state_dir(project_dir)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="ready")
        _add_task(state_dir, task_id="T002", status="blocked")

        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(
                    await c.call_tool(
                        "list_tasks",
                        {"cwd": str(project_dir), "status": "ready"},
                    )
                )

        tasks = _run(run())
        assert len(tasks) == 1
        assert tasks[0]["id"] == "T001"


# ===========================================================================
# Tool 3: get_task
# ===========================================================================

class TestGetTask:
    def test_happy_path_returns_task(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", title="My Task", status="ready", priority="high")
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("get_task", {"task_id": "T001"}))

        task = _run(run())
        assert task["id"] == "T001"
        assert task["title"] == "My Task"
        assert task["status"] == "ready"
        assert task["priority"] == "high"
        # retro-opps T003 — derived tier on every task response; the raw
        # fixture task is unscored so it fails safe to max.
        assert task["review_tier"] == "max"

    def test_error_on_unknown_task_id(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _init_state_dir(tmp_path)
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("get_task", {"task_id": "nonexistent"})

        with pytest.raises(ToolError, match="not found|nonexistent"):
            _run(run())

    def test_review_tier_matches_cli_with_global_config_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """retro-opps T003 AC (review MUST-FIX): the MCP tier must equal the
        CLI tier for the same task even when a tier key lives ONLY in the
        GLOBAL config layer — i.e. both surfaces use the merged loader.
        Before the fix, MCP used load_config (no global merge) and derived a
        different tier than `anvil show`."""
        import json as _json
        import os as _os

        from typer.testing import CliRunner

        from anvil.cli import app

        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        # Confirmed low-complexity task with review_risk=3: the DEFAULT
        # light ceiling (2) derives "standard"; a GLOBAL-ONLY
        # review_tier_light_risk_max: 3 flips it to "light" — but only for
        # a surface that merges the global layer. Unscored fixtures cannot
        # catch this (both loaders derive "max" regardless).
        _add_task(
            state_dir,
            task_id="T001",
            status="ready",
            priority="high",
            scores={
                "complexity": 2,
                "parallelizability": 3,
                "context_load": 3,
                "blast_radius": 2,
                "review_risk": 3,
                "agent_suitability": 4,
                "blast_radius_confirmed": True,
                "review_risk_confirmed": True,
            },
        )
        # Project config EXISTS and omits the tier key (merge must supply it).
        (state_dir / "config.yaml").write_text(
            'project_name: "Tier Parity"\nproject_id: "tier-parity"\n',
            encoding="utf-8",
        )
        global_cfg = tmp_path / "global-config.yaml"
        global_cfg.write_text(
            "review_tier_light_risk_max: 3\n", encoding="utf-8"
        )
        monkeypatch.setenv("ANVIL_GLOBAL_CONFIG", str(global_cfg))
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("get_task", {"task_id": "T001"}))

        mcp_tier = _run(run())["review_tier"]

        runner = CliRunner()
        original = _os.getcwd()
        _os.chdir(tmp_path)
        try:
            result = runner.invoke(
                app, ["show", "T001", "--json"], catch_exceptions=False
            )
        finally:
            _os.chdir(original)
        assert result.exit_code == 0, result.output
        cli_tier = _json.loads(result.output.strip().splitlines()[-1])["data"][
            "review_tier"
        ]

        assert mcp_tier == cli_tier
        # Pin that the global layer was genuinely merged (not both-defaulted):
        # the raised light ceiling admits the confirmed risk-3 task.
        assert mcp_tier == "light"


# ===========================================================================
# Tool 4: get_next_task
# ===========================================================================

class TestGetNextTask:
    def test_happy_path_returns_highest_priority_ready_task(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="ready", priority="low")
        _add_task(state_dir, task_id="T002", status="ready", priority="high")
        _add_task(state_dir, task_id="T003", status="ready", priority="medium")
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("get_next_task", {}))

        task = _run(run())
        assert task is not None
        assert task["id"] == "T002"
        assert task["priority"] == "high"
        assert task["review_tier"] == "max"  # unscored fixture fails safe
        assert task["conflict_warnings"] == []  # T009 — no active overlap

    def test_conflict_warnings_surface_residual_overlap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """retro-opps T009 — a claim whose runtime expected_files overlap the
        recommended task's likely_files (no conflict group) yields advisory
        conflict_warnings; selection is untouched."""
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(
            state_dir, task_id="T001", status="claimed",
            likely_files=["src/alpha.py"],
        )
        _add_task(
            state_dir, task_id="T002", status="ready", priority="high",
            likely_files=["src/beta.py"],
        )
        _add_active_claim(
            state_dir, claim_id="C042", task_id="T001",
            claimed_by="other-agent", expected_files=["src/beta.py"],
        )
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("get_next_task", {}))

        task = _run(run())
        assert task is not None
        assert task["id"] == "T002"  # selection untouched
        assert task["conflict_warnings"] == [
            {
                "claim_id": "C042",
                "actor": "other-agent",
                "files": ["src/beta.py"],
            }
        ]

    def test_returns_none_when_no_ready_tasks(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="blocked")
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("get_next_task", {}))

        task = _run(run())
        assert task is None

    def test_priority_ordering_high_over_medium_over_low(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """HIGH > MEDIUM > LOW — same feature, different priorities."""
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        for task_id, priority in [("T001", "low"), ("T002", "medium"), ("T003", "high")]:
            _add_task(state_dir, task_id=task_id, status="ready", priority=priority)
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("get_next_task", {}))

        task = _run(run())
        assert task["id"] == "T003"

    def test_skips_task_with_unmet_dependency(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A ready task whose dep is not done must not be returned."""
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="ready", priority="high",
                  dependencies=["T002"])
        _add_task(state_dir, task_id="T002", status="ready", priority="low")
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("get_next_task", {}))

        # T001 has unmet dep; T002 has no deps — T002 is the only eligible task
        task = _run(run())
        assert task is not None
        assert task["id"] == "T002"

    def test_tiebreak_by_id_asc_when_same_priority_and_suitability(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Same priority + no scores → tiebreak by id ascending."""
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        for task_id in ["T003", "T001", "T002"]:
            _add_task(state_dir, task_id=task_id, status="ready", priority="medium")
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("get_next_task", {}))

        task = _run(run())
        assert task["id"] == "T001"

    def test_prd_id_narrows_candidates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """T019: get_next_task(prd_id='v0.2') only returns tasks in that PRD —
        a higher-priority default-PRD task is invisible once the pool is scoped."""
        state_dir = _init_state_dir(tmp_path)
        _add_prd(state_dir, status="approved", prd_id="v0.2", is_default=0)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="ready", priority="high",
                  prd_id="default")
        _add_task(state_dir, task_id="T900", status="ready", priority="low",
                  prd_id="v0.2")
        monkeypatch.chdir(tmp_path)

        async def run() -> tuple[Any, Any]:
            async with Client(mcp) as c:
                unscoped = _data(await c.call_tool("get_next_task", {}))
                scoped = _data(
                    await c.call_tool("get_next_task", {"prd_id": "v0.2"})
                )
                return unscoped, scoped

        unscoped, scoped = _run(run())
        assert unscoped["id"] == "T001"  # high-priority default-PRD task wins
        assert scoped["id"] == "T900"    # candidate pool narrowed to v0.2

    def test_ceiling_withholds_over_ceiling_and_unconfirmed_tasks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#56: get_next_task honors the risk-axis ceiling via the SAME
        within_risk_ceiling helper as ClaimManager.next_claimable, so a ceilinged
        runner is only ever offered confirmed-within-ceiling work. Mirrors
        test_claims.py::TestRiskAxisNext to prove the two seams agree."""
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        # A: confirmed blast 2 -> eligible under a <=3 ceiling.
        _add_task(state_dir, task_id="A", status="ready", priority="high",
                  scores={"blast_radius": 2, "blast_radius_confirmed": True})
        # B: blast 2 but UNCONFIRMED -> frontier-only under a ceiling.
        _add_task(state_dir, task_id="B", status="ready", priority="high",
                  scores={"blast_radius": 2, "blast_radius_confirmed": False})
        # C: confirmed blast 5 -> over the ceiling; highest suitability so it
        # wins the UNRESTRICTED pick, proving the ceiling changes the outcome.
        _add_task(state_dir, task_id="C", status="ready", priority="high",
                  scores={"blast_radius": 5, "blast_radius_confirmed": True,
                          "agent_suitability": 5})
        monkeypatch.chdir(tmp_path)

        async def run() -> tuple[Any, Any]:
            async with Client(mcp) as c:
                unrestricted = _data(await c.call_tool("get_next_task", {}))
                ceilinged = _data(
                    await c.call_tool("get_next_task", {"max_blast": 3})
                )
                return unrestricted, ceilinged

        unrestricted, ceilinged = _run(run())
        assert unrestricted["id"] == "C"  # over-ceiling task wins with no ceiling
        assert ceilinged is not None
        assert ceilinged["id"] == "A"  # ceiling withholds C (over) and B (unconfirmed)

    def test_prd_id_scoped_pick_skips_cross_prd_active_claim_collision(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """T019 core: get_next_task(prd_id='v0.1') builds the conflict-group
        exclusion from ALL PRDs first, so a v0.1 candidate colliding with an
        ACTIVE v0.2 claim is skipped while a non-colliding v0.1 task is still
        returned."""
        state_dir = _init_state_dir(tmp_path)
        _add_prd(state_dir, status="approved", prd_id="v0.1", is_default=0)
        _add_prd(state_dir, status="approved", prd_id="v0.2", is_default=0)
        _add_feature(state_dir)
        # ACTIVE claim on a v0.2 task in the shared conflict_group.
        _add_task(state_dir, task_id="T800", status="claimed",
                  conflict_groups=["CG-shared"], prd_id="v0.2")
        _add_active_claim(state_dir, claim_id="C001", task_id="T800")
        # v0.1 candidate in the SAME group → must be skipped (cross-PRD).
        _add_task(state_dir, task_id="T100", status="ready", priority="high",
                  conflict_groups=["CG-shared"], prd_id="v0.1")
        # v0.1 candidate with no collision → must be returned.
        _add_task(state_dir, task_id="T101", status="ready", priority="low",
                  conflict_groups=["CG-T101"], prd_id="v0.1")
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(
                    await c.call_tool("get_next_task", {"prd_id": "v0.1"})
                )

        task = _run(run())
        assert task is not None
        assert task["id"] == "T101"


# ===========================================================================
# Tool 5: claim_task
# ===========================================================================

class TestClaimTask:
    def test_happy_path_returns_claim_response(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="ready")
        _add_prd(state_dir, status="reviewed")
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("claim_task", {
                    "task_id": "T001",
                    "claimed_by": "agent-x",
                    "expected_files": ["src/foo.py"],
                }))

        claim = _run(run())
        assert claim["task_id"] == "T001"
        assert claim["claimed_by"] == "agent-x"
        assert "id" in claim
        assert "lease_expires_at" in claim
        assert claim["expected_files"] == ["src/foo.py"]

    def test_error_when_prd_is_draft(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Gate: owning PRD in 'draft' status → ToolError.

        T012: the refusal now flows through ClaimManager's per-PRD gate (the
        duplicated inline get_prd() pre-check was removed), so the message is the
        transition gate's 'PRD must be in {reviewed, approved}' text, not the old
        inline 'PRD is in draft status' string."""
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="ready")
        _add_prd(state_dir, status="draft")
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("claim_task", {
                    "task_id": "T001",
                    "claimed_by": "agent-x",
                })

        with pytest.raises(ToolError, match="PRD must be in"):
            _run(run())

    def test_error_when_prd_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Gate: no PRD at all → ToolError."""
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="ready")
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("claim_task", {
                    "task_id": "T001",
                    "claimed_by": "agent-x",
                })

        # Tightened from "missing|draft|PRD" (loose: only the bare 'PRD' substring
        # matched): the missing-PRD case now flows through ClaimManager Gate 3,
        # which raises "no PRD found" - pin that so a future reword can't pass silently.
        with pytest.raises(ToolError, match="no PRD found"):
            _run(run())

    def test_claims_task_in_approved_nondefault_prd_via_mcp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """T012 / finding-A: claim_task honors the task's OWNING PRD via
        ClaimManager now that the duplicated inline get_prd() gate is gone. The
        default PRD is DRAFT but the task's non-default PRD 'v0.2' is APPROVED,
        so the MCP claim SUCCEEDS - CLI and MCP agree. The pre-T012 inline gate
        resolved the draft default PRD and wrongly refused this claim."""
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_prd(state_dir, status="draft")  # default PRD: draft
        _add_prd(state_dir, status="approved", prd_id="v0.2", is_default=0)
        _add_task(state_dir, task_id="T900", status="ready", prd_id="v0.2")
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("claim_task", {
                    "task_id": "T900",
                    "claimed_by": "agent-x",
                }))

        claim = _run(run())
        assert claim["task_id"] == "T900"
        assert claim["claimed_by"] == "agent-x"

    def test_error_on_double_claim(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Claiming an already-claimed task raises ToolError (ClaimError bubble)."""
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="ready")
        _add_prd(state_dir, status="reviewed")
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("claim_task", {"task_id": "T001", "claimed_by": "agent-x"})
                await c.call_tool("claim_task", {"task_id": "T001", "claimed_by": "agent-y"})

        with pytest.raises(ToolError):
            _run(run())


# ===========================================================================
# Tool 6: release_task
# ===========================================================================

class TestReleaseTask:
    def test_happy_path_releases_claim(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="claimed")
        _add_active_claim(state_dir, claim_id="C001", task_id="T001", claimed_by="agent-x")
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("release_task", {
                    "task_id": "T001",
                    "actor": "agent-x",
                }))

        resp = _run(run())
        assert resp["released"] is True
        assert resp["claim_id"] == "C001"

    def test_error_when_no_active_claim(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="ready")
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("release_task", {
                    "task_id": "T001",
                    "actor": "agent-x",
                })

        with pytest.raises(ToolError, match="No active claim|released|never claimed"):
            _run(run())

    def test_error_when_actor_does_not_own_claim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Critic-PR#45 regression: foreign actor must not be able to release."""
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="claimed")
        _add_active_claim(state_dir, claim_id="C001", task_id="T001", claimed_by="agent-x")
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("release_task", {
                    "task_id": "T001",
                    "actor": "agent-y",
                })

        with pytest.raises(ToolError):
            _run(run())


# ===========================================================================
# Tool 7: renew_claim
# ===========================================================================

class TestRenewClaim:
    def test_happy_path_returns_updated_lease(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="claimed")
        _add_active_claim(state_dir, claim_id="C001", task_id="T001", claimed_by="agent-x",
                          minutes_until_expiry=5)
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("renew_claim", {
                    "task_id": "T001",
                    "actor": "agent-x",
                    "extend_seconds": 900,
                }))

        resp = _run(run())
        assert "lease_expires_at" in resp
        new_expiry = datetime.fromisoformat(resp["lease_expires_at"])
        assert new_expiry > datetime.now(UTC)
        # No expected_files declared → progress gate is permissive → real renewal.
        assert resp["renewed"] is True

    def test_noop_when_no_progress_reports_not_renewed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """B46 part 2: a heartbeat with no file change on an expected file is a
        no-op — the tool returns renewed=False and the (unchanged) lease."""
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="claimed")
        _add_active_claim(state_dir, claim_id="C001", task_id="T001", claimed_by="agent-x",
                          expected_files=["src/foo.py"], minutes_until_expiry=30)
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("renew_claim", {
                    "task_id": "T001",
                    "actor": "agent-x",
                    "extend_seconds": 900,
                }))

        resp = _run(run())
        assert resp["renewed"] is False

    def test_error_when_no_active_claim(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="ready")
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("renew_claim", {
                    "task_id": "T001",
                    "actor": "agent-x",
                })

        with pytest.raises(ToolError, match="No active claim|released|expired"):
            _run(run())

    def test_error_when_actor_does_not_own_claim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Critic-PR#45 regression: foreign actor must not be able to renew."""
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="claimed")
        _add_active_claim(state_dir, claim_id="C001", task_id="T001", claimed_by="agent-x",
                          minutes_until_expiry=5)
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("renew_claim", {
                    "task_id": "T001",
                    "actor": "agent-y",
                })

        with pytest.raises(ToolError):
            _run(run())


# ===========================================================================
# Tool 8: generate_work_packet
# ===========================================================================

class TestGenerateWorkPacket:
    def test_markdown_format_returns_string_content(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", title="Build Widget", status="ready")
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("generate_work_packet", {
                    "task_id": "T001",
                    "format": "markdown",
                }))

        resp = _run(run())
        assert resp["format"] == "markdown"
        assert isinstance(resp["content"], str)
        assert "Build Widget" in resp["content"]

    def test_json_format_returns_dict_content(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", title="Build Widget", status="ready")
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("generate_work_packet", {
                    "task_id": "T001",
                    "format": "json",
                }))

        resp = _run(run())
        assert resp["format"] == "json"
        assert isinstance(resp["content"], dict)

    def test_error_on_unknown_task(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _init_state_dir(tmp_path)
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("generate_work_packet", {"task_id": "NOPE"})

        with pytest.raises(ToolError, match="not found|NOPE"):
            _run(run())


# ===========================================================================
# Tool 9: submit_progress
# ===========================================================================

class TestSubmitProgress:
    def test_happy_path_returns_recorded_true(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="claimed")
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("submit_progress", {
                    "task_id": "T001",
                    "actor": "agent-x",
                    "notes": "Half done.",
                }))

        resp = _run(run())
        assert resp["recorded"] is True

    def test_phase_is_recorded_in_event_payload(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """retro-opps T010 — submit_progress(phase=...) lands in the
        progress.noted JSONL payload."""
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="claimed")
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("submit_progress", {
                    "task_id": "T001",
                    "actor": "agent-x",
                    "notes": "running tests",
                    "phase": "tests",
                }))

        resp = _run(run())
        assert resp["recorded"] is True

        events = (state_dir / "events.jsonl").read_text(encoding="utf-8")
        rows = [json.loads(line) for line in events.strip().splitlines()]
        progress_rows = [r for r in rows if r["action"] == "progress.noted"]
        assert progress_rows, "no progress.noted event appended"
        payload = progress_rows[-1]["payload_json"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        assert payload["phase"] == "tests"
        # detail omitted when None — a no-detail row keeps the old byte shape.
        assert "detail" not in payload

    def test_no_phase_row_keeps_pre_t010_shape(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """retro-opps T010 (review finding): when phase/detail are unused the
        payload is byte-identical to the pre-T010 shape, so an OLDER anvil
        sharing the same workspace state dir replays it unchanged."""
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="claimed")
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("submit_progress", {
                    "task_id": "T001",
                    "actor": "agent-x",
                    "notes": "no phase here",
                }))

        _run(run())
        events = (state_dir / "events.jsonl").read_text(encoding="utf-8")
        rows = [json.loads(line) for line in events.strip().splitlines()]
        payload = [r for r in rows if r["action"] == "progress.noted"][-1]["payload_json"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        assert set(payload) == {"task_id", "actor", "notes", "noted_at"}

    def test_does_not_change_task_status(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """submit_progress records a note but must not change the task status."""
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="claimed")
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                await c.call_tool("submit_progress", {
                    "task_id": "T001",
                    "actor": "agent-x",
                    "notes": "Still in progress.",
                })
                return _data(await c.call_tool("get_task", {"task_id": "T001"}))

        task = _run(run())
        assert task["status"] == "claimed"

    def test_error_on_unknown_task(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _init_state_dir(tmp_path)
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("submit_progress", {
                    "task_id": "NOPE",
                    "actor": "agent-x",
                    "notes": "n/a",
                })

        with pytest.raises(ToolError, match="not found|NOPE"):
            _run(run())


# ===========================================================================
# Tool 10: submit_completion_evidence
# ===========================================================================

class TestSubmitCompletionEvidence:
    def test_happy_path_transitions_task_to_needs_review(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="in_progress")
        _add_active_claim(state_dir, claim_id="C001", task_id="T001", claimed_by="agent-x")
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("submit_completion_evidence", {
                    "task_id": "T001",
                    "actor": "agent-x",
                    "commands_run": ["pytest tests/ -v"],
                    "files_changed": ["src/foo.py"],
                    "output_excerpt": "3 passed",
                }))

        resp = _run(run())
        assert "evidence_id" in resp
        assert resp["evidence_id"].startswith("EV")
        assert resp["task_status"] == "needs_review"

    def test_error_when_no_active_claim(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Must have an active claim before submitting evidence."""
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="ready")
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("submit_completion_evidence", {
                    "task_id": "T001",
                    "actor": "agent-x",
                    "commands_run": ["pytest"],
                    "files_changed": [],
                })

        with pytest.raises(ToolError, match="No active claim|Claim"):
            _run(run())

    def test_error_on_unknown_task(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _init_state_dir(tmp_path)
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("submit_completion_evidence", {
                    "task_id": "NOPE",
                    "actor": "agent-x",
                    "commands_run": [],
                    "files_changed": [],
                })

        with pytest.raises(ToolError, match="not found|NOPE"):
            _run(run())

    def test_error_when_actor_does_not_own_claim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Critic-PR#45 P1 regression: foreign actor cannot force-complete a claim."""
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="in_progress")
        _add_active_claim(state_dir, claim_id="C001", task_id="T001", claimed_by="agent-x")
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("submit_completion_evidence", {
                    "task_id": "T001",
                    "actor": "agent-y",  # owner is agent-x
                    "commands_run": ["pytest"],
                    "files_changed": ["src/foo.py"],
                })

        with pytest.raises(ToolError, match="claim owner|claimed by"):
            _run(run())

    def test_error_when_commands_run_is_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Critic-PR#45 regression: backend rejects empty commands_run on active claim."""
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="in_progress")
        _add_active_claim(state_dir, claim_id="C001", task_id="T001", claimed_by="agent-x")
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("submit_completion_evidence", {
                    "task_id": "T001",
                    "actor": "agent-x",
                    "commands_run": [],  # backend should reject
                    "files_changed": ["src/foo.py"],
                })

        with pytest.raises(ToolError):
            _run(run())


# ===========================================================================
# T014: next_ready field in finish/submit responses
# ===========================================================================

class TestNextReadyField:
    """The finish/submit surfaces name the next claimable task (T014).

    Covers MCP submit_completion_evidence and apply_review_decision: the
    next_ready field respects dependencies, active claims, conflict groups,
    and — critically — file-conflict exclusion against active claims.
    """

    def test_submit_evidence_names_next_ready_task(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After finishing T001, the response names the next ready task."""
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="in_progress")
        _add_active_claim(state_dir, claim_id="C001", task_id="T001", claimed_by="agent-x")
        # A second ready task with no conflicts — should be named as next.
        _add_task(state_dir, task_id="T002", status="ready", priority="high")
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("submit_completion_evidence", {
                    "task_id": "T001",
                    "actor": "agent-x",
                    "commands_run": ["pytest -q"],
                    "files_changed": ["src/foo.py"],
                }))

        resp = _run(run())
        assert resp["task_status"] == "needs_review"
        assert resp["next_ready"] is not None
        assert resp["next_ready"]["id"] == "T002"
        assert resp["next_ready"]["priority"] == "high"

    def test_submit_evidence_next_ready_null_when_none_available(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """next_ready is null when no other claimable task exists."""
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="in_progress")
        _add_active_claim(state_dir, claim_id="C001", task_id="T001", claimed_by="agent-x")
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("submit_completion_evidence", {
                    "task_id": "T001",
                    "actor": "agent-x",
                    "commands_run": ["pytest -q"],
                    "files_changed": ["src/foo.py"],
                }))

        resp = _run(run())
        assert resp["next_ready"] is None

    def test_submit_evidence_next_ready_excludes_file_conflict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A task whose files overlap another agent's active claim is excluded;
        the next non-overlapping task is named instead."""
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="in_progress")
        _add_active_claim(state_dir, claim_id="C001", task_id="T001", claimed_by="agent-x")
        # Another agent holds an active claim on shared.py.
        _add_task(state_dir, task_id="T099", status="in_progress")
        _add_active_claim(
            state_dir, claim_id="C099", task_id="T099",
            claimed_by="agent-y", expected_files=["src/shared.py"],
        )
        # T002 (high) overlaps the active claim's file → must be excluded.
        _add_task(
            state_dir, task_id="T002", status="ready", priority="high",
            likely_files=["src/shared.py"],
        )
        # T003 (medium) touches a different file → eligible.
        _add_task(
            state_dir, task_id="T003", status="ready", priority="medium",
            likely_files=["src/other.py"],
        )
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("submit_completion_evidence", {
                    "task_id": "T001",
                    "actor": "agent-x",
                    "commands_run": ["pytest -q"],
                    "files_changed": ["src/foo.py"],
                }))

        resp = _run(run())
        assert resp["next_ready"] is not None
        # T002 is higher priority but excluded by file overlap; T003 wins.
        assert resp["next_ready"]["id"] == "T003"

    def test_submit_evidence_category_recorded_and_invalid_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """evidence-contracts:T006 — MCP category param records the same
        evidence shape as the CLI; invalid values are rejected naming the
        valid set."""
        state_dir = _init_state_dir(tmp_path)
        _add_prd(state_dir, status="approved")
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="claimed")
        _add_active_claim(
            state_dir, claim_id="C001", task_id="T001", claimed_by="agent-x"
        )
        monkeypatch.chdir(tmp_path)

        async def bad() -> None:
            async with Client(mcp) as c:
                await c.call_tool("submit_completion_evidence", {
                    "task_id": "T001", "actor": "agent-x",
                    "commands_run": ["echo ok"], "files_changed": ["x.py"],
                    "category": "sometimes",
                })

        with pytest.raises(ToolError, match="invalid_category"):
            _run(bad())

        async def good() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("submit_completion_evidence", {
                    "task_id": "T001", "actor": "agent-x",
                    "commands_run": ["echo ok"], "files_changed": ["x.py"],
                    "category": "diagnostic",
                }))

        assert _run(good())["evidence_id"]
        import sqlite3 as _sq

        conn = _sq.connect(str(state_dir / "state.db"))
        row = conn.execute(
            "SELECT category FROM evidence WHERE task_id='T001'"
        ).fetchone()
        conn.close()
        assert row[0] == "diagnostic"

    def test_apply_review_decision_enforces_claim_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """evidence-contracts:T005 AC4 (review finding): the MCP apply path
        enforces the contract identically to the CLI — ToolError
        claim_unproven on a failing assertion, task stays needs_review,
        approves once the artifact passes."""
        state_dir = _init_state_dir(tmp_path)
        _add_prd(state_dir, status="approved")
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="needs_review")
        _add_active_claim(
            state_dir, claim_id="C001", task_id="T001", claimed_by="agent-x"
        )
        _add_evidence(
            state_dir, evidence_id="EV0001", task_id="T001", claim_id="C001",
            commands_run=["echo ok"], files_changed=["x.py"],
        )
        # Attach the evidence contract directly (the fixture INSERT predates
        # claims/verification knobs).
        conn = sqlite3.connect(str(state_dir / "state.db"))
        conn.execute(
            "UPDATE tasks SET claims = ?, verification = ? WHERE id = 'T001'",
            (
                json.dumps([{"id": "candidate_measured", "subject": "gemma"}]),
                json.dumps({
                    "commands": ["echo ok"],
                    "artifact_assertions": [{
                        "artifact": "evidence-out.json",
                        "claim": "candidate_measured",
                        "assertions": [
                            {"path": "status", "op": "equals", "value": "measured"}
                        ],
                    }],
                }),
            ),
        )
        conn.commit()
        conn.close()
        (tmp_path / "evidence-out.json").write_text(
            json.dumps({"status": "failed"}), encoding="utf-8"
        )
        monkeypatch.chdir(tmp_path)

        async def refuse() -> None:
            async with Client(mcp) as c:
                await c.call_tool("apply_review_decision", {
                    "task_id": "T001", "approve": True, "reviewer": "human",
                })

        with pytest.raises(ToolError, match="claim_unproven"):
            _run(refuse())

        async def status_of() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("get_task", {"task_id": "T001"}))

        assert _run(status_of())["status"] == "needs_review"

        (tmp_path / "evidence-out.json").write_text(
            json.dumps({"status": "measured"}), encoding="utf-8"
        )

        async def approve() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("apply_review_decision", {
                    "task_id": "T001", "approve": True, "reviewer": "human",
                }))

        assert _run(approve())["decision"] == "accepted"

    def test_apply_review_decision_names_next_ready(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Approving a task surfaces the next claimable task in the response."""
        state_dir = _init_state_dir(tmp_path)
        _add_prd(state_dir, status="approved")
        _add_feature(state_dir)
        # T001 in needs_review (with evidence) about to be approved.
        _add_task(state_dir, task_id="T001", status="needs_review")
        _add_active_claim(state_dir, claim_id="C001", task_id="T001", claimed_by="agent-x")
        _add_evidence(state_dir, evidence_id="EV0001", task_id="T001", claim_id="C001",
                      commands_run=["pytest -q"], files_changed=["src/foo.py"])
        # T002 ready and unblocked.
        _add_task(state_dir, task_id="T002", status="ready", priority="medium")
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("apply_review_decision", {
                    "task_id": "T001",
                    "approve": True,
                    "reviewer": "human",
                }))

        resp = _run(run())
        assert resp["decision"] == "accepted"
        assert resp["next_ready"] is not None
        assert resp["next_ready"]["id"] == "T002"


# ===========================================================================
# Tool 11: check_conflicts
# ===========================================================================

class TestCheckConflicts:
    def test_no_conflicts_when_no_active_claims(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="ready")
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("check_conflicts", {
                    "task_id": "T001",
                    "proposed_files": ["src/foo.py"],
                }))

        resp = _run(run())
        assert resp["conflicts"] == []

    def test_conflict_detected_when_file_overlaps_other_claim(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Prior claim on T002 touching src/foo.py conflicts with T001's proposed_files."""
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="ready")
        _add_task(state_dir, task_id="T002", status="claimed")
        _add_active_claim(state_dir, claim_id="C002", task_id="T002", claimed_by="agent-b",
                          expected_files=["src/foo.py", "src/bar.py"])
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("check_conflicts", {
                    "task_id": "T001",
                    "proposed_files": ["src/foo.py"],
                }))

        resp = _run(run())
        assert len(resp["conflicts"]) == 1
        conflict = resp["conflicts"][0]
        assert conflict["file"] == "src/foo.py"
        assert conflict["task_id"] == "T002"
        assert conflict["claimed_by"] == "agent-b"
        assert "claim_id" in conflict

    def test_own_claim_excluded_from_conflicts(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """T001's own active claim must not appear as a conflict for T001."""
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="claimed")
        _add_active_claim(state_dir, claim_id="C001", task_id="T001", claimed_by="agent-x",
                          expected_files=["src/foo.py"])
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("check_conflicts", {
                    "task_id": "T001",
                    "proposed_files": ["src/foo.py"],
                }))

        resp = _run(run())
        assert resp["conflicts"] == []


# ===========================================================================
# Tool 12: get_dependency_graph
# ===========================================================================

class TestGetDependencyGraph:
    def test_happy_path_all_scope_returns_nodes_and_edges(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="done")
        _add_task(state_dir, task_id="T002", status="ready", dependencies=["T001"])
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("get_dependency_graph", {"scope": "all"}))

        resp = _run(run())
        node_ids = {n["id"] for n in resp["nodes"]}
        assert "T001" in node_ids
        assert "T002" in node_ids
        assert any(e["from"] == "T001" and e["to"] == "T002" for e in resp["edges"])

    def test_ready_to_claim_excludes_tasks_with_unmet_deps(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        # T001 ready but dep T002 not done → NOT ready_to_claim
        _add_task(state_dir, task_id="T001", status="ready", dependencies=["T002"])
        # T002 ready, no deps → ready_to_claim
        _add_task(state_dir, task_id="T002", status="ready")
        # T003 ready, dep T004 (done) → ready_to_claim
        _add_task(state_dir, task_id="T003", status="ready", dependencies=["T004"])
        _add_task(state_dir, task_id="T004", status="done")
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("get_dependency_graph", {"scope": "all"}))

        resp = _run(run())
        ready = set(resp["ready_to_claim"])
        assert "T001" not in ready
        assert "T002" in ready
        assert "T003" in ready
        assert "T004" not in ready

    def test_error_feature_scope_without_target_id(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _init_state_dir(tmp_path)
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("get_dependency_graph", {"scope": "feature"})

        with pytest.raises(ToolError, match="target_id"):
            _run(run())

    def test_task_scope_returns_transitive_deps(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """scope='task' returns the target task plus all transitive dependencies."""
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="done")
        _add_task(state_dir, task_id="T002", status="done", dependencies=["T001"])
        _add_task(state_dir, task_id="T003", status="ready", dependencies=["T002"])
        _add_task(state_dir, task_id="T004", status="ready")  # unrelated
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("get_dependency_graph", {
                    "scope": "task",
                    "target_id": "T003",
                }))

        resp = _run(run())
        node_ids = {n["id"] for n in resp["nodes"]}
        assert "T001" in node_ids
        assert "T002" in node_ids
        assert "T003" in node_ids
        assert "T004" not in node_ids


# ===========================================================================
# Tool 12b: edit_dependencies — batch dependency-edit primitive (T022/F007)
# ===========================================================================

def _deps_of(state_dir: Path, task_id: str) -> list[str]:
    """Read a task's persisted dependency list directly from state.db."""
    conn = sqlite3.connect(str(state_dir / "state.db"))
    try:
        row = conn.execute(
            "SELECT dependencies FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return list(json.loads(row[0]))


class TestEditDependencies:
    def test_batch_add_applies_all_edges(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        for n in range(1, 6):
            _add_task(state_dir, task_id=f"T00{n}", status="ready")
        monkeypatch.chdir(tmp_path)

        # Chain T002->T001, ..., T005->T004 (4 edges) in one call.
        add = [[f"T00{n}", f"T00{n - 1}"] for n in range(2, 6)]

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("edit_dependencies", {
                    "actor": "agent-x",
                    "add": add,
                }))

        resp = _run(run())
        assert len(resp["added"]) == 4
        assert set(resp["changed"]) == {"T002", "T003", "T004", "T005"}
        for n in range(2, 6):
            assert _deps_of(state_dir, f"T00{n}") == [f"T00{n - 1}"]

    def test_batch_cycle_rejected_no_partial_apply(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="ready")
        _add_task(state_dir, task_id="T002", status="ready")
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("edit_dependencies", {
                    "actor": "agent-x",
                    "add": [["T001", "T002"], ["T002", "T001"]],
                })

        with pytest.raises(ToolError, match="cycle"):
            _run(run())

        # No partial application: neither task gained a dependency.
        assert _deps_of(state_dir, "T001") == []
        assert _deps_of(state_dir, "T002") == []

    def test_unknown_task_rejects_whole_batch(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="ready")
        _add_task(state_dir, task_id="T002", status="ready")
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("edit_dependencies", {
                    "actor": "agent-x",
                    "add": [["T002", "T001"], ["T002", "T999"]],
                })

        with pytest.raises(ToolError):
            _run(run())
        # The valid edge in the rejected batch must NOT have applied.
        assert _deps_of(state_dir, "T002") == []


# ===========================================================================
# Tool 13: update_task_status
# ===========================================================================

class TestUpdateTaskStatus:
    def test_drafted_to_ready(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="drafted")
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("update_task_status", {
                    "task_id": "T001",
                    "to_status": "ready",
                    "actor": "agent-x",
                }))

        resp = _run(run())
        assert resp["from_status"] == "drafted"
        assert resp["to_status"] == "ready"

    def test_ready_to_drafted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="ready")
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("update_task_status", {
                    "task_id": "T001",
                    "to_status": "drafted",
                    "actor": "agent-x",
                }))

        resp = _run(run())
        assert resp["from_status"] == "ready"
        assert resp["to_status"] == "drafted"

    def test_in_progress_to_blocked(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="in_progress")
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("update_task_status", {
                    "task_id": "T001",
                    "to_status": "blocked",
                    "actor": "agent-x",
                    "reason": "Waiting for dependency.",
                }))

        resp = _run(run())
        assert resp["from_status"] == "in_progress"
        assert resp["to_status"] == "blocked"

    def test_blocked_to_in_progress(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """blocked → in_progress is allowed (blocked toggle for claimed/in_progress tasks)."""
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="blocked")
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("update_task_status", {
                    "task_id": "T001",
                    "to_status": "in_progress",
                    "actor": "agent-x",
                }))

        resp = _run(run())
        assert resp["from_status"] == "blocked"
        assert resp["to_status"] == "in_progress"

    def test_error_disallowed_transition(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """proposed → ready is not in the allowed set → ToolError."""
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="proposed")
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("update_task_status", {
                    "task_id": "T001",
                    "to_status": "ready",
                    "actor": "agent-x",
                })

        with pytest.raises(ToolError, match="Cannot transition|proposed|none"):
            _run(run())

    def test_error_on_unknown_task(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _init_state_dir(tmp_path)
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("update_task_status", {
                    "task_id": "NOPE",
                    "to_status": "ready",
                    "actor": "agent-x",
                })

        with pytest.raises(ToolError, match="not found|NOPE"):
            _run(run())


# ===========================================================================
# End-to-end: full agent lifecycle
# ===========================================================================

class TestFullAgentLifecycle:
    """claim_task → renew_claim → submit_progress → submit_completion_evidence.

    After evidence submission the claim is auto-released; a subsequent
    release_task call must fail with "no active claim".
    """

    def test_full_lifecycle(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="ready")
        _add_prd(state_dir, status="approved")
        monkeypatch.chdir(tmp_path)

        async def run() -> bool:
            async with Client(mcp) as c:
                # 1. Claim
                claim = _data(await c.call_tool("claim_task", {
                    "task_id": "T001",
                    "claimed_by": "agent-lifecycle",
                    "expected_files": ["src/widget.py"],
                }))
                assert claim["task_id"] == "T001"
                assert claim["claimed_by"] == "agent-lifecycle"

                # 2. Renew
                renew = _data(await c.call_tool("renew_claim", {
                    "task_id": "T001",
                    "actor": "agent-lifecycle",
                    "extend_seconds": 600,
                }))
                assert "lease_expires_at" in renew

                # 3. Submit progress
                progress = _data(await c.call_tool("submit_progress", {
                    "task_id": "T001",
                    "actor": "agent-lifecycle",
                    "notes": "50% complete.",
                }))
                assert progress["recorded"] is True

                # 4. Submit completion evidence — auto-releases claim
                evidence = _data(await c.call_tool("submit_completion_evidence", {
                    "task_id": "T001",
                    "actor": "agent-lifecycle",
                    "commands_run": ["pytest tests/"],
                    "files_changed": ["src/widget.py"],
                    "output_excerpt": "All tests pass.",
                }))
                assert evidence["task_status"] == "needs_review"
                assert evidence["evidence_id"].startswith("EV")
            return True

        assert _run(run()) is True

        # After evidence submission the claim is auto-released.
        # Attempting release again must fail with "no active claim".
        async def verify_released() -> None:
            async with Client(mcp) as c:
                with pytest.raises(ToolError, match="No active claim|released"):
                    await c.call_tool("release_task", {
                        "task_id": "T001",
                        "actor": "agent-lifecycle",
                    })

        _run(verify_released())


# ===========================================================================
# End-to-end: check_conflicts sees conflict created by claim_task
# ===========================================================================

class TestConflictsAfterClaim:
    def test_conflict_appears_after_claim_task(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """claim_task populates expected_files in the active claim; check_conflicts sees them."""
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="ready")
        _add_task(state_dir, task_id="T002", status="ready")
        _add_prd(state_dir, status="reviewed")
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                # Agent A claims T001 with src/shared.py
                await c.call_tool("claim_task", {
                    "task_id": "T001",
                    "claimed_by": "agent-a",
                    "expected_files": ["src/shared.py"],
                })
                # Agent B checks conflicts for T002 also touching src/shared.py
                return _data(await c.call_tool("check_conflicts", {
                    "task_id": "T002",
                    "proposed_files": ["src/shared.py"],
                }))

        resp = _run(run())
        assert len(resp["conflicts"]) == 1
        assert resp["conflicts"][0]["file"] == "src/shared.py"
        assert resp["conflicts"][0]["task_id"] == "T001"


# ===========================================================================
# End-to-end: get_dependency_graph ready_to_claim after seeding deps
# ===========================================================================

class TestDependencyGraphReadyToClaim:
    def test_ready_to_claim_correct_after_dep_seeding(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="done")
        _add_task(state_dir, task_id="T002", status="ready", dependencies=["T001"])
        _add_task(state_dir, task_id="T003", status="ready", dependencies=["T002"])
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("get_dependency_graph", {"scope": "all"}))

        resp = _run(run())
        ready = set(resp["ready_to_claim"])
        assert "T002" in ready      # dep T001 is done
        assert "T003" not in ready  # dep T002 is not done
        assert "T001" not in ready  # done tasks are not ready_to_claim


# ===========================================================================
# End-to-end: get_next_task priority ordering with sequential claiming
# ===========================================================================

class TestGetNextTaskPriorityOrdering:
    def test_priority_high_over_medium_over_low(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T_LOW",  status="ready", priority="low")
        _add_task(state_dir, task_id="T_MED",  status="ready", priority="medium")
        _add_task(state_dir, task_id="T_HIGH", status="ready", priority="high")
        _add_prd(state_dir, status="reviewed")
        monkeypatch.chdir(tmp_path)

        results: list[str] = []

        async def run() -> None:
            async with Client(mcp) as c:
                for _ in range(3):
                    next_task = _data(await c.call_tool("get_next_task", {}))
                    if next_task is None:
                        break
                    results.append(next_task["id"])
                    await c.call_tool("claim_task", {
                        "task_id": next_task["id"],
                        "claimed_by": "ordering-agent",
                    })

        _run(run())
        assert results[0] == "T_HIGH", f"Expected T_HIGH first, got {results}"
        assert results[1] == "T_MED",  f"Expected T_MED second, got {results}"
        assert results[2] == "T_LOW",  f"Expected T_LOW third, got {results}"

    def test_tiebreak_agent_suitability_desc_then_id_asc(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Same priority: agent_suitability desc tiebreak, then id asc."""
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T_A", status="ready", priority="medium",
                  scores={"agent_suitability": 5})
        _add_task(state_dir, task_id="T_B", status="ready", priority="medium",
                  scores={"agent_suitability": 2})
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("get_next_task", {}))

        task = _run(run())
        assert task["id"] == "T_A"


# ===========================================================================
# v1.13.0 workflow tools — fixtures
# ===========================================================================

_MINIMAL_PRD = """\
# Project: MCP Test Project

## Summary

A project for MCP workflow testing.

## Goals

- Verify the workflow MCP tools end-to-end.

## Requirements

- R001: The system accepts input.
- R002: The system produces output.

## Features

### F001: Core Feature

The single feature exercised by the test PRD.

**Requirements:** R001, R002

## Tasks

### T001: Wire input handler

**Feature:** F001
**Priority:** high
**Likely files:** src/app/handler.py

**Acceptance criteria:**

- Input is parsed without error.
- Invalid input is rejected.

**Verification:**

- `pytest tests/test_handler.py -v`

### T002: Wire output writer

**Feature:** F001
**Priority:** medium
**Likely files:** src/app/writer.py

**Acceptance criteria:**

- Output is written atomically.

**Verification:**

- `pytest tests/test_writer.py -v`
"""


def _write_prd_file(state_dir: Path, content: str = _MINIMAL_PRD) -> Path:
    """Drop a PRD file into .anvil/prd.md."""
    prd_path = state_dir / "prd.md"
    prd_path.write_text(content, encoding="utf-8")
    return prd_path


# A re-parse of ``_MINIMAL_PRD``: R001 dropped (→ superseded), R002 carried
# forward (→ unchanged), R003 added — a material change that exercises the
# prd.revised diff and the approved→draft status demotion.
_MINIMAL_PRD_V2 = """\
# Project: MCP Test Project

## Summary

A project for MCP workflow testing, revised.

## Goals

- Verify the workflow MCP tools end-to-end.

## Requirements

- R002: The system produces output.
- R003: The system logs activity.
"""

# A re-parse that re-lists R001 — an id retired in a prior revision. Because
# requirement ids are permanent lineage (single ``id`` PK), this cannot be
# revived and must be rejected rather than silently dropped.
_MINIMAL_PRD_READD = """\
# Project: MCP Test Project

## Summary

A project for MCP workflow testing, re-adding a retired id.

## Goals

- Verify the workflow MCP tools end-to-end.

## Requirements

- R001: The system accepts input, restored.
- R002: The system produces output.
"""


def _events_with_action(state_dir: Path, action: str) -> list[dict[str, Any]]:
    """Return the payloads of every events.jsonl line with the given action."""
    out: list[dict[str, Any]] = []
    text = (state_dir / "events.jsonl").read_text(encoding="utf-8")
    for line in text.splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get("action") == action:
            out.append(event.get("payload_json") or event.get("payload") or {})
    return out


# ===========================================================================
# Tool 14: init_project
# ===========================================================================


class TestInitProject:
    def test_happy_path_creates_state_dir_and_seeds_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("init_project", {
                    "name": "From MCP",
                }))

        resp = _run(run())
        assert resp["created"] is True
        assert resp["project_name"] == "From MCP"
        assert resp["project_id"] == "from-mcp"
        # T019: init_project reports the default PRD partition a fresh project
        # owns. prd_id is a REQUIRED field on InitProjectResponse (no field
        # default), so dropping the explicit `prd_id=DEFAULT_PRD_ID` assignment
        # in init_project raises a construction error rather than being masked
        # by a field default that would still serialize 'default'.
        from anvil.state.models import DEFAULT_PRD_ID

        assert resp["prd_id"] == DEFAULT_PRD_ID
        state_dir = tmp_path / ".anvil"
        assert state_dir.exists()
        assert (state_dir / "state.db").exists()
        assert (state_dir / "events.jsonl").exists()
        assert (state_dir / "config.yaml").exists()
        assert (state_dir / "packets").is_dir()

    def test_error_when_already_initialized(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_state_dir(tmp_path)
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("init_project", {})

        with pytest.raises(ToolError, match="already exists|reinitialize"):
            _run(run())

    def test_refuses_plugin_root_under_local_layout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """B44: under the legacy local layout, init_project refuses at the plugin root."""
        manifest = tmp_path / ".claude-plugin" / "plugin.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text('{"name": "anvil"}', encoding="utf-8")
        monkeypatch.delenv("ANVIL_ROOT", raising=False)
        monkeypatch.chdir(tmp_path)  # autouse fixture pins ANVIL_STATE_LAYOUT=local

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("init_project", {})

        with pytest.raises(ToolError, match="plugin root"):
            _run(run())

    def test_allows_plugin_root_under_workspace_layout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """B44: under the default workspace layout, init at the plugin root does NOT
        refuse — state goes to ~/.anvil/, never into the repo (dogfooding case)."""
        manifest = tmp_path / ".claude-plugin" / "plugin.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text('{"name": "anvil"}', encoding="utf-8")
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        monkeypatch.delenv("ANVIL_ROOT", raising=False)
        monkeypatch.setenv("ANVIL_STATE_LAYOUT", "workspace")
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("init_project", {"name": "X"}))

        assert _run(run())["created"] is True  # not refused


# ===========================================================================
# Tool 15: get_project_status
# ===========================================================================


class TestGetProjectStatus:
    def test_happy_path_returns_full_status(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state_dir = _init_state_dir(tmp_path, "Status Project")
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="ready")
        _add_task(state_dir, task_id="T002", status="ready")
        _add_task(state_dir, task_id="T003", status="blocked")
        _add_active_claim(
            state_dir, claim_id="C001", task_id="T001", claimed_by="agent-x"
        )
        _add_prd(state_dir, status="reviewed")
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("get_project_status", {}))

        data = _run(run())
        assert data["initialized"] is True
        assert data["project_name"] == "Status Project"
        assert data["prd_status"] == "reviewed"
        assert data["total_tasks"] == 3
        assert data["ready_queue_depth"] == 2
        assert data["active_claim_count"] == 1
        assert data["task_counts"]["ready"] == 2
        assert data["task_counts"]["blocked"] == 1

    def test_uninitialized_returns_initialized_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No ToolError — status doubles as the bootstrap probe."""
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("get_project_status", {}))

        data = _run(run())
        assert data["initialized"] is False
        assert data["project_id"] is None
        assert data["total_tasks"] == 0
        assert data["active_claim_count"] == 0
        # T020: uninitialized DB has no PRDs to roll up.
        assert data["prds"] == []

    def test_prds_rollup_additive_with_flat_totals(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """T020: get_project_status grows a per-PRD ``prds`` list while keeping
        the flat fields as the project total."""
        state_dir = _init_state_dir(tmp_path, "Status Project")
        _add_prd(state_dir, status="approved", prd_id="default", is_default=1)
        _add_prd(state_dir, status="draft", prd_id="v0.2", is_default=0)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="ready", prd_id="default")
        _add_task(state_dir, task_id="T002", status="blocked", prd_id="default")
        _add_task(state_dir, task_id="T900", status="ready", prd_id="v0.2")
        _add_active_claim(state_dir, claim_id="C001", task_id="T001")
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("get_project_status", {}))

        data = _run(run())
        # Flat project totals retained.
        assert data["total_tasks"] == 3
        assert data["ready_queue_depth"] == 2
        assert data["active_claim_count"] == 1
        # Additive per-PRD rollup.
        by_id = {e["prd_id"]: e for e in data["prds"]}
        assert set(by_id) == {"default", "v0.2"}
        assert by_id["default"]["total_tasks"] == 2
        assert by_id["default"]["ready_task_count"] == 1
        assert by_id["default"]["active_claim_count"] == 1
        assert by_id["default"]["task_counts"]["blocked"] == 1
        assert by_id["v0.2"]["total_tasks"] == 1
        assert by_id["v0.2"]["ready_task_count"] == 1
        assert by_id["v0.2"]["active_claim_count"] == 0


# ===========================================================================
# Tool 16: parse_prd
# ===========================================================================


class TestParsePrd:
    def test_happy_path_emits_prd_parsed_and_returns_counts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state_dir = _init_state_dir(tmp_path)
        _write_prd_file(state_dir)
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("parse_prd", {}))

        resp = _run(run())
        assert resp["requirement_count"] == 2
        assert resp["feature_count"] == 1
        assert resp["task_count"] == 2
        assert resp["errors"] == []
        assert resp["prd_status"] == "draft"
        # Verify the PRD was actually persisted.
        from anvil.clock import SystemClock
        from anvil.state.sqlite import SqliteBackend
        b = SqliteBackend(
            db_path=str(state_dir / "state.db"),
            events_path=str(state_dir / "events.jsonl"),
            clock=SystemClock(),
        )
        b.initialize()
        try:
            prd = b.get_prd()
            assert prd is not None
            assert prd.status.value == "draft"
        finally:
            b.close()

    def test_error_when_no_prd_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_state_dir(tmp_path)
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("parse_prd", {})

        with pytest.raises(ToolError, match="PRD file not found|prd.md"):
            _run(run())

    def test_error_when_named_prd_file_missing_uses_forward_slash_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_state_dir(tmp_path)
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("parse_prd", {"prd_id": "nope"})

        with pytest.raises(ToolError) as excinfo:
            _run(run())
        message = str(excinfo.value)
        assert "prds/nope.md" in message
        assert "prds\\nope.md" not in message

    def test_error_when_named_prd_file_unreadable_uses_forward_slash_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state_dir = _init_state_dir(tmp_path)
        blocked = state_dir / "prds" / "blocked.md"
        blocked.mkdir(parents=True)
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("parse_prd", {"prd_id": "blocked"})

        with pytest.raises(ToolError) as excinfo:
            _run(run())
        message = str(excinfo.value)
        assert "prds/blocked.md" in message
        assert "prds\\blocked.md" not in message
        assert "cannot read" in message.lower()

    def test_prd_id_scopes_to_named_partition(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """T019: parse_prd(prd_id='v0.2') reads .anvil/prds/v0.2.md and stamps
        the v0.2 partition so its rows land under id='v0.2', is_default=0 —
        leaving the default PRD partition untouched."""
        state_dir = _init_state_dir(tmp_path)
        # Default PRD already parsed.
        _write_prd_file(state_dir)
        # Named PRD source under the prds/ collection.
        prds_dir = state_dir / "prds"
        prds_dir.mkdir(exist_ok=True)
        (prds_dir / "v0.2.md").write_text(_MINIMAL_PRD, encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        async def run() -> tuple[Any, Any]:
            async with Client(mcp) as c:
                default_resp = _data(await c.call_tool("parse_prd", {}))
                named_resp = _data(
                    await c.call_tool("parse_prd", {"prd_id": "v0.2"})
                )
                return default_resp, named_resp

        default_resp, named_resp = _run(run())
        assert named_resp["errors"] == []
        assert named_resp["requirement_count"] == 2

        from anvil.clock import SystemClock
        from anvil.state.sqlite import SqliteBackend
        b = SqliteBackend(
            db_path=str(state_dir / "state.db"),
            events_path=str(state_dir / "events.jsonl"),
            clock=SystemClock(),
        )
        b.initialize()
        try:
            # The named PRD row exists and is NOT the default.
            named = b.get_prd("v0.2")
            assert named is not None
            assert named.id == "v0.2"
            assert named.is_default is False
            # The default PRD still resolves and stays the default.
            default = b.get_prd()
            assert default is not None
            assert default.id == "default"
            # Named PRD requirements landed in the v0.2 partition.
            v02_reqs = b.list_requirements(prd_id="v0.2")
            assert len(v02_reqs) == 2
        finally:
            b.close()

    def test_reparse_emits_revised_with_diff_and_supersedes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The MCP re-parse branch mirrors the CLI: the FIRST parse emits
        prd.parsed; a re-parse of the same PRD emits prd.revised carrying a diff
        (R001 superseded, R002 unchanged, R003 added) and the prior row is
        SUPERSEDED, not deleted."""
        state_dir = _init_state_dir(tmp_path)
        _write_prd_file(state_dir)
        monkeypatch.chdir(tmp_path)

        async def first() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("parse_prd", {}))

        _run(first())
        assert len(_events_with_action(state_dir, "prd.parsed")) == 1
        assert _events_with_action(state_dir, "prd.revised") == []

        _write_prd_file(state_dir, _MINIMAL_PRD_V2)

        async def second() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("parse_prd", {}))

        _run(second())

        revised = _events_with_action(state_dir, "prd.revised")
        assert len(revised) == 1
        # Re-parse must NOT re-create / wipe the PRD.
        assert len(_events_with_action(state_dir, "prd.parsed")) == 1
        payload = revised[0]
        assert payload["prd_id"] == "default"
        assert payload["revision"] == 2
        assert {r["id"] for r in payload["requirements_added"]} == {"R003"}
        assert {r["id"] for r in payload["requirements_superseded"]} == {"R001"}
        assert {r["id"] for r in payload["requirements_unchanged"]} == {"R002"}

        from anvil.clock import SystemClock
        from anvil.state.sqlite import SqliteBackend
        b = SqliteBackend(
            db_path=str(state_dir / "state.db"),
            events_path=str(state_dir / "events.jsonl"),
            clock=SystemClock(),
        )
        b.initialize()
        try:
            live = {r.id for r in b.list_requirements(prd_id="default")}
            full = {
                r.id: r.revision_superseded
                for r in b.list_requirements(
                    prd_id="default", include_superseded=True
                )
            }
        finally:
            b.close()
        assert live == {"R002", "R003"}
        assert full == {"R001": 2, "R002": None, "R003": None}, full

    def test_reparse_readding_retired_id_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Re-listing an id retired in a PRIOR revision must raise rather than
        silently drop the requirement. R001 is superseded in rev2, then a third
        parse re-adds it — that id is permanent lineage and cannot be revived."""
        state_dir = _init_state_dir(tmp_path)
        _write_prd_file(state_dir)
        monkeypatch.chdir(tmp_path)

        async def parse() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("parse_prd", {}))

        _run(parse())          # rev1: R001, R002
        _write_prd_file(state_dir, _MINIMAL_PRD_V2)
        _run(parse())          # rev2: R001 superseded, R002 kept, R003 added

        # Third parse re-adds the retired R001 → must be rejected, not dropped.
        _write_prd_file(state_dir, _MINIMAL_PRD_READD)

        async def readd() -> None:
            async with Client(mcp) as c:
                await c.call_tool("parse_prd", {})

        with pytest.raises(ToolError, match="R001.*superseded|superseded.*R001"):
            _run(readd())

        # No third revision was written — the rejected parse left the log alone.
        assert len(_events_with_action(state_dir, "prd.revised")) == 1

    def test_reparse_supersede_demotes_approved_prd_status_in_response(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A re-parse that supersedes a requirement demotes an APPROVED PRD back
        to draft; the ParsePrdResponse.prd_status must report the STORED status,
        not the parsed-markdown status (which would lie about the demotion)."""
        state_dir = _init_state_dir(tmp_path)
        _write_prd_file(state_dir)
        monkeypatch.chdir(tmp_path)

        async def setup_approved() -> None:
            async with Client(mcp) as c:
                await c.call_tool("parse_prd", {})
                await c.call_tool("review_prd", {})            # draft → reviewed
                await c.call_tool("review_prd", {"approve": True})  # → approved

        _run(setup_approved())

        # A material re-parse (R001 superseded) must demote approved → draft.
        _write_prd_file(state_dir, _MINIMAL_PRD_V2)

        async def reparse() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("parse_prd", {}))

        resp = _run(reparse())
        assert resp["prd_status"] == "draft", resp

        from anvil.clock import SystemClock
        from anvil.state.sqlite import SqliteBackend
        b = SqliteBackend(
            db_path=str(state_dir / "state.db"),
            events_path=str(state_dir / "events.jsonl"),
            clock=SystemClock(),
        )
        b.initialize()
        try:
            prd = b.get_prd("default")
        finally:
            b.close()
        assert prd is not None and prd.status.value == "draft"


# ===========================================================================
# Tool 17: review_prd
# ===========================================================================


class TestReviewPrd:
    def test_draft_to_reviewed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state_dir = _init_state_dir(tmp_path)
        _add_prd(state_dir, status="draft")
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("review_prd", {
                    "reviewer": "alice",
                    "notes": "Looks good.",
                }))

        resp = _run(run())
        assert resp["from_status"] == "draft"
        assert resp["to_status"] == "reviewed"
        assert resp["reviewer"] == "alice"

    def test_reviewed_to_approved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state_dir = _init_state_dir(tmp_path)
        _add_prd(state_dir, status="reviewed")
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("review_prd", {
                    "approve": True,
                    "reviewer": "bob",
                }))

        resp = _run(run())
        assert resp["from_status"] == "reviewed"
        assert resp["to_status"] == "approved"

    def test_error_when_wrong_status(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Approving while PRD is still draft → ToolError."""
        state_dir = _init_state_dir(tmp_path)
        _add_prd(state_dir, status="draft")
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("review_prd", {"approve": True})

        with pytest.raises(ToolError, match="reviewed|draft"):
            _run(run())

    def test_prd_id_reviews_only_named_partition(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """T019: review_prd(prd_id='v0.2') transitions ONLY the v0.2 PRD —
        the default PRD keeps its status, proving the per-PRD scoping."""
        state_dir = _init_state_dir(tmp_path)
        _add_prd(state_dir, status="draft")  # default PRD
        _add_prd(state_dir, status="draft", prd_id="v0.2", is_default=0)
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(
                    await c.call_tool(
                        "review_prd", {"prd_id": "v0.2", "reviewer": "alice"}
                    )
                )

        resp = _run(run())
        assert resp["from_status"] == "draft"
        assert resp["to_status"] == "reviewed"

        from anvil.clock import SystemClock
        from anvil.state.sqlite import SqliteBackend
        b = SqliteBackend(
            db_path=str(state_dir / "state.db"),
            events_path=str(state_dir / "events.jsonl"),
            clock=SystemClock(),
        )
        b.initialize()
        try:
            assert b.get_prd("v0.2").status.value == "reviewed"
            # Default PRD is untouched.
            assert b.get_prd("default").status.value == "draft"
        finally:
            b.close()


# ===========================================================================
# T021 — PRD ambiguity + $ANVIL_PRD parity on the MCP surface
#
# The MCP server must enforce the SAME resolution contract as the CLI: an
# ambiguous DB (>1 PRD, no default, no prd_id/$ANVIL_PRD) makes the
# single-PRD-resolving tool (review_prd) raise — translated to a ToolError —
# while the cross-PRD reads (get_project_status / get_next_task) stay clean.
# $ANVIL_PRD is honoured equally by the MCP server (AC2/AC3).
# ===========================================================================


class TestMcpPrdAmbiguityAndEnv:
    def _two_named_prds(self, tmp_path: Path) -> Path:
        """Two NON-default PRDs (v0.1, v0.2), no default — the ambiguous shape."""
        state_dir = _init_state_dir(tmp_path)
        _add_prd(state_dir, status="draft", prd_id="v0.1", is_default=0)
        _add_prd(state_dir, status="draft", prd_id="v0.2", is_default=0)
        return state_dir

    def test_review_prd_ambiguity_errors_listing_ids_and_knobs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """review_prd with no prd_id and no $ANVIL_PRD on an ambiguous DB raises
        a ToolError whose message lists the available ids and both knobs — the
        CLI ClickException message, translated by _resolve_prd_id."""
        monkeypatch.delenv("ANVIL_PRD", raising=False)
        self._two_named_prds(tmp_path)
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("review_prd", {"reviewer": "alice"})

        with pytest.raises(ToolError) as exc:
            _run(run())
        msg = str(exc.value)
        assert "v0.1" in msg and "v0.2" in msg
        assert "--prd" in msg
        assert "ANVIL_PRD" in msg

    def test_prd_ambiguity_cross_prd_reads_stay_clean(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """get_project_status / get_next_task aggregate across ALL PRDs and never
        resolve a single one, so they succeed on the same ambiguous DB.

        The asserts verify the ACTUAL cross-PRD aggregation (total_tasks == 2,
        ready_queue_depth == 2, and next picks a real task from EITHER PRD), not
        merely that the calls did not raise. That content is what proves these
        surfaces span all PRDs rather than routing through the single-PRD
        resolver; a bare "did not raise" would be non-discriminating since these
        tools structurally never reach the resolver on the no-prd_id path.
        """
        monkeypatch.delenv("ANVIL_PRD", raising=False)
        state_dir = self._two_named_prds(tmp_path)
        # Both PRDs need an approved status for a task to be claimable by next.
        _add_prd(state_dir, status="approved", prd_id="v0.1", is_default=0)
        _add_prd(state_dir, status="approved", prd_id="v0.2", is_default=0)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T100", status="ready", prd_id="v0.1")
        _add_task(state_dir, task_id="T900", status="ready", prd_id="v0.2")
        monkeypatch.chdir(tmp_path)

        async def run() -> tuple[Any, Any]:
            async with Client(mcp) as c:
                status = _data(await c.call_tool("get_project_status", {}))
                nxt = _data(await c.call_tool("get_next_task", {}))
                return status, nxt

        status, nxt = _run(run())
        assert status["initialized"] is True
        assert status["total_tasks"] == 2
        assert status["ready_queue_depth"] == 2
        # next picks SOME claimable task across both PRDs (no ambiguity raised).
        assert nxt is not None
        assert nxt["id"] in {"T100", "T900"}

    def test_prd_ambiguity_defused_by_explicit_prd_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An explicit prd_id defuses the ambiguity: review_prd(prd_id='v0.1')
        transitions ONLY v0.1, leaving v0.2 untouched."""
        monkeypatch.delenv("ANVIL_PRD", raising=False)
        state_dir = self._two_named_prds(tmp_path)
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(
                    await c.call_tool(
                        "review_prd", {"prd_id": "v0.1", "reviewer": "alice"}
                    )
                )

        resp = _run(run())
        assert resp["to_status"] == "reviewed"

        from anvil.clock import SystemClock
        from anvil.state.sqlite import SqliteBackend
        b = SqliteBackend(
            db_path=str(state_dir / "state.db"),
            events_path=str(state_dir / "events.jsonl"),
            clock=SystemClock(),
        )
        b.initialize()
        try:
            assert b.get_prd("v0.1").status.value == "reviewed"
            assert b.get_prd("v0.2").status.value == "draft"
        finally:
            b.close()

    def test_anvil_prd_env_resolves_review_like_explicit_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """$ANVIL_PRD is honoured by the MCP server exactly like an explicit
        prd_id: with $ANVIL_PRD=v0.2 and no prd_id, review_prd transitions ONLY
        v0.2 on the otherwise-ambiguous DB — matching the CLI's env behaviour."""
        state_dir = self._two_named_prds(tmp_path)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("ANVIL_PRD", "v0.2")

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("review_prd", {"reviewer": "bob"}))

        resp = _run(run())
        assert resp["to_status"] == "reviewed"

        from anvil.clock import SystemClock
        from anvil.state.sqlite import SqliteBackend
        b = SqliteBackend(
            db_path=str(state_dir / "state.db"),
            events_path=str(state_dir / "events.jsonl"),
            clock=SystemClock(),
        )
        b.initialize()
        try:
            assert b.get_prd("v0.2").status.value == "reviewed"
            assert b.get_prd("v0.1").status.value == "draft"
        finally:
            b.close()


# ===========================================================================
# Tool 18: plan_tasks
# ===========================================================================


class TestPlanTasks:
    def test_happy_path_emits_features_and_tasks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state_dir = _init_state_dir(tmp_path)
        _write_prd_file(state_dir)
        monkeypatch.chdir(tmp_path)

        async def run() -> tuple[Any, Any]:
            async with Client(mcp) as c:
                # parse_prd must run first so backend has a PRD row.
                await c.call_tool("parse_prd", {})
                plan = _data(await c.call_tool("plan_tasks", {}))
                tasks = _data(await c.call_tool("list_tasks", {}))
                return plan, tasks

        plan, tasks = _run(run())
        assert plan["feature_count"] == 1
        assert plan["task_count"] == 2
        # Tasks should be promoted to drafted after inference.
        statuses = {t["id"]: t["status"] for t in tasks}
        assert statuses.get("T001") == "drafted"
        assert statuses.get("T002") == "drafted"

    def test_error_when_no_prd_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_state_dir(tmp_path)
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("plan_tasks", {})

        with pytest.raises(ToolError, match="PRD file not found|prd.md"):
            _run(run())

    def test_error_when_named_prd_file_missing_uses_forward_slash_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_state_dir(tmp_path)
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("plan_tasks", {"prd_id": "nope"})

        with pytest.raises(ToolError) as excinfo:
            _run(run())
        message = str(excinfo.value)
        assert "prds/nope.md" in message
        assert "prds\\nope.md" not in message

    def test_error_when_named_prd_file_unreadable_uses_forward_slash_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state_dir = _init_state_dir(tmp_path)
        blocked = state_dir / "prds" / "blocked.md"
        blocked.mkdir(parents=True)
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("plan_tasks", {"prd_id": "blocked"})

        with pytest.raises(ToolError) as excinfo:
            _run(run())
        message = str(excinfo.value)
        assert "prds/blocked.md" in message
        assert "prds\\blocked.md" not in message
        assert "cannot read" in message.lower()

    def test_error_when_prd_file_present_but_not_parsed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """plan_tasks called with prd.md on disk but parse_prd never run.

        Regression for greptile PR #61 finding: previously plan_tasks would
        emit feature.created and task.created events into a backend with no
        PRD row, leaving review_prd and apply_review_decision to fail with
        'No PRD found in state' after the state was already mutated. Now
        plan_tasks must verify get_prd() is non-None first.
        """
        state_dir = _init_state_dir(tmp_path)
        _write_prd_file(state_dir)
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                # NOTE: deliberately skipping parse_prd to trigger the guard.
                await c.call_tool("plan_tasks", {})

        with pytest.raises(ToolError, match="No PRD found in state"):
            _run(run())

    def test_prd_id_plans_into_named_partition(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """T019: plan_tasks(prd_id='v0.2') reads .anvil/prds/v0.2.md, parses +
        plans into the v0.2 partition, and stamps prd_id on its task rows —
        without touching the default PRD's tasks."""
        state_dir = _init_state_dir(tmp_path)
        # Default PRD: parse + plan so it owns its own tasks.
        _write_prd_file(state_dir)
        # Named PRD source.
        prds_dir = state_dir / "prds"
        prds_dir.mkdir(exist_ok=True)
        (prds_dir / "v0.2.md").write_text(_MINIMAL_PRD, encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                await c.call_tool("parse_prd", {})
                await c.call_tool("plan_tasks", {})
                await c.call_tool("parse_prd", {"prd_id": "v0.2"})
                return _data(await c.call_tool("plan_tasks", {"prd_id": "v0.2"}))

        plan = _run(run())
        assert plan["feature_count"] == 1
        assert plan["task_count"] == 2

        from anvil.clock import SystemClock
        from anvil.state.sqlite import SqliteBackend
        b = SqliteBackend(
            db_path=str(state_dir / "state.db"),
            events_path=str(state_dir / "events.jsonl"),
            clock=SystemClock(),
        )
        b.initialize()
        try:
            # Named-PRD tasks landed in the v0.2 partition (ids are prefixed).
            v02_tasks = b.list_tasks(prd_id="v0.2")
            assert len(v02_tasks) == 2
            assert all(t.prd_id == "v0.2" for t in v02_tasks)
            assert {t.id for t in v02_tasks} == {"v0.2:T001", "v0.2:T002"}
            # Default-PRD tasks are unchanged and stay in the default partition.
            default_tasks = b.list_tasks(prd_id="default")
            assert {t.id for t in default_tasks} == {"T001", "T002"}
        finally:
            b.close()

    def test_plan_named_prd_with_no_default_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """plan_tasks(prd_id='v0.2') must succeed when ONLY a named PRD was ever
        parsed and NO default PRD row exists.

        Regression for the existence-guard: the guard probes the TARGET
        partition (result.prd.id), not the bare is_default=1 row. A bare
        get_prd() returns None here (no default), so the old default-only guard
        wrongly raised 'No PRD found' even though v0.2 is a real parsed
        partition. This forces the no-default-but-named-PRD path the default-only
        probe never reached.
        """
        state_dir = _init_state_dir(tmp_path)
        # ONLY a named PRD source — the default prd.md is deliberately absent,
        # and parse_prd is never called for the default, so no is_default row
        # exists in state.
        prds_dir = state_dir / "prds"
        prds_dir.mkdir(exist_ok=True)
        (prds_dir / "v0.2.md").write_text(_MINIMAL_PRD, encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                await c.call_tool("parse_prd", {"prd_id": "v0.2"})
                return _data(await c.call_tool("plan_tasks", {"prd_id": "v0.2"}))

        plan = _run(run())
        assert plan["feature_count"] == 1
        assert plan["task_count"] == 2

        from anvil.clock import SystemClock
        from anvil.state.sqlite import SqliteBackend
        b = SqliteBackend(
            db_path=str(state_dir / "state.db"),
            events_path=str(state_dir / "events.jsonl"),
            clock=SystemClock(),
        )
        b.initialize()
        try:
            # No default partition was ever created.
            assert b.get_prd("default") is None
            assert b.default_prd_id() is None
            # The named tasks landed in the v0.2 partition.
            v02_tasks = b.list_tasks(prd_id="v0.2")
            assert {t.id for t in v02_tasks} == {"v0.2:T001", "v0.2:T002"}
        finally:
            b.close()


# ===========================================================================
# Tool 18: plan_tasks — v1.15+ LLM task-generation backstop
# ===========================================================================


# PRD with features + requirements but NO `## Tasks` section. Triggers
# the LLM-backstop path in plan_tasks.
_PRD_WITHOUT_TASKS_MCP = """\
# Project: MCP LLM Backstop Test

## Summary

A project for exercising the LLM task-generation backstop via MCP.

## Goals

- Verify the backstop fires when tasks are absent.

## Requirements

- R001: Accept input.
- R002: Produce output.

## Features

### F001: Core Feature

The single feature exercised by the test PRD.

**Requirements:** R001, R002
"""


_CANNED_LLM_TASKS_MCP = """\
## Tasks

### T001: Implement input handler

**Feature:** F001
**Priority:** high
**Likely files:** src/app/handler.py

Parse the input correctly with validation.

**Acceptance criteria:**

- Valid input is parsed.
- Invalid input raises with the filename.

**Verification:**

- `pytest tests/test_handler.py -v`

### T002: Implement output writer

**Feature:** F001
**Priority:** medium
**Likely files:** src/app/writer.py

Write output to disk atomically.

**Acceptance criteria:**

- Output round-trips back to the input.

**Verification:**

- `pytest tests/test_writer.py -v`
"""


def _build_recorded_planner_provider(prd_content: str):  # type: ignore[no-untyped-def]
    """Construct a RecordedLLMProvider keyed to the planner's prompt for
    ``prd_content`` and a canned ``## Tasks`` response.

    Parses the PRD via ``parse_prd`` to recover the same model objects the
    production path passes to the planner, then builds the planner user
    prompt via the same helper and hashes it under the planner's tuning
    args (max_tokens=8000, temperature=0.0)."""
    from anvil.planning.llm import LLMResponse, RecordedLLMProvider
    from anvil.planning.llm_planner import (
        _SYSTEM_PROMPT,
        _build_user_prompt,
    )
    from anvil.planning.template import parse_prd

    parsed = parse_prd(prd_content, prd_id="prd")
    user_prompt = _build_user_prompt(
        parsed.prd, parsed.features, parsed.requirements, None
    )
    key = RecordedLLMProvider.record_key(
        _SYSTEM_PROMPT, user_prompt, max_tokens=8000, temperature=0.0
    )
    canned = LLMResponse(
        text=_CANNED_LLM_TASKS_MCP,
        input_tokens=100,
        cached_input_tokens=0,
        output_tokens=50,
        model="claude-opus-4-7",
        finish_reason="end_turn",
    )
    return RecordedLLMProvider({key: canned})


class TestPlanTasksLlmBackstop:
    """v1.15+ behaviour: when prd.md has features+requirements but no
    `## Tasks` section the MCP tool calls the LLM planner, appends to
    prd.md, re-parses, and reports llm_generated=True. Mirrors the CLI
    spec — keeps MCP and CLI behaviour in lock-step."""

    def _install_recorded_resolver(
        self,
        monkeypatch: pytest.MonkeyPatch,
        provider: Any,
    ) -> None:
        """Replace ``resolve_planner_provider`` so the tool uses a recorded
        provider without needing ANTHROPIC_API_KEY or a real API call."""
        from anvil.planning import llm_planner

        # v1.17.0 — resolve_planner_provider gained a `config` parameter.
        # The MCP tool passes the loaded config; the stub accepts and ignores.
        monkeypatch.setattr(
            llm_planner,
            "resolve_planner_provider",
            lambda config=None, *, model_override=None: (provider, "anthropic"),
        )

    def test_happy_path_generates_appends_and_reports_llm_flags(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PRD without `## Tasks` → plan_tasks calls LLM, mutates prd.md,
        emits task events, and returns llm_generated=True with
        llm_provider='anthropic'."""
        state_dir = _init_state_dir(tmp_path)
        _write_prd_file(state_dir, _PRD_WITHOUT_TASKS_MCP)
        monkeypatch.chdir(tmp_path)

        provider = _build_recorded_planner_provider(_PRD_WITHOUT_TASKS_MCP)
        self._install_recorded_resolver(monkeypatch, provider)

        async def run() -> tuple[Any, Any]:
            async with Client(mcp) as c:
                await c.call_tool("parse_prd", {})
                plan = _data(await c.call_tool("plan_tasks", {}))
                tasks = _data(await c.call_tool("list_tasks", {}))
                return plan, tasks

        plan, tasks = _run(run())
        assert plan["feature_count"] == 1
        assert plan["task_count"] == 2
        assert plan["llm_generated"] is True
        assert plan["llm_provider"] == "anthropic"

        # Tasks reached the backend with the canned IDs.
        task_ids = {t["id"] for t in tasks}
        assert {"T001", "T002"}.issubset(task_ids)

        # prd.md was mutated — `## Tasks` is now present on disk.
        prd_text = (state_dir / "prd.md").read_text(encoding="utf-8")
        assert "## Tasks" in prd_text
        assert "### T001" in prd_text and "### T002" in prd_text

    def test_use_llm_false_returns_zero_without_mutating_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With use_llm=False the tool MUST NOT call the LLM or touch
        prd.md. The response reports task_count=0 and llm_generated=False
        so the MCP caller can decide what to do next."""
        state_dir = _init_state_dir(tmp_path)
        _write_prd_file(state_dir, _PRD_WITHOUT_TASKS_MCP)
        monkeypatch.chdir(tmp_path)

        # Resolver should NOT fire when use_llm=False; install a raising
        # stub so an accidental call surfaces as a test failure.
        from anvil.planning import llm_planner

        def _explode(config=None, *, model_override=None) -> None:  # type: ignore[no-untyped-def]
            raise AssertionError(
                "resolve_planner_provider should not be called with use_llm=False"
            )

        monkeypatch.setattr(llm_planner, "resolve_planner_provider", _explode)

        prd_before = (state_dir / "prd.md").read_text(encoding="utf-8")

        async def run() -> Any:
            async with Client(mcp) as c:
                await c.call_tool("parse_prd", {})
                return _data(await c.call_tool("plan_tasks", {"use_llm": False}))

        plan = _run(run())
        assert plan["feature_count"] == 1
        assert plan["task_count"] == 0
        assert plan["llm_generated"] is False
        assert plan["llm_provider"] is None

        # File on disk is untouched.
        prd_after = (state_dir / "prd.md").read_text(encoding="utf-8")
        assert prd_before == prd_after

    def test_provider_unavailable_raises_tool_error_with_full_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When ``resolve_planner_provider`` raises
        ``PlannerProviderUnavailable`` the MCP tool must raise
        ``ToolError`` carrying the full multi-line setup message — never
        a silent ``task_count=0`` response."""
        state_dir = _init_state_dir(tmp_path)
        _write_prd_file(state_dir, _PRD_WITHOUT_TASKS_MCP)
        monkeypatch.chdir(tmp_path)

        from anvil.planning import llm_planner
        from anvil.planning.llm_planner import PlannerProviderUnavailable

        sentinel_msg = (
            "No LLM provider available for task generation. "
            "Set ANTHROPIC_API_KEY or install claude-agent-sdk."
        )

        def _raise(config=None, *, model_override=None) -> None:  # type: ignore[no-untyped-def]
            raise PlannerProviderUnavailable(sentinel_msg)

        monkeypatch.setattr(llm_planner, "resolve_planner_provider", _raise)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("parse_prd", {})
                await c.call_tool("plan_tasks", {})

        with pytest.raises(
            ToolError, match="ANTHROPIC_API_KEY|claude-agent-sdk"
        ):
            _run(run())

    def test_generate_llm_provider_error_raises_tool_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A generate()-time ``LLMProviderError`` from the default agent-sdk
        provider must surface as a clean ``ToolError``, not an unhandled
        exception. Regression for the agent-sdk default flip: the backstop
        used to only catch the resolve-time PlannerProviderUnavailable."""
        state_dir = _init_state_dir(tmp_path)
        _write_prd_file(state_dir, _PRD_WITHOUT_TASKS_MCP)
        monkeypatch.chdir(tmp_path)

        from anvil.planning import llm_planner
        from anvil.planning.llm import LLMProviderError

        class _FailingProvider:
            def generate(self, **kwargs):  # type: ignore[no-untyped-def]
                raise LLMProviderError(
                    "ClaudeAgentSDKProvider needs the `claude` CLI on PATH"
                )

        monkeypatch.setattr(
            llm_planner,
            "resolve_planner_provider",
            lambda config=None, *, model_override=None: (_FailingProvider(), "agent-sdk"),
        )

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("parse_prd", {})
                await c.call_tool("plan_tasks", {})

        with pytest.raises(ToolError, match="LLM call failed|claude"):
            _run(run())

    def test_named_prd_backstop_write_error_uses_forward_slash_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Generated-task write failures should not leak raw Windows paths."""
        state_dir = _init_state_dir(tmp_path)
        prds_dir = state_dir / "prds"
        prds_dir.mkdir()
        prd_path = prds_dir / "v0.2.md"
        prd_path.write_text(_PRD_WITHOUT_TASKS_MCP, encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        from anvil.planning.llm import LLMResponse

        class _Provider:
            def generate(self, **kwargs):  # type: ignore[no-untyped-def]
                return LLMResponse(
                    text=_CANNED_LLM_TASKS_MCP,
                    input_tokens=100,
                    cached_input_tokens=0,
                    output_tokens=50,
                    model="test-model",
                    finish_reason="end_turn",
                )

        self._install_recorded_resolver(monkeypatch, _Provider())

        async def parse() -> None:
            async with Client(mcp) as c:
                await c.call_tool("parse_prd", {"prd_id": "v0.2"})

        _run(parse())

        original_write_text = Path.write_text

        def _fail_named_prd_write(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            if self == prd_path:
                raise OSError(13, "simulated write failure", str(self))
            return original_write_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", _fail_named_prd_write)

        async def plan() -> None:
            async with Client(mcp) as c:
                await c.call_tool("plan_tasks", {"prd_id": "v0.2"})

        with pytest.raises(ToolError) as excinfo:
            _run(plan())
        message = str(excinfo.value)
        assert "cannot write generated tasks" in message.lower()
        assert "prds/v0.2.md" in message
        assert "prds\\v0.2.md" not in message


# ===========================================================================
# Tool 19: score_tasks
# ===========================================================================


class TestScoreTasks:
    def test_score_single_task_returns_full_score(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="drafted",
                  likely_files=["src/foo.py"])
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("score_tasks", {
                    "task_id": "T001",
                }))

        resp = _run(run())
        assert len(resp["scored"]) == 1
        entry = resp["scored"][0]
        assert entry["task_id"] == "T001"
        # All six dimensions populated (1-5 range).
        for dim in (
            "complexity", "parallelizability", "context_load",
            "blast_radius", "review_risk", "agent_suitability",
        ):
            assert 1 <= entry[dim] <= 5

    def test_error_on_unknown_task(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_state_dir(tmp_path)
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("score_tasks", {"task_id": "NOPE"})

        with pytest.raises(ToolError, match="not found|NOPE"):
            _run(run())

    def test_expansion_queue_enforces_recursive_depth_cap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        parent: str | None = None
        for task_id in ("root", "a", "b", "c", "d"):
            _add_task(
                state_dir,
                task_id=task_id,
                title=f"Task {task_id}",
                status="drafted",
                likely_files=[f"src/{task_id}_{idx}.py" for idx in range(5)],
                parent_task_id=parent,
            )
            parent = task_id
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("score_tasks", {}))

        resp = _run(run())
        queue_ids = [entry["task_id"] for entry in resp["expansion_queue"]]
        assert "d" not in queue_ids


# ===========================================================================
# Tool 20: review_tasks
# ===========================================================================


class TestReviewTasks:
    def test_promotes_drafted_to_ready_when_gates_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fully-formed drafted task should advance to ready."""
        state_dir = _init_state_dir(tmp_path)
        _write_prd_file(state_dir)
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                await c.call_tool("parse_prd", {})
                await c.call_tool("plan_tasks", {})
                return _data(await c.call_tool("review_tasks", {}))

        resp = _run(run())
        # Both T001 and T002 have AC + verification → both should advance.
        assert "T001" in resp["promoted_to_reviewed"]
        assert "T001" in resp["promoted_to_ready"]

    def test_blocked_task_appears_with_reason(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A drafted task with no acceptance criteria must block, not crash."""
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        # Drop a drafted task and manually clear its acceptance_criteria
        # so the gate fails.
        _add_task(state_dir, task_id="T001", status="drafted")
        # Wipe acceptance_criteria for T001 to trigger the gate.
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(str(state_dir / "state.db"))
        conn.execute(
            "UPDATE tasks SET acceptance_criteria = '[]' WHERE id = ?",
            ("T001",),
        )
        conn.commit()
        conn.close()
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("review_tasks", {}))

        resp = _run(run())
        blocked_ids = {b["task_id"] for b in resp["blocked"]}
        assert "T001" in blocked_ids
        assert "T001" not in resp["promoted_to_ready"]


# ===========================================================================
# Tool 21: apply_review_decision
# ===========================================================================


class TestApplyReviewDecision:
    def test_approve_transitions_to_done(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="needs_review")
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("apply_review_decision", {
                    "task_id": "T001",
                    "approve": True,
                    "reviewer": "alice",
                }))

        resp = _run(run())
        assert resp["task_id"] == "T001"
        assert resp["decision"] == "accepted"
        assert resp["from_status"] == "needs_review"
        # Backend auto-promotes accepted → done.
        assert resp["to_status"] in {"accepted", "done"}

    def test_reject_requires_reason(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="needs_review")
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("apply_review_decision", {
                    "task_id": "T001",
                    "approve": False,
                })

        with pytest.raises(ToolError, match="reason|Rejection"):
            _run(run())

    def test_error_when_task_not_in_needs_review(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="ready")
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("apply_review_decision", {
                    "task_id": "T001",
                    "approve": True,
                })

        with pytest.raises(ToolError, match="needs_review|expected"):
            _run(run())


# ===========================================================================
# Tool 21: apply_review_decision — strict completion-evidence enforcement
# (T025/B25 — closes the MCP-path gap: agents complete work via MCP, so the
# accept transition here must enforce required_evidence the same way the CLI
# `apply --approve --strict` does. Mirrors tests/test_strict_evidence.py.)
# ===========================================================================


class TestApplyReviewDecisionStrictEvidence:
    def _setup_needs_review(
        self,
        tmp_path: Path,
        *,
        required: list[str],
        with_screenshots: bool,
    ) -> Path:
        """init + task(needs_review) + required_evidence + one evidence row."""
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="needs_review")
        _set_required_evidence(state_dir, "T001", required)
        _add_evidence(
            state_dir,
            task_id="T001",
            commands_run=["pytest tests/ -v"],
            files_changed=["src/app/converter.py"],
            screenshots=["before.png", "after.png"] if with_screenshots else [],
        )
        return state_dir

    def test_strict_param_refuses_when_evidence_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """strict=True + missing required evidence → accept REFUSED.

        Task stays needs_review; error carries the stable code
        ``evidence_incomplete`` and names the missing item.
        """
        state_dir = self._setup_needs_review(
            tmp_path, required=["screenshots"], with_screenshots=False
        )
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("apply_review_decision", {
                    "task_id": "T001",
                    "approve": True,
                    "strict": True,
                })

        with pytest.raises(ToolError, match="evidence_incomplete.*screenshots"):
            _run(run())
        # Task NOT advanced — still awaiting review.
        assert _task_status(state_dir, "T001") == "needs_review"

    def test_config_strict_refuses_when_evidence_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No strict param — config strict_evidence: true drives the refusal."""
        state_dir = self._setup_needs_review(
            tmp_path, required=["screenshots"], with_screenshots=False
        )
        (state_dir / "config.yaml").write_text(
            "project_name: Test Project\n"
            "project_id: proj-test\n"
            "strict_evidence: true\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("apply_review_decision", {
                    "task_id": "T001",
                    "approve": True,
                })

        with pytest.raises(ToolError, match="evidence_incomplete"):
            _run(run())
        assert _task_status(state_dir, "T001") == "needs_review"

    def test_strict_allows_when_evidence_sufficient(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """strict=True + sufficient evidence → accepted/done (no-op gate)."""
        state_dir = self._setup_needs_review(
            tmp_path, required=["screenshots"], with_screenshots=True
        )
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("apply_review_decision", {
                    "task_id": "T001",
                    "approve": True,
                    "strict": True,
                }))

        resp = _run(run())
        assert resp["decision"] == "accepted"
        assert resp["to_status"] in {"accepted", "done"}
        assert _task_status(state_dir, "T001") in {"accepted", "done"}

    def test_default_advisory_approves_when_evidence_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Back-compat: no strict param, no config → accept proceeds to done
        even with missing required evidence (advisory preserved)."""
        state_dir = self._setup_needs_review(
            tmp_path, required=["screenshots"], with_screenshots=False
        )
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("apply_review_decision", {
                    "task_id": "T001",
                    "approve": True,
                }))

        resp = _run(run())
        assert resp["decision"] == "accepted"
        assert resp["to_status"] in {"accepted", "done"}
        assert _task_status(state_dir, "T001") in {"accepted", "done"}

    def test_strict_reject_is_never_gated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """strict=True + missing evidence + approve=False → reject succeeds."""
        self._setup_needs_review(
            tmp_path, required=["screenshots"], with_screenshots=False
        )
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("apply_review_decision", {
                    "task_id": "T001",
                    "approve": False,
                    "reason": "missing screenshots",
                    "strict": True,
                }))

        resp = _run(run())
        assert resp["decision"] == "rejected"

    def test_strict_no_required_evidence_is_noop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """strict=True but task declares no required_evidence → accept proceeds."""
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="needs_review")
        # No required_evidence injected; default verification is empty.
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("apply_review_decision", {
                    "task_id": "T001",
                    "approve": True,
                    "strict": True,
                }))

        resp = _run(run())
        assert resp["decision"] == "accepted"
        assert resp["to_status"] in {"accepted", "done"}


# ===========================================================================
# Tool 22: find_decisions (v1.14.0)
# ===========================================================================


_PRD_WITH_NEEDS_DECISION = """\
# Project: Decisions Test

## Summary

The system must serialize inputs [NEEDS DECISION: which format?].

## Goals

- Ship v1 [NEEDS DECISION].

## Requirements

- R001: System works.

## Open Questions

- none identified
"""


_PRD_WITH_OPEN_QUESTIONS = """\
# Project: Open Questions Test

## Summary

A clean PRD.

## Goals

- Ship.

## Requirements

- R001: System works.

## Open Questions

- What is the SLO target?
- Should we cache responses?
"""


class TestFindDecisions:
    def test_clean_prd_returns_total_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A PRD with no markers, no open questions, and well-formed tasks
        returns total=0 across all kinds."""
        state_dir = _init_state_dir(tmp_path)
        _write_prd_file(state_dir, _MINIMAL_PRD)
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                # parse_prd + plan_tasks so the backend has tasks (with AC
                # and verification commands from the _MINIMAL_PRD body).
                await c.call_tool("parse_prd", {})
                await c.call_tool("plan_tasks", {})
                return _data(await c.call_tool("find_decisions", {}))

        resp = _run(run())
        assert resp["total"] == 0
        assert resp["decisions"] == []
        assert resp["counts_by_kind"]["needs_decision"] == 0
        assert resp["counts_by_kind"]["open_question"] == 0
        assert resp["counts_by_kind"]["missing_field"] == 0

    def test_needs_decision_markers_are_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A PRD with two `[NEEDS DECISION]` markers returns two decisions
        of kind needs_decision with the right ids and shapes."""
        state_dir = _init_state_dir(tmp_path)
        _write_prd_file(state_dir, _PRD_WITH_NEEDS_DECISION)
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("find_decisions", {}))

        resp = _run(run())
        nd = [d for d in resp["decisions"] if d["kind"] == "needs_decision"]
        assert len(nd) == 2
        assert resp["counts_by_kind"]["needs_decision"] == 2
        # Sequential IDs starting at ND-001 (detector contract).
        assert {d["id"] for d in nd} == {"ND-001", "ND-002"}
        # Every entry has the required flat shape (Pydantic extra=forbid
        # would have failed the call already, but check populated fields).
        for entry in nd:
            assert entry["location"]
            assert entry["text"]
            assert entry["suggested_resolution_field"] == "inline rewrite"

    def test_open_questions_become_decisions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Items under `## Open Questions` (skipping placeholders) are
        surfaced as open_question decisions."""
        state_dir = _init_state_dir(tmp_path)
        _write_prd_file(state_dir, _PRD_WITH_OPEN_QUESTIONS)
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("find_decisions", {}))

        resp = _run(run())
        oq = [d for d in resp["decisions"] if d["kind"] == "open_question"]
        assert len(oq) == 2
        assert resp["counts_by_kind"]["open_question"] == 2
        assert {d["id"] for d in oq} == {"OQ001", "OQ002"}
        # Verify both texts surface.
        texts = " ".join(d["text"] for d in oq)
        assert "SLO" in texts
        assert "cache" in texts

    def test_missing_acceptance_criteria_reported_as_missing_field(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A task with empty acceptance_criteria yields an MF-*-AC decision."""
        state_dir = _init_state_dir(tmp_path)
        _write_prd_file(state_dir, _MINIMAL_PRD)
        _add_feature(state_dir, feat_id="F999", title="Broken Feature")
        # Insert a task whose acceptance_criteria are empty.
        _add_task(
            state_dir,
            task_id="T999",
            feature_id="F999",
            status="drafted",
        )
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(str(state_dir / "state.db"))
        conn.execute(
            "UPDATE tasks SET acceptance_criteria = '[]' WHERE id = ?",
            ("T999",),
        )
        conn.commit()
        conn.close()
        monkeypatch.chdir(tmp_path)

        async def run() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("find_decisions", {}))

        resp = _run(run())
        mf = [d for d in resp["decisions"] if d["kind"] == "missing_field"]
        # Default _add_task has empty verification commands as well, so we
        # expect at minimum the AC entry and (default) the V entry.
        mf_ids = {d["id"] for d in mf}
        assert "MF-T999-AC" in mf_ids

    def test_error_when_not_initialized(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No .anvil/ → ToolError mirroring the other workflow tools."""
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("find_decisions", {})

        with pytest.raises(ToolError, match="not initialized|init_project"):
            _run(run())

    def test_error_when_no_prd_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """anvil initialized but prd.md missing → ToolError (matches
        parse_prd behaviour; see find_decisions docstring for rationale)."""
        _init_state_dir(tmp_path)
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("find_decisions", {})

        with pytest.raises(ToolError, match="PRD file not found|prd.md"):
            _run(run())

    def test_error_when_prd_has_parse_failures(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression for greptile PR #62 finding: previously find_decisions
        silently proceeded when parse_prd surfaced errors, yielding a
        deceptive 0-open_questions count even though the PRD was malformed.
        Now it raises ToolError matching the CLI's exit-1 behaviour so the
        agent (or MCP client) surfaces the parse failure before drawing
        any conclusions from the decision list.
        """
        state_dir = _init_state_dir(tmp_path)
        # Write a PRD missing every required section — parse_prd will
        # surface 4+ errors (## Summary, ## Goals, ## Requirements, etc.).
        (state_dir / "prd.md").write_text(
            "# Project: Broken\n\nThis PRD has no required sections.\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("find_decisions", {})

        with pytest.raises(
            ToolError,
            match="PRD parse failed|parse_prd before find_decisions",
        ):
            _run(run())


# ===========================================================================
# Orphan-prune on re-parse (v1.15.0)
# ===========================================================================


_MCP_TWO_TASK_PRD = """\
# Project: MCP Orphan Test

## Summary

MCP-side orphan-prune coverage.

## Goals

- Test orphans via MCP.

## Requirements

- R001: First.

## Features

### F001: One feature

**Requirements:** R001

## Tasks

### T001: Keep me

**Feature:** F001
**Priority:** medium
**Likely files:** src/a.py

Stays.

**Acceptance criteria:**

- Stays.

**Verification:**

- `pytest a`

### T002: Delete me

**Feature:** F001
**Priority:** medium
**Likely files:** src/b.py

Removed on second parse.

**Acceptance criteria:**

- Was there.

**Verification:**

- `pytest b`
"""


_MCP_TWO_TASK_PRD_WITHOUT_T002 = """\
# Project: MCP Orphan Test

## Summary

MCP-side orphan-prune coverage.

## Goals

- Test orphans via MCP.

## Requirements

- R001: First.

## Features

### F001: One feature

**Requirements:** R001

## Tasks

### T001: Keep me

**Feature:** F001
**Priority:** medium
**Likely files:** src/a.py

Stays.

**Acceptance criteria:**

- Stays.

**Verification:**

- `pytest a`
"""


class TestPlanTasksOrphanPrune:
    """v1.15.0 behavior mirrored in MCP: plan_tasks emits task.deleted for
    entities removed from prd.md. prune_force=True overrides the safety
    check on unsafe statuses; without it, unsafe orphans raise ToolError."""

    def _setup_two_tasks(self, tmp_path: Path) -> Path:
        state_dir = _init_state_dir(tmp_path)
        (state_dir / "prd.md").write_text(_MCP_TWO_TASK_PRD, encoding="utf-8")
        return state_dir

    def _list_task_ids(self, tmp_path: Path) -> set[str]:
        import sqlite3
        db = tmp_path / ".anvil" / "state.db"
        with sqlite3.connect(str(db)) as conn:
            return {r[0] for r in conn.execute("SELECT id FROM tasks")}

    def _set_task_status(self, tmp_path: Path, task_id: str, status: str) -> None:
        import sqlite3
        db = tmp_path / ".anvil" / "state.db"
        with sqlite3.connect(str(db)) as conn:
            conn.execute(
                "UPDATE tasks SET status = ? WHERE id = ?", (status, task_id)
            )
            conn.commit()

    def test_safe_orphan_listed_in_pruned_task_ids(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Removing T002 from prd.md and re-running plan_tasks emits
        task.deleted; response.pruned_task_ids surfaces the change so MCP
        clients can show the user what was cleaned up."""
        state_dir = self._setup_two_tasks(tmp_path)
        monkeypatch.chdir(tmp_path)

        async def setup() -> None:
            async with Client(mcp) as c:
                await c.call_tool("parse_prd", {})
                await c.call_tool("plan_tasks", {})

        _run(setup())
        assert self._list_task_ids(tmp_path) == {"T001", "T002"}

        # Edit prd.md to remove T002 and re-plan.
        (state_dir / "prd.md").write_text(
            _MCP_TWO_TASK_PRD_WITHOUT_T002, encoding="utf-8"
        )

        async def re_plan() -> Any:
            async with Client(mcp) as c:
                await c.call_tool("parse_prd", {})
                return _data(await c.call_tool("plan_tasks", {}))

        resp = _run(re_plan())
        assert "T002" in resp["pruned_task_ids"], (
            f"pruned_task_ids should include T002; got {resp['pruned_task_ids']}"
        )
        assert self._list_task_ids(tmp_path) == {"T001"}

    def test_unsafe_orphan_raises_tool_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Orphan in unsafe status (claimed) blocks plan_tasks with a
        ToolError naming the task ID and prune_force option."""
        state_dir = self._setup_two_tasks(tmp_path)
        monkeypatch.chdir(tmp_path)

        async def setup() -> None:
            async with Client(mcp) as c:
                await c.call_tool("parse_prd", {})
                await c.call_tool("plan_tasks", {})

        _run(setup())
        self._set_task_status(tmp_path, "T002", "claimed")

        (state_dir / "prd.md").write_text(
            _MCP_TWO_TASK_PRD_WITHOUT_T002, encoding="utf-8"
        )

        async def re_plan() -> None:
            async with Client(mcp) as c:
                await c.call_tool("parse_prd", {})
                await c.call_tool("plan_tasks", {})

        with pytest.raises(ToolError, match="T002.*claimed|prune_force"):
            _run(re_plan())
        assert "T002" in self._list_task_ids(tmp_path)

    def test_prune_force_overrides_unsafe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """prune_force=True deletes orphans regardless of status."""
        state_dir = self._setup_two_tasks(tmp_path)
        monkeypatch.chdir(tmp_path)

        async def setup() -> None:
            async with Client(mcp) as c:
                await c.call_tool("parse_prd", {})
                await c.call_tool("plan_tasks", {})

        _run(setup())
        self._set_task_status(tmp_path, "T002", "claimed")

        (state_dir / "prd.md").write_text(
            _MCP_TWO_TASK_PRD_WITHOUT_T002, encoding="utf-8"
        )

        async def re_plan() -> Any:
            async with Client(mcp) as c:
                await c.call_tool("parse_prd", {})
                return _data(
                    await c.call_tool("plan_tasks", {"prune_force": True})
                )

        resp = _run(re_plan())
        assert "T002" in resp["pruned_task_ids"]
        assert self._list_task_ids(tmp_path) == {"T001"}


# ===========================================================================
# Audit-trail integrity: empty/whitespace actor guard (_require_actor)
# ===========================================================================


class TestRequireActor:
    """Each mutating tool that records an actor must reject empty or
    whitespace-only actor values before touching the backend.

    Covers: claim_task (claimed_by), release_task, renew_claim,
    submit_progress, submit_completion_evidence, update_task_status.
    """

    # -----------------------------------------------------------------------
    # claim_task — uses `claimed_by` as the actor field
    # -----------------------------------------------------------------------

    def test_claim_task_rejects_empty_claimed_by(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="ready")
        _add_prd(state_dir, status="reviewed")
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("claim_task", {
                    "task_id": "T001",
                    "claimed_by": "",
                })

        with pytest.raises(ToolError, match="actor|empty|whitespace"):
            _run(run())

    def test_claim_task_rejects_whitespace_claimed_by(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="ready")
        _add_prd(state_dir, status="reviewed")
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("claim_task", {
                    "task_id": "T001",
                    "claimed_by": "   ",
                })

        with pytest.raises(ToolError, match="actor|empty|whitespace"):
            _run(run())

    # -----------------------------------------------------------------------
    # release_task
    # -----------------------------------------------------------------------

    def test_release_task_rejects_empty_actor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="claimed")
        _add_active_claim(state_dir, claim_id="C001", task_id="T001", claimed_by="agent-x")
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("release_task", {
                    "task_id": "T001",
                    "actor": "",
                })

        with pytest.raises(ToolError, match="actor|empty|whitespace"):
            _run(run())

    def test_release_task_rejects_whitespace_actor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="claimed")
        _add_active_claim(state_dir, claim_id="C001", task_id="T001", claimed_by="agent-x")
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("release_task", {
                    "task_id": "T001",
                    "actor": "\t\n",
                })

        with pytest.raises(ToolError, match="actor|empty|whitespace"):
            _run(run())

    # -----------------------------------------------------------------------
    # renew_claim
    # -----------------------------------------------------------------------

    def test_renew_claim_rejects_empty_actor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="claimed")
        _add_active_claim(state_dir, claim_id="C001", task_id="T001", claimed_by="agent-x",
                          minutes_until_expiry=30)
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("renew_claim", {
                    "task_id": "T001",
                    "actor": "",
                })

        with pytest.raises(ToolError, match="actor|empty|whitespace"):
            _run(run())

    def test_renew_claim_rejects_whitespace_actor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="claimed")
        _add_active_claim(state_dir, claim_id="C001", task_id="T001", claimed_by="agent-x",
                          minutes_until_expiry=30)
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("renew_claim", {
                    "task_id": "T001",
                    "actor": "  ",
                })

        with pytest.raises(ToolError, match="actor|empty|whitespace"):
            _run(run())

    # -----------------------------------------------------------------------
    # submit_progress
    # -----------------------------------------------------------------------

    def test_submit_progress_rejects_empty_actor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="claimed")
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("submit_progress", {
                    "task_id": "T001",
                    "actor": "",
                    "notes": "in progress",
                })

        with pytest.raises(ToolError, match="actor|empty|whitespace"):
            _run(run())

    def test_submit_progress_rejects_whitespace_actor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="claimed")
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("submit_progress", {
                    "task_id": "T001",
                    "actor": " \t ",
                    "notes": "in progress",
                })

        with pytest.raises(ToolError, match="actor|empty|whitespace"):
            _run(run())

    # -----------------------------------------------------------------------
    # submit_completion_evidence
    # -----------------------------------------------------------------------

    def test_submit_completion_evidence_rejects_empty_actor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="in_progress")
        _add_active_claim(state_dir, claim_id="C001", task_id="T001", claimed_by="agent-x")
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("submit_completion_evidence", {
                    "task_id": "T001",
                    "actor": "",
                    "commands_run": ["pytest"],
                    "files_changed": ["src/foo.py"],
                })

        with pytest.raises(ToolError, match="actor|empty|whitespace"):
            _run(run())

    def test_submit_completion_evidence_rejects_whitespace_actor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="in_progress")
        _add_active_claim(state_dir, claim_id="C001", task_id="T001", claimed_by="agent-x")
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("submit_completion_evidence", {
                    "task_id": "T001",
                    "actor": "   ",
                    "commands_run": ["pytest"],
                    "files_changed": ["src/foo.py"],
                })

        with pytest.raises(ToolError, match="actor|empty|whitespace"):
            _run(run())

    # -----------------------------------------------------------------------
    # update_task_status
    # -----------------------------------------------------------------------

    def test_update_task_status_rejects_empty_actor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="drafted")
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("update_task_status", {
                    "task_id": "T001",
                    "to_status": "ready",
                    "actor": "",
                })

        with pytest.raises(ToolError, match="actor|empty|whitespace"):
            _run(run())

    def test_update_task_status_rejects_whitespace_actor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state_dir = _init_state_dir(tmp_path)
        _add_feature(state_dir)
        _add_task(state_dir, task_id="T001", status="drafted")
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("update_task_status", {
                    "task_id": "T001",
                    "to_status": "ready",
                    "actor": "\n",
                })

        with pytest.raises(ToolError, match="actor|empty|whitespace"):
            _run(run())
