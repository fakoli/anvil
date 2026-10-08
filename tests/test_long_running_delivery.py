"""Composed native delivery, correction and fresh ownership regressions."""

import hashlib
import json
from contextlib import contextmanager

import pytest
import typer

from anvil.attempt_view import read_attempt_view, read_evidence_preflight
from anvil.cli import hooks
from anvil.cli._helpers import _open_backend
from anvil.state.backend import EventRejected
from anvil.state.payloads import AcceptedAttemptInvalidation
from anvil.state.snapshot import serialize_state
from tests.test_attempt_surfaces_mcp import _files
from tests.test_bundle_execution import _event
from tests.test_profile_claims import _manager, _setup, _snapshot
from tests.test_profile_cli import _prepared
from tests.test_profile_live_boundaries import _claim_draft
from tests.test_profile_planning_cli import _invoke
from tests.test_timing_receipts import _receipt


@pytest.mark.parametrize("profiled", [False, True])
@pytest.mark.parametrize("workspace", [False, True])
def test_native_delivery_timing_correction_replay_and_fresh_generation(
    tmp_path, monkeypatch, profiled, workspace,
):
    if not profiled:
        monkeypatch.setattr("anvil.verification_profiles._read_file",
                            lambda *args: pytest.fail("legacy lifecycle read profile files"))
    root, state, _ = _prepared(tmp_path, monkeypatch, workspace=workspace, profiled=profiled)
    claimed = json.loads(_invoke(root, [
        "claim", "T001", "--shared-tree", "--actor", "author", "--json",
    ]).output)["data"]["claim"]
    monkeypatch.setenv("ANVIL_CLAIM_ID", claimed["id"])
    backend = _open_backend(state, project_root=root)
    try:
        task, claim = backend.get_task("T001"), backend.get_claim(claimed["id"])
        assert claim.git_metadata.target_path == str(root)
        at = claim.created_at.isoformat().replace("+00:00", "Z")
        attribution = {
            **{key: getattr(claim.attestation_context, key) for key in (
                "repository_id", "claim_start_sha", "prd_id", "prd_revision", "task_revision",
            )},
            "project_id": backend.get_project().id, "task_id": task.id,
            "claim_id": claim.id, "generation": claim.generation, "claimed_by": "author",
        }
        timing = _receipt(attribution=attribution, started_at=at, ended_at=at)
        receipt_file = tmp_path / "timing.json"
        before = task, claim
        for receipt in (timing, {**timing, "receipt_id": "interrupted-run", "outcome": "interrupted",
                                "ended_at": None, "exit_code": None, "elapsed_us": 500_000}):
            receipt_file.write_text(json.dumps(receipt))
            _invoke(root, ["progress", task.id, "tests", "--timing-file", str(receipt_file),
                           "--actor", "author", "--json"])
        assert (backend.get_task(task.id), backend.get_claim(claim.id)) == before
        assert backend.get_latest_evidence(task.id) is None
        preflight = read_evidence_preflight(state, task.id)
        attempt = read_attempt_view(state, task.id)
        assert preflight["event_cursor"] == attempt["event_cursor"]
        assert not any(proof["satisfied"] for proof in preflight["required_command_proofs"])
        totals = attempt["timing"]
        assert totals["complete_monotonic_execution_us"] == 1_500_000
        assert totals["interrupted_monotonic_observed_us"] == 500_000
        assert totals["summed_monotonic_execution_us"] == 2_000_000
        assert totals["utc_verification_elapsed_union_us"] == 0
        assert totals["forecast"] == "unavailable"
        assert attempt["custody"]["runner_stop"] == "unknown"

        # Real hook ingestion fixtures feed the actual public submit adapter.
        # Failed capture remains in the file alongside the passing captures.
        for command, code in [(task.verification.commands[0], 1),
                              *((command, 0) for command in task.verification.commands)]:
            with pytest.raises(typer.Exit) as captured:
                hooks.hook_capture_evidence(command=command, exit_code=code, actor="author",
                    stdout_file=None, stderr_file=None, output_sha256=None, cwd=root)
            assert captured.value.exit_code == 0
        buffer = state / ".evidence-buffer" / f"{claim.id}.json"
        retained_buffer = buffer.read_bytes()
        preflight = read_evidence_preflight(state, task.id)
        assert preflight["buffer"]["source_sha256"] == hashlib.sha256(retained_buffer).hexdigest()
        assert preflight["buffer"]["inspected_bytes"] == len(retained_buffer)
        assert preflight["buffer"]["valid_proof_count"] == len(task.verification.commands) + 1
        assert all(proof["satisfied"] for proof in preflight["required_command_proofs"])

        runner_file = root / "verify.py"
        runner_bytes = runner_file.read_bytes()
        if profiled:
            draft = _event("evidence.submitted", "task", task.id, {
                "task_id": task.id, "claim_id": claim.id, "submitted_by": "author",
                "evidence_id": "EV-CALLBACK", "commands_run": task.verification.commands,
                "files_changed": ["src/feature.txt"],
            })
            before = _snapshot(backend, state)
            with pytest.raises(EventRejected, match="verification profile refused"):
                backend.append(draft, pre_log_check=lambda: runner_file.write_bytes(b"drift\n"))
            assert _snapshot(backend, state) == before
            runner_file.write_bytes(runner_bytes)
        _invoke(root, ["submit", task.id, "--commands", ",".join(task.verification.commands),
                       "--files-changed", "src/feature.txt", "--actor", "author", "--json"])
        evidence = backend.get_latest_evidence(task.id)
        assert len(evidence.proofs) == len(task.verification.commands) + 1
        assert any(proof.exit_code == 1 for proof in evidence.proofs)
        assert buffer.read_bytes() == retained_buffer
        assert backend.get_claim(claim.id).status.value == "released"
        if profiled:
            draft = _event("task.applied", "task", task.id, {
                "schema_version": 1, "task_id": task.id, "decision": "accepted",
                "reviewer": "human", "review_attempt_id": evidence.id, "notes": "fixture",
            }, actor="human")
            before = _snapshot(backend, state)
            with pytest.raises(EventRejected, match="verification profile refused"):
                backend.append(draft, pre_log_check=lambda: runner_file.write_bytes(b"drift\n"))
            assert _snapshot(backend, state) == before
            runner_file.write_bytes(runner_bytes)
        _invoke(root, ["apply", task.id, "--approve", "--strict", "--reviewer", "human", "--json"])
        accepted = read_attempt_view(state, task.id)
        assert accepted["acceptance"]["accepted"]
        assert accepted["current"]["reviews"][0]["review_attempt_id"] == evidence.id
        historical_log = (state / "events.jsonl").read_bytes()
        manifest = root / "anvil-verification.toml"
        manifest_bytes = manifest.read_bytes()
        manifest.unlink()
        runner_file.unlink()

        # Replay and explicit correction retain original acceptance and proofs
        # while the old profile files are absent; neither invokes live guards.
        with monkeypatch.context() as patch:
            patch.setattr(backend, "_check_live_profiles", lambda *args: pytest.fail("live replay guard"))
            frozen = serialize_state(backend)
            backend.replay_from_empty(state / "events.jsonl")
            assert serialize_state(backend) == frozen
        assert (state / "events.jsonl").read_bytes() == historical_log
        binding = backend.acceptance_invalidation_binding(task.id)
        reference = AcceptedAttemptInvalidation(
            accepted_event_id=binding["accepted_event_id"], binding_digest=binding["binding_digest"],
            decision_id="composed-gap-review", reason="Fixture correction; keep prior proof bytes.",
            evidence_gap_reference="review/gap.json", evidence_gap_sha256="c" * 64,
            evidence_gap_reviewed_by="gap-reviewer", confirmed=True,
        )
        backend.invalidate_task_acceptance(task_id=task.id, reviewer="gap-reviewer",
                                          reference=reference, timestamp=backend._clock.now())
        corrected = read_attempt_view(state, task.id)
        assert not corrected["acceptance"]["accepted"]
        assert corrected["acceptance"]["latest_accepted_event_id"] == reference.accepted_event_id
        assert corrected["acceptance"]["invalidation"]["accepted_event_id"] == reference.accepted_event_id
        assert backend.get_latest_evidence(task.id) == evidence
        assert (state / "events.jsonl").read_bytes().startswith(historical_log)
        manifest.write_bytes(manifest_bytes)
        runner_file.write_bytes(runner_bytes)
        for old, new in (("drafted", "reviewed"), ("reviewed", "ready")):
            backend.append(_event("task.status_changed", "task", task.id,
                                 {"task_id": task.id, "from": old, "to": new}))
        _invoke(root, ["claim", task.id, "--shared-tree", "--actor", "author", "--json"])
        fresh = read_attempt_view(state, task.id)
        assert fresh["current"]["claim"]["generation"] == claim.generation + 1
        assert fresh["current"]["evidence"] is None
        assert fresh["history"]["evidence"][0]["id"] == evidence.id
        assert buffer.read_bytes() == retained_buffer
        fresh_preflight = read_evidence_preflight(state, task.id)
        assert fresh_preflight["buffer"]["status"] == "missing"
        assert not any(proof["satisfied"] for proof in fresh_preflight["required_command_proofs"])
        assert fresh_preflight["event_cursor"] == fresh["event_cursor"]
        final_log = (state / "events.jsonl").read_bytes()
        with monkeypatch.context() as patch:
            patch.setattr(backend, "_check_live_profiles", lambda *args: pytest.fail("live replay guard"))
            patch.setattr(backend, "_check_timing_noted", lambda *args: pytest.fail("live timing replay guard"))
            frozen = serialize_state(backend)
            backend.replay_from_empty(state / "events.jsonl")
            assert serialize_state(backend) == frozen
        assert (state / "events.jsonl").read_bytes() == final_log
        assert read_attempt_view(state, task.id) == fresh
        assert backend.get_latest_evidence(task.id) == evidence
        assert buffer.read_bytes() == retained_buffer
        bytes_before_read = _files(state)
        assert json.loads(_invoke(root, ["packet", task.id, "--attempt", "--format", "json"]).output) == fresh
        assert _files(state) == bytes_before_read
        _invoke(root, ["packet", task.id, "--format", "json"])
        assert (state / "packets").is_dir()  # Legacy packet writes remain supported.
    finally:
        backend.close()


