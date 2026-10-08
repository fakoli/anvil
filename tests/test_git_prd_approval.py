"""Bound Git approvals retain their exact reviewed ancestry through native replay."""

import json

import pytest

from anvil.state.backend import EventRejected
from anvil.state.models import Event
from tests.test_git_events import (
    _T0,
    _draft,
    _make_backend,
    _planning_graph_batch,
    _prd_parsed_payload,
    _prd_revised_payload,
)


def _reviewed(root, *, bound=True):
    backend = _make_backend(root)
    backend.append(_draft("project.created", {
        "id": "proj-1", "name": "Approval ancestry", "description": "",
        "created_at": _T0.isoformat(), "updated_at": _T0.isoformat(),
    }))
    content = backend.append(_draft(
        "prd.parsed", _prd_parsed_payload(expected_absent=True if bound else None),
        target_kind="prd", target_id="default",
    ))
    binding = {"expected_revision": 1}
    if bound:
        binding.update({
            "binding_version": 1, "content_event_id": content.id,
            "source_sha256": content.payload_json["source_sha256"],
            "material_sha256": content.payload_json["material_sha256"],
        })
    review = backend.append(_draft("prd.reviewed", {
        "project_id": "proj-1", "reviewer": "reviewer", **binding,
        **({"expected_status": "draft"} if bound else {}),
    }, target_kind="prd", target_id="default"))
    approval = _draft("prd.approved", {
        "project_id": "proj-1", "approver": "approver", **binding,
        **({"expected_status": "reviewed", "review_event_id": review.id} if bound else {}),
    }, target_kind="prd", target_id="default")
    return backend, content, review, approval


@pytest.mark.parametrize("unrelated_tail", [False, True])
def test_bound_approval_uses_exact_review_without_history_scan(
    tmp_path, monkeypatch, unrelated_tail,
):
    backend, content, review, approval = _reviewed(tmp_path)
    try:
        assert review.parent_event_id == content.id
        if unrelated_tail:
            tail = backend.append(_draft(
                "prd.parsed", _prd_parsed_payload(prd_id="other", expected_absent=True),
                target_kind="prd", target_id="other",
            ))
            assert tail.id != review.id
        path = tmp_path / "events.jsonl"
        before = path.read_bytes()

        def forbidden_scan():
            pytest.fail("cached approval must not parse full Git history")

        monkeypatch.setattr(backend, "_read_git_events_ordered", forbidden_scan)
        event = backend.append(approval)
        assert event.parent_event_id == review.id
        assert event.payload_json["review_event_id"] == review.id
        after = path.read_bytes()
        assert after.startswith(before)
        added = after[len(before):].splitlines()
        assert len(added) == 1 and Event.model_validate_json(added[0]) == event
        assert backend.get_prd("default").status.value == "approved"
    finally:
        backend.close()


def test_bound_approval_remains_approved_after_reopen_and_fresh_replay(tmp_path):
    backend, content, review, approval = _reviewed(tmp_path)
    backend.append(approval)
    expected = backend.get_prd("default")
    assert expected.status.value == "approved"
    original = (tmp_path / "events.jsonl").read_bytes()
    backend.close()
    reopened = _make_backend(tmp_path)
    try:
        assert reopened.get_prd("default") == expected
        reopened.replay_from_empty(str(tmp_path / "events.jsonl"))
        assert reopened.get_prd("default") == expected
        assert (tmp_path / "events.jsonl").read_bytes() == original
    finally:
        reopened.close()
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    # A union's physical order does not override the native causal chain.
    lines = original.splitlines(keepends=True)
    (fresh / "events.jsonl").write_bytes(b"".join(reversed(lines)))
    replayed = _make_backend(fresh)
    try:
        assert replayed.get_prd("default") == expected
        assert (fresh / "events.jsonl").read_bytes() == b"".join(reversed(lines))
        assert replayed.get_prd("default").review_event_id == review.id
        assert replayed.get_prd("default").content_event_id == content.id
    finally:
        replayed.close()


@pytest.mark.parametrize("field,value", [
    ("expected_revision", 2), ("source_sha256", "a" * 64),
    ("material_sha256", "b" * 64), ("content_event_id", "E-unrelated"),
    ("review_event_id", "E-unrelated"), ("review_event_id", 7),
])
def test_stale_or_malformed_approval_refuses_without_log_write(tmp_path, field, value):
    backend, _, _, approval = _reviewed(tmp_path)
    try:
        before = (tmp_path / "events.jsonl").read_bytes()
        current = backend.get_prd("default")
        approval.payload_json[field] = value
        with pytest.raises(EventRejected):
            backend.append(approval)
        assert (tmp_path / "events.jsonl").read_bytes() == before
        assert backend.get_prd("default") == current
    finally:
        backend.close()


def test_final_callback_refusal_precedes_parent_and_log(tmp_path, monkeypatch):
    backend, _, _, approval = _reviewed(tmp_path)
    try:
        before = (tmp_path / "events.jsonl").read_bytes()
        current = backend.get_prd("default")
        monkeypatch.setattr(backend, "_git_prd_cached_parent", lambda *_: pytest.fail("parent chosen"))

        def final_source_check():
            raise EventRejected("source changed before approval")

        with pytest.raises(EventRejected, match="source changed"):
            backend.append(approval, pre_log_check=final_source_check)
        assert (tmp_path / "events.jsonl").read_bytes() == before
        assert backend.get_prd("default") == current
    finally:
        backend.close()


def test_final_callback_cannot_replace_detached_review_binding(tmp_path):
    backend, _, review, approval = _reviewed(tmp_path)
    try:
        event = backend.append(approval, pre_log_check=lambda: approval.payload_json.update(
            review_event_id="E-mutated-raw-payload",
        ))
        assert event.payload_json["review_event_id"] == review.id
        assert event.parent_event_id == review.id
    finally:
        backend.close()


def test_legacy_revision_approval_keeps_content_parent_and_unbound_shape(tmp_path):
    backend, content, review, approval = _reviewed(tmp_path, bound=False)
    try:
        assert review.parent_event_id == content.id
        event = backend.append(approval)
        assert event.parent_event_id == content.id
        assert "binding_version" not in event.payload_json
        assert "review_event_id" not in event.payload_json
        line = json.loads((tmp_path / "events.jsonl").read_text().splitlines()[-1])
        assert line["payload_json"] == event.payload_json
    finally:
        backend.close()


def test_current_graph_and_revision_parents_keep_the_existing_chain(tmp_path):
    backend, content, _, _ = _reviewed(tmp_path)
    try:
        first = backend.append(_planning_graph_batch(
            "F1", ts=_T0, expected_prd_source_sha256=content.payload_json["source_sha256"],
        ))
        assert first.parent_event_id == content.id
        second = backend.append(_planning_graph_batch(
            "F2", ts=_T0, expected_prd_source_sha256=content.payload_json["source_sha256"],
        ))
        assert second.parent_event_id == first.id
        revised = backend.append(_draft(
            "prd.revised", {
                **_prd_revised_payload(expected_status="reviewed"), "status": "reviewed",
            },
            target_kind="prd", target_id="default",
        ))
        assert revised.parent_event_id == second.id
    finally:
        backend.close()
