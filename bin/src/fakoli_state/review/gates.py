"""Review gate functions for fakoli-state.

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

from fakoli_state.state.models import Evidence, Task

__all__ = ["evidence_complete"]


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
