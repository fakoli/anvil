"""Historical glob reservations retain lineage without weakening live claims."""

from pathlib import Path

import pytest

from anvil.state.backend import EventRejected
from anvil.state.models import ClaimStatus, Event, TaskStatus
from anvil.state.snapshot import serialize_state
from tests.test_bundle_state import (
    _backend,
    _bundle_claim_payload,
    _create_bundle,
    _event,
    _seed,
)
from tests.test_sqlite import (
    _make_backend,
    _make_claim_payload,
    _make_event,
    _make_evidence_payload,
    _setup_claimable_task,
)


def _legacy_claim(*, task_id="T001", paths=None, extra=None):
    payload = _make_claim_payload(
        task_id=task_id,
        expected_files=paths if paths is not None else ["src/**/*.py", "tests/test_?.py"],
    )
    payload.pop("generation")
    payload.update(extra or {})
    return _make_event("claim.created", payload, target_kind="claim", target_id="C001")


def _append_history(events: Path, *drafts):
    count = len(events.read_bytes().splitlines())
    with events.open("a", encoding="utf-8") as stream:
        for number, draft in enumerate(drafts, start=count + 1):
            event = Event(id=f"E{number:06d}", **draft.model_dump())
            stream.write(event.model_dump_json() + "\n")


def test_legacy_glob_claim_evidence_acceptance_survive_replay(tmp_path):
    backend = _make_backend(tmp_path)
    events = tmp_path / "events.jsonl"
    try:
        _setup_claimable_task(backend)
        claim = _legacy_claim()
        _append_history(
            events,
            claim,
            _make_event("evidence.submitted", _make_evidence_payload(),
                        target_kind="task", target_id="T001"),
            _make_event("task.applied", {
                "task_id": "T001", "reviewer": "reviewer", "decision": "accepted",
            }, target_kind="task", target_id="T001"),
        )
        original_log = events.read_bytes()
        backend.replay_from_empty(str(events))
        assert backend.get_task("T001").status is TaskStatus.done
        restored = backend.get_claim("C001")
        assert restored.status is ClaimStatus.released
        assert restored.expected_files == claim.payload_json["expected_files"]
        assert restored.attestation_context is None
        assert backend.get_latest_evidence("T001").claim_id == "C001"
        review = backend.list_reviews()[0]
        assert review.decision.value == "approve"
        assert backend._require_conn().execute(
            "SELECT review_attempt_id FROM reviews WHERE id = ?", (review.id,),
        ).fetchone()[0] is None
        snapshot = serialize_state(backend)
        backend.replay_from_empty(str(events))
        assert serialize_state(backend) == snapshot
        assert events.read_bytes() == original_log
    finally:
        backend.close()


def test_live_glob_claim_remains_refused_even_without_modern_fields(tmp_path):
    backend = _make_backend(tmp_path)
    try:
        _setup_claimable_task(backend)
        before = Path(backend._events_path).read_bytes()
        with pytest.raises(EventRejected, match="invalid claim file scope"):
            backend.append(_legacy_claim())
        assert backend.get_claim("C001") is None
        assert Path(backend._events_path).read_bytes() == before
    finally:
        backend.close()


@pytest.mark.parametrize("extra", [
    {"generation": 1}, {"generation": None}, {"actor_input": None},
    {"attestation_context": None}, {"git_metadata": None}, {"root_set": None},
])
def test_modern_glob_claim_cannot_replay_as_legacy(tmp_path, extra):
    backend = _make_backend(tmp_path)
    try:
        _setup_claimable_task(backend)
        _append_history(Path(backend._events_path), _legacy_claim(extra=extra))
        backend.replay_from_empty(backend._events_path)
        assert backend.get_claim("C001") is None
        assert backend.get_task("T001").status is TaskStatus.ready
    finally:
        backend.close()


@pytest.mark.parametrize("path", ["../*.py", "/src/*.py", "C:/src/*.py", "", 42])
def test_legacy_glob_exception_does_not_accept_invalid_path_structure(tmp_path, path):
    backend = _make_backend(tmp_path)
    try:
        _setup_claimable_task(backend)
        _append_history(Path(backend._events_path), _legacy_claim(paths=[path]))
        backend.replay_from_empty(backend._events_path)
        assert backend.get_claim("C001") is None
        assert backend.get_task("T001").status is TaskStatus.ready
    finally:
        backend.close()


def test_legacy_glob_cannot_bypass_active_bundle_reservation(tmp_path):
    backend = _backend(tmp_path)
    try:
        _seed(backend, second_prd=True)
        _create_bundle(backend)
        backend.append(_event(
            "bundle.claimed", _bundle_claim_payload(backend),
            target_kind="bundle", target_id="B001", actor="codex-main",
        ))
        bundle_claim = backend.get_bundle_claim("B001")
        _append_history(Path(backend._events_path), _legacy_claim(task_id="other:T001"))
        backend.replay_from_empty(backend._events_path)
        assert backend.get_claim("C001") is None
        assert backend.get_bundle_claim("B001") == bundle_claim
        assert backend.get_task("other:T001").status is TaskStatus.ready
    finally:
        backend.close()
