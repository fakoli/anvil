"""Bounded bundle attempts share a frontier and remain advisory."""

import pytest

from anvil.attempt_view import read_attempt_view
from anvil.project_snapshot import ProjectSnapshotError
from tests.test_bundle_execution import _backend, _seed
from tests.test_native_evidence_correction import accepted as accepted


def test_bundle_members_share_native_identity_and_frontier(tmp_path):
    backend = _backend(tmp_path)
    _seed(backend)
    result = read_attempt_view(tmp_path, "B001", bundle=True)
    assert result["identity"] == {
        "project_id": "proj",
        "prd_id": "release",
        "bundle_id": "B001",
    }
    assert [member["identity"]["stored_task_id"] for member in result["members"]] == [
        "release:T001",
        "release:T002",
    ]
    assert result["mutation_authority"] is False
    assert result == read_attempt_view(tmp_path, "B001", bundle=True)


def test_bundle_switch_is_strict(tmp_path):
    with pytest.raises(ProjectSnapshotError):
        read_attempt_view(tmp_path, "B001", bundle=1)


def test_bundle_lifecycle_attribution_and_checkpoint(tmp_path):
    from datetime import timedelta

    from anvil.bundles.manager import BundleManager
    from anvil.clock import FrozenClock
    from anvil.state.models import task_snapshot_revision
    from tests.test_bundle_execution import _NOW, _manager

    backend = _backend(tmp_path)
    _seed(backend)
    manager = _manager(backend, tmp_path)
    manager.claim("B001")
    view = read_attempt_view(tmp_path, "B001", bundle=True)
    for member in view["members"]:
        task = backend.get_task(member["identity"]["stored_task_id"])
        claim = member["current"]["claim"]
        assert claim["proof_attribution"]["task_revision"] == task_snapshot_revision(task)
        assert claim["proof_attribution"]["repository_id"] is None
        assert claim["bundle_claim_id"] is not None
    later = BundleManager(
        backend,
        FrozenClock(_NOW + timedelta(minutes=30)),
        actor="coordinator",
        project_root=tmp_path,
    )
    renewed = later.renew("B001")
    view = read_attempt_view(tmp_path, "B001", bundle=True)
    assert all(
        m["current"]["claim"]["lease_expires_at"]
        == renewed.lease_expires_at.isoformat().replace("+00:00", "Z")
        for m in view["members"]
    )
    from anvil.bundles.delivery import BundleDeliveryManager

    BundleDeliveryManager(
        backend, FrozenClock(_NOW + timedelta(minutes=30)), actor="coordinator"
    ).checkpoint("B001", commit_sha="a" * 40, pr_url="https://example.invalid/pr/1")
    delivery = read_attempt_view(tmp_path, "B001", bundle=True)["source_delivery"]
    assert delivery["recorded_status"] == "active"
    assert delivery["checkpoint"]["commit_sha"] == "a" * 40
    assert delivery["deployed"] == "unknown"
    assert delivery["checkpoint_is_delivery_proof"] is False
    later.release("B001", reason="replan")
    view = read_attempt_view(tmp_path, "B001", bundle=True)
    assert view["bundle"]["status"] == "replan_required"
    assert all(m["current"]["claim"]["status"] == "released" for m in view["members"])


@pytest.mark.parametrize("corruption", ["missing", "order", "foreign"])
def test_bundle_membership_must_match_immutable_creation(tmp_path, corruption):
    import sqlite3

    backend = _backend(tmp_path)
    _seed(backend)
    with sqlite3.connect(tmp_path / "state.db") as conn:
        if corruption == "missing":
            conn.execute("DELETE FROM execution_bundle_members WHERE position=1")
        elif corruption == "order":
            conn.execute("UPDATE execution_bundle_members SET position=position+3")
        else:
            conn.execute("UPDATE tasks SET prd_id='default' WHERE id='release:T002'")
    with pytest.raises(ProjectSnapshotError):
        read_attempt_view(tmp_path, "B001", bundle=True)


def test_bundle_one_frontier_and_cumulative_limits(tmp_path, monkeypatch):
    from contextlib import contextmanager

    import anvil.attempt_view as module

    backend = _backend(tmp_path)
    _seed(backend)
    original = module.query_only_transaction
    calls = []

    @contextmanager
    def counted(*args):
        calls.append(args)
        with original(*args) as resources:
            yield resources

    monkeypatch.setattr(module, "query_only_transaction", counted)
    before = (tmp_path / "events.jsonl").read_bytes()
    read_attempt_view(tmp_path, "B001", bundle=True)
    assert len(calls) == 1 and (tmp_path / "events.jsonl").read_bytes() == before
    # Each member fits separately; cumulative response accounting refuses the bundle.
    limit = 4000
    read_attempt_view(tmp_path, "release:T001", limits={"max_response_bytes": limit})
    with pytest.raises(ProjectSnapshotError) as error:
        read_attempt_view(tmp_path, "B001", bundle=True, limits={"max_response_bytes": limit})
    assert error.value.error.field == "max_response_bytes"
    with pytest.raises(ProjectSnapshotError):
        read_attempt_view(tmp_path, "B001", bundle=True, prd_id="default")


