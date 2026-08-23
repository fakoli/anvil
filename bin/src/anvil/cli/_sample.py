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

import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from anvil.state.backend import EventRejected

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

# Keep one canonical byte representation for both publication and lifecycle
# approval.  Writing the text directly would let Windows translate LF to CRLF
# while the seed pipeline continued hashing the LF-only embedded string.
_SAMPLE_PRD_BYTES = SAMPLE_PRD.encode("utf-8")

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

    def __init__(self, message: str, *, code: str = "sample_seed_error") -> None:
        super().__init__(message)
        self.code = code


def write_sample_prd(state_dir: Path) -> Path:
    """Write the embedded sample PRD to ``<state_dir>/prd.md`` and return its path.

    Overwrites any existing prd.md — ``--with-sample`` is an explicit opt-in
    that owns the file.
    """
    prd_path = state_dir / _SAMPLE_PRD_FILENAME
    prd_path.write_bytes(_SAMPLE_PRD_BYTES)
    return prd_path


def seed_sample_pipeline(
    backend: SqliteBackend,
    *,
    actor: str = "anvil-cli",
    project_root: Path | None = None,
    prd_path: Path | None = None,
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
    prd_text = SAMPLE_PRD
    if prd_path is not None:
        from anvil.cli._helpers import PrdSourceIngestError, ingest_prd_source

        try:
            published = ingest_prd_source(prd_path)
        except PrdSourceIngestError as exc:
            raise SampleSeedError(
                f"cannot verify the published sample PRD: {exc}"
            ) from None
        if published.source_bytes != _SAMPLE_PRD_BYTES:
            raise SampleSeedError(
                "the published sample PRD changed before lifecycle approval"
            )
        prd_text = published.markdown

    return seed_pipeline_from_prd(
        backend,
        prd_text,
        actor=actor,
        project_root=project_root,
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
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Persist the complete deterministic seed pipeline as one atomic event."""
    try:
        with backend.collect_planning_batch(actor=actor) as atomic_backend:
            return _seed_pipeline_from_prd_unbatched(
                atomic_backend,
                prd_text,
                actor=actor,
                review_notes=review_notes,
                parse_error_hint=parse_error_hint,
                project_root=project_root,
            )
    except EventRejected as exc:
        raise SampleSeedError(f"the PRD seed was rejected: {exc}") from None


def _seed_pipeline_from_prd_unbatched(
    backend: SqliteBackend,
    prd_text: str,
    *,
    actor: str = "anvil-cli",
    review_notes: str = "auto-seeded",
    parse_error_hint: str = "Fix the PRD and re-run.",
    project_root: Path | None = None,
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
    from anvil.planning.inference import BundlePlanningError, infer_all
    from anvil.planning.prd_persistence import (
        material_content_sha256,
        source_binding,
    )
    from anvil.planning.scoring import require_complete_score, score_task
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
        from anvil.planning.diagnostics import format_parse_error_summary

        detail = format_parse_error_summary(parsed.errors)
        raise SampleSeedError(
            "the PRD failed to parse "
            f"({len(parsed.errors)} error(s)): {detail}. "
            + parse_error_hint
        )

    source_bytes = prd_text.encode("utf-8")
    source = SimpleNamespace(
        source_bytes=source_bytes,
        markdown=prd_text,
        source_sha256=hashlib.sha256(source_bytes).hexdigest(),
        source_size_bytes=len(source_bytes),
        source_encoding="utf-8",
    )
    material_sha256 = material_content_sha256(source, parsed.prd.title)

    # Path identity is the only host-sensitive part of seeding.  Complete it
    # before the first PRD/feature/task event append so loader, mapping,
    # comparison, and collision-limit failures leave canonical state exactly
    # unchanged.  The bounded seed error is shared by sample, scan, and
    # init-from-repo callers instead of leaking a native exception.
    try:
        inference_result = infer_all(parsed.tasks, project_root=project_root)
    except BundlePlanningError as exc:
        raise SampleSeedError(
            f"seed planning inference refused: {exc}",
            code="path_identity_error",
        ) from None

    project_id = backend.get_project().id  # type: ignore[union-attr]

    # --- PRD lifecycle: parsed/revised → reviewed → approved ---------------
    now = clock.now()
    stored_prd_id = parsed.prd.id
    existing_prd = backend.get_prd(stored_prd_id)
    new_requirements = [
        {
            "id": requirement.id,
            "prd_section": requirement.prd_section,
            "text": requirement.text,
            "source_paragraph": requirement.source_paragraph,
            "derived": requirement.derived,
        }
        for requirement in parsed.requirements
    ]
    common_payload: dict[str, object] = {
        "project_id": project_id,
        "title": parsed.prd.title,
        "summary": parsed.prd.summary,
        "goals": parsed.prd.goals,
        "non_goals": parsed.prd.non_goals,
        "acceptance_criteria": parsed.prd.acceptance_criteria,
        "risks": parsed.prd.risks,
        "open_questions": parsed.prd.open_questions,
        "assumptions": [item.model_dump() for item in parsed.prd.assumptions],
        "material_sha256": material_sha256,
    }
    if existing_prd is None:
        content_action = "prd.parsed"
        content_payload = {
            **common_payload,
            "expected_absent": True,
            "status": parsed.prd.status.value,
            "requirements": new_requirements,
            **source_binding(source, 1),
        }
    else:
        live_requirements = backend.list_requirements(prd_id=stored_prd_id)
        all_requirements = backend.list_requirements(
            prd_id=stored_prd_id, include_superseded=True
        )
        live_by_id = {item.id: item for item in live_requirements}
        all_ids = {item.id for item in all_requirements}
        new_by_id = {str(item["id"]): item for item in new_requirements}
        readded_retired = sorted(
            requirement_id
            for requirement_id in new_by_id
            if requirement_id in all_ids and requirement_id not in live_by_id
        )
        if readded_retired:
            raise SampleSeedError(
                "the PRD reuses retired requirement id(s): "
                + ", ".join(readded_retired)
                + ". Use fresh ids before re-seeding."
            )
        content_action = "prd.revised"
        content_payload = {
            **common_payload,
            "prd_id": stored_prd_id,
            "revision": existing_prd.revision + 1,
            "expected_status": existing_prd.status.value,
            "is_default": existing_prd.is_default,
            "target_version": existing_prd.target_version,
            "target_tag": existing_prd.target_tag,
            "status": existing_prd.status.value,
            "requirements_added": [
                item for item in new_requirements if item["id"] not in all_ids
            ],
            "requirements_unchanged": [
                new_by_id[requirement_id]
                for requirement_id in live_by_id
                if requirement_id in new_by_id
            ],
            "requirements_superseded": [
                {
                    "id": item.id,
                    "prd_section": item.prd_section,
                    "text": item.text,
                    "source_paragraph": item.source_paragraph,
                    "derived": item.derived,
                }
                for item in live_requirements
                if item.id not in new_by_id
            ],
            **source_binding(source, existing_prd.revision + 1),
        }
        if (
            existing_prd.content_available
            and existing_prd.source_sha256 is not None
            and existing_prd.material_sha256 is not None
            and existing_prd.content_event_id is not None
        ):
            content_payload.update({
                "lineage_version": 1,
                "parent_revision": existing_prd.revision,
                "parent_source_sha256": existing_prd.source_sha256,
                "parent_material_sha256": existing_prd.material_sha256,
                "parent_content_event_id": existing_prd.content_event_id,
                "expected_lifecycle_revision": existing_prd.lifecycle_revision,
                "expected_lifecycle_source_sha256": (
                    existing_prd.lifecycle_source_sha256
                ),
                "expected_lifecycle_material_sha256": (
                    existing_prd.lifecycle_material_sha256
                ),
                "expected_lifecycle_content_event_id": (
                    existing_prd.lifecycle_content_event_id
                ),
            })
    try:
        backend.append(
            EventDraft(
                timestamp=now,
                actor=actor,
                action=content_action,
                target_kind="prd",
                target_id=(
                    project_id if content_action == "prd.parsed" else stored_prd_id
                ),
                payload_json=content_payload,
            )
        )
        current_prd = backend.get_prd(stored_prd_id)
        if current_prd is None:
            raise SampleSeedError("the PRD seed did not create projection state")
        if current_prd.status.value == "draft":
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
                        "expected_revision": current_prd.revision,
                        "expected_status": "draft",
                        "binding_version": 1,
                        "source_sha256": current_prd.source_sha256,
                        "material_sha256": current_prd.material_sha256,
                        "content_event_id": current_prd.content_event_id,
                        "reviewer": actor,
                        "notes": review_notes,
                    },
                )
            )
            current_prd = backend.get_prd(stored_prd_id)
        if current_prd is not None and current_prd.status.value == "reviewed":
            now = clock.now()
            backend.append(
                EventDraft(
                    timestamp=now,
                    actor=actor,
                    action="prd.approved",
                    target_kind="prd",
                    target_id=project_id,
                    payload_json={
                        "project_id": project_id,
                        "expected_revision": current_prd.revision,
                        "expected_status": "reviewed",
                        "binding_version": 1,
                        "source_sha256": current_prd.source_sha256,
                        "material_sha256": current_prd.material_sha256,
                        "content_event_id": current_prd.content_event_id,
                        "review_event_id": current_prd.review_event_id,
                        "approver": actor,
                    },
                )
            )
    except EventRejected as exc:
        raise SampleSeedError(f"the PRD seed was rejected: {exc}") from None

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
        computed = require_complete_score(score_task(task))
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
        # Mirror the review-tasks gate (T009) via the shared helper so the two
        # promotion paths cannot drift: a seeded ready task carries CONFIRMED
        # risk scores, so the B45 ceiling is live on the sample project too.
        from anvil.cli.plan import confirm_task_risk_scores

        confirm_task_risk_scores(backend, task, now, actor)
        promoted_ready.append(task.id)

    return {
        "features": len(parsed.features),
        "tasks": len(parsed.tasks),
        "ready": len(promoted_ready),
        "ready_ids": promoted_ready,
    }
