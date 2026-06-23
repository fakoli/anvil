"""prd sub-app: prd parse, prd review, prd find-decisions (Phase 3 + v1.14.0)."""

from __future__ import annotations

from pathlib import Path

import typer

from anvil.cli._helpers import (
    _DEFAULT_PRD_IDS,
    _PRD_FILENAME,
    PRD_OPTION,
    _get_project_id,
    _open_backend,
    _require_state_dir,
    _resolve_state_dir,
    canonical_prd_id,
    prd_source_path,
    resolve_prd_id,
)
from anvil.cli._json import JSON_OPTION, emit_success, fail
from anvil.state.models import EventDraft

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
            "Defaults to .anvil/prd.md in the current directory."
        ),
    ),
    prd: str | None = typer.Option(  # noqa: B008
        None,
        "--prd",
        help=(
            "Named PRD to parse (multi-PRD). Reads .anvil/prds/<id>.md and "
            "scopes the parse to that PRD partition. Omit for the default "
            "PRD (.anvil/prd.md). Ignored when --file is given."
        ),
    ),
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory. Defaults to the current working directory.",
        hidden=True,
    ),
) -> None:
    """Parse a PRD and store the result as a prd.parsed event.

    Reads .anvil/prd.md (or --file PATH, or .anvil/prds/<id>.md via --prd),
    calls the template parser, emits a prd.parsed event with the full PRD +
    requirements payload. With --prd the event carries that prd_id so the
    backend writes only that PRD's partition, leaving other PRDs untouched.

    Exits 1 if there are parse errors or the file cannot be read.
    On success, prints a summary of what was parsed.
    """
    from anvil.clock import SystemClock
    from anvil.planning.template import parse_prd

    state_dir = _resolve_state_dir(cwd)
    _require_state_dir(state_dir)

    # The parse-time prd_id controls id shape and the partition the event
    # writes into. ``--prd v0.2`` scopes to a named PRD; the default ('prd'
    # sentinel) keeps bare ids and the default partition, byte-identical to
    # the pre-multi-PRD behaviour. ``--file`` always reads the given path but
    # still honours ``--prd`` for the partition.
    parse_prd_id = prd if prd else "prd"

    if file is not None:
        prd_path = file
    else:
        prd_path = prd_source_path(state_dir, parse_prd_id)
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

    result = parse_prd(markdown, prd_id=parse_prd_id)

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

        # Named PRD: stamp the partition so the backend writes ONLY this PRD's
        # rows (the prd.parsed handler scopes its DELETE/UPSERT by prd_id),
        # leaving other PRDs' requirements untouched. The default PRD omits
        # these keys entirely so the payload stays byte-identical to the
        # pre-multi-PRD event (PrdParsedPayload defaults prd_id='default',
        # is_default=True), preserving replay equivalence.
        #
        # Gate on the RESOLVED parse_prd_id, not the raw ``--prd`` flag: the
        # reserved sentinels ``--prd default`` / ``--prd prd`` are legitimate
        # spellings of the DEFAULT PRD (per ``_DEFAULT_PRD_IDS`` / ``prd_source_path``
        # / ``parse_prd``), so they must take the default (no-stamp) branch.
        # Stamping is_default=False for them would INSERT an ``id='default'``
        # row with is_default=0, breaking the ux_prds_default invariant and
        # making the default PRD invisible to every is_default=1 consumer
        # (get_prd() no-arg, default_prd_id(), planning, claim gating).
        if parse_prd_id not in _DEFAULT_PRD_IDS:
            payload["prd_id"] = result.prd.id
            payload["is_default"] = False
            payload["title"] = result.prd.title
            payload["target_version"] = result.prd.target_version
            payload["target_tag"] = result.prd.target_tag

        draft = EventDraft(
            timestamp=now,
            actor="anvil-cli",
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
    prd: str | None = PRD_OPTION,
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

    ``--prd`` (T019) names which PRD partition to review on a multi-PRD project:
    the status check reads that PRD via ``get_prd`` and the emitted event carries
    its ``prd_id`` so the handler mutates only that PRD's row. Omitting it on a
    single-PRD project keeps the pre-T019 default-PRD behaviour unchanged.
    """
    from anvil.clock import SystemClock

    state_dir = _resolve_state_dir(cwd)
    _require_state_dir(state_dir)

    backend = _open_backend(state_dir)
    try:
        clock = SystemClock()
        now = clock.now()
        project_id = _get_project_id(backend)

        # T019: resolve which PRD this review targets. With no --prd/$ANVIL_PRD
        # the resolver returns the single/default PRD's id, so single-PRD
        # projects keep working unchanged; an explicit value scopes the lookup
        # and the emitted event to that partition. Collapse the default sentinel
        # ('prd') to the stored id ('default') so `--prd prd` finds the default
        # PRD row instead of looking up a nonexistent id='prd'.
        resolved_prd_id = canonical_prd_id(resolve_prd_id(backend, prd))

        prd_model = backend.get_prd(resolved_prd_id)
        if prd_model is None:
            typer.echo(
                "Error: no PRD found in state. Run `anvil prd parse` first.",
                err=True,
            )
            raise typer.Exit(code=1)

        # Stamp prd_id into the event payload ONLY for a named (non-default)
        # PRD. The default PRD omits the key so the payload stays byte-identical
        # to the pre-multi-PRD event (the payload defaults prd_id='default').
        def _scope(payload: dict[str, object]) -> dict[str, object]:
            if prd_model.id not in _DEFAULT_PRD_IDS:
                payload["prd_id"] = prd_model.id
            return payload

        if approve:
            if prd_model.status.value != "reviewed":
                typer.echo(
                    f"Error: PRD must be in 'reviewed' status to approve, "
                    f"got '{prd_model.status.value}'. "
                    "Run `anvil prd review` first.",
                    err=True,
                )
                raise typer.Exit(code=1)

            draft = EventDraft(
                timestamp=now,
                actor="anvil-cli",
                action="prd.approved",
                target_kind="prd",
                target_id=project_id,
                payload_json=_scope({"project_id": project_id, "approver": reviewer}),
            )
            backend.append(draft)
            typer.echo(f"PRD approved by '{reviewer}'.")
        else:
            if prd_model.status.value != "draft":
                typer.echo(
                    f"Error: PRD must be in 'draft' status to review, "
                    f"got '{prd_model.status.value}'. "
                    "Pass --approve to move from reviewed → approved.",
                    err=True,
                )
                raise typer.Exit(code=1)

            draft = EventDraft(
                timestamp=now,
                actor="anvil-cli",
                action="prd.reviewed",
                target_kind="prd",
                target_id=project_id,
                payload_json=_scope(
                    {"project_id": project_id, "reviewer": reviewer, "notes": notes}
                ),
            )
            backend.append(draft)
            typer.echo(f"PRD reviewed by '{reviewer}'.")
            typer.echo("Run `anvil prd review --approve` to approve.")
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
            "Defaults to .anvil/prd.md in the current directory."
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
    from anvil.planning.decisions import (
        DecisionKind,
        find_unresolved_decisions,
    )
    from anvil.planning.template import parse_prd

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


# ---------------------------------------------------------------------------
# prd resolve-decision (T018 — decision back-propagation)
# ---------------------------------------------------------------------------


@prd_app.command("resolve-decision")
def prd_resolve_decision(
    decision_id: str = typer.Argument(  # noqa: B008
        ...,
        metavar="DECISION_ID",
        help=(
            "The decision to resolve, as reported by `prd find-decisions` "
            "(e.g. ND-001, OQ001, MF-T012-AC)."
        ),
    ),
    resolution: str = typer.Option(  # noqa: B008
        ...,
        "--resolution",
        "-r",
        help="The answer to write back into the referenced PRD span.",
    ),
    resolved_by: str = typer.Option(  # noqa: B008
        "human",
        "--by",
        help="Identity recorded as the resolver in the event log.",
    ),
    file: Path | None = typer.Option(  # noqa: B008
        None,
        "--file",
        help=(
            "Path to the PRD markdown file. "
            "Defaults to .anvil/prd.md in the current directory."
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
    """Back-propagate a resolved decision into the PRD and record it (T018).

    Locates DECISION_ID via the same detector `prd find-decisions` uses,
    writes ``--resolution`` into the referenced PRD span *without overwriting
    unrelated content*, saves ``prd.md``, and appends an additive
    ``prd.decision_resolved`` event to the log. Resolving a ``[NEEDS DECISION]``
    marker rewrites the linked requirement inline; an open question moves to a
    ``## Decisions`` section; a missing field is added under its task block.

    The PRD source is edited on disk — re-run ``prd parse`` afterwards to
    refresh state.db. The event is the immutable audit fact that the decision
    was answered.
    """
    from anvil.clock import SystemClock
    from anvil.planning.decisions import (
        ResolutionError,
        apply_decision_to_markdown,
        find_unresolved_decisions,
    )
    from anvil.planning.template import parse_prd
    from anvil.state.transitions import (
        TransitionError,
        prd_decision_resolved,
    )

    cmd = "prd resolve-decision"
    state_dir = _resolve_state_dir(cwd)
    _require_state_dir(state_dir, command=cmd, json_output=json_output)

    prd_path = file if file is not None else state_dir / _PRD_FILENAME
    if not prd_path.exists():
        if json_output:
            fail(
                cmd,
                f"PRD file not found at {prd_path}.",
                code="not_found",
            )
        typer.echo(f"Error: PRD file not found at {prd_path}.", err=True)
        raise typer.Exit(code=1)

    try:
        markdown = prd_path.read_text(encoding="utf-8")
    except OSError as exc:
        if json_output:
            fail(cmd, f"cannot read {prd_path}: {exc}", code="io_error")
        typer.echo(f"Error: cannot read {prd_path}: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    result = parse_prd(markdown, prd_id="prd")
    if result.errors:
        msg = f"PRD parse failed with {len(result.errors)} error(s)."
        if json_output:
            fail(cmd, msg, code="parse_error")
        typer.echo(f"Error: {msg} Fix prd.md and re-run.", err=True)
        raise typer.Exit(code=1)

    # Pull tasks from the backend so MF-* decisions can be located.
    backend = _open_backend(state_dir)
    try:
        backend_tasks = backend.list_tasks()
        tasks_or_none = backend_tasks or None
        decisions = find_unresolved_decisions(
            markdown,
            prd=result.prd,
            tasks=tasks_or_none,
        )

        target = next((d for d in decisions if d.id == decision_id), None)
        if target is None:
            available = ", ".join(d.id for d in decisions) or "(none)"
            msg = (
                f"decision {decision_id!r} not found. "
                f"Run `anvil prd find-decisions`. Available: {available}"
            )
            if json_output:
                fail(cmd, msg, code="not_found")
            typer.echo(f"Error: {msg}", err=True)
            raise typer.Exit(code=1)

        # Validate the recorded transition BEFORE touching the file, so a bad
        # PRD status or empty input fails without a partial write.
        clock = SystemClock()
        now = clock.now()
        project_id = _get_project_id(backend)
        prd_model = backend.get_prd() or result.prd

        try:
            transition_payload = prd_decision_resolved(
                prd_model,
                decision_id=target.id,
                prd_ref=target.prd_ref,
                resolution=resolution,
                resolved_by=resolved_by,
                now=now,
            )
        except TransitionError as exc:
            if json_output:
                fail(cmd, exc.message, code=exc.code)
            typer.echo(f"Error: {exc.message}", err=True)
            raise typer.Exit(code=1) from exc

        # Back-propagate into the markdown (surgical, non-destructive).
        try:
            resolution_result = apply_decision_to_markdown(
                markdown, decision=target, resolution=resolution
            )
        except ResolutionError as exc:
            if json_output:
                fail(cmd, str(exc), code="resolution_failed")
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=1) from exc

        # Write the file first, then record the event. If the write fails we
        # never record a transition that does not match the file.
        try:
            prd_path.write_text(resolution_result.markdown, encoding="utf-8")
        except OSError as exc:
            if json_output:
                fail(cmd, f"cannot write {prd_path}: {exc}", code="io_error")
            typer.echo(f"Error: cannot write {prd_path}: {exc}", err=True)
            raise typer.Exit(code=1) from exc

        payload: dict[str, object] = {
            "project_id": project_id,
            "decision_id": target.id,
            "decision_kind": target.kind.value,
            "prd_ref": transition_payload["prd_ref"],
            "resolution": transition_payload["resolution"],
            "resolved_by": resolved_by,
            "section": resolution_result.section,
            "before": resolution_result.before,
            "after": resolution_result.after,
        }
        draft = EventDraft(
            timestamp=now,
            actor="anvil-cli",
            action="prd.decision_resolved",
            target_kind="prd",
            target_id=project_id,
            payload_json=payload,
        )
        event = backend.append(draft)
    finally:
        backend.close()

    if json_output:
        emit_success(
            cmd,
            {
                "prd_source": str(prd_path),
                "decision_id": target.id,
                "decision_kind": target.kind.value,
                "prd_ref": target.prd_ref,
                "section": resolution_result.section,
                "before": resolution_result.before,
                "after": resolution_result.after,
                "event_id": event.id if event is not None else None,
            },
        )
        return

    typer.echo(f"Resolved {target.id} ({target.kind.value}) in {prd_path}.")
    typer.echo(f"  section:  {resolution_result.section}")
    typer.echo(f"  before:   {_truncate(resolution_result.before)}")
    typer.echo(f"  after:    {_truncate(resolution_result.after)}")
    if event is not None:
        typer.echo(f"  recorded: {event.id} (prd.decision_resolved)")
    typer.echo("Run `anvil prd parse` to refresh state.db.")