def test_native_invalidation_keeps_acceptance_history_and_redacts_private_reason(accepted):
    import json

    from tests.test_native_evidence_correction import _invalidate, _reference
    from tests.test_project_snapshot import _state_path

    reference = _reference(accepted).model_copy(
        update={
            "reason": "/private/reason",
            "evidence_gap_reference": "/private/gap",
        }
    )
    event = _invalidate(accepted, reference)
    root = _state_path(accepted)
    before = (root / "events.jsonl").read_bytes()
    view = read_attempt_view(root, "T001")
    assert view["acceptance"]["accepted"] is False
    assert view["acceptance"]["latest_accepted_event_id"] == reference.accepted_event_id
    assert view["acceptance"]["invalidation"]["event_id"] == event.id
    correction = view["current"]["reviews"][-1]
    assert correction["kind"] == "acceptance_invalidation"
    assert correction["counts_toward_quality_rejection"] is False
    assert correction["claim_id"] == "C001" and correction["generation"] == 1
    assert "/private/" not in json.dumps(view)
    assert (root / "events.jsonl").read_bytes() == before
    accepted.replay_from_empty(str(root / "events.jsonl"))
    assert read_attempt_view(root, "T001") == view


def test_complete_and_interrupted_bundle_totals_are_distinct():
    from anvil.attempt_timing import project_attempt_timing, project_bundle_timing
    from tests.test_attempt_timing import _facts, _observe
    from tests.test_timing_receipts import _receipt

    facts = _facts()
    _observe(facts, _receipt(ended_at="2026-10-08T01:00:04Z", elapsed_us=3_000_000))
    _observe(
        facts,
        _receipt(
            receipt_id="stop",
            outcome="interrupted",
            exit_code=None,
            ended_at=None,
            elapsed_us=500_000,
        ),
        event_id="E3",
    )
    timing = project_attempt_timing(**facts)
    assert timing["complete_monotonic_execution_us"] == 3_000_000
    assert timing["interrupted_monotonic_observed_us"] == 500_000
    aggregate = project_bundle_timing([{"timing": timing}, {"timing": timing}])
    assert aggregate["utc_verification_elapsed_union_us"] == 4_000_000
    assert aggregate["complete_monotonic_execution_us"] == 6_000_000
    assert aggregate["interrupted_monotonic_observed_us"] == 1_000_000
    assert aggregate["summed_monotonic_execution_us"] == 7_000_000
    assert aggregate["monotonic_total_status"] == "mixed_partial"


def _preflight_fixture(tmp_path):
    from tests.test_bundle_execution import _manager

    backend = _backend(tmp_path)
    _seed(backend)
    _manager(backend, tmp_path).claim("B001")
    return backend


def _capture(tmp_path, *, exit_code=0, attr_change=None, captured_at=None):
    import json

    from anvil.naming import task_claim_buffer_path
    from anvil.state.models import HookCommandAttribution, hook_command_semantic_digest
    from tests.test_bundle_execution import _NOW

    view = read_attempt_view(tmp_path, "release:T001")
    attr = view["current"]["claim"]["proof_attribution"]
    if attr_change:
        attr.update(attr_change)
    attribution = HookCommandAttribution.model_validate(attr)
    timestamp = captured_at or _NOW
    record = {
        "claim_id": attribution.claim_id,
        "attribution": attr,
        "timestamp": timestamp.isoformat(),
        "command": "pytest -q",
        "exit_code": exit_code,
        "output_sha256": "a" * 64,
        "semantic_digest": hook_command_semantic_digest(
            attribution=attribution,
            command="pytest -q",
            exit_code=exit_code,
            output_sha256="a" * 64,
            captured_at=timestamp,
        ),
    }
    path = task_claim_buffer_path(tmp_path / ".evidence-buffer", attribution.claim_id)
    assert path is not None
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(record) + "\n")
    return path


