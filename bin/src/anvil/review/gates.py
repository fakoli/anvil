"""Review gate functions for anvil.

Gates are pure functions — no I/O, no database, no side-effects.
They answer: "may this transition proceed?" and explain what is missing.

Design:
- Each gate returns (passed: bool, missing_items: list[str]).
- Empty missing_items means the gate passed.
- The CLI ``apply`` command calls these gates BEFORE the human approves,
  so the reviewer is shown a complete picture of what is lacking.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from anvil.state.models import Evidence, Review, ReviewDecision, Task

__all__ = [
    "DeferredFinding",
    "deferred_findings",
    "deferred_findings_for_files",
    "evidence_complete",
]


# ---------------------------------------------------------------------------
# T017 — Surface deferred / failed-review evidence on file overlap.
#
# When a reviewer rejects (or requests changes on) a task at the finish gate,
# that verdict is recorded as a ``Review`` row (``reject`` / ``needs_changes``)
# whose ``notes`` carry the finding text and whose ``target_id`` is the task.
# The files that finding touched are the reviewed task's ``likely_files`` plus
# the ``files_changed`` of whatever evidence the agent submitted on it.
#
# A later task that intends to modify one of those same files should not start
# blind: the prior unresolved finding is exactly the context an agent needs.
# These functions are PURE (no I/O, no DB) — the CLI / MCP layer reads the
# ``reviews`` / ``tasks`` / ``evidence`` rows from the backend and hands them in;
# the work-packet renderer (``context.packets``) surfaces the returned findings
# whose touched files overlap the incoming claim's ``expected_files``.
# ---------------------------------------------------------------------------

# A review whose decision is one of these is an unresolved (deferred / failed)
# finding worth surfacing. ``approve`` is excluded — an approved task carries no
# outstanding finding. ``reject`` auto-reopens the task for rework (it lands at
# ``drafted``), so its finding stays live until the rework is re-reviewed.
_DEFERRED_DECISIONS: frozenset[str] = frozenset(
    {ReviewDecision.reject.value, ReviewDecision.needs_changes.value}
)


@dataclass(frozen=True)
class DeferredFinding:
    """A deferred / failed-review finding linked to the files it touched.

    Attributes:
        review_id:   The originating ``Review`` row id (stable, deterministic).
        task_id:     The task the finding was raised against.
        decision:    The review decision (``reject`` or ``needs_changes``).
        notes:       The reviewer's finding text (may be ``None``).
        files:       Sorted, de-duplicated files the finding touched — the
                     reviewed task's ``likely_files`` plus any submitted
                     evidence's ``files_changed``.
        overlapping_files:
                     When produced by :func:`deferred_findings_for_files`, the
                     subset of ``files`` that overlapped the queried
                     ``expected_files`` (sorted). Empty otherwise.
    """

    review_id: str
    task_id: str
    decision: str
    notes: str | None
    files: list[str]
    overlapping_files: list[str] = field(default_factory=list)


def deferred_findings(
    reviews: list[Review],
    tasks: list[Task],
    evidence: list[Evidence],
) -> list[DeferredFinding]:
    """Build queryable deferred / failed-review findings linked to their files.

    Pure: derives findings from already-loaded engine rows — no I/O.

    For every task-targeted review whose decision is ``reject`` or
    ``needs_changes`` (see :data:`_DEFERRED_DECISIONS`), the finding's files are
    the union of the reviewed task's ``likely_files`` and the ``files_changed``
    of every Evidence submitted on that task. Reviews on non-task targets (PRD,
    feature) and approvals are ignored.

    Args:
        reviews:  All ``Review`` rows (e.g. ``backend.list_reviews()``).
        tasks:    All ``Task`` rows (used to resolve a finding's likely_files).
        evidence: All ``Evidence`` rows (used to resolve files_changed).

    Returns:
        One :class:`DeferredFinding` per qualifying review, in ``reviews`` order
        (which the backend returns deterministically by id). ``overlapping_files``
        is empty on each — use :func:`deferred_findings_for_files` to filter and
        annotate by an incoming claim's expected files.
    """
    tasks_by_id: dict[str, Task] = {t.id: t for t in tasks}
    # task_id -> set of files touched by any evidence submitted on that task.
    evidence_files_by_task: dict[str, set[str]] = {}
    for ev in evidence:
        if ev.files_changed:
            evidence_files_by_task.setdefault(ev.task_id, set()).update(
                ev.files_changed
            )

    findings: list[DeferredFinding] = []
    for review in reviews:
        if review.target_kind.value != "task":
            continue
        if review.decision.value not in _DEFERRED_DECISIONS:
            continue

        task_id = review.target_id
        files: set[str] = set()
        task = tasks_by_id.get(task_id)
        if task is not None:
            files.update(task.likely_files)
        files.update(evidence_files_by_task.get(task_id, set()))

        findings.append(
            DeferredFinding(
                review_id=review.id,
                task_id=task_id,
                decision=review.decision.value,
                notes=review.notes,
                files=sorted(files),
            )
        )

    return findings


def deferred_findings_for_files(
    reviews: list[Review],
    tasks: list[Task],
    evidence: list[Evidence],
    expected_files: list[str],
) -> list[DeferredFinding]:
    """Return deferred findings whose touched files overlap ``expected_files``.

    This is the surface T017 wires into the work packet / claim response: when a
    new task is claimed or planned, the caller passes the incoming claim's
    ``expected_files`` (or the task's ``likely_files``) and gets back every prior
    unresolved finding that touched one of those same files, annotated with the
    exact overlapping subset.

    Pure: no I/O. Deterministic for fixed inputs.

    Args:
        reviews:        All ``Review`` rows.
        tasks:          All ``Task`` rows.
        evidence:       All ``Evidence`` rows.
        expected_files: The files the incoming claim / task intends to touch.

    Returns:
        The subset of :func:`deferred_findings` whose ``files`` intersect
        ``expected_files``, each with ``overlapping_files`` populated (sorted).
        Empty when ``expected_files`` is empty or nothing overlaps.
    """
    if not expected_files:
        return []

    wanted = set(expected_files)
    out: list[DeferredFinding] = []
    for finding in deferred_findings(reviews, tasks, evidence):
        overlap = sorted(wanted & set(finding.files))
        if overlap:
            out.append(
                DeferredFinding(
                    review_id=finding.review_id,
                    task_id=finding.task_id,
                    decision=finding.decision,
                    notes=finding.notes,
                    files=finding.files,
                    overlapping_files=overlap,
                )
            )
    return out


def evidence_complete(task: Task, evidence: Evidence) -> tuple[bool, list[str]]:
    """Validate that Evidence satisfies Task.verification.required_evidence.

    For each item in task.verification.required_evidence (e.g. "test output",
    "PR link", "screenshots"), checks whether the Evidence has corresponding
    content using the following substring-match rules:

    - "test" / "pytest" / "cargo test"   → check evidence.commands_run
    - "PR" / "pull request"              → check evidence.pr_url
    - "screenshot"                       → check evidence.screenshots (non-empty)
    - "files changed"                    → check evidence.files_changed (non-empty)
    - anything else                      → check evidence.output_excerpt OR
                                           evidence.known_limitations

    The match is case-insensitive and uses substring containment ("in").
    Conservative: missing if no plausible match is found for the required item.

    Args:
        task:     The Task whose verification.required_evidence list to check.
        evidence: The Evidence submitted by the agent.

    Returns:
        A tuple (passed, missing_items) where:
        - passed       is True if every required item is satisfied.
        - missing_items is a human-readable list of unsatisfied required items.
                       Empty list means everything passed.

    Usage by ``cli apply``:
        passed, missing = evidence_complete(task, evidence)
        if not passed:
            typer.echo(f"Missing evidence: {missing}", err=True)
    """
    required = task.verification.required_evidence
    if not required:
        return True, []

    missing: list[str] = []

    for item in required:
        item_lower = item.lower()

        if _is_test_related(item_lower):
            # Check commands_run for any test-invoking command.
            satisfied = any(
                _contains_test_keyword(cmd.lower())
                for cmd in evidence.commands_run
            )

        elif _is_pr_related(item_lower):
            # Check evidence.pr_url is set.
            satisfied = bool(evidence.pr_url)

        elif "screenshot" in item_lower:
            # Check evidence.screenshots is non-empty.
            satisfied = bool(evidence.screenshots)

        elif "files changed" in item_lower:
            # Check evidence.files_changed is non-empty.
            satisfied = bool(evidence.files_changed)

        else:
            # Fallback: check output_excerpt or known_limitations contain the item.
            corpus_lower = []
            if evidence.output_excerpt:
                corpus_lower.append(evidence.output_excerpt.lower())
            if evidence.known_limitations:
                corpus_lower.append(evidence.known_limitations.lower())
            satisfied = any(item_lower in text for text in corpus_lower)

        if not satisfied:
            missing.append(item)

    return len(missing) == 0, missing


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _is_test_related(item_lower: str) -> bool:
    """Return True if item_lower refers to test output or a test run."""
    test_keywords = ("test", "pytest", "cargo test")
    return any(kw in item_lower for kw in test_keywords)


_COLLECT_ONLY_RE = re.compile(r"(?<![A-Za-z0-9-])--(?:co|collect-only)(?:[\s=]|$)")


def _contains_test_keyword(cmd_lower: str) -> bool:
    """Return True if a command string actually runs tests.

    Excludes runner invocations that only enumerate / collect tests without
    executing them (e.g. ``pytest --collect-only``, ``pytest --co``), which
    exit 0 with zero tests run and would falsely satisfy a "tests pass"
    evidence gate. Reported in tech-debt-backlog CL-9 (PR #41 Critic-1).

    The collect-only check uses a word-boundary regex so it matches only the
    bare ``--co`` / ``--collect-only`` flags — not ``--color``, ``--config``,
    ``--continue-on-collection-errors``, or any other ``--co*`` flag a real
    test command might use. Greptile + critic PR #48 P1 caught this.
    """
    test_runners = (
        "pytest",
        "cargo test",
        "npm test",
        "npx jest",
        "python -m pytest",
        "python -m unittest",
        "go test",
        "mvn test",
        "gradle test",
        "make test",
        "uv run pytest",
    )
    if not any(runner in cmd_lower for runner in test_runners):
        return False
    if _COLLECT_ONLY_RE.search(cmd_lower):
        return False
    return True


def _is_pr_related(item_lower: str) -> bool:
    """Return True if item_lower refers to a pull request link.

    Uses word-boundary matching for the 'pr' abbreviation. A bare substring
    match was producing false positives on common English words: "improve",
    "sprint", "april", "approve", "process", "spread" — all contain the
    sequence "pr". Required-evidence strings with any of those would falsely
    route to the pr_url check and fail the gate. Greptile + Critic-1 both
    flagged this on PR #41.
    """
    if "pull request" in item_lower:
        return True
    return bool(re.search(r"\bpr\b", item_lower))
