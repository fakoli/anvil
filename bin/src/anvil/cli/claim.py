"""claim, release, renew, next commands (Phase 4)."""

from __future__ import annotations

import builtins
from pathlib import Path
from typing import Any

import typer

from anvil.cli._actor_output import (
    actor_flag_for_human,
    actor_identity_data,
    actor_mismatch_data,
    actor_mismatch_message,
    actor_notice_lines,
    bundle_continuation_data,
    bundle_continuation_lines,
    continuation_data,
    hook_env_for_human,
    safe_actor_label,
)
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
from anvil.cli._json import JSON_OPTION, dump_model, emit_success, fail, fail_with


def _refuse_actor_mismatch(
    command: str,
    *,
    owner: str,
    actual: str,
    action: str,
    json_output: bool,
) -> None:
    message = actor_mismatch_message(owner=owner, actual=actual, action=action)
    if json_output:
        fail_with(
            command,
            message,
            code="actor_mismatch",
            extra=actor_mismatch_data(owner=owner, actual=actual),
        )
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(code=1)

# ---------------------------------------------------------------------------
# claim subcommand
# ---------------------------------------------------------------------------


def claim(
    task_id: str = typer.Argument(..., help="Task ID to claim (e.g. T001)."),  # noqa: B008
    bundle_mode: bool = typer.Option(  # noqa: B008
        False,
        "--bundle",
        help="Claim TASK_ID as an execution bundle using one coordinator lease.",
    ),
    worktree: bool = typer.Option(  # noqa: B008
        False,
        "--worktree",
        help="Also create a git worktree at ../wt-<task_id>/.",
    ),
    shared_tree: bool = typer.Option(  # noqa: B008
        False,
        "--shared-tree",
        help=(
            "Explicitly claim into the shared checkout even when "
            "worktree_isolation: require is configured (for read-only / "
            "docs-only work that cannot conflict). Also silences the "
            "advisory shared-checkout warning."
        ),
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
        help=(
            "Claim actor. Precedence: --actor > ANVIL_ACTOR > "
            "ANVIL_GATE_ACTOR > derived local identity."
        ),
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
    from anvil.git_ops import (
        ClaimGitMutationTracker,
        ClaimPlanError,
        apply_claim_plan,
        claim_git_metadata,
        compensate_claim_plan_tracker,
        finalize_claim_plan_tracker,
        resolve_claim_plan,
        revalidate_claim_plan,
    )

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
            backend,
            clock,
            actor=resolved_actor,
            project_root=resolved_cwd,
            **lease_kwargs,
        )

        if bundle_mode:
            from anvil.bundles.manager import (
                BundleActorMismatch,
                BundleError,
                BundleManager,
            )

            execution_bundle = backend.get_bundle(task_id)
            if execution_bundle is None:
                if json_output:
                    fail("claim", f"bundle '{task_id}' not found.", code="not_found")
                typer.echo(f"Error: bundle '{task_id}' not found.", err=True)
                raise typer.Exit(code=1)
            bundle_manager = BundleManager(
                backend,
                clock,
                actor=resolved_actor,
                project_root=resolved_cwd,
                lease_minutes=lease_kwargs.get("default_lease_minutes", 240),
            )
            try:
                bundle_manager.preflight(task_id)
            except BundleActorMismatch as exc:
                if json_output:
                    fail_with(
                        "claim",
                        str(exc),
                        code="actor_mismatch",
                        extra={"owner": exc.owner, "resolved_actor": exc.actual},
                    )
                typer.echo(f"Error: {exc}", err=True)
                raise typer.Exit(code=1) from exc
            except BundleError as exc:
                if json_output:
                    fail("claim", str(exc), code="bundle_claim_error")
                typer.echo(f"Error: {exc}", err=True)
                raise typer.Exit(code=1) from exc
            branch_prefix = cfg.branch_prefix if cfg is not None else "agent"
            isolation = cfg.worktree_isolation if cfg is not None else "advisory"
            if isolation == "require" and not shared_tree:
                worktree = True
            try:
                plan = resolve_claim_plan(
                    task_id,
                    f"Bundle {task_id}",
                    cwd=resolved_cwd,
                    branch=branch,
                    branch_prefix=branch_prefix,
                    worktree=worktree,
                    isolation_required=isolation == "require" and not shared_tree,
                    shared_tree=shared_tree,
                    ignored_worktree_paths=(state_dir,),
                )
                metadata = claim_git_metadata(plan)
                mutation_tracker = ClaimGitMutationTracker(plan)
            except ClaimPlanError as exc:
                if json_output:
                    fail("claim", str(exc), code=exc.code)
                typer.echo(f"Error: {exc}", err=True)
                raise typer.Exit(code=1) from exc
            try:
                with backend.claim_operation_lock():
                    revalidate_claim_plan(plan, cwd=resolved_cwd)
                    bundle_result = bundle_manager.claim(
                        task_id,
                        branch=metadata.branch if metadata is not None else branch,
                        worktree_path=(
                            metadata.worktree_path if metadata is not None else None
                        ),
                        git_metadata=metadata,
                    )
                    try:
                        apply_claim_plan(
                            plan, cwd=resolved_cwd, tracker=mutation_tracker
                        )
                    except BaseException:
                        try:
                            bundle_manager.release(
                                task_id,
                                reason="transactional Git claim failed",
                            )
                        finally:
                            compensate_claim_plan_tracker(
                                mutation_tracker, cwd=resolved_cwd
                            )
                        raise
                    finalize_claim_plan_tracker(
                        mutation_tracker, cwd=resolved_cwd
                    )
            except ClaimPlanError as exc:
                if json_output:
                    fail("claim", str(exc), code=exc.code)
                typer.echo(f"Error: {exc}", err=True)
                raise typer.Exit(code=1) from exc
            except BundleError as exc:
                if json_output:
                    fail("claim", str(exc), code="bundle_claim_error")
                typer.echo(f"Error: {exc}", err=True)
                raise typer.Exit(code=1) from exc
            if json_output:
                emit_success(
                    "claim",
                    {
                        "bundle": dump_model(bundle_result.bundle),
                        "claim": dump_model(bundle_result.claim),
                        "branch": bundle_result.claim.branch,
                        "worktree": bundle_result.claim.worktree_path,
                        "warnings": warnings,
                        "actor_identity": actor_identity_data(
                            bundle_result.claim.claimed_by
                        ),
                        "continuation": bundle_continuation_data(
                            task_id, bundle_result.claim.claimed_by
                        ),
                    },
                )
                return
            typer.echo(
                f"Claimed bundle '{task_id}' as "
                f"{safe_actor_label(bundle_result.claim.claimed_by)} with "
                f"coordinator claim {bundle_result.claim.id}."
            )
            typer.echo(f"  Branch: {bundle_result.claim.branch or '-'}")
            typer.echo(f"  Worktree: {bundle_result.claim.worktree_path or '-'}")
            for line in bundle_continuation_lines(
                task_id, bundle_result.claim.claimed_by
            ):
                typer.echo(line)
            return

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
                    f"  Claim {c.other_claim_id} by {safe_actor_label(c.other_actor)}: "
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

        # Freeze every Git observation before reserving state. The same plan is
        # revalidated under the cross-process claim lock immediately before the
        # claim event and again before Git mutation.
        isolation = cfg.worktree_isolation if cfg is not None else "advisory"
        if not shared_tree and not worktree:
            if isolation == "require":
                worktree = True
                note = (
                    "worktree_isolation: require — claiming into an isolated "
                    "worktree (pass --shared-tree for read-only/shared work)."
                )
                if json_output:
                    warnings.append(note)
                else:
                    typer.echo(note, err=True)
            elif isolation == "advisory":
                shared_active = [
                    c for c in backend.list_active_claims()
                    if not c.worktree_path
                ]
                if shared_active:
                    others = ", ".join(
                        f"{c.task_id} ({safe_actor_label(c.claimed_by)})"
                        for c in shared_active[:4]
                    )
                    note = (
                        f"{len(shared_active)} other active claim(s) share this "
                        f"checkout ({others}) — concurrent edits can collide. "
                        "Use --worktree, or set worktree_isolation: require to "
                        "isolate by default."
                    )
                    if json_output:
                        warnings.append(note)
                    else:
                        typer.echo(f"Warning: {note}", err=True)
        try:
            branch_prefix = cfg.branch_prefix if cfg is not None else "agent"
            plan = resolve_claim_plan(
                task_id,
                task.title,
                cwd=resolved_cwd,
                branch=branch,
                branch_prefix=branch_prefix,
                worktree=worktree,
                isolation_required=isolation == "require" and not shared_tree,
                shared_tree=shared_tree,
                ignored_worktree_paths=(state_dir,),
            )
            metadata = claim_git_metadata(plan)
            mutation_tracker = ClaimGitMutationTracker(plan)
        except ClaimPlanError as exc:
            if json_output:
                fail("claim", str(exc), code=exc.code)
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=1) from exc

        try:
            with backend.claim_operation_lock():
                revalidate_claim_plan(plan, cwd=resolved_cwd)
                result = manager.claim(
                    task_id,
                    expected_files=expected_files,
                    force=force,
                    branch=metadata.branch if metadata is not None else branch,
                    worktree_path=(
                        metadata.worktree_path if metadata is not None else None
                    ),
                    git_metadata=metadata,
                    operation_locked=True,
                )
                try:
                    apply_claim_plan(plan, cwd=resolved_cwd, tracker=mutation_tracker)
                except BaseException:
                    try:
                        manager.release(
                            result.claim.id,
                            reason="transactional Git claim failed",
                        )
                    finally:
                        compensate_claim_plan_tracker(
                            mutation_tracker, cwd=resolved_cwd
                        )
                    raise
                finalize_claim_plan_tracker(
                    mutation_tracker, cwd=resolved_cwd
                )
        except ClaimPlanError as exc:
            if json_output:
                fail("claim", str(exc), code=exc.code)
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
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
        claim_obj = result.claim
    finally:
        backend.close()

    reported_branch = claim_obj.branch
    worktree_path = claim_obj.worktree_path
    if claim_obj.attestation_context is None:
        warnings.append(
            "Progress attestation unavailable: this project is not an accessible "
            "Git repository; legacy file-change renewal remains available."
        )

    if json_output:
        emit_success(
            "claim",
            {
                "claim": dump_model(claim_obj),
                "branch": reported_branch,
                "worktree": worktree_path,
                "warnings": warnings,
                "actor_identity": actor_identity_data(claim_obj.claimed_by),
                "continuation": continuation_data(
                    task_id,
                    claim_obj.id,
                    claim_obj.claimed_by,
                    attestation_context=(
                        claim_obj.attestation_context.model_dump(mode="json")
                        if claim_obj.attestation_context is not None
                        else None
                    ),
                    generation=claim_obj.generation,
                ),
            },
        )
        return

    # Confirmation output.
    typer.echo(f"Claimed task '{task_id}' as {safe_actor_label(claim_obj.claimed_by)}.")
    typer.echo(f"  Claim ID:    {claim_obj.id}")
    typer.echo(f"  Lease until: {claim_obj.lease_expires_at.isoformat()}")
    if reported_branch:
        typer.echo(f"  Branch:      {reported_branch}")
    if worktree_path:
        typer.echo(f"  Worktree:    {worktree_path}")
    if claim_obj.attestation_context is None:
        typer.echo(
            "Warning: progress attestation is unavailable because this project "
            "is not an accessible Git repository."
        )
    typer.echo("")
    for line in actor_notice_lines(claim_obj.claimed_by):
        typer.echo(line)
    hook_env = hook_env_for_human(claim_obj.claimed_by, claim_obj.id)
    if hook_env is not None:
        typer.echo(f"  Pin hook attribution: `{hook_env}`")
    else:
        typer.echo("  Use JSON/MCP structured hook_environment for hook attribution.")
    if claim_obj.attestation_context is not None:
        typer.echo(
            f"External progress: `anvil progress {task_id} PHASE "
            "--attestation-file PATH --actor ...`; an accepted attestation "
            "is consumed by the next renewal."
        )
    actor_flag = actor_flag_for_human(claim_obj.claimed_by)
    if actor_flag is not None:
        typer.echo(
            f"Run `anvil renew {claim_obj.id} {actor_flag}` to extend the lease "
            "before it expires."
        )
    else:
        typer.echo("Use the structured JSON/MCP renew argv to extend this lease.")


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
        help=(
            "Actor identity. Precedence: --actor > ANVIL_ACTOR > "
            "ANVIL_GATE_ACTOR > derived local identity."
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

        bundle_claim = builtins.next(
            (claim for claim in backend.list_bundle_claims() if claim.id == claim_id),
            None,
        )
        if bundle_claim is not None:
            if not force and bundle_claim.claimed_by != resolved_actor:
                _refuse_actor_mismatch(
                    "release",
                    owner=bundle_claim.claimed_by,
                    actual=resolved_actor,
                    action="Release",
                    json_output=json_output,
                )
            from anvil.bundles.manager import BundleError, BundleManager

            try:
                BundleManager(
                    backend,
                    clock,
                    actor=resolved_actor,
                    project_root=_resolve_project_dir(cwd),
                ).release(bundle_claim.bundle_id, force=force, reason=reason)
            except BundleError as exc:
                if json_output:
                    fail("release", str(exc), code="bundle_claim_error")
                typer.echo(f"Error: {exc}", err=True)
                raise typer.Exit(code=1) from exc
            if json_output:
                emit_success(
                    "release",
                    {
                        "claim_id": claim_id,
                        "released": True,
                        "reason": reason,
                        "actor_identity": actor_identity_data(resolved_actor),
                    },
                )
                return
            typer.echo(f"Released bundle claim '{claim_id}'.")
            return

        manager = ClaimManager(backend, clock, actor=resolved_actor)
        existing_claim = backend.get_claim(claim_id)
        if (
            existing_claim is not None
            and not force
            and existing_claim.claimed_by != resolved_actor
        ):
            _refuse_actor_mismatch(
                "release",
                owner=existing_claim.claimed_by,
                actual=resolved_actor,
                action="Release",
                json_output=json_output,
            )
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
            {
                "claim_id": claim_id,
                "released": True,
                "reason": reason,
                "actor_identity": actor_identity_data(resolved_actor),
            },
        )
        return

    typer.echo(f"Released claim '{claim_id}'.")
    if reason:
        typer.echo(f"  Reason: {reason}")
    for line in actor_notice_lines(resolved_actor):
        typer.echo(line)


