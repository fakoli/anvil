"""The attempt view is complete, causal, bounded, and observational."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import timedelta

import pytest

import anvil.attempt_view as view_module
from anvil.attempt_view import read_attempt_view
from anvil.project_snapshot import ProjectSnapshotError
from anvil.read_contracts import ReadErrorCode
from anvil.roots import registry
from anvil.state.models import RejectionReasonCode
from anvil.state.sqlite import SqliteBackend
from tests.test_project_snapshot import _NOW, _event, _seed, _state_path
from tests.test_sqlite import (
    _make_applied_payload,
    _make_claim_command_proof,
    _make_claim_payload,
    _make_event,
    _make_evidence_payload,
    _make_rejected_applied_payload,
    _setup_claimable_task_and_claim,
)


@pytest.fixture
def populated(backend):
    _seed(backend)
    return backend


@pytest.fixture
def claimed(backend, tmp_path, monkeypatch):
    original = registry.RootSetRegistry
    monkeypatch.setattr(
        registry, "RootSetRegistry", lambda: original(tmp_path / "owner")
    )
    _setup_claimable_task_and_claim(backend)
    return backend


def _submit(backend, evidence_id="EV001", *, now=None, claim_id="C001"):
    kwargs = {} if now is None else {"now": now}
    return backend.append(
        _make_event(
            "evidence.submitted",
            _make_evidence_payload(evidence_id=evidence_id, claim_id=claim_id),
            target_kind="task",
            target_id="T001",
            **kwargs,
        )
    )


def _read(backend, task="T001", **kwargs):
    return read_attempt_view(_state_path(backend), task, **kwargs)


def _reclaim(backend):
    backend.append(
        _make_event(
            "task.applied",
            _make_rejected_applied_payload(backend),
            target_kind="task",
            target_id="T001",
        )
    )
    for previous, following in (("drafted", "reviewed"), ("reviewed", "ready")):
        backend.append(
            _make_event(
                "task.status_changed",
                {"task_id": "T001", "from": previous, "to": following},
                target_kind="task",
                target_id="T001",
            )
        )
    backend.append(
        _make_event(
            "claim.created",
            _make_claim_payload(claim_id="C002", generation=2),
            target_kind="claim",
            target_id="C002",
        )
    )


def test_scoping_complete_verification_and_stable_read_only_digest(populated):
    root = _state_path(populated)
    # Hold the writer connection open so SQLite's own last-close cleanup is irrelevant.
    before = {
        p.name: p.read_bytes()
        for p in root.iterdir()
        if p.is_file() and p.suffix != ".db-shm"
    }
    view = _read(populated)
    assert view == _read(populated)
    assert {
        p.name: p.read_bytes()
        for p in root.iterdir()
        if p.is_file() and p.suffix != ".db-shm"
    } == before
    assert view["task"]["verification"]["required_evidence"] == ["EVIDENCE_SECRET"]
    assert view["task"]["verification"]["commands"] == ["COMMAND_SECRET"]
    assert (
        view["task"]["verification"]["required_proofs"][0]["command"]
        == "PROOF_COMMAND_SECRET"
    )
    named = _read(populated, "T001", prd_id="named")
    assert named["identity"]["stored_task_id"] == "named:T001"
    assert named["dependencies"][0]["ref"]["prd_id"] == "default"
    assert named == _read(populated, "named:T001")
    with pytest.raises(ProjectSnapshotError) as error:
        _read(populated, "named:T001", prd_id="default")
    assert error.value.error.code == ReadErrorCode.missing_target
    text = json.dumps(view)
    for excluded in ("SOURCE_SUMMARY_SECRET", "IMPLEMENTATION_SECRET", "PATH_SECRET"):
        assert excluded not in text
    assert view["custody"]["runner_stop"] == "unknown"
    assert view["mutation_authority"] is False


@pytest.mark.parametrize("skew", [False, True])
def test_latest_evidence_is_causal_even_under_timestamp_ties_and_skew(claimed, skew):
    first = _submit(claimed, "EVZZZ")
    _reclaim(claimed)
    second = _submit(
        claimed,
        "EVAAA",
        claim_id="C002",
        now=first.timestamp - timedelta(minutes=1) if skew else first.timestamp,
    )
    view = _read(claimed)
    assert view["current"]["evidence"]["id"] == "EVAAA"
    assert view["current"]["evidence"]["event_id"] == second.id
    assert view["history"]["evidence"][0]["id"] == "EVZZZ"
    assert view["history"]["counts"]["evidence"] == 2


def test_acceptance_is_bound_to_exact_evidence_and_event(claimed):
    _submit(claimed)
    event = claimed.append(
        _make_event(
            "task.applied",
            _make_applied_payload(),
            target_kind="task",
            target_id="T001",
        )
    )
    view = _read(claimed)
    review = view["current"]["reviews"][0]
    assert review["review_attempt_id"] == "EV001"
    assert review["event_id"] == event.id
    assert view["acceptance"]["accepted"] is True
    assert view["acceptance"]["latest_accepted_event_id"] == event.id
    assert view["source_delivery"] == "unknown"


def test_process_rejection_is_retained(claimed):
    _submit(claimed)
    # As in test_sqlite, model the retained terminal process fact in disposable state.
    with sqlite3.connect(_state_path(claimed) / "state.db") as conn:
        conn.execute("UPDATE claims SET status = 'stale' WHERE id = 'C001'")
    payload = _make_rejected_applied_payload(
        claimed, reason_code=RejectionReasonCode.claim_stale
    )
    event = claimed.append(
        _make_event("task.applied", payload, target_kind="task", target_id="T001")
    )
    view = _read(claimed)
    review = view["current"]["reviews"][0]
    assert review["rejection_category"] == "process"
    assert review["counts_toward_accept_rate"] == 0
    assert review["event_id"] == event.id
    assert any(e["id"] == event.id for e in view["events"])


def test_new_claim_does_not_inherit_old_evidence(claimed):
    _submit(claimed)
    _reclaim(claimed)
    view = _read(claimed)
    assert view["current"]["claim"]["generation"] == 2
    assert view["current"]["evidence"] is None
    assert view["history"]["evidence"][0]["id"] == "EV001"


def test_proof_metadata_retains_timing_without_paths_or_output():
    proof = _make_claim_command_proof(
        output=b"SECRET RAW OUTPUT", cwd_relative="secret/local/path"
    )
    safe = view_module._proof(proof)
    assert safe["evidence_core"]["started_at"] == proof["evidence_core"]["started_at"]
    assert safe["semantic_digest"] == proof["semantic_digest"]
    assert safe["evidence_core"]["generation"] == 1
    serialized = json.dumps(safe)
    assert "output_base64" not in serialized and "cwd_" not in serialized
    assert "SECRET RAW OUTPUT" not in serialized
    assert view_module._proof({"kind": "command"})["duration_status"] == "unknown"


def test_limits_are_lower_only_and_total_gate_precedes_scan(populated, monkeypatch):
    for requested in (
        {"max_event_records": True},
        {"max_response_bytes": 65537},
        {"oops": 1},
    ):
        with pytest.raises(ProjectSnapshotError) as error:
            _read(populated, limits=requested)
        assert error.value.error.code == ReadErrorCode.invalid_request

    def forbidden(*args):
        pytest.fail("oversized log must not reach the frontier scanner")

    monkeypatch.setattr(view_module, "_event_cursor", forbidden)
    with pytest.raises(ProjectSnapshotError) as error:
        _read(populated, limits={"max_event_log_bytes": 1})
    assert error.value.error.field == "max_event_log_bytes"


def test_exact_log_record_and_count_boundaries(populated):
    data = (_state_path(populated) / "events.jsonl").read_bytes()
    lines = data.splitlines()
    limits = {
        "max_event_log_bytes": len(data),
        "max_event_records": len(lines),
        "max_event_bytes": max(map(len, lines)),
    }
    assert _read(populated, limits=limits)["event_cursor"]["event_count"] == len(lines)
    for field, value in limits.items():
        with pytest.raises(ProjectSnapshotError) as error:
            _read(populated, limits={field: value - 1})
        assert error.value.error.code == ReadErrorCode.limit_exceeded
        assert error.value.error.field == field


def test_oversized_projected_cell_refuses_before_json_parse(populated, monkeypatch):
    root = _state_path(populated)
    with sqlite3.connect(root / "state.db") as conn:
        conn.execute(
            "UPDATE tasks SET verification = ? WHERE id = 'T001'", ("x" * 262145,)
        )
    original = view_module._strict_json

    def guarded(raw):
        assert len(raw) <= 262144
        return original(raw)

    monkeypatch.setattr(view_module, "_strict_json", guarded)
    with pytest.raises(ProjectSnapshotError) as error:
        _read(populated)
    assert error.value.error.field == "max_cell_bytes"


def test_response_overflow_refuses_instead_of_trimming(populated):
    with pytest.raises(ProjectSnapshotError) as error:
        _read(populated, limits={"max_response_bytes": 400})
    assert error.value.error.field == "max_response_bytes"


def test_all_mandatory_evidence_survives_light_packet_sized_lists(populated):
    required = [f"Mandatory evidence {i}" for i in range(30)]
    task = populated.get_task("T001")
    verification = task.verification.model_dump(mode="json")
    verification["required_evidence"] = required
    with sqlite3.connect(_state_path(populated) / "state.db") as conn:
        conn.execute(
            "UPDATE tasks SET verification = ? WHERE id = 'T001'",
            (json.dumps(verification),),
        )
    assert _read(populated)["task"]["verification"]["required_evidence"] == required


def test_projected_review_attempt_cannot_drift_from_its_event(claimed):
    _submit(claimed)
    claimed.append(
        _make_event(
            "task.applied",
            _make_applied_payload(),
            target_kind="task",
            target_id="T001",
        )
    )
    with sqlite3.connect(_state_path(claimed) / "state.db") as conn:
        conn.execute("UPDATE reviews SET review_attempt_id = NULL")
    with pytest.raises(ProjectSnapshotError) as error:
        _read(claimed)
    assert error.value.error.code == ReadErrorCode.projection_not_converged


def test_malformed_deep_cell_returns_closed_refusal(populated):
    with sqlite3.connect(_state_path(populated) / "state.db") as conn:
        conn.execute(
            "UPDATE tasks SET verification = ? WHERE id = 'T001'",
            ("[" * 2000 + "0" + "]" * 2000,),
        )
    with pytest.raises(ProjectSnapshotError) as error:
        _read(populated)
    assert error.value.error.code == ReadErrorCode.invalid_hierarchy


def test_log_ahead_and_source_replacement_refuse_without_healing(
    populated, monkeypatch
):
    root = _state_path(populated)
    event_file = root / "events.jsonl"
    original = event_file.read_bytes()
    event_file.write_bytes(original + original.splitlines(keepends=True)[-1])
    before = event_file.read_bytes()
    with pytest.raises(ProjectSnapshotError):
        _read(populated)
    assert event_file.read_bytes() == before
    event_file.write_bytes(original)
    compose = view_module._compose

    def replaced(*args):
        result = compose(*args)
        replacement = root / "replacement"
        replacement.write_bytes(original)
        replacement.replace(event_file)
        return result

    monkeypatch.setattr(view_module, "_compose", replaced)
    with pytest.raises(ProjectSnapshotError) as error:
        _read(populated)
    assert error.value.error.code == ReadErrorCode.projection_not_converged


def test_thousands_of_history_records_are_bounded(populated):
    # Unrelated history is scanned consistently but does not bloat the attempt.
    for i in range(1000):
        populated.append(
            _event(
                "progress.noted",
                {
                    "task_id": "named:T001",
                    "actor": "snapshot-test",
                    "notes": str(i),
                    "noted_at": _NOW.isoformat(),
                },
                kind="task",
                target="named:T001",
            )
        )
    assert _read(populated)["event_cursor"]["event_count"] == 1008
    with pytest.raises(ProjectSnapshotError) as error:
        _read(populated, limits={"max_event_records": 1007})
    assert error.value.error.field == "max_event_records"
    # Complete associated history cannot fit: return a refusal, never a suffix.
    with pytest.raises(ProjectSnapshotError) as error:
        _read(populated, "named:T001")
    assert error.value.error.field == "max_response_bytes"


def test_reader_waits_for_writer_and_observes_one_frontier(
    populated, frozen_clock, monkeypatch
):
    root = _state_path(populated)
    populated.close()
    appended, commit, done = threading.Event(), threading.Event(), threading.Event()
    results, failures = [], []
    original = SqliteBackend._insert_event_row

    def writer():
        backend = SqliteBackend(
            db_path=str(root / "state.db"),
            events_path=str(root / "events.jsonl"),
            clock=frozen_clock,
        )
        backend.initialize()

        def paused(*args, **kwargs):
            appended.set()
            assert commit.wait(5)
            return original(backend, *args, **kwargs)

        monkeypatch.setattr(backend, "_insert_event_row", paused)
        try:
            backend.append(
                _event(
                    "progress.noted",
                    {
                        "task_id": "T001",
                        "actor": "snapshot-test",
                        "notes": "barrier",
                        "noted_at": _NOW.isoformat(),
                    },
                    kind="task",
                    target="T001",
                )
            )
        except Exception as exc:
            failures.append(exc)
        finally:
            backend.close()

    def reader():
        try:
            results.append(read_attempt_view(root, "T001"))
        except Exception as exc:
            failures.append(exc)
        finally:
            done.set()

    w = threading.Thread(target=writer)
    w.start()
    assert appended.wait(5)
    r = threading.Thread(target=reader)
    r.start()
    assert not done.wait(0.1)
    commit.set()
    w.join(5)
    r.join(5)
    assert not failures and done.is_set()
    assert results[0]["event_cursor"]["event_count"] == 9