def test_preflight_missing_complete_failed_and_no_implicit_clock(tmp_path):
    from anvil.attempt_view import read_evidence_preflight

    _preflight_fixture(tmp_path)
    missing = read_evidence_preflight(tmp_path, "release:T001")
    assert missing["buffer"]["status"] == "missing"
    path = _capture(tmp_path, exit_code=1)
    before = path.read_bytes()
    result = read_evidence_preflight(tmp_path, "release:T001")
    assert result["schema_id"] == "anvil.state.evidence-preflight.v1"
    assert result["buffer"]["valid_proof_count"] == 1
    assert "lease_time_unobserved" in result["problems"]
    assert result["advisory_only"] is True and result["mutation_authority"] is False
    assert result["hard_proof_limits"] == {"max_items": 16, "max_bytes": 1048576}
    assert path.read_bytes() == before and result == read_evidence_preflight(
        tmp_path, "release:T001"
    )


@pytest.mark.parametrize("bad", ["duplicate", "identity", "expired", "overflow", "symlink"])
def test_preflight_refuses_or_labels_bad_complete_buffers(tmp_path, bad):
    from datetime import timedelta

    from anvil.attempt_view import read_evidence_preflight
    from tests.test_bundle_execution import _NOW

    _preflight_fixture(tmp_path)
    path = _capture(tmp_path, attr_change={"generation": 2} if bad == "identity" else None)
    if bad == "symlink":
        source = tmp_path / "private-buffer"
        path.rename(source)
        path.symlink_to(source)
    if bad in {"duplicate", "overflow"}:
        path.write_bytes(path.read_bytes() * (17 if bad == "overflow" else 2))
    if bad in {"overflow", "symlink"}:
        with pytest.raises(ProjectSnapshotError) as error:
            read_evidence_preflight(tmp_path, "release:T001")
        assert error.value.error.field == "buffer"
    else:
        result = read_evidence_preflight(
            tmp_path,
            "release:T001",
            observation_at=(_NOW + timedelta(hours=5 if bad == "expired" else 1))
            .isoformat()
            .replace("+00:00", "Z"),
        )
        expected = {
            "duplicate": "duplicate_semantic_digest",
            "identity": "attribution_mismatch",
            "expired": "claim_outside_lease",
        }[bad]
        assert expected in result["problems"]


