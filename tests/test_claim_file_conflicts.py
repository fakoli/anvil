"""Equivalent file spellings retain native claim exclusion across replay."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from anvil.bundles.execution import bundle_execution_scope_digest
from anvil.bundles.manager import BundleError
from anvil.claims.manager import ClaimError, ClaimManager
from anvil.clock import SystemClock
from anvil.state.backend import EventRejected
from anvil.state.models import ClaimStatus, Event
from tests.test_bundle_command_proofs import project as project
from tests.test_bundle_state import _create_bundle, _event


def _set_task_paths(backend, task_id, paths):
    task = backend.get_task(task_id)
    payload = {**task.model_dump(mode="json"), "prd_id": task.prd_id, "likely_files": paths}
    backend.append(_event("task.created", payload, target_kind="task", target_id=task_id))


def _add_candidate(backend, paths):
    payload = backend.get_task("release:T001").model_dump(mode="json")
    payload.update(id="release:T003", prd_id="release", likely_files=paths, status="ready")
    backend.append(_event("task.created", payload, target_kind="task", target_id=payload["id"]))


def _bundle_draft(backend, bundle_id):
    bundle = backend.get_bundle(bundle_id)
    now = datetime.now(UTC)
    paths = list(dict.fromkeys(
        path for task_id in bundle.task_ids for path in backend.get_task(task_id).likely_files
    ))
    return _event("bundle.claimed", {
        "id": f"BC-{bundle_id}", "bundle_id": bundle_id,
        "creation_event_id": bundle.creation_event_id, "claimed_by": bundle.coordinator,
        "expected_files": paths,
        "member_claims": [{"id": f"C-{task_id}", "task_id": task_id}
                          for task_id in bundle.task_ids],
        "created_at": now.isoformat(), "last_heartbeat_at": now.isoformat(),
        "lease_expires_at": (now + timedelta(hours=1)).isoformat(),
    }, target_kind="bundle", target_id=bundle_id, actor=bundle.coordinator, timestamp=now)


def _ordinary_draft(paths, *, task_id="release:T003", claim_id="C-EXTERNAL", force=False):
    now = datetime.now(UTC)
    return _event("claim.created", {
        "id": claim_id, "task_id": task_id, "claimed_by": "other",
        "claim_type": "task", "status": "active", "expected_files": paths,
        "created_at": now.isoformat(), "last_heartbeat_at": now.isoformat(),
        "lease_expires_at": (now + timedelta(hours=1)).isoformat(), "force": force,
    }, target_kind="claim", target_id=claim_id, actor="other", timestamp=now)


def _assert_live_and_replay_refuse(backend, events, draft, target_claim_id, *, match="(conflicts|overlap)"):
    log_before = events.read_bytes()
    active_before = backend.list_active_claims()
    with pytest.raises(EventRejected, match=match):
        backend.append(draft)
    assert events.read_bytes() == log_before
    # A divergent log bypasses the live precheck. The reducer must still retain
    # the first reservation and refuse the later overlapping authorization.
    event = Event(id=f"E{len(log_before.splitlines()) + 1:06d}", **draft.model_dump())
    with events.open("a", encoding="utf-8") as handle:
        handle.write(event.model_dump_json() + "\n")
    backend.replay_from_empty(str(events))
    assert backend.list_active_claims() == active_before
    assert backend.get_claim(target_claim_id) is None


@pytest.mark.parametrize("alias", ["./src/shared.py", "src/./shared.py", r"src\shared.py", "SRC/shared.py"])
def test_scoped_bundle_reservation_blocks_alias_in_second_bundle(project, alias):
    backend, manager, _, events, _ = project
    _set_task_paths(backend, "release:T001", ["./src/shared.py"])
    _add_candidate(backend, [alias])
    _create_bundle(backend, "B002", ["release:T003"])
    first = manager.claim("B001", expected_scope_digest=bundle_execution_scope_digest(backend, "B001"))
    assert backend.get_claim(first.claim.member_claim_ids["release:T001"]).expected_files == ["src/shared.py"]
    with pytest.raises(BundleError, match="conflicts with active claims"):
        manager.preflight("B002")
    _assert_live_and_replay_refuse(backend, events, _bundle_draft(backend, "B002"), "C-release:T003")
    assert backend.get_bundle_claim("B002") is None
    assert backend.get_task("release:T003").status.value == "ready"


@pytest.mark.parametrize("alias", ["./src/shared.py", r"src\shared.py", "SRC/shared.py"])
def test_legacy_ordinary_reservation_blocks_canonical_bundle(project, alias):
    backend, manager, _, events, _ = project
    _set_task_paths(backend, "release:T001", ["src/shared.py"])
    _add_candidate(backend, [alias])
    backend.append(_ordinary_draft([alias]))
    assert backend.get_claim("C-EXTERNAL").expected_files == [alias]
    with pytest.raises(BundleError, match="conflicts with active claims"):
        manager.preflight("B001")
    _assert_live_and_replay_refuse(backend, events, _bundle_draft(backend, "B001"), "C-release:T001")
    assert backend.get_bundle_claim("B001") is None


@pytest.mark.parametrize("force", [False, True])
def test_bundle_reservation_blocks_ordinary_alias_even_with_force(project, force):
    backend, manager, _, events, _ = project
    _set_task_paths(backend, "release:T001", ["./src/shared.py"])
    _add_candidate(backend, [])
    manager.claim("B001", expected_scope_digest=bundle_execution_scope_digest(backend, "B001"))
    ordinary = ClaimManager(backend, SystemClock(), actor="other")
    assert ordinary.check_conflicts("release:T003", [r"src\shared.py"])[0].overlapping_files == ["src/shared.py"]
    _assert_live_and_replay_refuse(backend, events, _ordinary_draft([r"src\shared.py"], force=force), "C-EXTERNAL")


def test_ordinary_live_conflict_normalizes_legacy_side_and_preserves_force(project):
    backend, _, _, events, _ = project
    _add_candidate(backend, [])
    backend.append(_ordinary_draft(["./src/shared.py"]))
    task = backend.get_task("release:T003").model_dump(mode="json")
    task.update(id="release:T004", prd_id="release", status="ready")
    backend.append(_event("task.created", task, target_kind="task", target_id=task["id"]))
    draft = _ordinary_draft([r"src\shared.py"], task_id="release:T004", claim_id="C-OTHER")
    draft.actor = "third"
    draft.payload_json["claimed_by"] = "third"
    manager = ClaimManager(backend, SystemClock(), actor="third")
    assert manager.check_conflicts("release:T004", [r"src\shared.py"])
    before = events.read_bytes()
    with pytest.raises(EventRejected, match="expected_files overlap"):
        backend.append(draft)
    assert events.read_bytes() == before
    draft.payload_json["force"] = True
    backend.append(draft)
    assert backend.get_claim("C-OTHER") is not None


def test_empty_scope_preflight_reads_only_member_custody(project, monkeypatch):
    backend, manager, *_ = project
    reads = []
    original = backend.task_action_context

    def member_context(task_id):
        reads.append(task_id)
        return original(task_id)

    monkeypatch.setattr(backend, "task_action_context", member_context)
    monkeypatch.setattr(backend, "list_active_claims", lambda: pytest.fail("hydrated unrelated claims"))
    manager.preflight("B001")
    assert reads == backend.get_bundle("B001").task_ids


def test_empty_scope_preflight_still_refuses_expired_active_member(project, monkeypatch):
    backend, manager, *_ = project
    expired = SimpleNamespace(id="C-EXPIRED", task_id="release:T001", expected_files=[],
                              status=ClaimStatus.active, lease_expires_at=datetime(2000, 1, 1, tzinfo=UTC))
    monkeypatch.setattr(backend, "task_action_context", lambda _: ([expired], False))
    with pytest.raises(BundleError, match="conflicts with active claims"):
        manager.preflight("B001")


@pytest.mark.parametrize("path", ["../unsafe.py", "/unsafe.py"])
def test_invalid_ordinary_scope_fails_closed_without_logging(project, path):
    backend, _, _, events, _ = project
    _add_candidate(backend, [])
    manager = ClaimManager(backend, SystemClock(), actor="other")
    with pytest.raises(ClaimError, match="Invalid claim file scope"):
        manager.check_conflicts("release:T003", [path])
    _assert_live_and_replay_refuse(
        backend, events, _ordinary_draft([path]), "C-EXTERNAL", match="invalid claim file scope",
    )


@pytest.mark.parametrize("path", ["../unsafe.py", "/unsafe.py"])
def test_invalid_bundle_scope_fails_closed_without_logging(project, path):
    backend, manager, _, events, _ = project
    _set_task_paths(backend, "release:T001", [path])
    with pytest.raises(BundleError, match="Invalid claim file scope"):
        manager.preflight("B001")
    _assert_live_and_replay_refuse(
        backend, events, _bundle_draft(backend, "B001"), "C-release:T001", match="invalid claim file scope",
    )
