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
from pathlib import Path

from anvil.state.models import (
    AssertionProof,
    CommandProof,
    DiffProof,
    Evidence,
    LinkProof,
    ProofArtifact,
    ProofKind,
    ProofRequirement,
    Review,
    ReviewDecision,
    Task,
)

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


# Angle-bracket placeholder in a required-evidence string, e.g. ``<date>`` in a
# generated ``captured in `docs/findings/<date>-foo.md` `` line — matched as a
# wildcard against the concrete evidence (#108.2) rather than literally.
_EVIDENCE_PLACEHOLDER_RE = re.compile(r"<[^<>]+>")


def _evidence_text_matches(required_lower: str, corpus_lower: str) -> bool:
    """Case-insensitive substring test of a required-evidence string against a
    corpus, treating ``<...>`` placeholders as wildcards.

    A generated packet can mint a required-evidence line before a value is known
    (``captured in `docs/findings/<date>-foo.md` ``), so a literal substring test
    against the concrete evidence (``2026-07-04-foo.md``) would never match
    (#108.2). With no placeholder this is the plain substring test as before, so
    existing exact-match evidence is unaffected.

    A placeholder only means something when literal text ANCHORS it. If stripping
    the placeholders leaves no non-whitespace literal — e.g. the requirement is
    entirely ``<justification>`` — a wildcard would match ANY non-empty evidence
    (vacuous acceptance), so we fall back to the exact substring test, which such
    a requirement won't spuriously satisfy; it stays unmet for a human to judge.
    Each placeholder matches a single value token (``\\S+``), not arbitrary text,
    so it can't swallow unrelated words.
    """
    if not _EVIDENCE_PLACEHOLDER_RE.search(required_lower):
        return required_lower in corpus_lower
    segments = _EVIDENCE_PLACEHOLDER_RE.split(required_lower)
    if not any(seg.strip() for seg in segments):
        # No literal anchor — refuse to wildcard-match (would accept anything).
        return required_lower in corpus_lower
    pattern = r"\S+".join(re.escape(seg) for seg in segments)
    return re.search(pattern, corpus_lower) is not None


# ---------------------------------------------------------------------------
# Evidence contracts (evidence-contracts:T004, issue #153) — per-claim gate
# ---------------------------------------------------------------------------

# Verdict severity order: the OVERALL verdict is the worst per-claim one.
_VERDICT_ORDER = ("failed", "blocked", "incomplete", "diagnostic_only", "passed")


@dataclass(frozen=True)
class ClaimVerdict:
    """The gate's answer for ONE claim: does the evidence prove it?

    ``proof_missing`` (unsatisfied ProofRequirement labels) is kept separate
    from ``missing`` (artifact absences + structural gaps) because the T005
    enforcement scope differs: command-proof strictness on the IMPLICIT
    claim stays governed by strict_evidence (the locked decision), while
    contract items always enforce.
    """

    claim: str  # claim id; "" is the implicit task-level claim
    verdict: str  # passed | failed | incomplete | blocked | diagnostic_only
    missing: list[str] = field(default_factory=list)
    proof_missing: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class GateVerdict:
    """Per-claim verdicts plus the overall (worst) verdict."""

    overall: str
    claims: list[ClaimVerdict] = field(default_factory=list)

    @property
    def unproven(self) -> list[ClaimVerdict]:
        return [c for c in self.claims if c.verdict != "passed"]

    @property
    def enforceable_unproven(self) -> list[ClaimVerdict]:
        """Unproven claims the T005 auto-strict gate refuses on.

        NAMED claims always enforce (the author wrote the contract). The
        implicit "" claim enforces only its CONTRACT items — assertion
        failures and non-proof gaps — while unsatisfied command proofs on it
        stay governed by strict_evidence, exactly as before this feature.
        """
        out: list[ClaimVerdict] = []
        for c in self.claims:
            if c.verdict == "passed":
                continue
            if c.claim or c.failures or c.missing or c.verdict in (
                "blocked",
                "diagnostic_only",
            ):
                out.append(c)
        return out


def _worse(a: str, b: str) -> str:
    return a if _VERDICT_ORDER.index(a) <= _VERDICT_ORDER.index(b) else b


