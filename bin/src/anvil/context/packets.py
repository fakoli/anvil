"""Work-packet renderer for anvil.

A work packet is the exact context an agent needs to execute one Task — intent,
acceptance criteria, scope, dependencies, decisions, constraints, verification
commands, and output contract — and nothing else.

Length budget (informational, not enforced):
- Small task (few deps, no decisions): ~800–1 500 chars of markdown.
- Large task (many deps + decisions):  ~4 000–6 000 chars of markdown.

The module is pure: no I/O, no logging, no LLM calls. The CLI (or MCP layer)
is responsible for collecting the inputs and writing the output to
``.anvil/packets/{task_id}.md``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from anvil.config import Config
    from anvil.review.gates import DeferredFinding
    from anvil.state.models import (
        Claim,
        Decision,
        Feature,
        Task,
    )

__all__ = [
    "FAST_LANE_REQUIRED_EVIDENCE_MAX",
    "LIGHTWEIGHT_BLAST_RADIUS_MAX",
    "LIGHTWEIGHT_COMPLEXITY_MAX",
    "WorkPacket",
    "fast_lane_packet",
    "is_lightweight",
    "render_packet",
]


# ---------------------------------------------------------------------------
# Lightweight-variant routing (T015) — score → packet shape.
#
# A task whose six-dimension score puts it at/below these complexity and
# blast-radius ceilings is "small": the agent does not need the full
# update-protocol prose or the status-flow walkthrough to execute it safely.
# These are deliberately conservative built-in ceilings (a 1-2/5 task); T020
# ("right-size process by score") layers config-driven thresholds on top of
# this same predicate. Anything unscored, or above either ceiling, gets the
# full packet — the safe default.
# ---------------------------------------------------------------------------

LIGHTWEIGHT_COMPLEXITY_MAX = 2
LIGHTWEIGHT_BLAST_RADIUS_MAX = 2


# ---------------------------------------------------------------------------
# Fast-lane evidence trimming (T020) — "right-size process by score".
#
# T015 trimmed the *update-protocol prose* of a small, low-blast task. T020
# goes one step further: on the same lightweight (fast-lane) packet it also
# trims the *required-evidence checklist* the agent must satisfy, down to a
# single essential field. A trivial change does not need a multi-item evidence
# ceremony — one verification line is enough — while a higher-blast task keeps
# the FULL required-evidence list. This is the "fewer required evidence fields,
# single-step" half of the acceptance criteria.
#
# Crucially this is a *packet-rendering* trim only: the task's stored
# ``Verification.required_evidence`` is never mutated, and completion still
# records an immutable evidence transition. The fast-lane only changes what the
# agent is *shown to need*, never the audit ledger. The review gate
# (``review.gates.evidence_complete``) still reads the task's full stored list
# — the fast-lane is advisory right-sizing of the packet, not a back-door that
# weakens the evidence record.
# ---------------------------------------------------------------------------

FAST_LANE_REQUIRED_EVIDENCE_MAX = 1


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
                   or writing to ``.anvil/packets/{task_id}.md``.
        json_data: Structured form returned by the MCP ``get_work_packet``
                   tool in Phase 6.  Keys mirror the markdown sections.
        variant:   ``"lightweight"`` for small low-blast tasks (see
                   :func:`is_lightweight`), else ``"full"``. The lightweight
                   variant trims the multi-line update-protocol prose and the
                   status-flow walkthrough to a single submit line; every
                   load-bearing section (goal, acceptance criteria, scope,
                   verification, claim) is always present. ``variant`` is also
                   echoed into ``json_data["variant"]`` for MCP consumers.
    """

    task_id: str
    markdown: str
    json_data: dict[str, Any]
    variant: str = "full"


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


