"""fan_out — create + claim one governed task per item (T005).

A step with a ``fan_out`` reference (e.g. ``${{ steps.find.output }}``) expands
into N governed tasks, one per item from the referenced step's output. Each task
claims with ``expected_files`` derived from its item, exercising the
single-winner lease + file-conflict wedge under parallel-shaped load.

Items are independent: a failing item respects the step's ``on_fail`` policy but
never aborts its siblings — every item is attempted.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING

from anvil.claims.manager import ClaimManager
from anvil.workflows.proof import proofs_satisfied
from anvil.workflows.runner import StepOutcome, StepRecord, WorkflowRunError
from anvil.workflows.tasks import (
    apply_workflow_task,
    create_workflow_task,
    submit_workflow_evidence,
)

if TYPE_CHECKING:
    from anvil.clock import Clock
    from anvil.state.sqlite import SqliteBackend
    from anvil.workflows.schema import Step

__all__ = ["FanOutExecutor", "fan_out_ref", "resolve_items", "run_fan_out"]

FanOutExecutor = Callable[["Step", str], StepOutcome]

_REF_RE = re.compile(r"^\$\{\{\s*steps\.([A-Za-z0-9_-]+)\.output\s*\}\}$")


def fan_out_ref(expr: str) -> str | None:
    """Return the step id a ``${{ steps.<id>.output }}`` expression references,
    or None if the expression is not a recognised step-output reference."""
    m = _REF_RE.match(expr.strip())
    return m.group(1) if m else None


def resolve_items(step: Step, step_outputs: dict[str, list[str]]) -> list[str]:
    """Resolve a step's ``fan_out`` expression to a list of items."""
    assert step.fan_out is not None  # caller guarantees
    ref = fan_out_ref(step.fan_out)
    if ref is None:
        raise WorkflowRunError(
            f"step '{step.id}': unsupported fan_out expression {step.fan_out!r}; "
            "expected ${{ steps.<id>.output }}"
        )
    if ref not in step_outputs:
        raise WorkflowRunError(
            f"step '{step.id}': fan_out references step '{ref}' which produced no output"
        )
    return step_outputs[ref]


def run_fan_out(
    backend: SqliteBackend,
    step: Step,
    items: list[str],
    *,
    fan_out_executor: FanOutExecutor,
    actor: str,
    clock: Clock,
    reviewer: str,
) -> list[StepRecord]:
    """Create + claim + drive one governed task per item; return a record each.

    A failing item is reopened (``on_fail: reopen``) or recorded ``failed``, but
    never aborts the remaining items.
    """
    records: list[StepRecord] = []
    for item in items:
        task_id = create_workflow_task(
            backend,
            title=f"{step.id}:{item}",
            description=step.run or step.id,
            actor=actor,
            clock=clock,
            likely_files=[item],
            step_id=step.id,
        )
        mgr = ClaimManager(backend, clock, actor=actor)
        # expected_files derived from the item — this is what exercises the
        # single-winner lease + file-conflict guarantee under fan-out.
        claim = mgr.claim(task_id, expected_files=[item]).claim

        outcome = fan_out_executor(step, item)
        passed = (
            proofs_satisfied(step.proof, outcome.proofs)
            if step.proof
            else outcome.success
        )

        if not passed:
            mgr.release(claim.id, reason=f"fan_out item '{item}' failed")
            status = "reopened" if step.on_fail == "reopen" else "failed"
            records.append(StepRecord(f"{step.id}:{item}", task_id, status))
            continue

        commands = outcome.commands_run or [f"run: {step.run or item}"]
        files = outcome.files_changed or [item]
        proof_excerpt = (
            "; ".join(f"{p.command} -> exit {p.exit_code}" for p in outcome.proofs)
            or None
        )
        evidence_id = submit_workflow_evidence(
            backend,
            task_id=task_id,
            claim_id=claim.id,
            actor=actor,
            clock=clock,
            commands=commands,
            files_changed=files,
            output_excerpt=proof_excerpt,
        )
        apply_workflow_task(backend, task_id=task_id, reviewer=reviewer, clock=clock)
        records.append(StepRecord(f"{step.id}:{item}", task_id, "applied", evidence_id))

    return records
