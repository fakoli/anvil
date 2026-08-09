"""Qualification tests for the atomic provider project snapshot."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from typer.testing import CliRunner

import anvil.project_snapshot as snapshot_module
from anvil.cli import app
from anvil.clock import FrozenClock
from anvil.project_snapshot import ProjectSnapshotError, read_project_snapshot
from anvil.read_contracts import (
    ProviderLimitNameV1,
    ProviderLimitRefusalV1,
    ReadErrorCode,
    ReadErrorV1,
    lowered_limits,
    snapshot_response_canonical_bytes,
)
from anvil.state.backend import SchemaProbeFailed
from anvil.state.models import Event, EventDraft
from anvil.state.sqlite import SqliteBackend

_NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
_RUNNER = CliRunner()


def _event(
    action: str,
    payload: dict[str, object],
    *,
    kind: str,
    target: str,
) -> EventDraft:
    return EventDraft(
        timestamp=_NOW,
        actor="snapshot-test",
        action=action,
        target_kind=kind,
        target_id=target,
        payload_json=payload,
    )


def _project_payload() -> dict[str, object]:
    return {
        "id": "project-1",
        "name": "Snapshot Project",
        "description": "PROJECT_DESCRIPTION_SECRET",
        "created_at": _NOW.isoformat(),
        "updated_at": _NOW.isoformat(),
    }


def _prd_payload(
    prd_id: str,
    *,
    title: str,
    source: bytes | None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "project_id": "project-1",
        "prd_id": prd_id,
        "is_default": prd_id == "default",
        "expected_absent": True,
        "title": title,
        "status": "approved",
        "summary": "SOURCE_SUMMARY_SECRET",
        "goals": [],
        "non_goals": [],
        "requirements": [],
        "acceptance_criteria": [],
        "risks": [],
        "open_questions": [],
    }
    if source is not None:
        payload.update(
            {
                "source_text": source.decode("utf-8"),
                "source_sha256": hashlib.sha256(source).hexdigest(),
                "source_size_bytes": len(source),
                "source_encoding": "utf-8",
                "source_revision": 1,
                "provenance_state": "available",
                "content_available": True,
            }
        )
    return payload


def _feature_payload(stored_id: str, prd_id: str) -> dict[str, object]:
    return {
        "id": stored_id,
        "prd_id": prd_id,
        "title": f"Feature {prd_id}",
        "description": "FEATURE_DESCRIPTION_SECRET",
        "status": "ready",
        "requirements": [],
        "tasks": [],
    }


def _task_payload(
    stored_id: str,
    feature_id: str,
    prd_id: str,
    *,
    dependencies: list[str],
) -> dict[str, object]:
    return {
        "id": stored_id,
        "feature_id": feature_id,
        "prd_id": prd_id,
        "title": f"Task {prd_id}",
        "description": "TASK_DESCRIPTION_SECRET",
        "status": "ready",
        "priority": "high",
        "dependencies": dependencies,
        "conflict_groups": ["CONFLICT_SECRET"],
        "scores": {},
        "acceptance_criteria": ["Visible criterion"],
        "implementation_notes": ["IMPLEMENTATION_SECRET"],
        "verification": {
            "commands": ["COMMAND_SECRET"],
            "manual_steps": ["MANUAL_SECRET"],
            "required_evidence": ["EVIDENCE_SECRET"],
            "required_proofs": [
                {
                    "kind": "command",
                    "command": "PROOF_COMMAND_SECRET",
                    "passing_exit_codes": [0],
                    "label": "PROOF_LABEL_SECRET",
                }
            ],
        },
        "likely_files": ["PATH_SECRET"],
        "parent_task_id": None,
        "created_at": _NOW.isoformat(),
        "updated_at": _NOW.isoformat(),
    }


def _seed(backend: SqliteBackend) -> None:
    backend.append(
        _event("project.created", _project_payload(), kind="project", target="project-1")
    )
    backend.append(
        _event("state.initialized", {}, kind="project", target="project-1")
    )
    backend.append(
        _event(
            "prd.parsed",
            _prd_payload("default", title="", source=None),
            kind="prd",
            target="default",
        )
    )
    source = "# Project: Named\r\nNFD: e\u0301\r\n".encode()
    backend.append(
        _event(
            "prd.parsed",
            _prd_payload("named", title="Named", source=source),
            kind="prd",
            target="named",
        )
    )
    for stored, prd in (("F001", "default"), ("named:F001", "named")):
        backend.append(
            _event(
                "feature.created",
                _feature_payload(stored, prd),
                kind="feature",
                target=stored,
            )
        )
    backend.append(
        _event(
            "task.created",
            _task_payload("T001", "F001", "default", dependencies=[]),
            kind="task",
            target="T001",
        )
    )
    backend.append(
        _event(
            "task.created",
            _task_payload(
                "named:T001",
                "named:F001",
                "named",
                dependencies=["T001"],
            ),
            kind="task",
            target="named:T001",
        )
    )


@pytest.fixture
def populated(backend: SqliteBackend) -> SqliteBackend:
    _seed(backend)
    return backend


def _state_path(backend: SqliteBackend) -> Path:
    return Path(backend._db_path).parent  # noqa: SLF001 - test boundary


@pytest.fixture
def cli_populated(
    tmp_path: Path,
    frozen_clock: FrozenClock,
) -> tuple[Path, Path]:
    state_dir = tmp_path / ".anvil"
    state_dir.mkdir()
    events_path = state_dir / "events.jsonl"
    events_path.touch()
    backend = SqliteBackend(
        db_path=str(state_dir / "state.db"),
        events_path=str(events_path),
        clock=frozen_clock,
    )
    backend.initialize()
    _seed(backend)
    backend.close()
    return tmp_path, state_dir


def _invoke_project_snapshot(root: Path, *args: str):  # type: ignore[no-untyped-def]
    original_cwd = os.getcwd()
    os.chdir(root)
    try:
        return _RUNNER.invoke(
            app,
            ["project", "snapshot", *args],
            catch_exceptions=False,
        )
    finally:
        os.chdir(original_cwd)


def _state_bytes(root: Path) -> tuple[bytes, bytes]:
    return (
        root.joinpath("state.db").read_bytes(),
        root.joinpath("events.jsonl").read_bytes(),
    )


def _read_error_schema() -> dict[str, object]:
    path = (
        Path(__file__).resolve().parents[1]
        / "bin/src/anvil/_data/contracts/provider-reads/v1/read-error.schema.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def test_project_snapshot_cli_success_is_closed_json_and_read_only(
    cli_populated: tuple[Path, Path],
) -> None:
    root, state_dir = cli_populated
    before = _state_bytes(state_dir)

    result = _invoke_project_snapshot(root, "--json")

    assert result.exit_code == 0, result.output
    assert not result.stderr
    assert len(result.stdout.strip().splitlines()) == 1
    envelope = json.loads(result.stdout)
    assert envelope["ok"] is True
    assert envelope["command"] == "project snapshot"
    data = envelope["data"]
    assert data["operation_id"] == "state.project.snapshot"
    assert data["operation_version"] == 1
    assert data["output_schema_id"] == "anvil.state.project-snapshot.v1"
    assert data["api_version"] == "12"
    assert data["schema_version"] == 20
    assert data["digest_algorithm"] == "sha256"
    assert data["truncated"] is False
    assert data["payload"]["schema_id"] == data["output_schema_id"]
    assert data["payload"]["project"] == {
        "project_id": "project-1",
        "name": "Snapshot Project",
    }
    assert len(data["payload"]["prds"]) == 2
    assert len(data["payload"]["features"]) == 2
    assert len(data["payload"]["tasks"]) == 2
    wire = result.stdout.encode("utf-8")
    for secret in (
        b"PROJECT_DESCRIPTION_SECRET",
        b"SOURCE_SUMMARY_SECRET",
        b"FEATURE_DESCRIPTION_SECRET",
        b"TASK_DESCRIPTION_SECRET",
        b"COMMAND_SECRET",
        b"MANUAL_SECRET",
        b"EVIDENCE_SECRET",
        b"PROOF_COMMAND_SECRET",
        b"PATH_SECRET",
    ):
        assert secret not in wire
    assert _state_bytes(state_dir) == before


def test_project_snapshot_cli_requires_json_without_state_access(
    tmp_path: Path,
) -> None:
    result = _invoke_project_snapshot(tmp_path)

    assert result.exit_code == 1
    assert not result.stderr
    assert len(result.stdout.strip().splitlines()) == 1
    envelope = json.loads(result.stdout)
    assert envelope["ok"] is False
    assert envelope["command"] == "project snapshot"
    assert envelope["error"]["code"] == "invalid_request"
    assert envelope["error"]["field"] == "request"
    assert envelope["error"]["truncated"] is False
    Draft202012Validator(_read_error_schema()).validate(envelope["error"])
    assert not (tmp_path / ".anvil").exists()


def test_project_snapshot_cli_limit_refusal_has_no_partial_payload(
    cli_populated: tuple[Path, Path],
) -> None:
    root, state_dir = cli_populated
    before = _state_bytes(state_dir)

    result = _invoke_project_snapshot(
        root,
        "--json",
        "--limit",
        "max_tasks=1",
    )

    assert result.exit_code == 1
    assert not result.stderr
    envelope = json.loads(result.stdout)
    assert "data" not in envelope
    error = envelope["error"]
    assert error == {
        "operation_id": "state.project.snapshot",
        "operation_version": 1,
        "limit_name": "max_tasks",
        "actual": 2,
        "limit": 1,
        "code": "limit_exceeded",
        "message": "A provider read limit was exceeded.",
        "truncated": False,
    }
    assert "payload" not in result.stdout
    Draft202012Validator(_read_error_schema()).validate(error)
    assert _state_bytes(state_dir) == before


def test_project_snapshot_cli_rejects_malformed_or_duplicate_limits_prelookup(
    tmp_path: Path,
) -> None:
    for arguments in (
        ("--limit", "unknown=1"),
        ("--limit", "max_tasks=+1"),
        ("--limit", "max_tasks=1", "--limit", "max_tasks=1"),
    ):
        result = _invoke_project_snapshot(tmp_path, "--json", *arguments)
        assert result.exit_code == 1
        assert not result.stderr
        envelope = json.loads(result.stdout)
        assert envelope["error"]["code"] == "invalid_request"
        assert envelope["error"]["field"] == "request"
        assert str(tmp_path.resolve()) not in result.stdout
        assert not (tmp_path / ".anvil").exists()


def test_projects_default_named_blank_title_provenance_and_redaction(
    populated: SqliteBackend,
) -> None:
    result = read_project_snapshot(_state_path(populated))
    assert result.payload.project.name == "Snapshot Project"
    assert [(item.local_id, item.title) for item in result.payload.prds] == [
        ("default", ""),
        ("named", "Named"),
    ]
    assert result.payload.prds[0].provenance_state == "legacy_unbound"
    assert result.payload.prds[0].source_sha256 is None
    assert result.payload.prds[1].provenance_state == "available"
    assert result.payload.prds[1].source_size_bytes == len(
        "# Project: Named\r\nNFD: e\u0301\r\n".encode()
    )
    assert result.payload.tasks[1].dependency_refs[0].prd_id == "default"
    assert result.payload.tasks[1].dependency_refs[0].task_id == "T001"
    assert [summary.count for summary in result.payload.tasks[0].verification_summaries] == [
        1,
        1,
        1,
        1,
    ]
    wire = snapshot_response_canonical_bytes(result)
    for secret in (
        b"PROJECT_DESCRIPTION_SECRET",
        b"SOURCE_SUMMARY_SECRET",
        b"FEATURE_DESCRIPTION_SECRET",
        b"TASK_DESCRIPTION_SECRET",
        b"CONFLICT_SECRET",
        b"IMPLEMENTATION_SECRET",
        b"COMMAND_SECRET",
        b"MANUAL_SECRET",
        b"EVIDENCE_SECRET",
        b"PROOF_COMMAND_SECRET",
        b"PROOF_LABEL_SECRET",
        b"PATH_SECRET",
        b"source_bytes",
    ):
        assert secret not in wire


def test_repeat_read_is_deterministic_and_does_not_mutate_state(
    populated: SqliteBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _state_path(populated)
    database = root / "state.db"
    events = root / "events.jsonl"
    before = (database.read_bytes(), events.read_bytes(), sorted(p.name for p in root.iterdir()))
    monkeypatch.setattr(
        SqliteBackend,
        "initialize",
        lambda _self: (_ for _ in ()).throw(AssertionError("initialize called")),
    )
    first = read_project_snapshot(root)
    second = read_project_snapshot(root)
    after = (database.read_bytes(), events.read_bytes(), sorted(p.name for p in root.iterdir()))
    assert first == second
    assert first.event_cursor.event_count == 8
    assert (
        first.event_cursor.event_frontier_sha256
        == "0417dbd4b57bf51d7b975425931f0f4f2391e1ef0082e8ca4f132f0857e5b3af"
    )
    assert before == after


@pytest.mark.parametrize(
    ("requested", "expected_name"),
    [
        ({"max_verification_summary_label_bytes": 3}, "max_verification_summary_label_bytes"),
        ({"max_string_bytes": 5}, "max_string_bytes"),
        ({"max_snapshot_bytes": 128}, "max_snapshot_bytes"),
        ({"max_response_bytes": 256}, "max_response_bytes"),
        ({"max_canonical_json_depth": 2}, "max_canonical_json_depth"),
    ],
)
def test_lowered_serialized_limits_report_exact_numeric_metadata(
    populated: SqliteBackend,
    requested: dict[str, int],
    expected_name: str,
) -> None:
    with pytest.raises(ProjectSnapshotError) as refusal:
        read_project_snapshot(
            _state_path(populated),
            limits=lowered_limits(requested),
        )
    assert isinstance(refusal.value.error, ProviderLimitRefusalV1)
    assert refusal.value.error.limit_name.value == expected_name
    assert refusal.value.error.actual > refusal.value.error.limit


def test_lowered_entity_limit_refuses_without_partial_payload(
    populated: SqliteBackend,
) -> None:
    with pytest.raises(ProjectSnapshotError) as refusal:
        read_project_snapshot(
            _state_path(populated),
            limits=lowered_limits({"max_tasks": 1}),
        )
    assert refusal.value.error == ProviderLimitRefusalV1(
        operation_id="state.project.snapshot",
        limit_name=ProviderLimitNameV1.max_tasks,
        actual=2,
        limit=1,
    )


def test_missing_and_incompatible_state_return_closed_errors(
    tmp_path: Path,
    populated: SqliteBackend,
) -> None:
    with pytest.raises(ProjectSnapshotError) as missing:
        read_project_snapshot(tmp_path / "missing")
    assert isinstance(missing.value.error, ReadErrorV1)
    assert missing.value.error.code is ReadErrorCode.state_unavailable

    root = _state_path(populated)
    populated.close()
    conn = sqlite3.connect(root / "state.db")
    conn.execute("PRAGMA user_version = 999")
    conn.close()
    with pytest.raises(ProjectSnapshotError) as incompatible:
        read_project_snapshot(root)
    assert isinstance(incompatible.value.error, ReadErrorV1)
    assert incompatible.value.error.code is ReadErrorCode.schema_incompatible


def test_schema_probe_failure_is_projection_refusal_not_schema_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_probe(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise SchemaProbeFailed("bounded test refusal")

    monkeypatch.setattr(snapshot_module, "query_only_transaction", fail_probe)
    with pytest.raises(ProjectSnapshotError) as refusal:
        read_project_snapshot(tmp_path)
    assert isinstance(refusal.value.error, ReadErrorV1)
    assert refusal.value.error.code is ReadErrorCode.projection_not_converged
    assert refusal.value.error.field == "projection"


def test_log_ahead_refuses_without_healing(populated: SqliteBackend) -> None:
    root = _state_path(populated)
    events = root / "events.jsonl"
    before_db = (root / "state.db").read_bytes()
    events.write_bytes(
        events.read_bytes()
        + b'{"id":"E999999","timestamp":"2026-08-08T12:00:00Z",'
        b'"actor":"x","action":"x","target_kind":"x","target_id":"x",'
        b'"payload_json":{}}\n'
    )
    poisoned_log = events.read_bytes()
    with pytest.raises(ProjectSnapshotError) as refusal:
        read_project_snapshot(root)
    assert isinstance(refusal.value.error, ReadErrorV1)
    assert refusal.value.error.code is ReadErrorCode.projection_not_converged
    assert (root / "state.db").read_bytes() == before_db
    assert events.read_bytes() == poisoned_log


def test_spoofed_feature_ownership_and_dependency_cycle_refuse_atomically(
    populated: SqliteBackend,
) -> None:
    root = _state_path(populated)
    populated.close()
    conn = sqlite3.connect(root / "state.db")
    conn.execute("UPDATE tasks SET feature_id = 'F001' WHERE id = 'named:T001'")
    conn.commit()
    conn.close()
    with pytest.raises(ProjectSnapshotError) as ownership:
        read_project_snapshot(root)
    assert isinstance(ownership.value.error, ReadErrorV1)
    assert ownership.value.error.code is ReadErrorCode.invalid_hierarchy

    conn = sqlite3.connect(root / "state.db")
    conn.execute(
        "UPDATE tasks SET feature_id = 'named:F001', dependencies = '[\"T001\"]' "
        "WHERE id = 'named:T001'"
    )
    conn.execute(
        "UPDATE tasks SET dependencies = '[\"named:T001\"]' WHERE id = 'T001'"
    )
    conn.commit()
    conn.close()
    with pytest.raises(ProjectSnapshotError) as cycle:
        read_project_snapshot(root)
    assert isinstance(cycle.value.error, ReadErrorV1)
    assert cycle.value.error.code is ReadErrorCode.dependency_cycle


def test_visible_change_moves_snapshot_digest_while_excluded_event_moves_cursor(
    populated: SqliteBackend,
) -> None:
    root = _state_path(populated)
    before = read_project_snapshot(root)
    populated.append(
        _event(
            "progress.noted",
            {
                "task_id": "T001",
                "phase": "build",
                "notes": "CURSOR_ONLY_SECRET",
                "noted_at": _NOW.isoformat(),
                "actor": "snapshot-test",
            },
            kind="task",
            target="T001",
        )
    )
    operational = read_project_snapshot(root)
    assert operational.snapshot_digest == before.snapshot_digest
    assert operational.event_cursor != before.event_cursor

    populated.close()
    conn = sqlite3.connect(root / "state.db")
    conn.execute("UPDATE tasks SET title = 'Changed visible title' WHERE id = 'T001'")
    conn.commit()
    conn.close()
    visible = read_project_snapshot(root)
    assert visible.snapshot_digest != operational.snapshot_digest


def test_reader_waits_for_log_first_writer_and_returns_complete_post_state(
    populated: SqliteBackend,
    frozen_clock: FrozenClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _state_path(populated)
    populated.close()
    log_appended = threading.Event()
    allow_commit = threading.Event()
    writer_done = threading.Event()
    reader_done = threading.Event()
    result: list[object] = []
    original_insert = SqliteBackend._insert_event_row

    def writer() -> None:
        backend = SqliteBackend(
            db_path=str(root / "state.db"),
            events_path=str(root / "events.jsonl"),
            clock=frozen_clock,
        )
        backend.initialize()

        def paused_insert(*args, **kwargs):  # type: ignore[no-untyped-def]
            log_appended.set()
            assert allow_commit.wait(5)
            return original_insert(backend, *args, **kwargs)

        monkeypatch.setattr(backend, "_insert_event_row", paused_insert)
        try:
            backend.append(
                _event(
                    "progress.noted",
                    {
                        "task_id": "T001",
                        "notes": "writer barrier",
                        "noted_at": _NOW.isoformat(),
                        "actor": "snapshot-test",
                    },
                    kind="task",
                    target="T001",
                )
            )
        finally:
            backend.close()
            writer_done.set()

    def reader() -> None:
        result.append(read_project_snapshot(root))
        reader_done.set()

    writer_thread = threading.Thread(target=writer)
    writer_thread.start()
    assert log_appended.wait(5)
    reader_thread = threading.Thread(target=reader)
    reader_thread.start()
    assert not reader_done.wait(0.1)
    allow_commit.set()
    assert writer_done.wait(5)
    assert reader_done.wait(5)
    writer_thread.join()
    reader_thread.join()
    snapshot = result[0]
    assert snapshot.event_cursor.event_count == 9  # type: ignore[attr-defined]
    assert len(snapshot.payload.tasks) == 2  # type: ignore[attr-defined]


def test_git_event_reorder_is_stable_and_envelope_tamper_refuses(
    tmp_path: Path,
    frozen_clock: FrozenClock,
) -> None:
    events_path = tmp_path / "events.jsonl"
    events_path.touch()
    backend = SqliteBackend(
        db_path=str(tmp_path / "state.db"),
        events_path=str(events_path),
        clock=frozen_clock,
        events_storage="git",
    )
    backend.initialize()
    backend.append(
        _event("project.created", _project_payload(), kind="project", target="project-1")
    )
    backend.append(
        _event("state.initialized", {}, kind="project", target="project-1")
    )
    backend.close()
    first = read_project_snapshot(tmp_path)
    lines = events_path.read_bytes().splitlines(keepends=True)
    events_path.write_bytes(b"".join(reversed(lines)))
    reordered = read_project_snapshot(tmp_path)
    assert reordered == first

    document = json.loads(lines[1])
    document["lamport"] += 1
    lines[1] = (json.dumps(document, separators=(",", ":")) + "\n").encode()
    events_path.write_bytes(b"".join(lines))
    with pytest.raises(ProjectSnapshotError) as refusal:
        read_project_snapshot(tmp_path)
    assert isinstance(refusal.value.error, ReadErrorV1)
    assert refusal.value.error.code is ReadErrorCode.projection_not_converged


def test_db_event_json_scalar_type_drift_refuses(populated: SqliteBackend) -> None:
    root = _state_path(populated)
    populated.close()
    conn = sqlite3.connect(root / "state.db")
    row = conn.execute(
        "SELECT payload_json FROM events WHERE action = 'prd.parsed' ORDER BY id LIMIT 1"
    ).fetchone()
    payload = json.loads(row[0])
    assert payload["is_default"] is True
    payload["is_default"] = 1
    conn.execute(
        "UPDATE events SET payload_json = ? WHERE action = 'prd.parsed' "
        "AND id = (SELECT id FROM events WHERE action = 'prd.parsed' ORDER BY id LIMIT 1)",
        (json.dumps(payload, separators=(",", ":")),),
    )
    conn.commit()
    conn.close()
    with pytest.raises(ProjectSnapshotError) as refusal:
        read_project_snapshot(root)
    assert refusal.value.error.code is ReadErrorCode.projection_not_converged


def test_local_event_sequence_gap_refuses(populated: SqliteBackend) -> None:
    root = _state_path(populated)
    populated.close()
    events = root / "events.jsonl"
    lines = events.read_bytes().splitlines(keepends=True)
    removed_id = json.loads(lines[1])["id"]
    events.write_bytes(b"".join((lines[0], *lines[2:])))
    conn = sqlite3.connect(root / "state.db")
    conn.execute("DELETE FROM events WHERE id = ?", (removed_id,))
    conn.commit()
    conn.close()
    with pytest.raises(ProjectSnapshotError) as refusal:
        read_project_snapshot(root)
    assert refusal.value.error.code is ReadErrorCode.projection_not_converged


def test_git_parent_cycle_refuses_even_with_matching_fingerprints(
    tmp_path: Path,
    frozen_clock: FrozenClock,
) -> None:
    events_path = tmp_path / "events.jsonl"
    events_path.touch()
    backend = SqliteBackend(
        db_path=str(tmp_path / "state.db"),
        events_path=str(events_path),
        clock=frozen_clock,
        events_storage="git",
    )
    backend.initialize()
    backend.append(
        _event("project.created", _project_payload(), kind="project", target="project-1")
    )
    backend.append(_event("state.initialized", {}, kind="project", target="project-1"))
    backend.close()
    documents = [json.loads(line) for line in events_path.read_bytes().splitlines()]
    documents[0]["parent_event_id"] = documents[1]["id"]
    documents[1]["parent_event_id"] = documents[0]["id"]
    events_path.write_bytes(
        b"".join(
            (json.dumps(document, separators=(",", ":")) + "\n").encode()
            for document in documents
        )
    )
    conn = sqlite3.connect(tmp_path / "state.db")
    for document in documents:
        event = Event.model_validate(document)
        material = json.dumps(
            event.model_dump(mode="json", exclude={"id"}),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        conn.execute(
            "UPDATE git_event_material SET fingerprint = ? WHERE event_id = ?",
            (hashlib.sha256(material).hexdigest(), event.id),
        )
    conn.commit()
    conn.close()
    with pytest.raises(ProjectSnapshotError) as refusal:
        read_project_snapshot(tmp_path)
    assert refusal.value.error.code is ReadErrorCode.projection_not_converged


@pytest.mark.parametrize("alias", [True, 1.0, "1"])
def test_git_lamport_requires_exact_json_integer(
    tmp_path: Path,
    frozen_clock: FrozenClock,
    alias: object,
) -> None:
    events_path = tmp_path / "events.jsonl"
    events_path.touch()
    backend = SqliteBackend(
        db_path=str(tmp_path / "state.db"),
        events_path=str(events_path),
        clock=frozen_clock,
        events_storage="git",
    )
    backend.initialize()
    backend.append(
        _event("project.created", _project_payload(), kind="project", target="project-1")
    )
    backend.append(_event("state.initialized", {}, kind="project", target="project-1"))
    backend.close()
    documents = [json.loads(line) for line in events_path.read_bytes().splitlines()]
    documents[0]["lamport"] = alias
    events_path.write_bytes(
        b"".join(
            (json.dumps(document, separators=(",", ":")) + "\n").encode()
            for document in documents
        )
    )
    with pytest.raises(ProjectSnapshotError) as refusal:
        read_project_snapshot(tmp_path)
    assert refusal.value.error.code is ReadErrorCode.projection_not_converged


def test_git_duplicate_envelopes_remain_one_committed_event(
    tmp_path: Path,
    frozen_clock: FrozenClock,
) -> None:
    events_path = tmp_path / "events.jsonl"
    events_path.touch()
    backend = SqliteBackend(
        db_path=str(tmp_path / "state.db"),
        events_path=str(events_path),
        clock=frozen_clock,
        events_storage="git",
    )
    backend.initialize()
    backend.append(
        _event("project.created", _project_payload(), kind="project", target="project-1")
    )
    backend.append(_event("state.initialized", {}, kind="project", target="project-1"))
    backend.close()
    lines = events_path.read_bytes().splitlines(keepends=True)
    baseline = read_project_snapshot(tmp_path)
    events_path.write_bytes(b"".join(line * 17 for line in lines))
    assert read_project_snapshot(tmp_path) == baseline


@pytest.mark.parametrize(
    ("statement", "parameters", "expected_code"),
    [
        ("DELETE FROM prds WHERE id = ?", ("named",), ReadErrorCode.missing_target),
        ("DELETE FROM features WHERE id = ?", ("named:F001",), ReadErrorCode.missing_target),
        (
            "UPDATE tasks SET parent_task_id = ? WHERE id = ?",
            ("named:T999", "named:T001"),
            ReadErrorCode.missing_target,
        ),
        (
            "UPDATE tasks SET dependencies = ? WHERE id = ?",
            ('["named:T999"]', "named:T001"),
            ReadErrorCode.missing_target,
        ),
    ],
)
def test_missing_hierarchy_relations_refuse(
    populated: SqliteBackend,
    statement: str,
    parameters: tuple[str, ...],
    expected_code: ReadErrorCode,
) -> None:
    root = _state_path(populated)
    populated.close()
    conn = sqlite3.connect(root / "state.db")
    conn.execute(statement, parameters)
    conn.commit()
    conn.close()
    with pytest.raises(ProjectSnapshotError) as refusal:
        read_project_snapshot(root)
    assert refusal.value.error.code is expected_code


def test_raw_storage_limits_refuse_with_exact_metadata(
    populated: SqliteBackend,
) -> None:
    root = _state_path(populated)
    populated.close()
    conn = sqlite3.connect(root / "state.db")
    conn.execute("UPDATE tasks SET title = ? WHERE id = 'T001'", ("x" * 65_537,))
    conn.commit()
    conn.close()
    with pytest.raises(ProjectSnapshotError) as refusal:
        read_project_snapshot(root)
    assert refusal.value.error == ProviderLimitRefusalV1(
        operation_id="state.project.snapshot",
        limit_name=ProviderLimitNameV1.max_string_bytes,
        actual=65_537,
        limit=65_536,
    )


def test_verification_item_count_is_summary_count_not_summary_cardinality(
    populated: SqliteBackend,
) -> None:
    root = _state_path(populated)
    populated.close()
    verification = json.dumps({"commands": ["x"] * 257}, separators=(",", ":"))
    conn = sqlite3.connect(root / "state.db")
    conn.execute(
        "UPDATE tasks SET verification = ? WHERE id = 'T001'", (verification,)
    )
    conn.commit()
    conn.close()
    result = read_project_snapshot(root)
    summary = result.payload.tasks[0].verification_summaries[0]
    assert summary.kind.value == "command"
    assert summary.count == 257


@pytest.mark.parametrize(
    "verification",
    [
        '{"commands":[],"commands":["x"]}',
        '{"required_proofs":[{}]}',
    ],
)
def test_malformed_verification_refuses_strictly(
    populated: SqliteBackend,
    verification: str,
) -> None:
    root = _state_path(populated)
    populated.close()
    conn = sqlite3.connect(root / "state.db")
    conn.execute(
        "UPDATE tasks SET verification = ? WHERE id = 'T001'", (verification,)
    )
    conn.commit()
    conn.close()
    with pytest.raises(ProjectSnapshotError) as refusal:
        read_project_snapshot(root)
    assert refusal.value.error.code is ReadErrorCode.invalid_hierarchy


def test_excluded_verification_body_does_not_consume_snapshot_limit(
    populated: SqliteBackend,
) -> None:
    root = _state_path(populated)
    baseline = read_project_snapshot(root)
    baseline_size = len(snapshot_response_canonical_bytes(baseline))
    populated.close()
    verification = json.dumps({"commands": ["x" * 10_000]}, separators=(",", ":"))
    conn = sqlite3.connect(root / "state.db")
    conn.execute(
        "UPDATE tasks SET verification = ? WHERE id = 'T001'", (verification,)
    )
    conn.commit()
    conn.close()
    result = read_project_snapshot(
        root,
        limits=lowered_limits({"max_snapshot_bytes": baseline_size + 100}),
    )
    assert result.payload.tasks[0].verification_summaries[0].count == 1


def test_excluded_verification_bodies_stream_without_aggregate_false_refusal(
    populated: SqliteBackend,
) -> None:
    root = _state_path(populated)
    baseline = read_project_snapshot(root)
    baseline_size = len(snapshot_response_canonical_bytes(baseline))
    populated.close()
    verification = json.dumps({"commands": ["x" * 8_500_000]}, separators=(",", ":"))
    conn = sqlite3.connect(root / "state.db")
    conn.execute("UPDATE tasks SET verification = ?", (verification,))
    conn.commit()
    conn.close()
    result = read_project_snapshot(
        root,
        limits=lowered_limits({"max_snapshot_bytes": baseline_size + 100}),
    )
    assert [task.verification_summaries[0].count for task in result.payload.tasks] == [
        1,
        1,
    ]


def test_prd_content_cap_precedes_blob_materialization(populated: SqliteBackend) -> None:
    root = _state_path(populated)
    populated.close()
    source = b"x" * 2_097_153
    conn = sqlite3.connect(root / "state.db")
    conn.execute("PRAGMA ignore_check_constraints = ON")
    conn.execute(
        "UPDATE prds SET source_bytes = ?, source_sha256 = ?, source_size_bytes = ?, "
        "source_encoding = 'utf-8', source_revision = revision, "
        "provenance_state = 'available', content_available = 1 WHERE id = 'named'",
        (source, hashlib.sha256(source).hexdigest(), len(source)),
    )
    conn.commit()
    conn.close()
    with pytest.raises(ProjectSnapshotError) as refusal:
        read_project_snapshot(root)
    assert refusal.value.error == ProviderLimitRefusalV1(
        operation_id="state.project.snapshot",
        limit_name=ProviderLimitNameV1.max_prd_content_bytes,
        actual=len(source),
        limit=2_097_152,
    )


def test_raw_json_cell_cap_refuses_before_unbounded_json_parsing(
    populated: SqliteBackend,
) -> None:
    root = _state_path(populated)
    populated.close()
    acceptance = "[" + (" " * 25_165_825) + '"Visible criterion"]'
    conn = sqlite3.connect(root / "state.db")
    conn.execute(
        "UPDATE tasks SET acceptance_criteria = ? WHERE id = 'T001'", (acceptance,)
    )
    conn.commit()
    conn.close()
    with pytest.raises(ProjectSnapshotError) as refusal:
        read_project_snapshot(root)
    assert isinstance(refusal.value.error, ReadErrorV1)
    assert refusal.value.error.code is ReadErrorCode.invalid_hierarchy
    assert refusal.value.error.field == "tasks"
    assert refusal.value.error.actual is None
    assert refusal.value.error.limit is None


def test_hard_snapshot_overflow_reports_limit_metadata(
    populated: SqliteBackend,
) -> None:
    root = _state_path(populated)
    populated.close()
    acceptance = json.dumps(["x" * 65_536] * 256, separators=(",", ":"))
    assert 16_777_216 < len(acceptance.encode()) < 25_165_824
    conn = sqlite3.connect(root / "state.db")
    conn.execute(
        "UPDATE tasks SET acceptance_criteria = ? WHERE id = 'T001'", (acceptance,)
    )
    conn.commit()
    conn.close()
    with pytest.raises(ProjectSnapshotError) as refusal:
        read_project_snapshot(root)
    assert isinstance(refusal.value.error, ProviderLimitRefusalV1)
    assert refusal.value.error.limit_name is ProviderLimitNameV1.max_snapshot_bytes
    assert refusal.value.error.actual > 16_777_216
    assert refusal.value.error.limit == 16_777_216


def test_aggregate_visible_snapshot_overflow_reports_limit_metadata(
    populated: SqliteBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _state_path(populated)
    populated.close()
    acceptance = json.dumps(["x" * 33_000] * 256, separators=(",", ":"))
    assert len(acceptance.encode()) < 16_777_216
    conn = sqlite3.connect(root / "state.db")
    conn.execute("UPDATE tasks SET acceptance_criteria = ?", (acceptance,))
    conn.commit()
    conn.close()
    monkeypatch.setattr(
        snapshot_module,
        "_json_string_list_for_task",
        lambda *_args: (_ for _ in ()).throw(AssertionError("materialized task JSON")),
    )
    with pytest.raises(ProjectSnapshotError) as refusal:
        read_project_snapshot(root)
    assert isinstance(refusal.value.error, ProviderLimitRefusalV1)
    assert refusal.value.error.limit_name is ProviderLimitNameV1.max_snapshot_bytes
    assert refusal.value.error.actual > 16_777_216
    assert refusal.value.error.limit == 16_777_216


def test_json_whitespace_does_not_consume_semantic_snapshot_limit(
    populated: SqliteBackend,
) -> None:
    root = _state_path(populated)
    baseline = read_project_snapshot(root)
    populated.close()
    padded = "[" + (" " * 8_500_000) + '"Visible criterion"]'
    conn = sqlite3.connect(root / "state.db")
    conn.execute("UPDATE tasks SET acceptance_criteria = ?", (padded,))
    conn.commit()
    conn.close()
    result = read_project_snapshot(root)
    assert result.snapshot_digest == baseline.snapshot_digest


def test_mixed_visible_aggregate_refuses_before_json_materialization(
    populated: SqliteBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _state_path(populated)
    populated.close()
    conn = sqlite3.connect(root / "state.db")
    columns = [row[1] for row in conn.execute("PRAGMA table_info(tasks)")]
    template = list(conn.execute("SELECT * FROM tasks WHERE id = 'T001'").fetchone())
    indexes = {name: columns.index(name) for name in columns}
    placeholders = ",".join("?" for _ in columns)
    statement = f"INSERT INTO tasks ({','.join(columns)}) VALUES ({placeholders})"
    acceptance = json.dumps(["a" * 60_000], separators=(",", ":"))
    for number in range(100, 260):
        values = list(template)
        values[indexes["id"]] = f"T{number}"
        values[indexes["title"]] = "t" * 60_000
        values[indexes["dependencies"]] = "[]"
        values[indexes["acceptance_criteria"]] = acceptance
        values[indexes["parent_task_id"]] = None
        conn.execute(statement, values)
    conn.commit()
    conn.close()
    monkeypatch.setattr(
        snapshot_module,
        "_json_string_list_for_task",
        lambda *_args: (_ for _ in ()).throw(AssertionError("materialized task JSON")),
    )
    with pytest.raises(ProjectSnapshotError) as refusal:
        read_project_snapshot(root)
    assert isinstance(refusal.value.error, ProviderLimitRefusalV1)
    assert refusal.value.error.limit_name is ProviderLimitNameV1.max_snapshot_bytes
    assert refusal.value.error.actual > refusal.value.error.limit


def test_long_named_prd_prefix_does_not_false_refuse_lower_bound(
    populated: SqliteBackend,
) -> None:
    root = _state_path(populated)
    populated.close()
    prd_id = "p" * 128
    feature_id = f"{prd_id}:F001"
    conn = sqlite3.connect(root / "state.db")
    conn.execute("UPDATE prds SET id = ? WHERE id = 'named'", (prd_id,))
    conn.execute(
        "UPDATE features SET id = ?, prd_id = ? WHERE id = 'named:F001'",
        (feature_id, prd_id),
    )
    conn.execute(
        "UPDATE tasks SET id = ?, feature_id = ?, prd_id = ? WHERE id = 'named:T001'",
        (f"{prd_id}:T001", feature_id, prd_id),
    )
    conn.execute(
        "WITH RECURSIVE seq(n) AS ("
        "SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < 20000) "
        "INSERT INTO tasks (id, feature_id, prd_id, title, description, status, "
        "priority, task_type, dependencies, conflict_groups, scores, "
        "acceptance_criteria, implementation_notes, verification, likely_files, "
        "claims, parent_task_id, created_at, updated_at) "
        "SELECT ? || ':T100.' || n, ?, ?, 't', '', 'ready', 'high', 'feature', "
        "'[]', '[]', '{}', '[]', '[]', '{}', '[]', '[]', NULL, ?, ? FROM seq",
        (prd_id, feature_id, prd_id, _NOW.isoformat(), _NOW.isoformat()),
    )
    conn.commit()
    conn.close()
    result = read_project_snapshot(root)
    assert len(result.payload.tasks) == 20_002
