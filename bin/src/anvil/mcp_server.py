"""FastMCP (stdio) server — agent-facing tools for anvil.

Each tool opens a fresh SqliteBackend against the project's .anvil/state.db.
State resolves per call from the cwd arg (workflow tools), else ANVIL_ROOT,
else Path.cwd(); the no-cwd tools are pinned to the server's launch directory.

Stale-claim reaping runs at the top of every mutating tool and on
get_project_summary; read-only listers skip it for latency. No tool touches
git — branch/worktree creation stays in the CLI so remote agents without git
access can still drive the PRD → plan → review → claim → apply lifecycle.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# FastMCP instance
# ---------------------------------------------------------------------------

mcp: FastMCP = FastMCP("anvil")

# ---------------------------------------------------------------------------
# Planning vs execution surface split (audit item L2)
# ---------------------------------------------------------------------------
#
# The 24 tools fall into two groups:
#
#   EXECUTION (14) — the turn-to-turn loop an agent runs while doing work:
#       get_next_task, claim_task, release_task, renew_claim, submit_progress,
#       submit_completion_evidence, update_task_status, get_task,
#       get_project_status, get_project_summary, list_tasks, check_conflicts,
#       generate_work_packet, get_dependency_graph
#
#   PLANNING (10) — one-shot bootstrap/plan/review operations run rarely (often
#       once per project), tagged ``planning`` below:
#       init_project, parse_prd, review_prd, plan_tasks, score_tasks,
#       review_tasks, apply_review_decision, edit_dependencies, find_decisions,
#       describe_surface
#
# Every planning tool carries the ``planning`` tag. The live stdio server hides
# the planning surface BY DEFAULT (``apply_surface_gate`` at startup) so a steady-
# state execution client never pays the ~1.2k-token planning schema cost on every
# turn. Setting ``ANVIL_MCP_PLANNING`` (truthy) keeps all 24 tools on the wire —
# use it for the planning phase, or run a second server entry with the flag set.
#
# IMPORTANT: the gate is applied ONLY when the live server starts (see
# ``apply_surface_gate``), never at import time. So ``from anvil.mcp_server import
# mcp`` still sees all 24 registered tools, and every introspection surface that
# reports "what the engine can do" — ``describe_surface``, ``anvil describe``,
# ``mcp_tool_names()``, the ``--help`` tool list, the Docker catalog smoke test —
# is unchanged. Only the per-turn wire surface of the *default* execution server
# shrinks. No tool is removed; all 24 remain reachable.

PLANNING_TAG = "planning"

# Env flag that opts a live server back into the full 24-tool surface.
_PLANNING_ENV = "ANVIL_MCP_PLANNING"


def _planning_surface_enabled(env: dict[str, str] | None = None) -> bool:
    """Return True when the planning surface should be exposed on the wire.

    Resolves from the ``ANVIL_MCP_PLANNING`` env var. Truthy values
    (``1``/``true``/``yes``/``on``, case-insensitive) enable the full 24-tool
    surface; anything else (incl. unset) yields the lean execution-only default.
    """
    import os

    source = os.environ if env is None else env
    raw = source.get(_PLANNING_ENV, "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def apply_surface_gate(
    server: FastMCP = mcp, env: dict[str, str] | None = None
) -> bool:
    """Hide the planning tool surface on *server* unless the env flag opts in.

    Called once at live-server startup (``main``) and by the context audit so the
    measured/served surface matches. Returns True when the planning surface is
    exposed (no gate applied), False when it was hidden.

    Idempotent and reversible: re-enables the planning tags first, then disables
    them when the flag is off, so calling it twice (or after a prior enable)
    converges to the same state.
    """
    if _planning_surface_enabled(env):
        # Full surface: ensure planning tools are visible (covers a prior gate).
        server.enable(tags={PLANNING_TAG})
        return True
    server.disable(tags={PLANNING_TAG})
    return False

# ---------------------------------------------------------------------------
# Return-type Pydantic models (what each tool returns)
# ---------------------------------------------------------------------------


class TaskCountsByStatus(BaseModel):
    """Task counts broken down by status for the project summary."""

    model_config = ConfigDict(extra="forbid")

    proposed: int = 0
    drafted: int = 0
    reviewed: int = 0
    ready: int = 0
    claimed: int = 0
    in_progress: int = 0
    blocked: int = 0
    needs_review: int = 0
    accepted: int = 0
    done: int = 0
    rejected: int = 0


class ProjectSummary(BaseModel):
    """Summary of project state returned by get_project_summary."""

    model_config = ConfigDict(extra="forbid")

    project_id: str
    project_name: str
    project_description: str
    prd_status: str | None
    task_counts: TaskCountsByStatus
    active_claim_count: int
    blocked_task_count: int
    ready_task_count: int


class ClaimResponse(BaseModel):
    """Claim details returned by claim_task."""

    model_config = ConfigDict(extra="forbid")

    id: str
    task_id: str
    claimed_by: str
    lease_expires_at: str
    branch: str | None
    worktree_path: str | None
    expected_files: list[str]


class ReleaseResponse(BaseModel):
    """Result of release_task."""

    model_config = ConfigDict(extra="forbid")

    released: bool
    claim_id: str


class RenewResponse(BaseModel):
    """Result of renew_claim."""

    model_config = ConfigDict(extra="forbid")

    lease_expires_at: str


class WorkPacketResponse(BaseModel):
    """Result of generate_work_packet."""

    model_config = ConfigDict(extra="forbid")

    format: str
    content: Any  # str for markdown, dict for json


class ProgressResponse(BaseModel):
    """Result of submit_progress."""

    model_config = ConfigDict(extra="forbid")

    recorded: bool


class NextReadyTask(BaseModel):
    """Compact descriptor of the next claimable task, surfaced in finish/submit
    responses so the caller can chain into the next piece of work. ``null``
    when no task is claimable."""

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    priority: str


class EvidenceResponse(BaseModel):
    """Result of submit_completion_evidence."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    task_status: str
    # T014: name the next claimable task (deps/claims/conflict-group/file-overlap
    # aware) so the agent can chain work; null when none is available.
    next_ready: NextReadyTask | None = None


class ConflictEntry(BaseModel):
    """A single conflict entry from check_conflicts."""

    model_config = ConfigDict(extra="forbid")

    file: str
    claim_id: str
    claimed_by: str
    task_id: str


class ConflictCheckResponse(BaseModel):
    """Result of check_conflicts."""

    model_config = ConfigDict(extra="forbid")

    conflicts: list[ConflictEntry]


class DependencyNode(BaseModel):
    """A node in the dependency graph."""

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    status: str
    priority: str
    feature_id: str


