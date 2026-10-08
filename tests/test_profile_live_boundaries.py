"""Real raw append boundaries inspect the selected prepared Git checkout."""
from pathlib import Path

import pytest

from anvil.state.backend import EventRejected
from tests.test_bundle_execution import _event
from tests.test_claims import _git, _make_git_repo
from tests.test_profile_claims import _manager, _metadata, _profile, _setup, _snapshot


def _claim_draft(backend, root, monkeypatch, *, bundle=False):
    # Capture a real native producer draft before append, then exercise the
    # mandatory raw boundary independently of the optional manager callback.
    drafts = []

    def capture(draft, **kwargs):
        drafts.append(draft)
        raise RuntimeError("captured before append")

    metadata = _metadata(root)
    with monkeypatch.context() as patch:
        patch.setattr(backend, "append", capture)
        with pytest.raises(RuntimeError, match="captured before append"):
            _manager(backend, root, bundle).claim(
                "B001" if bundle else "release:T003",
                branch=metadata.branch, git_metadata=metadata,
            )
    assert len(drafts) == 1
    return drafts[0]


def _submit(backend, claim):
    task = backend.get_task(claim.task_id)
    draft = _event("evidence.submitted", "task", task.id, {
        "task_id": task.id, "claim_id": claim.id, "submitted_by": claim.claimed_by,
        "evidence_id": "EV-PROFILE", "commands_run": task.verification.commands,
        "files_changed": ["src/feature.txt"],
    })
    return draft


def _accept(task_id):
    return _event("task.applied", "task", task_id, {
        "schema_version": 1, "task_id": task_id, "decision": "accepted",
        "reviewer": "independent", "review_attempt_id": "EV-PROFILE", "notes": "fixture",
    }, actor="independent")


@pytest.mark.parametrize("bundle", [False, True])
@pytest.mark.parametrize("change", ["none", "root", "target", "subdirectory", "foreign", "canonical", "manifest", "runner", "callback"])
def test_raw_creation_uses_actual_target_or_refuses_without_mutation(
    tmp_path, monkeypatch, bundle, change,
):
    root, state, backend = _setup(tmp_path, monkeypatch, configured=change != "root")
    draft = _claim_draft(backend, root, monkeypatch, bundle=bundle)
    payload = dict(draft.payload_json)
    metadata = dict(payload["git_metadata"])
    if change == "target":
        metadata["target_path"] = str(tmp_path / "absent")
    elif change == "subdirectory":
        metadata["target_path"] = str(root / "tools")
    elif change in {"foreign", "canonical"}:
        foreign = _make_git_repo(tmp_path / "foreign")
        _profile(foreign)
        metadata["target_path" if change == "foreign" else "canonical_root"] = str(foreign)
    elif change in {"manifest", "runner"}:
        (root / ("anvil-verification.toml" if change == "manifest" else "tools/verify.py")).write_text("changed")
    payload["git_metadata"] = metadata
    draft = draft.model_copy(update={"payload_json": payload})
    before = _snapshot(backend, state)
    def mutate_source():
        (root / "tools/verify.py").write_text("changed")

    callback = mutate_source if change == "callback" else None
    if change == "none":
        backend.append(draft)
        assert len(backend.list_active_claims()) == (2 if bundle else 1)
        if bundle:
            assert all(claim.attestation_context is None for claim in backend.list_active_claims())
    else:
        with pytest.raises(EventRejected, match="verification profile refused"):
            backend.append(draft, pre_log_check=callback)
        assert _snapshot(backend, state) == before
    backend.close()


@pytest.mark.parametrize("action", ["evidence", "accepted"])
@pytest.mark.parametrize("change", ["none", "runner", "manifest", "callback", "advanced"])
def test_exact_originating_claim_guards_evidence_and_released_acceptance(
    tmp_path, monkeypatch, action, change,
):
    root, state, backend = _setup(tmp_path, monkeypatch)
    backend.append(_claim_draft(backend, root, monkeypatch))
    claim = backend.list_active_claims()[0]
    submission = _submit(backend, claim)
    if action == "accepted":
        backend.append(submission)
        assert backend.get_claim(claim.id).status.value == "released"
    if change in {"runner", "manifest"}:
        (root / ("tools/verify.py" if change == "runner" else "anvil-verification.toml")).write_text("changed")
    elif change == "advanced":
        (root / "src/feature.txt").write_text("legitimate source work")
        _git(root, "add", "src/feature.txt")
        _git(root, "commit", "-m", "advance source")
        assert _git(root, "rev-parse", "HEAD") != claim.git_metadata.claim_start_sha
    draft = submission if action == "evidence" else _accept(claim.task_id)
    before = _snapshot(backend, state)
    def mutate_source():
        (root / "tools/verify.py").write_text("changed")

    callback = mutate_source if change == "callback" else None
    if change in {"none", "advanced"}:
        backend.append(draft)
        if action == "accepted":
            assert backend.get_task(claim.task_id).status.value == "done"
    else:
        with pytest.raises(EventRejected, match="verification profile refused"):
            backend.append(draft, pre_log_check=callback)
        assert _snapshot(backend, state) == before
    backend.close()


