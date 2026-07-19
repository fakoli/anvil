"""Deterministic, advisory behavioural-readiness checks for parsed PRDs.

This module deliberately performs no I/O and mutates no state. It helps an
author turn a PRD into a behaviour-first contract before design and task work
begin, without making a particular prose grammar mandatory.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from typing import Literal

from anvil.planning.template import ParseResult, parse_acceptance_grammar

__all__ = ["BehavioralFinding", "assess_behavioral_readiness", "findings_as_dicts"]


@dataclass(frozen=True)
class BehavioralFinding:
    """One stable, explainable advisory readiness finding."""

    id: str
    category: str
    severity: Literal["info", "warning"]
    location: str
    message: str
    challenge_question: str


_ACTOR_RE = re.compile(
    r"\b(?:user|customer|client|operator|administrator|admin|developer|"
    r"engineer|maintainer|team|reader|member|viewer)s?\b",
    re.IGNORECASE,
)
_OUTCOME_RE = re.compile(
    r"\b(?:reduc(?:e|es|ed|ing)|increas(?:e|es|ed|ing)|"
    r"complet(?:e|es|ed|ing)|publish(?:es|ed|ing)?|measur(?:e|es|ed|ing)|"
    r"report(?:s|ed|ing)?|track(?:s|ed|ing)?|return(?:s|ed|ing)?|"
    r"display(?:s|ed|ing)?|notif(?:y|ies|ied|ying)|allow(?:s|ed|ing)?|"
    r"prevent(?:s|ed|ing)?|export(?:s|ed|ing)?|import(?:s|ed|ing)?|"
    r"creat(?:e|es|ed|ing)|updat(?:e|es|ed|ing)|delet(?:e|es|ed|ing)|"
    r"validat(?:e|es|ed|ing)|reject(?:s|ed|ing)?|accept(?:s|ed|ing)?|"
    r"sav(?:e|es|ed|ing)|retriev(?:e|es|ed|ing)|search(?:es|ed|ing)?|"
    r"filter(?:s|ed|ing)?|record(?:s|ed|ing)?|visible|shown|available|"
    r"view(?:s|ed|ing)?|read(?:s|ing)?|open(?:s|ed|ing)?|"
    r"exists?|exits?)\b",
    re.IGNORECASE,
)
_EXPLICIT_OBSERVABLE_RE = re.compile(
    r"\b(?:return(?:s|ed|ing)?|display(?:s|ed|ing)?|notif(?:y|ies|ied|ying)|"
    r"allow(?:s|ed|ing)?|prevent(?:s|ed|ing)?|export(?:s|ed|ing)?|"
    r"reject(?:s|ed|ing)?|accept(?:s|ed|ing)?|visible|shown|available|"
    r"exists?|exits?|status|response|output|message)\b",
    re.IGNORECASE,
)
_VAGUE_OUTCOME_RE = re.compile(
    r"\b(?:better|improve|seamless|easy|fast|robust|reliable|intuitive|"
    r"high[ -]?quality)\b",
    re.IGNORECASE,
)
_IMPLEMENTATION_RE = re.compile(
    r"\b(?:implement|refactor|class|function|method|database|schema|table|"
    r"endpoint|api|postgres|sqlite|redis|react|typescript|python|library|"
    r"framework|migration)\b",
    re.IGNORECASE,
)
_BOUNDARY_RE = re.compile(
    r"\b(?:error|invalid|missing|empty|failure|fail|den(?:y|ied)|reject|"
    r"limit|duplicate|conflict|unauthori[sz]ed|not found|timeout|offline)\b",
    re.IGNORECASE,
)


def _finding(
    identifier: str,
    category: str,
    severity: Literal["info", "warning"],
    location: str,
    message: str,
    question: str,
) -> BehavioralFinding:
    return BehavioralFinding(
        id=identifier,
        category=category,
        severity=severity,
        location=location,
        message=message,
        challenge_question=question,
    )


def assess_behavioral_readiness(result: ParseResult) -> list[BehavioralFinding]:
    """Assess a successfully parsed PRD with fixed-order, advisory heuristics.

    The checks intentionally err on the side of a useful question. Free-form
    criteria and technical requirements remain valid Anvil inputs; the output
    merely makes the trade-off visible to an author who opted to inspect it.
    """
    prd = result.prd
    findings: list[BehavioralFinding] = []

    if not _ACTOR_RE.search(prd.summary):
        findings.append(
            _finding(
                "BR-001",
                "user_context",
                "warning",
                "## Summary",
                "The summary does not name the person or role whose behaviour should change.",
                "Who is the primary user or operator, and what situation are they in?",
            )
        )
    goal_text = " ".join(prd.goals)
    if not _OUTCOME_RE.search(goal_text) or _VAGUE_OUTCOME_RE.search(goal_text):
        findings.append(
            _finding(
                "BR-002",
                "outcome_clarity",
                "warning",
                "## Goals",
                "Goals do not yet express a concrete, observable outcome.",
                "What would a user be able to do or observe when this succeeds?",
            )
        )
    if not prd.non_goals:
        findings.append(
            _finding(
                "BR-003",
                "scope_boundary",
                "warning",
                "## Non-Goals",
                "No non-goals are declared, so the delivery boundary is unclear.",
                "What adjacent work is explicitly out of scope for this release?",
            )
        )
    if not prd.risks:
        findings.append(
            _finding(
                "BR-004",
                "risk_boundary",
                "info",
                "## Risks",
                "No risks or operational boundaries are declared.",
                "Which failure, dependency, or rollout risk should shape the design?",
            )
        )

    for requirement in result.requirements:
        has_observable_result = bool(
            _ACTOR_RE.search(requirement.text)
            and (
                _OUTCOME_RE.search(requirement.text)
                or _EXPLICIT_OBSERVABLE_RE.search(requirement.text)
            )
        )
        if _IMPLEMENTATION_RE.search(requirement.text) and not has_observable_result:
            findings.append(
                _finding(
                    f"BR-REQ-{requirement.id}-IMPLEMENTATION",
                    "implementation_led_requirement",
                    "info",
                    f"## Requirements > {requirement.id}",
                    "This requirement describes a technical mechanism without a "
                    "stated user-observable result.",
                    "What behaviour must this mechanism enable, and for whom?",
                )
            )

    all_criteria: list[tuple[str, str, list[str]]] = []
    if prd.acceptance_criteria:
        all_criteria.append(("PRD", "## Acceptance Criteria", prd.acceptance_criteria))
    for task in result.tasks:
        if task.acceptance_criteria:
            all_criteria.append(
                (
                    f"TASK-{task.id}",
                    f"## Tasks > {task.id} > Acceptance criteria",
                    task.acceptance_criteria,
                )
            )

    if not all_criteria:
        findings.append(
            _finding(
                "BR-005",
                "observable_acceptance",
                "warning",
                "## Acceptance Criteria",
                "No acceptance criteria describe the behaviour that would prove "
                "the PRD is working.",
                "What observable scenario would demonstrate success from the user’s perspective?",
            )
        )
    else:
        criteria_text: list[str] = []
        for source_id, location, criteria in all_criteria:
            criteria_text.extend(criteria)
            token_counts: dict[str, int] = {}
            logical_clauses = parse_acceptance_grammar(criteria)
            normalized_criteria = [" ".join(item.casefold().split()) for item in criteria]
            structured_groups: dict[int, tuple[str, int]] = {}
            cursor = 0
            for clause in logical_clauses:
                clause_members = clause.text.splitlines()
                member_indexes: list[int] = []
                for member in clause_members:
                    normalized_member = " ".join(member.casefold().split())
                    while (
                        cursor < len(normalized_criteria)
                        and normalized_criteria[cursor] != normalized_member
                    ):
                        cursor += 1
                    if cursor >= len(normalized_criteria):
                        break
                    member_indexes.append(cursor)
                    cursor += 1
                if clause.kind == "freeform" or not member_indexes:
                    continue
                response = (
                    clause.clauses.get("then")
                    or clause.clauses.get("shall")
                    or clause.text
                )
                response_index = member_indexes[-1]
                for member, member_index in zip(
                    clause_members, member_indexes, strict=False
                ):
                    if re.match(r"^\s*then\b", member, re.IGNORECASE):
                        response_index = member_index
                        break
                for member_index in member_indexes:
                    structured_groups[member_index] = (response, response_index)
            for criterion_index, criterion in enumerate(criteria, start=1):
                criterion_location = f"{location} [{criterion_index}]"
                normalized = " ".join(criterion.casefold().split())
                token = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:10]
                token_counts[token] = token_counts.get(token, 0) + 1
                criterion_id = f"{source_id}-{token}-{token_counts[token]:02d}"
                structured_group = structured_groups.get(criterion_index - 1)
                is_noise = bool(
                    re.match(r"^\s*(?:scenario|feature)\s*:", criterion, re.IGNORECASE)
                )
                if structured_group is None and not is_noise:
                    findings.append(
                        _finding(
                            f"BR-AC-{criterion_id}-UNSTRUCTURED",
                            "unstructured_acceptance",
                            "info",
                            criterion_location,
                            "This acceptance criterion is free-form; it may be "
                            "harder to turn into a repeatable behavioural test.",
                            "Can it be phrased as Given/When/Then or When…then…?",
                        )
                    )
                outcome_text = structured_group[0] if structured_group else criterion
                should_assess_outcome = (
                    structured_group is None
                    or structured_group[1] == criterion_index - 1
                )
                if (
                    should_assess_outcome
                    and not is_noise
                    and (
                        not _OUTCOME_RE.search(outcome_text)
                        or _VAGUE_OUTCOME_RE.search(outcome_text)
                    )
                ):
                    findings.append(
                        _finding(
                            f"BR-AC-{criterion_id}-NONOBSERVABLE",
                            "non_observable_acceptance",
                            "warning",
                            criterion_location,
                            "This acceptance criterion does not name an observable "
                            "result.",
                            "What output, state change, or visible response can be "
                            "checked?",
                        )
                    )
        if not _BOUNDARY_RE.search(" ".join(criteria_text)):
            findings.append(
                _finding(
                    "BR-006",
                    "failure_boundary",
                    "info",
                    "## Acceptance Criteria",
                    "Acceptance criteria do not describe a failure or boundary case.",
                    "What should happen for invalid input, a missing dependency, "
                    "or a denied action?",
                )
            )

    for task in result.tasks:
        if not task.verification.commands and not task.verification.manual_steps:
            findings.append(
                _finding(
                    f"BR-TASK-{task.id}-VERIFICATION",
                    "task_verification",
                    "warning",
                    f"## Tasks > {task.id} > Verification",
                    "This task has no verification command or manual check.",
                    "How will an implementer verify this task before submitting evidence?",
                )
            )

    # Identifiers are deliberately derived from stable source ids and finding
    # categories; sorting creates deterministic output even if parser traversal
    # changes or unrelated PRD sections are clarified in a later revision.
    return sorted(findings, key=lambda finding: finding.id)


def findings_as_dicts(findings: list[BehavioralFinding]) -> list[dict[str, str]]:
    """Return JSON-safe public records using the same order as human output."""
    return [asdict(finding) for finding in findings]
