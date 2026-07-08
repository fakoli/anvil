"""Tests for the per-claim evidence gate (evidence-contracts:T004).

evaluate_claims groups typed proofs + artifact assertions by claim, applies
the evidence-category taxonomy, and returns verdicts richer than pass/fail —
without touching the legacy evidence_complete surface.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from pathlib import Path

from anvil.review.gates import evaluate_claims, evidence_complete
from anvil.state.models import (
    ArtifactAssertion,
    CommandProof,
    Evidence,
    EvidenceCategory,
    ProofKind,
    ProofRequirement,
    Task,
    TaskClaim,
    Verification,
)

_NOW = datetime.datetime(2026, 7, 8, 8, 0, 0, tzinfo=datetime.UTC)
_SHA = hashlib.sha256(b"out").hexdigest()


def _task(**kwargs: object) -> Task:
    defaults: dict[str, object] = {
        "id": "T001",
        "feature_id": "F001",
        "title": "t",
        "description": "d",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    defaults.update(kwargs)
    return Task.model_validate(defaults)


def _evidence(**kwargs: object) -> Evidence:
    defaults: dict[str, object] = {
        "id": "EV001",
        "task_id": "T001",
        "claim_id": "C001",
        "submitted_at": _NOW,
        "submitted_by": "agent",
    }
    defaults.update(kwargs)
    return Evidence.model_validate(defaults)


def _assertion(**kwargs: object) -> ArtifactAssertion:
    defaults: dict[str, object] = {
        "artifact": "artifact.json",
        "claim": "cbc",
        "assertions": [
            {"path": "status", "op": "equals", "value": "measured"}
        ],
    }
    defaults.update(kwargs)
    return ArtifactAssertion.model_validate(defaults)


def _contract_task(**verification: object) -> Task:
    return _task(
        claims=[TaskClaim(id="cbc", subject="gemma4-12b-it")],
        verification=Verification(**verification),
    )


class TestClaimVerdicts:
    def test_passing_completion_evidence_is_passed(self, tmp_path: Path) -> None:
        (tmp_path / "artifact.json").write_text(
            json.dumps({"status": "measured"}), encoding="utf-8"
        )
        task = _contract_task(artifact_assertions=[_assertion()])
        verdict = evaluate_claims(task, _evidence(), project_root=tmp_path)
        assert verdict.overall == "passed"
        assert verdict.claims[0].claim == "cbc"
        assert verdict.unproven == []

    def test_same_evidence_diagnostic_is_diagnostic_only(
        self, tmp_path: Path
    ) -> None:
        """T004 AC: passing assertions on diagnostic evidence yield
        diagnostic_only — the voice-incident rule."""
        (tmp_path / "artifact.json").write_text(
            json.dumps({"status": "measured"}), encoding="utf-8"
        )
        task = _contract_task(artifact_assertions=[_assertion()])
        verdict = evaluate_claims(
            task,
            _evidence(category=EvidenceCategory.diagnostic),
            project_root=tmp_path,
        )
        assert verdict.overall == "diagnostic_only"

    def test_failed_assertion_names_the_predicate(self, tmp_path: Path) -> None:
        """T004 AC: failed assertions yield failed with the failing
        predicate named."""
        (tmp_path / "artifact.json").write_text(
            json.dumps({"status": "failed_unavailable"}), encoding="utf-8"
        )
        task = _contract_task(artifact_assertions=[_assertion()])
        verdict = evaluate_claims(task, _evidence(), project_root=tmp_path)
        assert verdict.overall == "failed"
        assert "'status'" in verdict.claims[0].failures[0]
        assert "failed_unavailable" in verdict.claims[0].failures[0]

    def test_missing_artifact_is_incomplete(self, tmp_path: Path) -> None:
        """T004 AC: a not-yet-written artifact is incomplete, not failed."""
        task = _contract_task(artifact_assertions=[_assertion()])
        verdict = evaluate_claims(task, _evidence(), project_root=tmp_path)
        assert verdict.overall == "incomplete"
        assert "does not exist" in verdict.claims[0].missing[0]

    def test_blocked_category_is_blocked(self, tmp_path: Path) -> None:
        (tmp_path / "artifact.json").write_text(
            json.dumps({"status": "measured"}), encoding="utf-8"
        )
        task = _contract_task(artifact_assertions=[_assertion()])
        verdict = evaluate_claims(
            task,
            _evidence(category=EvidenceCategory.blocked),
            project_root=tmp_path,
        )
        assert verdict.overall == "blocked"

    def test_unsatisfied_proof_requirement_is_incomplete_with_label(
        self, tmp_path: Path
    ) -> None:
        task = _contract_task(
            required_proofs=[
                ProofRequirement(
                    kind=ProofKind.command,
                    command="pytest -q",
                    label="`pytest -q` exits 0",
                    claim="cbc",
                )
            ]
        )
        verdict = evaluate_claims(task, _evidence(), project_root=tmp_path)
        assert verdict.overall == "incomplete"
        assert verdict.claims[0].missing == ["`pytest -q` exits 0"]

    def test_satisfied_proof_requirement_passes(self, tmp_path: Path) -> None:
        task = _contract_task(
            required_proofs=[
                ProofRequirement(
                    kind=ProofKind.command,
                    command="pytest -q",
                    label="`pytest -q` exits 0",
                    claim="cbc",
                )
            ]
        )
        evidence = _evidence(
            proofs=[
                CommandProof(
                    command="pytest -q",
                    exit_code=0,
                    output_sha256=_SHA,
                    captured_at=_NOW,
                )
            ]
        )
        verdict = evaluate_claims(task, evidence, project_root=tmp_path)
        assert verdict.overall == "passed"


class TestOverallOrdering:
    def test_overall_is_the_worst_claim_verdict(self, tmp_path: Path) -> None:
        """T004 AC: failed > blocked > incomplete > diagnostic_only > passed."""
        (tmp_path / "good.json").write_text(
            json.dumps({"status": "measured"}), encoding="utf-8"
        )
        (tmp_path / "bad.json").write_text(
            json.dumps({"status": "nope"}), encoding="utf-8"
        )
        task = _task(
            claims=[TaskClaim(id="good"), TaskClaim(id="bad")],
            verification=Verification(
                artifact_assertions=[
                    _assertion(artifact="good.json", claim="good"),
                    _assertion(artifact="bad.json", claim="bad"),
                ]
            ),
        )
        verdict = evaluate_claims(task, _evidence(), project_root=tmp_path)
        assert verdict.overall == "failed"
        by_claim = {c.claim: c.verdict for c in verdict.claims}
        assert by_claim == {"good": "passed", "bad": "failed"}
        assert [c.claim for c in verdict.unproven] == ["bad"]

    def test_incomplete_beats_diagnostic_only(self, tmp_path: Path) -> None:
        (tmp_path / "good.json").write_text(
            json.dumps({"status": "measured"}), encoding="utf-8"
        )
        task = _task(
            claims=[TaskClaim(id="good"), TaskClaim(id="missing")],
            verification=Verification(
                artifact_assertions=[
                    _assertion(artifact="good.json", claim="good"),
                    _assertion(artifact="never.json", claim="missing"),
                ]
            ),
        )
        verdict = evaluate_claims(
            task,
            _evidence(category=EvidenceCategory.diagnostic),
            project_root=tmp_path,
        )
        assert verdict.overall == "incomplete"


class TestBackCompat:
    def test_contractless_task_yields_implicit_passed(self, tmp_path: Path) -> None:
        """T004 AC: a task with no claims/assertions/proofs produces exactly
        today's behavior — implicit claim, passed, legacy gate untouched."""
        task = _task()
        verdict = evaluate_claims(task, _evidence(), project_root=tmp_path)
        assert verdict.overall == "passed"
        assert verdict.claims[0].claim == ""

        # Legacy surface byte-unchanged: same signature, same result shape.
        passed, missing = evidence_complete(task, _evidence())
        assert passed is True and missing == []

    def test_no_evidence_at_all_with_contract_is_incomplete(
        self, tmp_path: Path
    ) -> None:
        task = _contract_task(artifact_assertions=[_assertion()])
        verdict = evaluate_claims(task, None, project_root=tmp_path)
        assert verdict.overall == "incomplete"

    def test_unbound_requirements_form_implicit_claim(self, tmp_path: Path) -> None:
        """Unbound (claim=None) requirements keep task-level semantics."""
        (tmp_path / "artifact.json").write_text(
            json.dumps({"status": "measured"}), encoding="utf-8"
        )
        task = _task(
            verification=Verification(
                artifact_assertions=[_assertion(claim=None)]
            )
        )
        verdict = evaluate_claims(task, _evidence(), project_root=tmp_path)
        assert verdict.overall == "passed"
        assert verdict.claims[0].claim == ""
