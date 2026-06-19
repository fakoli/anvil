"""``anvil run-workflow <name>`` — drive a declarative workflow (WF-3, T003).

Loads ``.anvil/workflows/<name>.yaml``, parses it (T002), and runs each step
through Anvil's governed transitions via the sequential runner (T003). Runs the
steps and exits — no background process.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import typer

from anvil.cli._helpers import _open_backend, _require_state_dir, _resolve_state_dir
from anvil.workflows.parse import WorkflowParseError, parse_workflow
from anvil.workflows.proof import CommandProof
from anvil.workflows.runner import StepOutcome
from anvil.workflows.runner import run_workflow as _run_workflow
from anvil.workflows.schema import Step


def _default_executor(step: Step) -> StepOutcome:
    """Execute a step's declared ``proof`` commands as the work/verification.

    Running a step's ``run:`` prompt is a harness concern (an agent); this
    in-process default executes the declarative ``proof`` commands and emits a
    typed :class:`CommandProof` (with the real exit code) for each — the runner's
    typed gate (T004) decides pass/fail from those. A step with no proof is
    treated as dispatched.
    """
    if not step.proof:
        return StepOutcome(success=True)
    commands: list[str] = []
    proofs: list[CommandProof] = []
    for proof in step.proof:
        commands.append(proof.command)
        # ponytail: shell=True is deliberate — a proof is the workflow author's
        # own shell command (pipes/args/substitutions are the point), same trust
        # boundary as a CI step or Makefile. The .anvil/workflows/*.yaml file is
        # a committed, trusted artifact, not untrusted input. If workflows ever
        # accept third-party files, gate this behind an explicit --allow-exec.
        result = subprocess.run(proof.command, shell=True, check=False)  # noqa: S602
        proofs.append(CommandProof(command=proof.command, exit_code=result.returncode))
    return StepOutcome(commands_run=commands, proofs=proofs)


def run_workflow(
    name: str = typer.Argument(..., help="Workflow name (file .anvil/workflows/<name>.yaml)."),  # noqa: B008
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory. Defaults to the current working directory.",
        hidden=True,
    ),
) -> None:
    """Run the declarative workflow NAME to completion, then exit.

    Each step is driven through Anvil's governed transitions (create → claim →
    run → submit evidence → apply), producing one evidence row per applied step.
    No background process is started.
    """
    state_dir = _resolve_state_dir(cwd)
    _require_state_dir(state_dir, command="run-workflow", json_output=False)

    wf_path = state_dir / "workflows" / f"{name}.yaml"
    if not wf_path.exists():
        typer.echo(f"Error: no workflow at {wf_path}", err=True)
        raise typer.Exit(code=1)

    try:
        workflow = parse_workflow(wf_path.read_text(encoding="utf-8"))
    except WorkflowParseError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    from anvil.clock import SystemClock

    actor = os.environ.get("USER") or "agent"
    backend = _open_backend(state_dir)
    try:
        records = _run_workflow(
            backend, workflow, executor=_default_executor, actor=actor, clock=SystemClock()
        )
    finally:
        backend.close()

    applied = sum(1 for r in records if r.status == "applied")
    reopened = sum(1 for r in records if r.status == "reopened")
    typer.echo(
        f"Workflow '{workflow.name}': {applied} step(s) applied, "
        f"{reopened} reopened, {len(records)} total."
    )
