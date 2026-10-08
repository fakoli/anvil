"""Frozen profile checks through native managers and the existing proof gate."""
from pathlib import Path

import pytest

from anvil.bundles.manager import BundleError, BundleManager
from anvil.claims.manager import ClaimError, ClaimManager
from anvil.clock import FrozenClock
from anvil.review.gates import evaluate_claims, evidence_complete, evidence_missing_details
from anvil.state.models import CommandProof, Evidence, TaskStatus, Verification
from anvil.state.snapshot import serialize_state
from anvil.verification_profiles import MANIFEST, materialize_verification
from tests.test_bundle_execution import _NOW, _backend, _event, _seed
from tests.test_claims import _make_git_repo
from tests.test_verification_profiles import reference


def _profile(root: Path) -> Verification:
    (root / "tools").mkdir(exist_ok=True)
    (root / "tools/verify.py").write_text("raise AssertionError('must not execute')\n")
    return materialize_verification(Verification(profile=reference(root)), root)


def _setup(tmp_path, monkeypatch, *, profiles=True):
    root = _make_git_repo(tmp_path / "repo")
    verification = _profile(root) if profiles else Verification()
    state = tmp_path / "state"
    state.mkdir()
    backend = _backend(state)
    append = backend.append

    def seed_with_profile(draft, **kwargs):
        if draft.action == "task.created":
            draft = draft.model_copy(update={"payload_json": {
                **draft.payload_json,
                "verification": verification.model_dump(mode="json"),
            }})
        return append(draft, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(backend, "append", seed_with_profile)
        _seed(backend)
    # Ordinary claims use a separate task outside the execution bundle.
    ordinary = backend.get_task("release:T001")
    backend.append(_event("task.created", "task", "release:T003", {
        **ordinary.model_dump(mode="json"), "id": "release:T003",
        "prd_id": ordinary.prd_id,
    }))
    return root, state, backend


def _manager(backend, root, bundle):
    if bundle:
        return BundleManager(backend, FrozenClock(_NOW), actor="coordinator", project_root=root)
    return ClaimManager(backend, FrozenClock(_NOW), actor="author", project_root=root)


def _snapshot(backend, state):
    return (
        serialize_state(backend),
        backend.get_bundle("B001"),
        backend.get_bundle_claim("B001"),
        (state / "events.jsonl").read_bytes(),
    )


@pytest.mark.parametrize("bundle", [False, True])
def test_valid_profile_claims(tmp_path, monkeypatch, bundle):
    root, state, backend = _setup(tmp_path, monkeypatch)
    before = _snapshot(backend, state)
    _manager(backend, root, bundle).claim("B001" if bundle else "release:T003")
    assert len(backend.list_active_claims()) == (2 if bundle else 1)
    assert backend.get_task("release:T001" if bundle else "release:T003").status is TaskStatus.claimed
    assert _snapshot(backend, state) != before


@pytest.mark.parametrize("bundle", [False, True])
@pytest.mark.parametrize("change", ["root", "manifest", "runner", "binding"])
def test_profile_refusal_preserves_native_state(tmp_path, monkeypatch, bundle, change):
    root, state, backend = _setup(tmp_path, monkeypatch)
    if change == "manifest":
        (root / MANIFEST).write_text("private source text\n")
    elif change == "runner":
        (root / "tools/verify.py").write_text("private runner text\n")
    elif change == "binding":
        task = backend.get_task("release:T002" if bundle else "release:T003")
        _replace_verification(backend, task, Verification(profile=task.verification.profile))
    before = _snapshot(backend, state)
    manager = _manager(backend, None if change == "root" else root, bundle)
    error = BundleError if bundle else ClaimError
    code = {"root": "invalid_path", "manifest": "source_mismatch"}.get(change, "binding_mismatch")
    with pytest.raises(error, match=f"^verification profile refused: {code}$"):
        manager.claim("B001" if bundle else "release:T003")
    assert _snapshot(backend, state) == before
    if bundle:
        with pytest.raises(error, match=code):
            manager.preflight("B001")
        assert _snapshot(backend, state) == before


@pytest.mark.parametrize("bundle", [False, True])
@pytest.mark.parametrize("change", ["manifest", "runner"])
def test_callback_drift_is_checked_after_existing_guard(tmp_path, monkeypatch, bundle, change):
    root, state, backend = _setup(tmp_path, monkeypatch)
    before = _snapshot(backend, state)
    calls = []

    def source_guard():
        calls.append("existing guard")
        (root / (MANIFEST if change == "manifest" else "tools/verify.py")).write_text("changed")

    with pytest.raises(BundleError if bundle else ClaimError, match="verification profile refused"):
        _manager(backend, root, bundle).claim(
            "B001" if bundle else "release:T003", pre_log_check=source_guard,
        )
    assert calls == ["existing guard"]
    assert _snapshot(backend, state) == before


@pytest.mark.parametrize("bundle", [False, True])
def test_existing_guard_exception_preserved(tmp_path, monkeypatch, bundle):
    root, state, backend = _setup(tmp_path, monkeypatch)
    before = _snapshot(backend, state)
    refused = RuntimeError("canonical source guard refused")

    def source_guard():
        (root / MANIFEST).unlink()
        raise refused

    with pytest.raises(RuntimeError) as caught:
        _manager(backend, root, bundle).claim(
            "B001" if bundle else "release:T003", pre_log_check=source_guard,
        )
    assert caught.value is refused
    assert _snapshot(backend, state) == before


def _replace_verification(backend, task, verification):
    backend.append(_event("task.created", "task", task.id, {
        **task.model_dump(mode="json"), "prd_id": task.prd_id,
        "verification": verification.model_dump(mode="json"),
    }))


@pytest.mark.parametrize("bundle", [False, True])
def test_callback_persisted_contract_change_refused(tmp_path, monkeypatch, bundle):
    root, state, backend = _setup(tmp_path, monkeypatch)
    task = backend.get_task("release:T002" if bundle else "release:T003")
    expected = []

    def source_guard():
        # Native append simulates a changed persisted contract, not a mutated
        # in-memory snapshot. Only this deliberate edit may survive the refusal.
        _replace_verification(backend, task, Verification())
        expected.append(_snapshot(backend, state))

    with pytest.raises(BundleError if bundle else ClaimError, match="binding_mismatch"):
        _manager(backend, root, bundle).claim(
            "B001" if bundle else "release:T003", pre_log_check=source_guard,
        )
    assert len(expected) == 1
    assert _snapshot(backend, state) == expected[0]
    assert not backend.list_active_claims()


@pytest.mark.parametrize("bundle", [False, True])
@pytest.mark.parametrize("callback", [False, True])
def test_legacy_has_no_profile_reads_or_new_callback(tmp_path, monkeypatch, bundle, callback):
    root, state, backend = _setup(tmp_path, monkeypatch, profiles=False)

    def forbidden(*args, **kwargs):
        pytest.fail("legacy path touched profile files")

    monkeypatch.setattr("anvil.verification_profiles._read_file", forbidden)
    append = backend.append
    seen = []
    calls = []

    def guard():
        calls.append("guard")

    def observe(draft, **kwargs):
        seen.append(kwargs)
        return append(draft, **kwargs)

    monkeypatch.setattr(backend, "append", observe)
    kwargs = {"pre_log_check": guard} if callback else {}
    _manager(backend, None, bundle).claim("B001" if bundle else "release:T003", **kwargs)
    assert seen == [kwargs]
    assert calls == (["guard"] if callback else [])
    task = backend.get_task("release:T001")
    assert evaluate_claims(task, None, project_root=root).overall == "passed"


def _evidence(task, *, command=None, exit_code=0, category="completion"):
    return Evidence(
        id="EV001", task_id=task.id, claim_id="C001", submitted_by="reviewer",
        submitted_at=_NOW, category=category,
        proofs=[CommandProof(
            command=command or task.verification.commands[0], exit_code=exit_code,
            output_sha256="0" * 64, captured_at=_NOW,
        )],
    )


@pytest.mark.parametrize("change", ["manifest", "runner", "root", "binding"])
def test_old_successful_proof_cannot_pass_drift(tmp_path, monkeypatch, change):
    root, state, backend = _setup(tmp_path, monkeypatch)
    task = backend.get_task("release:T001")
    evidence = _evidence(task)
    assert evaluate_claims(task, evidence, project_root=root).overall == "passed"
    if change == "manifest":
        (root / MANIFEST).write_text("changed")
    elif change == "runner":
        (root / "tools/verify.py").write_text("changed")
    elif change == "binding":
        task = task.model_copy(update={"verification": Verification(profile=task.verification.profile)})
    verdict = evaluate_claims(task, evidence, project_root=None if change == "root" else root)
    assert verdict.overall == "failed"
    assert verdict.enforceable_unproven
    code = {"manifest": "source_mismatch", "root": "invalid_path"}.get(change, "binding_mismatch")
    assert verdict.enforceable_unproven[0].failures == [f"verification profile refused: {code}"]
    # These existing pure gates do not acquire profile filesystem authority.
    assert evidence_complete(task, evidence) == (True, [])
    assert evidence_missing_details(task, evidence) == ([], [])


@pytest.mark.parametrize("category,command,exit_code,verdict", [
    ("completion", "python tools/verify.py full", 0, "passed"),
    ("completion", "python tools/verify.py full ", 0, "incomplete"),
    ("completion", "python tools/verify.py full", 1, "incomplete"),
    ("completion", "python --version", 0, "incomplete"),
    ("diagnostic", "python tools/verify.py full", 0, "diagnostic_only"),
    ("advisory", "python tools/verify.py full", 0, "diagnostic_only"),
    ("blocked", "python tools/verify.py full", 0, "blocked"),
])
def test_profile_preserves_literal_proof_and_category_rules(
    tmp_path, monkeypatch, category, command, exit_code, verdict,
):
    root, state, backend = _setup(tmp_path, monkeypatch)
    task = backend.get_task("release:T001")
    evidence = _evidence(task, category=category, command=command, exit_code=exit_code)
    assert evaluate_claims(task, evidence, project_root=root).overall == verdict


def test_author_and_reviewer_resolve_independent_roots(tmp_path, monkeypatch):
    root, state, backend = _setup(tmp_path, monkeypatch)
    reviewer = _make_git_repo(tmp_path / "reviewer")
    assert _profile(reviewer) == backend.get_task("release:T001").verification
    (root / "fixture.txt").write_text("author fixture")
    (reviewer / "fixture.txt").write_text("independent reviewer fixture")
    task = backend.get_task("release:T001")
    evidence = _evidence(task)
    for checkout in (root, reviewer):
        assert evaluate_claims(task, evidence, project_root=checkout).overall == "passed"
    (root / "tools/verify.py").write_text("author drift")
    assert evaluate_claims(task, evidence, project_root=root).overall == "failed"
    assert evaluate_claims(task, evidence, project_root=reviewer).overall == "passed"