@pytest.mark.parametrize("events_storage", ["local", "git"])
def test_replay_does_not_read_current_profile_files(tmp_path, monkeypatch, events_storage):
    root, state, backend = _setup(tmp_path, monkeypatch, events_storage=events_storage)
    backend.append(_claim_draft(backend, root, monkeypatch))
    claim = backend.list_active_claims()[0]
    backend.append(_submit(backend, claim))
    backend.append(_accept(claim.task_id))
    before = _snapshot(backend, state)
    accepted_task = backend.get_task(claim.task_id)
    released_claim = backend.get_claim(claim.id)
    (root / "anvil-verification.toml").unlink()
    (root / "tools/verify.py").unlink()
    monkeypatch.setattr(backend, "_check_live_profiles", lambda *args: pytest.fail("live guard in replay"))
    backend.replay_from_empty(Path(state / "events.jsonl"))
    assert backend.get_task(claim.task_id) == accepted_task
    assert backend.get_claim(claim.id) == released_claim
    assert (state / "events.jsonl").read_bytes() == before[3]
    if events_storage == "local":
        assert _snapshot(backend, state) == before
    backend.close()


@pytest.mark.parametrize("selection", ["explicit", "implicit", "mismatch"])
def test_open_helper_pairs_actual_root_with_state_without_inverse_mapping(
    tmp_path, monkeypatch, selection,
):
    from anvil.cli._helpers import _open_backend

    root, state, backend = _setup(tmp_path, monkeypatch)
    draft = _claim_draft(backend, root, monkeypatch)
    backend.close()
    monkeypatch.setenv("ANVIL_ROOT", str(root))
    if selection == "implicit":
        destination = root / ".anvil"
        state.rename(destination)
        state = destination
    opened = _open_backend(state, project_root=root if selection == "explicit" else None)
    before = _snapshot(opened, state)
    try:
        if selection == "mismatch":
            with pytest.raises(EventRejected, match="identity_unavailable"):
                opened.append(draft)
            assert _snapshot(opened, state) == before
        else:
            opened.append(draft)
            assert len(opened.list_active_claims()) == 1
    finally:
        opened.close()


def test_drift_does_not_block_native_release(tmp_path, monkeypatch):
    root, state, backend = _setup(tmp_path, monkeypatch)
    backend.append(_claim_draft(backend, root, monkeypatch))
    claim = backend.list_active_claims()[0]
    (root / "tools/verify.py").unlink()
    backend.append(_event("claim.released", "claim", claim.id, {
        "claim_id": claim.id, "released_by": claim.claimed_by, "release_reason": "stopped",
    }))
    assert backend.get_claim(claim.id).status.value == "released"
    backend.close()


def test_legacy_raw_claim_has_no_profile_or_repository_reads(tmp_path, monkeypatch):
    root, state, backend = _setup(tmp_path, monkeypatch, profiles=False, configured=False)

    def forbidden(*args, **kwargs):
        pytest.fail("legacy profile filesystem check")

    monkeypatch.setattr("anvil.verification_profiles._read_file", forbidden)
    monkeypatch.setattr("anvil.claims.progress_attestation.inspect_local_repository", forbidden)
    _manager(backend, None, False).claim("release:T003")
    assert len(backend.list_active_claims()) == 1
    backend.close()


@pytest.mark.parametrize("change", ["none", "authorization", "commands", "baseline"])
def test_root_set_profile_requires_exact_prepared_capability(tmp_path, monkeypatch, change):
    from anvil.roots.registry import (
        authorize_root_set_claim,
        live_claim_append_authorized,
        request_digest,
    )
    from anvil.state.models import RootSetClaimBinding, RootSetRootFact

    root, state, backend = _setup(tmp_path, monkeypatch)
    draft = _claim_draft(backend, root, monkeypatch)
    task = backend.get_task("release:T003")
    metadata = _metadata(root)
    fact = RootSetRootFact(
        root_id="project", repository_id="project", baseline_sha=metadata.claim_start_sha,
        canonical_root=str(root), claim_worktree=str(root), branch=metadata.branch,
        verification_commands=tuple(task.verification.commands),
    )
    binding = RootSetClaimBinding(
        request_id="profile-root", primary_root_id="project", request_digest="a" * 64,
        root_set_digest=request_digest({"primary_root_id": "project", "roots": [fact.model_dump(mode="json")]}),
        reservation_id="R" + "b" * 32, root_facts=(fact,),
    )
    authorized_binding = binding
    capability = authorize_root_set_claim(binding, {
        "request_id": binding.request_id, "digest": binding.request_digest,
        "reservation_id": binding.reservation_id, "state": "pending",
    })
    if change in {"commands", "baseline"}:
        altered = fact.model_copy(update={
            "verification_commands": () if change == "commands" else fact.verification_commands,
            "baseline_sha": "c" * 40 if change == "baseline" else fact.baseline_sha,
        })
        binding = binding.model_copy(update={"root_facts": (altered,)})
    draft = draft.model_copy(update={"payload_json": {
        **draft.payload_json, "root_set": binding.model_dump(mode="json"),
    }})
    before = _snapshot(backend, state)
    if change == "authorization":
        with pytest.raises(EventRejected, match="verification profile refused"):
            backend.append(draft)
        assert _snapshot(backend, state) == before
    elif change in {"commands", "baseline"}:
        # A genuine capability for original facts cannot authorize substitution.
        with live_claim_append_authorized(binding=authorized_binding, authorization=capability):
            with pytest.raises(EventRejected):
                backend.append(draft)
        assert _snapshot(backend, state) == before
    else:
        with live_claim_append_authorized(binding=binding, authorization=capability):
            backend.append(draft)
        assert len(backend.list_active_claims()) == 1
    backend.close()


