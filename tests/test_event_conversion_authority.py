"""Changing event IDs must never reinterpret historical approval or proof."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager

import pytest

from anvil.cli import migrate as migration
from anvil.state.models import Event
from tests.test_cli import _FULL_PRD_CONTENT, _recursive_file_manifest
from tests.test_event_format_migration_safety import _backend, _convert
from tests.test_git_events import (
    _FIXTURE_EVENTS,
    _T0,
    _build_conversion_eligible_project,
    _build_local_project,
    _run_in,
    _snap,
)

_REFERENCE_CASES = [
    ("prd.revised", "parent_content_event_id", "E000001"),
    ("prd.revised", "expected_lifecycle_content_event_id", "E000002"),
    ("prd.revised", "lineage_version", 1),
    ("prd.reviewed", "content_event_id", "E000001"),
    ("prd.reviewed", "binding_version", 1),
    ("prd.approved", "content_event_id", "E000001"),
    ("prd.approved", "review_event_id", "E000002"),
    ("prd.approved", "binding_version", 1),
    ("bundle.status_changed", "creation_event_id", "E000001"),
    ("bundle.agent_observed", "creation_event_id", "E000001"),
    ("bundle.review_recorded", "creation_event_id", "E000001"),
    ("bundle.review_recorded", "disposition_event_id", "E000002"),
    ("bundle.checkpoint_recorded", "creation_event_id", "E000001"),
    ("bundle.superseded", "creation_event_id", "E000001"),
    ("bundle.withdrawn", "creation_event_id", "E000001"),
    ("bundle.claimed", "creation_event_id", "E000001"),
    ("bundle.claimed", "expected_scope_digest", "a" * 64),
    ("bundle.progress_noted", "creation_event_id", "E000001"),
    ("task.applied", "decision", "accepted"),
    ("task.applied", "invalidation", {"accepted_event_id": "E000001", "binding_digest": "a" * 64}),
]


def _write_event(path, action, payload):
    event = Event(
        id="E000001", timestamp=_T0, actor="authority-fixture",
        action=action, target_kind="project", target_id="proj-1", payload_json=payload,
    )
    path.write_text(event.model_dump_json() + "\n")


@pytest.mark.parametrize("action,field,value", _REFERENCE_CASES)
def test_native_authority_fields_refuse_before_rewrite(tmp_path, action, field, value):
    events = tmp_path / "events.jsonl"
    _write_event(events, action, {field: value})
    before = events.read_bytes()
    with pytest.raises(migration._EventConversionRefused, match="cannot safely change event identities"):
        migration._converted_event_history(events)
    assert events.read_bytes() == before


@pytest.mark.parametrize("action,field,value", _REFERENCE_CASES[:8])
def test_nested_planning_authority_refuses(tmp_path, action, field, value):
    events = tmp_path / "events.jsonl"
    _write_event(events, "planning.batch_applied", {
        "schema_version": 1, "prd_id": "default",
        "expected_prd_revision": None if action == "prd.revised" else 1,
        "expected_prd_source_sha256": None,
        "operations": [{
            "action": action, "target_kind": "prd", "target_id": "default",
            "payload_json": {field: value},
        }],
    })
    with pytest.raises(migration._EventConversionRefused, match="cannot safely change event identities"):
        migration._converted_event_history(events)


def test_opaque_metadata_and_entity_ids_are_not_rewritten(tmp_path):
    events = tmp_path / "events.jsonl"
    payload = {"task_id": "E000001", "provider_metadata": {
        "content_event_id": "E000001", "decision": "accepted",
        "expected_scope_digest": "a" * 64,
    }}
    _write_event(events, "sync_mapping.upserted", payload)
    rewritten, mapping, dropped = migration._converted_event_history(events)
    assert not dropped and set(mapping) == {"E000001"}
    assert json.loads(rewritten)["payload_json"] == payload


@pytest.mark.parametrize("apply", [False, True])
def test_original_accepted_history_refuses_without_new_artifacts(tmp_path, apply):
    if apply and os.name == "nt":
        pytest.skip("Windows apply refuses before inspecting State")
    original_state = _build_local_project(tmp_path)
    state = tmp_path / ".anvil"
    original_events = (state / "events.jsonl").read_bytes()
    assert original_events == _FIXTURE_EVENTS.read_bytes()
    events = [json.loads(line) for line in original_events.splitlines()]
    assert len(events) == 24
    assert any(event["action"] == "task.applied" and event["payload_json"]["decision"] == "accepted"
               for event in events)
    # Export the fixture's existing acceptance through the native file-only
    # producer. This adds no approval event and uses the isolated test key dir.
    from anvil import signing
    from anvil.cli.packet_apply import emit_acceptance_proof
    from anvil.state.models import AcceptanceProof

    accepted = next(Event.model_validate(event) for event in events
                    if event["action"] == "task.applied"
                    and event["payload_json"]["decision"] == "accepted")
    backend = _backend(state)
    try:
        proof_path = emit_acceptance_proof(state, backend, accepted.target_id, accepted)
        assert proof_path is not None
    finally:
        backend.close()
    signed_proof = AcceptanceProof.model_validate_json(proof_path.read_text())
    assert signing.verify(signed_proof.public_key, signed_proof.signed_bytes(), signed_proof.signature)
    before = _recursive_file_manifest(state)
    result = _run_in(tmp_path, ["migrate-events", "--to", "git", *(["--yes"] if apply else [])])
    assert result.exit_code == 1, result.output
    assert "cannot safely change event identities" in result.output
    assert len(result.output) < 600
    assert _recursive_file_manifest(state) == before
    retained_proof = AcceptanceProof.model_validate_json(proof_path.read_text())
    assert retained_proof.event_range == signed_proof.event_range
    assert signing.verify(retained_proof.public_key, retained_proof.signed_bytes(), retained_proof.signature)
    reopened = _backend(state)
    try:
        assert _snap(reopened) == original_state
    finally:
        reopened.close()


def _approved_project(root):
    result = _run_in(root, ["init", "--name", "Authority fixture"])
    assert result.exit_code == 0, result.output
    (root / ".anvil" / "prd.md").write_text(_FULL_PRD_CONTENT)
    for command in (
        ["prd", "parse"], ["prd", "review"], ["prd", "review", "--approve"],
        ["plan", "--no-llm"], ["score"], ["review", "tasks"],
    ):
        result = _run_in(root, command)
        assert result.exit_code == 0, result.output


def _prd_authority(state):
    backend = _backend(state)
    try:
        prd = backend.get_prd()
        assert prd is not None
        return {name: getattr(prd, name) for name in (
            "status", "revision", "source_sha256", "material_sha256", "content_event_id",
            "lifecycle_revision", "lifecycle_source_sha256", "lifecycle_material_sha256",
            "lifecycle_content_event_id", "review_event_id",
        )}
    finally:
        backend.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX conversion admission")
def test_native_approved_prd_refusal_preserves_source_and_claim_readiness(tmp_path):
    _approved_project(tmp_path)
    state = tmp_path / ".anvil"
    authority = _prd_authority(state)
    assert authority["status"] == "approved"
    assert authority["content_event_id"] and authority["review_event_id"]
    before = _recursive_file_manifest(state)
    result = _convert(tmp_path)
    assert result.exit_code == 1 and "cannot safely change event identities" in result.output
    assert _recursive_file_manifest(state) == before
    assert _prd_authority(state) == authority
    claim = _run_in(tmp_path, ["claim", "T001", "--actor", "authority-fixture", "--json"])
    assert claim.exit_code == 0, claim.output
    events = [json.loads(line) for line in (state / "events.jsonl").read_text().splitlines()]
    assert not any(event["action"] == "task.applied" for event in events)


@pytest.mark.skipif(os.name == "nt", reason="POSIX conversion publication")
@pytest.mark.parametrize("history", ["accepted", "approved"])
@pytest.mark.parametrize("phase", ["after_marker", "after_events.jsonl", "after_database"])
def test_old_pending_authority_is_preserved_and_resume_refuses(tmp_path, monkeypatch, history, phase):
    if history == "accepted":
        _build_local_project(tmp_path)
    else:
        _approved_project(tmp_path)
    state = tmp_path / ".anvil"

    def interrupt(observed):
        if observed == phase:
            raise RuntimeError("fixture interruption of previously admitted conversion")

    # Reproduce a marker created by the earlier converter, which had no semantic
    # admission. Only this setup disables the new guard; resume uses real code.
    with monkeypatch.context() as old_converter:
        old_converter.setattr(migration, "_require_convertible_authority", lambda *_: None)
        old_converter.setattr(migration, "_EVENT_CONVERSION_PHASE_HOOK", interrupt)
        failed = _convert(tmp_path)
        assert failed.exit_code == 1, failed.output
        assert "fixture interruption" in failed.output
    marker = state / ".event-format-migration.json"
    assert marker.is_file()
    identities = {path.name: path.stat().st_ino for path in (marker, state / "state.db", state / ".state-operation.lock")}
    before = _recursive_file_manifest(state)
    resumed = _convert(tmp_path, resume=True)
    assert resumed.exit_code == 1, resumed.output
    assert "cannot safely change event identities" in resumed.output
    assert _recursive_file_manifest(state) == before
    assert {path.name: path.stat().st_ino for path in (marker, state / "state.db", state / ".state-operation.lock")} == identities


@pytest.mark.skipif(os.name == "nt", reason="POSIX conversion admission")
def test_authority_appended_after_preview_is_rechecked_under_custody(tmp_path, monkeypatch):
    import anvil.state.sqlite as sqlite_module

    _build_conversion_eligible_project(tmp_path)
    state = tmp_path / ".anvil"
    original_lock = sqlite_module._state_operation_lock
    appended = False
    captured = {}

    @contextmanager
    def append_before_custody(*args, **kwargs):
        nonlocal appended
        if not appended and kwargs.get("create"):
            appended = True
            # Use native CLI producers, never an invented approval payload.
            (state / "prd.md").write_text(_FULL_PRD_CONTENT)
            for command in (["prd", "parse"], ["prd", "review"], ["prd", "review", "--approve"]):
                result = _run_in(tmp_path, command)
                assert result.exit_code == 0, result.output
            captured.update(_recursive_file_manifest(state))
        with original_lock(*args, **kwargs) as token:
            yield token

    monkeypatch.setattr(sqlite_module, "_state_operation_lock", append_before_custody)
    result = _convert(tmp_path)
    assert appended, "late authority must be added after the preview, before apply custody"
    assert result.exit_code == 1 and "cannot safely change event identities" in result.output
    after = _recursive_file_manifest(state)
    assert {name: content for name, content in after.items() if name != ".state-operation.lock"} == captured
    assert not (state / ".event-format-migration.json").exists()
    assert _prd_authority(state)["status"] == "approved"
