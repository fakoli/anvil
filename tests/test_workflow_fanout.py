"""T005 — fan_out: create + claim one governed task per item.

fan_out over N items creates N tasks and claims each (expected_files derived
from the item); a failing item respects on_fail without aborting siblings.
"""

from __future__ import annotations

from anvil.state.models import TaskStatus
from anvil.workflows.fanout import fan_out_ref, resolve_items, run_fan_out
from anvil.workflows.parse import parse_workflow
from anvil.workflows.runner import StepOutcome, WorkflowRunError, run_workflow
from anvil.workflows.schema import Step

_FANOUT_WF = """
name: fixall
steps:
  - id: find
    run: "list items"
  - id: fix
    needs: [find]
    fan_out: "${{ steps.find.output }}"
    run: "fix ${{ item }}"
"""


def test_fan_out_ref_parsing():
    assert fan_out_ref("${{ steps.find.output }}") == "find"
    assert fan_out_ref("${{steps.a_b-1.output}}") == "a_b-1"
    assert fan_out_ref("literal") is None


def test_resolve_items_errors_on_unknown_ref():
    step = Step(id="fix", fan_out="${{ steps.missing.output }}", run="x")
    try:
        resolve_items(step, {})
        raise AssertionError("expected WorkflowRunError")
    except WorkflowRunError:
        pass


def test_fan_out_creates_and_claims_one_task_per_item(approved_backend, frozen_clock):  # type: ignore[no-untyped-def]
    items = ["a.py", "b.py", "c.py"]

    def exec_step(step):  # type: ignore[no-untyped-def]
        # 'find' produces the items; 'fix' is fanned out (not called here).
        return StepOutcome(items=items) if step.id == "find" else StepOutcome()

    wf = parse_workflow(_FANOUT_WF)
    records = run_workflow(
        approved_backend, wf, executor=exec_step, actor="r", clock=frozen_clock
    )

    fix_records = [r for r in records if r.step_id.startswith("fix:")]
    assert len(fix_records) == 3  # one task per item
    for rec, item in zip(fix_records, items, strict=True):
        task = approved_backend.get_task(rec.task_id)
        assert task.status == TaskStatus.done
        assert task.likely_files == [item]  # expected_files derived from item


def test_fan_out_expected_files_on_the_claim(approved_backend, frozen_clock):  # type: ignore[no-untyped-def]
    """The claim carries expected_files=[item] — the lease/conflict surface."""
    step = Step(id="fix", fan_out="${{ steps.find.output }}", run="fix ${{ item }}")
    records = run_fan_out(
        approved_backend, step, ["x.py", "y.py"],
        fan_out_executor=lambda s, item: StepOutcome(),
        actor="r", clock=frozen_clock, reviewer="r",
    )
    assert len(records) == 2
    # each created task is done and scoped to its item
    files = sorted(approved_backend.get_task(r.task_id).likely_files[0] for r in records)
    assert files == ["x.py", "y.py"]


def test_failing_item_does_not_abort_siblings(approved_backend, frozen_clock):  # type: ignore[no-untyped-def]
    step = Step(
        id="fix", fan_out="${{ steps.find.output }}", run="fix", on_fail="reopen"
    )

    def exec_item(s, item):  # type: ignore[no-untyped-def]
        return StepOutcome(success=item != "bad.py")

    records = run_fan_out(
        approved_backend, step, ["ok1.py", "bad.py", "ok2.py"],
        fan_out_executor=exec_item,
        actor="r", clock=frozen_clock, reviewer="r",
    )
    by_item = {r.step_id: r for r in records}
    assert by_item["fix:ok1.py"].status == "applied"
    assert by_item["fix:ok2.py"].status == "applied"  # sibling after the failure ran
    assert by_item["fix:bad.py"].status == "reopened"  # reopened, not aborting
    assert (
        approved_backend.get_task(by_item["fix:bad.py"].task_id).status
        == TaskStatus.ready
    )


def test_failing_item_without_reopen_is_recorded_failed_not_raised(
    approved_backend, frozen_clock
):  # type: ignore[no-untyped-def]
    step = Step(id="fix", fan_out="${{ steps.find.output }}", run="fix")  # no on_fail
    records = run_fan_out(
        approved_backend, step, ["ok.py", "bad.py"],
        fan_out_executor=lambda s, item: StepOutcome(success=item == "ok.py"),
        actor="r", clock=frozen_clock, reviewer="r",
    )
    by_item = {r.step_id: r for r in records}
    assert by_item["fix:ok.py"].status == "applied"
    assert by_item["fix:bad.py"].status == "failed"  # recorded, siblings not aborted
