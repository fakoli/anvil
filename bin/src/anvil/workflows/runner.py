"""Sequential WF-3 runner (T003) — drive each step through Anvil's transitions.

For each step (in `needs` order) the runner: creates a workflow-origin task
(T003.1), claims it (single-winner lease), runs the step via an injected
``executor``, then submits evidence and applies — one `Evidence` row per step.
The runner runs the steps and returns; it starts no background process.

The ``executor`` is the seam where the *work* happens. Running a step's `run:`
prompt is a harness concern (an agent), so it is injected rather than baked in;
the CLI supplies a default that executes any declared `proof` commands. This
keeps the engine-side governance (claim → evidence → apply) testable without an
LLM in the loop.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from anvil.claims.manager import ClaimManager
from anvil.workflows.proof import CommandProof, proofs_satisfied
from anvil.workflows.tasks import (
    apply_workflow_task,
    create_workflow_task,
    submit_workflow_evidence,
)

if TYPE_CHECKING:
    from anvil.clock import Clock
    from anvil.state.sqlite import SqliteBackend
    from anvil.workflows.schema import Step, Workflow

__all__ = [
    "Executor",
    "StepOutcome",
    "StepRecord",
    "WorkflowRunError",
    "run_workflow",
]


class WorkflowRunError(Exception):
    """A step failed and its `on_fail` policy aborts the run."""


@dataclass
class StepOutcome:
    """What an executor reports back for one step.

    ``proofs`` carries typed :class:`CommandProof`s. For a step that declares
    ``proof`` requirements, the runner ignores ``success`` and gates on the
    typed proofs instead (T004) — "does a passing CommandProof exist," not "did
    the executor say so."
    """

    success: bool = True
    commands_run: list[str] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)
    proofs: list[CommandProof] = field(default_factory=list)


@dataclass
class StepRecord:
    """The runner's record of how one step resolved."""

    step_id: str
    task_id: str
    status: str  # "applied" | "reopened"
    evidence_id: str | None = None


Executor = Callable[["Step"], StepOutcome]


def _toposort(steps: list[Step]) -> list[Step]:
    """Order steps so every `needs` dependency precedes its dependents.

    Preserves declared order among independent steps (stable). Raises on a
    cycle — the parser already rejects unknown ids, so the only failure here is
    a true cycle.
    """
    by_id = {s.id: s for s in steps}
    ordered: list[Step] = []
    done: set[str] = set()
    visiting: set[str] = set()

    def visit(step: Step) -> None:
        if step.id in done:
            return
        if step.id in visiting:
            raise WorkflowRunError(f"workflow has a dependency cycle at step '{step.id}'")
        visiting.add(step.id)
        for dep in step.needs:
            visit(by_id[dep])
        visiting.discard(step.id)
        done.add(step.id)
        ordered.append(step)

    for step in steps:
        visit(step)
    return ordered


def run_workflow(
    backend: SqliteBackend,
    workflow: Workflow,
    *,
    executor: Executor,
    actor: str,
    clock: Clock,
    reviewer: str | None = None,
) -> list[StepRecord]:
    """Run ``workflow`` to completion, returning one record per step.

    Each successful step yields exactly one `Evidence` row (submit → apply). A
    failing step whose `on_fail` is ``reopen`` releases its task back to `ready`
    and the run continues; any other failure raises :class:`WorkflowRunError`.
    """
    reviewer = reviewer or actor
    records: list[StepRecord] = []

    for step in _toposort(workflow.steps):
        action = step.run or step.uses_code or step.id
        task_id = create_workflow_task(
            backend,
            title=step.id,
            description=action,
            actor=actor,
            clock=clock,
            run_name=workflow.name,
            step_id=step.id,
        )
        mgr = ClaimManager(backend, clock, actor=actor)
        claim = mgr.claim(task_id).claim

        outcome = executor(step)

        # Typed gate (T004): a proof-bearing step passes only if every `proof`
        # requirement has a matching passing CommandProof — the executor's
        # `success` bool is not trusted for these steps. No-proof steps fall
        # back to the bool.
        passed = (
            proofs_satisfied(step.proof, outcome.proofs)
            if step.proof
            else outcome.success
        )

        if not passed:
            mgr.release(claim.id, reason=f"step '{step.id}' failed")
            if step.on_fail == "reopen":
                records.append(StepRecord(step.id, task_id, "reopened"))
                continue
            raise WorkflowRunError(
                f"step '{step.id}' failed its proof gate and on_fail is not "
                "'reopen'; aborting run"
            )

        # Evidence requires non-empty commands AND files; record the declarative
        # action / an explicit "(none)" when the executor reported none (e.g. a
        # run-only step with no proof, or a check that changed no files).
        commands = outcome.commands_run or [f"run: {action}"]
        files = outcome.files_changed or ["(none)"]
        # Record the typed proof verdict so the pass is reconstructable from the
        # event log (the evidence.submitted payload), not just inferred.
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
        records.append(StepRecord(step.id, task_id, "applied", evidence_id))

    return records
