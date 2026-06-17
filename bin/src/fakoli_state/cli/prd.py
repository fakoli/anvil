"""prd sub-app: prd parse, prd review, prd find-decisions (Phase 3 + v1.14.0)."""

from __future__ import annotations

from pathlib import Path

import typer

from fakoli_state.cli._helpers import (
    _PRD_FILENAME,
    _get_project_id,
    _open_backend,
    _require_state_dir,
    _resolve_state_dir,
)
from fakoli_state.cli._json import JSON_OPTION, emit_success, fail
from fakoli_state.state.models import EventDraft

prd_app = typer.Typer(
    name="prd",
    help="PRD lifecycle commands: parse, review, approve.",
    no_args_is_help=True,
)


@prd_app.command("parse")
def prd_parse(
    file: Path | None = typer.Option(  # noqa: B008
        None,
        "--file",
        help=(
            "Path to the PRD markdown file. "
            "Defaults to .fakoli-state/prd.md in the current directory."
        ),
    ),
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory. Defaults to the current working directory.",
        hidden=True,
    ),
) -> None:
    """Parse prd.md and store the result as a prd.parsed event.

    Reads .fakoli-state/prd.md (or --file PATH), calls the template parser,
    emits a prd.parsed event with the full PRD + requirements payload.

    Exits 1 if there are parse errors or the file cannot be read.
    On success, prints a summary of what was parsed.
    """
    from fakoli_state.clock import SystemClock
    from fakoli_state.planning.template import parse_prd

    state_dir = _resolve_state_dir(cwd)
    _require_state_dir(state_dir)

    prd_path = file if file is not None else state_dir / _PRD_FILENAME
    if not prd_path.exists():
        typer.echo(
            f"Error: PRD file not found at {prd_path}. "
            "Author your PRD there or pass --file PATH.",
            err=True,
        )
        raise typer.Exit(code=1)

    try:
        markdown = prd_path.read_text(encoding="utf-8")
    except OSError as exc:
        typer.echo(f"Error: cannot read {prd_path}: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    result = parse_prd(markdown, prd_id="prd")

    if result.errors:
        for err in result.errors:
            typer.echo(
                f"  Parse error [{err.section}:{err.line}]: {err.message}",
                err=True,
            )
        typer.echo(
            f"Error: PRD parse failed with {len(result.errors)} error(s). "
            "Fix the issues above and re-run.",
            err=True,
        )
        raise typer.Exit(code=1)

    backend = _open_backend(state_dir)
    try:
        clock = SystemClock()
        now = clock.now()
        project_id = _get_project_id(backend)

        payload: dict[str, object] = {
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

        draft = EventDraft(
            timestamp=now,
            actor="fakoli-state-cli",
            action="prd.parsed",
            target_kind="prd",
            target_id=project_id,
            payload_json=payload,
        )
        backend.append(draft)
    finally:
        backend.close()

    typer.echo(
        f"Parsed {len(result.requirements)} requirements, "
        f"{len(result.features)} features, "
        f"{len(result.tasks)} tasks."
    )
    typer.echo(f"PRD source: {prd_path}")


@prd_app.command("review")
def prd_review(
    approve: bool = typer.Option(  # noqa: B008
        False,
        "--approve",
        help="Approve the PRD (reviewed → approved). Without this flag: draft → reviewed.",
    ),
    reviewer: str = typer.Option(  # noqa: B008
        "human",
        "--reviewer",
        help="Identity of the reviewer.",
    ),
    notes: str | None = typer.Option(  # noqa: B008
        None,
        "--notes",
        help="Optional review notes.",
    ),
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory. Defaults to the current working directory.",
        hidden=True,
    ),
) -> None:
    """Transition the PRD through the review lifecycle.

    Without --approve: draft → reviewed (emits prd.reviewed event).
    With --approve:    reviewed → approved (emits prd.approved event).
    """
    from fakoli_state.clock import SystemClock

    state_dir = _resolve_state_dir(cwd)
    _require_state_dir(state_dir)

    backend = _open_backend(state_dir)
    try:
        clock = SystemClock()
        now = clock.now()
        project_id = _get_project_id(backend)

        prd = backend.get_prd()
        if prd is None:
            typer.echo(
                "Error: no PRD found in state. Run `fakoli-state prd parse` first.",
                err=True,
            )
            raise typer.Exit(code=1)

        if approve:
            if prd.status.value != "reviewed":
                typer.echo(
                    f"Error: PRD must be in 'reviewed' status to approve, "
                    f"got '{prd.status.value}'. "
                    "Run `fakoli-state prd review` first.",
                    err=True,
                )
                raise typer.Exit(code=1)

            draft = EventDraft(
                timestamp=now,
                actor="fakoli-state-cli",
                action="prd.approved",
                target_kind="prd",
                target_id=project_id,
                payload_json={"project_id": project_id, "approver": reviewer},
            )
            backend.append(draft)
            typer.echo(f"PRD approved by '{reviewer}'.")
        else:
            if prd.status.value != "draft":
                typer.echo(
                    f"Error: PRD must be in 'draft' status to review, "
                    f"got '{prd.status.value}'. "
                    "Pass --approve to move from reviewed → approved.",
                    err=True,
                )
                raise typer.Exit(code=1)

            draft = EventDraft(
                timestamp=now,
                actor="fakoli-state-cli",
                action="prd.reviewed",
                target_kind="prd",
                target_id=project_id,
                payload_json={"project_id": project_id, "reviewer": reviewer, "notes": notes},
            )
            backend.append(draft)
            typer.echo(f"PRD reviewed by '{reviewer}'.")
            typer.echo("Run `fakoli-state prd review --approve` to approve.")
    finally:
        backend.close()


# ---------------------------------------------------------------------------
# prd find-decisions (v1.14.0)
# ---------------------------------------------------------------------------


_CONTEXT_TRUNCATE = 120


def _truncate(text: str, limit: int = _CONTEXT_TRUNCATE) -> str:
    """Trim a context paragraph for terminal display."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


@prd_app.command("find-decisions")
def prd_find_decisions(
    file: Path | None = typer.Option(  # noqa: B008
        None,
        "--file",
        help=(
            "Path to the PRD markdown file. "
            "Defaults to .fakoli-state/prd.md in the current directory."
        ),
    ),
    json_output: bool = JSON_OPTION,
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory. Defaults to the current working directory.",
        hidden=True,
    ),
) -> None:
    """Scan the PRD for items needing a human decision and print them.

    Read-only inspection: walks `[NEEDS DECISION]` markers in the raw
    markdown, items under `## Open Questions`, and tasks with empty
    `acceptance_criteria` / `verification.commands`. Output is grouped by
    kind (needs_decision, open_question, missing_field) with a summary
    line at the bottom.

    Exits 0 whether or not decisions are found — this is a probe, not a
    gate. Parse errors still exit 1 (matching `prd parse`) so the user
    fixes structural problems before they're hidden by missing data.
    """
    from fakoli_state.planning.decisions import (
        DecisionKind,
        find_unresolved_decisions,
    )
    from fakoli_state.planning.template import parse_prd

    state_dir = _resolve_state_dir(cwd)
    _require_state_dir(state_dir, command="prd find-decisions", json_output=json_output)

    prd_path = file if file is not None else state_dir / _PRD_FILENAME
    if not prd_path.exists():
        if json_output:
            fail(
                "prd find-decisions",
                f"PRD file not found at {prd_path}. "
                "Author your PRD there or pass --file PATH.",
                code="not_found",
            )
        typer.echo(
            f"Error: PRD file not found at {prd_path}. "
            "Author your PRD there or pass --file PATH.",
            err=True,
        )
        raise typer.Exit(code=1)

    try:
        markdown = prd_path.read_text(encoding="utf-8")
    except OSError as exc:
        if json_output:
            fail(
                "prd find-decisions",
                f"cannot read {prd_path}: {exc}",
                code="io_error",
            )
        typer.echo(f"Error: cannot read {prd_path}: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    result = parse_prd(markdown, prd_id="prd")

    if result.errors:
        if json_output:
            fail(
                "prd find-decisions",
                f"PRD parse failed with {len(result.errors)} error(s): "
                + "; ".join(
                    f"[{e.section}:{e.line}] {e.message}" for e in result.errors
                ),
                code="parse_error",
            )
        for err in result.errors:
            typer.echo(
                f"  Parse error [{err.section}:{err.line}]: {err.message}",
                err=True,
            )
        typer.echo(
            f"Error: PRD parse failed with {len(result.errors)} error(s). "
            "Fix the issues above and re-run.",
            err=True,
        )
        raise typer.Exit(code=1)

    # Pull tasks from the backend so missing_field detection has data to
    # walk. The backend may be empty (no plan run yet) — pass None in that
    # case so the detector skips the missing_field check rather than
    # synthesising decisions from PRD-tasks that aren't in state yet.
    tasks_or_none = None
    if state_dir.exists():
        backend = _open_backend(state_dir)
        try:
            backend_tasks = backend.list_tasks()
            if backend_tasks:
                tasks_or_none = backend_tasks
        finally:
            backend.close()

    decisions = find_unresolved_decisions(
        markdown,
        prd=result.prd,
        tasks=tasks_or_none,
    )

    if json_output:
        import dataclasses
        from typing import Any

        decisions_data: list[dict[str, Any]] = []
        for d in decisions:
            item = dataclasses.asdict(d)
            # ``kind`` is a DecisionKind (StrEnum) — coerce to its value so the
            # envelope carries the plain string, not the enum repr.
            item["kind"] = d.kind.value
            decisions_data.append(item)
        counts = {
            "needs_decision": sum(
                1 for d in decisions if d.kind.value == "needs_decision"
            ),
            "open_question": sum(
                1 for d in decisions if d.kind.value == "open_question"
            ),
            "missing_field": sum(
                1 for d in decisions if d.kind.value == "missing_field"
            ),
        }
        emit_success(
            "prd find-decisions",
            {
                "prd_source": str(prd_path),
                "decisions": decisions_data,
                "count": len(decisions),
                "counts_by_kind": counts,
            },
        )
        return

    # Group by kind, preserving the canonical order needs_decision →
    # open_question → missing_field. The detector already returns items in
    # that order so we can partition cheaply.
    by_kind: dict[DecisionKind, list] = {
        DecisionKind.needs_decision: [],
        DecisionKind.open_question: [],
        DecisionKind.missing_field: [],
    }
    for d in decisions:
        by_kind[d.kind].append(d)

    _KIND_HEADERS = {
        DecisionKind.needs_decision: "NEEDS DECISION markers",
        DecisionKind.open_question: "Open Questions",
        DecisionKind.missing_field: "Missing fields",
    }

    typer.echo(f"PRD source: {prd_path}")

    for kind in (
        DecisionKind.needs_decision,
        DecisionKind.open_question,
        DecisionKind.missing_field,
    ):
        items = by_kind[kind]
        if not items:
            continue
        typer.echo("")
        typer.echo(f"== {_KIND_HEADERS[kind]} ({len(items)}) ==")
        for d in items:
            typer.echo("")
            typer.echo(f"  [{d.id}] {d.kind.value}")
            typer.echo(f"    location: {d.location}")
            typer.echo(f"    text:     {d.text}")
            if d.context_paragraph:
                typer.echo(f"    context:  {_truncate(d.context_paragraph)}")
            typer.echo(f"    resolve:  {d.suggested_resolution_field}")

    typer.echo("")
    typer.echo(
        f"{len(decisions)} total: "
        f"{len(by_kind[DecisionKind.needs_decision])} NEEDS_DECISION, "
        f"{len(by_kind[DecisionKind.open_question])} open questions, "
        f"{len(by_kind[DecisionKind.missing_field])} missing fields."
    )
