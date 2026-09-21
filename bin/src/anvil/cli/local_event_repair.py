"""Fail-closed repair for one locally omitted, audited event line."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import typer

from anvil.cli._helpers import _require_state_dir, _resolve_state_dir
from anvil.cli.backup import _replay_into
from anvil.cli.repair import (
    _backup_sqlite,
    _checkpoint_and_fsync_live_projection,
    _fsync_directory,
    _fsync_file,
    _publish_staged_projection,
)
from anvil.project_snapshot import read_project_snapshot, validate_converged_event_log
from anvil.state.hashing import canonical_json_bytes

_COMMAND = "repair local-event"
_MARKER = ".local-event-recovery.json"
_RECOVERY_PREFIX = ".local-event-recovery-"
_MAX_INPUT_BYTES = 64 * 1024 * 1024
_MAX_LINE_BYTES = 2 * 1024 * 1024
_MAX_RECEIPT_BYTES = 64 * 1024
_PHASE_HOOK: Callable[[str], None] | None = None


class _Refusal(RuntimeError):
    """Closed, payload-free operator refusal."""


@dataclass(frozen=True, slots=True)
class _Plan:
    project_id: str
    event_id: str
    current: bytes
    target: bytes
    receipt_sha256: str
    event: Any


def _fail(message: str) -> None:
    raise _Refusal(message)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _regular(path: Path, *, required: bool = True) -> os.stat_result | None:
    try:
        entry = path.lstat()
    except FileNotFoundError:
        if required:
            _fail("required recovery artifact is unavailable")
        return None
    attrs = getattr(entry, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        stat.S_ISLNK(entry.st_mode)
        or attrs & reparse
        or not stat.S_ISREG(entry.st_mode)
        or entry.st_nlink != 1
    ):
        _fail("unsafe recovery artifact")
    return entry


def _directory(path: Path) -> os.stat_result:
    try:
        entry = path.lstat()
    except FileNotFoundError:
        _fail("required recovery directory is unavailable")
    attrs = getattr(entry, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if stat.S_ISLNK(entry.st_mode) or attrs & reparse or not stat.S_ISDIR(entry.st_mode):
        _fail("unsafe recovery directory")
    return entry


def _bounded_bytes(path: Path, *, limit: int = _MAX_INPUT_BYTES) -> bytes:
    _regular(path)
    chunks: list[bytes] = []
    total = 0
    try:
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(min(1024 * 1024, limit + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    _fail("recovery input exceeds its safety limit")
                chunks.append(chunk)
    except OSError:
        _fail("recovery input is unavailable")
    return b"".join(chunks)


def _event_lines(raw: bytes) -> list[bytes]:
    from pydantic import ValidationError

    from anvil.state.models import Event

    if not raw or not raw.endswith(b"\n"):
        _fail("event history is incomplete")
    lines = raw.splitlines(keepends=True)
    if not lines or any(len(line) > _MAX_LINE_BYTES or not line.endswith(b"\n") for line in lines):
        _fail("event history exceeds its safety limits")
    ids: list[int] = []
    try:
        for line in lines:
            event = Event.model_validate_json(line)
            if event.parent_event_id is not None or event.lamport is not None:
                _fail("event history is not local-only")
            if not event.id.startswith("E") or not event.id[1:].isdigit():
                _fail("event history is not local-only")
            ids.append(int(event.id[1:]))
    except (UnicodeDecodeError, ValidationError, ValueError):
        _fail("event history is invalid")
    if not ids or ids[0] != 1 or len(ids) != len(set(ids)):
        _fail("event history does not have one recoverable gap")
    return lines


def _event_for_line(line: bytes) -> Any:
    from pydantic import ValidationError

    from anvil.state.models import Event

    try:
        event = Event.model_validate_json(line)
    except (UnicodeDecodeError, ValidationError, ValueError):
        _fail("recovery event is invalid")
    if event.parent_event_id is not None or event.lamport is not None:
        _fail("recovery event is not local-only")
    return event


def _receipt(path: Path) -> tuple[dict[str, Any], str]:
    raw = _bounded_bytes(path, limit=_MAX_RECEIPT_BYTES)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        _fail("recovery receipt is invalid")
    fields = {
        "schema_version",
        "project_id",
        "event_id",
        "log_sha256",
        "event_line_sha256",
        "backup_path",
        "audit_record_sha256",
    }
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
    ):
        _fail("recovery receipt has an unsupported schema")
    for name in fields - {"schema_version"}:
        if not isinstance(value[name], str) or not value[name] or len(value[name].encode()) > 4096:
            _fail("recovery receipt is malformed")
    digest_names = ("log_sha256", "event_line_sha256", "audit_record_sha256")
    if any(
        len(value[name]) != 64 or any(ch not in "0123456789abcdef" for ch in value[name])
        for name in digest_names
    ):
        _fail("recovery receipt has an invalid digest")
    return value, _sha(canonical_json_bytes(value))


def _audit_matches(state_dir: Path, receipt: dict[str, Any], event: Any) -> None:
    audit = state_dir / "audit.jsonl"
    raw = _bounded_bytes(audit)
    matches = 0
    for line in raw.splitlines():
        if not line or len(line) > _MAX_LINE_BYTES:
            _fail("audit history exceeds its safety limits")
        try:
            record = json.loads(line)
            digest = _sha(canonical_json_bytes(record))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            _fail("audit history is invalid")
        if digest != receipt["audit_record_sha256"]:
            continue
        if (
            record.get("kind") != "write_failed_after_log"
            or record.get("event_id") != event.id
            or record.get("action") != event.action
            or record.get("target_id") != event.target_id
            or not isinstance(record.get("reason"), str)
            or not record["reason"].strip()
            or not isinstance(record.get("ts"), str)
        ):
            _fail("recovery receipt audit provenance does not match")
        try:
            audit_time = datetime.fromisoformat(record["ts"])
        except ValueError:
            _fail("recovery receipt audit provenance is invalid")
        if (
            audit_time.tzinfo is None
            or audit_time < event.timestamp
            or audit_time - event.timestamp > timedelta(minutes=5)
        ):
            _fail("recovery receipt audit provenance is invalid")
        matches += 1
    if matches != 1:
        _fail("recovery receipt audit provenance is unavailable")


def _plan(state_dir: Path, receipt_path: Path) -> _Plan:
    receipt, receipt_sha = _receipt(receipt_path)
    current = _bounded_bytes(state_dir / "events.jsonl")
    if _sha(current) != receipt["log_sha256"]:
        _fail("event history no longer matches the reviewed receipt")
    lines = _event_lines(current)
    values = [int(_event_for_line(line).id[1:]) for line in lines]
    differences = [values[index + 1] - values[index] for index in range(len(values) - 1)]
    gaps = [(index, values[index] + 1) for index, delta in enumerate(differences) if delta == 2]
    if len(gaps) != 1 or any(
        delta != 1 for index, delta in enumerate(differences) if index != gaps[0][0]
    ):
        _fail("event history does not have exactly one interior gap")
    index, missing = gaps[0]
    expected_id = f"E{missing:06d}"
    if receipt["event_id"] != expected_id:
        _fail("recovery receipt does not identify the gap")
    backup = _bounded_bytes(Path(receipt["backup_path"]))
    backup_lines = _event_lines(backup)
    if len(backup_lines) != index + 2 or backup_lines[: index + 1] != lines[: index + 1]:
        _fail("historical backup does not prove the missing event")
    missing_line = backup_lines[index + 1]
    event = _event_for_line(missing_line)
    if event.id != expected_id or _sha(missing_line) != receipt["event_line_sha256"]:
        _fail("historical backup does not match the reviewed receipt")
    if receipt["project_id"] != event.payload_json.get("project_id", receipt["project_id"]):
        _fail("recovery receipt project identity does not match the event")
    _audit_matches(state_dir, receipt, event)
    return _Plan(
        project_id=receipt["project_id"],
        event_id=event.id,
        current=current,
        target=b"".join((*lines[: index + 1], missing_line, *lines[index + 1 :])),
        receipt_sha256=receipt_sha,
        event=event,
    )


def _table_rows(conn: sqlite3.Connection) -> dict[str, list[str]]:
    tables = [
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    ]
    result: dict[str, list[str]] = {}
    for table in tables:
        quoted = table.replace('"', '""')
        cols = [row[1] for row in conn.execute(f'PRAGMA table_info("{quoted}")')]
        rows = []
        for row in conn.execute(f'SELECT * FROM "{quoted}"'):
            value = {
                name: ({"bytes": bytes(item).hex()} if isinstance(item, bytes) else item)
                for name, item in zip(cols, row, strict=True)
            }
            rows.append(canonical_json_bytes(value).decode("utf-8"))
        result[table] = sorted(rows)
    result["__schema__"] = [
        canonical_json_bytes(list(row)).decode("utf-8")
        for row in conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        )
    ]
    result["__user_version__"] = [str(conn.execute("PRAGMA user_version").fetchone()[0])]
    return result


def _normalized_lineages(rows: list[str]) -> list[str]:
    from anvil.state.payloads import ClaimCreatedPayload

    normalized: list[str] = []
    for row in rows:
        try:
            record = json.loads(row)
            fingerprint_text = record["creation_fingerprint"]
        except (KeyError, TypeError, ValueError):
            _fail("claim lineage fingerprint is invalid")
        if not isinstance(fingerprint_text, str):
            _fail("claim lineage fingerprint is invalid")
        try:
            fingerprint = json.loads(fingerprint_text)
        except json.JSONDecodeError:
            # v12 migration sentinels (for bundle-child claims without a
            # claim.created event) are not payloads. Leave them byte-exact;
            # they still have to equal the fresh replay's row below.
            normalized.append(row)
            continue
        try:
            ClaimCreatedPayload.model_validate(fingerprint)
        except ValueError:
            _fail("claim lineage fingerprint is invalid")
        if "root_set" not in fingerprint:
            fingerprint["root_set"] = None
            record["creation_fingerprint"] = json.dumps(
                fingerprint, sort_keys=True, separators=(",", ":")
            )
        normalized.append(canonical_json_bytes(record).decode("utf-8"))
    return sorted(normalized)


def _legacy_claims_sql(fresh_sql: str) -> str:
    removed = fresh_sql.replace("    root_set           TEXT,\n", "", 1)
    if removed == fresh_sql or removed.count("root_set") or not removed.endswith(")"):
        _fail("claims schema is not the supported fresh layout")
    return f"{removed[:-1]}, root_set TEXT)"


def _compatible_schema(baseline: list[str], observed: list[str]) -> bool:
    if baseline == observed:
        return True
    try:
        fresh = [json.loads(row) for row in baseline]
        live = [json.loads(row) for row in observed]
    except (TypeError, ValueError):
        return False
    if len(fresh) != len(live):
        return False
    differences = [(a, b) for a, b in zip(fresh, live, strict=True) if a != b]
    if len(differences) != 1:
        return False
    before, after = differences[0]
    return (
        before[:3] == after[:3] == ["table", "claims", "claims"]
        and isinstance(before[3], str)
        and after[3] == _legacy_claims_sql(before[3])
    )


def _compatible_rows(baseline: dict[str, list[str]], observed: dict[str, list[str]]) -> bool:
    if set(baseline) != set(observed):
        return False
    for name, rows in baseline.items():
        actual = observed[name]
        if name == "claim_replay_lineages":
            if rows != _normalized_lineages(actual):
                return False
        elif name == "__schema__":
            if not _compatible_schema(rows, actual):
                return False
        elif rows != actual:
            return False
    return True


def _readonly_connection(path: Path) -> sqlite3.Connection:
    _regular(path)
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        conn.execute("PRAGMA query_only=ON")
        return conn
    except sqlite3.Error:
        _fail("state projection is unavailable")


def _replay_and_prove(state_dir: Path, plan: _Plan, live: sqlite3.Connection | None = None) -> None:
    with tempfile.TemporaryDirectory(prefix="anvil-local-event-proof-") as temp:
        root = Path(temp)
        baseline_log = root / "baseline.jsonl"
        target_log = root / "events.jsonl"
        baseline_db = root / "baseline.db"
        recovered_db = root / "state.db"
        baseline_log.write_bytes(plan.current)
        target_log.write_bytes(plan.target)
        _replay_into(str(baseline_log), str(baseline_db), state_dir)
        _replay_into(str(target_log), str(recovered_db), state_dir)
        baseline = _readonly_connection(baseline_db)
        recovered = _readonly_connection(recovered_db)
        try:
            baseline_rows = _table_rows(baseline)
            observed = _table_rows(live) if live is not None else _live_rows(state_dir)
            if not _compatible_rows(baseline_rows, observed):
                _fail("live projection does not match the intact event history")
            recovered_rows = _table_rows(recovered)
            if {name: rows for name, rows in recovered_rows.items() if name != "events"} != {
                name: rows for name, rows in baseline_rows.items() if name != "events"
            }:
                _fail("recovered projection differs outside the audited event row")
            before_events = baseline_rows.get("events", [])
            after_events = recovered_rows.get("events", [])
            additions = set(after_events) - set(before_events)
            added_id = ""
            if len(additions) == 1:
                try:
                    added_id = json.loads(next(iter(additions))).get("id", "")
                except (TypeError, ValueError):
                    pass
            if (
                len(after_events) != len(before_events) + 1
                or not set(before_events).issubset(after_events)
                or len(additions) != 1
                or added_id != plan.event_id
            ):
                _fail("recovered projection does not contain exactly the audited event")
        finally:
            baseline.close()
            recovered.close()
        snapshot_root = root / "snapshot"
        snapshot_root.mkdir()
        shutil.copyfile(recovered_db, snapshot_root / "state.db")
        shutil.copyfile(target_log, snapshot_root / "events.jsonl")
        try:
            snapshot = read_project_snapshot(snapshot_root)
        except Exception:
            _fail("recovered projection did not pass strict snapshot validation")
        if snapshot.payload.project.project_id != plan.project_id:
            _fail("recovered projection project identity does not match the receipt")


def _live_rows(state_dir: Path) -> dict[str, list[str]]:
    from anvil.state.sqlite import query_only_transaction

    try:
        with query_only_transaction(state_dir / "state.db", state_dir / "events.jsonl") as (
            conn,
            _,
        ):
            return _table_rows(conn)
    except Exception:
        _fail("live projection cannot be inspected safely")


def _identity(path: Path) -> dict[str, int]:
    entry = _regular(path)
    assert entry is not None
    return {"dev": entry.st_dev, "ino": entry.st_ino}


def _safe_sidecars(state_db: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(f"{state_db}{suffix}")
        if os.path.lexists(sidecar):
            _regular(sidecar)


def _write_file(path: Path, content: bytes) -> None:
    with path.open("xb") as fh:
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())
    _regular(path)


def _write_marker(state_dir: Path, marker: dict[str, Any]) -> None:
    destination = state_dir / _MARKER
    if os.path.lexists(destination):
        _fail("another local event recovery is pending")
    temporary = state_dir / f".{_MARKER}.{uuid.uuid4().hex}.tmp"
    _write_file(temporary, canonical_json_bytes(marker))
    os.replace(temporary, destination)
    _regular(destination)
    _fsync_directory(state_dir)


def _stage(state_dir: Path, plan: _Plan, source: sqlite3.Connection) -> dict[str, Any]:
    recovery = state_dir / f"{_RECOVERY_PREFIX}{uuid.uuid4().hex}"
    recovery.mkdir(mode=0o700)
    _directory(recovery)
    before_log = recovery / "before-events.jsonl"
    after_log = recovery / "after-events.jsonl"
    before_db = recovery / "before-state.db"
    after_db = recovery / "after-state.db"
    _write_file(before_log, plan.current)
    _write_file(after_log, plan.target)
    _backup_sqlite(source, before_db)
    shutil.copyfile(before_db, after_db)
    staged = sqlite3.connect(str(after_db))
    try:
        from anvil.clock import SystemClock
        from anvil.state.sqlite import SqliteBackend

        writer = SqliteBackend(
            db_path=str(after_db),
            events_path=str(after_log),
            clock=SystemClock(),
        )
        writer._insert_event_row(staged, plan.event)  # noqa: SLF001
        staged.commit()
        staged.row_factory = sqlite3.Row
        with after_log.open("rb") as fh:
            validate_converged_event_log(staged, fh)
    except (OSError, sqlite3.Error, ValueError):
        _fail("staged projection failed strict verification")
    finally:
        staged.close()
    _fsync_file(after_db)
    _fsync_directory(recovery)
    before = _readonly_connection(before_db)
    after = _readonly_connection(after_db)
    live = _readonly_connection(state_dir / "state.db")
    try:
        if _table_rows(before) != _table_rows(live):
            _fail("live projection changed before staging")
        if not _same_except_event(_table_rows(before), _table_rows(after), plan.event_id):
            _fail("staged projection differs outside the audited event row")
    finally:
        before.close()
        after.close()
        live.close()
    names = (before_log.name, after_log.name, before_db.name, after_db.name)
    files = {name: _sha(_bounded_bytes(recovery / name)) for name in names}
    return {
        "schema_version": 1,
        "project_id": plan.project_id,
        "event_id": plan.event_id,
        "recovery_dir": recovery.name,
        "receipt_sha256": plan.receipt_sha256,
        "db_identity": _identity(state_dir / "state.db"),
        "log_identity": _identity(state_dir / "events.jsonl"),
        "files": files,
    }


def _marker(state_dir: Path) -> dict[str, Any]:
    raw = _bounded_bytes(state_dir / _MARKER, limit=_MAX_RECEIPT_BYTES)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        _fail("pending recovery marker is invalid")
    fields = {
        "schema_version",
        "project_id",
        "event_id",
        "recovery_dir",
        "receipt_sha256",
        "db_identity",
        "log_identity",
        "files",
    }
    if not isinstance(value, dict) or set(value) != fields or value.get("schema_version") != 1:
        _fail("pending recovery marker has an unsupported schema")
    recovery_dir = value["recovery_dir"]
    if (
        not isinstance(recovery_dir, str)
        or not recovery_dir.startswith(_RECOVERY_PREFIX)
        or "/" in recovery_dir
        or "\\" in recovery_dir
    ):
        _fail("pending recovery marker has unsafe paths")
    names = {"before-events.jsonl", "after-events.jsonl", "before-state.db", "after-state.db"}
    if not isinstance(value["files"], dict) or set(value["files"]) != names:
        _fail("pending recovery marker is malformed")
    return value


def _staged(state_dir: Path, marker: dict[str, Any]) -> dict[str, Path]:
    recovery = state_dir / marker["recovery_dir"]
    _directory(recovery)
    paths = {name: recovery / name for name in marker["files"]}
    for name, path in paths.items():
        digest = marker["files"][name]
        if not isinstance(digest, str) or _sha(_bounded_bytes(path)) != digest:
            _fail("pending recovery artifacts no longer match their marker")
    return paths


def _same_identity(path: Path, expected: Any) -> bool:
    entry = _regular(path)
    return (
        isinstance(expected, dict)
        and entry is not None
        and expected == {"dev": entry.st_dev, "ino": entry.st_ino}
    )


def _copy_log_in_place(path: Path, target: bytes) -> None:
    _regular(path)
    try:
        with path.open("r+b") as fh:
            fh.truncate(0)
            for offset in range(0, len(target), 1024 * 1024):
                fh.write(target[offset : offset + 1024 * 1024])
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        _fail("event history publication failed")
    _fsync_directory(path.parent)


def _hook(phase: str) -> None:
    if _PHASE_HOOK is not None:
        _PHASE_HOOK(phase)


def _publish(state_dir: Path, marker: dict[str, Any], paths: dict[str, Path]) -> None:
    state_db = state_dir / "state.db"
    events = state_dir / "events.jsonl"
    _safe_sidecars(state_db)
    if not _same_identity(state_db, marker["db_identity"]) or not _same_identity(
        events, marker["log_identity"]
    ):
        _fail("live state inode changed while recovery was pending")
    target = _bounded_bytes(paths["after-events.jsonl"])
    current = _bounded_bytes(events)
    before = _bounded_bytes(paths["before-events.jsonl"])
    if current not in {before, target} and not target.startswith(current):
        _fail("event history changed outside the recoverable publication prefix")
    before_conn = _readonly_connection(paths["before-state.db"])
    after_conn = _readonly_connection(paths["after-state.db"])
    live = _readonly_connection(state_db)
    try:
        state = _table_rows(live)
        permitted = {
            _freeze_rows(_table_rows(before_conn)),
            _freeze_rows(_table_rows(after_conn)),
        }
        if _freeze_rows(state) not in permitted:
            _fail("live projection changed outside the recoverable publication")
    finally:
        before_conn.close()
        after_conn.close()
        live.close()
    if current != target:
        _copy_log_in_place(events, target)
    _hook("after_log")
    live_write = sqlite3.connect(str(state_db))
    live_write.row_factory = sqlite3.Row
    try:
        _publish_staged_projection(paths["after-state.db"], live_write)
        _checkpoint_and_fsync_live_projection(live_write, state_db)
        _safe_sidecars(state_db)
        with events.open("rb") as fh:
            validate_converged_event_log(live_write, fh)
    except (OSError, sqlite3.Error, ValueError):
        _fail("recovered projection failed strict verification")
    finally:
        live_write.close()
    _hook("after_db")
    marker_path = state_dir / _MARKER
    _regular(marker_path)
    marker_path.unlink()
    _fsync_directory(state_dir)


def _freeze_rows(rows: dict[str, list[str]]) -> tuple[tuple[str, tuple[str, ...]], ...]:
    return tuple(sorted((name, tuple(items)) for name, items in rows.items()))


def _same_except_event(
    before: dict[str, list[str]], after: dict[str, list[str]], event_id: str
) -> bool:
    if set(before) != set(after):
        return False
    for name, rows in before.items():
        if name != "events" and rows != after[name]:
            return False
    additions = set(after["events"]) - set(before["events"])
    if (
        len(after["events"]) != len(before["events"]) + 1
        or not set(before["events"]).issubset(after["events"])
        or len(additions) != 1
    ):
        return False
    try:
        return json.loads(next(iter(additions))).get("id") == event_id
    except (TypeError, ValueError):
        return False


def _resume(state_dir: Path, receipt_path: Path | None) -> None:
    from anvil.state.sqlite import _schema_initialization_lock

    with _schema_initialization_lock(
        str(state_dir / "state.db"),
        str(state_dir / "events.jsonl"),
        allow_event_recovery=True,
    ):
        marker = _marker(state_dir)
        if receipt_path is not None:
            _, digest = _receipt(receipt_path)
            if digest != marker["receipt_sha256"]:
                _fail("reviewed receipt does not match the pending recovery")
        _safe_sidecars(state_dir / "state.db")
        if not _same_identity(state_dir / "state.db", marker["db_identity"]) or not _same_identity(
            state_dir / "events.jsonl", marker["log_identity"]
        ):
            _fail("live state inode changed while recovery was pending")
        paths = _staged(state_dir, marker)
        _publish(state_dir, marker, paths)


def _apply(state_dir: Path, receipt_path: Path) -> None:
    from anvil.state.sqlite import _schema_initialization_lock

    with _schema_initialization_lock(
        str(state_dir / "state.db"),
        str(state_dir / "events.jsonl"),
        allow_event_recovery=True,
    ):
        _safe_sidecars(state_dir / "state.db")
        plan = _plan(state_dir, receipt_path)
        source = _readonly_connection(state_dir / "state.db")
        try:
            _replay_and_prove(state_dir, plan, source)
            marker = _stage(state_dir, plan, source)
        finally:
            source.close()
        _write_marker(state_dir, marker)
        _hook("after_marker")
        _publish(state_dir, marker, _staged(state_dir, marker))


def _emit(json_output: bool, *, status: str, event_id: str | None = None) -> None:
    data = {"ok": True, "command": _COMMAND, "data": {"status": status}}
    if event_id is not None:
        data["data"]["event_id"] = event_id
    if json_output:
        typer.echo(json.dumps(data, separators=(",", ":")))
    else:
        typer.echo(f"Local event recovery {status}.")


def repair_local_event(
    receipt: Path | None = typer.Option(None, "--receipt", help="Reviewed recovery receipt."),  # noqa: B008
    apply: bool = typer.Option(False, "--apply", help="Publish the verified recovery."),  # noqa: B008
    resume: bool = typer.Option(False, "--resume", help="Resume the pending recovery."),  # noqa: B008
    exclusive_access: bool = typer.Option(  # noqa: B008
        False,
        "--exclusive-access",
        help="Attest ALL peer Anvil clients and operations are stopped until completion.",
    ),
    cwd: Path | None = typer.Option(None, "--cwd", hidden=True),  # noqa: B008
    json_output: bool = typer.Option(False, "--json"),  # noqa: B008
) -> None:
    """Recover exactly one audited local event omission, previewing by default."""
    state_dir = _resolve_state_dir(cwd)
    _require_state_dir(state_dir, command=_COMMAND, json_output=json_output)
    if apply and resume:
        error = "choose either --apply or --resume"
    elif (apply or resume) and not exclusive_access:
        error = "--exclusive-access is required for --apply and --resume"
    elif not resume and receipt is None:
        error = "--receipt is required unless --resume is used"
    else:
        error = None
    if error is not None:
        if json_output:
            typer.echo(
                json.dumps(
                    {
                        "ok": False,
                        "command": _COMMAND,
                        "error": {"code": "bad_request", "message": error},
                    },
                    separators=(",", ":"),
                )
            )
        else:
            typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=1)
    try:
        _directory(state_dir)
        _regular(state_dir / "state.db")
        _regular(state_dir / "events.jsonl")
        if not resume and os.path.lexists(state_dir / _MARKER):
            _fail("local event recovery is pending; use --resume")
        if resume:
            _resume(state_dir, receipt)
            _emit(json_output, status="resumed")
            return
        assert receipt is not None
        plan = _plan(state_dir, receipt)
        if apply:
            _apply(state_dir, receipt)
            _emit(json_output, status="applied", event_id=plan.event_id)
        else:
            _replay_and_prove(state_dir, plan)
            _emit(json_output, status="ready", event_id=plan.event_id)
    except _Refusal as exc:
        if json_output:
            typer.echo(
                json.dumps(
                    {
                        "ok": False,
                        "command": _COMMAND,
                        "error": {"code": "refused", "message": str(exc)},
                    },
                    separators=(",", ":"),
                )
            )
        else:
            typer.echo(f"Error: local event recovery refused: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except Exception:
        if json_output:
            typer.echo(
                json.dumps(
                    {
                        "ok": False,
                        "command": _COMMAND,
                        "error": {"code": "failed", "message": "local event recovery failed"},
                    },
                    separators=(",", ":"),
                )
            )
        else:
            typer.echo("Error: local event recovery failed", err=True)
        raise typer.Exit(code=1) from None


__all__ = ["repair_local_event"]