def evaluate_claims(
    task: Task,
    evidence: Evidence | None,
    *,
    project_root: Path,
) -> GateVerdict:
    """Evaluate the task's evidence CONTRACT, grouped by claim (T004).

    Both requirement surfaces participate: typed ``required_proofs`` and
    ``artifact_assertions``, each grouped by its ``claim`` binding (an
    unbound requirement belongs to the implicit task-level claim ``""``).
    Verdict semantics per claim:

    - ``failed`` — an artifact assertion evaluated and CONTRADICTED the
      claim (predicate/phase failure on an existing artifact).
    - ``incomplete`` — a required proof is unsatisfied or a required
      artifact does not exist yet.
    - ``blocked`` — the evidence's category is ``blocked``: the claim
      could not be proven for an environmental reason worth recording.
    - ``diagnostic_only`` — requirements would pass, but the evidence is
      ``diagnostic``/``advisory`` and can never satisfy a completion
      claim (the voice-incident rule).
    - ``passed`` — every bound requirement satisfied on completion (or
      promotion_quality) evidence.

    A task with no claims, no assertions, and no typed proofs yields a
    single implicit claim whose verdict is ``passed`` — byte-compatible
    with pre-contract behavior (the legacy ``evidence_complete`` gate is
    unchanged and still governs advisory/strict command-proof flows).
    """
    from anvil.review.assertions import evaluate_assertions
    from anvil.state.models import EvidenceCategory

    category = (
        evidence.category if evidence is not None else EvidenceCategory.completion
    )
    proofs = list(evidence.proofs) if evidence is not None else []

    # Group requirements by claim id ("" = implicit task-level claim).
    claim_ids: list[str] = [c.id for c in task.claims]
    groups: dict[str, dict[str, list]] = {
        cid: {"proofs": [], "assertions": []} for cid in claim_ids
    }

    def _group(claim: str | None) -> dict[str, list]:
        key = claim or ""
        if key not in groups:
            groups[key] = {"proofs": [], "assertions": []}
        return groups[key]

    for req in task.verification.required_proofs:
        _group(req.claim)["proofs"].append(req)
    for assertion in task.verification.artifact_assertions:
        _group(assertion.claim)["assertions"].append(assertion)

    # One engine pass over ALL assertions, indexed back per claim.
    all_assertions = list(task.verification.artifact_assertions)
    assertion_results = (
        evaluate_assertions(all_assertions, project_root)
        if all_assertions
        else []
    )
    results_by_claim: dict[str, list] = {}
    for result in assertion_results:
        results_by_claim.setdefault(result.claim or "", []).append(result)

    verdicts: list[ClaimVerdict] = []
    for cid, group in groups.items():
        missing: list[str] = []
        failures: list[str] = []
        has_requirements = bool(group["proofs"]) or bool(group["assertions"])

        proof_missing: list[str] = []
        for req in group["proofs"]:
            if not _proof_satisfies(req, proofs):
                proof_missing.append(req.label)

        for result in results_by_claim.get(cid, []):
            if result.passed:
                continue
            if result.missing_artifact:
                missing.extend(result.failures)
            else:
                failures.extend(result.failures)

        # Review finding (T004, recorded decision): a NAMED claim binding
        # zero requirements is structurally unprovable — fail closed as
        # incomplete rather than pass vacuously, or T005's auto-strict could
        # be gamed by declaring claims without contracts. The parser stays
        # permissive (a WIP PRD may declare labels first); the gate is the
        # enforcement point. The implicit "" claim is exempt (back-compat).
        if cid and not has_requirements:
            missing.append(
                f"claim {cid!r} binds no proof requirements or artifact "
                "assertions — declare a contract or remove the claim"
            )
        # Review finding: with NO evidence row at all, on-disk artifacts must
        # not prove a contract-bearing claim (proof decoupled from claim is
        # the incident class). Only the implicit no-contract claim may pass.
        elif (
            evidence is None
            and has_requirements
            and not missing
            and not proof_missing
            and not failures
        ):
            missing.append("no evidence submitted for this task")

        if failures:
            # Deliberate precedence: a machine-derived CONTRADICTION outranks
            # a self-declared blocked category — an agent cannot hide a
            # contradicting artifact behind "blocked".
            verdict = "failed"
        elif category is EvidenceCategory.blocked:
            verdict = "blocked"
        elif missing or proof_missing:
            verdict = "incomplete"
        elif category in (
            EvidenceCategory.diagnostic,
            EvidenceCategory.advisory,
        ):
            # The voice-incident rule: diagnostic evidence may be excellent
            # context and still proves NO completion claim.
            verdict = "diagnostic_only"
        else:
            verdict = "passed"
        verdicts.append(
            ClaimVerdict(
                claim=cid,
                verdict=verdict,
                missing=missing,
                proof_missing=proof_missing,
                failures=failures,
            )
        )

    if not verdicts:
        # No contract declared. The implicit claim still honors the evidence
        # TAXONOMY (T006 review MUST-FIX): an affirmative blocked/diagnostic
        # category is a "this is not done" signal that must reach the apply
        # gate even without a contract — otherwise `submit --category
        # blocked` on a plain task is silently approvable, the exact
        # incident class. Completion evidence keeps the historical pass.
        if category is EvidenceCategory.blocked:
            implicit = "blocked"
        elif category in (
            EvidenceCategory.diagnostic,
            EvidenceCategory.advisory,
        ):
            implicit = "diagnostic_only"
        else:
            implicit = "passed"
        verdicts = [ClaimVerdict(claim="", verdict=implicit)]

    overall = verdicts[0].verdict
    for cv in verdicts[1:]:
        overall = _worse(overall, cv.verdict)
    return GateVerdict(overall=overall, claims=verdicts)

