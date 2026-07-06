"""claim, release, renew, next commands (Phase 4)."""

from __future__ import annotations

from pathlib import Path

import typer

from anvil.cli._helpers import (
    PRD_OPTION,
    _lease_manager_kwargs,
    _load_config_optional,
    _open_backend,
    _reap_stale_claims,
    _require_state_dir,
    _resolve_project_dir,
    _resolve_state_dir,
    canonical_prd_id,
    resolve_actor,
    resolve_prd_id,
)
from anvil.cli._json import JSON_OPTION, dump_model, emit_success, fail

# ---------------------------------------------------------------------------
# claim subcommand
# ---------------------------------------------------------------------------


def claim(
    task_id: str = typer.Argument(..., help="Task ID to claim (e.g. T001)."),  # noqa: B008
    worktree: bool = typer.Option(  # noqa: B008
        False,
        "--worktree",
        help="Also create a git worktree at ../wt-<task_id>/.",
    ),
    force: bool = typer.Option(  # noqa: B008
        False,
        "--force",
        help=(
            "Override file-conflict warnings (overlapping likely_files with "
            "an active claim), override crossPrdGuard: refuse, and silence "
            "dependency/cross-PRD warnings. The claim itself proceeds either "
            "way for the dependency check; --force only silences the noise."
        ),
    ),
    actor: str | None = typer.Option(  # noqa: B008
        None,
        "--actor",
        help="Claim actor; defaults to $USER or 'agent'.",
    ),
    lease_minutes: float | None = typer.Option(  # noqa: B008
        None,
        "--lease",
        help=(
            "Lease duration in minutes for this claim. Overrides "
            "default_lease_minutes from project/global config.yaml "
            "(precedence: this flag > project config > global config > "
            "built-in 60)."
        ),
    ),
    branch: str | None = typer.Option(  # noqa: B008
        None,
        "--branch",
        help=(
            "Attach the claim to an existing or caller-named branch instead "
            "of generating the default agent/<task>-<slug> name. If the branch "
            "exists it is checked out; otherwise it is created. The branch name "
            "is recorded on the claim. Without this flag the default "
            "auto-generated branch is used (behavior unchanged)."
        ),
    ),
    prd: str | None = PRD_OPTION,
    json_output: bool = JSON_OPTION,
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory. Defaults to the current working directory.",
        hidden=True,
    ),
) -> None:
    """Acquire an exclusive lease on TASK_ID and create an agent/<task>-<slug> branch.

    With ``--json`` emits ``{"ok": true, "command": "claim", "data":
    {"claim": {...}, "branch": "...", "worktree": "..." | null,
    "warnings": [...]}}``. File-conflict and missing-task failures yield a
    ``{"ok": false, ...}`` envelope with a non-zero exit; non-fatal
    dependency/cross-PRD/branch/worktree warnings are collected into
    ``warnings`` instead of being printed to stderr.
    """

    from anvil.claims.manager import ClaimError, ClaimManager, ConflictWarning
    from anvil.clock import SystemClock
    from anvil.git_ops.branch import create_branch_for_task, use_named_branch
    from anvil.git_ops.worktree import create_worktree_for_task

    resolved_actor = resolve_actor(actor)
    # Git ops run in the user's PROJECT dir, not the state base dir: in the
    # default workspace layout the base dir is ~/.anvil/workspaces/<key>,
    # which is never a git repo, so branch/worktree creation silently
    # no-oped for every workspace-layout claim (found reproducing the
    # README flow on 0.3.0; same resolver-mismatch class as the 2026-06-22
    # postmortem).
    resolved_cwd = _resolve_project_dir(cwd)
    state_dir = _resolve_state_dir(cwd)
    _require_state_dir(state_dir, command="claim", json_output=json_output)

    # Non-fatal warnings collected for the JSON envelope (dependency, branch,
    # and worktree warnings that go to stderr in human mode).
    warnings: list[str] = []

    # Load the project config once, up front, so the ClaimManager honours
    # default_lease_minutes / default_heartbeat_minutes from config.yaml
    # instead of always falling back to the 60-min ClaimManager default
    # (BUG 2 — the MCP path wired these; the CLI did not). The same loaded
    # config also supplies branch_prefix below.
    #
    # T016/B17 — lease precedence: an explicit ``--lease`` flag wins over the
    # configured (project>global merged) lease, which wins over the built-in
    # 60-min default.
    cfg = _load_config_optional(state_dir)
    lease_kwargs = _lease_manager_kwargs(cfg, lease_override=lease_minutes)

    backend = _open_backend(state_dir)
    try:
        clock = SystemClock()

        # Reap stale claims before doing anything.
        _reap_stale_claims(backend)

        manager = ClaimManager(
            backend, clock, actor=resolved_actor, **lease_kwargs
        )

        # Gate: task must exist.
        task = backend.get_task(task_id)
        if task is None:
            if json_output:
                # backend.close() runs in the finally below as typer.Exit unwinds.
                fail("claim", f"task '{task_id}' not found.", code="not_found")
            typer.echo(f"Error: task '{task_id}' not found.", err=True)
            raise typer.Exit(code=1)

        cross_prd_warning: str | None = None
        # T007: if the caller intentionally scoped the claim loop to a PRD
        # partition (--prd or $ANVIL_PRD, both arriving through PRD_OPTION), do
        # not let a typo'd task id silently drift into another PRD's work. Warn
        # by default; projects can opt into a hard stop with crossPrdGuard:
        # refuse, and --force still means "I know, proceed".
        scoped_prd_id = (
            canonical_prd_id(resolve_prd_id(backend, prd))
            if prd and prd.strip()
            else None
        )
        if scoped_prd_id is not None:
            task_prd = backend.get_prd_for_task(task)
            task_prd_id = (
                task_prd.id
                if task_prd is not None
                else canonical_prd_id(task.prd_id or "default")
            )
            if task_prd_id != scoped_prd_id:
                detail = (
                    f"task '{task_id}' belongs to PRD '{task_prd_id}', "
                    f"not active PRD '{scoped_prd_id}'"
                )
                guard = cfg.cross_prd_guard if cfg is not None else "warn"
                if guard == "refuse" and not force:
                    message = f"{detail}. Pass --force to override."
                    if json_output:
                        fail("claim", message, code="cross_prd_guard")
                    typer.echo(f"Error: {message}", err=True)
                    raise typer.Exit(code=1)
                if not force:
                    cross_prd_warning = (
                        f"{detail}. Claimed anyway; pass --force to silence "
                        "this warning."
                    )

        # Pre-claim conflict check (file overlap + group).  Fetch expected_files
        # from likely_files — the manager uses these for overlap detection.
        expected_files = list(task.likely_files) if task.likely_files else []
        conflicts: list[ConflictWarning] = manager.check_conflicts(task_id, expected_files)
        if conflicts and not force:
            if json_output:
                detail = "; ".join(
                    f"claim {c.other_claim_id} by '{c.other_actor}' "
                    f"overlaps {c.overlapping_files}"
                    for c in conflicts
                )
                fail(
                    "claim",
                    f"task '{task_id}' has file conflicts with active claims: "
                    f"{detail}. Pass --force to override.",
                    code="conflict",
                )
            typer.echo(
                f"Warning: task '{task_id}' has file conflicts with active claims:",
                err=True,
            )
            for c in conflicts:
                typer.echo(
                    f"  Claim {c.other_claim_id} by '{c.other_actor}': "
                    f"overlapping files: {c.overlapping_files}",
                    err=True,
                )
            typer.echo(
                "Pass --force to override and claim anyway.",
                err=True,
            )
            raise typer.Exit(code=1)

        # Dependency check (v1.16.0). Soft gate: warn when one or more of
        # task.dependencies are not yet `done`, but proceed with the claim.
        # The user's stacked-PR workflow (claim T002 while T001 is still
        # in_progress and merge them together) is legitimate; we just want
        # them to KNOW the deps aren't done so the choice is informed.
        # --force silences the warning. Mirrors the conflict-check pattern
        # one above but with warn-only semantics.
        if task.dependencies and not force:
            undone_deps: list[tuple[str, str]] = []  # (dep_id, status)
            for dep_id in task.dependencies:
                dep = backend.get_task(dep_id)
                if dep is None:
                    undone_deps.append((dep_id, "not-found"))
                elif dep.status.value != "done":
                    undone_deps.append((dep_id, dep.status.value))
            if undone_deps:
                if json_output:
                    dep_detail = ", ".join(
                        f"{dep_id} ({dep_status})" for dep_id, dep_status in undone_deps
                    )
                    warnings.append(
                        f"task '{task_id}' has {len(undone_deps)} dependency(ies) "
                        f"not yet done: {dep_detail}."
                    )
                else:
                    typer.echo(
                        f"Warning: task '{task_id}' has "
                        f"{len(undone_deps)} dependency(ies) that are not yet "
                        "`done`. Claiming anyway, but the work may be "
                        "blocked or need rebasing once the deps land:",
                        err=True,
                    )
                    for dep_id, dep_status in undone_deps:
                        typer.echo(
                            f"  - {dep_id} ({dep_status})",
                            err=True,
                        )
                    typer.echo(
                        "Pass --force to silence this warning, OR claim the "
                        "dependencies first, OR plan a stacked-branch workflow.",
                        err=True,
                    )

        # T027: a caller-supplied --branch attaches the claim to an existing or
        # named branch instead of generating agent/<task>-<slug>. We resolve the
        # branch FIRST (git checkout / checkout -b) so the resolved name can be
        # recorded on the claim itself. The default (auto-generated) path is
        # unchanged: the branch is created AFTER the claim and is not stored on
        # the claim row (it is reported in the JSON envelope / human output).
        recorded_branch: str | None = None
        if branch is not None:
            # checkout=not worktree: with --worktree, don't move main's HEAD onto
            # the named branch — the worktree add checks it out (#104, same fix as
            # the auto-generated path below).
            branch_result = use_named_branch(
                branch, cwd=resolved_cwd, checkout=not worktree
            )
            if branch_result.created and branch_result.branch:
                # Record the branch on the claim so state reflects the user's
                # own git workflow (the whole point of T027).
                recorded_branch = branch_result.branch
            elif not branch_result.created:
                # git unavailable / not a repo / invalid name — fall back to
                # recording the requested name on the claim so the intent is
                # preserved even when the working tree is not a git repo. The
                # branch warning is surfaced below exactly like the default path.
                recorded_branch = branch

        try:
            result = manager.claim(
                task_id,
                expected_files=expected_files,
                force=force,
                branch=recorded_branch,
            )
        except ClaimError as exc:
            if json_output:
                fail("claim", str(exc), code="claim_error")
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=1) from exc

        if cross_prd_warning is not None:
            if json_output:
                warnings.append(cross_prd_warning)
            else:
                typer.echo(f"Warning: {cross_prd_warning}", err=True)

        # Git branch creation — non-blocking; warnings go to stderr.
        # v1.15.0: branch_prefix is host-project-configurable so claims
        # respect `feature/` / `fix/` conventions instead of forcing
        # `agent/` everywhere. Reuses the config loaded up front (cfg);
        # falls back to the default prefix when config.yaml is missing or
        # failed to load (cfg is None).
        #
        # T027: when --branch was supplied the branch was already resolved
        # above (branch_result is set); skip auto-generation.
        if branch is None:
            branch_prefix = cfg.branch_prefix if cfg is not None else "agent"

            branch_result = create_branch_for_task(
                task_id,
                task.title,
                cwd=resolved_cwd,
                branch_prefix=branch_prefix,
                # For --worktree, create the branch WITHOUT checking it out in
                # the main repo (#104). `git worktree add` then checks it out in
                # the NEW worktree — a branch already checked out in the main
                # worktree can't be added to another one ("already used by
                # worktree"). The main checkout stays on its original branch.
                checkout=not worktree,
            )
        if branch_result.created and branch_result.reason:
            if json_output:
                warnings.append(f"branch: {branch_result.reason}")
            else:
                typer.echo(f"Warning (branch): {branch_result.reason}", err=True)
        elif not branch_result.created:
            if json_output:
                warnings.append(f"git branch not created — {branch_result.reason}")
            else:
                typer.echo(
                    f"Warning: git branch not created — {branch_result.reason}",
                    err=True,
                )

        # Optional worktree creation.
        worktree_path: str | None = None
        if worktree:
            if branch_result.created and branch_result.branch:
                wt_result = create_worktree_for_task(
                    task_id,
                    branch_result.branch,
                    cwd=resolved_cwd,
                )
                if wt_result.created:
                    worktree_path = wt_result.path
                elif json_output:
                    warnings.append(f"worktree not created — {wt_result.reason}")
                else:
                    typer.echo(
                        f"Warning: worktree not created — {wt_result.reason}",
                        err=True,
                    )
            elif json_output:
                warnings.append("--worktree skipped because no branch was created.")
            else:
                typer.echo(
                    "Warning: --worktree skipped because no branch was created.",
                    err=True,
                )

        claim_obj = result.claim
    finally:
        backend.close()

    # T027: prefer the branch recorded on the claim (set when --branch is
    # supplied) so the reported branch reflects what state actually holds, even
    # when git is unavailable. Falls back to the auto-generated branch from the
    # checkout result for the default path (claim_obj.branch is None there).
    reported_branch = claim_obj.branch or (
        branch_result.branch if branch_result.created else None
    )

    if json_output:
        emit_success(
            "claim",
            {
                "claim": dump_model(claim_obj),
                "branch": reported_branch,
                "worktree": worktree_path,
                "warnings": warnings,
            },
        )
        return

    # Confirmation output.
    typer.echo(f"Claimed task '{task_id}' as '{resolved_actor}'.")
    typer.echo(f"  Claim ID:    {claim_obj.id}")
    typer.echo(f"  Lease until: {claim_obj.lease_expires_at.isoformat()}")
    if reported_branch:
        typer.echo(f"  Branch:      {reported_branch}")
    if worktree_path:
        typer.echo(f"  Worktree:    {worktree_path}")
    typer.echo("")
    typer.echo(
        f"Run `anvil renew {claim_obj.id}` to extend the lease before it expires."
    )


