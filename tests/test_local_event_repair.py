"""Focused coverage for one audited local event recovery."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from anvil.cli import app, local_event_repair
from anvil.cli.backup import _replay_into
from anvil.clock import FrozenClock
from anvil.state.hashing import canonical_json_bytes
from anvil.state.models import EventDraft
from anvil.state.sqlite import SqliteBackend

runner = CliRunner()
_NOW = datetime(2026, 9, 20, tzinfo=UTC)


def _state(tmp_path: Path) -> tuple[Path, Path]:
    state = tmp_path / ".anvil"
    state.mkdir()
    events = state / "events.jsonl"
    events.touch()
    backend = SqliteBackend(
        db_path=str(state / "state.db"),
        events_path=str(events),
        clock=FrozenClock(_NOW),
    )
    backend.initialize()
    try:
        backend.append(
            EventDraft(
                timestamp=_NOW,
                actor="test",
                action="project.created",
                target_kind="project",
                target_id="proj-1",
                payload_json={
                    "id": "proj-1",
                    "name": "repair",
                    "description": "",
                    "created_at": _NOW.isoformat(),
                    "updated_at": _NOW.isoformat(),
                },
            )
        )
        backend.append(
            EventDraft(
                timestamp=_NOW,
                actor="test",
                action="state.initialized",
                target_kind="project",
                target_id="proj-1",
                payload_json={},
            )
        )
        backend.append(
            EventDraft(
                timestamp=_NOW,
                actor="test",
                action="state.initialized",
                target_kind="project",
                target_id="proj-1",
                payload_json={},
            )
        )
    finally:
        backend.close()
    lines = events.read_bytes().splitlines(keepends=True)
    backup = tmp_path / "historical.jsonl"
    backup.write_bytes(b"".join(lines[:2]))
    current = b"".join((lines[0], lines[2]))
    events.write_bytes(current)
    rebuilt = tmp_path / "rebuilt.db"
    _replay_into(str(events), str(rebuilt), state)
    os.replace(rebuilt, state / "state.db")
    audit = {
        "ts": _NOW.isoformat(),
        "kind": "write_failed_after_log",
        "event_id": "E000002",
        "action": "state.initialized",
        "target_id": "proj-1",
        "reason": "synthetic write failure",
    }
    (state / "audit.jsonl").write_text(json.dumps(audit) + "\n", encoding="utf-8")
    receipt = {
        "schema_version": 1,
        "project_id": "proj-1",
        "event_id": "E000002",
        "log_sha256": hashlib.sha256(current).hexdigest(),
        "event_line_sha256": hashlib.sha256(lines[1]).hexdigest(),
        "backup_path": str(backup),
        "audit_record_sha256": hashlib.sha256(canonical_json_bytes(audit)).hexdigest(),
    }
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    return state, receipt_path


def _args(tmp_path: Path, receipt: Path, *extra: str) -> list[str]:
    return [
        "repair",
        "local-event",
        "--receipt",
        str(receipt),
        "--cwd",
        str(tmp_path),
        *extra,
    ]


def _pending(tmp_path: Path, receipt: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    def interrupt(phase: str) -> None:
        if phase == "after_marker":
            raise RuntimeError("interrupt")

    monkeypatch.setattr(local_event_repair, "_PHASE_HOOK", interrupt)
    result = runner.invoke(app, _args(tmp_path, receipt, "--apply", "--exclusive-access"))
    assert result.exit_code != 0
    marker = tmp_path / ".anvil" / ".local-event-recovery.json"
    assert marker.exists()
    monkeypatch.setattr(local_event_repair, "_PHASE_HOOK", None)
    return marker


def _resume_args(tmp_path: Path) -> list[str]:
    return [
        "repair",
        "local-event",
        "--resume",
        "--exclusive-access",
        "--cwd",
        str(tmp_path),
    ]


def _legacy_claim_state(tmp_path: Path) -> tuple[Path, Path]:
    state = tmp_path / ".anvil"
    state.mkdir()
    source = Path(__file__).parent / "fixtures" / "replay" / "sample-project" / "events.jsonl"
    lines = source.read_bytes().splitlines(keepends=True)
    missing = lines[1]
    current = b"".join((lines[0], *lines[2:]))
    events = state / "events.jsonl"
    events.write_bytes(current)
    backup = tmp_path / "historical.jsonl"
    backup.write_bytes(b"".join(lines[:2]))
    _replay_into(str(events), str(state / "state.db"), state)
    conn = sqlite3.connect(state / "state.db")
    try:
        conn.execute("ALTER TABLE claims DROP COLUMN root_set")
        conn.execute("ALTER TABLE claims ADD COLUMN root_set TEXT")
        for claim_id, fingerprint in conn.execute(
            "SELECT claim_id, creation_fingerprint FROM claim_replay_lineages"
        ):
            value = json.loads(fingerprint)
            value.pop("root_set")
            conn.execute(
                "UPDATE claim_replay_lineages SET creation_fingerprint = ? WHERE claim_id = ?",
                (json.dumps(value, sort_keys=True, separators=(",", ":")), claim_id),
            )
        conn.commit()
    finally:
        conn.close()
    audit = {
        "ts": "2026-05-24T18:00:00+00:00",
        "kind": "write_failed_after_log",
        "event_id": "E000002",
        "action": "state.initialized",
        "target_id": "proj-1",
        "reason": "synthetic write failure",
    }
    (state / "audit.jsonl").write_text(json.dumps(audit) + "\n", encoding="utf-8")
    receipt = {
        "schema_version": 1,
        "project_id": "proj-1",
        "event_id": "E000002",
        "log_sha256": hashlib.sha256(current).hexdigest(),
        "event_line_sha256": hashlib.sha256(missing).hexdigest(),
        "backup_path": str(backup),
        "audit_record_sha256": hashlib.sha256(canonical_json_bytes(audit)).hexdigest(),
    }
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    return state, receipt_path


def test_preview_then_apply_recovers_only_the_audited_event(tmp_path: Path) -> None:
    state, receipt = _state(tmp_path)
    before = (state / "events.jsonl").read_bytes()
    preview = runner.invoke(app, _args(tmp_path, receipt, "--json"))
    assert preview.exit_code == 0, preview.output
    assert json.loads(preview.output)["data"]["status"] == "ready"
    assert (state / "events.jsonl").read_bytes() == before
    applied = runner.invoke(app, _args(tmp_path, receipt, "--apply", "--exclusive-access"))
    assert applied.exit_code == 0, applied.output
    assert b'"id":"E000002"' in (state / "events.jsonl").read_bytes()
    assert not (state / ".local-event-recovery.json").exists()


def test_resume_uses_only_staged_artifacts_after_interruption(tmp_path: Path) -> None:
    state, receipt = _state(tmp_path)

    def interrupt(phase: str) -> None:
        if phase == "after_marker":
            raise RuntimeError("interrupt")

    local_event_repair._PHASE_HOOK = interrupt
    try:
        interrupted = runner.invoke(app, _args(tmp_path, receipt, "--apply", "--exclusive-access"))
    finally:
        local_event_repair._PHASE_HOOK = None
    assert interrupted.exit_code != 0
    assert (state / ".local-event-recovery.json").exists()
    pending = runner.invoke(app, _args(tmp_path, receipt))
    assert pending.exit_code != 0
    assert "use --resume" in pending.output
    resumed = runner.invoke(
        app,
        [
            "repair",
            "local-event",
            "--resume",
            "--exclusive-access",
            "--cwd",
            str(tmp_path),
        ],
    )
    assert resumed.exit_code == 0, resumed.output
    assert not (state / ".local-event-recovery.json").exists()


def test_changed_frontier_after_preview_refuses_without_rewrite(tmp_path: Path) -> None:
    state, receipt = _state(tmp_path)
    assert runner.invoke(app, _args(tmp_path, receipt)).exit_code == 0
    writer = SqliteBackend(
        db_path=str(state / "state.db"),
        events_path=str(state / "events.jsonl"),
        clock=FrozenClock(_NOW),
    )
    writer.initialize()
    try:
        writer.append(
            EventDraft(
                timestamp=_NOW,
                actor="writer",
                action="state.initialized",
                target_kind="project",
                target_id="proj-1",
                payload_json={},
            )
        )
    finally:
        writer.close()
    changed = (state / "events.jsonl").read_bytes()
    result = runner.invoke(app, _args(tmp_path, receipt, "--apply", "--exclusive-access"))
    assert result.exit_code != 0
    assert (state / "events.jsonl").read_bytes() == changed


def test_legacy_claim_root_set_representation_is_preserved(tmp_path: Path) -> None:
    state, receipt = _legacy_claim_state(tmp_path)
    conn = sqlite3.connect(state / "state.db")
    try:
        before_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'claims'"
        ).fetchone()[0]
        before_fingerprints = conn.execute(
            "SELECT creation_fingerprint FROM claim_replay_lineages ORDER BY claim_id"
        ).fetchall()
    finally:
        conn.close()
    preview = runner.invoke(app, _args(tmp_path, receipt))
    assert preview.exit_code == 0, preview.output
    applied = runner.invoke(app, _args(tmp_path, receipt, "--apply", "--exclusive-access"))
    assert applied.exit_code == 0, applied.output
    conn = sqlite3.connect(state / "state.db")
    try:
        assert (
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'claims'"
            ).fetchone()[0]
            == before_sql
        )
        assert (
            conn.execute(
                "SELECT creation_fingerprint FROM claim_replay_lineages ORDER BY claim_id"
            ).fetchall()
            == before_fingerprints
        )
    finally:
        conn.close()


def test_non_null_root_set_lineage_refuses(tmp_path: Path) -> None:
    state, receipt = _legacy_claim_state(tmp_path)
    root_fact = {
        "root_id": "root1",
        "repository_id": "repo1",
        "baseline_sha": "a" * 40,
        "canonical_root": "/root",
        "claim_worktree": "/worktree",
        "branch": "main",
        "verification_commands": ["true"],
    }
    root_digest = hashlib.sha256(
        json.dumps(
            {"primary_root_id": "root1", "roots": [root_fact]},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    conn = sqlite3.connect(state / "state.db")
    try:
        claim_id, fingerprint = conn.execute(
            "SELECT claim_id, creation_fingerprint FROM claim_replay_lineages LIMIT 1"
        ).fetchone()
        value = json.loads(fingerprint)
        value["root_set"] = {
            "schema_version": 1,
            "request_id": "request1",
            "primary_root_id": "root1",
            "request_digest": "b" * 64,
            "root_set_digest": root_digest,
            "reservation_id": "R1",
            "root_facts": [root_fact],
        }
        conn.execute(
            "UPDATE claim_replay_lineages SET creation_fingerprint = ? WHERE claim_id = ?",
            (json.dumps(value, sort_keys=True, separators=(",", ":")), claim_id),
        )
        conn.commit()
    finally:
        conn.close()
    assert runner.invoke(app, _args(tmp_path, receipt)).exit_code != 0


def test_bundle_child_lineage_sentinel_stays_byte_exact() -> None:
    row = canonical_json_bytes(
        {
            "claim_id": "C001",
            "creation_fingerprint": "bundle-child:C001:T001",
            "collision_detected": 0,
        }
    ).decode("utf-8")
    assert local_event_repair._normalized_lineages([row]) == [row]  # noqa: SLF001


def test_receipt_schema_version_must_be_an_integer(tmp_path: Path) -> None:
    state, receipt = _state(tmp_path)
    value = json.loads(receipt.read_text(encoding="utf-8"))
    value["schema_version"] = True
    receipt.write_text(json.dumps(value), encoding="utf-8")
    assert runner.invoke(app, _args(tmp_path, receipt)).exit_code != 0
    assert (state / "events.jsonl").exists()


def test_live_row_divergence_refuses_without_rewrite(tmp_path: Path) -> None:
    state, receipt = _state(tmp_path)
    before = (state / "events.jsonl").read_bytes()
    conn = sqlite3.connect(state / "state.db")
    try:
        conn.execute("UPDATE projects SET name = 'diverged'")
        conn.commit()
    finally:
        conn.close()
    result = runner.invoke(app, _args(tmp_path, receipt))
    assert result.exit_code != 0
    assert (state / "events.jsonl").read_bytes() == before


def test_fabricated_receipt_refuses_without_mutating_live_state(tmp_path: Path) -> None:
    state, receipt = _state(tmp_path)
    before = (state / "events.jsonl").read_bytes()
    value = json.loads(receipt.read_text(encoding="utf-8"))
    value["event_line_sha256"] = "0" * 64
    receipt.write_text(json.dumps(value), encoding="utf-8")
    result = runner.invoke(app, _args(tmp_path, receipt))
    assert result.exit_code != 0
    assert (state / "events.jsonl").read_bytes() == before


def test_unsafe_backup_refuses_without_mutating_live_state(tmp_path: Path) -> None:
    state, receipt = _state(tmp_path)
    before = (state / "events.jsonl").read_bytes()
    value = json.loads(receipt.read_text(encoding="utf-8"))
    unsafe = tmp_path / "unsafe.jsonl"
    unsafe.symlink_to(value["backup_path"])
    value["backup_path"] = str(unsafe)
    receipt.write_text(json.dumps(value), encoding="utf-8")
    result = runner.invoke(app, _args(tmp_path, receipt))
    assert result.exit_code != 0
    assert (state / "events.jsonl").read_bytes() == before


@pytest.mark.parametrize("phase", ["after_log", "after_db"])
def test_resume_after_partial_publication(tmp_path: Path, phase: str) -> None:
    state, receipt = _state(tmp_path)

    def interrupt(value: str) -> None:
        if value == phase:
            raise RuntimeError("interrupt")

    local_event_repair._PHASE_HOOK = interrupt
    try:
        result = runner.invoke(app, _args(tmp_path, receipt, "--apply", "--exclusive-access"))
    finally:
        local_event_repair._PHASE_HOOK = None
    assert result.exit_code != 0
    assert (state / ".local-event-recovery.json").exists()
    resumed = runner.invoke(
        app,
        _resume_args(tmp_path),
    )
    assert resumed.exit_code == 0, resumed.output


@pytest.mark.parametrize("tamper", ["symlink", "hardlink", "content"])
def test_tampered_staged_artifact_refuses_and_retains_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    state, receipt = _state(tmp_path)
    marker_path = _pending(tmp_path, receipt, monkeypatch)
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    recovery = state / marker["recovery_dir"]
    staged = recovery / "after-events.jsonl"
    if tamper == "symlink":
        staged.unlink()
        staged.symlink_to(recovery / "before-events.jsonl")
    elif tamper == "hardlink":
        staged.unlink()
        os.link(recovery / "before-events.jsonl", staged)
    else:
        staged.write_bytes(b"changed\n")
    result = runner.invoke(app, _resume_args(tmp_path))
    assert result.exit_code != 0
    assert marker_path.exists()


def test_partial_target_prefix_resumes_and_garbage_prefix_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, receipt = _state(tmp_path)
    marker_path = _pending(tmp_path, receipt, monkeypatch)
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    target = (state / marker["recovery_dir"] / "after-events.jsonl").read_bytes()
    events = state / "events.jsonl"
    with events.open("r+b") as fh:
        fh.truncate(0)
        fh.write(target[:17])
    assert runner.invoke(app, _resume_args(tmp_path)).exit_code == 0
    assert events.read_bytes() == target

    garbage_root = tmp_path / "garbage"
    garbage_root.mkdir()
    state, receipt = _state(garbage_root)
    marker_path = _pending(garbage_root, receipt, monkeypatch)
    events = state / "events.jsonl"
    with events.open("r+b") as fh:
        fh.truncate(0)
        fh.write(b"not-the-target")
    result = runner.invoke(app, _resume_args(garbage_root))
    assert result.exit_code != 0
    assert marker_path.exists()


def test_preopened_backend_survives_resumed_in_place_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, receipt = _state(tmp_path)
    old_inode = os.stat(state / "state.db").st_ino
    peer = SqliteBackend(
        db_path=str(state / "state.db"),
        events_path=str(state / "events.jsonl"),
        clock=FrozenClock(_NOW),
    )
    peer.initialize()
    try:
        _pending(tmp_path, receipt, monkeypatch)
        assert runner.invoke(app, _resume_args(tmp_path)).exit_code == 0
        assert os.stat(state / "state.db").st_ino == old_inode
        assert peer.get_project() is not None
        peer.append(
            EventDraft(
                timestamp=_NOW,
                actor="peer",
                action="state.initialized",
                target_kind="project",
                target_id="proj-1",
                payload_json={},
            )
        )
        assert b'"id":"E000004"' in (state / "events.jsonl").read_bytes()
    finally:
        peer.close()
