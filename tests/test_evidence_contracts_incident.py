"""End-to-end regression for issue #153 — the voice-benchmark incident.

The incident: a task claimed it had benchmarked a *candidate* model, the
evidence gate accepted it, but the submitted artifacts showed the candidate
run failed at STT (connection refused) and only the *baseline* was ever
measured. A command exiting 0 proved the command exited 0; nothing proved
the claim. This test recreates that exact shape and asserts the evidence
contract now makes it impossible to approve.

Two layers, sharing one set of fixture artifacts under
``tests/fixtures/voice_incident/``:

- Full CLI lifecycle (claim → submit → apply): the artifact is re-read at
  apply time, so swapping the file between apply calls drives the headline
  refusal (AC1) and the corrected approval (AC3).
- Gate-level (``evaluate_claims``): the category- and topology-sensitive
  cases (AC2, AC4) that don't need the whole CLI to prove.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from typer.testing import CliRunner

from anvil.cli import app
from anvil.review.gates import evaluate_claims
from anvil.state.models import (
    ArtifactAssertion,
    Evidence,
    EvidenceCategory,
    Predicate,
    Task,
    TaskClaim,
    Verification,
)

runner = CliRunner()

_FIXTURES = Path(__file__).parent / "fixtures" / "voice_incident"
_ARTIFACT = "candidate-gemma.json"  # where the PRD binds its assertion

# The evidence contract at the heart of issue #153: the candidate was
# actually measured (not the baseline), the LLM stage produced a timing, no
# stage failed before the LLM, and the topology matches the intended profile.
_ASSERTIONS = [
    Predicate(path="candidate_identity.candidate_id", op="equals", value="gemma4-12b-it"),
    Predicate(path="status", op="equals", value="measured"),
    Predicate(path="stage_timings_ms.llm_ms", op="not_null"),
    Predicate(path="errors[*].stage", op="not_contains", value="stt"),
    Predicate(path="topology.profile", op="equals", value="voice-fast"),
    Predicate(path="topology.stt_endpoint_host", op="equals", value="stt.internal:8080"),
]


def _incident_task() -> Task:
    import datetime

    now = datetime.datetime(2026, 7, 8, tzinfo=datetime.UTC)
    return Task(
        id="T001",
        feature_id="F001",
        title="Run candidate benchmark matrix",
        description="Benchmark gemma4-12b-it against the baseline.",
        created_at=now,
        updated_at=now,
        claims=[TaskClaim(id="candidate_benchmark_completed", subject="gemma4-12b-it")],
        verification=Verification(
            artifact_assertions=[
                ArtifactAssertion(
                    artifact=_ARTIFACT,
                    claim="candidate_benchmark_completed",
                    assertions=list(_ASSERTIONS),
                    stage_order=["stt", "llm", "tts"],
                    stage_path="errors[*].stage",
                    must_not_fail_before="llm",
                )
            ]
        ),
    )


def _evidence(category: EvidenceCategory = EvidenceCategory.completion) -> Evidence:
    import datetime

    now = datetime.datetime(2026, 7, 8, tzinfo=datetime.UTC)
    return Evidence(
        id="EV001",
        task_id="T001",
        claim_id="C001",
        submitted_at=now,
        submitted_by="agent",
        category=category,
    )


def _place(fixture: str, root: Path) -> None:
    """Copy a fixture artifact into the project root as the bound artifact."""
    shutil.copy(_FIXTURES / fixture, root / _ARTIFACT)


# ---------------------------------------------------------------------------
# Gate-level: the category / topology matrix (AC2, AC4) + the core AC1/AC3
# ---------------------------------------------------------------------------


class TestIncidentGate:
    def test_candidate_failed_at_stt_is_refused(self, tmp_path: Path) -> None:
        """AC1 at the gate: the incident state — candidate failed pre-LLM —
        is UNPROVEN, with the named claim in the verdict."""
        _place("candidate-failed-stt.json", tmp_path)
        verdict = evaluate_claims(_incident_task(), _evidence(), project_root=tmp_path)
        assert verdict.overall == "failed"
        (unproven,) = verdict.enforceable_unproven
        assert unproven.claim == "candidate_benchmark_completed"
        # The failure names the pre-LLM stage and the missing candidate status.
        joined = " ".join(unproven.failures)
        assert "stt" in joined or "before" in joined

    def test_failed_rows_marked_diagnostic_are_diagnostic_only(
        self, tmp_path: Path
    ) -> None:
        """AC2: the SAME failing artifact submitted as diagnostic yields a
        diagnostic_only verdict — still not approvable, but honestly labeled
        as context rather than a false completion."""
        _place("candidate-measured.json", tmp_path)  # even a passing artifact…
        verdict = evaluate_claims(
            _incident_task(),
            _evidence(category=EvidenceCategory.diagnostic),
            project_root=tmp_path,
        )
        assert verdict.overall == "diagnostic_only"
        assert verdict.enforceable_unproven  # not approvable

    def test_corrected_measured_candidate_approves(self, tmp_path: Path) -> None:
        """AC3 at the gate: the corrected artifact (measured, non-null
        timings, matching topology) proves the claim."""
        _place("candidate-measured.json", tmp_path)
        verdict = evaluate_claims(_incident_task(), _evidence(), project_root=tmp_path)
        assert verdict.overall == "passed"
        assert verdict.enforceable_unproven == []

    def test_topology_mismatch_fails_the_claim(self, tmp_path: Path) -> None:
        """AC4: a measured run with the WRONG profile/endpoint host still
        fails — a benchmark of the wrong topology is not the claimed one."""
        _place("candidate-topology-mismatch.json", tmp_path)
        verdict = evaluate_claims(_incident_task(), _evidence(), project_root=tmp_path)
        assert verdict.overall == "failed"
        joined = " ".join(verdict.enforceable_unproven[0].failures)
        assert "profile" in joined or "topology" in joined


# ---------------------------------------------------------------------------
# Full CLI lifecycle: the incident is refused, the fix approves (AC1 + AC3)
# ---------------------------------------------------------------------------


_INCIDENT_PRD = """# Project: Voice Benchmark Incident

