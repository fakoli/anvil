"""``anvil gate-check`` — finish-gate decision for native agent harnesses (B42 Phase 2).

Read-only. Answers one question for an agent that is about to end its turn:
*does this actor's active claim have evidence satisfying its task's verification,
or should the agent be told to finish the work first?*

It exists for OpenClaw's native ``before_agent_finalize`` plugin — anvil's first
blocking gate, stronger than Claude Code's non-blocking hooks. The node plugin
shells out to ``anvil gate-check --json --actor agent`` cwd-scoped and maps the
result to ``continue`` / ``revise``. Honors the "anvil writes nothing for
OpenClaw" contract: this verb only *reads* ``state.db``.

Decision (default-OPEN — invisible unless there is a real reason to block):

* not a tracked anvil project / not initialized      → continue (exit 0)
* anvil state unavailable (missing / corrupt db)     → continue (exit 0)
* the actor holds no active claim                     → continue (exit 0)
* the claimed task's evidence is complete             → continue (exit 0)
* the claimed task's evidence is missing/incomplete   → **block** (exit 2)

"Complete" mirrors anvil's own accept path exactly: :func:`review.gates.evidence_complete`
checks that each ``Task.verification.required_evidence`` item has a corresponding
field on the submitted ``Evidence``. NOTE: anvil records no command exit codes on
``Evidence``, so this asserts "evidence was submitted for the claimed
verification", **not** "the commands exited 0". A true green-tests gate is a
larger, separate change.

Exit codes (so a jq-less host can branch on ``$?``, mirroring ``next -q``)::

    0 = continue    2 = block (revise)    1 = genuine error (invalid ANVIL_ROOT)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from anvil.cli._helpers import (
    StateRootError,
    _open_backend,
    _resolve_state_dir,
)
from anvil.cli._json import JSON_OPTION, emit_success, fail
from anvil.review.gates import evidence_complete

if TYPE_CHECKING:
    from anvil.state.models import Claim

__all__ = ["gate_check"]

_COMMAND = "gate-check"

_CONTINUE_MSG = "No claimed-but-unverified anvil task; finalization may proceed."


def gate_check(
    actor: str | None = typer.Option(  # noqa: B008
        None,
        "--actor",
        help=(
            "Actor whose active claim to gate. Defaults to $USER or 'agent' "
            "(the identity an agent harness claims under via MCP/CLI)."
        ),
    ),
    json_output: bool = JSON_OPTION,
    quiet: bool = typer.Option(  # noqa: B008
        False,
        "-q",
        "--quiet",
        help="Exit-code only (0 continue / 2 block); print nothing to stdout.",
    ),
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory. Defaults to the current working directory.",
        hidden=True,
    ),
) -> None:
    """Decide whether an agent may finalize, or must finish a claimed task first.

    Read-only finish-gate for native harnesses (OpenClaw ``before_agent_finalize``).
    Default-open: only blocks when *this actor* holds an active claim whose task
    has missing/incomplete verification evidence. Exits 0 (continue) / 2 (block).
    """
    resolved_actor = actor or os.environ.get("USER") or "agent"

    # ANVIL_ROOT set but invalid is a genuine error — surface it (exit 1), and
    # under --json as a parseable envelope (mirrors drift.py).
    try:
        state_dir = _resolve_state_dir(cwd)
    except StateRootError as exc:
        if json_output:
            fail(_COMMAND, str(exc), code="state_root_invalid")
        raise

    # Default-OPEN: a directory that is not a tracked anvil project is never a
    # block — the gate must be invisible everywhere except a real anvil project.
    if not state_dir.exists():
        _emit(json_output, quiet, _continue_decision(
            resolved_actor, "Directory is not a tracked anvil project; finalization may proceed.",
        ))
        return

    # db unavailable (missing / locked / corrupt) → cannot gate → continue;
    # never crash a finalize. NOTE the catch is scoped to the backend *open*
    # only, so a wrong decision predicate still surfaces loudly in tests rather
    # than being masked into a silently-inert gate.
    try:
        backend = _open_backend(state_dir)
    except Exception:  # noqa: BLE001 — any open failure means "cannot gate"
        _emit(json_output, quiet, _continue_decision(
            resolved_actor, "anvil state unavailable; finalization may proceed.",
        ))
        return

    try:
        claim = _active_claim_for(backend, resolved_actor)
        if claim is None:
            task_id: str | None = None
            claim_id: str | None = None
            passed, missing, instruction = True, [], _CONTINUE_MSG
        else:
            claim_id = claim.id
            task = backend.get_task(claim.task_id)
            if task is None:
                # Active claim with no task row — anomalous; do not block on it.
                task_id = None
                passed, missing, instruction = True, [], _CONTINUE_MSG
            else:
                task_id = task.id
                evidence = backend.get_latest_evidence(task.id)
                if evidence is None:
                    # No evidence yet: missing == everything the task requires.
                    required = list(task.verification.required_evidence)
                    passed, missing = (not required), required
                else:
                    passed, missing = evidence_complete(task, evidence)
                instruction = (
                    _CONTINUE_MSG if passed
                    else _block_instruction(task_id, resolved_actor, missing)
                )
    finally:
        backend.close()

    _emit(json_output, quiet, {
        "block": not passed,
        "action": "continue" if passed else "revise",
        "actor": resolved_actor,
        "task": task_id,
        "claim": claim_id,
        "evidence_gate": {"passed": passed, "missing": list(missing)},
        "instruction": instruction,
    })


def _active_claim_for(backend: Any, actor: str) -> Claim | None:
    """First ACTIVE claim held by *actor* (the gate only judges this actor's work)."""
    for claim in backend.list_active_claims():
        if claim.claimed_by == actor:
            return claim
    return None


def _block_instruction(task_id: str, actor: str, missing: list[str]) -> str:
    miss = ", ".join(missing) if missing else "verification evidence"
    return (
        f"Task {task_id} is claimed by '{actor}' but its verification evidence is "
        f"incomplete (missing: {miss}). Run the task's verification commands and "
        f"submit evidence before finishing — e.g. `anvil submit {task_id}` — then "
        f"end your turn. (anvil checks that required evidence was submitted, not "
        f"that commands exited 0.)"
    )


def _continue_decision(actor: str, instruction: str) -> dict[str, Any]:
    """A no-block decision payload (the default-open cases)."""
    return {
        "block": False,
        "action": "continue",
        "actor": actor,
        "task": None,
        "claim": None,
        "evidence_gate": {"passed": True, "missing": []},
        "instruction": instruction,
    }


def _emit(json_output: bool, quiet: bool, decision: dict[str, Any]) -> None:
    """Emit the decision and set the exit code (0 continue / 2 block)."""
    block = decision["block"]
    if quiet:
        if block:
            raise typer.Exit(code=2)
        return
    if json_output:
        emit_success(_COMMAND, decision)
    else:
        typer.echo(decision["instruction"])
    if block:
        raise typer.Exit(code=2)