def is_lightweight(
    task: Task,
    *,
    complexity_max: int = LIGHTWEIGHT_COMPLEXITY_MAX,
    blast_radius_max: int = LIGHTWEIGHT_BLAST_RADIUS_MAX,
) -> bool:
    """Return True if *task*'s score routes it to the lightweight packet.

    A task is lightweight when BOTH its complexity and blast_radius scores are
    populated and at/below the respective ceilings. The conjunction is
    deliberate: a 1/5-complexity change that nonetheless touches a 5/5
    blast-radius surface (schema, config, public API) still earns the full
    packet, and a task that has not been scored at all (either dimension
    ``None``) always gets the full packet — we never trim context off a task
    we have not assessed.

    Pure — depends only on ``task.scores``; no I/O, no mutation. ``task_type``
    is intentionally NOT part of the gate: the routing is by *score*, so a
    high-blast ``refactor`` is treated exactly like a high-blast ``feature``.
    The thresholds are parameters so T020 can drive them from config.
    """
    complexity = task.scores.complexity
    blast_radius = task.scores.blast_radius
    if complexity is None or blast_radius is None:
        return False
    return complexity <= complexity_max and blast_radius <= blast_radius_max


def _required_evidence_for(
    task: Task,
    *,
    lightweight: bool,
    fast_lane_required_evidence_max: int = FAST_LANE_REQUIRED_EVIDENCE_MAX,
) -> list[str]:
    """Return the required-evidence list to *render* for *task*.

    The full packet shows every declared item. The fast-lane (lightweight)
    packet trims the list to at most ``fast_lane_required_evidence_max`` items
    (default 1) — the "fewer required evidence fields, single-step" half of
    T020 — preserving declaration order so the first / most essential item is
    the one kept. Pure: depends only on the task's declared list; never mutates
    ``task.verification``. A task that declares <= the ceiling, or that is not
    on the fast-lane, is returned unchanged.
    """
    declared = list(task.verification.required_evidence)
    if not lightweight:
        return declared
    if fast_lane_required_evidence_max < 0:
        return declared
    return declared[:fast_lane_required_evidence_max]


def _deferred_finding_lines(findings: list[DeferredFinding]) -> list[str]:
    """Render the 'Prior unresolved review findings' section (T017).

    Each finding names the originating review, the task it was raised against,
    the decision, the overlapping files (the reason it surfaced here), and the
    reviewer's note. Pure formatting; empty list yields no lines so the section
    header is omitted entirely when there is nothing to surface.
    """
    if not findings:
        return []
    lines: list[str] = [
        "## Prior unresolved review findings (overlapping files)",
        "",
        (
            "A prior task touching one of this task's files was deferred or "
            "failed review. Address or explicitly carry forward these findings:"
        ),
        "",
    ]
    for f in findings:
        overlap = ", ".join(f.overlapping_files) or "—"
        note = f.notes.strip() if f.notes else "(no note recorded)"
        lines.append(
            f"- **{f.review_id}** on {f.task_id} ({f.decision}) — "
            f"overlaps `{overlap}`: {note}"
        )
    lines.append("")
    return lines