class DependencyEdge(BaseModel):
    """A directed edge in the dependency graph (from → to)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_task: str = Field(alias="from")
    to_task: str = Field(alias="to")


class DependencyGraphResponse(BaseModel):
    """Result of get_dependency_graph."""

    model_config = ConfigDict(extra="forbid")

    nodes: list[DependencyNode]
    edges: list[DependencyEdge]
    ready_to_claim: list[str]


class StatusUpdateResponse(BaseModel):
    """Result of update_task_status."""

    model_config = ConfigDict(extra="forbid")

    from_status: str
    to_status: str


class EditDependenciesResponse(BaseModel):
    """Result of edit_dependencies.

    ``changed`` lists every task whose dependency set was actually mutated;
    ``added`` / ``removed`` are the ``[source, target]`` edges (source depends
    on target) that took effect — no-op edges are excluded from both.
    """

    model_config = ConfigDict(extra="forbid")

    changed: list[str]
    added: list[list[str]]
    removed: list[list[str]]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_STATE_DIR_NAME = ".anvil"

_PRIORITY_ORDER = {
    "critical": 4,
    "high": 3,
    "medium": 2,
    "low": 1,
}

# Allowed transitions for update_task_status per spec:
# "Limited to drafted↔ready and blocked toggle"
_ALLOWED_STATUS_TRANSITIONS: dict[str, set[str]] = {
    "drafted": {"ready"},
    "ready": {"drafted"},
    "in_progress": {"blocked"},
    "blocked": {"in_progress"},
    # spec also allows toggling blocked for claimed tasks
    "claimed": {"blocked"},
}


def _require_actor(actor: str) -> str:
    """Strip leading/trailing whitespace and raise ToolError when empty.

    An empty or whitespace-only actor would write a blank ``actor`` field into
    every audit event emitted by the tool, making the audit trail useless for
    attribution. Raise early so the caller gets a clear error rather than a
    silent blank entry in the event log.

    Returns the stripped actor string on success so callers can write::

        actor = _require_actor(actor)
    """
    stripped = actor.strip()
    if not stripped:
        raise ToolError(
            "actor must not be empty or whitespace — "
            "pass the agent or user identity for audit-trail attribution."
        )
    return stripped


def _resolve_state_dir(cwd: str | None = None) -> Path:
    """Return the absolute path to .anvil/ for the given cwd.

    Each MCP tool call resolves state relative to cwd at call time so agents
    can invoke from any project directory. The optional ``cwd`` argument lets
    workflow tools (init_project, parse_prd, etc.) point at a different
    project root without restarting the MCP server.

    Resolution precedence (T005/B07) — identical to the CLI
    (``cli/_helpers._resolve_state_dir``), so a host configures one project
    root and both surfaces agree:

        explicit ``cwd`` arg  >  ANVIL_ROOT env  >  Path.cwd()

    ``ANVIL_ROOT`` points at the project root (the dir containing
    ``.anvil/``) and is consulted only when no explicit ``cwd`` is
    given. If it is set but does not contain a ``.anvil/`` directory we
    raise ``ToolError`` — never a silent fall back to cwd, which would mask the
    misconfiguration for an MCP host that has no meaningful cwd.
    """
    # Delegate to the centralized CLI resolver so the env-override precedence
    # lives in exactly one place. Translate its ClickException into a ToolError
    # so MCP clients receive a structured error instead of a CLI-shaped one.
    from anvil.cli._helpers import StateRootError
    from anvil.cli._helpers import _resolve_state_dir as _cli_resolve

    try:
        return _cli_resolve(Path(cwd) if cwd else None)
    except StateRootError as exc:
        raise ToolError(exc.message) from exc


def _open_backend(state_dir: Path):  # type: ignore[return]
    """Open a fresh SqliteBackend for the given state_dir.

    Raises ToolError if the state directory does not exist (project not
    initialized). Caller must call backend.close() in a try/finally.
    """
    from anvil.clock import SystemClock
    from anvil.config import read_events_storage
    from anvil.state.sqlite import SqliteBackend

    if not state_dir.exists():
        raise ToolError(
            f"anvil not initialized in {state_dir.parent}. "
            "Run `anvil init` in your project root first.",
        )
    db_path = str(state_dir / "state.db")
    events_path = str(state_dir / "events.jsonl")
    backend = SqliteBackend(
        db_path=db_path,
        events_path=events_path,
        clock=SystemClock(),
        # v1.22.0: the storage mode decides the event-id format and the
        # replay strategy, so it must be resolved BEFORE the backend opens —
        # mirrors cli/_helpers._open_backend.
        events_storage=read_events_storage(state_dir / "config.yaml"),
    )
    backend.initialize()
    return backend


def _reap_stale(backend: Any) -> None:
    """Run the stale-claim detector; failures are best-effort (never block)."""
    try:
        from anvil.claims.stale import detect_and_release_stale
        from anvil.clock import SystemClock

        detect_and_release_stale(backend, SystemClock())
    except Exception:  # noqa: BLE001
        pass


def _find_active_claim_for_task(backend: Any, task_id: str) -> Any | None:
    """Return the active Claim for task_id, or None if none found."""
    for claim in backend.list_active_claims():
        if claim.task_id == task_id:
            return claim
    return None


def _compute_next_ready(backend: Any, actor: str | None = None) -> dict[str, Any] | None:
    """Return a thin descriptor of the next claimable task, or None.

    Shared by the finish/submit surfaces (T014) so the response can name the
    next ready task immediately after a task transitions out of the active set.
    Reuses ``ClaimManager.next_ready_excluding_active_files`` so the suggestion
    respects dependencies, active claims, conflict groups AND file-conflict
    exclusions (a task whose files overlap an active claim is never named).

    The descriptor is intentionally compact — {id, title, priority} — so the
    field stays cheap on a hot path and stable across CLI/MCP. Returns None
    when no task is claimable.
    """
    from anvil.claims.manager import ClaimManager
    from anvil.clock import SystemClock

    manager = ClaimManager(
        backend,
        SystemClock(),
        actor=actor or "agent",
    )
    task = manager.next_ready_excluding_active_files()
    if task is None:
        return None
    return {
        "id": task.id,
        "title": task.title,
        "priority": task.priority.value,
    }


def _resolve_strict_evidence(strict: bool | None, state_dir: Path) -> bool:
    """Resolve the effective strict-evidence mode for an MCP tool call.

    Mirrors ``cli/packet_apply._resolve_strict_evidence`` so the MCP accept
    path enforces the same completion-evidence gate as ``anvil apply``.
    The MCP path is the surface agents actually use (they complete work via
    MCP, not the CLI), so leaving it ungated lets an agent mark a task done
    with missing required evidence — exactly what strict mode exists to stop.

    Precedence (same as the CLI):

        explicit ``strict`` param (True/False)  >  config.strict_evidence  >  False

    Args:
        strict: Tri-state override. ``True``/``False`` are explicit; ``None``
            defers to the project config (then the default).
        state_dir: ``.anvil/`` directory whose ``config.yaml`` carries
            ``strict_evidence``.

    Returns:
        True if strict enforcement is in effect, else False (advisory default).

    Fail-closed on intent (should_fix): if ``config.yaml`` *exists* but fails
    to load, we do NOT silently treat strict as off — we emit a warning to
    stderr so a broken config that was meant to enable enforcement does not
    quietly disable it. (We still fall back to ``False`` to avoid hard-failing
    every accept on a malformed config, matching the soft-load contract used
    everywhere else; the warning is the signal.)
    """
    if strict is not None:
        return strict

    config_path = state_dir / "config.yaml"
    if not config_path.exists():
        return False

    import yaml

    try:
        from anvil.config import load_config

        return load_config(config_path).strict_evidence
    except (FileNotFoundError, OSError, ValueError, yaml.YAMLError) as exc:
        print(
            f"Warning: config.yaml load failed "
            f"({type(exc).__name__}: {exc}); strict-evidence enforcement could "
            "not be resolved from config and is treated as OFF for this call. "
            "Fix config.yaml to restore strict mode.",
            file=sys.stderr,
        )
        return False


def _load_fast_lane_config(state_dir: Path):  # type: ignore[no-untyped-def]
    """Soft-load the project config for T020 fast-lane packet routing.

    Returns a ``Config`` (carrying ``fast_lane_complexity_max`` /
    ``fast_lane_blast_radius_max``) or ``None`` when there is no config.yaml or
    it fails to parse — in which case ``generate_work_packet`` falls back to
    ``render_packet`` with the renderer's built-in default ceilings. A broken
    config never blocks packet generation.
    """
    config_path = state_dir / "config.yaml"
    if not config_path.exists():
        return None

    import yaml

    try:
        from anvil.config import load_config

        return load_config(config_path)
    except (FileNotFoundError, OSError, ValueError, yaml.YAMLError) as exc:
        print(
            f"Warning: config.yaml load failed "
            f"({type(exc).__name__}: {exc}); fast-lane packet thresholds could "
            "not be resolved from config; using built-in defaults for this call.",
            file=sys.stderr,
        )
        return None


# ---------------------------------------------------------------------------
# Tool 1: get_project_summary
# ---------------------------------------------------------------------------


@mcp.tool
def get_project_summary() -> ProjectSummary:
    """Summarize project state: info, task counts by status, active claims,
    blocked count, ready count. Reaps stale claims first."""
    state_dir = _resolve_state_dir()
    backend = _open_backend(state_dir)
    try:
        _reap_stale(backend)

        project = backend.get_project()
        if project is None:
            raise ToolError(
                "Project not found — run `anvil init` to initialize.",
            )

        prd = backend.get_prd()
        all_tasks = backend.list_tasks()
        active_claims = backend.list_active_claims()

        counts = TaskCountsByStatus()
        blocked_count = 0
        ready_count = 0
        for task in all_tasks:
            status_val = task.status.value
            if hasattr(counts, status_val):
                setattr(counts, status_val, getattr(counts, status_val) + 1)
            if status_val == "blocked":
                blocked_count += 1
            if status_val == "ready":
                ready_count += 1

        return ProjectSummary(
            project_id=project.id,
            project_name=project.name,
            project_description=project.description,
            prd_status=prd.status.value if prd is not None else None,
            task_counts=counts,
            active_claim_count=len(active_claims),
            blocked_task_count=blocked_count,
            ready_task_count=ready_count,
        )
    finally:
        backend.close()


# ---------------------------------------------------------------------------
# Tool 2: list_tasks
# ---------------------------------------------------------------------------


@mcp.tool
def list_tasks(
    status: str | None = None,
    feature_id: str | None = None,
    claimed_by: str | None = None,
    task_type: str | None = None,
    cwd: str | None = None,
) -> list[dict[str, Any]]:
    """List tasks, optionally filtered by status, feature_id, task_type
    (feature/bugfix/refactor/modify), and/or claimed_by actor.

    Args:
        claimed_by: Filter to tasks with an active claim held by this actor.
        cwd: Project root. Defaults to ``Path.cwd()``.
    """
    state_dir = _resolve_state_dir(cwd)
    backend = _open_backend(state_dir)
    try:
        tasks = backend.list_tasks(
            status=status, feature_id=feature_id, task_type=task_type
        )

        if claimed_by is not None:
            # Cross-reference active claims to filter by actor.
            active_claims = backend.list_active_claims()
            claimed_task_ids = {
                c.task_id for c in active_claims if c.claimed_by == claimed_by
            }
            tasks = [t for t in tasks if t.id in claimed_task_ids]

        return [json.loads(t.model_dump_json()) for t in tasks]
    finally:
        backend.close()


# ---------------------------------------------------------------------------
# Tool 3: get_task
# ---------------------------------------------------------------------------


@mcp.tool
def get_task(task_id: str) -> dict[str, Any]:
    """Return the full Task with the given ID (ToolError if not found)."""
    state_dir = _resolve_state_dir()
    backend = _open_backend(state_dir)
    try:
        task = backend.get_task(task_id)
        if task is None:
            raise ToolError(
                f"Task '{task_id}' not found.",
            )
        return json.loads(task.model_dump_json())
    finally:
        backend.close()


# ---------------------------------------------------------------------------
# Tool 4: get_next_task
# ---------------------------------------------------------------------------


@mcp.tool
def get_next_task(actor: str | None = None) -> dict[str, Any] | None:
    """Return the single highest-priority ready task that has no overlapping
    active claim, or null if none is claimable.

    Ordering: critical > high > medium > low; tiebreak agent_suitability desc,
    then id asc.
    """
    state_dir = _resolve_state_dir()
    backend = _open_backend(state_dir)
    try:
        # Read-only listers don't reap (per module docstring); MCP clients
        # call get_project_summary or a mutating tool to trigger reaping.

        # Single full-table fetch + in-memory partition; halves the SQLite
        # round-trips on this hot path versus calling list_tasks(status=...)
        # then list_tasks() again for the done/conflict sets.
        all_tasks = backend.list_tasks()
        if not all_tasks:
            return None
        ready_tasks = [t for t in all_tasks if t.status.value == "ready"]
        if not ready_tasks:
            return None

        active_claims = backend.list_active_claims()
        claimed_task_ids: set[str] = {c.task_id for c in active_claims}
        done_task_ids: set[str] = {
            t.id for t in all_tasks if t.status.value == "done"
        }

        # Build active conflict groups.
        active_conflict_groups: set[str] = set()
        for t in all_tasks:
            if t.id in claimed_task_ids:
                for cg_id in t.conflict_groups:
                    active_conflict_groups.add(cg_id)

        candidates = []
        for task in ready_tasks:
            if task.id in claimed_task_ids:
                continue
            if any(dep_id not in done_task_ids for dep_id in task.dependencies):
                continue
            if any(cg_id in active_conflict_groups for cg_id in task.conflict_groups):
                continue
            candidates.append(task)

        if not candidates:
            return None

        def _sort_key(t: Any) -> tuple[int, int, str]:
            # Priority: higher rank = higher priority = sort first (negate).
            priority_rank = _PRIORITY_ORDER.get(t.priority.value, 0)
            # agent_suitability: higher = better = sort first (negate).
            suitability = (
                t.scores.agent_suitability
                if t.scores.agent_suitability is not None
                else 0
            )
            return (-priority_rank, -suitability, t.id)

        candidates.sort(key=_sort_key)
        best = candidates[0]
        return json.loads(best.model_dump_json())
    finally:
        backend.close()


# ---------------------------------------------------------------------------
# Tool 5: claim_task
# ---------------------------------------------------------------------------


@mcp.tool
def claim_task(
    task_id: str,
    claimed_by: str,
    expected_files: list[str] | None = None,
    lease_duration_seconds: int = 900,
) -> ClaimResponse:
    """Acquire an exclusive lease on task_id for claimed_by.

    Reaps stale claims first; refuses (ToolError) while the PRD is in 'draft'.
    lease_duration_seconds defaults to 900 (15 min).
    """
    claimed_by = _require_actor(claimed_by)
    state_dir = _resolve_state_dir()
    backend = _open_backend(state_dir)
    try:
        from anvil.claims.manager import ClaimError, ClaimManager
        from anvil.clock import SystemClock

        _reap_stale(backend)

        # PRD gate: refuse if PRD is draft.
        prd = backend.get_prd()
        if prd is None or prd.status.value == "draft":
            prd_status = prd.status.value if prd is not None else "missing"
            raise ToolError(
                f"Cannot claim task '{task_id}': PRD is in '{prd_status}' status. "
                "The PRD must be reviewed or approved before tasks can be claimed.",
            )

        lease_minutes = max(1, lease_duration_seconds // 60)
        manager = ClaimManager(
            backend,
            SystemClock(),
            actor=claimed_by,
            default_lease_minutes=lease_minutes,
        )

        files = expected_files or []

        try:
            result = manager.claim(task_id, expected_files=files)
        except ClaimError as exc:
            raise ToolError(str(exc)) from exc

        claim = result.claim
        return ClaimResponse(
            id=claim.id,
            task_id=claim.task_id,
            claimed_by=claim.claimed_by,
            lease_expires_at=claim.lease_expires_at.isoformat(),
            branch=claim.branch,
            worktree_path=claim.worktree_path,
            expected_files=claim.expected_files,
        )
    finally:
        backend.close()


# ---------------------------------------------------------------------------
# Tool 6: release_task
# ---------------------------------------------------------------------------


@mcp.tool
def release_task(
    task_id: str,
    actor: str,
    reason: str | None = None,
) -> ReleaseResponse:
    """Release the active claim on task_id held by actor; returns the released
    claim_id. Reaps stale claims first."""
    actor = _require_actor(actor)
    state_dir = _resolve_state_dir()
    backend = _open_backend(state_dir)
    try:
        from anvil.claims.manager import ClaimError, ClaimManager
        from anvil.clock import SystemClock

        _reap_stale(backend)

        active_claim = _find_active_claim_for_task(backend, task_id)
        if active_claim is None:
            raise ToolError(
                f"No active claim found for task '{task_id}'. "
                "The task may already be released or was never claimed.",
            )

        manager = ClaimManager(
            backend,
            SystemClock(),
            actor=actor,
        )

        try:
            manager.release(active_claim.id, reason=reason)
        except ClaimError as exc:
            raise ToolError(str(exc)) from exc

        return ReleaseResponse(released=True, claim_id=active_claim.id)
    finally:
        backend.close()


# ---------------------------------------------------------------------------
# Tool 7: renew_claim
# ---------------------------------------------------------------------------


@mcp.tool
def renew_claim(
    task_id: str,
    actor: str,
    extend_seconds: int = 900,
) -> RenewResponse:
    """Extend the lease on the active claim for task_id by extend_seconds
    (default 900 = 15 min). Reaps stale claims first."""
    actor = _require_actor(actor)
    state_dir = _resolve_state_dir()
    backend = _open_backend(state_dir)
    try:
        from anvil.claims.manager import ClaimError, ClaimManager
        from anvil.clock import SystemClock

        _reap_stale(backend)

        active_claim = _find_active_claim_for_task(backend, task_id)
        if active_claim is None:
            raise ToolError(
                f"No active claim found for task '{task_id}'. "
                "The task may have been released or its lease may have expired.",
            )

        lease_minutes = max(1, extend_seconds // 60)
        manager = ClaimManager(
            backend,
            SystemClock(),
            actor=actor,
            default_lease_minutes=lease_minutes,
        )

        try:
            updated_claim = manager.renew(active_claim.id)
        except ClaimError as exc:
            raise ToolError(str(exc)) from exc

        return RenewResponse(
            lease_expires_at=updated_claim.lease_expires_at.isoformat()
        )
    finally:
        backend.close()


# ---------------------------------------------------------------------------
# Tool 8: generate_work_packet
# ---------------------------------------------------------------------------


@mcp.tool
def generate_work_packet(
    task_id: str,
    format: Literal["markdown", "json"] = "markdown",
) -> WorkPacketResponse:
    """Render the work packet for task_id (task brief, dependencies, prior
    findings) as markdown or JSON."""
    state_dir = _resolve_state_dir()
    backend = _open_backend(state_dir)
    try:
        from anvil.context.packets import fast_lane_packet, render_packet
        from anvil.state.models import Task

        task = backend.get_task(task_id)
        if task is None:
            raise ToolError(f"Task '{task_id}' not found.")

        feature = backend.get_feature(task.feature_id)

        dependencies_completed: list[Task] = []
        dependencies_open: list[Task] = []
        for dep_id in task.dependencies:
            dep = backend.get_task(dep_id)
            if dep is None:
                continue
            if dep.status.value == "done":
                dependencies_completed.append(dep)
            else:
                dependencies_open.append(dep)

        active_claim = _find_active_claim_for_task(backend, task_id)

        # T017 — surface prior deferred / failed-review findings whose files
        # overlap this task's files (the active claim's expected_files when
        # claimed, else the planner's likely_files hint).
        from anvil.review.gates import deferred_findings_for_files

        overlap_files = (
            active_claim.expected_files
            if active_claim is not None and active_claim.expected_files
            else task.likely_files
        )
        deferred = deferred_findings_for_files(
            backend.list_reviews(),
            backend.list_tasks(),
            backend.list_evidence(),
            overlap_files,
        )

        # T020 — route the fast-lane from the project's config thresholds when a
        # config can be loaded; fall back to the renderer's built-in defaults
        # otherwise. A broken config never blocks packet generation.
        cfg = _load_fast_lane_config(state_dir)
        if cfg is not None:
            packet = fast_lane_packet(
                task,
                cfg,
                feature=feature,
                dependencies_completed=dependencies_completed,
                dependencies_open=dependencies_open,
                related_decisions=None,
                active_claim=active_claim,
                deferred_findings=deferred,
            )
        else:
            packet = render_packet(
                task,
                feature=feature,
                dependencies_completed=dependencies_completed,
                dependencies_open=dependencies_open,
                related_decisions=None,
                active_claim=active_claim,
                deferred_findings=deferred,
            )

        if format == "json":
            return WorkPacketResponse(format="json", content=packet.json_data)
        return WorkPacketResponse(format="markdown", content=packet.markdown)
    finally:
        backend.close()


# ---------------------------------------------------------------------------
# Tool 9: submit_progress
# ---------------------------------------------------------------------------


@mcp.tool
def submit_progress(
    task_id: str,
    actor: str,
    notes: str,
) -> ProgressResponse:
    """Record a progress note for task_id as a 'progress.noted' audit event.
    Does NOT change task status. Reaps stale claims first."""
    actor = _require_actor(actor)
    state_dir = _resolve_state_dir()
    backend = _open_backend(state_dir)
    try:
        from anvil.clock import SystemClock
        from anvil.state.models import EventDraft

        _reap_stale(backend)

        task = backend.get_task(task_id)
        if task is None:
            raise ToolError(f"Task '{task_id}' not found.")

        clock = SystemClock()
        now = clock.now()

        draft = EventDraft(
            timestamp=now,
            actor=actor,
            action="progress.noted",
            target_kind="task",
            target_id=task_id,
            payload_json={
                "task_id": task_id,
                "actor": actor,
                "notes": notes,
                "noted_at": now.isoformat(),
            },
        )
        backend.append(draft)
        return ProgressResponse(recorded=True)
    finally:
        backend.close()


# ---------------------------------------------------------------------------
# Tool 10: submit_completion_evidence
# ---------------------------------------------------------------------------


@mcp.tool
def submit_completion_evidence(
    task_id: str,
    actor: str,
    commands_run: list[str],
    files_changed: list[str],
    output_excerpt: str | None = None,
    pr_url: str | None = None,
    commit_sha: str | None = None,
) -> EvidenceResponse:
    """Submit completion evidence for task_id (requires an active claim held by
    actor). Auto-releases the claim and moves the task to needs_review; names
    the next claimable task. Reaps stale claims first."""
    actor = _require_actor(actor)
    state_dir = _resolve_state_dir()
    backend = _open_backend(state_dir)
    try:
        from anvil.clock import SystemClock
        from anvil.state.backend import EventRejected
        from anvil.state.models import EventDraft

        _reap_stale(backend)

        task = backend.get_task(task_id)
        if task is None:
            raise ToolError(f"Task '{task_id}' not found.")

        active_claim = _find_active_claim_for_task(backend, task_id)
        if active_claim is None:
            raise ToolError(
                f"No active claim found for task '{task_id}'. "
                "Claim the task first before submitting evidence.",
            )

        # Enforce actor ownership — only the claim owner may submit evidence.
        # Without this guard any MCP caller can force-complete another agent's
        # claim by passing a different actor name (caught by critic-PR#45-P1).
        if active_claim.claimed_by != actor:
            raise ToolError(
                f"Task '{task_id}' is claimed by '{active_claim.claimed_by}', "
                f"not '{actor}'. Only the claim owner may submit completion evidence.",
            )

        evidence_id = "EV" + uuid.uuid4().hex[:8].upper()
        clock = SystemClock()
        now = clock.now()

        draft = EventDraft(
            timestamp=now,
            actor=actor,
            action="evidence.submitted",
            target_kind="task",
            target_id=task_id,
            payload_json={
                "task_id": task_id,
                "claim_id": active_claim.id,
                "submitted_by": actor,
                "evidence_id": evidence_id,
                "commands_run": commands_run,
                "files_changed": files_changed,
                "output_excerpt": output_excerpt,
                "pr_url": pr_url,
                "commit_sha": commit_sha,
                "screenshots": [],
                "known_limitations": None,
            },
        )

        try:
            backend.append(draft)
        except EventRejected as exc:
            raise ToolError(str(exc)) from exc

        fresh_task = backend.get_task(task_id)
        task_status = fresh_task.status.value if fresh_task is not None else "needs_review"

        # T014: name the next claimable task now that this one has left the
        # active set. The submitting actor's own (now-released) claim is
        # excluded from file-conflict checks, so a follow-on task touching the
        # same files this agent just finished is still eligible.
        next_ready_raw = _compute_next_ready(backend, actor)
        next_ready = (
            NextReadyTask(**next_ready_raw) if next_ready_raw is not None else None
        )

        return EvidenceResponse(
            evidence_id=evidence_id,
            task_status=task_status,
            next_ready=next_ready,
        )
    finally:
        backend.close()


# ---------------------------------------------------------------------------
# Tool 11: check_conflicts
# ---------------------------------------------------------------------------


@mcp.tool
def check_conflicts(
    task_id: str,
    proposed_files: list[str],
) -> ConflictCheckResponse:
    """Check proposed_files against active claims (excluding task_id's own),
    returning one conflict entry per overlapping file per claim. Empty list
    means no conflicts."""
    state_dir = _resolve_state_dir()
    backend = _open_backend(state_dir)
    try:
        proposed_set = set(proposed_files)
        active_claims = backend.list_active_claims()

        conflicts: list[ConflictEntry] = []
        for claim in active_claims:
            # Skip this task's own claim.
            if claim.task_id == task_id:
                continue
            overlap = proposed_set & set(claim.expected_files)
            for file in sorted(overlap):
                conflicts.append(
                    ConflictEntry(
                        file=file,
                        claim_id=claim.id,
                        claimed_by=claim.claimed_by,
                        task_id=claim.task_id,
                    )
                )

        return ConflictCheckResponse(conflicts=conflicts)
    finally:
        backend.close()


# ---------------------------------------------------------------------------
# Tool 12: get_dependency_graph
# ---------------------------------------------------------------------------


@mcp.tool
def get_dependency_graph(
    scope: Literal["all", "feature", "task"] = "all",
    target_id: str | None = None,
) -> DependencyGraphResponse:
    """Return the task dependency graph (nodes, edges, ready_to_claim).

    scope='all' is the whole project; 'feature' is one feature's tasks; 'task'
    is the target plus its transitive deps (target_id required for the latter
    two). ready_to_claim = ready tasks with all deps done and no active claim.
    """
    state_dir = _resolve_state_dir()
    backend = _open_backend(state_dir)
    try:
        all_tasks = backend.list_tasks()
        task_map = {t.id: t for t in all_tasks}
        active_claims = backend.list_active_claims()
        claimed_task_ids = {c.task_id for c in active_claims}
        done_task_ids = {t.id for t in all_tasks if t.status.value == "done"}

        # Determine which tasks are in scope.
        if scope == "all":
            scoped_tasks = all_tasks
        elif scope == "feature":
            if target_id is None:
                raise ToolError(
                    "target_id is required when scope='feature'."
                )
            scoped_tasks = [t for t in all_tasks if t.feature_id == target_id]
        elif scope == "task":
            if target_id is None:
                raise ToolError(
                    "target_id is required when scope='task'."
                )
            # Collect the target task plus all its transitive dependencies.
            visited: set[str] = set()
            queue = [target_id]
            while queue:
                tid = queue.pop()
                if tid in visited:
                    continue
                visited.add(tid)
                t = task_map.get(tid)
                if t is None:
                    continue
                for dep_id in t.dependencies:
                    if dep_id not in visited:
                        queue.append(dep_id)
            scoped_tasks = [task_map[tid] for tid in visited if tid in task_map]
        else:
            scoped_tasks = all_tasks

        scoped_ids = {t.id for t in scoped_tasks}

        nodes = [
            DependencyNode(
                id=t.id,
                title=t.title,
                status=t.status.value,
                priority=t.priority.value,
                feature_id=t.feature_id,
            )
            for t in scoped_tasks
        ]

        # Edges: dependency relationships within scope.
        edges = []
        for t in scoped_tasks:
            for dep_id in t.dependencies:
                if dep_id in scoped_ids:
                    edges.append(
                        DependencyEdge(
                            **{"from": dep_id, "to": t.id}
                        )
                    )

        # ready_to_claim: ready tasks with all deps done and no active claim.
        ready_to_claim = []
        for t in scoped_tasks:
            if t.status.value != "ready":
                continue
            if t.id in claimed_task_ids:
                continue
            if any(dep_id not in done_task_ids for dep_id in t.dependencies):
                continue
            ready_to_claim.append(t.id)

        return DependencyGraphResponse(
            nodes=nodes,
            edges=edges,
            ready_to_claim=sorted(ready_to_claim),
        )
    finally:
        backend.close()


# ---------------------------------------------------------------------------
# Tool 12b: edit_dependencies — batch dependency-edit primitive (T022/F007)
# ---------------------------------------------------------------------------


@mcp.tool(tags={PLANNING_TAG})
def edit_dependencies(
    actor: str,
    add: list[list[str]] | None = None,
    remove: list[list[str]] | None = None,
) -> EditDependenciesResponse:
    """Apply a batch of dependency edits atomically, rejecting cycles.

    ``add`` / ``remove`` are ``[source, target]`` pairs meaning *source depends
    on target*. The whole batch is validated up front: any unknown task,
    self-dependency, or cycle rejects the ENTIRE batch (ToolError) with no
    partial apply. Task status is preserved.
    """
    from anvil.clock import SystemClock
    from anvil.planning._plan_helpers import (
        BatchDepError,
        DepEdge,
        emit_batch_dep_events,
        plan_batch_dep_edits,
    )

    add = add or []
    remove = remove or []
    if not add and not remove:
        raise ToolError(
            "no edges supplied; pass at least one add or remove pair."
        )

    def _to_edges(pairs: list[list[str]], op: str) -> list[DepEdge]:
        out: list[DepEdge] = []
        for pair in pairs:
            if len(pair) != 2:
                raise ToolError(
                    f"invalid {op} edge {pair!r}: expected a [source, target] pair."
                )
            out.append(DepEdge(op=op, source=pair[0], target=pair[1]))
        return out

    edges = _to_edges(add, "add") + _to_edges(remove, "remove")

    state_dir = _resolve_state_dir()
    backend = _open_backend(state_dir)
    try:
        clock = SystemClock()
        all_tasks = backend.list_tasks()
        tasks_by_id = {t.id: t for t in all_tasks}

        # Validate the WHOLE batch before emitting anything — a raised
        # BatchDepError here means zero events were appended (no partial apply).
        try:
            batch_plan = plan_batch_dep_edits(all_tasks, edges)
        except BatchDepError as exc:
            raise ToolError(exc.message) from exc

        changed = emit_batch_dep_events(
            backend, tasks_by_id, batch_plan, actor=actor, clock=clock
        )
        return EditDependenciesResponse(
            changed=changed,
            added=[list(e) for e in batch_plan.added],
            removed=[list(e) for e in batch_plan.removed],
        )
    finally:
        backend.close()


# ---------------------------------------------------------------------------
# Tool 13: update_task_status
# ---------------------------------------------------------------------------


@mcp.tool
def update_task_status(
    task_id: str,
    to_status: Literal["drafted", "ready", "blocked", "in_progress"],
    actor: str,
    reason: str | None = None,
) -> StatusUpdateResponse:
    """Transition task_id to a new status. Only these moves are allowed
    (any other raises ToolError): drafted↔ready, in_progress/claimed→blocked,
    blocked→in_progress. Reaps stale claims first."""
    actor = _require_actor(actor)
    state_dir = _resolve_state_dir()
    backend = _open_backend(state_dir)
    try:
        from anvil.clock import SystemClock
        from anvil.state.backend import EventRejected
        from anvil.state.models import EventDraft

        _reap_stale(backend)

        task = backend.get_task(task_id)
        if task is None:
            raise ToolError(f"Task '{task_id}' not found.")

        from_status = task.status.value
        allowed_targets = _ALLOWED_STATUS_TRANSITIONS.get(from_status, set())

        if to_status not in allowed_targets:
            raise ToolError(
                f"Cannot transition task '{task_id}' from '{from_status}' to '{to_status}'. "
                f"Allowed targets from '{from_status}': {sorted(allowed_targets) or 'none'}. "
                "This tool supports only: drafted↔ready and blocked toggle.",
            )

        clock = SystemClock()
        now = clock.now()

        draft = EventDraft(
            timestamp=now,
            actor=actor,
            action="task.status_changed",
            target_kind="task",
            target_id=task_id,
            payload_json={
                "task_id": task_id,
                "from": from_status,
                "to": to_status,
                "reason": reason,
            },
        )

        try:
            backend.append(draft)
        except EventRejected as exc:
            raise ToolError(str(exc)) from exc

        return StatusUpdateResponse(from_status=from_status, to_status=to_status)
    finally:
        backend.close()


# ===========================================================================
# Workflow tools (init / PRD / plan / review / apply)
# ===========================================================================
#
# These complete the PRD → plan → review → approve → claim → apply lifecycle
# for non-Claude-Code MCP clients. Each mirrors the corresponding CLI handler
# via shared modules (no logic duplication), touches no git, and accepts an
# optional ``cwd`` to target a project root other than the server's launch dir.

_PRD_FILENAME = "prd.md"


# ---------------------------------------------------------------------------
# Tool 14: init_project
# ---------------------------------------------------------------------------


class InitProjectResponse(BaseModel):
    """Result of init_project."""

    model_config = ConfigDict(extra="forbid")

    project_id: str
    project_name: str
    state_dir: str
    created: bool


@mcp.tool(tags={PLANNING_TAG})
def init_project(
    name: str | None = None,
    cwd: str | None = None,
) -> InitProjectResponse:
    """Scaffold a fresh .anvil/ state directory in the target project root.

    Creates the canonical layout (config.yaml, state.db, events.jsonl,
    packets/), seeds the project row, and emits project.created +
    state.initialized. Non-destructive: raises ToolError if .anvil/ already
    exists (use ``anvil init --force`` from the CLI to reinit) or inside the
    plugin root.

    Args:
        name: Project name. Defaults to the cwd basename.
        cwd:  Project root. Defaults to Path.cwd().
    """
    from anvil.cli._helpers import _is_plugin_root, _resolve_base_dir, _slug
    from anvil.clock import SystemClock
    from anvil.config import write_default_config
    from anvil.state.models import EventDraft
    from anvil.state.sqlite import SqliteBackend

    # MUST-FIX 1: resolve the project root the SAME way reads do
    # (explicit cwd > ANVIL_ROOT > Path.cwd()), so init_project and
    # every read tool (get_project_status, etc.) agree on the project dir.
    base = _resolve_base_dir(Path(cwd) if cwd else None)

    if _is_plugin_root(base):
        raise ToolError(
            f"Refusing to initialize anvil in {base}: this is the "
            "plugin root, not a project directory. Pass cwd= a project path.",
        )

    state_dir = base / _STATE_DIR_NAME
    if state_dir.exists():
        raise ToolError(
            f"{state_dir} already exists. Use the `anvil init --force` "
            "CLI command to reinitialize (MCP init_project is non-destructive).",
        )

    project_name = name if name else base.name
    project_id = _slug(project_name)

    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "packets").mkdir(exist_ok=True)
        (state_dir / "events.jsonl").touch()
        write_default_config(state_dir / "config.yaml", project_name=project_name)
    except (OSError, FileExistsError) as exc:
        raise ToolError(f"Failed to scaffold {state_dir}: {exc}") from exc

    backend = SqliteBackend(
        db_path=str(state_dir / "state.db"),
        events_path=str(state_dir / "events.jsonl"),
        clock=SystemClock(),
    )
    try:
        # initialize() must be inside try so a failure during schema
        # bootstrap still triggers backend.close() in the finally block.
        backend.initialize()
        now = SystemClock().now()
        backend.append(EventDraft(
            timestamp=now,
            actor="anvil-mcp",
            action="project.created",
            target_kind="project",
            target_id=project_id,
            payload_json={
                "id": project_id,
                "name": project_name,
                "description": "",
                "created_at": now.isoformat(),
                "updated_at": now.isoformat(),
            },
        ))
        backend.append(EventDraft(
            timestamp=now,
            actor="anvil-mcp",
            action="state.initialized",
            target_kind="project",
            target_id=project_id,
            payload_json={},
        ))
    finally:
        backend.close()

    return InitProjectResponse(
        project_id=project_id,
        project_name=project_name,
        state_dir=str(state_dir),
        created=True,
    )


# ---------------------------------------------------------------------------
# Tool 15: get_project_status
# ---------------------------------------------------------------------------


class ProjectStatusResponse(BaseModel):
    """Result of get_project_status — a structured equivalent of
    ``anvil status``."""

    model_config = ConfigDict(extra="forbid")

    initialized: bool
    project_id: str | None
    project_name: str | None
    state_dir: str
    prd_status: str | None
    task_counts: TaskCountsByStatus
    total_tasks: int
    ready_queue_depth: int
    active_claim_count: int


@mcp.tool
def get_project_status(cwd: str | None = None) -> ProjectStatusResponse:
    """Return PRD status, task counts by state, active-claim count, and ready-
    queue depth. The canonical "am I bootstrapped?" probe: returns
    initialized=False with empty counts (no exception) when .anvil/ is absent.

    Args:
        cwd: Project root. Defaults to Path.cwd().
    """
    state_dir = _resolve_state_dir(cwd)
    empty_counts = TaskCountsByStatus()

    if not state_dir.exists():
        return ProjectStatusResponse(
            initialized=False,
            project_id=None,
            project_name=None,
            state_dir=str(state_dir),
            prd_status=None,
            task_counts=empty_counts,
            total_tasks=0,
            ready_queue_depth=0,
            active_claim_count=0,
        )

    backend = _open_backend(state_dir)
    try:
        project = backend.get_project()
        prd = backend.get_prd()
        all_tasks = backend.list_tasks()
        active_claims = backend.list_active_claims()

        counts = TaskCountsByStatus()
        ready_depth = 0
        for task in all_tasks:
            status_val = task.status.value
            if hasattr(counts, status_val):
                setattr(counts, status_val, getattr(counts, status_val) + 1)
            if status_val == "ready":
                ready_depth += 1

        return ProjectStatusResponse(
            initialized=True,
            project_id=project.id if project is not None else None,
            project_name=project.name if project is not None else None,
            state_dir=str(state_dir),
            prd_status=prd.status.value if prd is not None else None,
            task_counts=counts,
            total_tasks=len(all_tasks),
            ready_queue_depth=ready_depth,
            active_claim_count=len(active_claims),
        )
    finally:
        backend.close()


# ---------------------------------------------------------------------------
# Tool 16: parse_prd
# ---------------------------------------------------------------------------


class ParseErrorEntry(BaseModel):
    """One ParseError from the PRD parser."""

    model_config = ConfigDict(extra="forbid")

    section: str
    line: int
    message: str


class ParsePrdResponse(BaseModel):
    """Result of parse_prd."""

    model_config = ConfigDict(extra="forbid")

    prd_status: str
    requirement_count: int
    feature_count: int
    task_count: int
    errors: list[ParseErrorEntry]
    prd_path: str


@mcp.tool(tags={PLANNING_TAG})
def parse_prd(
    file: str | None = None,
    cwd: str | None = None,
) -> ParsePrdResponse:
    """Parse the PRD markdown into requirements/features/tasks and emit
    prd.parsed; returns counts. Parse errors are returned in the response (not
    raised) so the caller can fix and retry; ToolError is raised only for
    operational failures (missing/unreadable file, project not initialized).

    Args:
        file: PRD path (absolute or cwd-relative). Defaults to .anvil/prd.md.
        cwd:  Project root. Defaults to Path.cwd().
    """
    from anvil.clock import SystemClock
    from anvil.planning.template import parse_prd as _parse_prd_impl
    from anvil.state.models import EventDraft

    state_dir = _resolve_state_dir(cwd)
    if not state_dir.exists():
        raise ToolError(
            f"anvil not initialized in {state_dir.parent}. "
            "Call init_project first.",
        )

    if file is not None:
        prd_path = Path(file)
        if not prd_path.is_absolute():
            base = Path(cwd).resolve() if cwd else Path.cwd().resolve()
            prd_path = (base / prd_path).resolve()
    else:
        prd_path = state_dir / _PRD_FILENAME

    if not prd_path.exists():
        raise ToolError(
            f"PRD file not found at {prd_path}. "
            "Author your PRD there or pass file= an explicit path.",
        )

    try:
        markdown = prd_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ToolError(f"Cannot read {prd_path}: {exc}") from exc

    result = _parse_prd_impl(markdown, prd_id="prd")

    # Surface errors in the response without short-circuiting the event.
    # When errors exist we skip emission (mirrors the CLI which exits 1
    # before applying); otherwise we emit prd.parsed exactly like the CLI.
    errors_out = [
        ParseErrorEntry(section=e.section, line=e.line, message=e.message)
        for e in result.errors
    ]

    if result.errors:
        return ParsePrdResponse(
            prd_status=result.prd.status.value,
            requirement_count=len(result.requirements),
            feature_count=len(result.features),
            task_count=len(result.tasks),
            errors=errors_out,
            prd_path=str(prd_path),
        )

    backend = _open_backend(state_dir)
    try:
        clock = SystemClock()
        now = clock.now()
        project = backend.get_project()
        project_id = project.id if project is not None else "project"

        payload: dict[str, Any] = {
            "project_id": project_id,
            "status": result.prd.status.value,
            "summary": result.prd.summary,
            "goals": result.prd.goals,
            "non_goals": result.prd.non_goals,
            "requirements": [
                {
                    "id": r.id,
                    "prd_section": r.prd_section,
                    "text": r.text,
                    "source_paragraph": r.source_paragraph,
                    "derived": r.derived,
                }
                for r in result.requirements
            ],
            "acceptance_criteria": result.prd.acceptance_criteria,
            "risks": result.prd.risks,
            "open_questions": result.prd.open_questions,
        }

        backend.append(EventDraft(
            timestamp=now,
            actor="anvil-mcp",
            action="prd.parsed",
            target_kind="prd",
            target_id=project_id,
            payload_json=payload,
        ))
    finally:
        backend.close()

    return ParsePrdResponse(
        prd_status=result.prd.status.value,
        requirement_count=len(result.requirements),
        feature_count=len(result.features),
        task_count=len(result.tasks),
        errors=errors_out,
        prd_path=str(prd_path),
    )


# ---------------------------------------------------------------------------
# Tool 17: review_prd
# ---------------------------------------------------------------------------


class ReviewPrdResponse(BaseModel):
    """Result of review_prd."""

    model_config = ConfigDict(extra="forbid")

    from_status: str
    to_status: str
    reviewer: str


@mcp.tool(tags={PLANNING_TAG})
def review_prd(
    approve: bool = False,
    reviewer: str = "human",
    notes: str | None = None,
    cwd: str | None = None,
) -> ReviewPrdResponse:
    """Advance the PRD review state: draft → reviewed (default), or reviewed →
    approved when approve=True. Emits prd.reviewed or prd.approved.

    Args:
        approve:  True moves reviewed → approved; False moves draft → reviewed.
        reviewer: Identity recorded in the event payload.
        notes:    Optional reviewer notes (recorded on prd.reviewed only).
        cwd:      Project root. Defaults to Path.cwd().
    """
    from anvil.clock import SystemClock
    from anvil.state.backend import EventRejected
    from anvil.state.models import EventDraft

    state_dir = _resolve_state_dir(cwd)
    if not state_dir.exists():
        raise ToolError(
            f"anvil not initialized in {state_dir.parent}. "
            "Call init_project first.",
        )

    backend = _open_backend(state_dir)
    try:
        prd = backend.get_prd()
        if prd is None:
            raise ToolError(
                "No PRD found in state. Run parse_prd first.",
            )
        from_status = prd.status.value
        project = backend.get_project()
        project_id = project.id if project is not None else "project"

        if approve:
            if from_status != "reviewed":
                raise ToolError(
                    f"PRD must be in 'reviewed' status to approve, "
                    f"got '{from_status}'. Call review_prd without "
                    "approve=True first.",
                )
            action = "prd.approved"
            to_status = "approved"
            payload: dict[str, Any] = {"project_id": project_id, "approver": reviewer}
        else:
            if from_status != "draft":
                raise ToolError(
                    f"PRD must be in 'draft' status to review, "
                    f"got '{from_status}'. Pass approve=True to move "
                    "reviewed → approved.",
                )
            action = "prd.reviewed"
            to_status = "reviewed"
            payload = {
                "project_id": project_id,
                "reviewer": reviewer,
                "notes": notes,
            }

        clock = SystemClock()
        now = clock.now()
        try:
            backend.append(EventDraft(
                timestamp=now,
                actor=reviewer,
                action=action,
                target_kind="prd",
                target_id=project_id,
                payload_json=payload,
            ))
        except EventRejected as exc:
            raise ToolError(str(exc)) from exc

        return ReviewPrdResponse(
            from_status=from_status,
            to_status=to_status,
            reviewer=reviewer,
        )
    finally:
        backend.close()


# ---------------------------------------------------------------------------
# Tool 18: plan_tasks
# ---------------------------------------------------------------------------


class PlanTasksResponse(BaseModel):
    """Result of plan_tasks."""

    model_config = ConfigDict(extra="forbid")

    feature_count: int
    task_count: int
    conflict_group_count: int
    warnings: list[ParseErrorEntry]
    # LLM backstop signalling. ``llm_generated`` is True when this call drafted
    # a ``## Tasks`` section via the LLM and appended it to prd.md;
    # ``llm_provider`` is the resolved provider slug (else None).
    llm_generated: bool = False
    llm_provider: str | None = None
    # Orphan-prune signalling: task/feature IDs that were in state.db but absent
    # from the new PRD parse and deleted this call. Empty when none were pruned.
    pruned_task_ids: list[str] = []
    pruned_feature_ids: list[str] = []


@mcp.tool(tags={PLANNING_TAG})
def plan_tasks(
    cwd: str | None = None,
    use_llm: bool = True,
    prune_force: bool = False,
) -> PlanTasksResponse:
    """Run the planner over the current PRD: generate features and tasks, infer
    dependencies and conflict groups, then promote proposed tasks to drafted.

    When the PRD has features but no ``## Tasks`` section, the LLM planner
    drafts tasks, appends them to prd.md, and re-parses (set use_llm=False to
    opt out and keep the deterministic parse). The provider is resolved from
    .anvil/config.yaml, else env auto-detect; see docs/llm-providers.md.

    PRD parse errors surface as warnings; LLM failures raise ToolError rather
    than returning a silent zero-count.

    Args:
        cwd: Project root. Defaults to Path.cwd().
        use_llm: When True (default), draft tasks via LLM if the PRD has
            features but 0 tasks.
        prune_force: When True, delete orphan tasks that advanced past
            ``ready`` (default False raises ToolError so claim/evidence
            history is not lost silently).
    """
    from anvil.clock import SystemClock
    from anvil.planning.inference import infer_all
    from anvil.planning.llm_planner import (
        PlannerProviderUnavailable,
        TaskGenerationError,
        generate_tasks_markdown,
    )
    from anvil.planning.template import parse_prd as _parse_prd_impl
    from anvil.state.backend import EventRejected
    from anvil.state.models import EventDraft

    state_dir = _resolve_state_dir(cwd)
    if not state_dir.exists():
        raise ToolError(
            f"anvil not initialized in {state_dir.parent}. "
            "Call init_project first.",
        )

    prd_path = state_dir / _PRD_FILENAME
    if not prd_path.exists():
        raise ToolError(
            f"PRD file not found at {prd_path}. "
            "Author your PRD and call parse_prd first.",
        )

    try:
        markdown = prd_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ToolError(f"Cannot read {prd_path}: {exc}") from exc

    # v1.17.0 — load config so the LLM-planner backstop honors the
    # project's llm_provider / llm_tier / bedrock / custom-endpoint knobs.
    # Soft-load: a missing or malformed config falls back to env-only
    # resolution rather than blocking the tool.
    #
    # Mirrors cli/plan.py's _load_config_optional pattern: narrow handler
    # for expected error types first, then a labeled last-resort guard for
    # everything else (yaml.YAMLError and friends). That split lets ops
    # distinguish "your YAML is broken" from "the config module itself
    # blew up" in the debug log. (mcp-critic SHOULD FIX, PR #65)
    config = None
    config_path = state_dir / "config.yaml"
    if config_path.exists():
        try:
            from anvil.config import load_config as _load_config

            config = _load_config(config_path)
        except (FileNotFoundError, OSError, ValueError) as exc:
            print(
                f"plan_tasks: config.yaml load failed "
                f"({type(exc).__name__}: {exc}); falling back to env-only "
                "LLM resolution.",
                file=sys.stderr,
            )
        except Exception as exc:  # noqa: BLE001 — last-resort guard, never re-raise
            # yaml.YAMLError and any other unexpected error: warn and
            # fall back. Distinct prefix so the debug log distinguishes
            # this from the narrow-handler path above.
            print(
                f"plan_tasks: unexpected config.yaml load error "
                f"({type(exc).__name__}: {exc}); falling back to env-only "
                "LLM resolution.",
                file=sys.stderr,
            )

    result = _parse_prd_impl(markdown, prd_id="prd")
    warnings = [
        ParseErrorEntry(section=e.section, line=e.line, message=e.message)
        for e in result.errors
    ]

    # ------------------------------------------------------------------
    # LLM task-generation backstop (v1.15+)
    #
    # When the PRD has features+requirements but no `## Tasks` section the
    # deterministic parser yields 0 tasks. Previously plan_tasks returned
    # task_count=0 silently and downstream tools were left without tasks
    # to operate on. Now we call the LLM planner, append generated tasks
    # to prd.md, and re-parse before any events are emitted.
    # ------------------------------------------------------------------
    llm_generated = False
    llm_provider: str | None = None
    if (
        use_llm
        and len(result.tasks) == 0
        and len(result.features) > 0
    ):
        try:
            gen_result = generate_tasks_markdown(
                prd=result.prd,
                features=result.features,
                requirements=result.requirements,
                config=config,
            )
        except PlannerProviderUnavailable as exc:
            raise ToolError(str(exc)) from exc
        except TaskGenerationError as exc:
            # mcp-critic SHOULD FIX from PR #63: TaskGenerationError's
            # message can include up to 500 chars of raw LLM output (see
            # llm_planner._validate_and_normalize). Re-raising it through
            # ToolError leaks that to the MCP client. The full exception
            # is logged for ops, but the client sees a safe summary.
            print(
                f"LLM task generation failed for plan_tasks: {exc}",
                file=sys.stderr,
            )
            raise ToolError(
                "LLM task generation failed: the response did not contain "
                "any '### TXXX:' blocks. Check the LLM provider's output "
                "in stderr for the full response; fix prd.md or re-tune "
                "the prompt and re-run plan_tasks."
            ) from exc

        # Idempotency guard: only append `## Tasks` when not already
        # present, so re-running plan_tasks after a previous append is a
        # no-op on the file.
        try:
            current_markdown = prd_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ToolError(f"Cannot re-read {prd_path}: {exc}") from exc

        from anvil.planning._plan_helpers import has_tasks_section
        if not has_tasks_section(current_markdown):
            new_markdown = (
                current_markdown.rstrip() + "\n\n" + gen_result.markdown + "\n"
            )
            try:
                prd_path.write_text(new_markdown, encoding="utf-8")
            except OSError as exc:
                raise ToolError(
                    f"Cannot write generated tasks to {prd_path}: {exc}"
                ) from exc

        # Re-parse so the event emission below sees the new tasks.
        try:
            markdown = prd_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ToolError(f"Cannot re-read {prd_path}: {exc}") from exc
        result = _parse_prd_impl(markdown, prd_id="prd")
        llm_generated = True
        llm_provider = gen_result.provider_used

    backend = _open_backend(state_dir)
    try:
        # Guard: `parse_prd` must have run first so the backend has a PRD row.
        # Without this check, an out-of-order call would emit feature/task
        # events into a backend with no PRD row, leaving downstream tools
        # (review_prd, apply_review_decision) to fail with "No PRD found"
        # after the state was already mutated. Fail loudly here instead.
        if backend.get_prd() is None:
            raise ToolError(
                "No PRD found in state. Call parse_prd before plan_tasks so "
                "the PRD row exists before feature and task events are emitted."
            )

        clock = SystemClock()

        # --------------------------------------------------------------
        # Orphan-prune (v1.15.0). Shares planning._plan_helpers with the
        # CLI — see that module's docstring for the multi-critic review
        # finding that drove the extraction (previously this logic was
        # duplicated, the safe-status set was triplicated, and the CLI
        # was missing the TransactionAborted catch that the MCP had).
        # --------------------------------------------------------------
        from anvil.planning._plan_helpers import (
            classify_orphans,
            emit_prune_events,
        )

        classification = classify_orphans(
            backend.list_tasks(),
            {t.id for t in result.tasks},
            backend.list_features(),
            {f.id for f in result.features},
        )

        if classification.unsafe_task_orphans and not prune_force:
            blocked = ", ".join(
                f"{t.id}({t.status.value})"
                for t in classification.unsafe_task_orphans
            )
            raise ToolError(
                f"{len(classification.unsafe_task_orphans)} orphan task(s) "
                "removed from prd.md have advanced past `ready` status; "
                "deleting silently would lose claim/evidence history. "
                f"Blocked: {blocked}. Release the claims (or complete the "
                "work) and re-call plan_tasks, OR re-call with "
                "prune_force=True to delete despite the status (audit "
                "history is preserved either way)."
            )

        try:
            prune_result = emit_prune_events(
                backend,
                classification,
                actor="anvil-mcp",
                clock=clock,
                prune_force=prune_force,
            )
        except EventRejected as exc:
            raise ToolError(str(exc)) from exc

        pruned_task_ids = prune_result.pruned_task_ids
        pruned_feature_ids = prune_result.pruned_feature_ids

        # Emit feature.created per feature.
        for feature in result.features:
            now = clock.now()
            try:
                backend.append(EventDraft(
                    timestamp=now,
                    actor="anvil-mcp",
                    action="feature.created",
                    target_kind="feature",
                    target_id=feature.id,
                    payload_json=feature.model_dump(mode="json"),
                ))
            except EventRejected as exc:
                raise ToolError(str(exc)) from exc

        # Emit task.created per task.
        for task in result.tasks:
            now = clock.now()
            try:
                backend.append(EventDraft(
                    timestamp=now,
                    actor="anvil-mcp",
                    action="task.created",
                    target_kind="task",
                    target_id=task.id,
                    payload_json=task.model_dump(mode="json"),
                ))
            except EventRejected as exc:
                raise ToolError(str(exc)) from exc

        inference_result = infer_all(result.tasks)

        for inferred_task in inference_result.tasks:
            now = clock.now()
            try:
                backend.append(EventDraft(
                    timestamp=now,
                    actor="anvil-mcp",
                    action="task.created",
                    target_kind="task",
                    target_id=inferred_task.id,
                    payload_json=inferred_task.model_dump(mode="json"),
                ))
            except EventRejected as exc:
                raise ToolError(str(exc)) from exc

            current = backend.get_task(inferred_task.id)
            if current is not None and current.status.value == "proposed":
                now = clock.now()
                try:
                    backend.append(EventDraft(
                        timestamp=now,
                        actor="anvil-mcp",
                        action="task.status_changed",
                        target_kind="task",
                        target_id=inferred_task.id,
                        payload_json={
                            "task_id": inferred_task.id,
                            "from": "proposed",
                            "to": "drafted",
                            "reason": "plan_tasks: initial draft after inference",
                        },
                    ))
                except EventRejected as exc:
                    raise ToolError(str(exc)) from exc

        # CL-4 — persist the inferred ConflictGroups so the conflict_groups
        # table round-trips them (parity with `anvil plan`). The task rows
        # already carry the group IDs; these events populate the dedicated
        # table with the full group records.
        for cg in inference_result.conflict_groups:
            now = clock.now()
            try:
                backend.append(EventDraft(
                    timestamp=now,
                    actor="anvil-mcp",
                    action="conflict_group.upserted",
                    target_kind="conflict_group",
                    target_id=cg.id,
                    payload_json=cg.model_dump(mode="json"),
                ))
            except EventRejected as exc:
                raise ToolError(str(exc)) from exc

        return PlanTasksResponse(
            feature_count=len(result.features),
            task_count=len(result.tasks),
            conflict_group_count=len(inference_result.conflict_groups),
            warnings=warnings,
            llm_generated=llm_generated,
            llm_provider=llm_provider,
            pruned_task_ids=pruned_task_ids,
            pruned_feature_ids=pruned_feature_ids,
        )
    finally:
        backend.close()


# `_has_tasks_section` and `_TASKS_HEADING_RE` previously lived here as a
# twin of cli/plan.py. As of v1.15.0 post-review they live in
# planning/_plan_helpers.py — see that module's docstring for the
# multi-critic finding that drove the extraction.


# ---------------------------------------------------------------------------
# Tool 19: score_tasks
# ---------------------------------------------------------------------------


class TaskScoreEntry(BaseModel):
    """One per-task score in the score_tasks response."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    complexity: int
    parallelizability: int
    context_load: int
    blast_radius: int
    review_risk: int
    agent_suitability: int


class ExpansionQueueEntry(BaseModel):
    """One task queued for sub-task expansion (complexity >= threshold),
    carrying the task identity, its complexity, a suggested split size, and the
    exact CLI follow-up command. Expansion itself runs via the planner agent /
    ``expand --use-llm``, never here."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    title: str
    complexity: int
    suggested_subtasks: int
    expand_command: str


class ScoreTasksResponse(BaseModel):
    """Result of score_tasks."""

    model_config = ConfigDict(extra="forbid")

    scored: list[TaskScoreEntry]
    skipped_already_scored: int
    # ``expansion_queue`` lists every task at/above ``auto_expand_threshold``
    # when ``auto_expand`` is on; empty when disabled.
    auto_expand: bool
    auto_expand_threshold: int
    expansion_queue: list[ExpansionQueueEntry]


@mcp.tool(tags={PLANNING_TAG})
def score_tasks(
    task_id: str | None = None,
    cwd: str | None = None,
) -> ScoreTasksResponse:
    """Run the rule-based (non-LLM) scoring engine on one task or all unscored
    tasks across six dimensions; emits task.scored per task.

    Pass task_id to always re-score that one task; pass None to score only
    tasks whose scores are incomplete (the rest count toward
    skipped_already_scored). The response also carries a deterministic
    expansion_queue of high-complexity tasks; the LLM-side expansion runs via
    the planner agent, never here.

    Args:
        task_id: Specific task to score (always re-scored). None scores all
                 unscored tasks.
        cwd:     Project root. Defaults to Path.cwd().
    """
    from anvil.cli._helpers import _scores_complete
    from anvil.clock import SystemClock
    from anvil.config import DEFAULT_AUTO_EXPAND_THRESHOLD
    from anvil.planning.scoring import build_recursive_expansion_queue, score_task
    from anvil.state.backend import EventRejected
    from anvil.state.models import EventDraft

    state_dir = _resolve_state_dir(cwd)
    if not state_dir.exists():
        raise ToolError(
            f"anvil not initialized in {state_dir.parent}. "
            "Call init_project first.",
        )

    # v1.21.0 — soft-load config for the auto-expansion knobs. Mirrors the
    # plan_tasks pattern above: a missing or malformed config never blocks
    # the tool; we fall back to the defaults (auto_expand on, threshold 4).
    auto_expand = True
    auto_expand_threshold = DEFAULT_AUTO_EXPAND_THRESHOLD
    config_path = state_dir / "config.yaml"
    if config_path.exists():
        try:
            from anvil.config import load_config as _load_config

            _config = _load_config(config_path)
            auto_expand = _config.auto_expand
            auto_expand_threshold = _config.auto_expand_threshold
        except (FileNotFoundError, OSError, ValueError) as exc:
            print(
                f"score_tasks: config.yaml load failed "
                f"({type(exc).__name__}: {exc}); falling back to default "
                "auto-expansion settings.",
                file=sys.stderr,
            )
        except Exception as exc:  # noqa: BLE001 — last-resort guard, never re-raise
            # yaml.YAMLError and any other unexpected error: warn and fall
            # back. Distinct prefix so the debug log distinguishes this
            # from the narrow-handler path above.
            print(
                f"score_tasks: unexpected config.yaml load error "
                f"({type(exc).__name__}: {exc}); falling back to default "
                "auto-expansion settings.",
                file=sys.stderr,
            )

    backend = _open_backend(state_dir)
    try:
        if task_id is not None:
            task = backend.get_task(task_id)
            if task is None:
                raise ToolError(f"Task '{task_id}' not found.")
            tasks_to_score = [task]
            skipped = 0
        else:
            all_tasks = backend.list_tasks()
            tasks_to_score = [t for t in all_tasks if not _scores_complete(t)]
            skipped = len(all_tasks) - len(tasks_to_score)

        clock = SystemClock()
        scored: list[TaskScoreEntry] = []
        for task in tasks_to_score:
            computed = score_task(task)
            now = clock.now()
            payload: dict[str, Any] = {
                "task_id": task.id,
                "scores": {
                    "complexity": computed.complexity,
                    "parallelizability": computed.parallelizability,
                    "context_load": computed.context_load,
                    "blast_radius": computed.blast_radius,
                    "review_risk": computed.review_risk,
                    "agent_suitability": computed.agent_suitability,
                },
                "explanation": computed.explanation,
            }
            try:
                backend.append(EventDraft(
                    timestamp=now,
                    actor="anvil-mcp",
                    action="task.scored",
                    target_kind="task",
                    target_id=task.id,
                    payload_json=payload,
                ))
            except EventRejected as exc:
                raise ToolError(str(exc)) from exc

            scored.append(TaskScoreEntry(
                task_id=task.id,
                complexity=computed.complexity,
                parallelizability=computed.parallelizability,
                context_load=computed.context_load,
                blast_radius=computed.blast_radius,
                review_risk=computed.review_risk,
                agent_suitability=computed.agent_suitability,
            ))

        # v1.21.0 — re-fetch AFTER the task.scored events landed so the
        # queue covers every task at/above threshold (including ones scored
        # in earlier runs), not just this call's batch.
        expansion_queue: list[ExpansionQueueEntry] = []
        if auto_expand:
            expansion_queue = [
                ExpansionQueueEntry(
                    task_id=candidate.task_id,
                    title=candidate.title,
                    complexity=candidate.complexity,
                    suggested_subtasks=candidate.suggested_subtasks,
                    expand_command=(
                        f"anvil expand {candidate.task_id} --use-llm"
                    ),
                )
                for candidate in build_recursive_expansion_queue(
                    backend.list_tasks(), threshold=auto_expand_threshold
                )
            ]

        return ScoreTasksResponse(
            scored=scored,
            skipped_already_scored=skipped,
            auto_expand=auto_expand,
            auto_expand_threshold=auto_expand_threshold,
            expansion_queue=expansion_queue,
        )
    finally:
        backend.close()


# ---------------------------------------------------------------------------
# Tool 20: review_tasks
# ---------------------------------------------------------------------------


class BlockedTaskEntry(BaseModel):
    """One task that failed a review gate."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    reason: str


class ReviewTasksResponse(BaseModel):
    """Result of review_tasks."""

    model_config = ConfigDict(extra="forbid")

    promoted_to_reviewed: list[str]
    promoted_to_ready: list[str]
    blocked: list[BlockedTaskEntry]


@mcp.tool(tags={PLANNING_TAG})
def review_tasks(cwd: str | None = None) -> ReviewTasksResponse:
    """Promote tasks through drafted → reviewed → ready, applying the review
    gates. Returns the promoted task IDs per stage plus any tasks a gate
    blocked (with reasons).

    Args:
        cwd: Project root. Defaults to Path.cwd().
    """
    from anvil.clock import SystemClock
    from anvil.state.backend import EventRejected
    from anvil.state.models import EventDraft
    from anvil.state.transitions import (
        TransitionError,
        task_drafted_to_reviewed,
        task_reviewed_to_ready,
    )

    state_dir = _resolve_state_dir(cwd)
    if not state_dir.exists():
        raise ToolError(
            f"anvil not initialized in {state_dir.parent}. "
            "Call init_project first.",
        )

    backend = _open_backend(state_dir)
    try:
        clock = SystemClock()
        all_tasks = backend.list_tasks()

        drafted = [t for t in all_tasks if t.status.value == "drafted"]
        already_reviewed_ids = {
            t.id for t in all_tasks if t.status.value == "reviewed"
        }

        promoted_to_reviewed: list[str] = []
        promoted_to_ready: list[str] = []
        blocked: list[BlockedTaskEntry] = []

        # drafted → reviewed
        for task in drafted:
            now = clock.now()
            try:
                task_drafted_to_reviewed(task, now)
            except TransitionError as exc:
                blocked.append(BlockedTaskEntry(task_id=task.id, reason=exc.message))
                continue
            try:
                backend.append(EventDraft(
                    timestamp=now,
                    actor="anvil-mcp",
                    action="task.status_changed",
                    target_kind="task",
                    target_id=task.id,
                    payload_json={
                        "task_id": task.id,
                        "from": "drafted",
                        "to": "reviewed",
                        "reason": "review_tasks: gate passed",
                    },
                ))
            except EventRejected as exc:
                raise ToolError(str(exc)) from exc
            promoted_to_reviewed.append(task.id)

        # reviewed → ready (covers tasks promoted just above plus pre-existing reviewed)
        candidates = backend.list_tasks()
        promoted_set = set(promoted_to_reviewed)
        for task in candidates:
            if task.status.value != "reviewed":
                continue
            if task.id not in promoted_set and task.id not in already_reviewed_ids:
                continue
            now = clock.now()
            try:
                task_reviewed_to_ready(task, now)
            except TransitionError as exc:
                blocked.append(BlockedTaskEntry(task_id=task.id, reason=exc.message))
                continue
            try:
                backend.append(EventDraft(
                    timestamp=now,
                    actor="anvil-mcp",
                    action="task.status_changed",
                    target_kind="task",
                    target_id=task.id,
                    payload_json={
                        "task_id": task.id,
                        "from": "reviewed",
                        "to": "ready",
                        "reason": "review_tasks: promoted to ready",
                    },
                ))
            except EventRejected as exc:
                raise ToolError(str(exc)) from exc
            promoted_to_ready.append(task.id)

        return ReviewTasksResponse(
            promoted_to_reviewed=promoted_to_reviewed,
            promoted_to_ready=promoted_to_ready,
            blocked=blocked,
        )
    finally:
        backend.close()


# ---------------------------------------------------------------------------
# Tool 21: apply_review_decision
# ---------------------------------------------------------------------------


class ApplyReviewResponse(BaseModel):
    """Result of apply_review_decision."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    decision: str  # "accepted" or "rejected"
    from_status: str
    to_status: str
    reviewer: str
    # The next claimable task after this disposition (an approval may unblock
    # dependents); null when none is available.
    next_ready: NextReadyTask | None = None


@mcp.tool(tags={PLANNING_TAG})
def apply_review_decision(
    task_id: str,
    approve: bool,
    reviewer: str = "human",
    reason: str | None = None,
    strict: bool | None = None,
    cwd: str | None = None,
) -> ApplyReviewResponse:
    """Apply a human review decision on a needs_review task: approve (→ accepted
    → done) or reject (→ rejected/drafted for rework). Emits task.applied; the
    backend auto-promotes through accepted → done on approval.

    Under strict evidence mode an approval REFUSES (ToolError code
    ``evidence_incomplete``, listing the missing items) before any event is
    appended, leaving the task in needs_review; rejections are never gated.
    Strict resolves as: explicit ``strict`` param > config ``strict_evidence`` >
    False (advisory).

    Args:
        task_id:  Task awaiting review (must be in needs_review status).
        approve:  True accepts the work; False rejects it.
        reviewer: Identity recorded in the event payload.
        reason:   Required when approve=False; recorded as review notes.
        strict:   Evidence-gate override (approve only). None defers to config.
        cwd:      Project root. Defaults to Path.cwd().
    """
    from anvil.clock import SystemClock
    from anvil.state.backend import EventRejected
    from anvil.state.models import EventDraft

    state_dir = _resolve_state_dir(cwd)
    if not state_dir.exists():
        raise ToolError(
            f"anvil not initialized in {state_dir.parent}. "
            "Call init_project first.",
        )

    if not approve and not reason:
        raise ToolError(
            "Rejection requires reason= (non-empty). "
            "Pass approve=True to accept, or provide a rejection reason.",
        )

    backend = _open_backend(state_dir)
    try:
        task = backend.get_task(task_id)
        if task is None:
            raise ToolError(f"Task '{task_id}' not found.")

        from_status = task.status.value
        if from_status != "needs_review":
            raise ToolError(
                f"Task '{task_id}' has status '{from_status}', "
                "expected 'needs_review'. Submit completion evidence first.",
            )

        # T025/B25 — completion-evidence ENFORCEMENT on the MCP accept path.
        # Only approvals are gated (rejecting a task with missing evidence is
        # the right move). When strict is in effect and the gate is INCOMPLETE,
        # refuse BEFORE appending the task.applied event so the task stays in
        # needs_review. A complete gate, or a task with no required_evidence,
        # is a no-op. DEFAULT (strict None, no config) preserves the historical
        # advisory behaviour — accept proceeds regardless.
        if approve and _resolve_strict_evidence(strict, state_dir):
            from anvil.review.gates import evidence_complete

            evidence_obj = backend.get_latest_evidence(task_id)
            if evidence_obj is not None:
                gate_passed, gate_missing = evidence_complete(task, evidence_obj)
            elif task.verification.required_evidence:
                # No evidence at all when something is required is a failure.
                gate_passed, gate_missing = (
                    False,
                    list(task.verification.required_evidence),
                )
            else:
                gate_passed, gate_missing = True, []

            if not gate_passed:
                # Standard MCP error surface: raise ToolError. The message
                # carries the stable code ``evidence_incomplete`` plus the
                # missing items so callers can branch on it the same way the
                # CLI's JSON ``error.code`` does.
                raise ToolError(
                    f"evidence_incomplete: strict evidence gate refused "
                    f"approval of task '{task_id}'; required evidence is "
                    f"missing ({', '.join(gate_missing)}). Task remains in "
                    "needs_review. Submit the missing evidence and retry, or "
                    "pass strict=False to override for this call.",
                )

        decision = "accepted" if approve else "rejected"
        clock = SystemClock()
        now = clock.now()
        payload: dict[str, Any] = {
            "task_id": task_id,
            "reviewer": reviewer,
            "decision": decision,
            "notes": reason,
        }

        try:
            backend.append(EventDraft(
                timestamp=now,
                actor=reviewer,
                action="task.applied",
                target_kind="task",
                target_id=task_id,
                payload_json=payload,
            ))
        except EventRejected as exc:
            raise ToolError(str(exc)) from exc

        # Read fresh status after the backend's auto-promotion (accepted → done
        # on approval, needs_review → drafted on rejection, etc.).
        fresh = backend.get_task(task_id)
        to_status = fresh.status.value if fresh is not None else decision

        # T014: name the next claimable task after this disposition. Use the
        # reviewer as the actor (a human reviewer holds no active claims, so
        # all foreign locks are honoured).
        next_ready_raw = _compute_next_ready(backend, reviewer)
        next_ready = (
            NextReadyTask(**next_ready_raw) if next_ready_raw is not None else None
        )

        return ApplyReviewResponse(
            task_id=task_id,
            decision=decision,
            from_status=from_status,
            to_status=to_status,
            reviewer=reviewer,
            next_ready=next_ready,
        )
    finally:
        backend.close()


# ===========================================================================
# Decision resolution
# ===========================================================================
#
# One read-only tool that surfaces unresolved decisions in the PRD so the
# `resolve-decisions` skill (markdown) can drive Q&A. Detection logic lives
# in anvil.planning.decisions and is shared with the CLI.


# ---------------------------------------------------------------------------
# Tool 22: find_decisions
# ---------------------------------------------------------------------------


class UnresolvedDecisionEntry(BaseModel):
    """One unresolved-decision record, flat for over-the-wire transport."""

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: str  # "needs_decision" | "open_question" | "missing_field"
    location: str
    text: str
    context_paragraph: str
    suggested_resolution_field: str


class FindDecisionsResponse(BaseModel):
    """Result of find_decisions."""

    model_config = ConfigDict(extra="forbid")

    decisions: list[UnresolvedDecisionEntry]
    counts_by_kind: dict[str, int]
    total: int


@mcp.tool(tags={PLANNING_TAG})
def find_decisions(cwd: str | None = None) -> FindDecisionsResponse:
    """Scan the PRD for items needing a human decision (read-only; emits no
    events). Walks three sources: inline ``[NEEDS DECISION]`` markers,
    ``## Open Questions`` items, and tasks with empty acceptance_criteria or
    verification.commands. Drives the `resolve-decisions` skill.

    Returns the decisions (needs_decision, then open_question, then
    missing_field), counts by kind, and the total. Raises ToolError when
    .anvil/ or prd.md is missing.

    Args:
        cwd: Project root. Defaults to ``Path.cwd()``.
    """
    from anvil.planning.decisions import find_unresolved_decisions
    from anvil.planning.template import parse_prd as _parse_prd_impl

    state_dir = _resolve_state_dir(cwd)
    if not state_dir.exists():
        raise ToolError(
            f"anvil not initialized in {state_dir.parent}. "
            "Call init_project first.",
        )

    prd_path = state_dir / _PRD_FILENAME
    if not prd_path.exists():
        raise ToolError(
            f"PRD file not found at {prd_path}. "
            "Author your PRD and call parse_prd before find_decisions.",
        )

    try:
        markdown = prd_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ToolError(f"Cannot read {prd_path}: {exc}") from exc

    result = _parse_prd_impl(markdown, prd_id="prd")
    # Match the CLI's behavior: if the parse failed, surface the errors
    # rather than silently returning a deceptive 0-open_questions count
    # (the PRD model exists but with empty sections). The needs_decision
    # detector works against raw markdown and would still find inline
    # markers, but the user almost certainly wants the parse failure
    # surfaced first so they can fix the structural problem before
    # interpreting the decision list.
    if result.errors:
        error_summary = "; ".join(
            f"[{e.section}:{e.line}] {e.message}" for e in result.errors[:5]
        )
        if len(result.errors) > 5:
            error_summary += f"; (+{len(result.errors) - 5} more)"
        raise ToolError(
            f"PRD parse failed with {len(result.errors)} error(s); "
            f"fix prd.md and call parse_prd before find_decisions. {error_summary}"
        )

    backend = _open_backend(state_dir)
    try:
        backend_tasks = backend.list_tasks()
        tasks_or_none = backend_tasks if backend_tasks else None
    finally:
        backend.close()

    decisions = find_unresolved_decisions(
        markdown,
        prd=result.prd,
        tasks=tasks_or_none,
    )

    entries = [
        UnresolvedDecisionEntry(
            id=d.id,
            kind=d.kind.value,
            location=d.location,
            text=d.text,
            context_paragraph=d.context_paragraph,
            suggested_resolution_field=d.suggested_resolution_field,
        )
        for d in decisions
    ]

    counts: dict[str, int] = {
        "needs_decision": 0,
        "open_question": 0,
        "missing_field": 0,
    }
    for d in decisions:
        counts[d.kind.value] = counts.get(d.kind.value, 0) + 1

    return FindDecisionsResponse(
        decisions=entries,
        counts_by_kind=counts,
        total=len(entries),
    )


# ---------------------------------------------------------------------------
# Tool 23: describe_surface (self-describing command surface — T012)
# ---------------------------------------------------------------------------


@mcp.tool(tags={PLANNING_TAG})
def describe_surface() -> dict[str, Any]:
    """Return a machine-readable manifest of the anvil command surface: the CLI
    subcommands and MCP tool names this engine exposes, plus engine version,
    schema version, and a stable ``api_version`` to pin against. Introspected
    live, needs no project. Lets an MCP-only host discover the surface."""
    # Imported lazily and reused so the CLI and MCP surfaces report the IDENTICAL
    # manifest (single source of truth — no second hand-maintained list).
    from anvil.cli.describe import build_manifest

    return build_manifest()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

# A one-line usage string shared by ``--help`` and the unknown-flag error path.
_USAGE = "usage: python -m anvil.mcp_server [--help] [--version]"


def _help_text() -> str:
    """Render the ``--help`` page.

    Deliberately self-contained (no project/backend access) so it works inside
    a bare Docker image where no ``.anvil/`` exists yet. The tool list is
    introspected live from the registered FastMCP surface so it can never drift
    from reality. ``ANVIL_ROOT`` is documented here because the container
    image resolves project state through it (a bind-mounted host directory).
    """
    from anvil import __version__
    from anvil.cli.describe import mcp_tool_names

    tools = mcp_tool_names()
    lines = [
        f"anvil-mcp {__version__} — FastMCP (stdio) server",
        "",
        _USAGE,
        "",
        "Run with no arguments to start the stdio MCP server (the default; this",
        "is what an MCP client launches). --help and --version print and exit 0",
        "without opening a backend, so they are safe as a container smoke test.",
        "",
        "Options:",
        "  -h, --help      Show this help and exit.",
        "  -v, --version   Print the engine version and exit.",
        "",
        "Environment:",
        "  ANVIL_ROOT  Project root holding .anvil/ (defaults to the",
        "                     current working directory). In Docker, bind-mount the",
        "                     host project here, e.g. -v \"$PWD:/project\" -e",
        "                     ANVIL_ROOT=/project.",
        "  ANVIL_MCP_PLANNING  When truthy (1/true/yes/on), the live server",
        "                     exposes the full 24-tool surface. By DEFAULT the 10",
        "                     one-shot planning tools (init_project, parse_prd,",
        "                     review_prd, plan_tasks, score_tasks, review_tasks,",
        "                     apply_review_decision, edit_dependencies,",
        "                     find_decisions, describe_surface) are hidden from the",
        "                     per-turn wire surface to cut always-on context; the",
        "                     14 execution tools remain. All 24 are always",
        "                     registered (this list reflects the full surface).",
        "",
        f"Registered tools ({len(tools)}):",
    ]
    lines += [f"  {name}" for name in tools]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for the MCP server.

    With no recognised flags this starts the blocking stdio server (the default
    an MCP client invokes) and never returns. ``--help``/``--version`` short-
    circuit before ``mcp.run()`` so a container smoke test
    (``docker run --rm anvil-mcp --help``) prints and exits cleanly
    instead of hanging on stdio. Backward-compatible: the no-arg path is
    unchanged.
    """
    args = sys.argv[1:] if argv is None else argv

    if any(a in ("-h", "--help") for a in args):
        print(_help_text())
        return 0

    if any(a in ("-v", "--version") for a in args):
        from anvil import __version__

        print(__version__)
        return 0

    # Reject unknown flags rather than silently ignoring them and starting the
    # server — a typo'd flag should fail fast, not block on stdio forever.
    unknown = [a for a in args if a.startswith("-")]
    if unknown:
        print(f"anvil-mcp: unrecognized arguments: {' '.join(unknown)}", file=sys.stderr)
        print(_USAGE, file=sys.stderr)
        return 2

    # L2: hide the one-shot planning tool surface on the live wire UNLESS the
    # operator opts back in via ANVIL_MCP_PLANNING. This shrinks the always-on
    # per-turn cost for the common execution client without removing any tool —
    # all 24 stay registered (introspection/--help/describe unchanged) and the
    # planning 10 return the moment the flag is set. Applied here, not at import,
    # so only the live server's wire surface is affected.
    if not apply_surface_gate(mcp):
        print(
            "anvil-mcp: planning tools hidden (execution surface only). "
            f"Set {_PLANNING_ENV}=1 to expose the full 24-tool surface for "
            "planning.",
            file=sys.stderr,
        )

    mcp.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
