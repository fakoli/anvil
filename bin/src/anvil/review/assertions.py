"""Artifact predicate engine (evidence-contracts:T003, issue #153).

The pure evaluator behind artifact assertions: load a JSON artifact, resolve
dotted paths (single-level ``[*]`` wildcard), apply the 12 generic predicate
operators, and return a structured pass/fail per assertion with observed
values and human-readable reasons.

Total by contract: a missing artifact, malformed JSON, or unresolvable path
is a FAILED assertion with a reason — never a traceback. No domain
knowledge, no state, no network; the only I/O is reading the declared
artifact files under the project root.

Semantics worth pinning (the voice-incident cases):

- ``not_contains`` / ``not_equals`` PASS on an unresolved path — an artifact
  with no ``errors`` recorded no failing stage, so ``errors[*].stage
  not_contains stt`` passes vacuously. Every other operator FAILS on an
  unresolved path (you cannot prove ``equals`` against nothing).
- ``must_not_fail_before: llm`` fails when any recorded failure stage sorts
  strictly before ``llm`` in the declared ``stage_order``.
- ``must_reach: llm`` is stricter: it also fails on a failure AT ``llm``
  (the stage was entered but never completed).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from anvil.state.models import PredicateOp

if TYPE_CHECKING:
    from anvil.state.models import ArtifactAssertion, Predicate

__all__ = [
    "AssertionResult",
    "evaluate_assertions",
]


@dataclass(frozen=True)
class AssertionResult:
    """The outcome of one ArtifactAssertion against its artifact."""

    artifact: str
    claim: str | None
    passed: bool
    failures: list[str] = field(default_factory=list)
    observed: dict[str, Any] = field(default_factory=dict)
    # T004: a not-yet-written artifact is INCOMPLETE evidence, not a
    # contradiction — the gate maps this flag to the incomplete verdict.
    missing_artifact: bool = False


def _resolve_path(doc: Any, path: str) -> tuple[bool, list[Any]]:
    """Resolve a dotted path with one optional ``[*]`` wildcard level.

    Returns ``(found, values)``: ``found`` is False when any segment is
    missing; ``values`` is the list of matched values (one for scalar paths,
    many when the wildcard expands a list). A wildcard over an EMPTY list
    resolves found=True with zero values — "there are no entries" is an
    answer, not a missing path.
    """
    values: list[Any] = [doc]
    for segment in path.split("."):
        wildcard = segment.endswith("[*]")
        key = segment[:-3] if wildcard else segment
        next_values: list[Any] = []
        for value in values:
            if key:
                if not isinstance(value, dict) or key not in value:
                    return False, []
                value = value[key]
            if wildcard:
                if not isinstance(value, list):
                    return False, []
                next_values.extend(value)
            else:
                next_values.append(value)
        values = next_values
    return True, values


def _check_predicate(doc: Any, pred: Predicate) -> tuple[bool, str | None, Any]:
    """Evaluate one predicate. Returns (passed, failure_reason, observed)."""
    found, values = _resolve_path(doc, pred.path)
    observed: Any = values if len(values) != 1 else values[0]
    op = pred.op

    # Vacuous-truth operators: an unresolved path proves the absence.
    if not found:
        if op in (PredicateOp.not_contains, PredicateOp.not_equals):
            return True, None, None
        return False, f"path {pred.path!r} not found in artifact", None

    if op is PredicateOp.exists:
        return True, None, observed
    if op is PredicateOp.not_null:
        if values and all(v is not None for v in values):
            return True, None, observed
        return False, f"{pred.path!r} is null or empty", observed
    if op is PredicateOp.equals:
        if values and all(v == pred.value for v in values):
            return True, None, observed
        return False, f"{pred.path!r} != {pred.value!r} (observed {observed!r})", observed
    if op is PredicateOp.not_equals:
        if all(v != pred.value for v in values):
            return True, None, observed
        return False, f"{pred.path!r} == {pred.value!r}", observed
    if op is PredicateOp.contains:
        if pred.value in values:
            return True, None, observed
        return False, (
            f"{pred.path!r} does not contain {pred.value!r} "
            f"(observed {observed!r})"
        ), observed
    if op is PredicateOp.not_contains:
        if pred.value not in values:
            return True, None, observed
        return False, f"{pred.path!r} contains {pred.value!r}", observed
    if op in (PredicateOp.gt, PredicateOp.gte, PredicateOp.lt, PredicateOp.lte):
        checks = {
            PredicateOp.gt: lambda v: v > pred.value,
            PredicateOp.gte: lambda v: v >= pred.value,
            PredicateOp.lt: lambda v: v < pred.value,
            PredicateOp.lte: lambda v: v <= pred.value,
        }
        try:
            if values and all(
                v is not None and checks[op](v) for v in values
            ):
                return True, None, observed
        except TypeError:
            return False, (
                f"{pred.path!r} is not comparable to {pred.value!r} "
                f"(observed {observed!r})"
            ), observed
        return False, (
            f"{pred.path!r} fails {op.value} {pred.value!r} "
            f"(observed {observed!r})"
        ), observed
    if op in (PredicateOp.len_eq, PredicateOp.len_gte):
        # Length of the single resolved value (list/str), or the wildcard
        # match count when the path expanded.
        target = values[0] if len(values) == 1 else values
        try:
            length = len(target)
        except TypeError:
            return False, f"{pred.path!r} has no length (observed {observed!r})", observed
        ok = length == pred.value if op is PredicateOp.len_eq else length >= pred.value
        if ok:
            return True, None, observed
        return False, f"len({pred.path!r}) == {length}, fails {op.value} {pred.value!r}", observed

    # Unreachable while PredicateOp is exhaustive; fail closed if it grows.
    return False, f"unknown operator {op!r}", observed  # pragma: no cover


def _check_phases(doc: Any, assertion: ArtifactAssertion) -> list[str]:
    """Evaluate must_reach / must_not_fail_before against recorded failures."""
    failures: list[str] = []
    if not (assertion.must_reach or assertion.must_not_fail_before):
        return failures
    if not assertion.stage_order or not assertion.stage_path:
        return [
            "phase predicates require both stage_order and stage_path on the assertion"
        ]
    order = {stage: idx for idx, stage in enumerate(assertion.stage_order)}
    found, stages = _resolve_path(doc, assertion.stage_path)
    # Review finding: a stage value the artifact records that stage_order
    # does not know (typo, rename, casing) must be LOUD — silently filtering
    # it would let the exact incident class this engine exists for pass.
    unknown = sorted({s for s in (stages if found else []) if s not in order})
    if unknown:
        failures.append(
            f"artifact records unknown stage(s) {unknown!r} not in stage_order"
        )
    failed_stages = [s for s in stages if s in order] if found else []

    if assertion.must_not_fail_before is not None:
        target = order.get(assertion.must_not_fail_before)
        if target is None:
            failures.append(
                f"must_not_fail_before stage {assertion.must_not_fail_before!r} "
                "is not in stage_order"
            )
        else:
            early = [s for s in failed_stages if order[s] < target]
            if early:
                failures.append(
                    f"artifact failed at stage {early[0]!r}, before "
                    f"{assertion.must_not_fail_before!r}"
                )
    if assertion.must_reach is not None:
        target = order.get(assertion.must_reach)
        if target is None:
            failures.append(
                f"must_reach stage {assertion.must_reach!r} is not in stage_order"
            )
        else:
            blocking = [s for s in failed_stages if order[s] <= target]
            if blocking:
                failures.append(
                    f"artifact failed at stage {blocking[0]!r}, so it never "
                    f"completed {assertion.must_reach!r}"
                )
    return failures


def evaluate_assertions(
    assertions: list[ArtifactAssertion], project_root: Path
) -> list[AssertionResult]:
    """Evaluate every assertion against its artifact under *project_root*.

    Pure and total: one result per assertion, failures carry human-readable
    reasons with observed values, and no input can raise.
    """
    results: list[AssertionResult] = []
    for assertion in assertions:
        # Trust note: PRD-declared paths share the trust boundary of
        # verification commands (which run with shell=True) — but reading
        # OUTSIDE the project root is never a legitimate contract, so an
        # escaping path is a failed assertion, not a capability.
        artifact_path = (project_root / assertion.artifact).resolve()
        failures: list[str] = []
        observed: dict[str, Any] = {}

        try:
            root_resolved = project_root.resolve()
            if not artifact_path.is_relative_to(root_resolved):
                results.append(
                    AssertionResult(
                        artifact=assertion.artifact,
                        claim=assertion.claim,
                        passed=False,
                        failures=[
                            f"artifact path {assertion.artifact!r} escapes "
                            "the project root"
                        ],
                    )
                )
                continue
            raw = artifact_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            results.append(
                AssertionResult(
                    artifact=assertion.artifact,
                    claim=assertion.claim,
                    passed=False,
                    failures=[f"artifact {assertion.artifact!r} does not exist"],
                    missing_artifact=True,
                )
            )
            continue
        except OSError as exc:
            results.append(
                AssertionResult(
                    artifact=assertion.artifact,
                    claim=assertion.claim,
                    passed=False,
                    failures=[f"artifact {assertion.artifact!r} unreadable: {exc}"],
                )
            )
            continue

        try:
            doc = json.loads(raw)
        except ValueError as exc:
            results.append(
                AssertionResult(
                    artifact=assertion.artifact,
                    claim=assertion.claim,
                    passed=False,
                    failures=[f"artifact {assertion.artifact!r} is not valid JSON: {exc}"],
                )
            )
            continue

        for pred in assertion.assertions:
            passed, reason, value = _check_predicate(doc, pred)
            observed[pred.path] = value
            if not passed and reason:
                failures.append(reason)

        failures.extend(_check_phases(doc, assertion))

        results.append(
            AssertionResult(
                artifact=assertion.artifact,
                claim=assertion.claim,
                passed=not failures,
                failures=failures,
                observed=observed,
            )
        )
    return results
