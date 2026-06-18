"""Sample-PRD seeding for ``anvil init --with-sample`` (backlog T004).

This module owns the one-command standalone quickstart: a self-contained
sample ``prd.md`` plus a deterministic, LLM-free runner that drives the
existing engine pipeline (``prd parse`` → ``prd review`` →
``prd review --approve`` → ``plan`` → ``score`` → ``review tasks``) so that
``anvil next`` returns a ready task with no further input.

Design notes
------------
* The sample PRD is embedded as a module constant (``SAMPLE_PRD``) rather than
  shipped as a data file, because the wheel only packages
  ``src/anvil`` (see ``pyproject.toml`` ``[tool.hatch.build.targets.wheel]``)
  and bundling loose data files would require extra packaging config. A string
  constant is always importable from the installed package.
* The PRD already contains a ``## Tasks`` section, so ``plan`` never reaches
  its LLM task-generation backstop — seeding is fully offline and requires no
  ``ANTHROPIC_API_KEY``.
* Each task carries non-empty ``**Acceptance criteria:**`` and
  ``**Verification:**`` blocks, which are exactly the gate that
  ``review tasks`` enforces for the ``drafted → reviewed → ready`` promotion.
  Without them no task would reach ``ready`` and ``next`` would be empty.
* The seeding helpers call into the same engine modules the per-command CLI
  bodies use (``planning.template.parse_prd``, ``planning.inference.infer_all``,
  ``planning.scoring.score_task``, ``state.transitions``) so behaviour cannot
  drift from the hand-run command path.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from anvil.state.sqlite import SqliteBackend


# ---------------------------------------------------------------------------
# Embedded sample PRD
# ---------------------------------------------------------------------------

# A small, self-contained PRD for a fictional "Markdown link checker" CLI.
# It parses cleanly with planning.template.parse_prd (features + requirements
# + tasks), and every task has the acceptance-criteria + verification fields
# the review gate requires, so the full seed run ends with ready tasks.
SAMPLE_PRD = """# Project: Markdown Link Checker

## Summary

A small command-line tool that scans Markdown files for broken local links.
It walks one or more `.md` files, extracts every relative link target, and
reports the ones that do not resolve on disk. Aimed at documentation authors
who want a fast pre-commit check without standing up a full link-checking
service.

## Goals

- Report every relative link in a Markdown file whose target file is missing.
- Accept multiple input files and aggregate the results in one run.
- Exit non-zero when any broken link is found so CI can gate on it.
- Keep output greppable: one broken link per line with file and line number.

## Non-Goals

- Validating external `http(s)://` URLs (network access is out of scope for v1).
- Rewriting or auto-fixing broken links.
- Parsing link syntax inside fenced code blocks.

## Requirements

- R001: The CLI accepts one or more Markdown file paths as positional arguments.
- R002: Each input file is read as UTF-8 and scanned line by line.
- R003: Relative link targets are resolved against the containing file's directory.
- R004: A link whose resolved target does not exist is reported as broken.
- R005: The tool exits 1 when at least one broken link is found, else 0.
- R006: Each broken-link report line includes the source file and line number.

## Acceptance Criteria

- Running `mdlinks README.md` with all links valid prints nothing and exits 0.
- Running `mdlinks README.md` with one missing target prints one line and exits 1.
- Running `mdlinks a.md b.md` aggregates broken links from both files.
- A broken-link line contains the source filename and the 1-based line number.

## Risks

- Markdown link syntax has many edge cases; v1 handles only `[text](path)` links.
- Symbolic links could cause false positives; document the limitation for v1.

## Open Questions

- Should anchor fragments (`path#section`) be validated, or just the file part?

## Features

### F001: Link extraction

Extracts relative link targets from a Markdown file with their line numbers.

**Requirements:** R001, R002, R006

### F002: Link resolution and reporting

Resolves each target on disk, reports the missing ones, and sets the exit code.

**Requirements:** R003, R004, R005

## Tasks

### T001: Implement Markdown link extraction

**Feature:** F001
**Priority:** high
**Likely files:** src/mdlinks/extract.py

Scan a Markdown string line by line and yield each `[text](target)` link as a
`(line_number, target)` pair. Skip absolute `http(s)://` URLs and anchors that
start with `#`. Return relative targets only.

**Acceptance criteria:**

- `extract_links("[a](b.md)")` yields a pair whose target is `b.md`.
- `extract_links("[a](https://x)")` yields nothing (absolute URL skipped).
- Line numbers are 1-based and match the source line of each link.

**Verification:**

- `pytest tests/test_extract.py -v`

### T002: Resolve targets and report broken links

**Feature:** F002
**Priority:** medium
**Likely files:** src/mdlinks/check.py, src/mdlinks/cli.py