@pytest.mark.parametrize(
    "exit_code,passing,satisfied", [(0, [0], True), (1, [0], False), (1, [1], True)]
)
def test_preflight_uses_pinned_native_command_requirement(
    tmp_path, monkeypatch, exit_code, passing, satisfied
):
    import tests.test_bundle_execution as fixture
    from anvil.attempt_view import read_evidence_preflight

    original = fixture._event

    def event(action, kind, target, payload, **kwargs):
        if action == "task.created":
            payload["verification"]["required_proofs"] = [
                {
                    "kind": "command",
                    "command": "pytest -q",
                    "label": "native pinned check",
                    "passing_exit_codes": passing,
                },
                {"kind": "command", "command": "different command", "label": "absent check"},
            ]
        return original(action, kind, target, payload, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(fixture, "_event", event)
        _preflight_fixture(tmp_path)
    _capture(tmp_path, exit_code=exit_code)
    result = read_evidence_preflight(tmp_path, "release:T001")
    assert [item["satisfied"] for item in result["required_command_proofs"]] == [satisfied, False]
    assert result["buffer"]["valid_proof_count"] == 1
    assert "required_command_proof_missing" in result["problems"]


def test_bundle_records_review_disposition_without_inventing_merge(tmp_path):
    from anvil.bundles.review import BundleReviewManager
    from anvil.clock import FrozenClock
    from anvil.state.models import ReviewDecision
    from tests.test_bundle_execution import _NOW, _implement_bundle

    backend = _backend(tmp_path)
    _implement_bundle(backend, tmp_path)
    for reviewer, angle in (
        ("reviewer-a", "correctness"),
        ("reviewer-b", "security"),
        ("reviewer-c", "integration"),
    ):
        BundleReviewManager(backend, FrozenClock(_NOW), actor=reviewer).record(
            "B001", review_round=1, angle=angle, decision=ReviewDecision.approve
        )
    BundleReviewManager(backend, FrozenClock(_NOW), actor="coordinator").finalize("B001")
    result = read_attempt_view(tmp_path, "B001", bundle=True)
    assert result["bundle"]["status"] == "reviewed_unintegrated"
    assert len(result["bundle"]["reviews"]) == 3
    assert result["source_delivery"]["recorded_status"] == "reviewed_unintegrated"
    assert result["source_delivery"]["deployed"] == "unknown"


def test_bundle_cumulative_transfer_budget_covers_repeated_rows(tmp_path, monkeypatch):
    import anvil.attempt_view as module

    backend = _backend(tmp_path)
    _seed(backend)
    original = module._rows
    seen = []

    def measured(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs.get("budget") is not None:
            seen.append(tuple(kwargs["budget"]))
        return result

    monkeypatch.setattr(module, "_rows", measured)
    read_attempt_view(tmp_path, "B001", bundle=True)
    assert all(
        before[0] <= after[0] and before[1] <= after[1]
        for before, after in zip(seen, seen[1:], strict=False)
    )
    assert seen[-1][0] > len((tmp_path / "events.jsonl").read_bytes().splitlines())
    with pytest.raises(ProjectSnapshotError) as error:
        read_attempt_view(
            tmp_path, "B001", bundle=True, limits={"max_event_records": seen[-1][0] - 1}
        )
    assert error.value.error.field == "max_event_records"


def test_preflight_inspects_buffer_inside_single_query_only_frontier(tmp_path, monkeypatch):
    from contextlib import contextmanager

    import anvil.attempt_view as module
    from anvil.claims import evidence_import

    _preflight_fixture(tmp_path)
    _capture(tmp_path)
    transaction, inspect = module.query_only_transaction, evidence_import.inspect_command_buffer
    held = []
    calls = []

    @contextmanager
    def observed(*args):
        with transaction(*args) as resources:
            held.append(resources[0])
            yield resources
            held.pop()

    def inspected(*args, **kwargs):
        assert len(held) == 1
        assert held[0].execute("PRAGMA query_only").fetchone()[0] == 1
        calls.append(1)
        return inspect(*args, **kwargs)

    monkeypatch.setattr(module, "query_only_transaction", observed)
    monkeypatch.setattr(evidence_import, "inspect_command_buffer", inspected)
    before = {
        path.name: path.read_bytes()
        for path in tmp_path.iterdir()
        if path.is_file() and not path.name.endswith("-shm")
    }
    module.read_evidence_preflight(tmp_path, "release:T001")
    assert calls == [1]
    assert before == {
        path.name: path.read_bytes()
        for path in tmp_path.iterdir()
        if path.is_file() and not path.name.endswith("-shm")
    }


@pytest.mark.parametrize("scope", ["default", "release"])
def test_bundle_default_and_named_four_digit_members(tmp_path, monkeypatch, scope):
    import tests.test_bundle_execution as fixture

    prefix = "" if scope == "default" else scope + ":"
    original_event, original_prd = fixture._event, fixture.append_exact_approved_prd

    def scoped(value):
        if isinstance(value, str):
            if value == "release":
                return scope
            if value.startswith("release:"):
                return prefix + ("T1000" if value == "release:T001" else value.split(":", 1)[1])
        if isinstance(value, list):
            return [scoped(item) for item in value]
        if isinstance(value, dict):
            return {key: scoped(item) for key, item in value.items()}
        return value

    def event(action, kind, target, payload, **kwargs):
        return original_event(action, kind, scoped(target), scoped(payload), **kwargs)

    def prd(backend, **kwargs):
        kwargs["prd_id"] = scope
        kwargs["parsed_payload"] = scoped(kwargs["parsed_payload"])
        kwargs["parsed_payload"]["is_default"] = scope == "default"
        return original_prd(backend, **kwargs)

    backend = _backend(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(fixture, "_event", event)
        patch.setattr(fixture, "append_exact_approved_prd", prd)
        _seed(backend)
    result = read_attempt_view(tmp_path, "B001", bundle=True)
    assert result["identity"]["prd_id"] == scope
    assert result["members"][0]["identity"]["task_id"] == "T1000"
    assert result["members"][0]["identity"]["stored_task_id"] == prefix + "T1000"


def test_invalidation_history_stays_separate_after_new_generation(accepted):
    from tests.test_native_evidence_correction import _invalidate, _reference
    from tests.test_project_snapshot import _state_path
    from tests.test_sqlite import _make_claim_payload, _make_event

    reference = _reference(accepted)
    correction = _invalidate(accepted, reference)
    for previous, following in (("drafted", "reviewed"), ("reviewed", "ready")):
        accepted.append(
            _make_event(
                "task.status_changed",
                {"task_id": "T001", "from": previous, "to": following},
                target_kind="task",
                target_id="T001",
            )
        )
    accepted.append(
        _make_event(
            "claim.created",
            _make_claim_payload(claim_id="C002", generation=2),
            target_kind="claim",
            target_id="C002",
        )
    )
    result = read_attempt_view(_state_path(accepted), "T001")
    assert result["current"]["claim"]["generation"] == 2
    assert result["current"]["evidence"] is None and result["current"]["reviews"] == []
    assert result["acceptance"]["accepted"] is False
    assert result["acceptance"]["invalidation"]["event_id"] == correction.id
    assert result["history"]["reviews"][-1]["kind"] == "acceptance_invalidation"