# ---------------------------------------------------------------------------
# release subcommand
# ---------------------------------------------------------------------------


def release(
    claim_id: str = typer.Argument(..., help="Claim ID to release (e.g. C001)."),  # noqa: B008
    force: bool = typer.Option(  # noqa: B008
        False,
        "--force",
        help="Force release even if the claim belongs to another actor.",
    ),
    reason: str | None = typer.Option(  # noqa: B008
        None,
        "--reason",
        help="Human-readable reason for the release.",
    ),
    actor: str | None = typer.Option(  # noqa: B008
        None,
        "--actor",
        help="Actor identity; defaults to $USER or 'agent'.",
    ),
    json_output: bool = JSON_OPTION,
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory. Defaults to the current working directory.",
        hidden=True,
    ),
) -> None:
    """Release a claim by CLAIM_ID, returning the task to 'ready'.

    With ``--json`` emits ``{"ok": true, "command": "release", "data":
    {"claim_id": "...", "released": true, "reason": "..." | null}}``.
    A ClaimError yields a ``{"ok": false, ...}`` envelope with exit 1.
    """

    from anvil.claims.manager import ClaimError, ClaimManager
    from anvil.clock import SystemClock

    resolved_actor = resolve_actor(actor)
    state_dir = _resolve_state_dir(cwd)
    _require_state_dir(state_dir, command="release", json_output=json_output)

    backend = _open_backend(state_dir)
    try:
        clock = SystemClock()
        _reap_stale_claims(backend)

        manager = ClaimManager(backend, clock, actor=resolved_actor)
        try:
            manager.release(claim_id, force=force, reason=reason)
        except ClaimError as exc:
            if json_output:
                fail("release", str(exc), code="claim_error")
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
    finally:
        backend.close()

    if json_output:
        emit_success(
            "release",
            {"claim_id": claim_id, "released": True, "reason": reason},
        )
        return

    typer.echo(f"Released claim '{claim_id}'.")
    if reason:
        typer.echo(f"  Reason: {reason}")


