"""packet, submit, apply commands (Phase 5)."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from anvil.cli._helpers import (
    PRD_OPTION,
    _load_config_optional,
    _open_backend,
    _reap_stale_claims,
    _require_state_dir,
    _resolve_state_dir,
    canonical_prd_id,
    resolve_actor,
    resolve_prd_id,
)
from anvil.cli._json import JSON_OPTION, dump_model, emit_success, fail
from anvil.naming import safe_path_component
from anvil.state.models import CommandProof, EventDraft


def _read_command_proofs(state_dir: Path, claim_id: str) -> list[CommandProof]:
    """Reconcile the per-claim evidence buffer into typed CommandProofs.

    The capture-evidence hook writes one JSONL record per bash command to
    ``.anvil/.evidence-buffer/<claim-id>.json``. Each well-formed record
    (carrying ``command`` + ``exit_code`` + ``output_sha256``) becomes a
    :class:`CommandProof`. ``output_sha256`` is carried through as-is, NOT
    re-verified here — the proof is only as trustworthy as the hook that wrote
    the buffer (see the TRUST BOUNDARY note on the proof models; hardening
    tracked in docs/tech-debt-backlog.md). Malformed or partial records (e.g. a
    pre-SL-3 hook line with no ``output_sha256``) are skipped, never fatal:
    ``submit`` must still succeed.
    """
    import datetime

    buffer_file = state_dir / ".evidence-buffer" / f"{claim_id}.json"
    if not buffer_file.exists():
        return []
    try:
        lines = buffer_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    proofs: list[CommandProof] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            captured_at = datetime.datetime.fromisoformat(rec["timestamp"])
            if captured_at.tzinfo is None:
                captured_at = captured_at.replace(tzinfo=datetime.UTC)
            proofs.append(
                CommandProof(
                    command=rec["command"],
                    exit_code=int(rec["exit_code"]),
                    output_sha256=rec["output_sha256"],
                    captured_at=captured_at,
                )
            )
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            continue  # skip partial / pre-SL-3 records — never block submit
    return proofs


def emit_acceptance_proof(
    state_dir: Path, backend: object, task_id: str, applied_event: object
) -> Path | None:
    """Build, sign, and persist a portable ``AcceptanceProof`` for an accepted task.

    Emitted on acceptance (B48 part 2): a signed receipt binding the task +
    claim/lease + actor + observed ``CommandProof``s + the event-log range,
    written to ``<state_dir>/proofs/<task_id>-<event_id>.json`` and verifiable
    off-host with only the signer's public key (see ``anvil proof verify``).

    File-only by design: the proof is a portable export, not replayable engine
    state, so it never affects replay determinism. Returns the written path, or
    ``None`` — this NEVER raises: a signing hiccup must not block acceptance.
    """
    try:
        from anvil import signing
        from anvil.state.models import AcceptanceProof, CommandProof, EventRange

        evidence = backend.get_latest_evidence(task_id)  # type: ignore[attr-defined]
        if evidence is None:
            return None  # nothing submitted to attest to
        command_results = [p for p in evidence.proofs if isinstance(p, CommandProof)]
        start_id = backend.first_event_id(task_id) or applied_event.id  # type: ignore[attr-defined]
        project = backend.get_project()  # type: ignore[attr-defined]
        project_id = project.id if project is not None else ""
        private_key, public_key_hex, signer_id = signing.load_or_create_signer()
        proof = AcceptanceProof(
            project_id=project_id,
            task_id=task_id,
            claim_id=evidence.claim_id,
            actor=evidence.submitted_by,
            command_results=command_results,
            event_range=EventRange(start=start_id, end=applied_event.id),  # type: ignore[attr-defined]
            created_at=applied_event.timestamp,  # type: ignore[attr-defined]
            signer_id=signer_id,
            public_key=public_key_hex,
        )
        signing.sign_proof(proof, private_key)
        proofs_dir = state_dir / "proofs"
        proofs_dir.mkdir(parents=True, exist_ok=True)
        out = proofs_dir / f"{safe_path_component(task_id)}-{applied_event.id}.json"  # type: ignore[attr-defined]
        out.write_text(proof.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return out
    except Exception:  # noqa: BLE001 — emission is best-effort; never block accept
        import logging

        # Best-effort, but not silent: a swallowed signing/serialization bug
        # would otherwise look identical to "no evidence to attest to". Leave a
        # breadcrumb so an empty proofs/ dir is diagnosable.
        logging.getLogger(__name__).warning(
            "AcceptanceProof emission failed for task %s; task still accepted "
            "but no signed proof was written",
            task_id,
            exc_info=True,
        )
        return None


def _strict_evidence_env() -> bool | None:
    """Parse ``$ANVIL_STRICT_EVIDENCE`` into a tri-state bool (None = unset).

    Truthy: ``1/true/yes/on``; falsy: ``0/false/no/off`` (case-insensitive). An
    unset or unrecognized value returns None so the next precedence tier decides.
    Shared by the CLI and MCP apply paths so they resolve strict identically.
    """
    import os

    raw = os.environ.get("ANVIL_STRICT_EVIDENCE")
    if raw is None:
        return None
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    return None


def _warn_if_env_overrides_strict_config(env: bool, state_dir: Path) -> None:
    """Warn when ``$ANVIL_STRICT_EVIDENCE`` DISABLES strict that ``config.yaml``
    explicitly enabled — the security-reducing override should never be silent.

    Only the *disable* direction warns: enabling strict via the env var (the
    intended fleet default) is silent, so a fleet that exports the var on every
    project does not emit noise. No warning either when there is no config (the
    default is already off, so the env isn't overriding an explicit choice).
    """
    if env is not False:
        return
    config = _load_config_optional(state_dir)
    if config is not None and config.strict_evidence:
        typer.echo(
            "Warning: ANVIL_STRICT_EVIDENCE is disabling strict-evidence that "
            "config.yaml enabled (strict_evidence: true). Unset the env var to "
            "honor the project config.",
            err=True,
        )


def _resolve_strict_evidence(strict_flag: bool | None, state_dir: Path) -> bool:
    """Resolve the effective strict-evidence mode for this invocation.

    T025/B25 + B48 — completion-evidence enforcement precedence:

        --strict/--no-strict flag  >  $ANVIL_STRICT_EVIDENCE  >  config  >  False

    The ``ANVIL_STRICT_EVIDENCE`` env var lets an **autonomous loop / fleet**
    turn strict mode on for every unattended ``apply`` without editing each
    project's config (B48 acceptance 1). An explicit per-call flag still wins.

    Args:
        strict_flag: The tri-state CLI flag value. ``True`` (--strict) and
            ``False`` (--no-strict) are explicit overrides; ``None`` means the
            flag was not passed, so the env (then config, then default) decides.
        state_dir: The ``.anvil/`` directory; its ``config.yaml`` is
            soft-loaded for the ``strict_evidence`` field. A missing/broken
            config falls back to ``False`` (advisory), same as everywhere else.

    Returns:
        True if strict enforcement is in effect, else False (advisory default).
    """
    if strict_flag is not None:
        return strict_flag
    env = _strict_evidence_env()
    if env is not None:
        _warn_if_env_overrides_strict_config(env, state_dir)
        return env
    config = _load_config_optional(state_dir)
    if config is None:
        return False
    return config.strict_evidence


def _merge_check_block(
    task_id: str,
    backend: object,
    state_dir: Path,
    cwd: Path | None,
) -> tuple[str, dict[str, object] | None]:
    """Cheap base-freshness report for the apply gate (retro-opps T007).

    Returns ``(mode, block)`` where mode is the resolved ``merge_check``
    config value (flagless: config > default "advisory") and block is the
    report dict, ``{"skipped": reason}`` when no branch is resolvable, or
    ``None`` when mode is "off" or the probe itself errored. NEVER runs the
    heavy merged-tree checks (that is `anvil merge-check --run-checks`) and
    never raises — an advisory surface must not break `apply`.
    """
    cfg = _load_config_optional(state_dir)
    mode = cfg.merge_check if cfg is not None else "advisory"
    if mode == "off":
        return mode, None
    try:
        from anvil.cli.merge_check import _branch_for_task
        from anvil.git_ops.freshness import check_freshness, resolve_base

        project_root = (cwd or Path.cwd()).resolve()
        branch = _branch_for_task(backend, task_id, project_root)
        if branch is None:
            return mode, {"skipped": "no branch recorded for this task"}
        base = resolve_base(project_root)
        report = check_freshness(branch, cwd=project_root, base=base)
        return mode, {
            "task_id": task_id,
            "branch": branch,
            "base_ref": report.base.ref,
            "remote_checked": report.base.remote_checked,
            "base_reason": report.base.reason,
            "behind_count": report.behind_count,
            "has_conflicts": report.has_conflicts,
            # T005 review guidance: gate on VERIFIABLE staleness only —
            # behind_count None (probe failed) or offline degradation must
            # never read as stale (local-first).
            "stale": bool(report.behind_count) or report.has_conflicts is True,
        }
    except Exception:  # noqa: BLE001 — advisory probe must never break apply
        return mode, None


def _echo_merge_check_block(block: dict[str, object] | None) -> None:
    """Human-mode rendering of the merge-check block. Quiet when clean."""
    if block is None:
        return
    if "skipped" in block:
        return  # nothing useful to say; JSON carries the reason
    if block["stale"]:
        detail = []
        if block["behind_count"]:
            detail.append(
                f"{block['behind_count']} commit(s) behind {block['base_ref']}"
            )
        if block["has_conflicts"] is True:
            detail.append("merged tree has textual conflicts")
        typer.echo(f"Merge check: STALE — {'; '.join(detail)}.")
        typer.echo(
            f"  Run `anvil merge-check {block['task_id']} --run-checks` "
            "after rebasing to re-verify."
        )
    elif not block["remote_checked"]:
        typer.echo(
            f"Merge check: local base '{block['base_ref']}' used "
            f"({block['base_reason']})."
        )


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
    prd: str | None = PRD_OPTION,
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory. Defaults to the current working directory.",
        hidden=True,
    ),
) -> None:
    """Render a work packet for TASK_ID and write it to .anvil/packets/.

    ``--prd`` (T019) asserts the task belongs to the named PRD partition: an
    explicit ``--prd``/``$ANVIL_PRD`` that doesn't match the task's ``prd_id``
    is a not-found error (the ``get_task`` lookup is unscoped because task IDs
    are globally unique). Omitting it keeps the pre-T019 behaviour unchanged.
    """
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

        # T019: when a PRD is explicitly named, assert the task lives in that
        # partition (get_task is unscoped because task IDs are unique). Collapse
        # the default sentinel ('prd') so `--prd prd` matches a task stored with
        # prd_id='default' instead of raising a false mismatch.
        if prd:
            scoped_prd_id = canonical_prd_id(resolve_prd_id(backend, prd))
            if task.prd_id and task.prd_id != scoped_prd_id:
                typer.echo(
                    f"Error: task '{task_id}' belongs to PRD '{task.prd_id}', "
                    f"not '{scoped_prd_id}'.",
                    err=True,
                )
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
        out_path = packets_dir / f"{safe_path_component(task_id)}.json"
        content = json.dumps(work_packet.json_data, indent=2)
    else:
        out_path = packets_dir / f"{safe_path_component(task_id)}.md"
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
    import uuid

    from anvil.clock import SystemClock

    resolved_actor = resolve_actor(actor)
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

        # SL-3 / B48: reconcile the per-claim evidence buffer (real exit codes
        # captured by the PostToolUse hook) into typed CommandProofs — the
        # observed proofs the gate trusts.
        command_proofs = _read_command_proofs(state_dir, task_claim.id)

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
            "proofs": [p.model_dump(mode="json") for p in command_proofs],
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
                proofs=command_proofs,
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

        # retro-opps T007 — cheap base-freshness probe alongside the evidence
        # gate. Computed once for review-only AND --approve; --reject is never
        # affected (rejecting a stale branch is exactly the right move).
        merge_mode, merge_block = (
            _merge_check_block(task_id, backend, state_dir, cwd)
            if not reject
            else ("off", None)
        )

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
                        "merge_check": merge_block,  # T007 — same block as --approve
                        # No decision in review-only mode, so no proof is emitted;
                        # carry the key (null) to keep the apply envelope uniform.
                        "proof_path": None,
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
            _echo_merge_check_block(merge_block)
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
                elif (
                    task.verification.required_evidence
                    or task.verification.required_proofs
                ):
                    # No evidence row at all but the task demands something —
                    # fail closed. Check BOTH surfaces (a planner-created task
                    # declares required_proofs, not required_evidence).
                    gate_passed, gate_missing = (
                        False,
                        list(task.verification.required_evidence)
                        + [r.label for r in task.verification.required_proofs],
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

            # retro-opps T007 — merge_check: strict refuses approval when the
            # branch is VERIFIABLY stale or conflicted, BEFORE the task.applied
            # event. Unverifiable probes (behind_count None) and offline
            # degradation never refuse — local-first. advisory/off never enter.
            if (
                merge_mode == "strict"
                and merge_block is not None
                and merge_block.get("stale")
            ):
                if json_output:
                    from anvil.cli._json import fail_with

                    fail_with(
                        "apply",
                        f"merge check refused approval of task '{task_id}': "
                        f"branch '{merge_block['branch']}' is stale against "
                        f"{merge_block['base_ref']}. Task remains in "
                        "needs_review.",
                        code="base_stale",
                        extra={"merge_check": merge_block},
                    )
                typer.echo(
                    f"Error: merge check REFUSED approval of task '{task_id}': "
                    f"branch '{merge_block['branch']}' is stale against "
                    f"{merge_block['base_ref']}. Task remains in needs_review.",
                    err=True,
                )
                _echo_merge_check_block(merge_block)
                typer.echo(
                    "Rebase/merge the base and re-verify, or set "
                    "merge_check: advisory in config.yaml.",
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
        applied_event = backend.append(draft)

        # B48 part 2: on acceptance, emit a portable signed AcceptanceProof.
        # Best-effort and file-only (never blocks the accept, never touches
        # replayable state).
        proof_path: Path | None = None
        if approve and applied_event is not None:
            proof_path = emit_acceptance_proof(
                state_dir, backend, task_id, applied_event
            )

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
                "proof_path": str(proof_path) if proof_path is not None else None,
                "merge_check": merge_block,  # T007 — null when mode off/probe failed
            },
        )
        return

    if approve:
        _echo_merge_check_block(merge_block)
        typer.echo(f"Task '{task_id}' approved by '{resolved_reviewer}' → done.")
        if proof_path is not None:
            typer.echo(f"  Signed proof: {proof_path}")
    else:
        typer.echo(
            f"Task '{task_id}' rejected by '{resolved_reviewer}' → drafted "
            "(rejection recorded; task returned to 'drafted' for rework)."
        )
        if reason:
            typer.echo(f"  Reason: {reason}")
