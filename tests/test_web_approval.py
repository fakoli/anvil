"""Browser review decisions retain the exact task and evidence preview."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from typer.testing import CliRunner

from anvil.cli import app, packet_apply
from tests.test_attempt_view import _reclaim, _submit
from tests.test_sqlite import _make_backend, _setup_claimable_task_and_claim


@pytest.fixture
def review_project(tmp_path, monkeypatch):
    state_dir = tmp_path / ".anvil"
    state_dir.mkdir()
    monkeypatch.setenv("ANVIL_ROOT", str(tmp_path))
    backend = _make_backend(state_dir)
    _setup_claimable_task_and_claim(backend)
    _submit(backend)
    try:
        yield backend, state_dir
    finally:
        backend.close()


def _preview_arguments(backend):
    return [
        "--expected-evidence-id", backend.get_latest_evidence("T001").id,
        "--expected-task-updated-at", backend.get_task("T001").updated_at.isoformat(),
    ]


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_apply_binds_the_confirmed_preview(review_project, decision):
    backend, state_dir = review_project
    arguments = _preview_arguments(backend)
    result = CliRunner().invoke(app, [
        "apply", "T001", f"--{decision}", "--strict", "--reviewer", "human",
        "--reason", "Reviewed the displayed evidence.", "--json", *arguments,
    ])
    assert result.exit_code == 0, result.output
    expected_status = "done" if decision == "approve" else "drafted"
    assert backend.get_task("T001").status.value == expected_status
    applied = [
        json.loads(line) for line in (state_dir / "events.jsonl").read_text().splitlines()
        if json.loads(line)["action"] == "task.applied"
    ][-1]["payload_json"]
    assert applied["expected_evidence_id"] == arguments[1]
    assert datetime.fromisoformat(applied["expected_task_updated_at"]) == datetime.fromisoformat(
        arguments[3]
    )
    if decision == "approve":
        assert applied["review_attempt_id"] == arguments[1]


@pytest.mark.parametrize("changed", ["evidence", "task"])
def test_stale_preview_refuses_without_a_decision(review_project, changed):
    backend, state_dir = review_project
    arguments = _preview_arguments(backend)
    if changed == "evidence":
        arguments[1] = "older-evidence"
    else:
        arguments[3] = (backend.get_task("T001").updated_at - timedelta(seconds=1)).isoformat()
    before = (state_dir / "events.jsonl").read_bytes()
    result = CliRunner().invoke(app, [
        "apply", "T001", "--approve", "--strict", "--json", *arguments,
    ])
    assert result.exit_code == 1, result.output
    assert json.loads(result.output)["error"]["code"] == "stale_preview"
    assert backend.get_task("T001").status.value == "needs_review"
    assert (state_dir / "events.jsonl").read_bytes() == before


@pytest.mark.parametrize("arguments", [
    ["--expected-evidence-id", "EV001"],
    ["--expected-task-updated-at", "2026-05-24T18:00:00Z"],
    ["--expected-evidence-id", "EV001", "--expected-task-updated-at", "invalid"],
    ["--expected-evidence-id", "EV001", "--expected-task-updated-at", "2026-05-24T18:00:00"],
])
def test_preview_requires_a_complete_valid_binding(review_project, arguments):
    _, state_dir = review_project
    before = (state_dir / "events.jsonl").read_bytes()
    result = CliRunner().invoke(app, ["apply", "T001", "--approve", "--json", *arguments])
    assert result.exit_code == 1, result.output
    assert json.loads(result.output)["error"]["code"] == "bad_request"
    assert (state_dir / "events.jsonl").read_bytes() == before


def test_new_attempt_between_preview_check_and_append_cannot_replace_it(
    review_project, monkeypatch,
):
    backend, state_dir = review_project
    arguments = _preview_arguments(backend)

    def resubmit_during_gate(task_id, active_backend, state_dir, cwd):
        _reclaim(active_backend)
        _submit(active_backend, evidence_id="EV002", claim_id="C002")
        return "off", None

    monkeypatch.setattr(packet_apply, "_merge_check_block", resubmit_during_gate)
    result = CliRunner().invoke(app, [
        "apply", "T001", "--approve", "--strict", "--json", *arguments,
    ])
    assert result.exit_code != 0
    assert json.loads(result.output)["error"]["code"] == "review_refused"
    assert backend.get_task("T001").status.value == "needs_review"
    assert backend.get_latest_evidence("T001").id == "EV002"
    decisions = [
        json.loads(line)["payload_json"]["decision"]
        for line in (state_dir / "events.jsonl").read_text().splitlines()
        if json.loads(line)["action"] == "task.applied"
    ]
    assert decisions == ["rejected"]


def test_existing_cli_without_preview_keeps_its_event_shape(review_project):
    backend, state_dir = review_project
    result = CliRunner().invoke(app, ["apply", "T001", "--approve", "--json"])
    assert result.exit_code == 0, result.output
    assert backend.get_task("T001").status.value == "done"
    payload = json.loads((state_dir / "events.jsonl").read_text().splitlines()[-1])["payload_json"]
    assert "expected_evidence_id" not in payload
    assert "expected_task_updated_at" not in payload


@pytest.mark.parametrize("binding", [True, False])
@pytest.mark.parametrize("verifier", ["claims", "merge"])
def test_strict_approval_refuses_required_verifier_errors(review_project, monkeypatch, binding, verifier):
    from anvil.review import gates
    from anvil.state.models import TaskClaim

    backend, state_dir = review_project
    arguments = _preview_arguments(backend) if binding else []
    if verifier == "claims":
        # Keep the persisted preview identity; inject only the verifier contract
        # on the typed read so the error branch can be isolated.
        original_get_task = type(backend).get_task

        def contracted_task(self, task_id):
            task = original_get_task(self, task_id)
            if task is not None:
                task.claims = [TaskClaim(id="required_claim", subject="implementation")]
            return task

        def unavailable(*args, **kwargs):
            raise RuntimeError("injected verifier failure")

        monkeypatch.setattr(type(backend), "get_task", contracted_task)
        monkeypatch.setattr(gates, "evaluate_claims", unavailable)
    else:
        monkeypatch.setattr(packet_apply, "_merge_check_block", lambda *args: ("strict", None))
    before = (state_dir / "events.jsonl").read_bytes()
    result = CliRunner().invoke(app, [
        "apply", "T001", "--approve", "--strict", "--json", *arguments,
    ])
    assert result.exit_code == 1, result.output
    assert json.loads(result.output)["error"]["code"] == "verifier_unavailable"
    assert backend.get_task("T001").status.value == "needs_review"
    assert (state_dir / "events.jsonl").read_bytes() == before
