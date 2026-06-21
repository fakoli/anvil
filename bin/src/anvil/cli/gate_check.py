"""``anvil gate-check`` — finish-gate decision for native agent harnesses (B42 Phase 2).

Read-only. Answers one question for an agent that is about to end its turn:
*do this actor's active claims have evidence satisfying their tasks' verification,
or should the agent be told to finish the work first?*

It exists for OpenClaw's native ``before_agent_finalize`` plugin — anvil's first
blocking gate, stronger than Claude Code's non-blocking hooks. The node plugin
shells out to ``anvil gate-check --json --actor agent --cwd <dir>`` and maps the
result to ``continue`` / ``revise``. Honors the "anvil writes nothing for
OpenClaw" contract: this verb makes no anvil writes for OpenClaw — it only reads
state (opening the backend may apply an idempotent schema migration, same as any
read verb).

Decision (default-OPEN — invisible unless there is a real reason to block):

* not a tracked anvil project / not initialized       → continue (exit 0)
* anvil state unavailable (open OR read error)         → continue (exit 0)
* the actor holds no active claim                       → continue (exit 0)
* every claimed task's evidence is complete             → continue (exit 0)
* ANY claimed task's evidence is missing/incomplete     → **block** (exit 2)

Multiple claims: anvil does not cap claims per actor, so the gate evaluates
**every** active claim the actor holds (deterministically, by task id) and blocks
if any is unverified.

"Complete" uses anvil's own predicate :func:`review.gates.evidence_complete`
(each ``Task.verification.required_evidence`` item must have a corresponding field
on the submitted ``Evidence``). NOTE: anvil's *accept* gate is advisory by default
(``strict_evidence``); this finish-gate enforces evidence **submission**
regardless, to pre-empt the "declare done without evidence" failure mode. And
anvil records no command exit codes on ``Evidence``, so this asserts evidence was
*submitted* for the claimed verification, **not** that the commands exited 0.

Exit codes (so a jq-less host can branch on ``$?``, mirroring ``next -q``)::

    0 = continue    2 = block (revise)    1 = genuine error (invalid ANVIL_ROOT)
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from anvil.cli._helpers import (
    StateRootError,
    _open_backend,
    _resolve_state_dir,
    resolve_actor,
)
from anvil.cli._json import JSON_OPTION, emit_success, fail
from anvil.review.gates import evidence_complete

if TYPE_CHECKING:
    from anvil.state.models import Claim, Evidence, Task

__all__ = ["_read_actor_rows", "decide_from_rows", "gate_check"]

_COMMAND = "gate-check"

_CONTINUE_MSG = "No claimed-but-unverified anvil task; finalization may proceed."


def gate_check(
    actor: str | None = typer.Option(  # noqa: B008
        None,
        "--actor",
        help=(
            "Actor whose active claims to gate. Defaults to $USER or 'agent' "
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
    resolved_actor = resolve_actor(actor)

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

    # db unavailable (missing / locked / corrupt) at OPEN time → cannot gate →
    # continue; never crash a finalize.
    try:
        backend = _open_backend(state_dir)
    except Exception:  # noqa: BLE001 — any open failure means "cannot gate"
        _emit(json_output, quiet, _continue_decision(
            resolved_actor, "anvil state unavailable; finalization may proceed.",
        ))
        return

    # READ the actor's claims+tasks+evidence inside a guard so a read-time db
    # fault (corrupt page, locked db, malformed row) also defaults-open instead
    # of crashing the finalize. The guard is scoped to the *reads* only — the
    # pure decision below runs OUTSIDE it, so a wrong predicate still surfaces in
    # tests (and the real-backend block test would flip to exit 0 and fail).
    rows: list[tuple[Claim, Task | None, Evidence | None]] | None
    try:
        rows = _read_actor_rows(backend, resolved_actor)
    except Exception:  # noqa: BLE001 — read-time fault ⇒ cannot gate ⇒ continue
        rows = None
    finally:
        backend.close()

    if rows is None:
        _emit(json_output, quiet, _continue_decision(
            resolved_actor, "anvil state unavailable; finalization may proceed.",
        ))
        return

    _emit(json_output, quiet, decide_from_rows(resolved_actor, rows))


def decide_from_rows(
    actor: str, rows: list[tuple[Claim, Task | None, Evidence | None]]
) -> dict[str, Any]:
    """The pure finish-gate decision over an actor's claim rows. Shared by
    ``gate-check`` and the Codex ``anvil hook stop-gate`` so the evidence logic
    lives in ONE place. Evaluates EVERY claim; blocks if any is unverified, the
    offending task chosen deterministically (sorted by id — list_active_claims has
    no ORDER BY). Returns the same decision dict ``gate-check`` emits.
    """
    incomplete: list[tuple[str, str, list[str]]] = []  # (task_id, claim_id, missing)
    for claim, task, evidence in rows:
        if task is None:
            continue  # active claim with no task row — anomalous; don't block on it
        if evidence is None:
            required = list(task.verification.required_evidence)
            passed, missing = (not required), required
        else:
            passed, missing = evidence_complete(task, evidence)
        if not passed:
            incomplete.append((task.id, claim.id, missing))

    if not incomplete:
        return _continue_decision(actor, _CONTINUE_MSG)

    incomplete.sort(key=lambda x: x[0])
    task_id, claim_id, missing = incomplete[0]
    others = len(incomplete) - 1
    return {
        "block": True,
        "action": "revise",
        "actor": actor,
        "task": task_id,
        "claim": claim_id,
        "evidence_gate": {"passed": False, "missing": list(missing)},
        "instruction": _block_instruction(task_id, actor, missing, others),
    }


def _read_actor_rows(
    backend: Any, actor: str
) -> list[tuple[Claim, Task | None, Evidence | None]]:
    """Read every active claim held by *actor*, with its task + latest evidence.

    Reads only — the pass/fail judgement happens in the caller (outside the
    read-error guard) so a predicate bug is never masked into a silent continue.
    """
    rows: list[tuple[Claim, Task | None, Evidence | None]] = []
    for claim in backend.list_active_claims():
        if claim.claimed_by != actor:
            continue
        task = backend.get_task(claim.task_id)
        evidence = backend.get_latest_evidence(task.id) if task is not None else None
        rows.append((claim, task, evidence))
    return rows


def _block_instruction(task_id: str, actor: str, missing: list[str], others: int) -> str:
    miss = ", ".join(missing) if missing else "verification evidence"
    extra = (
        f" (and {others} other claimed task(s) also need evidence)" if others else ""
    )
    return (
        f"Task {task_id} is claimed by '{actor}' but its verification evidence is "
        f"incomplete (missing: {miss}){extra}. Run the task's verification commands "
        f"and submit evidence before finishing — e.g. `anvil submit {task_id}` — then "
        f"end your turn. (anvil checks that required evidence was submitted, not that "
        f"commands exited 0.)"
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
