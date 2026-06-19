"""T004 — typed per-step proof gate.

A proof-bearing step passes only when a matching passing CommandProof exists —
the executor's success bool cannot fake it (the SL-3 property). A failing proof
fails the run; a passing proof applies the step; the verdict is recorded in the
evidence so it is reconstructable from the event log.
"""

from __future__ import annotations

from anvil.state.models import TaskStatus
from anvil.workflows.parse import parse_workflow
from anvil.workflows.proof import CommandProof, proofs_satisfied, requirement_satisfied
from anvil.workflows.runner import StepOutcome, WorkflowRunError, run_workflow
from anvil.workflows.schema import Proof

_PROOF_WF = """
name: gated
steps:
  - id: check
    run: "verify the thing"
    proof:
      - command: "run-the-check"
        passing_exit_codes: [0]
"""


# --------------------------------------------------------------------------- #
# Unit: the gate predicate
# --------------------------------------------------------------------------- #


def test_requirement_satisfied_only_by_matching_passing_proof():
    req = Proof(command="pytest", passing_exit_codes=[0])
    assert requirement_satisfied(req, [CommandProof(command="pytest", exit_code=0)])
    # wrong exit code
    assert not requirement_satisfied(req, [CommandProof(command="pytest", exit_code=1)])
    # wrong command
    assert not requirement_satisfied(req, [CommandProof(command="other", exit_code=0)])
    # no proof at all
    assert not requirement_satisfied(req, [])


def test_proofs_satisfied_requires_all():
    reqs = [Proof(command="a"), Proof(command="b")]
    assert not proofs_satisfied(reqs, [CommandProof(command="a", exit_code=0)])
    assert proofs_satisfied(
        reqs,
        [CommandProof(command="a", exit_code=0), CommandProof(command="b", exit_code=0)],
    )


# --------------------------------------------------------------------------- #
# Runner integration
# --------------------------------------------------------------------------- #


def test_failing_proof_fails_the_run(approved_backend, frozen_clock):  # type: ignore[no-untyped-def]
    wf = parse_workflow(_PROOF_WF)

    def exec_fail(step):  # type: ignore[no-untyped-def]
        return StepOutcome(
            proofs=[CommandProof(command="run-the-check", exit_code=1)]
        )

    try:
        run_workflow(approved_backend, wf, executor=exec_fail, actor="r", clock=frozen_clock)
        raise AssertionError("expected the failing proof to abort the run")
    except WorkflowRunError:
        pass


def test_passing_proof_applies_and_is_recorded(approved_backend, frozen_clock):  # type: ignore[no-untyped-def]
    wf = parse_workflow(_PROOF_WF)

    def exec_pass(step):  # type: ignore[no-untyped-def]
        return StepOutcome(
            proofs=[CommandProof(command="run-the-check", exit_code=0)]
        )

    records = run_workflow(
        approved_backend, wf, executor=exec_pass, actor="r", clock=frozen_clock
    )
    assert records[0].status == "applied"
    assert approved_backend.get_task(records[0].task_id).status == TaskStatus.done

    # AC: verdict reconstructable from the event log (evidence payload).
    ev = [e for e in approved_backend.list_evidence() if e.task_id == records[0].task_id]
    assert len(ev) == 1
    assert ev[0].output_excerpt is not None
    assert "exit 0" in ev[0].output_excerpt


def test_success_bool_cannot_fake_a_proof(approved_backend, frozen_clock):  # type: ignore[no-untyped-def]
    """The SL-3 property: claiming success without a CommandProof does not pass."""
    wf = parse_workflow("""
name: nofake
steps:
  - id: check
    run: "verify"
    on_fail: reopen
    proof:
      - command: "must-run"
""")

    # executor lies: success=True but emits NO CommandProof for the requirement.
    def exec_lie(step):  # type: ignore[no-untyped-def]
        return StepOutcome(success=True, proofs=[])

    records = run_workflow(
        approved_backend, wf, executor=exec_lie, actor="r", clock=frozen_clock
    )
    # gate refused → step reopened, not applied
    assert records[0].status == "reopened"
    assert approved_backend.get_task(records[0].task_id).status == TaskStatus.ready