def _render_markdown(
    task: Task,
    *,
    feature: Feature | None,
    dependencies_completed: list[Task],
    dependencies_open: list[Task],
    related_decisions: list[Decision],
    active_claim: Claim | None,
    lightweight: bool,
    deferred_findings: list[DeferredFinding] | None = None,
    fast_lane_required_evidence_max: int = FAST_LANE_REQUIRED_EVIDENCE_MAX,
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
    lines.append(f"**Type:** {task.task_type.value}")
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

    # --- Prior unresolved review findings (T017) ---
    lines.extend(_deferred_finding_lines(deferred_findings or []))

    # --- Verification ---
    lines.append("## Verification")
    lines.append("")
    if task.verification.commands:
        lines.append("Commands:")
        for cmd in task.verification.commands:
            lines.append(f"- `{cmd}`")
        lines.append("")
    required_evidence = _required_evidence_for(
        task,
        lightweight=lightweight,
        fast_lane_required_evidence_max=fast_lane_required_evidence_max,
    )
    if required_evidence:
        lines.append("Required evidence:")
        for item in required_evidence:
            lines.append(f"- {item}")
        if lightweight and len(required_evidence) < len(
            task.verification.required_evidence
        ):
            # Make the trim explicit so the agent (and any human reading the
            # packet) knows the checklist was right-sized, not lost.
            lines.append(
                "- _(fast-lane: evidence checklist trimmed to the essential"
                " item; the full task record retains every requirement)_"
            )
        lines.append("")
    # SL-3 / B48 typed proofs. Rendered in FULL (never fast-lane-trimmed): the
    # gate enforces every one, so hiding any would let the agent miss a
    # requirement it is graded on. Each label says what must be *observed*.
    if task.verification.required_proofs:
        lines.append("Required proofs (observed, not asserted):")
        for req in task.verification.required_proofs:
            lines.append(f"- {req.label}")
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
    # The lightweight variant (small, low-blast task) collapses this section to
    # the one line the agent actually needs — the submit command — and drops
    # the heartbeat reminder and the status-flow walkthrough. The full variant
    # keeps all three so a higher-stakes task documents the whole lifecycle.
    lines.append("## Update protocol")
    lines.append("")
    if lightweight:
        lines.append(
            f"- On completion, submit evidence via"
            f" `anvil submit {task.id}"
            f" --commands ... --files-changed ...`"
        )
    else:
        if active_claim is not None:
            lines.append(
                f"- Heartbeat your claim every 5 minutes via"
                f" `anvil renew {active_claim.id}`"
            )
        lines.append(
            f"- On completion, submit evidence via"
            f" `anvil submit {task.id}"
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
    lightweight: bool,
    deferred_findings: list[DeferredFinding] | None = None,
    fast_lane_required_evidence_max: int = FAST_LANE_REQUIRED_EVIDENCE_MAX,
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

    # The lightweight variant carries only the submit command — the same trim
    # the markdown renderer applies — so an MCP consumer sees the identical
    # right-sized protocol. The full variant carries the status flow and (when
    # claimed) the renew command.
    update_protocol: dict[str, str] = {
        "submit_command": (
            f"anvil submit {task.id} --commands ... --files-changed ..."
        ),
    }
    if not lightweight:
        update_protocol["status_flow"] = (
            "claimed → in_progress → needs_review → accepted → done"
        )
        if active_claim is not None:
            update_protocol["renew_command"] = (
                f"anvil renew {active_claim.id}"
            )

    # T020 — the right-sized required-evidence checklist the agent is shown.
    # ``task_data["verification"]["required_evidence"]`` still carries the FULL
    # declared list (the immutable record an MCP consumer can audit against);
    # this top-level key carries only what the fast-lane asks the agent to
    # satisfy, mirroring the markdown trim exactly.
    rendered_required_evidence = _required_evidence_for(
        task,
        lightweight=lightweight,
        fast_lane_required_evidence_max=fast_lane_required_evidence_max,
    )

    # T017 — prior unresolved review findings that touch this task's files.
    deferred_findings_data: list[dict[str, Any]] = [
        {
            "review_id": f.review_id,
            "task_id": f.task_id,
            "decision": f.decision,
            "notes": f.notes,
            "files": list(f.files),
            "overlapping_files": list(f.overlapping_files),
        }
        for f in (deferred_findings or [])
    ]

    return {
        "task_id": task.id,
        "task": task_data,
        "feature": feature_data,
        "dependencies_completed": deps_completed_data,
        "dependencies_open": deps_open_data,
        "decisions": decisions_data,
        "active_claim": claim_data,
        "deferred_findings": deferred_findings_data,
        "required_evidence": rendered_required_evidence,
        "update_protocol": update_protocol,
        "variant": "lightweight" if lightweight else "full",
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
    lightweight: bool | None = None,
    deferred_findings: list[DeferredFinding] | None = None,
    fast_lane_required_evidence_max: int = FAST_LANE_REQUIRED_EVIDENCE_MAX,
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
        lightweight:
            Force the lightweight / full variant. ``None`` (the default) lets
            the renderer decide from the task's score via :func:`is_lightweight`
            — a small, low-blast task gets a trimmed update protocol. Pass a
            bool to override (e.g. a config-driven caller in T020). The header,
            goal, acceptance criteria, scope, decisions, and claim sections are
            identical in both variants; the lightweight variant right-sizes the
            update-protocol prose AND the required-evidence checklist.
        deferred_findings:
            T017 — prior unresolved review findings (``reject`` /
            ``needs_changes``) whose touched files overlap this task's incoming
            files, as produced by
            :func:`anvil.review.gates.deferred_findings_for_files`. When
            non-empty, the packet surfaces a "Prior unresolved review findings"
            section (markdown) and a ``deferred_findings`` array (json) so an
            agent picking up a task that touches a previously-deferred file sees
            the outstanding finding instead of starting blind. Defaults to none
            (no section rendered) — fully back-compatible with existing callers.
        fast_lane_required_evidence_max:
            T020 — the maximum number of ``required_evidence`` items the
            lightweight (fast-lane) packet renders, in declaration order
            (default 1: a single essential field, single-step). Ignored on the
            full packet, which always renders every declared item. Set to a
            negative number to disable evidence trimming entirely (render the
            full list even on the fast-lane). This only changes what the agent
            is *shown*; the task's stored ``Verification.required_evidence`` and
            the eventual completion-evidence record are never altered.

    Returns:
        A :class:`WorkPacket` with ``markdown`` (human/Claude-paste form),
        ``json_data`` (structured form for the MCP layer), and ``variant``
        (``"lightweight"`` or ``"full"``).
    """
    resolved_deps_completed: list[Task] = dependencies_completed or []
    resolved_deps_open: list[Task] = dependencies_open or []
    resolved_decisions: list[Decision] = related_decisions or []
    resolved_findings: list[DeferredFinding] = deferred_findings or []
    resolved_lightweight: bool = (
        is_lightweight(task) if lightweight is None else lightweight
    )

    markdown = _render_markdown(
        task,
        feature=feature,
        dependencies_completed=resolved_deps_completed,
        dependencies_open=resolved_deps_open,
        related_decisions=resolved_decisions,
        active_claim=active_claim,
        lightweight=resolved_lightweight,
        deferred_findings=resolved_findings,
        fast_lane_required_evidence_max=fast_lane_required_evidence_max,
    )
    json_data = _render_json(
        task,
        feature=feature,
        dependencies_completed=resolved_deps_completed,
        dependencies_open=resolved_deps_open,
        related_decisions=resolved_decisions,
        active_claim=active_claim,
        lightweight=resolved_lightweight,
        deferred_findings=resolved_findings,
        fast_lane_required_evidence_max=fast_lane_required_evidence_max,
    )

    return WorkPacket(
        task_id=task.id,
        markdown=markdown,
        json_data=json_data,
        variant="lightweight" if resolved_lightweight else "full",
    )


def fast_lane_packet(
    task: Task,
    config: Config,
    *,
    feature: Feature | None = None,
    dependencies_completed: list[Task] | None = None,
    dependencies_open: list[Task] | None = None,
    related_decisions: list[Decision] | None = None,
    active_claim: Claim | None = None,
    deferred_findings: list[DeferredFinding] | None = None,
) -> WorkPacket:
    """Render a work packet, routing the fast-lane from *config* thresholds.

    This is T020's config-driven entry point: it reads
    ``config.fast_lane_complexity_max`` and ``config.fast_lane_blast_radius_max``
    and uses them as the :func:`is_lightweight` ceilings, so a project can widen
    or narrow the fast-lane in ``config.yaml`` without touching code. A task at
    or below BOTH ceilings (and scored on both dimensions) routes to the minimal
    fast-lane packet — trimmed update protocol and a single-step required-
    evidence checklist; anything above either ceiling, or unscored, gets the
    full packet (the safe default).

    The routing is purely about *packet shape*. The task's stored verification
    and the eventual completion-evidence transition are untouched — a fast-lane
    task still records the same immutable evidence ledger as any other.

    Thin wrapper over :func:`render_packet`; the heavy lifting (and the purity
    guarantees) live there. Kept here so CLI/MCP callers have one obvious
    config-aware seam instead of re-deriving the ``lightweight`` decision at
    every call site.
    """
    lightweight = is_lightweight(
        task,
        complexity_max=config.fast_lane_complexity_max,
        blast_radius_max=config.fast_lane_blast_radius_max,
    )
    return render_packet(
        task,
        feature=feature,
        dependencies_completed=dependencies_completed,
        dependencies_open=dependencies_open,
        related_decisions=related_decisions,
        active_claim=active_claim,
        lightweight=lightweight,
        deferred_findings=deferred_findings,
    )
