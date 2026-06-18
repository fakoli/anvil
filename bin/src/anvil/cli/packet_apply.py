"""packet, submit, apply commands (Phase 5)."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from anvil.cli._helpers import (
    _load_config_optional,
    _open_backend,
    _reap_stale_claims,
    _require_state_dir,
    _resolve_state_dir,
)
from anvil.cli._json import JSON_OPTION, dump_model, emit_success, fail
from anvil.state.models import EventDraft


def _resolve_strict_evidence(strict_flag: bool | None, state_dir: Path) -> bool:
    """Resolve the effective strict-evidence mode for this invocation.

    T025/B25 — completion-evidence enforcement precedence:

        explicit --strict/--no-strict flag  >  config.strict_evidence  >  False

    Args:
        strict_flag: The tri-state CLI flag value. ``True`` (--strict) and
            ``False`` (--no-strict) are explicit overrides; ``None`` means the
            flag was not passed, so the config (then the default) decides.
        state_dir: The ``.anvil/`` directory; its ``config.yaml`` is
            soft-loaded for the ``strict_evidence`` field. A missing/broken
            config falls back to ``False`` (advisory), same as everywhere else.

    Returns:
        True if strict enforcement is in effect, else False (advisory default).
    """
    if strict_flag is not None:
        return strict_flag
    config = _load_config_optional(state_dir)
    if config is None:
        return False
    return config.strict_evidence


def _compute_next_ready(backend: object, actor: str) -> dict[str, object] | None:
    """Return a compact descriptor of the next claimable task, or None (T014).

    Reuses ``ClaimManager.next_ready_excluding_active_files`` so the suggestion
    respects dependencies, active claims, conflict groups AND file-conflict
    exclusions. Shape mirrors the MCP ``next_ready`` field: {id, title,
    priority}. Returns None when no task is claimable.
    """
    from anvil.claims.manager import ClaimManager
    from anvil.clock import SystemClock

    manager = ClaimManager(backend, SystemClock(), actor=actor)  # type: ignore[arg-type]
    task = manager.next_ready_excluding_active_files()
    if task is None:
        return None
    return {
        "id": task.id,
        "title": task.title,
        "priority": task.priority.value,
    }


# ---------------------------------------------------------------------------
# packet subcommand
# ---------------------------------------------------------------------------


def packet(
    task_id: str = typer.Argument(..., help="Task ID to render a work packet for (e.g. T001)."),  # noqa: B008
    fmt: str = typer.Option(  # noqa: B008
        "md",
        "--format",
        "-f",
        help="Output format: md (default) or json.",
    ),
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory. Defaults to the current working directory.",
        hidden=True,
    ),
) -> None:
    """Render a work packet for TASK_ID and write it to .anvil/packets/."""
    from anvil.context.packets import fast_lane_packet, render_packet

    state_dir = _resolve_state_dir(cwd)
    _require_state_dir(state_dir)

    backend = _open_backend(state_dir)
    try:
        _reap_stale_claims(backend)

        # Fetch the task.
        task = backend.get_task(task_id)
        if task is None:
            typer.echo(f"Error: task '{task_id}' not found.", err=True)
            raise typer.Exit(code=1)

        # Fetch the parent feature via the Backend protocol.
        feature = backend.get_feature(task.feature_id)

        # Split dependencies into completed and open.
        from anvil.state.models import Task

        dependencies_completed: list[Task] = []
        dependencies_open: list[Task] = []
        if task.dependencies:
            dep_tasks = [backend.get_task(dep_id) for dep_id in task.dependencies]
            for dep in dep_tasks:
                if dep is None:
                    continue
                if dep.status.value == "done":
                    dependencies_completed.append(dep)
                else:
                    dependencies_open.append(dep)

        # Fetch the active claim for this task, if any.
        active_claim = None
        for claim in backend.list_active_claims():
            if claim.task_id == task_id:
                active_claim = claim
                break

        # T017 — surface prior deferred / failed-review findings whose files
        # overlap this task's files. The concrete claim (if any) carries the
        # agent's declared ``expected_files``; before a claim exists we fall back
        # to the planner-populated ``likely_files`` overlap hint on the task.
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
        # config is available; fall back to the renderer's built-in defaults
        # (via render_packet) when there is no/broken config.yaml.
        cfg = _load_config_optional(state_dir)
        if cfg is not None:
            work_packet = fast_lane_packet(
                task,
                cfg,
                feature=feature,
                dependencies_completed=dependencies_completed,
                dependencies_open=dependencies_open,
                related_decisions=None,  # Phase 6+ wiring
                active_claim=active_claim,
                deferred_findings=deferred,
            )
        else:
            work_packet = render_packet(
                task,
                feature=feature,
                dependencies_completed=dependencies_completed,
                dependencies_open=dependencies_open,
                related_decisions=None,  # Phase 6+ wiring
                active_claim=active_claim,
                deferred_findings=deferred,
            )
    finally:
        backend.close()

    # Determine output path and content.
    packets_dir = state_dir / "packets"
    packets_dir.mkdir(exist_ok=True)

    if fmt == "json":
        out_path = packets_dir / f"{task_id}.json"
        content = json.dumps(work_packet.json_data, indent=2)
    else:
        out_path = packets_dir / f"{task_id}.md"
        content = work_packet.markdown

    out_path.write_text(content, encoding="utf-8")
    typer.echo(f"Wrote packet to {out_path}")
    typer.echo("")
    # Echo the rendered content matching the selected format. Greptile PR #41
    # flagged that we always echoed markdown regardless of --format, so
    # `packet --format json` printed markdown to stdout while writing JSON to
    # the file — confusing for any caller piping the output downstream.
    typer.echo(content)


# ---------------------------------------------------------------------------
# submit subcommand
# ---------------------------------------------------------------------------


def _split_repeatable(values: list[str]) -> list[str]:
    """Normalize a repeatable comma-aware option into a clean list of values.

    The flag is repeatable (``multiple=True`` semantics), so each occurrence is
    one value. When the flag is passed exactly once, its value is split on
    commas to preserve backward compatibility with the legacy comma-joined
    form. When it is passed more than once, each occurrence is kept verbatim so
    that values containing embedded commas survive intact (CL-2).
    """
    if len(values) == 1:
        return [v.strip() for v in values[0].split(",") if v.strip()]
    return [v.strip() for v in values if v.strip()]


def submit(
    task_id: str = typer.Argument(..., help="Task ID to submit evidence for (e.g. T001)."),  # noqa: B008
    commands: list[str] = typer.Option(  # noqa: B008
        ...,
        "--commands",
        help=(
            "Verification command(s) that were run. Repeatable: pass --commands "
            "once per command (one occurrence == one value, so commands with "
            "embedded commas survive intact). A single occurrence is still split "
            "on commas for backward compatibility."
        ),
    ),
    files_changed: list[str] = typer.Option(  # noqa: B008
        ...,
        "--files-changed",
        help=(
            "File path(s) modified. Repeatable: pass --files-changed once per "
            "path (one occurrence == one value, so paths with embedded commas "
            "survive intact). A single occurrence is still split on commas for "
            "backward compatibility."
        ),
    ),
    output_file: Path | None = typer.Option(  # noqa: B008
        None,
        "--output-file",
        help="Path to a file whose content will be used as the output excerpt.",
    ),
    pr_url: str | None = typer.Option(  # noqa: B008
        None,
        "--pr-url",
        help="Pull request URL.",
    ),
    commit_sha: str | None = typer.Option(  # noqa: B008
        None,
        "--commit-sha",
        help="Commit SHA associated with this submission.",
    ),
    screenshots: str | None = typer.Option(  # noqa: B008
        None,
        "--screenshots",
        help=(
            "Comma-separated paths to screenshot files "
            "(for tasks with screenshot evidence requirements)."
        ),
    ),
    known_limitations: str | None = typer.Option(  # noqa: B008
        None,
        "--known-limitations",
        help="Known limitations or caveats.",
    ),
    actor: str | None = typer.Option(  # noqa: B008
        None,
        "--actor",
        help="Actor submitting evidence; defaults to $USER or 'agent'.",
    ),
    json_output: bool = JSON_OPTION,
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory. Defaults to the current working directory.",
        hidden=True,
    ),
) -> None:
    """Record completion evidence for TASK_ID; auto-releases the active claim.

    With ``--json`` emits ``{"ok": true, "command": "submit", "data":
    {"evidence_id": "...", "claim_id": "...", "submitted_by": "...",
    "commands_run": [...], "files_changed": [...], "task": {...},
    "evidence_gate": {"passed": bool, "missing": [...]},
    "next_ready": {"id", "title", "priority"} | null}}``. ``next_ready``
    (T014) names the next claimable task — dependency-, claim-, conflict-group-
    and file-overlap-aware — so the agent can chain straight into the next
    piece of work, or null when none is available. A missing active claim
    yields a ``{"ok": false, ...}`` envelope with exit 1.
    """
    import os
    import uuid

    from anvil.clock import SystemClock

    resolved_actor = actor or os.environ.get("USER") or "agent"
    state_dir = _resolve_state_dir(cwd)
    _require_state_dir(state_dir, command="submit", json_output=json_output)

    backend = _open_backend(state_dir)
    try:
        clock = SystemClock()
        _reap_stale_claims(backend)

        # Locate the active claim for this task.
        active_claims = backend.list_active_claims()
        task_claim = None
        for c in active_claims:
            if c.task_id == task_id:
                task_claim = c
                break

        if task_claim is None:
            if json_output:
                fail(
                    "submit",
                    f"no active claim found for task '{task_id}'. "
                    f"Run `anvil claim {task_id}` first.",
                    code="no_active_claim",
                )
            typer.echo(
                f"Error: no active claim found for task '{task_id}'. "
                f"Run `anvil claim {task_id}` first.",
                err=True,
            )
            raise typer.Exit(code=1)

        # Parse repeatable / comma-separated arguments. --commands and
        # --files-changed are repeatable (one occurrence == one value), so a
        # value containing commas survives intact when the flag is repeated.
        # A single occurrence is still split on commas for backward
        # compatibility with the legacy comma-joined form (CL-2).
        commands_list = _split_repeatable(commands)
        files_list = _split_repeatable(files_changed)
        screenshots_list = (
            [p.strip() for p in screenshots.split(",") if p.strip()]
            if screenshots
            else []
        )

        # Read and truncate output file content if provided.
        output_excerpt: str | None = None
        if output_file is not None:
            try:
                raw = output_file.read_text(encoding="utf-8", errors="replace")
                output_excerpt = raw[:8000]
            except OSError as exc:
                typer.echo(
                    f"Warning: cannot read --output-file {output_file}: {exc}",
                    err=True,
                )

        # Build a unique evidence ID with "EV" prefix (mirrors ClaimManager UUID pattern).
        evidence_id = "EV" + uuid.uuid4().hex[:8].upper()

        now = clock.now()

        payload: dict[str, object] = {
            "task_id": task_id,
            "claim_id": task_claim.id,
            "submitted_by": resolved_actor,
            "evidence_id": evidence_id,
            "commands_run": commands_list,
            "files_changed": files_list,
            "output_excerpt": output_excerpt,
            "pr_url": pr_url,
            "commit_sha": commit_sha,
            "screenshots": screenshots_list,
            "known_limitations": known_limitations,
        }

        draft = EventDraft(
            timestamp=now,
            actor=resolved_actor,
            action="evidence.submitted",
            target_kind="task",
            target_id=task_id,
            payload_json=payload,
        )
        backend.append(draft)

        # Fetch the fresh task state and evidence for gates summary.
        fresh_task = backend.get_task(task_id)

        # T014: compute the next claimable task while the backend is still open
        # (the JSON envelope is emitted after the finally-block closes it). The
        # suggestion is dep/claim/conflict-group/file-overlap aware, and the
        # submitting actor's own (now-released) claim is excluded from the
        # file-conflict check so chained work on the same files stays eligible.
        next_ready = _compute_next_ready(backend, resolved_actor)
    finally:
        backend.close()

    # Compute the evidence gate once — used by both the JSON envelope and the
    # human gate-summary block below. Failures here are non-fatal (the gate is
    # informational); ``gate`` stays None and is reported as null / skipped.
    gate: tuple[bool, list[str]] | None = None
    if fresh_task is not None:
        try:
            from anvil.review.gates import evidence_complete
            from anvil.state.models import Evidence

            evidence_obj = Evidence(
                id=evidence_id,
                task_id=task_id,
                claim_id=task_claim.id,
                commands_run=commands_list,
                output_excerpt=output_excerpt,
                files_changed=files_list,
                pr_url=pr_url,
                commit_sha=commit_sha,
                screenshots=screenshots_list,
                known_limitations=known_limitations,
                submitted_at=now,
                submitted_by=resolved_actor,
            )
            gate = evidence_complete(fresh_task, evidence_obj)
        except Exception:  # noqa: BLE001
            gate = None  # gate summary is informational; never block the command

    if json_output:
        emit_success(
            "submit",
            {
                "evidence_id": evidence_id,
                "claim_id": task_claim.id,
                "submitted_by": resolved_actor,
                "commands_run": commands_list,
                "files_changed": files_list,
                "pr_url": pr_url,
                "commit_sha": commit_sha,
                "task": dump_model(fresh_task) if fresh_task is not None else None,
                "evidence_gate": (
                    {"passed": gate[0], "missing": gate[1]}
                    if gate is not None
                    else None
                ),
                "next_ready": next_ready,
            },
        )
        return

    typer.echo(f"Evidence submitted for task '{task_id}'.")
    typer.echo(f"  Evidence ID:  {evidence_id}")
    typer.echo(f"  Claim ID:     {task_claim.id} (auto-released)")
    typer.echo(f"  Submitted by: {resolved_actor}")
    typer.echo(f"  Commands:     {commands_list}")
    typer.echo(f"  Files:        {files_list}")
    if pr_url:
        typer.echo(f"  PR URL:       {pr_url}")
    if commit_sha:
        typer.echo(f"  Commit SHA:   {commit_sha}")
    typer.echo("")
    typer.echo(f"Task '{task_id}' status → needs_review.")
    typer.echo(f"Run `anvil apply {task_id}` when ready for human review.")

    # Gate summary: reuse the gate computed above.
    if gate is not None:
        passed, missing = gate
        if passed:
            typer.echo("Evidence gate: PASSED — all required evidence present.")
        else:
            typer.echo(
                "Evidence gate: INCOMPLETE — missing items for required_evidence:"
            )
            for item in missing:
                typer.echo(f"  - {item}")


# ---------------------------------------------------------------------------
# apply subcommand
# ---------------------------------------------------------------------------


def apply(
    task_id: str = typer.Argument(..., help="Task ID to apply a review decision to (e.g. T001)."),  # noqa: B008
    approve: bool = typer.Option(  # noqa: B008
        False,
        "--approve",
        help="Approve: transition needs_review → accepted → done.",
    ),
    reject: bool = typer.Option(  # noqa: B008
        False,
        "--reject",
        help="Reject: transition needs_review → rejected.",
    ),
    reason: str | None = typer.Option(  # noqa: B008
        None,
        "--reason",
        help="Review notes; required when using --reject.",
    ),
    reviewer: str | None = typer.Option(  # noqa: B008
        None,
        "--reviewer",
        help="Reviewer identity; defaults to $USER or 'human'.",
    ),
    strict: bool | None = typer.Option(  # noqa: B008
        None,
        "--strict/--no-strict",
        help=(
            "Enforce the completion-evidence gate: refuse --approve when "
            "required evidence is missing (exit 1). Overrides config "
            "strict_evidence for this invocation. Precedence: flag > config "
            "> default (advisory). --reject is never affected."
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
    """Human review gate: transition needs_review → accepted (→ done) or → rejected.

    With ``--json`` emits ``{"ok": true, "command": "apply", "data": {...}}``.
    Both modes return the SAME ``data`` key set so a consumer can read the
    outcome uniformly: ``{task_id, status, decision, reviewer, reason,
    has_evidence, evidence_gate, task, next_ready}``. ``next_ready`` (T014)
    names the next claimable task — dependency-, claim-, conflict-group- and
    file-overlap-aware — or null when none is available. In review-only mode
    (neither
    --approve nor --reject) ``decision``/``reviewer``/``reason`` are null and
    ``status``/``task`` reflect the current needs_review task; with
    --approve/--reject ``decision``/``reviewer``/``reason`` are set and
    ``status``/``task`` reflect the post-apply task. Error paths (task not
    found, wrong status, both flags, --reject without --reason) yield a
    ``{"ok": false, ...}`` envelope with exit 1.

    Completion-evidence enforcement (T025/B25). When strict mode is in effect
    (``--strict`` flag, or ``strict_evidence: true`` in config; precedence:
    flag > config > default(False)), ``--approve`` REFUSES with exit 1 and the
    JSON error code ``evidence_incomplete`` (listing the missing required-
    evidence items) instead of transitioning the task to done. A complete gate,
    or a task that declares no ``required_evidence``, makes strict a no-op and
    apply proceeds normally. ``--reject`` is never affected. The DEFAULT (no
    flag, no config) keeps the historical advisory behaviour byte-for-byte:
    the gate is shown but approval proceeds.
    """
    import os

    state_dir = _resolve_state_dir(cwd)
    _require_state_dir(state_dir, command="apply", json_output=json_output)

    resolved_reviewer = reviewer or os.environ.get("USER") or "human"

    backend = _open_backend(state_dir)
    try:
        _reap_stale_claims(backend)

        task = backend.get_task(task_id)
        if task is None:
            if json_output:
                fail("apply", f"task '{task_id}' not found.", code="not_found")
            typer.echo(f"Error: task '{task_id}' not found.", err=True)
            raise typer.Exit(code=1)

        if task.status.value != "needs_review":
            if json_output:
                fail(
                    "apply",
                    f"task '{task_id}' has status '{task.status.value}', "
                    "expected 'needs_review'. "
                    "Run `anvil submit` first to record completion evidence.",
                    code="invalid_status",
                )
            typer.echo(
                f"Error: task '{task_id}' has status '{task.status.value}', "
                "expected 'needs_review'. "
                "Run `anvil submit` first to record completion evidence.",
                err=True,
            )
            raise typer.Exit(code=1)

        # Review-only mode: neither --approve nor --reject; show evidence summary.
        if not approve and not reject:
            # Fetch the latest evidence row for this task via the Backend protocol.
            evidence_obj = backend.get_latest_evidence(task_id)
            gate: tuple[bool, list[str]] | None = None
            if evidence_obj is not None:
                try:
                    from anvil.review.gates import evidence_complete

                    gate = evidence_complete(task, evidence_obj)
                except Exception:  # noqa: BLE001
                    gate = None

            if json_output:
                # T014: keep the unified key set — review-only mode reports the
                # same next_ready field as decision mode (state is unchanged, so
                # this is a read-only snapshot of the current next claimable task).
                review_only_next_ready = _compute_next_ready(
                    backend, resolved_reviewer
                )
                emit_success(
                    "apply",
                    {
                        "task_id": task_id,
                        "status": task.status.value,
                        "decision": None,
                        "reviewer": None,
                        "reason": None,
                        "has_evidence": evidence_obj is not None,
                        "next_ready": review_only_next_ready,
                        "evidence_gate": (
                            {"passed": gate[0], "missing": gate[1]}
                            if gate is not None
                            else None
                        ),
                        "task": dump_model(task),
                    },
                )
                return

            if evidence_obj is not None and gate is not None:
                passed, missing = gate
                typer.echo(f"Task '{task_id}' awaiting review (status: needs_review).")
                typer.echo("")
                if passed:
                    typer.echo("Evidence gate: PASSED — all required evidence present.")
                else:
                    typer.echo(
                        "Evidence gate: INCOMPLETE — missing items for required_evidence:"
                    )
                    for item in missing:
                        typer.echo(f"  - {item}")
            elif evidence_obj is not None:
                typer.echo(f"Task '{task_id}' awaiting review (status: needs_review).")
            else:
                typer.echo(f"Task '{task_id}' awaiting review (status: needs_review).")
                typer.echo("No evidence found — run `anvil submit` first.")
            typer.echo("")
            typer.echo(
                "Pass --approve to accept or --reject --reason TEXT to reject."
            )
            raise typer.Exit(code=0)

        # Mutual exclusion guard.
        if approve and reject:
            if json_output:
                fail(
                    "apply",
                    "pass either --approve or --reject, not both.",
                    code="bad_request",
                )
            typer.echo(
                "Error: pass either --approve or --reject, not both.",
                err=True,
            )
            raise typer.Exit(code=1)

        # --reject requires --reason.
        if reject and not reason:
            if json_output:
                fail(
                    "apply",
                    "--reject requires --reason TEXT.",
                    code="bad_request",
                )
            typer.echo(
                "Error: --reject requires --reason TEXT.",
                err=True,
            )
            raise typer.Exit(code=1)

        # T025/B25 — completion-evidence ENFORCEMENT. Only --approve is gated;
        # --reject is never blocked (rejecting a task with missing evidence is
        # exactly the right move). When strict is in effect and the gate is
        # INCOMPLETE, refuse the approval BEFORE appending the task.applied
        # event, so the task stays in needs_review. A complete gate, or a task
        # with no required_evidence, makes this a no-op (strict_passed stays
        # True). The DEFAULT (flag absent, config absent/False) never enters the
        # refuse branch, so advisory behaviour is preserved byte-for-byte.
        if approve:
            strict_active = _resolve_strict_evidence(strict, state_dir)
            if strict_active:
                from anvil.review.gates import evidence_complete

                strict_evidence_obj = backend.get_latest_evidence(task_id)
                # No evidence at all when something is required is itself a
                # failure; gate against an evidence-less view by reusing the
                # gate with whatever the backend has. If required_evidence is
                # empty the gate passes (strict is a no-op).
                if strict_evidence_obj is not None:
                    gate_passed, gate_missing = evidence_complete(
                        task, strict_evidence_obj
                    )
                elif task.verification.required_evidence:
                    gate_passed, gate_missing = (
                        False,
                        list(task.verification.required_evidence),
                    )
                else:
                    gate_passed, gate_missing = True, []

                if not gate_passed:
                    if json_output:
                        # Error envelope with the stable code "evidence_incomplete"
                        # plus a structured "missing" list naming the unsatisfied
                        # required-evidence items. fail_with() prints to stdout
                        # and raises Exit(1).
                        from anvil.cli._json import fail_with

                        fail_with(
                            "apply",
                            f"strict evidence gate refused approval of task "
                            f"'{task_id}': required evidence is missing "
                            f"({', '.join(gate_missing)}). Task remains in "
                            "needs_review.",
                            code="evidence_incomplete",
                            extra={"missing": gate_missing},
                        )
                    typer.echo(
                        f"Error: strict evidence gate REFUSED approval of "
                        f"task '{task_id}': required evidence is missing. "
                        f"Task remains in needs_review.",
                        err=True,
                    )
                    for item in gate_missing:
                        typer.echo(f"  - {item}", err=True)
                    typer.echo(
                        "Submit the missing evidence and re-run, or use "
                        "--no-strict to override for this invocation.",
                        err=True,
                    )
                    raise typer.Exit(code=1)

        from anvil.clock import SystemClock

        clock = SystemClock()
        now = clock.now()

        if approve:
            decision = "accepted"
        else:
            decision = "rejected"

        payload: dict[str, object] = {
            "task_id": task_id,
            "reviewer": resolved_reviewer,
            "decision": decision,
            "notes": reason,
        }

        draft = EventDraft(
            timestamp=now,
            actor=resolved_reviewer,
            action="task.applied",
            target_kind="task",
            target_id=task_id,
            payload_json=payload,
        )
        backend.append(draft)

        # Re-fetch the post-transition task so the JSON envelope reports the
        # resulting status (done on approve; drafted on reject).
        result_task = backend.get_task(task_id)

        # Fetch the latest evidence + gate so the decision-mode JSON envelope
        # carries the SAME key set as review-only mode (has_evidence /
        # evidence_gate). Gate is evaluated against the pre-transition task
        # (which was needs_review and still holds the required_evidence list).
        decision_evidence = backend.get_latest_evidence(task_id)
        decision_gate: tuple[bool, list[str]] | None = None
        if decision_evidence is not None:
            try:
                from anvil.review.gates import evidence_complete

                decision_gate = evidence_complete(task, decision_evidence)
            except Exception:  # noqa: BLE001
                decision_gate = None

        # T014: name the next claimable task after this disposition (approving a
        # task to done can unblock dependents). Computed while the backend is
        # open; the JSON envelope below is emitted after the finally closes it.
        apply_next_ready = _compute_next_ready(backend, resolved_reviewer)
    finally:
        backend.close()

    if json_output:
        emit_success(
            "apply",
            {
                "task_id": task_id,
                "status": (
                    result_task.status.value if result_task is not None else None
                ),
                "decision": decision,
                "reviewer": resolved_reviewer,
                "reason": reason,
                "has_evidence": decision_evidence is not None,
                "evidence_gate": (
                    {"passed": decision_gate[0], "missing": decision_gate[1]}
                    if decision_gate is not None
                    else None
                ),
                "task": dump_model(result_task) if result_task is not None else None,
                "next_ready": apply_next_ready,
            },
        )
        return

    if approve:
        typer.echo(f"Task '{task_id}' approved by '{resolved_reviewer}' → done.")
    else:
        typer.echo(
            f"Task '{task_id}' rejected by '{resolved_reviewer}' → drafted "
            "(rejection recorded; task returned to 'drafted' for rework)."
        )
        if reason:
            typer.echo(f"  Reason: {reason}")