def test_profiled_bundle_selected_state_final_guard_and_public_shared_frontier(tmp_path, monkeypatch):
    import anvil.attempt_view as views

    root, state, backend = _setup(tmp_path, monkeypatch)
    draft = _claim_draft(backend, root, monkeypatch, bundle=True)
    backend.close()
    monkeypatch.setenv("ANVIL_ROOT", str(root))
    implicit = _open_backend(state)
    try:
        before = _snapshot(implicit, state)
        with pytest.raises(EventRejected, match="identity_unavailable"):
            implicit.append(draft)
        assert _snapshot(implicit, state) == before
    finally:
        implicit.close()
    # Deliberately selected arbitrary library State needs its explicit root.
    backend = _open_backend(state, project_root=root)
    try:
        runner_file = root / "tools/verify.py"
        runner_bytes = runner_file.read_bytes()
        before = _snapshot(backend, state)
        with pytest.raises(EventRejected, match="verification profile refused"):
            backend.append(draft, pre_log_check=lambda: runner_file.write_bytes(b"callback drift\n"))
        assert _snapshot(backend, state) == before
        runner_file.write_bytes(runner_bytes)
        backend.append(draft)
        transaction = views.query_only_transaction
        frontiers = []

        @contextmanager
        def observed(*args):
            with transaction(*args) as resources:
                frontiers.append(resources[0])
                yield resources

        monkeypatch.setattr(views, "query_only_transaction", observed)
        monkeypatch.setattr("anvil.cli.packet_apply._resolve_state_dir", lambda cwd: state)
        bytes_before = _files(state)
        public = json.loads(_invoke(root, ["packet", "B001", "--attempt", "--bundle", "--format", "json"]).output)
        assert len(frontiers) == 1
        assert len(public["members"]) == 2
        claims = {claim.id: claim for claim in backend.list_active_claims()}
        for member in public["members"]:
            claim = claims[member["current"]["claim"]["id"]]
            assert claim.bundle_claim_id == backend.get_bundle_claim("B001").id
            assert claim.attestation_context is None
            assert member["task"]["verification"] == backend.get_task(claim.task_id).verification.model_dump(mode="json")
        assert public["custody"]["runner_stop"] == "unknown"
        assert public["source_delivery"]["deployed"] == "unknown"
        assert _files(state) == bytes_before
        runner_file.unlink()
        assert read_attempt_view(state, "B001", bundle=True) == public
        _manager(backend, root, True).release("B001", reason="runner stopped; replan")
        released = read_attempt_view(state, "B001", bundle=True)
        assert released["bundle"]["status"] == "replan_required"
        assert all(member["current"]["claim"]["status"] == "released" for member in released["members"])
    finally:
        backend.close()
