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


def _add_prd(state_dir: Path, status: str = "reviewed") -> None:
    """Insert a PRD row directly via SQLite.

    status options: 'draft', 'reviewed', 'approved'
    """
    db_path = str(state_dir / "state.db")
    conn = sqlite3.connect(db_path)
    conn.execute("""
        INSERT OR REPLACE INTO prds
        (project_id, status, summary, goals, non_goals, requirements,
         acceptance_criteria, risks, open_questions)
        VALUES ('proj-test', ?, 'Test summary.', '[]', '[]', '[]', '[]', '[]', '[]')
    """, (status,))
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
        (id, feature_id, title, description, status, priority, task_type,
         dependencies, conflict_groups, scores, acceptance_criteria,
         implementation_notes, verification, likely_files,
         parent_task_id, created_at, updated_at)
        VALUES (?, ?, ?, 'A test task.', ?, ?, ?,
         ?, ?, ?, '["Tests pass."]', '[]', '{}', ?,
         ?, ?, ?)""",
        (
            task_id,
            feature_id,
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
# Test: list_tools — all 24 registered (full surface, planning gate off)
# ===========================================================================

# The 10 one-shot planning tools, tagged ``planning`` and hidden from the live
# wire surface by default (L2). The remaining 14 are the always-on execution
# surface.
_PLANNING_TOOLS = {
    "init_project", "parse_prd", "review_prd", "plan_tasks", "score_tasks",
    "review_tasks", "apply_review_decision", "edit_dependencies",
    "find_decisions", "describe_surface",
}
_EXECUTION_TOOLS = {
    "get_project_summary", "list_tasks", "get_task", "get_next_task",
    "claim_task", "release_task", "renew_claim", "generate_work_packet",
    "submit_progress", "submit_completion_evidence", "check_conflicts",
    "get_dependency_graph", "update_task_status", "get_project_status",
}
_ALL_TOOLS = _PLANNING_TOOLS | _EXECUTION_TOOLS


class TestListTools:
    def test_list_tools_returns_all_twenty_four(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
        assert len(_ALL_TOOLS) == 24


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
        assert len(names) == 14

    def test_env_flag_exposes_full_surface(self) -> None:
        from anvil.mcp_server import apply_surface_gate

        exposed = apply_surface_gate(mcp, env={"ANVIL_MCP_PLANNING": "1"})
        assert exposed is True
        names = self._wire_names()
        assert _ALL_TOOLS <= names
        assert len(names & _ALL_TOOLS) == 24

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
        assert len(self._wire_names()) == 14
        apply_surface_gate(mcp, env={"ANVIL_MCP_PLANNING": "1"})
        assert len(self._wire_names() & _ALL_TOOLS) == 24
        apply_surface_gate(mcp, env={})
        assert len(self._wire_names()) == 14

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

        # Enabled: it returns the full 24-tool manifest (introspection is
        # registry-based, so it reports the whole engine surface).
        apply_surface_gate(mcp, env={"ANVIL_MCP_PLANNING": "1"})

        async def call_enabled() -> Any:
            async with Client(mcp) as c:
                return _data(await c.call_tool("describe_surface", {}))

        manifest = _run(call_enabled())
        assert manifest["mcp"]["count"] == 24

    def test_registry_still_reports_all_24_when_gated(self) -> None:
        # describe/mcp_tool_names introspect the registry, NOT the wire surface,
        # so the documented "full engine surface" never shrinks under the gate.
        from anvil.cli.describe import mcp_tool_names
        from anvil.mcp_server import apply_surface_gate

        apply_surface_gate(mcp, env={})
        names = set(mcp_tool_names())
        assert _ALL_TOOLS <= names
        assert len(names) == 24


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

    def test_error_on_unknown_task_id(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _init_state_dir(tmp_path)
        monkeypatch.chdir(tmp_path)

        async def run() -> None:
            async with Client(mcp) as c:
                await c.call_tool("get_task", {"task_id": "nonexistent"})

        with pytest.raises(ToolError, match="not found|nonexistent"):
            _run(run())


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
        """Gate: PRD in 'draft' status → ToolError."""
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

        with pytest.raises(ToolError, match="draft|PRD"):
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

        with pytest.raises(ToolError, match="missing|draft|PRD"):
            _run(run())

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
            lambda config=None: (provider, "anthropic"),
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

        def _explode(config=None) -> None:  # type: ignore[no-untyped-def]
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

        def _raise(config=None) -> None:  # type: ignore[no-untyped-def]
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