For each extracted `(line, target)` pair, resolve the target against the source
file's directory and check existence. Collect the broken ones, print one line
per broken link as `file:line: target`, and exit 1 when any are broken.

**Acceptance criteria:**

- A target that resolves to an existing file is not reported.
- A target that does not resolve is printed as `file:line: target`.
- The process exits 1 when any link is broken and 0 when all resolve.

**Verification:**

- `pytest tests/test_check.py -v`
"""

# The filename the sample PRD is written to inside .anvil/. Kept here
# (rather than imported) so this module has no import cycle with init_status.
_SAMPLE_PRD_FILENAME = "prd.md"


# ---------------------------------------------------------------------------
# Seed runner
# ---------------------------------------------------------------------------


class SampleSeedError(RuntimeError):
    """Raised when the sample-PRD seed pipeline cannot complete.

    Carries a human-actionable message; the CLI surfaces it as a clean
    ``Error: ...`` line (exit 1) rather than a traceback.
    """


def write_sample_prd(state_dir: Path) -> Path:
    """Write the embedded sample PRD to ``<state_dir>/prd.md`` and return its path.

    Overwrites any existing prd.md — ``--with-sample`` is an explicit opt-in
    that owns the file.
    """
    prd_path = state_dir / _SAMPLE_PRD_FILENAME
    prd_path.write_text(SAMPLE_PRD, encoding="utf-8")
    return prd_path


def seed_sample_pipeline(
    backend: SqliteBackend, *, actor: str = "anvil-cli"
) -> dict[str, Any]:
    """Drive parse → plan → score → review entirely offline against ``backend``.

    Reuses the same engine modules the per-command CLI bodies use so the seed
    path cannot drift from the hand-run command sequence:

    1. ``parse_prd`` the embedded PRD (no provider → deterministic, no network).
    2. Emit ``prd.parsed`` then ``prd.reviewed`` / ``prd.approved`` events so
       the PRD lifecycle matches a real review.
    3. Emit ``feature.created`` / ``task.created`` for every parsed entity,
       run dependency + conflict inference, and promote ``proposed → drafted``.
    4. Score every task with the rule-based scorer (no LLM).
    5. Promote ``drafted → reviewed → ready`` through the real transition
       guards (acceptance-criteria + verification gate).

    Returns a small summary dict (counts) for the caller to print. Raises
    :class:`SampleSeedError` if the embedded PRD fails to parse — that would be
    a packaging bug, surfaced cleanly rather than as a traceback.
    """
    return seed_pipeline_from_prd(
        backend,
        SAMPLE_PRD,
        actor=actor,
        parse_error_hint=(
            "This is an anvil packaging bug — please report it."
        ),
    )


def seed_pipeline_from_prd(
    backend: SqliteBackend,
    prd_text: str,
    *,
    actor: str = "anvil-cli",
    review_notes: str = "auto-seeded",
    parse_error_hint: str = "Fix the PRD and re-run.",
) -> dict[str, Any]:
    """Drive parse → plan → score → review offline for an *arbitrary* PRD text.

    This is the generalised engine behind :func:`seed_sample_pipeline` and the
    T008 brownfield ``scan`` command: given any PRD markdown that
    ``planning.template.parse_prd`` accepts (with ``## Features`` / ``## Tasks``
    sections carrying acceptance-criteria + verification), it appends the full
    canonical event sequence so ``anvil next`` returns a ready task — no
    network, no LLM, no API key.

    The seeding steps are identical to (and shared with) the sample path so the
    brownfield path can never drift from the hand-run command sequence. Raises
    :class:`SampleSeedError` (carrying *parse_error_hint*) if *prd_text* fails
    to parse.
    """
    from anvil.clock import SystemClock
    from anvil.planning.inference import infer_all
    from anvil.planning.scoring import score_task
    from anvil.planning.template import parse_prd
    from anvil.state.models import EventDraft
    from anvil.state.transitions import (
        TransitionError,
        task_drafted_to_reviewed,
        task_reviewed_to_ready,
    )

    clock = SystemClock()

    parsed = parse_prd(prd_text, prd_id="prd")
    if parsed.errors:
        detail = "; ".join(
            f"[{e.section}:{e.line}] {e.message}" for e in parsed.errors
        )
        raise SampleSeedError(
            "the PRD failed to parse "
            f"({len(parsed.errors)} error(s)): {detail}. "
            + parse_error_hint
        )

    project_id = backend.get_project().id  # type: ignore[union-attr]

    # --- PRD lifecycle: parsed → reviewed → approved -----------------------
    now = clock.now()
    backend.append(
        EventDraft(
            timestamp=now,
            actor=actor,
            action="prd.parsed",
            target_kind="prd",
            target_id=project_id,
            payload_json={
                "project_id": project_id,
                "status": parsed.prd.status.value,
                "summary": parsed.prd.summary,
                "goals": parsed.prd.goals,
                "non_goals": parsed.prd.non_goals,
                "requirements": [
                    {
                        "id": r.id,
                        "prd_section": r.prd_section,
                        "text": r.text,
                        "source_paragraph": r.source_paragraph,
                        "derived": r.derived,
                    }
                    for r in parsed.requirements
                ],
                "acceptance_criteria": parsed.prd.acceptance_criteria,
                "risks": parsed.prd.risks,
                "open_questions": parsed.prd.open_questions,
            },
        )
    )
    now = clock.now()
    backend.append(
        EventDraft(
            timestamp=now,
            actor=actor,
            action="prd.reviewed",
            target_kind="prd",
            target_id=project_id,
            payload_json={
                "project_id": project_id,
                "reviewer": actor,
                "notes": review_notes,
            },
        )
    )
    now = clock.now()
    backend.append(
        EventDraft(
            timestamp=now,
            actor=actor,
            action="prd.approved",
            target_kind="prd",
            target_id=project_id,
            payload_json={"project_id": project_id, "approver": actor},
        )
    )

    # --- Features + tasks: create → infer → promote to drafted -------------
    for feature in parsed.features:
        now = clock.now()
        backend.append(
            EventDraft(
                timestamp=now,
                actor=actor,
                action="feature.created",
                target_kind="feature",
                target_id=feature.id,
                payload_json=feature.model_dump(mode="json"),
            )
        )

    for task in parsed.tasks:
        now = clock.now()
        backend.append(
            EventDraft(
                timestamp=now,
                actor=actor,
                action="task.created",
                target_kind="task",
                target_id=task.id,
                payload_json=task.model_dump(mode="json"),
            )
        )

    inference_result = infer_all(parsed.tasks)
    for inferred in inference_result.tasks:
        now = clock.now()
        backend.append(
            EventDraft(
                timestamp=now,
                actor=actor,
                action="task.created",
                target_kind="task",
                target_id=inferred.id,
                payload_json=inferred.model_dump(mode="json"),
            )
        )
        current = backend.get_task(inferred.id)
        if current is not None and current.status.value == "proposed":
            now = clock.now()
            backend.append(
                EventDraft(
                    timestamp=now,
                    actor=actor,
                    action="task.status_changed",
                    target_kind="task",
                    target_id=inferred.id,
                    payload_json={
                        "task_id": inferred.id,
                        "from": "proposed",
                        "to": "drafted",
                        "reason": "seed: initial draft after inference",
                    },
                )
            )

    # --- Score every task (rule-based, no LLM) -----------------------------
    for task in backend.list_tasks():
        computed = score_task(task)
        now = clock.now()
        backend.append(
            EventDraft(
                timestamp=now,
                actor=actor,
                action="task.scored",
                target_kind="task",
                target_id=task.id,
                payload_json={
                    "task_id": task.id,
                    "scores": {
                        "complexity": computed.complexity,
                        "parallelizability": computed.parallelizability,
                        "context_load": computed.context_load,
                        "blast_radius": computed.blast_radius,
                        "review_risk": computed.review_risk,
                        "agent_suitability": computed.agent_suitability,
                    },
                    "explanation": computed.explanation,
                },
            )
        )

    # --- Promote drafted → reviewed → ready --------------------------------
    promoted_ready: list[str] = []
    for task in backend.list_tasks():
        if task.status.value != "drafted":
            continue
        now = clock.now()
        try:
            task_drafted_to_reviewed(task, now)
        except TransitionError:
            continue
        backend.append(
            EventDraft(
                timestamp=now,
                actor=actor,
                action="task.status_changed",
                target_kind="task",
                target_id=task.id,
                payload_json={
                    "task_id": task.id,
                    "from": "drafted",
                    "to": "reviewed",
                    "reason": "seed: gate passed",
                },
            )
        )

    for task in backend.list_tasks():
        if task.status.value != "reviewed":
            continue
        now = clock.now()
        try:
            task_reviewed_to_ready(task, now)
        except TransitionError:
            continue
        backend.append(
            EventDraft(
                timestamp=now,
                actor=actor,
                action="task.status_changed",
                target_kind="task",
                target_id=task.id,
                payload_json={
                    "task_id": task.id,
                    "from": "reviewed",
                    "to": "ready",
                    "reason": "seed: promoted to ready",
                },
            )
        )
        promoted_ready.append(task.id)

    return {
        "features": len(parsed.features),
        "tasks": len(parsed.tasks),
        "ready": len(promoted_ready),
        "ready_ids": promoted_ready,
    }