# ---------------------------------------------------------------------------
# renew subcommand
# ---------------------------------------------------------------------------


def renew(
    claim_id: str = typer.Argument(..., help="Claim ID to renew (e.g. C001)."),  # noqa: B008
    actor: str | None = typer.Option(  # noqa: B008
        None,
        "--actor",
        help=(
            "Actor identity. Precedence: --actor > ANVIL_ACTOR > "
            "ANVIL_GATE_ACTOR > derived local identity."
        ),
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

        bundle_claim = builtins.next(
            (claim for claim in backend.list_bundle_claims() if claim.id == claim_id),
            None,
        )
        if bundle_claim is not None:
            if bundle_claim.claimed_by != resolved_actor:
                _refuse_actor_mismatch(
                    "renew",
                    owner=bundle_claim.claimed_by,
                    actual=resolved_actor,
                    action="Renewal",
                    json_output=json_output,
                )
            from anvil.bundles.manager import BundleError, BundleManager

            try:
                updated = BundleManager(
                    backend,
                    clock,
                    actor=resolved_actor,
                    project_root=_resolve_project_dir(cwd),
                    lease_minutes=lease_kwargs.get("default_lease_minutes", 240),
                ).renew(bundle_claim.bundle_id)
            except BundleError as exc:
                if json_output:
                    fail("renew", str(exc), code="bundle_claim_error")
                typer.echo(f"Error: {exc}", err=True)
                raise typer.Exit(code=1) from exc
            if json_output:
                emit_success(
                    "renew",
                    {
                        "claim": dump_model(updated),
                        "renewed": True,
                        "actor_identity": actor_identity_data(resolved_actor),
                    },
                )
                return
            typer.echo(f"Renewed bundle claim '{claim_id}'.")
            typer.echo(f"  New lease until: {updated.lease_expires_at.isoformat()}")
            return

        manager = ClaimManager(
            backend, clock, actor=resolved_actor, **lease_kwargs
        )
        before = backend.get_claim(claim_id)
        if before is not None and before.claimed_by != resolved_actor:
            _refuse_actor_mismatch(
                "renew",
                owner=before.claimed_by,
                actual=resolved_actor,
                action="Renewal",
                json_output=json_output,
            )
        try:
            renewal = manager.renew_with_result(claim_id)
            updated = renewal.claim
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
    extended = renewal.renewed
    progress = {
        "source": renewal.progress_source,
        "digest": renewal.attestation_digest,
        "generation": renewal.attestation_generation,
        "trust_mode": renewal.attestation_trust_mode,
    }

    if json_output:
        emit_success(
            "renew",
            {
                "claim": dump_model(updated),
                "renewed": extended,
                "progress": progress,
                "actor_identity": actor_identity_data(resolved_actor),
            },
        )
        return

    if extended:
        typer.echo(f"Renewed claim '{claim_id}'.")
        typer.echo(f"  New lease until: {updated.lease_expires_at.isoformat()}")
        typer.echo(f"  Last heartbeat:  {updated.last_heartbeat_at.isoformat()}")
        typer.echo(f"  Progress source: {renewal.progress_source}")
        if renewal.attestation_digest is not None:
            typer.echo(f"  Attestation:     {renewal.attestation_digest}")
            typer.echo(f"  Generation:      {renewal.attestation_generation}")
            typer.echo(f"  Trust mode:      {renewal.attestation_trust_mode}")
    else:
        typer.echo(f"Renew declined for '{claim_id}': no progress since last heartbeat.")
        typer.echo(f"  Lease still expires at: {updated.lease_expires_at.isoformat()}")
        typer.echo("  Change a file among the claim's expected files, or release and re-claim.")
    for line in actor_notice_lines(resolved_actor):
        typer.echo(line)


# ---------------------------------------------------------------------------
# next subcommand
# ---------------------------------------------------------------------------


def next(  # noqa: A001
    actor: str | None = typer.Option(  # noqa: B008
        None,
        "--actor",
        help="Actor identity; defaults to $USER or 'agent'.",
    ),
    bundle_mode: bool = typer.Option(  # noqa: B008
        False,
        "--bundle",
        help="Recommend a claimable execution bundle and explain bundle refusals.",
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
    {"task": {...} | null, "governor": {...}, "withheld_reason": ...}}``.
    The governor block always reports the exact observation window, rate
    numerator/denominator, configured or task-escalated floor, review-queue
    depth/cap, and recovery guidance. ``task`` is null when nothing is
    claimable (exit 0, an empty queue is not an error); ``withheld_reason`` and
    ``governor.offer_throttled`` distinguish governed withholding from an
    empty or otherwise ineligible queue.

    With ``-q``/``--quiet`` prints nothing and uses the exit code as the
    signal: 0 if a task is claimable, 3 if the queue is empty (an empty
    queue is not an error).
    """

    from anvil.claims.manager import ClaimManager
    from anvil.clock import SystemClock

    resolved_actor = resolve_actor(actor)
    state_dir = _resolve_state_dir(cwd)
    _require_state_dir(state_dir, command="next", json_output=json_output)

    if bundle_mode and (
        task_type is not None
        or max_blast is not None
        or max_review_risk is not None
    ):
        message = (
            "--type, --max-blast, and --max-review-risk are task-only filters "
            "and cannot be combined with --bundle."
        )
        if json_output:
            fail("next", message, code="invalid_bundle_filter")
        typer.echo(f"Error: {message}", err=True)
        raise typer.Exit(code=2)

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
        if bundle_mode:
            from anvil.state.rollup import compute_bundle_rollup

            bundles = backend.list_bundles(prd_id=scoped_prd_id)
            bundle_ids = {bundle.id for bundle in bundles}
            rollups = compute_bundle_rollup(
                bundles,
                backend.list_tasks(),
                [
                    claim
                    for claim in backend.list_bundle_claims()
                    if claim.bundle_id in bundle_ids
                ],
                [
                    review
                    for bundle in bundles
                    for review in backend.list_bundle_reviews(bundle.id)
                ],
                backend.list_active_claims(),
                now=clock.now(),
                actor=resolved_actor,
            )
            selected = builtins.next(
                (entry for entry in rollups if entry.claimable), None
            )
            if quiet:
                raise typer.Exit(0 if selected is not None else 3)
            if json_output:
                emit_success(
                    "next",
                    {
                        "bundle": dump_model(selected) if selected else None,
                        "bundle_refusals": [
                            dump_model(entry) for entry in rollups if not entry.claimable
                        ],
                        "withheld_reason": None if selected else "bundle_refused",
                    },
                )
                return
            if selected is not None:
                typer.echo(f"Next recommended bundle: {selected.bundle_id}")
                typer.echo(
                    f"  Coordinator: {safe_actor_label(selected.coordinator)}"
                )
                typer.echo(
                    "  Throughput: "
                    f"tasks={selected.throughput['tasks']}/"
                    f"{selected.throughput['max_tasks']} serial-stages="
                    f"{selected.throughput['serial_stages']}/"
                    f"{selected.throughput['max_serial_stages']}"
                )
                typer.echo("")
                typer.echo(
                    f"Run `anvil claim --bundle {selected.bundle_id}` to acquire the lease."
                )
                return
            typer.echo("No claimable execution bundles available.")
            for entry in rollups:
                for refusal in entry.refusals:
                    detail = refusal["detail"]
                    remediation = refusal["remediation"]
                    if refusal["code"] == "coordinator":
                        coordinator = safe_actor_label(entry.coordinator)
                        detail = f"coordinator is {coordinator}; caller differs."
                        remediation = (
                            f"Run as {coordinator} or assign a replacement bundle."
                        )
                    typer.echo(
                        f"  {entry.bundle_id} [{refusal['code']}]: "
                        f"{detail} Remediation: {remediation}"
                    )
            return
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
            as_of=clock.now(),
        )
        diagnosis = manager.diagnose_next_offer(
            task_type=task_type,
            max_blast=max_blast,
            max_review_risk=max_review_risk,
            metrics=metrics,
            prd_id=scoped_prd_id,
        )
        task = diagnosis.task
        # B49 observability: distinguish a governed withhold (review queue
        # saturated / runner below the accept-rate floor) from a genuinely empty
        # queue — otherwise an idle fleet is indistinguishable from a done one.
        withheld_reason = diagnosis.withheld_reason
        governor_task_id = diagnosis.governor_task_id
        if task is None and scoped_prd_id is not None:
            if diagnosis.ready_count == 0:
                scoped_empty_message = f"No ready tasks in this PRD ({scoped_prd_id})."
            else:
                scoped_empty_message = (
                    f"No claimable tasks in this PRD ({scoped_prd_id})."
                )

        governor_projection = metrics.projection(
            resolved_actor,
            task_id=task.id if task is not None else governor_task_id,
        )
        governor_projection["withheld_reason"] = withheld_reason
        governor_projection["offer_throttled"] = withheld_reason in {
            "review_queue_saturated",
            "actor_below_floor",
            "task_accept_rate_floor",
        }

        # retro-opps T009 — ADVISORY collision visibility: the selected task's
        # likely_files intersected with active claims' expected_files, via the
        # same ClaimManager.check_conflicts the claim gate uses (never
        # duplicated). Selection above is untouched — conflict-GROUP holds
        # already exclude candidates; this surfaces the residual overlap a
        # claim's runtime expected_files can introduce outside any group.
        conflict_warnings: list[dict[str, Any]] = []
        if task is not None and task.likely_files:
            conflict_warnings = [
                {
                    "claim_id": w.other_claim_id,
                    "actor": w.other_actor,
                    "files": list(w.overlapping_files),
                }
                for w in manager.check_conflicts(task.id, list(task.likely_files))
            ]
    finally:
        backend.close()

    if quiet:
        # ponytail: the exit code is the loop seam (`while anvil next -q`). A
        # governed withhold is still "no work right now" -> exit 3 (the loop
        # backs off either way); the reason is surfaced in --json / human mode.
        raise typer.Exit(0 if task is not None else 3)

    # retro-opps T003 — derive-only review tier, computed at read time from
    # the loaded config (None → module defaults). Recomputed every read, so
    # a risk confirmation at `anvil review tasks` flips it with no migration.
    from anvil.planning.scoring import review_tier

    task_review_tier = review_tier(task, config=cfg) if task is not None else None

    if json_output:
        data = {
            "task": dump_model(task) if task is not None else None,
            "review_tier": task_review_tier,
            "conflict_warnings": conflict_warnings,
            "withheld_reason": withheld_reason,
            "governor": governor_projection,
        }
        if scoped_empty_message is not None:
            data["prd"] = scoped_prd_id
            data["message"] = scoped_empty_message
        emit_success("next", data)
        if scoped_empty_message is not None:
            raise typer.Exit(code=3)
        return

    if task is None:
        typer.echo(
            "Governor: "
            f"numerator={governor_projection['numerator']} "
            f"denominator={governor_projection['denominator']} "
            f"rate={governor_projection['rate']} "
            f"floor={governor_projection['floor']} "
            f"window_days={governor_projection['window_days']} "
            f"as_of={governor_projection['as_of']}"
        )
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
                f"No work offered: actor {safe_actor_label(resolved_actor)} is below the "
                "accept-rate floor."
            )
        elif withheld_reason == "task_accept_rate_floor":
            typer.echo(
                "No work offered: the next eligible task requires accept-rate "
                f"floor {governor_projection['floor']}."
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
        typer.echo(f"Recovery: {governor_projection['guidance']}")
        return

    typer.echo(f"Next recommended task: {task.id}")
    typer.echo(f"  Title:    {task.title}")
    typer.echo(f"  Priority: {task.priority.value}")
    typer.echo(f"  Review tier: {task_review_tier}")
    if task.scores.complexity is not None:
        typer.echo(f"  Complexity: {task.scores.complexity}")
    typer.echo(
        "  Governor: "
        f"numerator={governor_projection['numerator']} "
        f"denominator={governor_projection['denominator']} "
        f"rate={governor_projection['rate']} "
        f"floor={governor_projection['floor']} "
        f"window_days={governor_projection['window_days']} "
        f"as_of={governor_projection['as_of']}"
    )
    typer.echo(f"  Recovery: {governor_projection['guidance']}")
    for warning in conflict_warnings:
        typer.echo(
            f"  Conflict warning: files {', '.join(warning['files'])} overlap "
            f"active claim {warning['claim_id']} "
            f"({safe_actor_label(str(warning['actor']))})."
        )
    typer.echo("")
    typer.echo(f"Run `anvil claim {task.id}` to acquire the lease.")