def evidence_complete(task: Task, evidence: Evidence) -> tuple[bool, list[str]]:
    """Validate that Evidence satisfies the Task's declared requirements.

    Two requirement surfaces are checked, and BOTH must be satisfied:

    1. ``task.verification.required_proofs`` (SL-3 / B48) — typed. A ``command``
       requirement is satisfied **only** by a :class:`CommandProof` whose
       ``exit_code`` is in ``passing_exit_codes``; an ``assertion`` proof can't
       impersonate a command, so free text in a description/output field can't
       satisfy it. NOTE the authenticity of a CommandProof rests on a trusted
       hook writer (output_sha256 is recorded, not re-verified) — see the TRUST
       BOUNDARY note on the proof models and :func:`_proof_satisfies`.
    2. ``task.verification.required_evidence`` (legacy) — free-text substring
       heuristics over the descriptive ``Evidence`` string fields. Kept for
       back-compat; the planner emits typed ``required_proofs`` instead, so this
       list is empty for engine-created tasks. Rules (case-insensitive ``in``):

       - "test" / "pytest" / "cargo test"   → check evidence.commands_run
       - "PR" / "pull request"              → check evidence.pr_url
       - "screenshot"                       → check evidence.screenshots
       - "files changed"                    → check evidence.files_changed
       - anything else                      → output_excerpt / known_limitations

    Args:
        task:     The Task whose verification requirements to check.
        evidence: The Evidence submitted by the agent.

    Returns:
        A tuple (passed, missing_items) where ``passed`` is True iff every
        required item — typed proof and legacy string — is satisfied, and
        ``missing_items`` lists the unsatisfied ones (typed requirements
        contribute their ``label``). Empty ``missing_items`` means it passed.

    Usage by ``cli apply``:
        passed, missing = evidence_complete(task, evidence)
        if not passed:
            typer.echo(f"Missing evidence: {missing}", err=True)
    """
    missing: list[str] = []

    # Legacy free-text path (dormant for engine-created tasks; kept for
    # back-compat). Substring heuristics over the descriptive string fields.
    for item in task.verification.required_evidence:
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
            # Fallback: check output_excerpt or known_limitations contain the
            # item, treating any <...> placeholders as wildcards (#108.2).
            corpus_lower = []
            if evidence.output_excerpt:
                corpus_lower.append(evidence.output_excerpt.lower())
            if evidence.known_limitations:
                corpus_lower.append(evidence.known_limitations.lower())
            satisfied = any(
                _evidence_text_matches(item_lower, text) for text in corpus_lower
            )

        if not satisfied:
            missing.append(item)

    # SL-3 / B48 typed path: each requirement is satisfied only by a matching
    # typed proof — a command requirement needs a CommandProof whose exit_code is
    # in the passing set, so free text in a description field can't satisfy it.
    # (The proof's authenticity still rests on a trusted hook writer — TRUST
    # BOUNDARY note on the proof models; output_sha256 is not re-verified here.)
    for req in task.verification.required_proofs:
        if not _proof_satisfies(req, evidence.proofs):
            missing.append(req.label)

    # A legacy required_evidence string and a typed required_proofs label can
    # coincide; report each missing item once (order-preserving dedup) so the
    # reviewer doesn't see a confusing duplicate.
    missing = list(dict.fromkeys(missing))
    return len(missing) == 0, missing


def _proof_satisfies(req: ProofRequirement, proofs: list[ProofArtifact]) -> bool:
    """True iff some proof in ``proofs`` satisfies the typed requirement ``req``.

    The discriminator (``req.kind``) selects the predicate. A ``command``
    requirement is the load-bearing one: it matches only a :class:`CommandProof`
    whose ``command`` equals the pinned command AND whose ``exit_code`` is in
    ``passing_exit_codes``. There is no substring branch and no field-flattening
    fallback, so an :class:`AssertionProof` carrying the command text cannot
    satisfy it, and a recorded command that exited non-zero is correctly refused.

    Scope of the guarantee: this closes the "free text in a description field
    satisfies the gate" hole. It does NOT independently re-execute the command —
    the CommandProof's authenticity rests on a trusted hook writer (see the TRUST
    BOUNDARY note on the proof models). A harness in which the agent can write the
    evidence buffer can still fabricate a passing CommandProof.
    """
    if req.kind is ProofKind.command:
        return any(
            isinstance(p, CommandProof)
            and p.command == req.command
            and p.exit_code in req.passing_exit_codes
            for p in proofs
        )
    if req.kind is ProofKind.diff:
        return any(isinstance(p, DiffProof) for p in proofs)
    if req.kind is ProofKind.link:
        return any(
            isinstance(p, LinkProof)
            and (req.link_contains is None or req.link_contains in p.url)
            for p in proofs
        )
    if req.kind is ProofKind.assertion:
        return any(isinstance(p, AssertionProof) for p in proofs)
    return False  # pragma: no cover — ProofKind is exhaustive


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