@pytest.mark.parametrize("action", ["claim", "bundle", "evidence", "accepted"])
@pytest.mark.parametrize("change", ["remove", "replace", "title"])
def test_raw_callback_cannot_change_prepared_profile_task(tmp_path, monkeypatch, action, change):
    from anvil.state.models import Verification
    from tests.test_profile_claims import _replace_verification

    root, state, backend = _setup(tmp_path, monkeypatch)
    draft = _claim_draft(backend, root, monkeypatch, bundle=action == "bundle")
    if action in {"evidence", "accepted"}:
        backend.append(draft)
        claim = backend.list_active_claims()[0]
        draft = _submit(backend, claim)
        if action == "accepted":
            backend.append(draft)
            draft = _accept(claim.task_id)
    task_ids = ["release:T001", "release:T002"] if action == "bundle" else ["release:T003"]
    callback_state = []

    def replace_contract():
        for task_id in task_ids:
            task = backend.get_task(task_id)
            if change == "title":
                backend.append(_event("task.created", "task", task_id, {
                    **task.model_dump(mode="json"), "prd_id": task.prd_id,
                    "title": "changed native task",
                }))
            else:
                verification = Verification() if change == "remove" else task.verification.model_copy(
                    update={"manual_steps": ["changed required verification"]},
                )
                _replace_verification(backend, task, verification)
        (root / "tools/verify.py").write_text("changed after contract replacement")
        callback_state.append(_snapshot(backend, state))

    with pytest.raises(EventRejected):
        backend.append(draft, pre_log_check=replace_contract)
    assert _snapshot(backend, state) == callback_state[0]
    backend.close()


@pytest.mark.parametrize("change", ["forged", "stale"])
def test_profile_creation_requires_current_frozen_task_context(tmp_path, monkeypatch, change):
    from anvil.state.models import task_snapshot_revision

    root, state, backend = _setup(tmp_path, monkeypatch)
    draft = _claim_draft(backend, root, monkeypatch)
    payload = dict(draft.payload_json)
    context = dict(payload["attestation_context"])
    if change == "forged":
        context["task_revision"] = "c" * 64
        payload["attestation_context"] = context
        draft = draft.model_copy(update={"payload_json": payload})
    else:
        task = backend.get_task("release:T003")
        backend.append(_event("task.created", "task", task.id, {
            **task.model_dump(mode="json"), "prd_id": task.prd_id,
            "title": "legitimate newer native task",
        }))
        assert task_snapshot_revision(backend.get_task(task.id)) != context["task_revision"]
    before = _snapshot(backend, state)
    with pytest.raises(EventRejected, match="verification profile refused"):
        backend.append(draft)
    assert _snapshot(backend, state) == before
    backend.close()


def test_profile_evidence_rechecks_exact_custody_after_callback(tmp_path, monkeypatch):
    root, state, backend = _setup(tmp_path, monkeypatch)
    backend.append(_claim_draft(backend, root, monkeypatch))
    claim = backend.list_active_claims()[0]
    draft = _submit(backend, claim)
    callback_state = []

    def change_generation():
        backend.append(_event("claim.released", "claim", claim.id, {
            "claim_id": claim.id, "released_by": claim.claimed_by, "release_reason": "runner stopped",
        }))
        metadata = _metadata(root)
        _manager(backend, root, False).claim(
            claim.task_id, branch=metadata.branch, git_metadata=metadata,
        )
        assert backend.list_active_claims()[0].generation == claim.generation + 1
        callback_state.append(_snapshot(backend, state))

    with pytest.raises(EventRejected):
        backend.append(draft, pre_log_check=change_generation)
    assert _snapshot(backend, state) == callback_state[0]
    backend.close()


@pytest.mark.parametrize("callback", [False, True])
def test_profile_evidence_refuses_expired_exact_claim(tmp_path, monkeypatch, callback):
    root, state, backend = _setup(tmp_path, monkeypatch)
    backend.append(_claim_draft(backend, root, monkeypatch))
    claim = backend.list_active_claims()[0]
    draft = _submit(backend, claim)
    before = _snapshot(backend, state)

    def expire():
        backend._clock.advance(hours=4)

    if not callback:
        expire()
    with pytest.raises(EventRejected, match="verification profile refused"):
        backend.append(draft, pre_log_check=expire if callback else None)
    assert _snapshot(backend, state) == before
    backend.close()
