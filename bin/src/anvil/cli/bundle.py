"""Deterministic CLI contract for execution bundles."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from anvil.cli._helpers import (
    _open_backend,
    _require_state_dir,
    _resolve_state_dir,
    resolve_actor,
)
from anvil.cli._json import JSON_OPTION, dump_model, emit_success, fail

bundle_app = typer.Typer(help="Create, inspect, review, and deliver execution bundles.")


def _fail(command: str, message: str, json_output: bool) -> None:
    if json_output:
        fail(command, message, code="bundle_error")
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(code=1)


def _state(cwd: Path | None, command: str, json_output: bool):  # type: ignore[no-untyped-def]
    state_dir = _resolve_state_dir(cwd)
    _require_state_dir(state_dir, command=command, json_output=json_output)
    return state_dir, _open_backend(state_dir)


@bundle_app.command("create")
def create_bundle(
    bundle_id: str,
    task_ids: list[str] = typer.Argument(..., help="Ordered member task IDs."),  # noqa: B008
    coordinator: str | None = typer.Option(None, "--coordinator"),  # noqa: B008
    prd_id: str = typer.Option(..., "--prd"),  # noqa: B008
    actor: str | None = typer.Option(None, "--actor"),  # noqa: B008
    max_tasks: int = typer.Option(12, "--max-tasks", min=1, max=500),  # noqa: B008
    max_serial_stages: int = typer.Option(  # noqa: B008
        6, "--max-serial-stages", min=1, max=500
    ),
    max_reviews: int = typer.Option(3, "--max-reviews", min=1, max=20),  # noqa: B008
    max_rereviews: int = typer.Option(1, "--max-rereviews", min=0),  # noqa: B008
    required_angle: list[str] | None = typer.Option(  # noqa: B008
        None, "--required-angle"
    ),
    json_output: bool = JSON_OPTION,
    cwd: Path | None = typer.Option(None, "--cwd", hidden=True),  # noqa: B008
) -> None:
    """Create a planned execution bundle with ordered membership."""
    from anvil.bundles.catalog import BundleCatalog, BundleCatalogError
    from anvil.clock import SystemClock
    from anvil.state.models import BundleReviewPolicy, BundleThroughputBudget

    command = "bundle create"
    state_dir, backend = _state(cwd, command, json_output)
    del state_dir
    resolved_actor = resolve_actor(actor)
    resolved_coordinator = (coordinator or resolved_actor).strip()
    try:
        bundle = BundleCatalog(backend, SystemClock(), actor=resolved_actor).create(
            bundle_id,
            prd_id=prd_id,
            task_ids=task_ids,
            coordinator=resolved_coordinator,
            review_policy=BundleReviewPolicy(
                max_reviews=max_reviews,
                max_rereviews=max_rereviews,
                required_angles=required_angle or [],
            ),
            throughput_budget=BundleThroughputBudget(
                max_tasks=max_tasks,
                max_serial_stages=max_serial_stages,
            ),
        )
    except (BundleCatalogError, ValueError) as exc:
        _fail(command, str(exc), json_output)
    finally:
        backend.close()
    if json_output:
        emit_success(command, {"bundle": dump_model(bundle)})
        return
    typer.echo(f"Created bundle '{bundle.id}' ({bundle.status.value}).")
    typer.echo(f"  PRD:         {bundle.prd_id}")
    typer.echo(f"  Coordinator: {bundle.coordinator}")
    typer.echo(f"  Members:     {', '.join(bundle.task_ids)}")


@bundle_app.command("show")
def show_bundle(
    bundle_id: str,
    json_output: bool = JSON_OPTION,
    cwd: Path | None = typer.Option(None, "--cwd", hidden=True),  # noqa: B008
) -> None:
    """Show one bundle, its claim, and current review verdicts."""
    from anvil.bundles.catalog import BundleCatalog, BundleCatalogError
    from anvil.clock import SystemClock

    command = "bundle show"
    _, backend = _state(cwd, command, json_output)
    try:
        bundle = BundleCatalog(backend, SystemClock(), actor="reader").get(bundle_id)
        claim = backend.get_bundle_claim(bundle_id)
        reviews = backend.list_bundle_reviews(bundle_id)
    except BundleCatalogError as exc:
        _fail(command, str(exc), json_output)
    finally:
        backend.close()
    data = {
        "bundle": dump_model(bundle),
        "claim": dump_model(claim) if claim else None,
        "reviews": [dump_model(review) for review in reviews],
    }
    if json_output:
        emit_success(command, data)
        return
    typer.echo(f"Bundle {bundle.id}: {bundle.status.value}")
    typer.echo(f"  PRD: {bundle.prd_id}; coordinator: {bundle.coordinator}")
    typer.echo(f"  Members: {', '.join(bundle.task_ids)}")
    typer.echo(f"  Claim: {claim.id if claim else '(none)'}")
    typer.echo(f"  Reviews: {len(reviews)}")
    if bundle.checkpoint:
        typer.echo(f"  Checkpoint: {json.dumps(dump_model(bundle.checkpoint), sort_keys=True)}")
    if bundle.superseded_by:
        typer.echo(f"  Superseded by: {bundle.superseded_by}")


@bundle_app.command("list")
def list_bundles(
    prd_id: str | None = typer.Option(None, "--prd"),  # noqa: B008
    json_output: bool = JSON_OPTION,
    cwd: Path | None = typer.Option(None, "--cwd", hidden=True),  # noqa: B008
) -> None:
    """List execution bundles in stable ID order."""
    command = "bundle list"
    _, backend = _state(cwd, command, json_output)
    try:
        bundles = backend.list_bundles(prd_id=prd_id)
    finally:
        backend.close()
    if json_output:
        emit_success(command, {"bundles": [dump_model(bundle) for bundle in bundles]})
        return
    if not bundles:
        typer.echo("No execution bundles.")
        return
    for bundle in bundles:
        typer.echo(
            f"{bundle.id}  {bundle.status.value}  coordinator={bundle.coordinator} "
            f"members={len(bundle.task_ids)}"
        )


def _manager(backend, state_dir: Path, actor: str):  # type: ignore[no-untyped-def]
    from anvil.bundles.manager import BundleManager
    from anvil.clock import SystemClock

    return BundleManager(
        backend,
        SystemClock(),
        actor=actor,
        project_root=state_dir.parent,
    )


@bundle_app.command("claim")
def claim_bundle(
    bundle_id: str,
    actor: str | None = typer.Option(None, "--actor"),  # noqa: B008
    shared_tree: bool = typer.Option(False, "--shared-tree"),  # noqa: B008
    json_output: bool = JSON_OPTION,
    cwd: Path | None = typer.Option(None, "--cwd", hidden=True),  # noqa: B008
) -> None:
    """Claim a bundle without Git side effects (use top-level claim for Git)."""
    from anvil.bundles.manager import BundleError

    command = "bundle claim"
    state_dir, backend = _state(cwd, command, json_output)
    try:
        from anvil.cli._helpers import _load_config_optional

        cfg = _load_config_optional(state_dir)
        isolation = cfg.worktree_isolation if cfg is not None else "advisory"
        if isolation == "require" and not shared_tree:
            _fail(
                command,
                "worktree_isolation: require; use top-level `anvil claim --bundle "
                "--worktree`, or pass --shared-tree.",
                json_output,
            )
        result = _manager(backend, state_dir, resolve_actor(actor)).claim(bundle_id)
    except BundleError as exc:
        _fail(command, str(exc), json_output)
    finally:
        backend.close()
    if json_output:
        emit_success(
            command,
            {"bundle": dump_model(result.bundle), "claim": dump_model(result.claim)},
        )
        return
    typer.echo(f"Claimed bundle '{bundle_id}' with coordinator claim {result.claim.id}.")


@bundle_app.command("packet")
def bundle_packet(
    bundle_id: str,
    format: str = typer.Option("markdown", "--format"),  # noqa: B008
    actor: str | None = typer.Option(None, "--actor"),  # noqa: B008
    json_output: bool = JSON_OPTION,
    cwd: Path | None = typer.Option(None, "--cwd", hidden=True),  # noqa: B008
) -> None:
    """Render the aggregate coordinator work packet."""
    from anvil.bundles.manager import BundleError

    command = "bundle packet"
    state_dir, backend = _state(cwd, command, json_output)
    try:
        packet = _manager(backend, state_dir, resolve_actor(actor)).packet(bundle_id)
    except BundleError as exc:
        _fail(command, str(exc), json_output)
    finally:
        backend.close()
    if format not in {"markdown", "json"}:
        _fail(command, "format must be markdown or json", json_output)
    content = packet.markdown if format == "markdown" else packet.json_data
    if json_output:
        emit_success(command, {"format": format, "content": content})
        return
    typer.echo(json.dumps(content, sort_keys=True) if format == "json" else content)


@bundle_app.command("progress")
def bundle_progress(
    bundle_id: str,
    phase: str,
    detail: str | None = typer.Option(None, "--detail"),  # noqa: B008
    member_task_id: list[str] | None = typer.Option(  # noqa: B008
        None, "--member-task"
    ),
    actor: str | None = typer.Option(None, "--actor"),  # noqa: B008
    json_output: bool = JSON_OPTION,
    cwd: Path | None = typer.Option(None, "--cwd", hidden=True),  # noqa: B008
) -> None:
    """Record coordinator progress on an active bundle claim."""
    from anvil.bundles.manager import BundleError

    command = "bundle progress"
    state_dir, backend = _state(cwd, command, json_output)
    try:
        _manager(backend, state_dir, resolve_actor(actor)).note_progress(
            bundle_id,
            phase=phase,
            detail=detail,
            member_task_ids=member_task_id,
        )
    except BundleError as exc:
        _fail(command, str(exc), json_output)
    finally:
        backend.close()
    if json_output:
        emit_success(command, {"bundle_id": bundle_id, "phase": phase, "recorded": True})
        return
    typer.echo(f"Progress recorded for bundle '{bundle_id}': {phase}")


@bundle_app.command("status")
def bundle_status(
    bundle_id: str | None = typer.Argument(None),
    json_output: bool = JSON_OPTION,
    cwd: Path | None = typer.Option(None, "--cwd", hidden=True),  # noqa: B008
) -> None:
    """Show claimability, rollups, refusals, and remediation for bundles."""
    from anvil.clock import SystemClock
    from anvil.state.rollup import compute_bundle_rollup

    command = "bundle status"
    _, backend = _state(cwd, command, json_output)
    try:
        if bundle_id:
            bundle = backend.get_bundle(bundle_id)
            bundles = [bundle] if bundle is not None else []
        else:
            bundles = backend.list_bundles()
        if bundle_id and not bundles:
            _fail(command, f"Bundle '{bundle_id}' not found.", json_output)
        tasks = backend.list_tasks()
        claims = backend.list_active_claims()
        bundle_ids = {bundle.id for bundle in bundles}
        rollups = compute_bundle_rollup(
            bundles,
            tasks,
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
            claims,
            now=SystemClock().now(),
        )
    finally:
        backend.close()
    if json_output:
        emit_success(command, {"bundles": [dump_model(entry) for entry in rollups]})
        return
    for entry in rollups:
        typer.echo(
            f"Bundle {entry.bundle_id} ({entry.status}) claimable={entry.claimable}"
        )
        for refusal in entry.refusals:
            typer.echo(
                f"  [{refusal['code']}] {refusal['detail']} "
                f"Remediation: {refusal['remediation']}"
            )


def _delivery_manager(backend, actor: str):  # type: ignore[no-untyped-def]
    from anvil.bundles.delivery import BundleDeliveryManager
    from anvil.clock import SystemClock

    return BundleDeliveryManager(backend, SystemClock(), actor=actor)


@bundle_app.command("review")
def review_bundle(
    bundle_id: str,
    angle: str = typer.Option(..., "--angle"),  # noqa: B008
    decision: str = typer.Option(..., "--decision"),  # noqa: B008
    review_round: int = typer.Option(1, "--round", min=1),  # noqa: B008
    notes: str | None = typer.Option(None, "--notes"),  # noqa: B008
    actor: str | None = typer.Option(None, "--actor"),  # noqa: B008
    json_output: bool = JSON_OPTION,
    cwd: Path | None = typer.Option(None, "--cwd", hidden=True),  # noqa: B008
) -> None:
    """Record one independent adversarial bundle verdict."""
    from anvil.bundles.review import BundleReviewError, BundleReviewManager
    from anvil.clock import SystemClock
    from anvil.state.models import ReviewDecision

    command = "bundle review"
    _, backend = _state(cwd, command, json_output)
    try:
        gate = BundleReviewManager(
            backend, SystemClock(), actor=resolve_actor(actor)
        ).record(
            bundle_id,
            review_round=review_round,
            angle=angle,
            decision=ReviewDecision(decision),
            notes=notes,
        )
    except (BundleReviewError, ValueError) as exc:
        _fail(command, str(exc), json_output)
    finally:
        backend.close()
    if json_output:
        emit_success(command, {"gate": gate.__dict__})
        return
    typer.echo(
        f"Recorded {decision} review for bundle '{bundle_id}' "
        f"(round {review_round}, angle {angle})."
    )
    typer.echo(f"  Gate passed: {gate.passed}; replan required: {gate.replan_required}")


@bundle_app.command("finalize-review")
def finalize_bundle_review(
    bundle_id: str,
    actor: str | None = typer.Option(None, "--actor"),  # noqa: B008
    json_output: bool = JSON_OPTION,
    cwd: Path | None = typer.Option(None, "--cwd", hidden=True),  # noqa: B008
) -> None:
    """Apply a complete bounded review gate as the coordinator."""
    from anvil.bundles.review import BundleReviewError, BundleReviewManager
    from anvil.clock import SystemClock

    command = "bundle finalize-review"
    _, backend = _state(cwd, command, json_output)
    try:
        gate = BundleReviewManager(
            backend, SystemClock(), actor=resolve_actor(actor)
        ).finalize(bundle_id)
        bundle = backend.get_bundle(bundle_id)
    except BundleReviewError as exc:
        _fail(command, str(exc), json_output)
    finally:
        backend.close()
    if json_output:
        emit_success(command, {"bundle": dump_model(bundle), "gate": gate.__dict__})
        return
    typer.echo(f"Finalized review for bundle '{bundle_id}' -> {bundle.status.value}.")


@bundle_app.command("checkpoint")
def checkpoint_bundle(
    bundle_id: str,
    commit_sha: str | None = typer.Option(None, "--commit"),  # noqa: B008
    pr_url: str | None = typer.Option(None, "--pr-url"),  # noqa: B008
    actor: str | None = typer.Option(None, "--actor"),  # noqa: B008
    json_output: bool = JSON_OPTION,
    cwd: Path | None = typer.Option(None, "--cwd", hidden=True),  # noqa: B008
) -> None:
    """Record a delivery checkpoint without fabricating task evidence."""
    from anvil.bundles.delivery import BundleDeliveryError

    command = "bundle checkpoint"
    _, backend = _state(cwd, command, json_output)
    try:
        checkpoint = _delivery_manager(backend, resolve_actor(actor)).checkpoint(
            bundle_id, commit_sha=commit_sha, pr_url=pr_url
        )
    except (BundleDeliveryError, ValueError) as exc:
        _fail(command, str(exc), json_output)
    finally:
        backend.close()
    if json_output:
        emit_success(command, {"checkpoint": dump_model(checkpoint)})
        return
    typer.echo(f"Checkpointed bundle '{bundle_id}'.")
    typer.echo(f"  Commit: {checkpoint.commit_sha or '(none)'}")
    typer.echo(f"  PR:     {checkpoint.pr_url or '(none)'}")


@bundle_app.command("reconcile")
def reconcile_bundle(
    bundle_id: str,
    commit_sha: str | None = typer.Option(None, "--commit"),  # noqa: B008
    pr_url: str | None = typer.Option(None, "--pr-url"),  # noqa: B008
    merged: bool = typer.Option(False, "--merged"),  # noqa: B008
    actor: str | None = typer.Option(None, "--actor"),  # noqa: B008
    json_output: bool = JSON_OPTION,
    cwd: Path | None = typer.Option(None, "--cwd", hidden=True),  # noqa: B008
) -> None:
    """Idempotently reconcile delivery state and checkpoint metadata."""
    from anvil.bundles.delivery import BundleDeliveryError

    command = "bundle reconcile"
    _, backend = _state(cwd, command, json_output)
    try:
        _delivery_manager(backend, resolve_actor(actor)).reconcile(
            bundle_id, commit_sha=commit_sha, pr_url=pr_url, merged=merged
        )
        bundle = backend.get_bundle(bundle_id)
    except (BundleDeliveryError, ValueError) as exc:
        _fail(command, str(exc), json_output)
    finally:
        backend.close()
    if json_output:
        emit_success(command, {"bundle": dump_model(bundle)})
        return
    typer.echo(f"Reconciled bundle '{bundle_id}' -> {bundle.status.value}.")


@bundle_app.command("supersede")
def supersede_bundle(
    bundle_id: str,
    replacement_bundle_id: str = typer.Option(..., "--replacement"),  # noqa: B008
    actor: str | None = typer.Option(None, "--actor"),  # noqa: B008
    json_output: bool = JSON_OPTION,
    cwd: Path | None = typer.Option(None, "--cwd", hidden=True),  # noqa: B008
) -> None:
    """Supersede a bundle while preserving both histories."""
    from anvil.bundles.delivery import BundleDeliveryError

    command = "bundle supersede"
    _, backend = _state(cwd, command, json_output)
    try:
        _delivery_manager(backend, resolve_actor(actor)).supersede(
            bundle_id, replacement_bundle_id=replacement_bundle_id
        )
        bundle = backend.get_bundle(bundle_id)
    except (BundleDeliveryError, ValueError) as exc:
        _fail(command, str(exc), json_output)
    finally:
        backend.close()
    if json_output:
        emit_success(command, {"bundle": dump_model(bundle)})
        return
    typer.echo(f"Superseded bundle '{bundle_id}' with '{replacement_bundle_id}'.")
