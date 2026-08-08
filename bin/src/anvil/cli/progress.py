"""``anvil progress`` — record a structured progress phase for a task.

retro-opps:T011. The CLI twin of the MCP ``submit_progress`` tool: appends
one ``progress.noted`` audit event carrying an optional structured ``phase``
label plus free-text detail. Audit-only — task status never changes, no
claim is required. The read side (``anvil status`` / ``notify-digest``)
lands in T012.
"""

from __future__ import annotations

from pathlib import Path

import typer

from anvil.actors import ActorIdentityError, canonicalize_new_actor
from anvil.cli._actor_output import (
    actor_identity_data,
    actor_mismatch_data,
    actor_mismatch_message,
    actor_notice_lines,
)
from anvil.cli._helpers import (
    StateRootError,
    _open_backend,
    _require_state_dir,
    _resolve_project_dir,
    _resolve_state_dir,
    resolve_actor,
)
from anvil.cli._json import JSON_OPTION, emit_success, fail, fail_with

_COMMAND = "progress"


def progress(
    task_id: str = typer.Argument(..., help="Task the progress note is for."),
    phase: str = typer.Argument(
        ...,
        help=(
            "Structured phase label for the heartbeat bus "
            '(e.g. "build", "tests", "review-fixes").'
        ),
    ),
    detail: str | None = typer.Option(  # noqa: B008
        None, "--detail", help="Free-text elaboration for the phase."
    ),
    bundle_mode: bool = typer.Option(  # noqa: B008
        False,
        "--bundle",
        help="Record coordinator progress for an execution bundle.",
    ),
    attestation_file: Path | None = typer.Option(  # noqa: B008
        None,
        "--attestation-file",
        help=(
            "Canonical JSON claim-bound progress attestation. Accepted evidence "
            "is consumed once by the next claim renewal; free-text notes never renew."
        ),
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
    """Record a progress phase for TASK_ID as a ``progress.noted`` audit event.

    Does NOT change task status and does not require an active claim —
    mirrors the MCP ``submit_progress`` tool so agents and humans share one
    event shape. ``anvil status`` surfaces the latest phase per active claim
    (T012).
    """
    resolved_actor = resolve_actor(actor)

    try:
        state_dir = _resolve_state_dir(cwd)
    except StateRootError as exc:
        if json_output:
            fail(_COMMAND, str(exc), code="state_root_invalid")
        raise
    _require_state_dir(state_dir, command=_COMMAND, json_output=json_output)

    backend = _open_backend(state_dir)
    try:
        from anvil.clock import SystemClock
        from anvil.state.models import EventDraft

        if bundle_mode:
            if attestation_file is not None:
                if json_output:
                    fail(
                        _COMMAND,
                        "--attestation-file is supported only for standalone task claims.",
                        code="bad_request",
                    )
                typer.echo(
                    "Error: --attestation-file is supported only for standalone task claims.",
                    err=True,
                )
                raise typer.Exit(code=1)
            from anvil.bundles.manager import BundleError, BundleManager

            try:
                BundleManager(
                    backend,
                    SystemClock(),
                    actor=resolved_actor,
                    project_root=Path.cwd(),
                ).note_progress(task_id, phase=phase, detail=detail)
            except BundleError as exc:
                if json_output:
                    fail(_COMMAND, str(exc), code="bundle_progress_error")
                typer.echo(f"Error: {exc}", err=True)
                raise typer.Exit(code=1) from exc
            if json_output:
                emit_success(
                    _COMMAND,
                    {
                        "bundle_id": task_id,
                        "actor": resolved_actor,
                        "phase": phase,
                        "detail": detail,
                        "recorded": True,
                        "actor_identity": actor_identity_data(resolved_actor),
                    },
                )
                return
            typer.echo(f"Progress recorded for bundle '{task_id}': {phase}")
            for line in actor_notice_lines(resolved_actor):
                typer.echo(line)
            return

        if attestation_file is not None:
            from anvil import signing
            from anvil.claims.manager import ClaimError, ClaimManager
            from anvil.claims.progress_attestation import (
                ProgressAttestationError,
                load_progress_attestation,
            )
            from anvil.cli.proof import _default_trust_path

            try:
                trusted = signing.load_trust_list(_default_trust_path())
                with attestation_file.open("rb") as stream:
                    loaded = load_progress_attestation(stream, trusted_issuers=trusted)
                if loaded.payload.task_id != task_id:
                    raise ProgressAttestationError(
                        "task_mismatch",
                        "attestation task does not match the progress command",
                    )
                persisted = ClaimManager(
                    backend,
                    SystemClock(),
                    actor=resolved_actor,
                    project_root=_resolve_project_dir(cwd),
                ).accept_progress_attestation(loaded)
            except (OSError, ClaimError, ProgressAttestationError) as exc:
                code = getattr(exc, "code", "progress_attestation_error")
                if json_output:
                    fail(_COMMAND, str(exc), code=str(code))
                typer.echo(f"Error: {exc}", err=True)
                raise typer.Exit(code=1) from exc

            attestation = {
                "digest": persisted.semantic_digest,
                "generation": persisted.generation,
                "trust_mode": persisted.trust_mode,
                "kind": persisted.kind,
                "issuer_id": persisted.issuer_id,
            }
            if json_output:
                emit_success(
                    _COMMAND,
                    {
                        "task_id": task_id,
                        "actor": resolved_actor,
                        "phase": phase,
                        "detail": detail,
                        "recorded": True,
                        "event_action": "progress.attested",
                        "attestation": attestation,
                        "actor_identity": actor_identity_data(resolved_actor),
                    },
                )
                return
            typer.echo(
                f"Progress attestation accepted for '{task_id}': {persisted.semantic_digest}"
            )
            typer.echo(f"  Generation: {persisted.generation}")
            typer.echo(f"  Trust mode: {persisted.trust_mode}")
            typer.echo("  The next successful renewal consumes this attestation once.")
            for line in actor_notice_lines(resolved_actor):
                typer.echo(line)
            return

        task = backend.get_task(task_id)
        if task is None:
            if json_output:
                fail(
                    _COMMAND,
                    f"task '{task_id}' not found.",
                    code="task_not_found",
                )
            typer.echo(f"Error: task '{task_id}' not found.", err=True)
            raise typer.Exit(code=1)

        active_claim = next(
            (claim for claim in backend.list_active_claims() if claim.task_id == task_id),
            None,
        )
        if active_claim is None:
            try:
                resolved_actor = canonicalize_new_actor(resolved_actor)
            except ActorIdentityError as exc:
                if json_output:
                    fail(_COMMAND, str(exc), code="actor_invalid")
                typer.echo(f"Error: {exc}", err=True)
                raise typer.Exit(code=1) from exc
        if active_claim is not None and active_claim.claimed_by != resolved_actor:
            message = actor_mismatch_message(
                owner=active_claim.claimed_by,
                actual=resolved_actor,
                action="Progress update",
            )
            if json_output:
                fail_with(
                    _COMMAND,
                    message,
                    code="actor_mismatch",
                    extra=actor_mismatch_data(
                        owner=active_claim.claimed_by, actual=resolved_actor
                    ),
                )
            typer.echo(f"Error: {message}", err=True)
            raise typer.Exit(code=1)

        now = SystemClock().now()
        draft = EventDraft(
            timestamp=now,
            actor=resolved_actor,
            action="progress.noted",
            target_kind="task",
            target_id=task_id,
            payload_json={
                "task_id": task_id,
                "actor": resolved_actor,
                "notes": detail or phase,
                "noted_at": now.isoformat(),
                # Same omit-when-None discipline as the MCP tool (T010) —
                # here phase is always present by construction.
                "phase": phase,
                **({"detail": detail} if detail is not None else {}),
            },
        )
        backend.append(draft)
    finally:
        backend.close()

    if json_output:
        emit_success(
            _COMMAND,
            {
                "task_id": task_id,
                "actor": resolved_actor,
                "phase": phase,
                "detail": detail,
                "recorded": True,
                "actor_identity": actor_identity_data(resolved_actor),
            },
        )
        return
    detail_note = f" — {detail}" if detail else ""
    typer.echo(f"Progress recorded for '{task_id}': {phase}{detail_note}")
    for line in actor_notice_lines(resolved_actor):
        typer.echo(line)
