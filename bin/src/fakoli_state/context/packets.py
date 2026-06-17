"""Work-packet renderer for fakoli-state.

A work packet is the exact context an agent needs to execute one Task — intent,
acceptance criteria, scope, dependencies, decisions, constraints, verification
commands, and output contract — and nothing else.

Length budget (informational, not enforced):
- Small task (few deps, no decisions): ~800–1 500 chars of markdown.
- Large task (many deps + decisions):  ~4 000–6 000 chars of markdown.

The module is pure: no I/O, no logging, no LLM calls. The CLI (or MCP layer)
is responsible for collecting the inputs and writing the output to
``.fakoli-state/packets/{task_id}.md``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fakoli_state.state.models import (
        Claim,
        Decision,
        Feature,
        Task,
    )

__all__ = ["WorkPacket", "render_packet"]


# ---------------------------------------------------------------------------
# Output type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkPacket:
    """A rendered work packet for a single Task — what an agent receives.

    Attributes:
        task_id:   The Task's ID (e.g. ``T001``), used as the packet filename
                   stem by the CLI.
        markdown:  Human/Claude-paste form suitable for pasting into a prompt
                   or writing to ``.fakoli-state/packets/{task_id}.md``.
        json_data: Structured form returned by the MCP ``get_work_packet``
                   tool in Phase 6.  Keys mirror the markdown sections.
    """

    task_id: str
    markdown: str
    json_data: dict[str, Any]


# ---------------------------------------------------------------------------
# Internal helpers — each renders one logical section of the markdown.
# ---------------------------------------------------------------------------


def _score_str(value: int | None) -> str:
    """Return ``"N/5"`` or ``"unscored"``."""
    if value is None:
        return "unscored"
    return f"{value}/5"


def _bullets(items: list[str], *, none_label: str = "None declared.") -> str:
    """Return a bulleted list or a fallback label when *items* is empty."""
    if not items:
        return none_label
    return "\n".join(f"- {item}" for item in items)


def _render_markdown(
    task: Task,
    *,
    feature: Feature | None,
    dependencies_completed: list[Task],
    dependencies_open: list[Task],
    related_decisions: list[Decision],
    active_claim: Claim | None,
) -> str:
    """Build the full markdown string from the normalised inputs."""
    lines: list[str] = []

    # --- Header ---
    lines.append(f"# {task.id} — {task.title}")
    lines.append("")

    if feature is not None:
        lines.append(f"**Feature:** {feature.id} — {feature.title}")
    lines.append(f"**Status:** {task.status.value}")
    lines.append(f"**Priority:** {task.priority.value}")
    lines.append(
        f"**Agent suitability:** {_score_str(task.scores.agent_suitability)}"
    )
    lines.append(f"**Complexity:** {_score_str(task.scores.complexity)}")
    lines.append("")

    # --- Goal ---
    lines.append("## Goal")
    lines.append("")
    lines.append(task.description)
    lines.append("")

    # --- Acceptance criteria ---
    if task.acceptance_criteria:
        lines.append("## Acceptance criteria")
        lines.append("")
        lines.append(_bullets(task.acceptance_criteria))
        lines.append("")

    # --- Dependencies (completed) ---
    if dependencies_completed:
        lines.append("## Dependencies (completed)")
        lines.append("")
        for dep in dependencies_completed:
            lines.append(f"- {dep.id}: {dep.title}")
        lines.append("")

    # --- Dependencies (open) ---
    if dependencies_open:
        lines.append("## Dependencies (open)")
        lines.append("")
        for dep in dependencies_open:
            lines.append(f"- {dep.id}: {dep.title}")
        lines.append("")

    # --- Scope ---
    if task.likely_files:
        lines.append("## Scope (likely files)")
        lines.append("")
        for path in task.likely_files:
            lines.append(f"- {path}")
        lines.append("")

    # --- Constraints / non-goals ---
    lines.append("## Constraints / non-goals")
    lines.append("")
    lines.append(_bullets(task.implementation_notes, none_label="None declared."))
    lines.append("")

    # --- Decisions ---
    if related_decisions:
        lines.append("## Decisions affecting this task")
        lines.append("")
        for dec in related_decisions:
            lines.append(f"- {dec.id}: {dec.title} — {dec.decision}")
        lines.append("")

    # --- Verification ---
    lines.append("## Verification")
    lines.append("")
    if task.verification.commands:
        lines.append("Commands:")
        for cmd in task.verification.commands:
            lines.append(f"- `{cmd}`")
        lines.append("")
    if task.verification.required_evidence:
        lines.append("Required evidence:")
        for item in task.verification.required_evidence:
            lines.append(f"- {item}")
        lines.append("")
    if task.verification.manual_steps:
        lines.append("Manual steps:")
        for step in task.verification.manual_steps:
            lines.append(f"- {step}")
        lines.append("")

    # --- Active claim ---
    if active_claim is not None:
        lines.append("## Active claim")
        lines.append("")
        lines.append(f"**Claim ID:** {active_claim.id}")
        lines.append(
            f"**Lease expires:** {active_claim.lease_expires_at.isoformat()}"
        )
        lines.append(f"**Branch:** {active_claim.branch or '—'}")
        lines.append(f"**Worktree:** {active_claim.worktree_path or '—'}")
        lines.append("")

    # --- Update protocol ---
    lines.append("## Update protocol")
    lines.append("")
    if active_claim is not None:
        lines.append(
            f"- Heartbeat your claim every 5 minutes via"
            f" `fakoli-state renew {active_claim.id}`"
        )
    lines.append(
        f"- On completion, submit evidence via"
        f" `fakoli-state submit {task.id}"
        f" --commands ... --files-changed ...`"
    )
    lines.append(
        "- Status will transition"
        " `claimed → in_progress → needs_review → accepted → done`"
    )

    # Strip any trailing blank line the loop may have accumulated.
    return "\n".join(lines).rstrip() + "\n"


def _render_json(
    task: Task,
    *,
    feature: Feature | None,
    dependencies_completed: list[Task],
    dependencies_open: list[Task],
    related_decisions: list[Decision],
    active_claim: Claim | None,
) -> dict[str, Any]:
    """Build the structured JSON dict that mirrors the markdown sections."""
    task_data: dict[str, Any] = json.loads(task.model_dump_json())
    feature_data: dict[str, Any] | None = (
        json.loads(feature.model_dump_json()) if feature is not None else None
    )
    deps_completed_data: list[dict[str, Any]] = [
        json.loads(d.model_dump_json()) for d in dependencies_completed
    ]
    deps_open_data: list[dict[str, Any]] = [
        json.loads(d.model_dump_json()) for d in dependencies_open
    ]
    decisions_data: list[dict[str, Any]] = [
        json.loads(d.model_dump_json()) for d in related_decisions
    ]
    claim_data: dict[str, Any] | None = (
        json.loads(active_claim.model_dump_json()) if active_claim is not None else None
    )

    update_protocol: dict[str, str] = {
        "submit_command": (
            f"fakoli-state submit {task.id} --commands ... --files-changed ..."
        ),
        "status_flow": (
            "claimed → in_progress → needs_review → accepted → done"
        ),
    }
    if active_claim is not None:
        update_protocol["renew_command"] = (
            f"fakoli-state renew {active_claim.id}"
        )

    return {
        "task_id": task.id,
        "task": task_data,
        "feature": feature_data,
        "dependencies_completed": deps_completed_data,
        "dependencies_open": deps_open_data,
        "decisions": decisions_data,
        "active_claim": claim_data,
        "update_protocol": update_protocol,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def render_packet(
    task: Task,
    *,
    feature: Feature | None = None,
    dependencies_completed: list[Task] | None = None,
    dependencies_open: list[Task] | None = None,
    related_decisions: list[Decision] | None = None,
    active_claim: Claim | None = None,
) -> WorkPacket:
    """Render a Task plus its surrounding context into a WorkPacket.

    The caller is responsible for supplying the right context objects; this
    function is pure (no I/O, no logging, no LLM calls) and deterministic for
    a fixed input.

    Args:
        task:
            The primary Task to render.  Required.
        feature:
            Parent Feature, included in the packet header when present.
        dependencies_completed:
            Tasks in ``task.dependencies`` that have reached ``done`` status.
            Surfaced separately from open dependencies so the agent sees the
            gap between what is finished and what must still happen before this
            task can be completed.
        dependencies_open:
            Tasks in ``task.dependencies`` that are NOT yet ``done``.
        related_decisions:
            Decisions where ``task.id`` is in ``decision.related_tasks``.
            Pass only the pre-filtered subset — do not pass all decisions.
        active_claim:
            If present, the packet documents the claim's lease and branch so
            the agent knows the boundary it is working within, and the update
            protocol section includes the exact ``renew`` command.

    Returns:
        A :class:`WorkPacket` with both ``markdown`` (human/Claude-paste form)
        and ``json_data`` (structured form for the MCP layer).
    """
    resolved_deps_completed: list[Task] = dependencies_completed or []
    resolved_deps_open: list[Task] = dependencies_open or []
    resolved_decisions: list[Decision] = related_decisions or []

    markdown = _render_markdown(
        task,
        feature=feature,
        dependencies_completed=resolved_deps_completed,
        dependencies_open=resolved_deps_open,
        related_decisions=resolved_decisions,
        active_claim=active_claim,
    )
    json_data = _render_json(
        task,
        feature=feature,
        dependencies_completed=resolved_deps_completed,
        dependencies_open=resolved_deps_open,
        related_decisions=resolved_decisions,
        active_claim=active_claim,
    )

    return WorkPacket(
        task_id=task.id,
        markdown=markdown,
        json_data=json_data,
    )