# ---------------------------------------------------------------------------
# renew subcommand
# ---------------------------------------------------------------------------


def renew(
    claim_id: str = typer.Argument(..., help="Claim ID to renew (e.g. C001)."),  # noqa: B008
    actor: str | None = typer.Option(  # noqa: B008
        None,
        "--actor",
        help="Actor identity; defaults to $USER or 'agent'.",
    ),
    lease_minutes: float | None = typer.Option(  # noqa: B008
        None,
        "--lease",
        help=(
            "Lease extension in minutes. Overrides default_lease_minutes "
            "from project/global config.yaml (precedence: this flag > "
            "project config > global config > built-in 60)."
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
    """Extend the lease heartbeat on CLAIM_ID.

    With ``--json`` emits ``{"ok": true, "command": "renew", "data":
    {"claim": {...}, "renewed": bool}}`` carrying the updated Claim and a
    ``renewed`` flag — ``false`` when the heartbeat was a no-op (no progress
    since the last heartbeat, B46 part 2), so the lease was NOT extended.
    A ClaimError yields a ``{"ok": false, ...}`` envelope with exit 1.
    """

    from anvil.claims.manager import ClaimError, ClaimManager
    from anvil.clock import SystemClock

    resolved_actor = resolve_actor(actor)
    state_dir = _resolve_state_dir(cwd)
    _require_state_dir(state_dir, command="renew", json_output=json_output)

    # BUG 2: renew must also honour config.yaml default_lease_minutes —
    # renew() extends the lease by default_lease_minutes, so without this the
    # CLI would always extend by 60 min regardless of config.
    #
    # T016/B17 — same lease precedence as claim: explicit --lease flag wins
    # over the merged project>global config, which wins over the 60-min default.
    cfg = _load_config_optional(state_dir)
    lease_kwargs = _lease_manager_kwargs(cfg, lease_override=lease_minutes)

    backend = _open_backend(state_dir)
    try:
        clock = SystemClock()
        _reap_stale_claims(backend)

        manager = ClaimManager(
            backend, clock, actor=resolved_actor, **lease_kwargs
        )
        before = backend.get_claim(claim_id)
        try:
            updated = manager.renew(claim_id)
        except ClaimError as exc:
            if json_output:
                fail("renew", str(exc), code="claim_error")
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
    finally:
        backend.close()

    # B46 part 2 — renew() is a no-op (lease unchanged) when the claim shows no
    # progress since the last heartbeat. Detect that so we report it honestly
    # instead of announcing a fresh lease that was never granted.
    extended = before is None or updated.lease_expires_at != before.lease_expires_at

    if json_output:
        emit_success("renew", {"claim": dump_model(updated), "renewed": extended})
        return

    if extended:
        typer.echo(f"Renewed claim '{claim_id}'.")
        typer.echo(f"  New lease until: {updated.lease_expires_at.isoformat()}")
        typer.echo(f"  Last heartbeat:  {updated.last_heartbeat_at.isoformat()}")
    else:
        typer.echo(f"Renew declined for '{claim_id}': no progress since last heartbeat.")
        typer.echo(f"  Lease still expires at: {updated.lease_expires_at.isoformat()}")
        typer.echo("  Change a file among the claim's expected files, or release and re-claim.")


# ---------------------------------------------------------------------------
# next subcommand
# ---------------------------------------------------------------------------


def next(  # noqa: A001
    actor: str | None = typer.Option(  # noqa: B008
        None,
        "--actor",
        help="Actor identity; defaults to $USER or 'agent'.",
    ),
    task_type: str | None = typer.Option(  # noqa: B008
        None,
        "--type",
        help="Only recommend tasks of this type "
        "(feature, bugfix, refactor, modify).",
    ),
    max_blast: int | None = typer.Option(  # noqa: B008
        None,
        "--max-blast",
        envvar="ANVIL_MAX_BLAST",
        help="[EXPERIMENTAL] Risk ceiling for a low-risk (e.g. local) runner: "
        "only recommend tasks whose blast_radius is CONFIRMED and <= N. "
        "Unconfirmed/unscored tasks are frontier-only (ineligible) even below "
        "the ceiling, so the filter fails SAFE, not open — the blast/review-risk "
        "heuristics ride on an untrusted filename regex. Risk scores are confirmed "
        "when a task passes `anvil review tasks` (v0.4.0), so a ceiling returns "
        "confirmed within-ceiling ready tasks; a project whose tasks have not been "
        "review-confirmed yields an empty queue.",
    ),
    max_review_risk: int | None = typer.Option(  # noqa: B008
        None,
        "--max-review-risk",
        envvar="ANVIL_MAX_REVIEW_RISK",
        help="[EXPERIMENTAL] Risk ceiling: only recommend tasks whose review_risk "
        "is confirmed and <= M (same safe-by-construction semantics as "
        "--max-blast; confirmed at the `anvil review tasks` gate).",
    ),
    prd: str | None = PRD_OPTION,
    json_output: bool = JSON_OPTION,
    quiet: bool = typer.Option(  # noqa: B008
        False,
        "-q",
        "--quiet",
        help="Print nothing; exit 0 if a task is claimable, 3 if the queue "
        "is empty. Loop seam for jq-less shells.",
    ),
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory. Defaults to the current working directory.",
        hidden=True,
    ),
) -> None:
    """Pick the highest-priority claimable task without claiming it.

    Prints the recommended task ID and title.  Run `anvil claim TASK_ID`
    to acquire the lease after reviewing the recommendation. ``--type`` scopes
    the recommendation to a single task type.

    ``--prd`` (T019) scopes the CANDIDATE pool to one PRD partition while
    coordination still spans ALL PRDs: ``next --prd v0.1`` will skip a v0.1
    task whose conflict_group is held by an active v0.2 claim. Omitting it
    (single-PRD projects) keeps the all-PRDs behaviour unchanged.

    With ``--json`` emits ``{"ok": true, "command": "next", "data":
    {"task": {...} | null}}`` — ``task`` is null when nothing is claimable
    (exit 0, an empty queue is not an error).

    With ``-q``/``--quiet`` prints nothing and uses the exit code as the
    signal: 0 if a task is claimable, 3 if the queue is empty (an empty
    queue is not an error).
    """

    from anvil.claims.manager import ClaimManager
    from anvil.clock import SystemClock

    resolved_actor = resolve_actor(actor)
    state_dir = _resolve_state_dir(cwd)
    _require_state_dir(state_dir, command="next", json_output=json_output)

    backend = _open_backend(state_dir)
    try:
        clock = SystemClock()
        _reap_stale_claims(backend)

        # T019: only narrow the candidate pool when a PRD was explicitly named
        # (flag or $ANVIL_PRD, both surfaced via PRD_OPTION's envvar wiring).
        # An explicit value always wins verbatim through resolve_prd_id; with
        # no selection we pass prd_id=None so a single-PRD project's output
        # stays byte-identical to pre-T019. Collapse the default sentinel ('prd')
        # so `--prd prd` narrows to the stored prd_id='default' partition.
        scoped_prd_id = canonical_prd_id(resolve_prd_id(backend, prd)) if prd else None
        scoped_ready_tasks = (
            backend.list_tasks(
                status="ready", task_type=task_type, prd_id=scoped_prd_id
            )
            if scoped_prd_id is not None
            else []
        )
        scoped_empty_message: str | None = None

        manager = ClaimManager(backend, clock, actor=resolved_actor)
        # B49 — accept-rate governor: gate the pull seam on review-debt + the
        # runner's recent accept-rate, configured from config.yaml (defaults
        # when absent). Composes with the B45 risk-axis ceilings.
        from anvil.claims.metrics import AcceptRateMetrics

        cfg = _load_config_optional(state_dir)
        metrics = AcceptRateMetrics(
            backend,
            clock,
            window_days=cfg.accept_rate_window_days if cfg is not None else 7.0,
            floor=cfg.accept_rate_floor if cfg is not None else 0.80,
            needs_review_cap=cfg.needs_review_cap if cfg is not None else 10,
        )
        task = manager.next_claimable(
            task_type=task_type,
            max_blast=max_blast,
            max_review_risk=max_review_risk,
            metrics=metrics,
            prd_id=scoped_prd_id,
        )
        # B49 observability: distinguish a governed withhold (review queue
        # saturated / runner below the accept-rate floor) from a genuinely empty
        # queue — otherwise an idle fleet is indistinguishable from a done one.
        withheld_reason = (
            metrics.withhold_reason(resolved_actor) if task is None else None
        )
        # B45 ceilings can also empty the result (and currently always do, since
        # confirmation is inert). Distinguish that from a truly empty queue too.
        if (
            task is None
            and withheld_reason is None
            and (max_blast is not None or max_review_risk is not None)
        ):
            withheld_reason = "risk_ceiling"
        if task is None and scoped_prd_id is not None:
            if not scoped_ready_tasks:
                scoped_empty_message = f"No ready tasks in this PRD ({scoped_prd_id})."
                withheld_reason = "no_ready_tasks_in_prd"
            elif withheld_reason is None:
                scoped_empty_message = (
                    f"No claimable tasks in this PRD ({scoped_prd_id})."
                )
                withheld_reason = "no_claimable_tasks_in_prd"
    finally:
        backend.close()

    if quiet:
        # ponytail: the exit code is the loop seam (`while anvil next -q`). A
        # governed withhold is still "no work right now" -> exit 3 (the loop
        # backs off either way); the reason is surfaced in --json / human mode.
        raise typer.Exit(0 if task is not None else 3)

    if json_output:
        data = {
            "task": dump_model(task) if task is not None else None,
            "withheld_reason": withheld_reason,
        }
        if scoped_empty_message is not None:
            data["prd"] = scoped_prd_id
            data["message"] = scoped_empty_message
        emit_success("next", data)
        if scoped_empty_message is not None:
            raise typer.Exit(code=3)
        return

    if task is None:
        if scoped_empty_message is not None:
            typer.echo(scoped_empty_message)
            raise typer.Exit(code=3)
        if withheld_reason == "review_queue_saturated":
            typer.echo(
                "No work offered: the human review queue is saturated "
                "(needs_review at the cap). Clear reviews to resume."
            )
        elif withheld_reason == "actor_below_floor":
            typer.echo(
                f"No work offered: actor '{resolved_actor}' is below the "
                "accept-rate floor. Let current work clear review first."
            )
        elif withheld_reason == "risk_ceiling":
            typer.echo(
                "No work within the requested risk ceiling. NOTE the risk-axis "
                "ceilings are EXPERIMENTAL: with no risk-confirmation source yet, "
                "every task is treated as unconfirmed, so a ceilinged query "
                "returns nothing (see `anvil next --help`)."
            )
        else:
            typer.echo("No claimable tasks available.")
        return

    typer.echo(f"Next recommended task: {task.id}")
    typer.echo(f"  Title:    {task.title}")
    typer.echo(f"  Priority: {task.priority.value}")
    if task.scores.complexity is not None:
        typer.echo(f"  Complexity: {task.scores.complexity}")
    typer.echo("")
    typer.echo(f"Run `anvil claim {task.id}` to acquire the lease.")