## Summary

Recreation of issue #153: a candidate-benchmark task whose evidence must
prove the candidate was measured, not merely that a command exited 0.

## Goals

- Make the voice incident impossible to approve.

## Requirements

- R001: A candidate-benchmark claim is proven only by a measured candidate artifact.

## Features

### F001: Candidate benchmark

**Requirements:** R001

## Tasks

### T001: Run candidate benchmark matrix

**Feature:** F001
**Claims:** candidate_benchmark_completed (measurement: gemma4-12b-it)

Benchmark gemma4-12b-it against the baseline.

**Acceptance criteria:**

- The candidate is measured, not just the baseline.

**Verification:**

- `echo ok`

**Artifact assertions:**

```yaml
- artifact: candidate-gemma.json
  claim: candidate_benchmark_completed
  assertions:
    - path: candidate_identity.candidate_id
      op: equals
      value: gemma4-12b-it
    - path: status
      op: equals
      value: measured
    - path: stage_timings_ms.llm_ms
      op: not_null
    - path: errors[*].stage
      op: not_contains
      value: stt
    - path: topology.profile
      op: equals
      value: voice-fast
    - path: topology.stt_endpoint_host
      op: equals
      value: stt.internal:8080
  stage_order: [stt, llm, tts]
  stage_path: errors[*].stage
  must_not_fail_before: llm
```
"""


def _cli(tmp_path: Path, cmd: list[str]):  # type: ignore[no-untyped-def]
    original = os.getcwd()
    os.chdir(tmp_path)
    try:
        return runner.invoke(app, cmd, catch_exceptions=False)
    finally:
        os.chdir(original)


class TestIncidentLifecycle:
    def _setup_submitted(self, tmp_path: Path) -> None:
        assert _cli(tmp_path, ["init", "--name", "Incident"]).exit_code == 0
        (tmp_path / ".anvil" / "prd.md").write_text(_INCIDENT_PRD, encoding="utf-8")
        for cmd in (
            ["prd", "parse"], ["prd", "review"], ["prd", "review", "--approve"],
            ["plan"], ["review", "tasks"],
        ):
            assert _cli(tmp_path, cmd).exit_code == 0, cmd
        assert _cli(
            tmp_path, ["claim", "T001", "--actor", "bench-agent"]
        ).exit_code == 0
        assert _cli(
            tmp_path,
            ["submit", "T001", "--commands", "echo ok", "--files-changed", "x.py"],
        ).exit_code == 0

    def test_incident_refused_then_corrected_approves(self, tmp_path: Path) -> None:
        """The full acceptance test from issue #153: the failed-at-STT
        candidate is refused with the claim named; the corrected measured
        artifact then approves — same task, same contract, artifact re-read
        at apply time."""
        self._setup_submitted(tmp_path)

        # The incident: candidate failed at STT → apply MUST refuse.
        _place("candidate-failed-stt.json", tmp_path)
        refused = _cli(
            tmp_path, ["apply", "T001", "--approve", "--reviewer", "rv", "--json"]
        )
        assert refused.exit_code == 1, refused.output
        envelope = json.loads(refused.output.strip().splitlines()[-1])
        assert envelope["error"]["code"] == "claim_unproven"
        verdict = envelope["error"]["claim_verdict"]
        assert verdict["overall"] == "failed"
        assert any(
            c["claim"] == "candidate_benchmark_completed"
            for c in verdict["claims"]
        )

        # Still in needs_review — the refusal did not advance the task.
        show = _cli(tmp_path, ["show", "T001", "--json"])
        status = json.loads(show.output.strip().splitlines()[-1])["data"]["task"]["status"]
        assert status == "needs_review"

        # The correction: a genuinely measured candidate → approves.
        _place("candidate-measured.json", tmp_path)
        approved = _cli(
            tmp_path, ["apply", "T001", "--approve", "--reviewer", "rv", "--json"]
        )
        assert approved.exit_code == 0, approved.output
        data = json.loads(approved.output.strip().splitlines()[-1])["data"]
        assert data["status"] == "done"
        # The NAMED candidate claim is proven; overall stays incomplete only
        # because the implicit claim's unbound `echo ok` command proof never
        # attached (advisory under strict_evidence — so approval proceeds).
        by_claim = {
            c["claim"]: c["verdict"] for c in data["claim_verdict"]["claims"]
        }
        assert by_claim["candidate_benchmark_completed"] == "passed"
